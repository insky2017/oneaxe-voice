"""One resident model generation with independent PC and mobile sessions."""

import base64
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from .backend import BusyError, GPUError, QwenEngine
from .config import ROOT
from .modes import STREAM_MODES, validate_mode
from .worker_client import WorkerClient, WorkerRPCError


class SessionError(GPUError):
    """A public, stable error which can be sent through the mobile protocol."""

    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class Session:
    session_id: str
    kind: str
    mode: str
    generation: str
    worker: object
    credential_id: str | None = None
    state: str = "active"
    terminal_reason: str | None = None
    result: dict = field(default_factory=lambda: {
        "text": "", "pending": "", "audio_processed_samples": 0,
    })
    operation_lock: object = field(default_factory=threading.Lock)


class Worker:
    def __init__(self, settings, mode):
        self.process = None
        self.socket = None
        self.client = None
        interpreter = Path(os.environ.get("ONEAXE_VOICE_STREAM_PYTHON", ROOT / ".venv-stream/bin/python")).expanduser()
        if not interpreter.is_file():
            raise GPUError("流式环境尚未安装，请运行 bin/install-stream-env")
        model = settings.model_dir if mode == "qwen-stream" else Path(
            os.environ.get("ONEAXE_VOICE_R2T2_MODEL_DIR", Path.home() / "tools/models/Confucius4-R2T2")
        ).expanduser()
        if not (model / "config.json").is_file():
            raise GPUError("所选模式的本地模型不存在")
        parent, child = socket.socketpair()
        self.socket = parent
        env = dict(os.environ, ONEAXE_WORKER_FD=str(child.fileno()),
                   CUDA_VISIBLE_DEVICES=str(settings.cuda_device),
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="4",
                   VLLM_WORKER_MULTIPROC_METHOD="spawn", TOKENIZERS_PARALLELISM="false")
        if mode in {"r2t2", "qwen-stream"}:
            env["OMP_WAIT_POLICY"] = "PASSIVE"
        try:
            log = settings.runtime_dir / "stream-worker.log"
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as output:
                self.process = subprocess.Popen(
                    [str(interpreter), "-m", "oneaxe_voice.concurrent_worker", mode, str(model)],
                    cwd=ROOT, env=env, pass_fds=(child.fileno(),),
                    stdout=output, stderr=output, start_new_session=True,
                )
            self.client = WorkerClient(parent)
            self.client.wait_ready()
        except Exception:
            self.close()
            raise
        finally:
            child.close()

    def call(self, op, **values):
        if self.client is None:
            raise GPUError("流式工作进程已关闭")
        return self.client.call(op, **values)

    def is_ready(self):
        return (self.process is not None and self.process.poll() is None and
                (self.client is None or self.client.failure is None))

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.process is not None:
            # vLLM spawns CUDA children. Reap the whole group, not only its wrapper.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        if self.socket:
            self.socket.close()


