"""Opt-in real-clock PC/e15l dual-stream acceptance; never manages models.

Run with the Voice repository's existing .venv/bin/python after dictation is
paused. PC sends Chinese WAV audio for 620 seconds by default; e15l sends a
600-second English PCM file through the installed Rust/WSS client. Only
metrics, checks, and hashes are persisted. Microphone/GTK evidence is separate.
"""

import argparse
import asyncio
from contextlib import suppress
import hashlib
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import time
from types import SimpleNamespace


SAMPLE_RATE = 16000
PC_START_MARGIN_SECONDS = 20
MONITOR_INTERVAL_SECONDS = 0.5
REMOTE_HOST = "sky@e15l"
PC_KEYWORD = "\u5927\u5bb6\u597d"
REMOTE_KEYWORD = "mobile"
REMOTE_METRIC_KEYS = (
    "complete", "elapsed_ms", "fixed_characters", "fixed_updates", "first_fixed_ms",
    "sent_samples", "processed_samples", "max_buffered_samples", "max_observed_lag_samples",
    "expected_keyword_checked", "foreign_keyword_checked",
)
REMOTE_WRAPPER_ERRORS = {
    "UNKNOWN_PROBE_ERROR", "INVALID_PCM_SIZE", "INVALID_MODE", "INVALID_METRICS",
    "PROBE_OUTPUT_OVERSIZE", "REMOTE_CLIENT_START_FAILED",
}

CLIENT_STATIC_ERRORS = {
    "EXPECTED_KEYWORD_MISSING": "\u8bc6\u522b\u7ed3\u679c\u672a\u5305\u542b\u6d4b\u8bd5\u8981\u6c42\u7684\u5173\u952e\u6587\u5b57",
    "FOREIGN_KEYWORD_PRESENT": "\u8bc6\u522b\u7ed3\u679c\u51fa\u73b0\u53e6\u4e00\u58f0\u9053\u7684\u6d4b\u8bd5\u6587\u5b57",
    "FINAL_INCOMPLETE": "\u670d\u52a1\u7aef\u672a\u6b63\u5e38\u5b8c\u6210",
    "PROBE_TIMEOUT": "\u9a8c\u8bc1\u8d85\u65f6",
    "SESSION_CANCELLED": "\u9a8c\u8bc1\u4f1a\u8bdd\u5df2\u53d6\u6d88",
    "EVENT_CHANNEL_CLOSED": "\u4e8b\u4ef6\u901a\u9053\u5df2\u5173\u95ed",
    "CAPACITY_EXCEEDED": "\u6682\u65f6\u6ca1\u6709\u53ef\u7528\u7684\u79fb\u52a8\u4f1a\u8bdd\u540d\u989d",
    "UNAUTHORIZED": "\u8bbe\u5907\u51ed\u636e\u65e0\u6548\u6216\u5df2\u540a\u9500",
    "FORBIDDEN": "\u8bbe\u5907\u51ed\u636e\u7f3a\u5c11\u6240\u9700\u6743\u9650",
    "MODEL_CHANGED": "PC \u6a21\u578b\u5df2\u53d8\u5316\uff0c\u8bf7\u91cd\u65b0\u67e5\u8be2\u540e\u5f00\u59cb",
    "MODEL_NOT_READY": "\u8bf7\u7b49\u5f85 PC \u6a21\u578b\u52a0\u8f7d\u5b8c\u6210",
    "SERVICE_UNAVAILABLE": "\u8bed\u97f3\u670d\u52a1\u6682\u4e0d\u53ef\u7528",
    "TLS_IDENTITY": "TLS \u8bc1\u4e66\u6216\u670d\u52a1\u8eab\u4efd\u9a8c\u8bc1\u5931\u8d25",
    "CONNECTION_LOST": "\u8fde\u63a5\u5df2\u65ad\u5f00\uff0c\u5df2\u6536\u5230\u6587\u5b57\u4fdd\u7559\uff0c\u5c3e\u90e8\u53ef\u80fd\u672a\u5b8c\u6210",
    "CLIENT_BUFFER_EXCEEDED": "\u672a\u53d1\u9001\u97f3\u9891\u8fbe\u5230 2 \u79d2\u4e0a\u9650\uff0c\u672c\u8f6e\u5df2\u505c\u6b62\uff0c\u5df2\u6536\u5230\u6587\u5b57\u4fdd\u7559",
    "KEYRING_UNAVAILABLE": "\u7cfb\u7edf\u5bc6\u94a5\u73af\u4e0d\u53ef\u7528\u6216\u5df2\u9501\u5b9a\uff0c\u51ed\u636e\u672a\u5199\u5165\u666e\u901a\u6587\u4ef6\u3002",
    "CREDENTIAL_MISSING": "\u5c1a\u672a\u914d\u7f6e\u8bbe\u5907\u51ed\u636e"
}

