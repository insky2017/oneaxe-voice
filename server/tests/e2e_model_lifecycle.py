"""Isolated real GPU, desktop and X11 model lifecycle check.

Run with --audio pointing to a consented 16 kHz mono PCM16 WAV. The script
uses a private DBus/Xvfb, temporary runtime, API port and PulseAudio source.
It never changes the installed services or prints credentials/transcripts.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave

import httpx
from websockets.sync.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oneaxe_voice.config import ROOT, Settings
from oneaxe_voice.desktop import control


def wait_for(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
        except (httpx.ConnectError, httpx.ConnectTimeout, FileNotFoundError,
                ConnectionRefusedError):
            result = None
        if result:
            return result
        time.sleep(.2)
    raise AssertionError("timed out waiting for model lifecycle condition")


def run(*command):
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          timeout=10).stdout.strip()


def gpu_pids():
    return {int(pid) for pid in run("nvidia-smi", "--query-compute-apps=pid",
                                    "--format=csv,noheader").splitlines()}


def gpu_group(worker_pid):
    if not worker_pid:
        return set()
    members = {int(pid) for pid, group in
               (line.split() for line in run("ps", "-eo", "pid=,pgid=").splitlines())
               if int(group) == worker_pid}
    return members & gpu_pids()


class Evidence:
    """Retain progress without recording API tokens or recognized text."""

    def __init__(self, runtime, full_idle):
        self.runtime = runtime
        self.started = time.monotonic()
        self.value = {"schema_version": 1, "status": "running",
                      "started_at": datetime.now(timezone.utc).isoformat(),
                      "runtime_dir": str(runtime), "full_idle_requested": full_idle,
                      "checks": {"full_idle_checked": False},
                      "timings_seconds": {}, "checkpoints": []}
        self.begin("setup")

    def persist(self, final=False):
        targets = [self.runtime / "model-lifecycle-e2e.json"]
        if final:
            targets.append(ROOT / "work/model-lifecycle-e2e.json")
        for path in targets:
            fd, name = tempfile.mkstemp(prefix=".lifecycle-evidence-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(self.value, output, indent=2, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)

    def begin(self, stage):
        self.value["current_stage"] = stage
        self.persist()

    def checkpoint(self, name, **details):
        self.value["checkpoints"].append(
            {"name": name, "elapsed_seconds": round(time.monotonic() - self.started, 3),
             **details})
        self.persist()

    def checked(self, name, started=None, **details):
        self.value["checks"][name] = True
        if started is not None:
            self.value["timings_seconds"][name] = round(time.monotonic() - started, 3)
        self.checkpoint(name, **details)


def model_snapshot(status):
    return {key: status.get(key) for key in
            ("state", "mode", "model_loaded", "busy", "worker_pid", "device",
             "auto_unload", "idle_seconds")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--xvfb", default=shutil.which("Xvfb"))
    parser.add_argument("--display", default=":99")
    parser.add_argument("--full-idle", action="store_true",
                        help="Wait for the real 120-second automatic unload")
    args = parser.parse_args()
    if not args.xvfb:
        parser.error("Xvfb is required; pass --xvfb /path/to/Xvfb")
    if not os.environ.get("ONEAXE_TEST_PRIVATE_DBUS"):
        return subprocess.call(["dbus-run-session", "--", sys.executable, __file__,
                                *sys.argv[1:]],
                               env=dict(os.environ, DISPLAY=args.display,
                                        ONEAXE_TEST_PRIVATE_DBUS="1"))
    with wave.open(str(args.audio), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        sample = audio.readframes(audio.getnframes())
    ROOT.joinpath("work").mkdir(exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="lifecycle-e2e-", dir=ROOT / "work"))
    evidence = Evidence(runtime, args.full_idle)
    print("Isolated lifecycle diagnostics:", runtime, flush=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, DISPLAY=args.display, XDG_SESSION_TYPE="x11",
               XDG_CONFIG_HOME=str(runtime / "config"), PYTHONPATH=str(ROOT),
               ONEAXE_VOICE_RUNTIME_DIR=str(runtime),
               ONEAXE_VOICE_API_URL=f"http://127.0.0.1:{port}",
               ONEAXE_VOICE_IDLE_SECONDS="0")
    os.environ.update(env)
    (runtime / "desktop.json").write_text(json.dumps({"mode": "r2t2",
                                                        "source": "oneaxe_lifecycle_e2e"}))
    # Settings.from_env reads process environment, while child processes use env.
    settings = Settings(runtime_dir=runtime, api_url=env["ONEAXE_VOICE_API_URL"])
    procs = []
    logs = []
    module = None
    writer = None
    target = None
    stop_feed = threading.Event()
    feed_errors = []
    seen_gpu = set()
    fifo = runtime / "audio.pipe"
    outcome = "failed"
    exit_code = 1

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    previous_sigterm = signal.signal(signal.SIGTERM, interrupted)

    def start(name, command):
        log = (runtime / f"{name}.log").open("w")
        logs.append(log)
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log,
                                start_new_session=True)
        procs.append(proc)
        return proc

    def api_status():
        with httpx.Client(base_url=settings.api_url,
                          headers={"Authorization": "Bearer " + settings.token_path.read_text().strip()},
                          trust_env=False, timeout=10) as client:
            response = client.get("/api/dictation/status")
            response.raise_for_status()
            return response.json()

    def post(path, payload=None):
        with httpx.Client(base_url=settings.api_url,
                          headers={"Authorization": "Bearer " + settings.token_path.read_text().strip()},
                          trust_env=False, timeout=20) as client:
            return client.post(path, json=payload)

    def model_ready():
        status = api_status()
        if not (status["mode"] == "r2t2" and status["state"] == "ready"
                and status["model_loaded"] and not status["busy"]
                and status["device"] == "cuda:0" and status["worker_pid"]):
            return None
        desktop = control(settings, "status")
        cached = desktop.get("model_status", {})
        if (desktop.get("state") == "idle" and not desktop.get("preparing")
                and not desktop.get("model_unloading") and not desktop.get("model_error")
                and desktop.get("elapsed_seconds") == 0
                and cached.get("state") == "ready" and cached.get("mode") == "r2t2"
                and cached.get("model_loaded") and not cached.get("busy")
                and cached.get("worker_pid") == status["worker_pid"]
                and cached.get("auto_unload") == status["auto_unload"]
                and cached.get("idle_seconds") == status["idle_seconds"]):
            return status
        return None

    def model_unloaded():
        status = api_status()
        if not (status["state"] == "unloaded" and not status["model_loaded"]
                and not status["busy"] and status["worker_pid"] is None):
            return None
        desktop = control(settings, "status")
        cached = desktop.get("model_status", {})
        if (not desktop.get("preparing") and not desktop.get("model_unloading")
                and not desktop.get("model_error") and cached.get("state") == "unloaded"
                and not cached.get("model_loaded") and not cached.get("busy")
                and cached.get("worker_pid") is None and cached.get("mode") == status["mode"]
                and cached.get("auto_unload") == status["auto_unload"]
                and cached.get("idle_seconds") == status["idle_seconds"]):
            return status
        return None

    def observe_gpu(worker_pid):
        pids = wait_for(lambda: gpu_group(worker_pid), 15)
        seen_gpu.update(pids)
        return pids

    def released_gpu(pids):
        wait_for(lambda: not pids.intersection(gpu_pids()), 20)
        return {"gpu_pids": sorted(pids), "remaining_gpu_pids": []}

    def unload_model(check):
        evidence.begin(check)
        status = wait_for(model_ready, 250)
        pids = observe_gpu(status["worker_pid"])
        started = time.monotonic()
        control(settings, "model_unload")
        unloaded = wait_for(model_unloaded, 20)
        evidence.checkpoint(check + "_api_and_desktop_unloaded", api=model_snapshot(unloaded))
        evidence.checked(check, started, api=model_snapshot(unloaded),
                         **released_gpu(pids))

    def feed():
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                pcm = sample + b"\0" * 64000
                started = time.monotonic()
                for pos in range(0, len(pcm), 640):
                    if stop_feed.is_set():
                        break
                    block = memoryview(pcm[pos:pos + 640])
                    while block and not stop_feed.is_set():
                        try:
                            block = block[os.write(fd, block):]
                        except BlockingIOError:
                            stop_feed.wait(.01)
                    stop_feed.wait(max(0, started + (pos + 640) / 32000 - time.monotonic()))
            finally:
                os.close(fd)
        except OSError as exc:
            if not stop_feed.is_set():
                feed_errors.append(type(exc).__name__)

    try:
        evidence.value["baseline_gpu_pids"] = sorted(gpu_pids())
        xserver = start("xvfb", [args.xvfb, args.display, "-screen", "0", "1280x800x24",
                                   "-nolisten", "tcp", "-ac"])
        time.sleep(1)
        assert xserver.poll() is None, "Xvfb failed; choose an unused --display"
        start("xfwm", ["xfwm4", "--replace", "--compositor=off"])
        subprocess.run([sys.executable, "-m", "oneaxe_voice.cli", "init"], env=env,
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        evidence.begin("startup_preheat")
        started = time.monotonic()
        api = start("api", [sys.executable, "-m", "uvicorn", "oneaxe_voice.server:create_app",
                            "--factory", "--host", "127.0.0.1", "--port", str(port),
                            "--no-access-log", "--no-proxy-headers"])
        start("desktop", [sys.executable, "-m", "oneaxe_voice.cli", "desktop-run"])
        start("hotkey", ["/usr/bin/python3", str(ROOT / "tests/x11_hotkey.py")])
        ready = wait_for(model_ready, 250)
        assert ready["device"] == "cuda:0" and ready["auto_unload"] is False
        first_pid = ready["worker_pid"]
        first_gpu = observe_gpu(first_pid)
        evidence.checked("startup_preheated", started, api=model_snapshot(ready),
                         gpu_pids=sorted(first_gpu))
        evidence.checked("gpu_worker_seen")

        unload_model("manual_unload_released_gpu")
        evidence.begin("status_polling_after_unload")
        for _ in range(5):
            assert model_unloaded(), "status read reloaded the model or returned a stale desktop cache"
            time.sleep(.3)
        evidence.checked("status_did_not_reload")

        # Selecting the active mode must reload it after an explicit unload.
        evidence.begin("same_mode_reload")
        started = time.monotonic()
        control(settings, "configure", mode="r2t2")
        ready = wait_for(model_ready, 250)
        assert ready["worker_pid"] != first_pid
        reloaded_gpu = observe_gpu(ready["worker_pid"])
        evidence.checked("same_mode_reloaded", started, api=model_snapshot(ready),
                         gpu_pids=sorted(reloaded_gpu))

        evidence.begin("enable_idle_policy")
        control(settings, "configure", auto_unload=True)
        wait_for(lambda: api_status()["auto_unload"] is True)
        assert api_status()["idle_seconds"] == 120
        policy_path = runtime / "model-policy.json"
        assert json.loads(policy_path.read_text())["auto_unload"] is True
        assert policy_path.stat().st_mode & 0o777 == 0o600
        evidence.checkpoint("idle_policy_enabled", idle_seconds=120)
        if args.full_idle:
            evidence.begin("real_120_second_idle_unload")
            # A real prepare resets last_used without replacing the loaded worker.
            idle_started = time.monotonic()
            response = post("/api/dictation/prepare", {"mode": "r2t2"})
            response.raise_for_status()
            ready = wait_for(model_ready, 15)
            idle_gpu = observe_gpu(ready["worker_pid"])
            evidence.checkpoint("idle_clock_reset", api=model_snapshot(ready),
                                gpu_pids=sorted(idle_gpu))
            last_heartbeat = time.monotonic()
            transition = None
            last_ready_at = None

            def idle_unloaded():
                nonlocal last_heartbeat, transition, last_ready_at
                status = api_status()
                elapsed = time.monotonic() - idle_started
                assert status["auto_unload"] and status["idle_seconds"] == 120
                if status["state"] in {"unloading", "unloaded"}:
                    assert elapsed >= 120, "automatic unload started before the real 120-second timeout"
                    if transition is None:
                        transition = elapsed
                        evidence.checkpoint("idle_unload_observed", idle_elapsed_seconds=round(elapsed, 3),
                                            api=model_snapshot(status))
                elif status["state"] == "ready":
                    last_ready_at = elapsed
                else:
                    raise AssertionError("unexpected model state during idle wait")
                if time.monotonic() - last_heartbeat >= 15:
                    last_heartbeat = time.monotonic()
                    evidence.checkpoint("idle_wait", idle_elapsed_seconds=round(elapsed, 3),
                                        api=model_snapshot(status))
                return model_unloaded() if status["state"] == "unloaded" else None

            unloaded = wait_for(idle_unloaded, 140)
            evidence.checked("full_idle_checked", idle_started, api=model_snapshot(unloaded),
                             configured_idle_seconds=120,
                             last_ready_elapsed_seconds=round(last_ready_at, 3),
                             unload_observed_elapsed_seconds=round(transition, 3),
                             **released_gpu(idle_gpu))
            evidence.begin("reload_after_idle_unload")
            started = time.monotonic()
            control(settings, "model_load")
            ready = wait_for(model_ready, 250)
            evidence.checked("idle_unload_reloaded", started, api=model_snapshot(ready),
                             gpu_pids=sorted(observe_gpu(ready["worker_pid"])))

        evidence.begin("disable_idle_policy")
        control(settings, "configure", auto_unload=False)
        wait_for(lambda: api_status()["auto_unload"] is False)
        assert api_status()["idle_seconds"] == 0
        evidence.checked("idle_policy_disabled")

        # A real streaming session owns the engine until the socket closes.
        evidence.begin("stream_busy_unload")
        ready = wait_for(model_ready, 250)
        stream_gpu = observe_gpu(ready["worker_pid"])
        evidence.checkpoint("stream_model_ready", api=model_snapshot(ready), gpu_pids=sorted(stream_gpu))
        token = settings.token_path.read_text().strip()
        url = settings.api_url.replace("http://", "ws://") + "/api/dictation/stream"
        with connect(url, additional_headers={"Authorization": "Bearer " + token},
                     proxy=None, open_timeout=10) as ws:
            ws.send(json.dumps({"mode": "r2t2"}))
            assert json.loads(ws.recv(timeout=15))["type"] == "ready"
            assert api_status()["busy"]
            assert post("/api/dictation/unload").status_code == 429
            ws.send("cancel")
        wait_for(lambda: not api_status()["busy"], 20)
        evidence.checked("stream_busy_unload_rejected", status_code=429)

        # Exercise the actual shortcut and recording state with the private source.
        evidence.begin("f8_model_load")
        fifo.unlink(missing_ok=True)
        module = run("pactl", "load-module", "module-pipe-source",
                     "source_name=oneaxe_lifecycle_e2e", "file=" + str(fifo),
                     "format=s16le", "rate=16000", "channels=1")
        control(settings, "model_load")
        ready = wait_for(model_ready, 250)
        f8_gpu = observe_gpu(ready["worker_pid"])
        evidence.checkpoint("f8_model_ready", api=model_snapshot(ready), gpu_pids=sorted(f8_gpu))
        target_file = runtime / "input-target.json"
        target = start("input-target", [sys.executable,
                                         str(ROOT / "tests/input_target.py"), str(target_file)])
        window = run("xdotool", "search", "--sync", "--onlyvisible", "--class",
                     "OneAxeVoiceTest").splitlines()[-1]
        run("xdotool", "windowactivate", "--sync", window)
        run("xdotool", "mousemove", "--window", window, "180", "100", "click", "1")
        wait_for(lambda: target_file.exists(), 10)
        assert json.loads(target_file.read_text())["text"] == "", "input target was not initially empty"
        evidence.begin("f8_recording")
        started = time.monotonic()
        run("xdotool", "key", "F8")
        wait_for(lambda: control(settings, "status").get("state") == "recording", 25)
        writer = threading.Thread(target=feed)
        writer.start()
        assert control(settings, "status")["capture_active"]
        wait_for(lambda: api_status()["busy"], 20)
        assert post("/api/dictation/unload").status_code == 429
        evidence.checked("f8_busy_unload_rejected", status_code=429)
        writer.join(timeout=len(sample) / 32000 + 10)
        assert not writer.is_alive(), "audio feeder did not complete"
        assert not feed_errors, "audio feeder failed"
        evidence.checkpoint("f8_audio_feed_finished", audio_seconds=round(len(sample) / 32000, 3))
        evidence.begin("f8_tail_flush_and_delivery")
        run("xdotool", "key", "F8")

        def dictation_finished():
            status = control(settings, "status")
            return status if (status.get("state") in {"idle", "error"}
                              and not status.get("capture_active")
                              and not status.get("recognizing")
                              and status.get("elapsed_seconds") == 0) else None

        finished = wait_for(dictation_finished, 90)
        assert finished["state"] == "idle" and finished["last_error"] is None
        assert finished["last_action"] == "pasted" and not finished.get("paste_paused")

        def delivered_full_transcript():
            transcript = (runtime / "last-transcript.txt").read_text(encoding="utf-8")
            session = json.loads((runtime / "last-session.json").read_text(encoding="utf-8"))
            actual = json.loads(target_file.read_text(encoding="utf-8"))["text"]
            return (transcript, session, actual) if transcript and transcript == session["text"] == actual else None

        transcript, session, actual = wait_for(delivered_full_transcript, 20)
        records = session["segments"]
        sequences = [record["sequence"] for record in records]
        assert records and sequences == sorted(set(sequences))
        assert all(record["mode"] == "r2t2" for record in records)
        assert finished["segments_pasted"] == len(records)
        assert target.poll() is None, "input target exited before delivery"
        evidence.checked("f8_recording_passed", started, transcript_characters=len(transcript),
                         target_characters=len(actual), segments_saved=len(records),
                         segments_pasted=finished["segments_pasted"], full_text_matches=True,
                         transcript_sha256=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
                         target_sha256=hashlib.sha256(actual.encode("utf-8")).hexdigest())
        evidence.checked("busy_unload_rejected")

        unload_model("post_f8_unload_released_gpu")
        evidence.begin("post_f8_reload")
        started = time.monotonic()
        control(settings, "model_load")
        ready = wait_for(model_ready, 250)
        restart_gpu = observe_gpu(ready["worker_pid"])
        evidence.checked("post_f8_reloaded", started, api=model_snapshot(ready),
                         gpu_pids=sorted(restart_gpu))

        # The policy file belongs to the API and survives an API process restart.
        evidence.begin("api_restart_policy_persistence")
        started = time.monotonic()
        control(settings, "configure", auto_unload=True)
        wait_for(lambda: api_status()["auto_unload"] is True)
        os.killpg(api.pid, signal.SIGTERM)
        api.wait(timeout=20)
        evidence.checkpoint("api_stopped", **released_gpu(restart_gpu))
        start("api-restart", [sys.executable, "-m", "uvicorn",
                              "oneaxe_voice.server:create_app", "--factory", "--host",
                              "127.0.0.1", "--port", str(port), "--no-access-log",
                              "--no-proxy-headers"])
        persisted = wait_for(model_unloaded, 20)
        assert persisted["auto_unload"] is True
        assert persisted["idle_seconds"] == 120
        evidence.checked("policy_persisted", started, api=model_snapshot(persisted))
        outcome, exit_code = "passed", 0
    except BaseException as exc:
        outcome = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        if isinstance(exc, SystemExit) and isinstance(exc.code, int):
            exit_code = exc.code
        else:
            exit_code = 130 if outcome == "interrupted" else 1
        evidence.value["error"] = {"kind": type(exc).__name__,
                                   "stage": evidence.value["current_stage"],
                                   "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
        evidence.checkpoint("execution_" + outcome)
    finally:
        evidence.begin("cleanup")
        cleanup_errors = []
        stop_feed.set()
        if writer:
            writer.join(timeout=3)
            if writer.is_alive():
                cleanup_errors.append("audio_feeder_still_running")
        if module:
            try:
                result = subprocess.run(["pactl", "unload-module", module], check=False,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=10)
                if result.returncode:
                    cleanup_errors.append("pulse_module_unload_failed")
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_errors.append(type(exc).__name__)
        fifo.unlink(missing_ok=True)
        for proc in reversed(procs):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        for log in logs:
            log.close()
        try:
            clearance = released_gpu(seen_gpu)
            evidence.checked("cleanup_gpu_pids_cleared", **clearance)
        except Exception as exc:
            cleanup_errors.append(type(exc).__name__)
        if cleanup_errors:
            evidence.value["cleanup_errors"] = cleanup_errors
            if outcome == "passed":
                outcome, exit_code = "failed", 1
        evidence.value.update(status=outcome, finished_at=datetime.now(timezone.utc).isoformat(),
                              current_stage="finished", total_seconds=round(time.monotonic() - evidence.started, 3))
        evidence.checkpoint("cleanup_complete")
        evidence.persist(final=True)
        signal.signal(signal.SIGTERM, previous_sigterm)
    print(json.dumps({"status": outcome, **evidence.value["checks"],
                      "evidence": str(ROOT / "work/model-lifecycle-e2e.json")}), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
