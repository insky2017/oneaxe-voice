"""A small desktop controller: toggle capture, call ASR, and deliver clipboard text."""

import asyncio
from contextlib import aclosing, suppress
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import time
import tempfile
import uuid

import httpx

from .capture import CaptureError, pack_recording, pcm_chunks, segment_recordings, select_source, sources
from .config import Settings
from .paste import append_delta, copy_text, current_target, deliver, plain_text
from .modes import MODES, validate_mode

LOGGER = logging.getLogger(__name__)


def preferences(settings: Settings) -> dict:
    """Read local preferences; source=None means a uniquely identified DJI device."""
    path = settings.runtime_dir / "desktop.json"
    value = {"source": None, "clipboard_only": False, "shortcut": "F8", "mode": "vad", "preview": True,
             "icon_theme": "light",
             "pause_ms": 700, "segment_seconds": 15, "max_session_seconds": 900,
             "vad_mode": 2, "vad_min_dbfs": -60, "queue_size": 8}
    if path.exists():
        value.update(json.loads(path.read_text()))
    validate_mode(value["mode"])
    for key, low, high in (("pause_ms", 300, 2000), ("segment_seconds", 3, 30),
                           ("max_session_seconds", 10, 3600), ("vad_min_dbfs", -100, 0)):
        value[key] = float(value[key])
        if not low <= value[key] <= high:
            raise ValueError(f"{key} 须为 {low}–{high}")
    for key, low, high in (("vad_mode", 0, 3), ("queue_size", 1, 16)):
        number = float(value[key])
        if not number.is_integer() or not low <= number <= high:
            raise ValueError(f"{key} 须为 {low}–{high} 的整数")
        value[key] = int(number)
    return value


async def notify(title: str, message: str) -> None:
    """Show transient status without disclosing the transcript."""
    def send():
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["notify-send", "--app-name=OneAxe Voice", "--icon=audio-input-microphone",
                 "--expire-time=4000", "--hint=string:x-canonical-private-synchronous:oneaxe-voice",
                 title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
            )
    await asyncio.to_thread(send)