# Keeping SSH stdin open leases this child only; EOF kills its process group.
REMOTE_RUNNER = ("STATIC_ERRORS = " + repr(CLIENT_STATIC_ERRORS)
    + "\nMETRIC_KEYS = " + repr(REMOTE_METRIC_KEYS) + "\n") + r'''
import decimal, hashlib, json, math, os, pathlib, select, signal, subprocess, sys, threading

child = None
lease_thread = None
lease_stop = threading.Event()
def stop_child():
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        except ProcessLookupError:
            pass

def watch_lease():
    # Buffered stdin may retain its lock during interpreter shutdown.
    descriptor = sys.stdin.fileno()
    try:
        while not lease_stop.is_set():
            readable, _, _ = select.select([descriptor], [], [], 0.1)
            if readable and not os.read(descriptor, 1):
                stop_child()
                return
    except OSError:
        stop_child()

result = {}
stage = "INVALID_PCM_SIZE"
try:
    mode, source, seconds = sys.argv[1], pathlib.Path(sys.argv[2]).expanduser(), decimal.Decimal(sys.argv[3])
    keyword = sys.argv[4]
    if not seconds.is_finite() or seconds <= 0 or not source.is_file() or source.stat().st_size != seconds * 32000:
        raise ValueError("INVALID_PCM_SIZE")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    result = {"pcm_bytes": source.stat().st_size, "source_sha256": digest.hexdigest()}
    if mode == "probe":
        runtime = "/run/user/" + str(os.getuid())
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = runtime
        environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + runtime + "/bus"
        binary = pathlib.Path.home() / ".local/bin/oneaxe-voice-linux"
        stage = "REMOTE_CLIENT_START_FAILED"
        child = subprocess.Popen([str(binary), "--probe-pcm", str(source),
            "--expect", keyword, "--reject", "\u5927\u5bb6\u597d"],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        lease_thread = threading.Thread(target=watch_lease, daemon=True)
        lease_thread.start()
        output, error = child.communicate()
        result["probe_exit_code"] = child.returncode
        result["probe_stdout_bytes"] = len(output)
        result["probe_stderr_bytes"] = len(error)
        stage = "PROBE_FAILED"
        if child.returncode != 0:
            text = error.decode("utf-8", "replace")
            code = next((code for code, phrase in STATIC_ERRORS.items() if phrase in text), "UNKNOWN_PROBE_ERROR")
            raise ValueError(code)
        if len(output) > 16384:
            raise ValueError("PROBE_OUTPUT_OVERSIZE")
        stage = "INVALID_METRICS"
        metrics = json.loads(output)
        if not isinstance(metrics, dict):
            raise ValueError("INVALID_METRICS")
        selected = {key: metrics[key] for key in METRIC_KEYS if key in metrics
            and (metrics[key] is None or type(metrics[key]) in (bool, int, float))
            and (type(metrics[key]) is not float or math.isfinite(metrics[key]))}
        result["metrics"] = selected
        missing = [key for key in METRIC_KEYS if key not in metrics]
        boolean_keys = ("complete", "expected_keyword_checked", "foreign_keyword_checked")
        invalid = [key for key in METRIC_KEYS if key in metrics and not (
            key in boolean_keys and type(metrics[key]) is bool
            or key == "first_fixed_ms" and metrics[key] is None
            or key not in boolean_keys and type(metrics[key]) is int and metrics[key] >= 0)]
        if missing or invalid:
            result["missing_metric_keys"] = missing
            result["invalid_metric_keys"] = invalid
            raise ValueError("INVALID_METRICS")
        result["metrics_validated"] = True
    elif mode != "inspect":
        raise ValueError("INVALID_MODE")
    print(json.dumps({"ok": True, **result}), flush=True)
except BaseException as error:
    codes = set(STATIC_ERRORS) | {"UNKNOWN_PROBE_ERROR", "INVALID_PCM_SIZE", "INVALID_MODE", "INVALID_METRICS", "PROBE_OUTPUT_OVERSIZE"}
    candidate = error.args[0] if error.args else None
    code = candidate if isinstance(candidate, str) and candidate in codes else stage
    print(json.dumps({"ok": False, "failure_code": code, "failure_stage": stage,
        "exception_kind": type(error).__name__, **result}), flush=True)
    sys.exit(1)
finally:
    lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=3)
    stop_child()
'''


