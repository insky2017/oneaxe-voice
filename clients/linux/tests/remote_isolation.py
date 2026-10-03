"""Opt-in short PC/remote-disconnect and mobile-capacity isolation acceptance.

Uses existing credentials on each host. This proves process termination and
disconnect isolation, not an explicit V1 cancel control. Run after dual_stream
ends and dictation is paused. Never manages models or changes source audio.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from uuid import UUID

import dual_stream as shared


LOCAL_CREDENTIAL_ID = "daac0cbc-bb30-4bed-8352-c0e3fa0f5a87"
REMOTE_CREDENTIAL_ID = "b332c405-a34b-4bd9-8650-4e06a82962bf"
CAPACITY_MESSAGE = "\u6682\u65f6\u6ca1\u6709\u53ef\u7528\u7684\u79fb\u52a8\u4f1a\u8bdd\u540d\u989d"

REMOTE_SHORT_PROBE = "STATIC_ERRORS = " + repr(shared.CLIENT_STATIC_ERRORS) + "\n" + r'''
import contextlib, hashlib, json, os, pathlib, signal, subprocess, sys, tempfile, threading

child = None
terminated = threading.Event()
def stop_child():
    if child is not None and child.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()

def lease():
    while sys.stdin.buffer.read(1):
        pass
    terminated.set()
    stop_child()

try:
    source, seconds, keyword = pathlib.Path(sys.argv[1]).expanduser(), int(sys.argv[2]), sys.argv[3]
    if not source.is_file() or not 2 <= seconds <= 10:
        raise ValueError()
    with source.open("rb") as stream:
        pcm = stream.read(seconds * 32000)
    if len(pcm) < 64000 or len(pcm) % 2:
        raise ValueError()
    with tempfile.TemporaryDirectory(prefix="oneaxe-voice-isolation-") as directory:
        clipped = pathlib.Path(directory) / "short.pcm"
        descriptor = os.open(clipped, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(pcm)
        runtime = "/run/user/" + str(os.getuid())
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = runtime
        environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + runtime + "/bus"
        binary = pathlib.Path.home() / ".local/bin/oneaxe-voice-linux"
        child = subprocess.Popen([str(binary), "--probe-pcm", str(clipped),
            "--expect", keyword, "--reject", "\u5927\u5bb6\u597d"], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        threading.Thread(target=lease, daemon=True).start()
        _, error = child.communicate()
        code = next((code for code, phrase in STATIC_ERRORS.items()
                     if phrase.encode() in error), "UNKNOWN_PROBE_ERROR") if child.returncode > 0 else None
        print(json.dumps({"ok": True, "source_sha256": hashlib.sha256(pcm).hexdigest(),
            "pcm_bytes": len(pcm), "rust_exit_code": child.returncode,
            "process_group_terminated": terminated.is_set(),
            "normal_probe_completion": child.returncode == 0,
            "failure_code": code}), flush=True)
except BaseException:
    print(json.dumps({"ok": False}), flush=True)
    sys.exit(1)
finally:
    stop_child()
'''


class ObservedSocket:
    def __init__(self, socket, observations):
        self.socket = socket
        self.observations = observations
        self.fixed = ""
        self.sequence = 0

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def observe(self, raw):
        event = json.loads(raw)
        seq = event.get("seq", 0)
        if type(seq) is not int or seq <= self.sequence:
            return
        self.sequence = seq
        if event.get("type") == "ready":
            self.observations["ready_at"] = time.monotonic()
            self.observations["session_id"] = event["session_id"]
        elif event.get("type") == "partial" and event.get("text") != self.fixed:
            self.fixed = event["text"]
            self.observations["fixed_update_times"].append(time.monotonic())

    async def recv(self):
        raw = await self.socket.recv()
        self.observe(raw)
        return raw

    def __aiter__(self):
        return self.receive()

    async def receive(self):
        async for raw in self.socket:
            self.observe(raw)
            yield raw


def observe_pc(helper, observations):
    connect = helper.connect

    @asynccontextmanager
    async def observed_connect(*args, **kwargs):
        async with connect(*args, **kwargs) as socket:
            yield ObservedSocket(socket, observations)

    # This imported helper object exists only in this test's Python process.
    helper.connect = observed_connect


def require_preserved(status, before, pc_id):
    shared.require_model_preserved(status, before)
    sessions = status.get("active_sessions")
    shared.require(isinstance(sessions, list) and len(sessions) <= 2, "UNEXPECTED_SESSION_COUNT")
    pc = [row for row in sessions if row.get("kind") == "pc"]
    if pc_id is not None and pc:
        shared.require(len(pc) == 1 and pc[0].get("session_id") == pc_id, "PC_SESSION_CHANGED")
    return sessions


async def guard(client, runtime, before, observations, pc_task, stop, report):
    started_at = time.monotonic()
    report.update({"guard_samples": 0, "guard_elapsed_seconds": 0,
        "guard_pc_active_observed": False, "guard_pc_absence_rechecks": 0})
    try:
        while not stop.is_set():
            shared.require_desktop_idle(await shared.desktop_status(runtime))
            status = await shared.service_status(client)
            report["guard_samples"] += 1
            report["guard_elapsed_seconds"] = round(time.monotonic() - started_at, 4)
            sessions = require_preserved(status, before, observations.get("session_id"))
            pc_present = bool(observations.get("session_id")) and any(
                row.get("kind") == "pc" and row.get("session_id") == observations["session_id"]
                for row in sessions)
            if pc_present:
                report["guard_pc_active_observed"] = True
            # A status request may predate ready; only confirm loss after seeing the PC lease.
            if report["guard_pc_active_observed"] and not pc_present and not pc_task.done():
                elapsed = time.monotonic() - observations["ready_at"]
                report["guard_pc_elapsed_seconds"] = round(elapsed, 4)
                # Finishing may remove its lease before helper.session returns.
                if elapsed < report["pc_capture_seconds"] - 0.5:
                    report["guard_pc_absence_rechecks"] += 1
                    status = await shared.service_status(client)
                    report["guard_samples"] += 1
                    sessions = require_preserved(status, before, observations["session_id"])
                    if not pc_task.done() and time.monotonic() - observations["ready_at"] < report["pc_capture_seconds"] - 0.5:
                        shared.require(any(row.get("kind") == "pc"
                            and row.get("session_id") == observations["session_id"] for row in sessions),
                            "PC_SESSION_DISAPPEARED")
            try:
                await asyncio.wait_for(stop.wait(), 0.2)
            except asyncio.TimeoutError:
                pass
    finally:
        report["guard_elapsed_seconds"] = round(time.monotonic() - started_at, 4)
        if "ready_at" in observations:
            report["guard_pc_elapsed_seconds"] = round(time.monotonic() - observations["ready_at"], 4)


async def wait_mobile(client, before, observations, remote_process, *, present):
    deadline = time.monotonic() + (8 if present else 10)
    while time.monotonic() < deadline:
        status = await shared.service_status(client)
        sessions = require_preserved(status, before, observations["session_id"])
        shared.require(any(row.get("kind") == "pc" and row.get("session_id") == observations["session_id"]
                           for row in sessions), "PC_SESSION_NOT_PRESERVED")
        mobile = [row for row in sessions if row.get("kind") == "mobile"]
        if present and len(mobile) == 1 and mobile[0].get("state") == "active":
            return mobile[0]["session_id"]
        if not present and not mobile and status.get("mobile_slots_available") == 1:
            return None
        if present:
            shared.require(remote_process.returncode is None, "REMOTE_PROBE_ENDED_EARLY")
        await asyncio.sleep(0.05)
    raise shared.TestFailure("REMOTE_SLOT_START_TIMEOUT" if present else "REMOTE_SLOT_RELEASE_TIMEOUT")


async def check_capacity(binary, pcm, processes):
    with tempfile.TemporaryDirectory(prefix="oneaxe-voice-capacity-") as directory:
        path = Path(directory) / "short.pcm"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(pcm[:32000])
        process = await asyncio.create_subprocess_exec(str(binary), "--probe-pcm", str(path),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        processes.append(process)
        try:
            output, error = await asyncio.wait_for(process.communicate(), 5)
            matched = CAPACITY_MESSAGE.encode() in error
            if process.returncode != 2 or not matched or output:
                code = next((code for code, phrase in shared.CLIENT_STATIC_ERRORS.items()
                             if phrase.encode() in error), "UNKNOWN_PROBE_ERROR")
                diagnostics = {"probe_exit_code": process.returncode, "client_failure_code": code}
                if code in shared.CLIENT_STATIC_ERRORS:
                    diagnostics["static_message"] = shared.CLIENT_STATIC_ERRORS[code]
                raise shared.TestFailure("LOCAL_CAPACITY_REJECTION_MISSING", diagnostics)
            return {"exit_code": process.returncode, "capacity_message_matched": matched,
                "pcm_probe_used": True, "successful_session_report_emitted": False,
                "microphone_capture_created": False,
                "microphone_evidence_basis": "PCM path rejected by capabilities before session and capture creation"}
        finally:
            await shared.stop_remote(process)


async def supervise(coroutine, watcher):
    task = asyncio.create_task(coroutine)
    try:
        done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            watcher.result()
            raise shared.TestFailure("DESKTOP_MONITOR_STOPPED")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def wait_pc_ready(observations, pc):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "session_id" in observations:
            return
        if pc.done():
            pc.result()
            raise shared.TestFailure("PC_ENDED_BEFORE_READY")
        await asyncio.sleep(0.01)
    raise shared.TestFailure("PC_READY_TIMEOUT")


async def run(args):
    report = {"passed": False, "pc_capture_seconds": args.seconds,
        "remote_source_seconds": args.remote_seconds,
        "termination_method": "process_group_termination_and_websocket_disconnect",
        "explicit_v1_cancel_tested": False, "remote_sent_samples_measured": False,
        "credential_separation_basis": "previous_independent_CLI_issuance_and_keyring_import",
        "credential_keyrings_read_by_test": False,
        "credential_ids_sha256": {"local": shared.digest(args.local_credential_id.encode()),
                                  "remote": shared.digest(args.remote_credential_id.encode())}}
    observations = {"fixed_update_times": []}
    report["remote_expected_keyword_sha256"] = shared.digest(args.remote_keyword.encode())
    tasks, processes, stop = [], [], asyncio.Event()
    pc = remote = None
    try:
        shared.require(UUID(args.local_credential_id) != UUID(args.remote_credential_id), "CREDENTIAL_IDS_NOT_DISTINCT")
        settings, helper, helper_hash = shared.load_voice(args.voice_repo)
        report["helper_source_sha256"] = helper_hash
        observe_pc(helper, observations)
        audio = helper.pcm(args.pc_audio)
        shared.require(len(audio) >= 32000, "PC_SOURCE_TOO_SHORT")
        report["pc_source_audio_sha256"] = shared.digest(audio)
        shared.require_desktop_idle(await shared.desktop_status(settings.runtime_dir))
        token = settings.token_path.read_text().strip()
        shared.require(token and "\r" not in token and "\n" not in token, "PC_TOKEN_INVALID")
        async with helper.httpx.AsyncClient(base_url=settings.api_url, trust_env=False,
                follow_redirects=False, timeout=5, headers={"Authorization": "Bearer " + token}) as client:
            before = await shared.service_status(client)
            shared.require_ready(before, args.expected_mode)
            report["model_mode"] = before["mode"]
            report["remote_capabilities_gate"] = "installed_Rust_client_before_audio"
            generation = (before["server_instance_id"], before["model_generation"])
            pc = asyncio.create_task(helper.session(settings.api_url, token, audio, args.seconds,
                "pc", generation))
            tasks.append(pc)
            watcher = asyncio.create_task(guard(client, settings.runtime_dir, before, observations, pc, stop, report))
            tasks.append(watcher)
            await supervise(shared.wait_for_pc(client, settings.runtime_dir, pc), watcher)
            await supervise(wait_pc_ready(observations, pc), watcher)
            command = shlex.join(("python3", "-u", "-c", REMOTE_SHORT_PROBE, args.remote_pcm, str(args.remote_seconds), args.remote_keyword))
            remote = await asyncio.create_subprocess_exec("ssh", "-T", "-o", "BatchMode=yes", "-o",
                "ConnectTimeout=10", shared.REMOTE_HOST, command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            processes.append(remote)
            remote_id = await supervise(wait_mobile(client, before, observations, remote, present=True), watcher)
            active_at = time.monotonic()
            report["remote_session_id_sha256"] = shared.digest(remote_id.encode())
            report["capacity"] = await supervise(check_capacity(args.local_client, audio, processes), watcher)
            await supervise(asyncio.sleep(max(0, active_at + 2 - time.monotonic())), watcher)
            status = await shared.service_status(client)
            sessions = require_preserved(status, before, observations["session_id"])
            shared.require(any(row.get("session_id") == remote_id and row.get("state") == "active"
                               for row in sessions), "REMOTE_NOT_ACTIVE_AT_TERMINATION")
            requested_at = time.monotonic()
            await supervise(shared.stop_remote(remote), watcher)
            remote_result = await supervise(shared.collect_remote(remote), watcher)
            report["remote"] = {key: remote_result[key] for key in ("source_sha256", "pcm_bytes",
                "rust_exit_code", "process_group_terminated", "normal_probe_completion")}
            report["remote"]["actual_source_seconds"] = remote_result["pcm_bytes"] / 32000
            code = remote_result.get("failure_code")
            if code in shared.CLIENT_STATIC_ERRORS:
                report["remote"]["failure_code"] = code
                report["remote"]["static_message"] = shared.CLIENT_STATIC_ERRORS[code]
            shared.require(remote_result.get("process_group_terminated") is True
                           and remote_result.get("normal_probe_completion") is False
                           and remote_result.get("rust_exit_code", 0) < 0, "REMOTE_TERMINATION_NOT_OBSERVED")
            await supervise(wait_mobile(client, before, observations, remote, present=False), watcher)
            released_at = time.monotonic()
            report["remote_active_before_termination_seconds"] = round(requested_at - active_at, 4)
            report["remote_slot_release_seconds"] = round(released_at - requested_at, 4)
            async def finish_pc():
                return await asyncio.wait_for(asyncio.shield(pc), args.seconds + 60)
            metrics, fixed = await supervise(finish_pc(), watcher)
            updates_after = [at for at in observations["fixed_update_times"] if at > released_at]
            report["pc"] = shared.numeric_metrics(metrics)
            report["pc_fixed_updates_after_remote_slot_release"] = len(updates_after)
            report["first_pc_fixed_update_after_release_ms"] = (
                round((updates_after[0] - released_at) * 1000, 2) if updates_after else None)
            report["pc_source_audio_sha256"] = shared.digest(audio)
            shared.require_desktop_idle(await shared.desktop_status(settings.runtime_dir))
            after = await shared.service_status(client)
            cleanup_deadline = time.monotonic() + 5
            while after.get("active_sessions") and time.monotonic() < cleanup_deadline:
                require_preserved(after, before, observations["session_id"])
                await asyncio.sleep(0.1)
                after = await shared.service_status(client)
            require_preserved(after, before, observations["session_id"])
            report["checks"] = {
                "credentials_previously_issued_separately": args.local_credential_id != args.remote_credential_id,
                "capacity_rejected_without_capture": report["capacity"]["capacity_message_matched"],
                "remote_process_terminated": remote_result["process_group_terminated"],
                "remote_slot_released": after.get("mobile_slots_available") == 1,
                "pc_session_unchanged": metrics["session_id"] == observations["session_id"],
                "pc_live_fixed_text_after_disconnect": bool(updates_after),
                "pc_full_finish": metrics.get("complete") is True and metrics.get("final_reason") == "finished"
                    and metrics.get("sent_samples") == args.seconds * 16000
                    and metrics.get("processed_samples") == args.seconds * 16000,
                "pc_expected_chinese": shared.PC_KEYWORD in fixed,
                "pc_remote_keyword_absent": args.remote_keyword.casefold() not in fixed.casefold(),
                "no_remaining_test_sessions": after.get("active_sessions") == [],
            }
            report["passed"] = all(report["checks"].values())
    except BaseException as error:
        report["failure"] = shared.safe_failure(error)
        report["failure_code"] = report["failure"]["code"]
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for process in processes:
            await shared.stop_remote(process)
        report["pc_partial_observations"] = {"ready_observed": "ready_at" in observations,
            "fixed_updates_seen": len(observations["fixed_update_times"])}
        if pc is not None and pc.done() and not pc.cancelled() and pc.exception() is None:
            metrics, _ = pc.result()
            report["pc"] = shared.numeric_metrics(metrics)
        if remote is not None and "remote" not in report:
            try:
                value = await shared.collect_remote(remote)
                report["remote"] = {key: value[key] for key in ("source_sha256", "pcm_bytes",
                    "rust_exit_code", "process_group_terminated", "normal_probe_completion")}
                code = value.get("failure_code")
                if code in shared.CLIENT_STATIC_ERRORS:
                    report["remote"]["failure_code"] = code
                    report["remote"]["static_message"] = shared.CLIENT_STATIC_ERRORS[code]
            except BaseException as error:
                report["remote_failure"] = shared.safe_failure(error)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-repo", type=Path, required=True)
    parser.add_argument("--pc-audio", type=Path, required=True)
    parser.add_argument("--remote-pcm", required=True)
    parser.add_argument("--remote-keyword", default=shared.REMOTE_KEYWORD)
    parser.add_argument("--expected-mode", help="Optional assertion for the server's current mode; does not select a model")
    parser.add_argument("--local-client", type=Path, default=Path.home() / ".local/bin/oneaxe-voice-linux")
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument("--remote-seconds", type=int, default=10)
    parser.add_argument("--local-credential-id", default=LOCAL_CREDENTIAL_ID)
    parser.add_argument("--remote-credential-id", default=REMOTE_CREDENTIAL_ID)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 45 or not 2 <= args.remote_seconds <= 10:
        parser.error("PC duration must be 30-45 seconds; remote source must be 2-10 seconds")
    if not args.remote_keyword.strip():
        parser.error("remote keyword must not be empty")
    result = asyncio.run(run(args))
    shared.save_report(args.output, result)
    print(json.dumps({"passed": result["passed"], "output": str(args.output),
                      "failure_code": result.get("failure_code")}))
    raise SystemExit(0 if result["passed"] else 1)