class Desktop:
    """Record a session continuously and consume its utterances in strict order."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.task = None
        self.delivery_task = None
        self.stop = asyncio.Event()
        self.started = 0.0
        self.last_toggle = 0.0
        self.prepare_task = None
        self.menu_until = 0.0
        self.preview = ""
        self.full_text = ""
        self.records = []
        self.only_copy = False
        self.session_id = ""
        self.state = {"state": "idle", "last_action": None, "last_error": None,
                      "capture_active": False, "recognizing": False}

    def status(self) -> dict:
        value = dict(self.state)
        value["elapsed_seconds"] = round(time.monotonic() - self.started, 1) if self.task else 0
        value["pid"] = os.getpid()
        config = preferences(self.settings)
        value["selected_mode"] = config["mode"]
        value.setdefault("mode", config["mode"])
        value["preparing"] = self.prepare_task is not None
        value["preview_enabled"] = config["preview"]
        value["clipboard_only"] = config["clipboard_only"]
        value["menu_paused"] = time.monotonic() < self.menu_until
        return value

    async def dispatch(self, action: str, **options) -> dict:
        if action == "status":
            return self.status()
        if action == "ui":
            if options.get("menu_open"):
                self.menu_until = time.monotonic() + 3
            return {**self.status(), "preview": self.preview[-600:]}
        if action == "menu":
            self.menu_until = time.monotonic() + (3 if options.get("opened") else .3)
            return self.status()
        if action == "configure":
            config = preferences(self.settings)
            for key, value in options.items():
                if key == "mode":
                    validate_mode(value)
                elif key == "icon_theme" and value in {"light", "dark"}:
                    pass
                elif key == "source" and (value is None or isinstance(value, str)):
                    pass
                elif key == "pause_ms" and isinstance(value, (float, int)) and 300 <= value <= 2000:
                    pass
                elif key == "max_session_seconds" and isinstance(value, (float, int)) and 10 <= value <= 3600:
                    pass
                elif key not in {"preview", "clipboard_only"} or not isinstance(value, bool):
                    raise ValueError("不支持的桌面设置")
                config[key] = value
            self._atomic_json("desktop.json", config)
            if "mode" in options and not self.task and self.prepare_task is None:
                self.prepare_task = asyncio.create_task(self._prepare_selected())
            return self.status()
        if action == "copy":
            path = self.settings.runtime_dir / "last-transcript.txt"
            text = self.full_text or (path.read_text() if path.exists() else "")
            if text:
                await asyncio.to_thread(copy_text, text)
            return self.status()
        if action == "cancel":
            if self.task:
                if not self.task.cancelling():
                    self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            return self.status()
        if action != "toggle":
            raise ValueError("未知桌面操作")
        now = time.monotonic()
        if now - self.last_toggle < 0.35:
            return self.status()
        self.last_toggle = now
        if self.task:
            if self.state.get("capture_active") and not self.stop.is_set():
                self.stop.set()
                self.state["state"] = "finishing"
            else:
                await notify("OneAxe Voice 正在收尾", "剩余语音按顺序识别，完成后可开启下一轮")
            return self.status()
        config = preferences(self.settings)
        self.state = {"state": "starting", "last_action": None, "last_error": None,
                      "capture_active": True, "recognizing": False, "warming": True,
                      "segments_done": 0, "segments_pasted": 0, "queued_segments": 0,
                      "voice_active": False, "audio_seconds": 0, "mode": config["mode"]}
        self.preview, self.full_text, self.records = "", "", []
        self.session_id = str(uuid.uuid4())
        self.only_copy = config["clipboard_only"]
        self.started = now
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self.dictate())
        return self.status()

    def _atomic_json(self, name, value):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.settings.runtime_dir, delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, ensure_ascii=False, indent=2)
        try:
            temporary.replace(self.settings.runtime_dir / name)
        finally:
            temporary.unlink(missing_ok=True)

    async def _prepare_selected(self):
        try:
            token = self.settings.token_path.read_text().strip()
            async with httpx.AsyncClient(base_url=self.settings.api_url, trust_env=False,
                                         timeout=250, headers={"Authorization": "Bearer " + token}) as client:
                while True:
                    mode = preferences(self.settings)["mode"]
                    self.state.update(preparing_mode=mode, last_error=None)
                    await self._request(client, "/api/dictation/prepare", json={"mode": mode})
                    if mode == preferences(self.settings)["mode"] or self.task:
                        break
        except Exception:
            self.state["last_error"] = "模式加载失败，可重试或切回稳听；详情见本机日志"
        finally:
            self.prepare_task = None

    async def _wait_menu(self):
        while time.monotonic() < self.menu_until:
            await asyncio.sleep(.05)

    async def _request(self, client, path, **kwargs):
        """Retry only explicit busy rejections; never resubmit an uncertain request."""
        deadline = time.monotonic() + 30
        while True:
            response = await client.post(path, **kwargs)
            if response.status_code == 429 and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                continue
            if response.is_error:
                detail = response.json().get("detail", "识别服务返回错误")
                raise CaptureError(f"HTTP {response.status_code}: {detail}")
            return response.json()

    async def _warmup(self, client):
        try:
            if self.prepare_task:
                await asyncio.shield(self.prepare_task)
            return await self._request(client, "/api/dictation/warmup")
        finally:
            self.state["warming"] = False

    async def _produce(self, source, config, queue):
        overflow = None

        def progress(active, seconds):
            self.state.update(voice_active=active, audio_seconds=round(seconds, 2))

        async with aclosing(segment_recordings(source, self.stop, config, progress)) as recordings:
            async for segment in recordings:
                try:
                    queue.put_nowait(segment)
                except asyncio.QueueFull:
                    # Close/reap the recorder before waiting for queue space. Keep
                    # this final segment; do not silently lose it or grow memory.
                    overflow = segment
                    self.stop.set()
                    self.state["stopped_reason"] = "backlog"
                    break
                self.state["queued_segments"] = queue.qsize()
        self.state.update(capture_active=False, voice_active=False, state="finishing")
        if not self.stop.is_set():
            self.state["stopped_reason"] = "session_limit"
        self.stop.set()
        if overflow is not None:
            await notify("OneAxe Voice 已停止录音", "识别积压，正在处理已录制的语音")
            await queue.put(overflow)
        await queue.put(None)

    def _save(self, session_id, text, records):
        """Atomically retain the complete result and raw per-segment ASR text."""
        values = {
            "last-transcript.txt": text,
            "last-session.json": json.dumps(
                {"session_id": session_id, "text": text, "segments": records},
                ensure_ascii=False, indent=2,
            ) + "\n",
        }
        for name, value in values.items():
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.settings.runtime_dir,
                                             delete=False) as output:
                temporary = Path(output.name)
                output.write(value)
            try:
                temporary.replace(self.settings.runtime_dir / name)
            finally:
                temporary.unlink(missing_ok=True)

    async def _consume(self, client, warmup, queue, target, config):
        while True:
            segment = await queue.get()
            self.state["queued_segments"] = queue.qsize()
            if segment is None:
                return
            await warmup
            self.state["recognizing"] = True
            recording = pack_recording(segment.pcm)
            result = await self._request(
                client, "/api/dictation/transcribe",
                files={"file": ("segment.wav", recording.wav, "audio/wav")},
            )
            text = plain_text(result["text"])
            self.state.update(recognizing=False, request_id=result["request_id"],
                              device=result.get("device"),
                              segments_done=self.state["segments_done"] + 1)
            if not text:
                continue
            accumulated = self.full_text + append_delta(self.full_text, text)
            self.preview = accumulated[-600:]
            await self._publish(accumulated, result["text"], segment.reason,
                                recording.seconds, result["request_id"], target)

    async def _publish(self, accumulated, raw, reason, seconds, request_id, target):
        if not accumulated.startswith(self.full_text):
            raise CaptureError("流式结果修改了已输入文字，已停止本轮；全文保存在本机")
        delta = accumulated[len(self.full_text):]
        if not delta:
            return
        self.full_text = accumulated
        self.records.append({"sequence": self.state["segments_done"], "text": raw,
                             "reason": reason, "audio_seconds": seconds, "request_id": request_id,
                             "mode": self.state["mode"],
                             "received_at_seconds": round(time.monotonic() - self.started, 3)})
        self._save(self.session_id, accumulated, self.records)
        await self._wait_menu()
        payload = accumulated if self.only_copy else delta
        self.delivery_task = asyncio.create_task(asyncio.to_thread(
            deliver, payload, target, self.only_copy,
            prefix=" " if not self.only_copy and delta.startswith(" ") else "",
        ))
        try:
            action = await asyncio.shield(self.delivery_task)
        finally:
            await asyncio.gather(self.delivery_task, return_exceptions=True)
            self.delivery_task = None
        if action == "focus_changed":
            self.only_copy = True
            self.state["paste_paused"] = True
            await asyncio.to_thread(copy_text, accumulated)
            await notify("OneAxe Voice 已改为复制", "窗口发生变化，本轮后续内容只累积到剪贴板")
        if action == "pasted":
            self.state["segments_pasted"] += 1
        self.state["last_action"] = action

    async def _produce_stream(self, source, config, queue):
        total = 0
        pending = bytearray()
        overflow = None
        async with aclosing(pcm_chunks(source, self.stop, config["max_session_seconds"])) as stream:
            async for chunk in stream:
                pending.extend(chunk)
                total += len(chunk)
                self.state["audio_seconds"] = round(total / 32000, 2)
                while len(pending) >= 5120:
                    data = bytes(pending[:5120])
                    del pending[:5120]
                    try:
                        queue.put_nowait(data)
                    except asyncio.QueueFull:
                        overflow = data
                        self.stop.set()
                        self.state["stopped_reason"] = "backlog"
                        break
                self.state["queued_segments"] = queue.qsize()
                if overflow is not None:
                    break
        self.state.update(capture_active=False, state="finishing")
        if overflow is not None:
            await notify("OneAxe Voice 已停止录音", "流式识别积压，正在补齐已录制内容")
            await queue.put(overflow)
        if pending:
            await queue.put(bytes(pending[:len(pending) // 2 * 2]))
        await queue.put(None)

    async def _consume_stream(self, queue, target, mode):
        from websockets.asyncio.client import connect
        if self.prepare_task:
            await asyncio.shield(self.prepare_task)
        token = self.settings.token_path.read_text().strip()
        async with connect(self.settings.api_url.rstrip('/').replace('http://', 'ws://', 1) + "/api/dictation/stream", proxy=None,
                           additional_headers={"Authorization": "Bearer " + token},
                           max_size=2 * 1024 * 1024, max_queue=2,
                           ping_interval=20, ping_timeout=250, open_timeout=10, close_timeout=2) as ws:
            await ws.send(json.dumps({"mode": mode}))
            ready = json.loads(await asyncio.wait_for(ws.recv(), 250))
            if ready["type"] != "ready":
                raise CaptureError(ready.get("detail", "流式引擎未就绪"))
            self.state["warming"] = False
            last_sent = 0.0
            pending_result = None
            raw_committed = ""
            while True:
                data = await queue.get()
                self.state.update(queued_segments=queue.qsize(), recognizing=True)
                await ws.send("finish" if data is None else data)
                result = json.loads(await asyncio.wait_for(ws.recv(), 250))
                if result["type"] == "error":
                    raise CaptureError(result["detail"])
                self.state.update(recognizing=False, device=result["device"], request_id=result["request_id"],
                                  window_seconds=result.get("window_seconds"), inference_ms=result.get("inference_ms"))
                self.preview = result.get("preview", "")
                if plain_text(result["text"]) != self.full_text:
                    pending_result = result
                if pending_result is not None and (data is None or time.monotonic() - last_sent >= .25):
                    self.state["segments_done"] += 1
                    raw_delta = pending_result["text"][len(raw_committed):]
                    raw_committed = pending_result["text"]
                    await self._publish(plain_text(pending_result["text"]), raw_delta,
                                        "stop" if data is None else "stream", result["audio_seconds"],
                                        result["request_id"], target)
                    pending_result = None
                    last_sent = time.monotonic()
                if data is None:
                    break

    async def dictate(self) -> None:
        """Supervise capture and ordered ASR; errors/cancel stop and reap every task."""
        children = []
        try:
            config = preferences(self.settings)
            config["mode"] = self.state["mode"]  # Freeze the selected engine for this session.
            await self._wait_menu()
            await asyncio.sleep(0.15)  # Let GNOME release its shortcut keyboard grab.
            target = await asyncio.to_thread(current_target)
            self.state.update(target_window=target.window, target_focus=target.focus)
            source = select_source(await asyncio.to_thread(sources), config["source"])
            self.state.update(state="recording", source=source["description"])
            token = self.settings.token_path.read_text().strip()
            async with httpx.AsyncClient(
                base_url=self.settings.api_url, trust_env=False, timeout=180,
                headers={"Authorization": "Bearer " + token},
            ) as client:
                if config["mode"] == "vad":
                    warmup = asyncio.create_task(self._warmup(client))
                    queue = asyncio.Queue(maxsize=config["queue_size"])
                    producer = asyncio.create_task(self._produce(source["name"], config, queue))
                    consumer = asyncio.create_task(self._consume(client, warmup, queue, target, config))
                    children = [producer, consumer, warmup]
                else:
                    queue = asyncio.Queue(maxsize=400)  # At most 64 s / 2.05 MB PCM, including cold start.
                    producer = asyncio.create_task(self._produce_stream(source["name"], config, queue))
                    consumer = asyncio.create_task(self._consume_stream(queue, target, config["mode"]))
                    children = [producer, consumer]
                try:
                    await notify("OneAxe Voice 持续听写中", f"{MODES[config['mode']]} · {config['shortcut']} 结束并补齐尾部")
                    await asyncio.gather(producer, consumer)
                finally:
                    for child in children:
                        if not child.done() and not child.cancelling():
                            child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
            self.state["state"] = "idle"
            if self.state["last_action"] is None:
                self.state["last_action"] = "silence"
                await notify("OneAxe Voice 已结束", "本轮没有识别到可输入的语音")
            else:
                await notify("OneAxe Voice 已结束", f"已处理 {self.state['segments_done']} 段，全文保存在本机")
        except asyncio.CancelledError:
            self.state.update(state="idle", last_action="cancelled")
            await notify("OneAxe Voice 已取消", "后续识别已停止，已经输入的文字保留")
        except Exception as exc:
            message = str(exc) if isinstance(exc, (CaptureError, ValueError)) else "听写失败，已完成部分保存在本机，请检查服务状态"
            if isinstance(exc, subprocess.CalledProcessError):
                program = Path(exc.cmd[0]).name
                message = f"桌面命令 {program} 失败，请确认焦点位于输入窗口"
                LOGGER.error("desktop_command_failed program=%s exit=%s", program, exc.returncode)
            self.state.update(state="error", last_error=message)
            LOGGER.error("dictation_error kind=%s", type(exc).__name__)
            await notify("OneAxe Voice 未完成", message)
        finally:
            self.stop.set()
            self.state.update(capture_active=False, recognizing=False, warming=False, voice_active=False,
                              queued_segments=0)
            self.task = None


async def serve(settings: Settings) -> None:
    """Expose a private Unix socket so repeated shortcut invocations share one recorder."""
    if os.environ.get("XDG_SESSION_TYPE") != "x11" or not os.environ.get("DISPLAY"):
        raise ValueError("桌面输入当前需要 X11 会话")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.runtime_dir.chmod(0o700)
    lock = (settings.runtime_dir / "desktop.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise ValueError("桌面服务已经运行") from exc
    path = settings.runtime_dir / "desktop.sock"
    path.unlink(missing_ok=True)
    desktop = Desktop(settings)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    async def handle(reader, writer):
        try:
            credentials = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("3i", credentials)[1] != os.getuid():
                raise ValueError("仅允许当前用户")
            request = json.loads(await asyncio.wait_for(reader.readline(), 3))
            result = await desktop.dispatch(request.pop("action"), **request)
            writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except Exception:
            writer.write(b'{"error":"desktop request failed"}\n')
            with suppress(Exception):
                await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    try:
        server = await asyncio.start_unix_server(handle, str(path), limit=4096)
        path.chmod(0o600)
        async with server:
            await shutdown.wait()
            await desktop.dispatch("cancel")
            if desktop.prepare_task:
                desktop.prepare_task.cancel()
                await asyncio.gather(desktop.prepare_task, return_exceptions=True)
    finally:
        path.unlink(missing_ok=True)
        lock.close()


def control(settings: Settings, action: str, **options) -> dict:
    """Send a short command; the shortcut can start its installed user service on demand."""
    path = settings.runtime_dir / "desktop.sock"
    if action == "toggle":
        unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user/oneaxe-voice-tray.service"
        if unit.exists():
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run(["systemctl", "--user", "start", "oneaxe-voice-tray.service"],
                               capture_output=True, timeout=5)
    for attempt in range(21):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(10)
                client.connect(str(path))
                client.sendall((json.dumps({"action": action, **options}) + "\n").encode())
                with client.makefile("rb") as stream:
                    result = json.loads(stream.readline(65536))
                if "error" in result:
                    raise ValueError(result["error"])
                return result
        except (FileNotFoundError, ConnectionRefusedError):
            if action != "toggle":
                return {"state": "stopped"}
            if attempt == 0:
                subprocess.run(["systemctl", "--user", "start", "oneaxe-voice-desktop.service"], check=True, timeout=15)
            time.sleep(0.1)
    raise ValueError("桌面服务未启动，请先运行 desktop-setup 并检查日志")