class TestFailure(Exception):
    def __init__(self, code, diagnostics=None):
        self.code = code
        self.diagnostics = diagnostics
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise TestFailure(code)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def load_voice(repo):
    repo = repo.expanduser().resolve()
    helper_path = repo / "tests/e2e_concurrent.py"
    require(helper_path.is_file(), "VOICE_HELPER_MISSING")
    sys.path.insert(0, str(repo))
    from oneaxe_voice.config import Settings
    spec = importlib.util.spec_from_file_location("voice_dual_e2e_helpers", helper_path)
    require(spec is not None and spec.loader is not None, "VOICE_HELPER_IMPORT")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    require(tuple(inspect.signature(helper.session).parameters) == (
        "url", "token", "audio", "seconds", "role", "generation", "cancel_after"),
        "VOICE_HELPER_SIGNATURE")
    return Settings.from_env(), helper, digest(helper_path.read_bytes())


async def desktop_status(runtime):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(runtime / "desktop.sock")), 2)
    except (FileNotFoundError, ConnectionRefusedError):
        return {"state": "stopped"}
    try:
        writer.write(b'{"action":"status"}\n')
        await asyncio.wait_for(writer.drain(), 2)
        raw = await asyncio.wait_for(reader.readline(), 2)
        require(0 < len(raw) <= 65536, "DESKTOP_STATUS_FORMAT")
        value = json.loads(raw)
        require(isinstance(value, dict) and "error" not in value, "DESKTOP_STATUS_FAILED")
        return value
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


def require_desktop_idle(status):
    if status.get("state") == "stopped":
        return
    require(all(status.get(key) is False for key in ("capture_active", "recognizing", "preparing")),
            "DESKTOP_RECORDING_OR_RECOGNIZING")
    require(not status.get("warming") and not status.get("model_unloading")
            and status.get("state") not in ("recording", "finishing"), "DESKTOP_NOT_IDLE")


async def service_status(client):
    response = await client.get("/api/dictation/status")
    require(response.status_code == 200, "SERVICE_STATUS_HTTP")
    value = response.json()
    require(isinstance(value, dict), "SERVICE_STATUS_FORMAT")
    return value


def require_ready(status):
    require(status.get("mode") == "r2t2" and status.get("model_loaded") is True
            and status.get("state") == "ready" and status.get("busy") is False
            and status.get("pc_busy") is False and status.get("active_sessions") == [],
            "SERVICE_NOT_READY_OR_BUSY")
    require(all(isinstance(status.get(key), str) and status[key]
                for key in ("server_instance_id", "model_generation")), "SERVICE_IDENTITY_MISSING")
    require(type(status.get("worker_pid")) is int, "SERVICE_WORKER_MISSING")


def ssh_command(mode, source, seconds, keyword=REMOTE_KEYWORD):
    remote = shlex.join(("python3", "-u", "-c", REMOTE_RUNNER, mode, source, str(seconds), keyword))
    return ("ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", REMOTE_HOST, remote)