class EngineRouter:
    def __init__(self, settings):
        self.settings = settings
        self.offline = QwenEngine(settings)
        self.worker = None
        self.mode = "vad"
        self.gate = threading.Lock()
        self.active_stream = None
        self.server_instance_id = uuid.uuid4().hex
        self.model_generation = None
        self._lifecycle_lock = threading.RLock()
        self._sessions = {}
        self._terminal_sessions = OrderedDict()
        self._credential_validator = None
        self.last_used = time.monotonic()
        self.loading = False
        self.unloading = False
        self.error = None
        self._policy_lock = threading.Lock()
        self._policy_path = settings.runtime_dir / "model-policy.json"
        self.idle_seconds = settings.idle_seconds
        if self._policy_path.exists():
            policy = json.loads(self._policy_path.read_text(encoding="utf-8"))
            if type(policy) is not dict or type(policy.get("auto_unload")) is not bool:
                raise ValueError("模型空闲策略文件无效")
            self.idle_seconds = 120.0 if policy["auto_unload"] else 0.0

    def status(self):
        with self._lifecycle_lock:
            mode, worker = self.mode, self.worker
            loaded = worker is not None and self._worker_ready(worker)
            value = self.offline.status() if mode == "vad" else {
                "model_loaded": loaded,
                "device": f"cuda:{self.settings.cuda_device}" if loaded else None,
                "model": "Qwen3-ASR-1.7B" if mode == "qwen-stream" else "Confucius4-R2T2",
            }
            error = self.error or value.get("last_error") or ("SERVICE_UNAVAILABLE" if worker and not loaded else None)
            with self._policy_lock:
                idle_seconds = self.idle_seconds
            state = ("unloading" if self.unloading else
                     "loading" if self.loading or value.get("state") == "loading" else
                     "transcribing" if value.get("state") == "transcribing" else
                     "error" if error and not value.get("model_loaded") else
                     "ready" if value.get("model_loaded") else "unloaded")
            mobile_used = sum(item.kind == "mobile" for item in self._sessions.values())
            return {**value, "state": state, "mode": mode, "busy": bool(self._sessions) or self.gate.locked(),
                    "pc_busy": self.gate.locked(), "last_error": error,
                    "worker_pid": worker.process.pid if worker and worker.process else None,
                    "auto_unload": idle_seconds > 0, "idle_seconds": idle_seconds,
                    "idle_unload_seconds": idle_seconds,
                    "server_instance_id": self.server_instance_id,
                    "model_generation": self.model_generation if value.get("model_loaded") else None,
                    "active_sessions": [{**self._binding(item), "kind": item.kind, "state": item.state}
                                        for item in self._sessions.values()],
                    "mobile_slots_available": 1 - mobile_used, "max_sessions": 2,
                    "mobile_slots": {"capacity": 1, "used": mobile_used, "available": 1 - mobile_used}}

    def set_auto_unload(self, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError("auto_unload 必须是布尔值")
        with self._policy_lock:
            self.settings.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix=".model-policy-", dir=self.settings.runtime_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump({"auto_unload": enabled}, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self._policy_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.idle_seconds = 120.0 if enabled else 0.0
        return self.status()

    def _acquire(self):
        if not self.gate.acquire(blocking=False):
            raise BusyError("听写引擎正在使用，请等待本轮结束")

    def _select(self, mode):
        validate_mode(mode)
        with self._lifecycle_lock:
            self._refresh_worker()
            ready = (self.offline.status().get("model_loaded") if mode == "vad" else self.worker is not None)
            if mode == self.mode and ready:
                self.error = None
                return
            self.loading, self.error = True, None
            changing = mode != self.mode
        try:
            if changing:
                self._terminate_mobile("MODEL_CHANGED")
                self.offline.unload_if_idle(force=True)
                if self.worker:
                    self.worker.close()
                with self._lifecycle_lock:
                    self.worker = None
                    self.model_generation = None
                    self.mode = mode
            if mode == "vad":
                self.offline.warmup()
            else:
                worker = Worker(self.settings, mode)
                with self._lifecycle_lock:
                    self.worker = worker
            with self._lifecycle_lock:
                self.model_generation = uuid.uuid4().hex
        except Exception as exc:
            with self._lifecycle_lock:
                self.error = type(exc).__name__
            raise
        finally:
            with self._lifecycle_lock:
                self.loading = False

    def prepare(self, mode):
        self._acquire()
        try:
            self._select(mode)
            return self.status()
        finally:
            self.last_used = time.monotonic()
            self.gate.release()

    def warmup(self):
        return self.prepare("vad")

    def transcribe(self, data, request_id):
        self._acquire()
        try:
            if self.mode != "vad":
                self._select("vad")
            previously_loaded = bool(self.offline.status().get("model_loaded"))
            result = self.offline.transcribe(data, request_id)
            with self._lifecycle_lock:
                if (not previously_loaded or self.model_generation is None) and self.offline.status().get("model_loaded"):
                    self.model_generation = uuid.uuid4().hex
            return result
        finally:
            with self._lifecycle_lock:
                if not self.offline.status().get("model_loaded"):
                    self.model_generation = None
            self.last_used = time.monotonic()
            self.gate.release()

    def begin(self, mode, session):
        self._acquire()
        item = None
        try:
            if mode == "vad":
                raise ValueError("VAD 模式不支持流式会话")
            self._select(mode)
            with self._lifecycle_lock:
                if session in self._sessions or session in self._terminal_sessions:
                    raise SessionError("INVALID_MESSAGE", "会话标识已使用")
                item = Session(session, "pc", mode, self.model_generation, self.worker, state="starting")
                self._sessions[session] = item
                self.active_stream = session
            return self._start_session(item)
        except BaseException as exc:
            if item is None:
                self.gate.release()
            else:
                self.cancel(session, reason=getattr(exc, "code", "SERVICE_UNAVAILABLE"))
            raise

    def set_mobile_credential_validator(self, callback):
        with self._lifecycle_lock:
            self._credential_validator = callback

    def begin_mobile(self, session, expected_server_instance_id, expected_model_generation, credential_id):
        with self._lifecycle_lock:
            self._refresh_worker()
            if self._credential_validator is not None and not self._credential_validator(credential_id):
                raise SessionError("UNAUTHORIZED", "设备凭据已失效")
            if (expected_server_instance_id != self.server_instance_id or
                    expected_model_generation != self.model_generation):
                raise SessionError("MODEL_CHANGED", "模型状态已变化，请重新查询")
            loaded = self.worker is not None if self.mode != "vad" else self.offline.status().get("model_loaded")
            if self.loading or self.unloading or not loaded:
                raise SessionError("MODEL_NOT_READY", "请等待 PC 加载模型")
            if self.mode not in STREAM_MODES:
                raise SessionError("MODEL_UNSUPPORTED", "当前模式尚未接入远端流式接口")
            if any(item.kind == "mobile" for item in self._sessions.values()):
                raise SessionError("CAPACITY_EXCEEDED", "手机听写名额已占用", True)
            if session in self._sessions or session in self._terminal_sessions:
                raise SessionError("INVALID_MESSAGE", "会话标识已使用")
            item = Session(session, "mobile", self.mode, self.model_generation, self.worker,
                           credential_id, state="starting")
            self._sessions[session] = item
        return self._start_session(item)

    def _start_session(self, item):
        try:
            result = self._call_worker(item, "start")
        except GPUError as exc:
            with self._lifecycle_lock:
                terminated = item.state == "terminated"
                reason = item.terminal_reason
            if terminated:
                raise SessionError(reason, "流式会话已结束") from exc
            if not isinstance(exc, WorkerRPCError) or exc.fatal:
                self._worker_failed(item.worker)
            else:
                self.cancel(item.session_id, reason=self._public_error(exc).code)
            raise self._public_error(exc) from exc
        except BaseException:
            self.cancel(item.session_id, reason="SERVICE_UNAVAILABLE")
            raise
        with self._lifecycle_lock:
            if (self._sessions.get(item.session_id) is item and item.state == "starting" and
                    self.worker is item.worker and self.model_generation == item.generation and
                    not self.loading and not self.unloading):
                item.state = "active"
                if isinstance(result, dict):
                    item.result.update({key: result[key] for key in item.result if key in result})
                return self._binding(item)
            reason = item.terminal_reason or "MODEL_CHANGED"
        # Cancellation may have reached the worker before this start was sent.
        # The late start must never leave an unleased decoder occupying capacity.
        with suppress(GPUError):
            self._call_worker(item, "cancel")
        self.cancel(item.session_id, reason=reason)
        raise SessionError(reason, "流式会话已结束")

    def _binding(self, item):
        return {"session_id": item.session_id, "session": item.session_id,
                "server_instance_id": self.server_instance_id, "model_generation": item.generation,
                "mode": item.mode, "device": f"cuda:{self.settings.cuda_device}"}

    def _session_snapshot(self, item):
        binding = self._binding(item)
        return {**binding, "binding": binding, "kind": item.kind, "state": item.state,
                "terminal_reason": item.terminal_reason, **item.result}

    def session_status(self, session):
        with self._lifecycle_lock:
            self._refresh_worker()
            item = self._sessions.get(session) or self._terminal_sessions.get(session)
            if item is None:
                raise SessionError("INVALID_MESSAGE", "流式会话不存在")
            return self._session_snapshot(item)

    def _get_session(self, session):
        with self._lifecycle_lock:
            item = self._sessions.get(session)
            if item is None:
                previous = self._terminal_sessions.get(session)
                reason = previous.terminal_reason if previous else "INVALID_MESSAGE"
                if reason in ("finished", "cancelled"):
                    reason = "INVALID_MESSAGE"
                raise SessionError(reason, "流式会话已结束")
            return item

    def _call_worker(self, item, op, **values):
        return item.worker.call(op, session=item.session_id, **values)

    def _public_error(self, exc):
        code = getattr(exc, "code", "SERVICE_UNAVAILABLE")
        code = {"INVALID_AUDIO": "INVALID_MESSAGE", "SESSION_FAILED": "SERVICE_UNAVAILABLE",
                "CANCELLED": "SERVICE_UNAVAILABLE"}.get(code, code)
        return SessionError(code,
                            getattr(exc, "message", "流式推理失败，已停止本轮"),
                            getattr(exc, "retryable", True))

    def _operation(self, session, op, **values):
        item = self._get_session(session)
        with item.operation_lock:
            with self._lifecycle_lock:
                if self._sessions.get(session) is item and item.state == "finished" and op == "finish":
                    return dict(item.result)
                if self._sessions.get(session) is not item or item.state != "active":
                    raise SessionError(item.terminal_reason or "INVALID_MESSAGE", "流式会话已结束")
            try:
                result = self._call_worker(item, op, **values)
            except GPUError as exc:
                with self._lifecycle_lock:
                    if item.state == "terminated":
                        raise SessionError(item.terminal_reason, "流式会话已结束") from exc
                if not isinstance(exc, WorkerRPCError) or exc.fatal:
                    self._worker_failed(item.worker)
                else:
                    self.cancel(session, reason=self._public_error(exc).code)
                raise self._public_error(exc) from exc
            with self._lifecycle_lock:
                if self._sessions.get(session) is not item or item.state != "active":
                    raise SessionError(item.terminal_reason or "INVALID_MESSAGE", "流式会话已结束")
                item.result = {**item.result, **{key: value for key, value in result.items()
                                              if key not in ("rpc_id", "session", "ok")}}
                if op == "finish":
                    item.state = "finished"
                return dict(item.result)

    def feed(self, session, data):
        return self._operation(session, "audio", pcm=base64.b64encode(data).decode())

    def finish(self, session):
        return self._operation(session, "finish")

    def flush(self, session):
        return self._operation(session, "flush")

    def _retire(self, item, reason):
        if self._sessions.get(item.session_id) is not item:
            return
        self._sessions.pop(item.session_id, None)
        self._mark_terminal(item, reason)
        self._terminal_sessions[item.session_id] = item
        while len(self._terminal_sessions) > 256:
            self._terminal_sessions.popitem(last=False)
        if item.kind == "pc" and self.active_stream == item.session_id:
            self.active_stream = None
            self.gate.release()
        if not self._sessions:
            self.last_used = time.monotonic()

    def _mark_terminal(self, item, reason):
        item.state, item.terminal_reason = "terminated", reason
        item.result = {**item.result, "pending": "", "preview": item.result.get("text", "")[-600:]}

    def end(self, session, abort=False):
        if abort:
            self.cancel(session)
            return
        with self._lifecycle_lock:
            item = self._sessions.get(session)
            if item is None or item.state == "terminated":
                return
            self._mark_terminal(item, "finished")
        try:
            with suppress(GPUError):
                self._call_worker(item, "end")
        finally:
            with self._lifecycle_lock:
                self._retire(item, "finished")

    def cancel(self, session, reason="cancelled"):
        with self._lifecycle_lock:
            item = self._sessions.get(session)
            if item is None or item.state == "terminated":
                return
            self._mark_terminal(item, reason)
        try:
            with suppress(GPUError):
                self._call_worker(item, "cancel")
        finally:
            with self._lifecycle_lock:
                self._retire(item, reason)

    def revoke_credential_sessions(self, credential_id):
        with self._lifecycle_lock:
            sessions = [item.session_id for item in self._sessions.values()
                        if item.kind == "mobile" and item.credential_id == credential_id]
        for session in sessions:
            self.cancel(session, reason="UNAUTHORIZED")
        return len(sessions)

    def _terminate_mobile(self, reason):
        with self._lifecycle_lock:
            sessions = [item.session_id for item in self._sessions.values() if item.kind == "mobile"]
        for session in sessions:
            self.cancel(session, reason)

    def _worker_failed(self, worker):
        with self._lifecycle_lock:
            if self.worker is not worker:
                return
            self.worker, self.model_generation = None, None
            self.error = "SERVICE_UNAVAILABLE"
            for item in list(self._sessions.values()):
                if item.worker is worker:
                    self._retire(item, "SERVICE_UNAVAILABLE")
            worker.close()

    def _refresh_worker(self):
        worker = self.worker
        if worker is not None and not self._worker_ready(worker):
            self._worker_failed(worker)

    def _worker_ready(self, worker):
        check = getattr(worker, "is_ready", None)
        return not callable(check) or bool(check())

    def unload_if_idle(self, force=False):
        if not self.gate.acquire(blocking=False):
            return False
        try:
            with self._policy_lock:
                idle_seconds = self.idle_seconds
            if not force and not (idle_seconds > 0 and
                                  time.monotonic() - self.last_used >= idle_seconds):
                return False
            if not (self.worker or self.offline.status().get("model_loaded")):
                return False
            with self._lifecycle_lock:
                if self._sessions:
                    return False
                self.unloading = True
            released = self.offline.unload_if_idle(force=True)
            if self.worker:
                self.worker.close()
                self.worker = None
                released = True
            with self._lifecycle_lock:
                self.model_generation = None
                self.error = None
            return released
        finally:
            with self._lifecycle_lock:
                self.unloading = False
            self.gate.release()

    def unload(self):
        if self.gate.locked():
            raise BusyError("听写引擎正在使用，请等待本轮结束")
        if not self.gate.acquire(blocking=False):
            raise BusyError("听写引擎正在使用，请等待本轮结束")
        try:
            with self._lifecycle_lock:
                self.unloading = True
            try:
                self._terminate_mobile("MODEL_NOT_READY")
                self.offline.unload_if_idle(force=True)
                if self.worker:
                    self.worker.close()
                    self.worker = None
                self.error = None
                self.model_generation = None
            finally:
                self.unloading = False
        finally:
            self.gate.release()
        return self.status()
