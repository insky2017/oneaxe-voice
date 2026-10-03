"""Bounded, bidirectional client for the dictation V1 stream protocol."""

import asyncio
from contextlib import suppress
import json

from .capture import CaptureError


class StreamInput:
    """Keep queued and credit-blocked PCM within the same two-second bound."""

    def __init__(self, max_samples=32000):
        self.queue = asyncio.Queue(maxsize=64)
        self.max_samples = max_samples
        self.buffered_samples = 0

    def put_nowait(self, item):
        samples = len(item) // 2 if isinstance(item, bytes) else 0
        if self.buffered_samples + samples > self.max_samples:
            raise asyncio.QueueFull
        self.queue.put_nowait(item)
        self.buffered_samples += samples

    async def get(self):
        return await self.queue.get()

    def sent(self, samples):
        self.buffered_samples -= samples

    def qsize(self):
        return self.queue.qsize()


class StreamClient:
    """Send PCM under absolute credit while independently receiving snapshots."""

    def __init__(self, api_url, token, *, on_ready=None, on_event=None,
                 on_sent=None, on_abort=None, keepalive_seconds=10,
                 finish_timeout=250):
        self.url = api_url.rstrip("/").replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1) + "/api/dictation/v1/stream"
        self.token = token
        self.on_ready = on_ready
        self.on_event = on_event
        self.on_sent = on_sent
        self.on_abort = on_abort
        self.keepalive_seconds = keepalive_seconds
        self.finish_timeout = finish_timeout
        self.sent_samples = 0
        self.audio_send_limit = 0
        self.received_samples = 0
        self.processed_samples = 0
        self.identity = None
        self.seq = 0
        self.text = ""
        self.credit = asyncio.Event()
        self.send_lock = asyncio.Lock()

    @staticmethod
    def _count(value, name):
        number = value.get(name)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise CaptureError("流式服务返回了无效的音频进度")
        return number

    @staticmethod
    def _decode(message):
        try:
            value = json.loads(message)
        except (ValueError, TypeError) as exc:
            raise CaptureError("流式服务返回了无效消息") from exc
        if not isinstance(value, dict):
            raise CaptureError("流式服务返回了无效消息")
        return value

    @staticmethod
    def _error(event):
        return CaptureError(event.get("message") or event.get("detail") or
                            "流式识别未完成，请检查服务状态")

    def _progress(self, event):
        received = self._count(event, "audio_received_samples")
        processed = self._count(event, "audio_processed_samples")
        limit = self._count(event, "audio_send_limit")
        if (received < self.received_samples or processed < self.processed_samples or
                processed > received or received > self.sent_samples or
                limit < self.audio_send_limit or limit < received):
            raise CaptureError("流式服务返回了倒退的音频进度")
        self.received_samples, self.processed_samples = received, processed
        self.audio_send_limit = limit
        self.credit.set()

    def _accept(self, event, *, ready=False):
        identity = tuple(event.get(key) for key in (
            "server_instance_id", "model_generation", "session_id"))
        seq = event.get("seq")
        if (not all(isinstance(value, str) and value for value in identity) or
                isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0):
            raise CaptureError("流式服务返回了无效的会话标识")
        if ready:
            self.identity = identity
        elif identity != self.identity:
            raise CaptureError("流式会话标识发生变化，已停止本轮")
        if seq <= self.seq:
            return False
        self.seq = seq
        if event["type"] in {"ready", "flow"}:
            self._progress(event)
        elif "audio_processed_samples" in event:
            processed = self._count(event, "audio_processed_samples")
            if processed < self.processed_samples or processed > self.sent_samples:
                raise CaptureError("流式服务返回了倒退的音频进度")
            self.processed_samples = processed
        if event["type"] in {"partial", "final", "error"}:
            text = event.get("text")
            if not isinstance(text, str) or not text.startswith(self.text):
                raise CaptureError("流式结果修改了已输入文字，已停止本轮；全文保存在本机")
            self.text = text
        return True

    async def _send_control(self, ws, kind):
        async with self.send_lock:
            message = {"type": kind}
            if kind in {"flush", "finish"}:
                message["after_audio_samples"] = self.sent_samples
            await ws.send(json.dumps(message))

    async def _send(self, ws, stream):
        while True:
            item = await stream.get()
            if isinstance(item, bytes):
                if not 2 <= len(item) <= 5120 or len(item) % 2:
                    raise CaptureError("流式音频帧须为 2–5120 字节的 PCM16")
                samples = len(item) // 2
                while self.sent_samples + samples > self.audio_send_limit:
                    self.credit.clear()
                    await self.credit.wait()
                async with self.send_lock:
                    self.sent_samples += samples
                    await ws.send(item)
                if isinstance(stream, StreamInput):
                    stream.sent(samples)
                kind = "audio"
            else:
                kind = "finish" if item is None else item
                if kind not in {"flush", "finish"}:
                    raise CaptureError("不支持的流式控制")
                await self._send_control(ws, kind)
            if self.on_sent:
                self.on_sent(kind, self.sent_samples)
            if kind == "finish":
                return

    async def _receive(self, ws):
        while True:
            event = self._decode(await ws.recv())
            if event.get("type") not in {"flow", "partial", "final", "error", "keepalive"}:
                raise CaptureError("流式服务返回了未知事件")
            if not self._accept(event):
                continue
            if self.on_event:
                self.on_event(event)
            if event["type"] == "error":
                raise self._error(event)
            if event["type"] == "final":
                return event

    async def _keepalive(self, ws):
        while True:
            await asyncio.sleep(self.keepalive_seconds)
            await self._send_control(ws, "keepalive")

    async def run(self, stream):
        from websockets.asyncio.client import connect

        async with connect(self.url, proxy=None,
                           additional_headers={"Authorization": "Bearer " + self.token},
                           max_size=2 * 1024 * 1024, max_queue=8,
                           ping_interval=20, ping_timeout=30,
                           open_timeout=10, close_timeout=2) as ws:
            tasks = []
            try:
                await ws.send(json.dumps({
                    "type": "start", "protocol_version": 1, "mode": "r2t2",
                    "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                }))
                ready = self._decode(await asyncio.wait_for(ws.recv(), 250))
                if ready.get("type") == "error":
                    raise self._error(ready)
                if ready.get("type") != "ready":
                    raise CaptureError("流式引擎未就绪")
                self._accept(ready, ready=True)
                if self.on_ready:
                    self.on_ready(ready)
                sender = asyncio.create_task(self._send(ws, stream))
                receiver = asyncio.create_task(self._receive(ws))
                keeper = asyncio.create_task(self._keepalive(ws))
                tasks = [sender, receiver, keeper]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if receiver in done:
                    return receiver.result()
                return await asyncio.wait_for(receiver, self.finish_timeout)
            except BaseException:
                if self.on_abort:
                    self.on_abort()
                for task in tasks:
                    task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(self._send_control(ws, "cancel"), .25)
                raise
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