async def collect_remote(process):
    async def bounded_output(stream):
        pieces, size = [], 0
        if stream is not None:
            while chunk := await stream.read(4096):
                if size < 16384:
                    pieces.append(chunk[:16384 - size])
                size += len(chunk)
        return b"".join(pieces), size

    (raw, size), (stderr, stderr_size), _ = await asyncio.gather(
        bounded_output(process.stdout), bounded_output(process.stderr), process.wait())
    diagnostics = {"ssh_exit_code": process.returncode, "runner_stdout_bytes": size,
        "runner_stderr_bytes": stderr_size}
    if b"Fatal Python error" in stderr and (b"finalizing" in stderr or b"interpreter shutdown" in stderr):
        diagnostics["stderr_reason"] = "PYTHON_INTERPRETER_SHUTDOWN"
    elif b"Permission denied" in stderr:
        diagnostics["stderr_reason"] = "SSH_AUTHENTICATION_FAILED"
    elif b"Could not resolve hostname" in stderr:
        diagnostics["stderr_reason"] = "SSH_HOST_RESOLUTION_FAILED"
    elif b"Connection timed out" in stderr:
        diagnostics["stderr_reason"] = "SSH_CONNECTION_TIMEOUT"
    elif stderr:
        diagnostics["stderr_reason"] = "UNCLASSIFIED_STDERR"
    if size > 16384:
        raise TestFailure("REMOTE_REPORT_OVERSIZE", {"failure_layer": "wrapper_report", **diagnostics})
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        layer = "ssh" if process.returncode == 255 else "wrapper_report"
        raise TestFailure("REMOTE_REPORT_INVALID", {"failure_layer": layer, **diagnostics}) from None
    if not isinstance(value, dict):
        raise TestFailure("REMOTE_REPORT_FAILED", {"failure_layer": "wrapper_report", **diagnostics})
    diagnostics["runner_report_ok"] = value.get("ok") is True
    for key in ("probe_exit_code", "probe_stdout_bytes", "probe_stderr_bytes", "pcm_bytes"):
        if type(value.get(key)) is int:
            diagnostics[key] = value[key]
    metrics = value.get("metrics")
    if isinstance(metrics, dict):
        selected = {key: metrics[key] for key in REMOTE_METRIC_KEYS if key in metrics
            and (metrics[key] is None or type(metrics[key]) in (bool, int, float))
            and (type(metrics[key]) is not float or math.isfinite(metrics[key]))}
        diagnostics["metrics"] = selected
        boolean_keys = ("complete", "expected_keyword_checked", "foreign_keyword_checked")
        diagnostics["metrics_validated"] = value.get("metrics_validated") is True and all(
            key in selected and (
                key in boolean_keys and type(selected[key]) is bool
                or key == "first_fixed_ms" and selected[key] is None
                or key not in boolean_keys and type(selected[key]) is int and selected[key] >= 0)
            for key in REMOTE_METRIC_KEYS)
    for key in ("missing_metric_keys", "invalid_metric_keys"):
        if isinstance(value.get(key), list):
            diagnostics[key] = [item for item in value[key] if isinstance(item, str) and item in REMOTE_METRIC_KEYS]
    stage = value.get("failure_stage")
    if isinstance(stage, str) and stage in REMOTE_WRAPPER_ERRORS | {"PROBE_FAILED"}:
        diagnostics["failure_stage"] = stage
    kind = value.get("exception_kind")
    if kind in ("ValueError", "KeyError", "JSONDecodeError", "OSError", "FileNotFoundError", "PermissionError"):
        diagnostics["exception_kind"] = kind
    if process.returncode != 0 or value.get("ok") is not True:
        allowed = set(CLIENT_STATIC_ERRORS) | REMOTE_WRAPPER_ERRORS
        candidate = value.get("failure_code")
        if value.get("ok") is True:
            code, layer = "REMOTE_WRAPPER_EXIT_FAILED", "wrapper_exit"
        elif isinstance(candidate, str) and candidate in allowed:
            code = candidate
            layer = "probe" if candidate in CLIENT_STATIC_ERRORS or candidate == "UNKNOWN_PROBE_ERROR" else "wrapper"
            if candidate in ("INVALID_METRICS", "PROBE_OUTPUT_OVERSIZE"):
                layer = "metrics"
        else:
            code, layer = "REMOTE_PROBE_FAILED", "wrapper_report"
        diagnostics["failure_layer"] = layer
        if code in CLIENT_STATIC_ERRORS:
            diagnostics["static_message"] = CLIENT_STATIC_ERRORS[code]
        raise TestFailure(code, diagnostics)
    if diagnostics.get("metrics_validated") is not True and "probe_exit_code" in value:
        raise TestFailure("INVALID_METRICS", {"failure_layer": "metrics", **diagnostics})
    require(isinstance(value.get("source_sha256"), str)
            and len(value["source_sha256"]) == 64, "REMOTE_SOURCE_HASH_INVALID")
    value["ssh_exit_code"] = process.returncode
    value["runner_stderr_bytes"] = stderr_size
    return value


