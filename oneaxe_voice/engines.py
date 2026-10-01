"""Single active GPU backend and a killable, isolated streaming subprocess."""

import base64
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

from .backend import BusyError, GPUError, QwenEngine
from .config import ROOT
from .modes import validate_mode


class Worker:
    def __init__(self, settings, mode):
        self.process = None
        self.socket = None
        self.stream = None
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
        parent.settimeout(240)
        self.stream = parent.makefile("rb")
        env = dict(os.environ, ONEAXE_WORKER_FD=str(child.fileno()),
                   CUDA_VISIBLE_DEVICES=str(settings.cuda_device),
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="4",
                   VLLM_WORKER_MULTIPROC_METHOD="spawn", TOKENIZERS_PARALLELISM="false")
        try:
            log = settings.runtime_dir / "stream-worker.log"
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as output:
                self.process = subprocess.Popen(
                    [str(interpreter), "-m", "oneaxe_voice.stream_worker", mode, str(model)],
                    cwd=ROOT, env=env, pass_fds=(child.fileno(),),
                    stdout=output, stderr=output, start_new_session=True,
                )
            self._receive()
        except Exception:
            self.close()
            raise
        finally:
            child.close()

    def _receive(self):
        line = self.stream.readline(2 * 1024 * 1024)
        if not line or not line.endswith(b"\n"):
            raise GPUError("流式工作进程意外退出，请查看本机诊断日志")
        result = json.loads(line)
        if "error" in result:
            raise GPUError("流式推理失败（" + result["error"] + "），已停止本轮")
        return result

    def call(self, op, **values):
        try:
            self.socket.sendall((json.dumps({"op": op, **values}) + "\n").encode())
            return self._receive()
        except (OSError, ValueError) as exc:
            raise GPUError("流式工作进程通信失败") from exc

    def close(self):
        if self.process is not None:
            # vLLM spawns CUDA children. Reap the whole group, not only its wrapper.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        if self.stream:
            self.stream.close()
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
        self.last_used = time.monotonic()
        self.loading = False
        self.error = None

    def status(self):
        value = self.offline.status() if self.mode == "vad" else {
            "state": "loading" if self.loading else "ready" if self.worker else "unloaded",
            "model_loaded": self.worker is not None,
            "device": f"cuda:{self.settings.cuda_device}" if self.worker else None,
            "model": "Qwen3-ASR-1.7B" if self.mode == "qwen-stream" else "Confucius4-R2T2",
        }
        return {**value, "mode": self.mode, "busy": self.gate.locked(),
                "last_error": self.error, "worker_pid": self.worker.process.pid if self.worker else None}

    def _acquire(self):
        if not self.gate.acquire(blocking=False):
            raise BusyError("听写引擎正在使用，请等待本轮结束")

    def _select(self, mode):
        validate_mode(mode)
        if mode != self.mode:
            self.offline.unload_if_idle(force=True)
            if self.worker:
                self.worker.close()
                self.worker = None
            self.mode = mode
        self.loading, self.error = True, None
        try:
            if mode == "vad":
                self.offline.warmup()
            elif self.worker is None:
                self.worker = Worker(self.settings, mode)
        except Exception as exc:
            self.error = type(exc).__name__
            raise
        finally:
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
            return self.offline.transcribe(data, request_id)
        finally:
            self.last_used = time.monotonic()
            self.gate.release()

    def begin(self, mode, session):
        self._acquire()
        try:
            self._select(mode)
            self.worker.call("start")
            self.active_stream = session
        except BaseException:
            if self.worker:
                self.worker.close()
                self.worker = None
            self.gate.release()
            raise

    def feed(self, session, data):
        if self.active_stream != session:
            raise ValueError("流式会话已结束")
        return self.worker.call("audio", pcm=base64.b64encode(data).decode())

    def finish(self, session):
        if self.active_stream != session:
            raise ValueError("流式会话已结束")
        return self.worker.call("finish")

    def flush(self, session):
        if self.active_stream != session:
            raise ValueError("流式会话已结束")
        return self.worker.call("flush")

    def end(self, session, abort=False):
        if self.active_stream != session:
            return
        try:
            if abort and self.worker:
                self.worker.close()
                self.worker = None
        finally:
            self.active_stream = None
            self.last_used = time.monotonic()
            self.gate.release()

    def unload_if_idle(self, force=False):
        if not self.gate.acquire(blocking=False):
            return False
        try:
            self.offline.unload_if_idle(force=force)
            if self.worker and (force or self.settings.idle_seconds > 0 and
                                time.monotonic() - self.last_used >= self.settings.idle_seconds):
                self.worker.close()
                self.worker = None
                return True
            return False
        finally:
            self.gate.release()
