"""Thread safe request multiplexing over the worker's private JSON socket."""

from concurrent.futures import Future, TimeoutError
import json
import socket
import threading
import uuid

from .backend import GPUError


class WorkerRPCError(GPUError):
    def __init__(self, code, message, *, retryable=False, fatal=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fatal = fatal


class WorkerClient:
    """One reader owns the socket; callers wait on their individual futures."""

    def __init__(self, connection, *, timeout=240, max_pending=256):
        self.connection = connection
        self.timeout = timeout
        self.max_pending = max_pending
        self.stream = connection.makefile("rb")
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending = {}
        self._ready = Future()
        self._failure = None
        self._reader = threading.Thread(target=self._read, name="voice-worker-rpc", daemon=True)
        self._reader.start()

    def wait_ready(self):
        try:
            return self._ready.result(timeout=self.timeout)
        except TimeoutError as exc:
            raise WorkerRPCError("SERVICE_UNAVAILABLE", "流式模型加载超时", retryable=True, fatal=True) from exc

    @property
    def failure(self):
        with self._lock:
            return self._failure

    def _fail(self, error):
        with self._lock:
            if self._failure is None:
                self._failure = error
            pending = list(self._pending.values())
            self._pending.clear()
            if not self._ready.done():
                self._ready.set_exception(self._failure)
        for future, _ in pending:
            future.set_exception(self._failure)

    def _read(self):
        try:
            while True:
                line = self.stream.readline(2 * 1024 * 1024)
                if not line or not line.endswith(b"\n"):
                    raise ValueError("worker socket ended")
                result = json.loads(line)
                if not isinstance(result, dict):
                    raise ValueError("invalid worker response")
                rpc_id = result.get("rpc_id")
                if rpc_id is None:
                    with self._lock:
                        if result.get("ready") is True and not self._ready.done():
                            self._ready.set_result(result)
                            continue
                    raise ValueError("invalid worker readiness")
                if not isinstance(rpc_id, str):
                    raise ValueError("invalid worker request identifier")
                with self._lock:
                    pending = self._pending.pop(rpc_id, None)
                future, session = pending if pending is not None else (None, None)
                if future is not None and result.get("session") != session:
                    error = WorkerRPCError("SERVICE_UNAVAILABLE", "流式响应会话不匹配", fatal=True)
                    future.set_exception(error)
                    self._fail(error)
                    return
                if result.get("ok") is not True or "error" in result:
                    error = WorkerRPCError(
                        result.get("code", "SERVICE_UNAVAILABLE"),
                        result.get("message", "流式推理失败，已停止本轮"),
                        retryable=result.get("retryable") is True,
                        fatal=result.get("fatal") is True,
                    )
                    if future is not None:
                        future.set_exception(error)
                    if error.fatal:
                        self._fail(error)
                        return
                elif future is not None:
                    future.set_result(result)
        except (OSError, ValueError) as exc:
            self._fail(WorkerRPCError(
                "SERVICE_UNAVAILABLE", "流式工作进程通信失败", retryable=True, fatal=True,
            ))

    def call(self, op, *, session, **values):
        rpc_id = uuid.uuid4().hex
        future = Future()
        with self._lock:
            if self._failure is not None:
                raise self._failure
            if len(self._pending) >= self.max_pending:
                raise WorkerRPCError("CAPACITY_EXCEEDED", "流式请求队列已满", retryable=True)
            self._pending[rpc_id] = (future, session)
        try:
            payload = {"rpc_id": rpc_id, "op": op, "session": session, **values}
            with self._send_lock:
                self.connection.sendall((json.dumps(payload) + "\n").encode())
        except (OSError, ValueError, TypeError) as exc:
            self._fail(WorkerRPCError(
                "SERVICE_UNAVAILABLE", "流式工作进程通信失败", retryable=True, fatal=True,
            ))
        try:
            return future.result(timeout=self.timeout)
        except TimeoutError as exc:
            with self._lock:
                self._pending.pop(rpc_id, None)
            raise WorkerRPCError("SERVICE_UNAVAILABLE", "流式请求超时", retryable=True) from exc

    def close(self):
        self._fail(WorkerRPCError(
            "SERVICE_UNAVAILABLE", "流式工作进程已关闭", retryable=True, fatal=True,
        ))
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2)
        self.stream.close()
        self.connection.close()