def safe_failure(error):
    result = {"kind": type(error).__name__}
    if isinstance(error, TestFailure):
        result["code"] = error.code
        if error.diagnostics is not None:
            result["diagnostics"] = error.diagnostics
        return result
    text = str(error)
    exact = {"no live text before capture stopped": "PC_NO_LIVE_TEXT",
        "fixed text regressed": "FIXED_TEXT_REGRESSED",
        "connection ended before final": "PC_FINAL_MISSING",
        "unsent audio exceeded two-second buffer": "CLIENT_BUFFER_EXCEEDED",
        "model changed while binding": "MODEL_CHANGED_WHILE_BINDING"}
    code = exact.get(text)
    if text.startswith("server error: ") and text[14:] in CLIENT_STATIC_ERRORS:
        code = text[14:]
    result["code"] = code or type(error).__name__
    return result


def retain_session_results(report, pc, remote):
    outcomes = report.setdefault("session_outcomes", {})
    for role, task in (("pc", pc), ("remote", remote)):
        if task is None or not task.done():
            continue
        if task.cancelled():
            outcomes[role] = {"status": "cancelled"}
        elif task.exception() is not None:
            failure = safe_failure(task.exception())
            outcomes[role] = {"status": "failed", **failure}
            if role == "remote" and isinstance(failure.get("diagnostics"), dict):
                diagnostics = failure["diagnostics"]
                if isinstance(diagnostics.get("metrics"), dict):
                    report["remote"] = diagnostics["metrics"]
                    report["remote_metrics_validated"] = diagnostics.get("metrics_validated") is True
        else:
            outcomes[role] = {"status": "completed"}
            if role == "pc":
                metrics, _ = task.result()
                report["pc"] = numeric_metrics(metrics)
            else:
                report["remote"] = task.result()["metrics"]


