"""V1 session transport shared by restricted devices and the local desktop."""

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import time
import uuid

import anyio
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from .backend import BusyError
from .device_auth import MOBILE_READ, MOBILE_STREAM
from .modes import STREAM_MODES


LOGGER = logging.getLogger("uvicorn.error")
AUDIO_FORMAT = {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
CLOSE_CODES = {"CAPACITY_EXCEEDED": 1013, "SERVICE_UNAVAILABLE": 1011}


@dataclass(frozen=True)
class StreamLimits:
    window_samples: int = 32000
    max_frame_bytes: int = 5120
    input_timeout: float = 30
    start_timeout: float = 5
    session_seconds: float = 3600
    send_timeout: float = 5
    monitor_interval: float = .1
    max_messages_per_second: int = 100
    work_queue_size: int = 128
    output_queue_size: int = 16

    def __post_init__(self):
        if self.output_queue_size < 4:
            raise ValueError("V1 output queue needs four snapshot slots")


class ProtocolError(ValueError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def failure(exc):
    if hasattr(exc, "code") and hasattr(exc, "message"):
        return exc.code, exc.message, bool(getattr(exc, "retryable", False))
    if isinstance(exc, BusyError):
        return "CAPACITY_EXCEEDED", "当前没有可用会话名额", True
    return "SERVICE_UNAVAILABLE", "语音服务暂不可用", True


def stream_window_samples(mode, limits):
    # Qwen consumes 32000 samples per decode. An integral client frame must
    # be able to cross that boundary before processed credit can advance.
    minimum = 32000 + limits.max_frame_bytes // 2 if mode == "qwen-stream" else 0
    return max(limits.window_samples, minimum)


def capabilities(engine, limits):
    value = engine.status()
    state = value.get("state", "unloaded")
    if state == "transcribing":
        state = "ready"
    ready = state == "ready" and bool(value.get("model_loaded"))
    supported = value.get("mode") in STREAM_MODES
    slots = int(value.get("mobile_slots_available", 0))
    if state == "error":
        reason = "SERVICE_UNAVAILABLE"
    elif not ready:
        reason = "MODEL_NOT_READY"
    elif not supported:
        reason = "MODEL_UNSUPPORTED"
    elif not slots:
        reason = "CAPACITY_EXCEEDED"
    else:
        reason = None
    return {"protocol_version": 1, "server_instance_id": value.get("server_instance_id"),
            "model_generation": value.get("model_generation") if ready else None,
            "model_id": value.get("model"), "mode": value.get("mode"),
            "model_state": state, "ready": ready, "stream_supported": supported,
            "can_start": reason is None, "unavailable_reason": reason,
            "max_sessions": value.get("max_sessions", 2), "mobile_slots_available": slots,
            "audio": {**AUDIO_FORMAT, "max_frame_bytes": limits.max_frame_bytes},
            "flow": {"window_samples": stream_window_samples(value.get("mode"), limits),
                     "client_buffer_max_ms": 2000},
            "session_max_seconds": limits.session_seconds}


def validate_start(value, mobile):
    keys = {"type", "protocol_version", "audio"}
    keys |= {"expected_server_instance_id", "expected_model_generation"} if mobile else {"mode"}
    valid = (isinstance(value, dict) and set(value) == keys and value.get("type") == "start"
             and type(value.get("protocol_version")) is int and value["protocol_version"] == 1
             and isinstance(value.get("audio"), dict) and value["audio"] == AUDIO_FORMAT
             and type(value["audio"].get("sample_rate")) is int
             and type(value["audio"].get("channels")) is int)
    if mobile:
        valid = valid and all(isinstance(value.get(key), str) and 0 < len(value[key]) <= 128
                              for key in ("expected_server_instance_id", "expected_model_generation"))
    else:
        valid = valid and isinstance(value.get("mode"), str) and value["mode"] in STREAM_MODES
    if not valid:
        raise ProtocolError("INVALID_MESSAGE", "start 消息或音频规格无效")


class V1Stream:
    def __init__(self, ws, engine, credentials, worker_call, limits, mobile):
        self.ws, self.engine, self.credentials = ws, engine, credentials
        self.worker_call, self.limits, self.mobile = worker_call, limits, mobile
        self.session = str(uuid.uuid4())
        self.principal = ws.scope.get("voice.principal")
        self.binding = {}
        self.received = self.processed = self.seq = 0
        self.text = self.pending = ""
        self.bound = self.finished = self.finishing = self.disconnected = False
        self.terminal = asyncio.Event()
        self.work = asyncio.Queue(maxsize=limits.work_queue_size)
        self.output = asyncio.Queue(maxsize=limits.output_queue_size)
        self.tasks = []
        self.start_task = None
        self.begin_attempted = False
        self.cancel_task = None
        self.terminal_enqueued = False

    def progress(self):
        return {"audio_received_samples": self.received, "audio_processed_samples": self.processed,
                "audio_send_limit": self.processed + stream_window_samples(self.binding.get("mode"), self.limits)}

    def emit(self, event, close_code=None):
        if self.terminal_enqueued:
            return
        if event is not None and close_code is None and self.terminal.is_set():
            return
        queued = [self.output.get_nowait() for _ in range(self.output.qsize())]
        kind = event.get("type") if event else None
        # Replacing a snapshot must append it after older snapshots of other
        # kinds, so wire progress never moves backwards.
        if kind in {"flow", "partial", "keepalive"}:
            queued = [item for item in queued if item[0].get("type") != kind]
        if close_code is not None or event is None:
            self.terminal_enqueued = True
            if len(queued) >= self.limits.output_queue_size:
                for removable in ("keepalive", "partial", "flow"):
                    for index, item in enumerate(queued):
                        if item[0].get("type") == removable:
                            queued.pop(index)
                            break
                    if len(queued) < self.limits.output_queue_size:
                        break
        for item in queued:
            self.output.put_nowait(item)
        self.output.put_nowait((event, close_code))

    def absorb(self, result):
        if not isinstance(result, dict):
            raise ProtocolError("SERVICE_UNAVAILABLE", "引擎返回无效结果")
        text = result.get("text", self.text)
        processed = result.get("audio_processed_samples", self.processed)
        if (not isinstance(text, str) or not text.startswith(self.text)
                or type(processed) is not int or not self.processed <= processed <= self.received):
            raise ProtocolError("SERVICE_UNAVAILABLE", "识别结果或音频进度不一致")
        self.text = text
        self.pending = result.get("pending", "")
        self.processed = processed

    def stop(self, code=None, message=None, reason="cancelled", retryable=False):
        if self.terminal.is_set():
            return
        self.terminal.set()
        if self.bound and not self.finished:
            self.cancel_task = asyncio.create_task(self.worker_call(self.engine.cancel, self.session,
                                                                   code or reason))
        if self.disconnected:
            self.emit(None)
        elif code:
            self.emit({"type": "error", "code": code, "message": message,
                       "retryable": retryable,
                       "retry_after_ms": 2000 if code in {"CAPACITY_EXCEEDED", "SERVICE_UNAVAILABLE"} else None,
                       "text": self.text, "pending": "", "complete": False, **self.progress()},
                      CLOSE_CODES.get(code, 1008))
        else:
            self.emit({"type": "final", "reason": reason, "text": self.text,
                       "pending": "", "complete": False, **self.progress()}, 1000)

    async def writer(self):
        while True:
            event, close_code = await self.output.get()
            if event is None or self.disconnected:
                return
            self.seq += 1
            event = {**event, "server_instance_id": self.binding.get("server_instance_id"),
                     "model_generation": self.binding.get("model_generation"),
                     "session_id": self.session, "request_id": self.session, "seq": self.seq}
            await asyncio.wait_for(self.ws.send_json(event), self.limits.send_timeout)
            if close_code is not None:
                await self.ws.close(code=close_code)
                return

    async def receiver(self):
        rate = deque()
        try:
            while not self.terminal.is_set():
                message = await asyncio.wait_for(self.ws.receive(), self.limits.input_timeout)
                if self.terminal.is_set():
                    return
                if message["type"] == "websocket.disconnect":
                    self.disconnected = True
                    self.stop()
                    return
                now = time.monotonic()
                while rate and rate[0] <= now - 1:
                    rate.popleft()
                rate.append(now)
                if len(rate) > self.limits.max_messages_per_second:
                    raise ProtocolError("RATE_LIMITED", "输入消息过于频繁")
                if self.mobile and not self.credentials.is_active(self.principal.credential_id):
                    raise ProtocolError("UNAUTHORIZED", "设备凭据已失效")
                data = message.get("bytes")
                if data is not None:
                    if self.finishing or not 2 <= len(data) <= self.limits.max_frame_bytes or len(data) % 2:
                        raise ProtocolError("INVALID_MESSAGE", "PCM 帧长度无效或会话正在结束")
                    samples = len(data) // 2
                    if self.received + samples > int(self.limits.session_seconds * 16000):
                        raise ProtocolError("SESSION_LIMIT", "会话达到音频时长上限")
                    if self.received + samples > self.progress()["audio_send_limit"]:
                        raise ProtocolError("FLOW_CONTROL_EXCEEDED", "音频超过累计发送许可")
                    self.received += samples
                    self.enqueue(("audio", data))
                    self.emit({"type": "flow", **self.progress()})
                    continue
                raw = message.get("text", "")
                if len(raw) > 1024:
                    raise ProtocolError("INVALID_MESSAGE", "控制消息过大")
                try:
                    control = json.loads(raw)
                except (ValueError, TypeError):
                    raise ProtocolError("INVALID_MESSAGE", "控制消息必须为 JSON") from None
                if not isinstance(control, dict):
                    raise ProtocolError("INVALID_MESSAGE", "控制消息无效")
                op = control.get("type")
                if op in {"flush", "finish"}:
                    if (set(control) != {"type", "after_audio_samples"}
                            or type(control["after_audio_samples"]) is not int
                            or control["after_audio_samples"] != self.received or self.finishing):
                        raise ProtocolError("INVALID_MESSAGE", "音频顺序屏障无效")
                    self.finishing = op == "finish"
                    self.enqueue((op, control["after_audio_samples"]))
                elif op in {"cancel", "keepalive"} and set(control) == {"type"}:
                    if op == "cancel":
                        self.stop()
                        return
                    self.emit({"type": "keepalive", **self.progress()})
                else:
                    raise ProtocolError("INVALID_MESSAGE", "未知控制消息")
        except asyncio.TimeoutError:
            self.stop("SESSION_TIMEOUT", "连接超过 30 秒没有输入")
        except WebSocketDisconnect:
            self.disconnected = True
            self.stop()
        except Exception as exc:
            self.stop(*failure(exc)[:2], retryable=failure(exc)[2])

    def enqueue(self, item):
        try:
            self.work.put_nowait(item)
        except asyncio.QueueFull:
            raise ProtocolError("FLOW_CONTROL_EXCEEDED", "本会话待处理消息过多") from None

    async def processor(self):
        flushed_at = None
        try:
            while not self.terminal.is_set():
                op, data = await self.work.get()
                if op == "audio":
                    result = await self.worker_call(self.engine.feed, self.session, data)
                elif op == "flush":
                    if flushed_at == data:
                        continue
                    result = await self.worker_call(self.engine.flush, self.session)
                    flushed_at = data
                else:
                    result = await self.worker_call(self.engine.finish, self.session)
                if self.terminal.is_set():
                    return
                self.absorb(result)
                if op == "finish" and self.processed != self.received:
                    raise ProtocolError("SERVICE_UNAVAILABLE", "结束时仍有未完成的音频")
                extras = {key: result[key] for key in ("device", "window_seconds", "inference_ms", "preview")
                          if key in result}
                self.emit({"type": "flow", **self.progress()})
                event = {**extras, **self.progress(), "type": "final" if op == "finish" else "partial",
                         "text": self.text, "pending": self.pending,
                         "audio_seconds": self.received / 16000}
                if op == "finish":
                    self.finished = True
                    self.terminal.set()
                    event.update(reason="finished", complete=True, pending="")
                self.emit(event, 1000 if op == "finish" else None)
        except Exception as exc:
            code, message, retryable = failure(exc)
            self.stop(code, message, retryable=retryable)

    async def monitor(self):
        started = time.monotonic()
        try:
            while not self.terminal.is_set():
                await asyncio.sleep(self.limits.monitor_interval)
                if time.monotonic() - started >= self.limits.session_seconds:
                    self.stop("SESSION_LIMIT", "会话达到时长上限")
                    return
                if self.mobile:
                    await asyncio.to_thread(self.credentials.refresh)
                    if not self.credentials.is_active(self.principal.credential_id):
                        self.stop("UNAUTHORIZED", "设备凭据已失效")
                        return
                snapshot = await self.worker_call(self.engine.session_status, self.session)
                reason = snapshot.get("terminal_reason")
                if reason and not self.terminal.is_set():
                    self.absorb(snapshot)
                    code = reason if reason in {
                        "MODEL_CHANGED", "MODEL_NOT_READY", "UNAUTHORIZED", "SERVICE_UNAVAILABLE"
                    } else "SERVICE_UNAVAILABLE"
                    messages = {"MODEL_CHANGED": "PC 已切换模型，本轮已结束",
                                "MODEL_NOT_READY": "PC 已卸载模型，本轮已结束",
                                "UNAUTHORIZED": "设备凭据已失效"}
                    self.stop(code, messages.get(code, "语音会话已停止"))
        except Exception as exc:
            code, message, retryable = failure(exc)
            self.stop(code, message, retryable=retryable)

    async def run(self):
        await self.ws.accept()
        try:
            hello = await asyncio.wait_for(self.ws.receive_json(), self.limits.start_timeout)
            validate_start(hello, self.mobile)
            if self.mobile:
                if not self.credentials.is_active(self.principal.credential_id):
                    raise ProtocolError("UNAUTHORIZED", "设备凭据已失效")
                start_args = (self.engine.begin_mobile, self.session,
                              hello["expected_server_instance_id"],
                              hello["expected_model_generation"], self.principal.credential_id)
            else:
                start_args = (self.engine.begin, hello["mode"], self.session)
            self.begin_attempted = True
            self.start_task = asyncio.create_task(self.worker_call(*start_args))
            self.binding = await asyncio.shield(self.start_task)
            self.bound = True
            self.emit({"type": "ready", "device": self.binding.get("device"), **self.progress()})
            self.tasks = [asyncio.create_task(method()) for method in (
                self.writer, self.receiver, self.processor, self.monitor)]
            await self.tasks[0]
        except WebSocketDisconnect:
            self.disconnected = True
        except asyncio.CancelledError:
            self.disconnected = True
        except (Exception, asyncio.TimeoutError) as exc:
            if not self.bound:
                code, message, retryable = failure(exc)
                if isinstance(exc, (ValueError, asyncio.TimeoutError)) and not hasattr(exc, "code"):
                    code, message, retryable = "INVALID_MESSAGE", "未收到有效的 start 消息", False
                with suppress(Exception):
                    await self.ws.send_json({"type": "error", "code": code, "message": message,
                                             "retryable": retryable, "retry_after_ms": 2000 if retryable else None,
                                             "session_id": None, "seq": 0, "text": "", "pending": "",
                                             "complete": False})
                    await self.ws.close(code=CLOSE_CODES.get(code, 1008))
            elif not self.disconnected:
                LOGGER.error("v1_transport_failed kind=%s request_id=%s", type(exc).__name__, self.session)
        finally:
            # ASGI cancellation must not interrupt this session's engine lease
            # cleanup or cancel the unrelated desktop session.
            with anyio.CancelScope(shield=True):
                self.terminal.set()
                for task in self.tasks:
                    task.cancel()
                if self.start_task is not None:
                    await asyncio.gather(self.start_task, return_exceptions=True)
                if self.begin_attempted and not self.finished and self.cancel_task is None:
                    self.cancel_task = asyncio.create_task(self.worker_call(self.engine.cancel, self.session,
                                                                           "cancelled"))
                if self.cancel_task is not None:
                    await asyncio.gather(self.cancel_task, return_exceptions=True)
                await asyncio.gather(*self.tasks, return_exceptions=True)
                if self.begin_attempted:
                    with suppress(Exception):
                        await self.worker_call(self.engine.end, self.session, not self.finished)
                with suppress(Exception):
                    await self.ws.close()


def register_v1(app, engine, credentials, worker_call, limits=None):
    limits = limits or StreamLimits()

    @app.get("/api/mobile/v1/capabilities")
    async def mobile_capabilities():
        return capabilities(engine, limits)

    @app.websocket("/api/mobile/v1/dictation/stream")
    async def mobile_stream(ws: WebSocket):
        await V1Stream(ws, engine, credentials, worker_call, limits, mobile=True).run()

    @app.websocket("/api/dictation/v1/stream")
    async def pc_stream(ws: WebSocket):
        await V1Stream(ws, engine, credentials, worker_call, limits, mobile=False).run()

    class DeviceRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(min_length=1, max_length=80)

    @app.get("/api/devices")
    async def list_devices():
        return {"devices": await asyncio.to_thread(credentials.list_devices)}

    @app.post("/api/devices")
    async def issue_device(value: DeviceRequest):
        try:
            return await asyncio.to_thread(credentials.issue, value.name)
        except ValueError as exc:
            raise HTTPException(422, "设备名称无效") from exc

    async def invalidate(credential_id, rotate):
        try:
            result = await asyncio.to_thread(credentials.rotate if rotate else credentials.revoke,
                                            credential_id)
        except KeyError as exc:
            raise HTTPException(404, "设备凭据不存在") from exc
        await worker_call(engine.revoke_credential_sessions, credential_id)
        return result

    @app.post("/api/devices/{credential_id}/revoke")
    async def revoke_device(credential_id: str):
        return await invalidate(credential_id, False)

    @app.post("/api/devices/{credential_id}/rotate")
    async def rotate_device(credential_id: str):
        return await invalidate(credential_id, True)