async def stop_remote(process):
    if process is None or process.returncode is not None:
        return
    if process.stdin is not None:
        process.stdin.close()
        with suppress(Exception):
            await process.stdin.wait_closed()
    try:
        await asyncio.wait_for(process.wait(), 4)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def remote_inspect(args):
    process = await asyncio.create_subprocess_exec(*ssh_command("inspect", args.remote_pcm, args.seconds, getattr(args, "remote_keyword", REMOTE_KEYWORD)),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        return await asyncio.wait_for(collect_remote(process), 20)
    finally:
        await stop_remote(process)


async def remote_probe(args, processes):
    process = await asyncio.create_subprocess_exec(*ssh_command("probe", args.remote_pcm, args.seconds, getattr(args, "remote_keyword", REMOTE_KEYWORD)),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    processes.append(process)
    try:
        return await collect_remote(process)
    finally:
        await stop_remote(process)


def numeric_metrics(metrics):
    def safe(value):
        if type(value) in (bool, int):
            return value
        if type(value) is float:
            require(math.isfinite(value), "PC_METRIC_NOT_FINITE")
            return value
        if isinstance(value, list):
            return [safe(item) for item in value]
        if isinstance(value, dict):
            return {str(key): safe(item) for key, item in value.items()}
        raise TestFailure("PC_METRIC_FORMAT")
    excluded = {"role", "session_id", "model_generation", "final_reason", "transcript_sha256"}
    result = {key: safe(value) for key, value in metrics.items() if key not in excluded}
    result["transcript_sha256"] = metrics["transcript_sha256"]
    result["session_id_sha256"] = digest(metrics["session_id"].encode())
    result["final_reason_finished"] = metrics.get("final_reason") == "finished"
    return result


async def monitor(client, runtime, before, report, stop):
    first, last, count = None, None, 0
    identities = {}
    started = time.monotonic()
    while not stop.is_set():
        require_desktop_idle(await desktop_status(runtime))
        status = await service_status(client)
        require(status.get("mode") == "r2t2" and status.get("model_loaded") is True
                and all(status.get(key) == before[key]
                        for key in ("server_instance_id", "model_generation", "worker_pid")),
                "MODEL_OR_WORKER_CHANGED")
        sessions = status.get("active_sessions")
        require(isinstance(sessions, list) and len(sessions) <= 2, "UNEXPECTED_ACTIVE_SESSIONS")
        active = [row for row in sessions if row.get("state") == "active"]
        kinds = [row.get("kind") for row in active]
        require(all(kind in ("pc", "mobile") for kind in kinds)
                and kinds.count("pc") <= 1 and kinds.count("mobile") <= 1,
                "UNEXPECTED_ACTIVE_SESSION_ROLES")
        for row in active:
            kind, session_id = row["kind"], row.get("session_id")
            require(isinstance(session_id, str) and session_id, "SESSION_ID_MISSING")
            require(kind not in identities or identities[kind] == session_id, "ACTIVE_SESSION_REPLACED")
            identities[kind] = session_id
        report["session_id_sha256"] = {kind: digest(value.encode()) for kind, value in identities.items()}
        if set(kinds) == {"pc", "mobile"}:
            now = time.monotonic()
            first = now if first is None else first
            last = now
            count += 1
            require(len({row.get("session_id") for row in active}) == 2, "SESSION_ID_COLLISION")
        report["overlap"] = {"sample_interval_seconds": MONITOR_INTERVAL_SECONDS,
            "dual_active_samples": count,
            "observed_active_overlap_seconds": round(last - first, 4) if first is not None else 0,
            "first_dual_active_seconds": round(first - started, 4) if first is not None else None}
        try:
            await asyncio.wait_for(stop.wait(), MONITOR_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def wait_for_pc(client, runtime, pc):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        require_desktop_idle(await desktop_status(runtime))
        if pc.done():
            pc.result()
            raise TestFailure("PC_ENDED_BEFORE_REMOTE_START")
        status = await service_status(client)
        if any(row.get("kind") == "pc" and row.get("state") == "active"
               for row in status.get("active_sessions", [])):
            return
        await asyncio.sleep(0.05)
    raise TestFailure("PC_START_TIMEOUT")


async def run(args):
    report = {"passed": False, "requested_dual_seconds": args.seconds,
        "pc_capture_seconds": args.seconds + PC_START_MARGIN_SECONDS,
        "remote_capture_seconds": args.seconds, "overlap_tolerance_seconds": 2,
        "unverified_remote_metrics": ["early_late_lag_growth", "last_fixed_text_time", "max_fixed_text_gap", "transcript_sha256"],
        "microphone_and_gui_acceptance_included": False}
    report["remote_expected_keyword_sha256"] = digest(args.remote_keyword.encode())
    tasks, processes, stop = [], [], asyncio.Event()
    pc = remote = None
    try:
        settings, helper, helper_hash = load_voice(args.voice_repo)
        report["helper_source_sha256"] = helper_hash
        require_desktop_idle(await desktop_status(settings.runtime_dir))
        audio = helper.pcm(args.pc_audio)
        pc_source_hash = digest(audio)
        inspected = await remote_inspect(args)
        require(inspected["source_sha256"] != pc_source_hash, "AUDIO_SOURCES_NOT_DISTINCT")
        report["source_audio_sha256"] = {"pc": pc_source_hash, "remote": inspected["source_sha256"]}
        pc_token = settings.token_path.read_text().strip()
        require(bool(pc_token) and "\n" not in pc_token and "\r" not in pc_token, "PC_TOKEN_INVALID")
        async with helper.httpx.AsyncClient(base_url=settings.api_url, trust_env=False,
                follow_redirects=False, timeout=5, headers={"Authorization": "Bearer " + pc_token}) as client:
            before = await service_status(client)
            require_ready(before)
            require_desktop_idle(await desktop_status(settings.runtime_dir))
            generation = (before["server_instance_id"], before["model_generation"])
            watcher = asyncio.create_task(monitor(client, settings.runtime_dir, before, report, stop))
            tasks.append(watcher)
            pc = asyncio.create_task(helper.session(settings.api_url, pc_token, audio,
                args.seconds + PC_START_MARGIN_SECONDS, "pc", generation))
            tasks.append(pc)
            await wait_for_pc(client, settings.runtime_dir, pc)
            if watcher.done():
                watcher.result()
                raise TestFailure("DESKTOP_MONITOR_STOPPED")
            remote = asyncio.create_task(remote_probe(args, processes))
            tasks.append(remote)
            async def supervised():
                pending = {pc, remote}
                while pending:
                    done, _ = await asyncio.wait([*pending, watcher], return_when=asyncio.FIRST_COMPLETED)
                    if watcher in done:
                        watcher.result()
                        raise TestFailure("DESKTOP_MONITOR_STOPPED")
                    for task in done:
                        pending.remove(task)
                return tuple(task.exception() if task.exception() is not None else task.result()
                             for task in (pc, remote))
            pc_result, remote_result = await asyncio.wait_for(supervised(), args.seconds + 120)
            failure = next((value for value in (remote_result, pc_result) if isinstance(value, BaseException)), None)
            if failure is not None:
                raise failure
            pc_metrics, fixed = pc_result
            require(remote_result["source_sha256"] == inspected["source_sha256"], "REMOTE_AUDIO_CHANGED")
            require_desktop_idle(await desktop_status(settings.runtime_dir))
            after = await service_status(client)
            report["source_audio_sha256"] = {"pc": pc_source_hash, "remote": inspected["source_sha256"]}
            report["pc"] = numeric_metrics(pc_metrics)
            report["remote"] = remote_result["metrics"]
            thresholds = SimpleNamespace(max_processed_lag_seconds=2, max_lag_growth_seconds=0.5,
                                         max_fixed_gap_seconds=30)
            report["pc_performance"] = helper.performance_evidence(pc_metrics, thresholds)
            remote_metrics = remote_result["metrics"]
            expected = args.seconds * SAMPLE_RATE
            report["checks"] = {
                "pc_performance": report["pc_performance"]["passed"],
                "pc_chinese_present": any("\u4e00" <= char <= "\u9fff" for char in fixed),
                "pc_expected_keyword": PC_KEYWORD in fixed,
                "pc_foreign_keyword_absent": args.remote_keyword.casefold() not in fixed.casefold(),
                "pc_observed_session_matches": report.get("session_id_sha256", {}).get("pc")
                    == digest(pc_metrics["session_id"].encode()),
                "remote_keyword_checks": remote_metrics.get("expected_keyword_checked") is True
                    and remote_metrics.get("foreign_keyword_checked") is True,
                "remote_all_audio_processed": remote_metrics.get("sent_samples") == expected
                    and remote_metrics.get("processed_samples") == expected,
                "remote_complete": remote_metrics.get("complete") is True,
                "remote_live_text": type(remote_metrics.get("first_fixed_ms")) is int
                    and 0 <= remote_metrics["first_fixed_ms"] <= 30000
                    and remote_metrics.get("fixed_updates", 0) >= args.seconds / 30,
                "remote_real_clock": args.seconds * 1000 <= remote_metrics.get("elapsed_ms", 0)
                    <= (args.seconds + 90) * 1000,
                "remote_buffer_bounded": 0 <= remote_metrics.get("max_buffered_samples", -1) <= 32000,
                "remote_observed_lag_bounded": 0 <= remote_metrics.get("max_observed_lag_samples", -1) <= 32000,
                "approximately_full_dual_overlap": report.get("overlap", {}).get("observed_active_overlap_seconds", 0)
                    >= args.seconds - 2,
                "model_and_worker_preserved": after.get("model_loaded") is True and all(
                    after.get(key) == before[key] for key in ("server_instance_id", "model_generation", "worker_pid")),
            }
            report["passed"] = all(report["checks"].values())
    except BaseException as error:
        report["failure"] = safe_failure(error)
        report["failure_code"] = report["failure"]["code"]
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        retain_session_results(report, pc, remote)
        for process in processes:
            await stop_remote(process)
    return report


def save_report(path, report):
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="ascii", dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            json.dump(report, target, ensure_ascii=True, allow_nan=False, indent=2)
            target.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-repo", type=Path, required=True)
    parser.add_argument("--pc-audio", type=Path, required=True, help="Chinese 16kHz mono PCM16 WAV")
    parser.add_argument("--remote-pcm", required=True, help="English raw PCM16LE/16kHz/mono file on e15l")
    parser.add_argument("--remote-keyword", default=REMOTE_KEYWORD)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not 600 <= arguments.seconds <= 3580:
        parser.error("seconds must be between 600 and 3580")
    if not arguments.remote_keyword.strip():
        parser.error("remote keyword must not be empty")
    result = asyncio.run(run(arguments))
    save_report(arguments.output, result)
    print(json.dumps({"passed": result["passed"], "output": str(arguments.output),
                      "failure_code": result.get("failure_code")}))
    raise SystemExit(0 if result["passed"] else 1)
