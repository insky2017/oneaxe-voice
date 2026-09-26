"""A small desktop controller: toggle capture, call ASR, and deliver clipboard text."""

import asyncio
from contextlib import suppress
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

import httpx

from .capture import CaptureError, record, select_source, sources
from .config import Settings
from .paste import current_target, deliver, plain_text

LOGGER = logging.getLogger(__name__)


def preferences(settings: Settings) -> dict:
    """Read local preferences; source=None means a uniquely identified DJI device."""
    path = settings.runtime_dir / "desktop.json"
    value = {"source": None, "max_seconds": 60, "silence_dbfs": -50,
             "clipboard_only": False, "shortcut": "F8"}
    if path.exists():
        value.update(json.loads(path.read_text()))
    if not 0.1 <= float(value["max_seconds"]) <= 60:
        raise ValueError("max_seconds 须为 0.1–60")
    if not -100 <= float(value["silence_dbfs"]) <= 0:
        raise ValueError("silence_dbfs 须为 -100–0")
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
    """Keep recording state independent from the GPU service."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.task = None
        self.stop = asyncio.Event()
        self.started = 0.0
        self.last_toggle = 0.0
        self.state = {"state": "idle", "last_action": None, "last_error": None}

    def status(self) -> dict:
        value = dict(self.state)
        value["elapsed_seconds"] = round(time.monotonic() - self.started, 1) if self.task else 0
        value["pid"] = os.getpid()
        return value

    async def dispatch(self, action: str) -> dict:
        if action == "status":
            return self.status()
        if action == "cancel":
            if self.task:
                # An X11 paste already dispatched to another thread cannot be
                # recalled. Finish delivery instead of claiming it was cancelled.
                if self.state["state"] == "delivering":
                    await asyncio.shield(self.task)
                else:
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
            if self.state["state"] == "recording":
                self.state["state"] = "stopping"
                self.stop.set()
            else:
                await notify("OneAxe Voice 正在处理", "请等待本次识别结束")
            return self.status()
        self.state = {"state": "starting", "last_action": None, "last_error": None}
        self.started = now
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self.dictate())
        return self.status()

    async def dictate(self) -> None:
        """Process one utterance; cancellation and failures never emit a paste."""
        try:
            config = preferences(self.settings)
            # GNOME releases its shortcut grab before we sample the input focus.
            await asyncio.sleep(0.15)
            target = await asyncio.to_thread(current_target)
            source = select_source(await asyncio.to_thread(sources), config["source"])
            self.state.update(state="recording", source=source["description"])
            await notify("OneAxe Voice 正在录音", f"{source['description']} · 再按 {config['shortcut']} 结束，最长 {config['max_seconds']} 秒")
            recording = await record(source["name"], self.stop, float(config["max_seconds"]))
            self.state.update(audio_seconds=round(recording.seconds, 2), rms_dbfs=recording.rms_dbfs)
            if recording.loudest_frame_dbfs < float(config["silence_dbfs"]):
                self.state.update(state="idle", last_action="silence")
                await notify("OneAxe Voice 未检测到声音", "请检查麦克风发射器、静音和音量")
                return
            self.state["state"] = "transcribing"
            await notify("OneAxe Voice 正在识别", "首次使用需要加载本地模型")
            token = self.settings.token_path.read_text().strip()
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:8097", trust_env=False, timeout=180,
                headers={"Authorization": "Bearer " + token},
            ) as client:
                response = await client.post(
                    "/api/dictation/transcribe",
                    files={"file": ("dictation.wav", recording.wav, "audio/wav")},
                )
            if response.is_error:
                detail = response.json().get("detail", "识别服务返回错误")
                raise CaptureError(f"HTTP {response.status_code}: {detail}")
            result = response.json()
            text = plain_text(result["text"])
            self.state.update(request_id=result["request_id"], device=result.get("device"))
            if not text:
                self.state.update(state="idle", last_action="empty")
                await notify("OneAxe Voice 没有识别到文字", "请重新录制")
                return
            destination = self.settings.runtime_dir / "last-transcript.txt"
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(text)
            self.state["state"] = "delivering"
            action = await asyncio.to_thread(deliver, text, target, bool(config["clipboard_only"]))
            self.state.update(state="idle", last_action=action)
            messages = {"pasted": "文字已输入，未发送回车", "focus_changed": "窗口已切换，文字已复制，可手动粘贴",
                        "copied": "文字已复制，可手动粘贴", "empty": "没有可输入的文字"}
            await notify("OneAxe Voice 完成", messages[action])
        except asyncio.CancelledError:
            self.state.update(state="idle", last_action="cancelled")
            await notify("OneAxe Voice 已取消", "本次录音不会输入文字")
        except Exception as exc:
            message = str(exc) if isinstance(exc, (CaptureError, ValueError)) else "桌面听写失败，请检查设备及服务状态"
            self.state.update(state="error", last_error=message)
            LOGGER.error("dictation_error kind=%s", type(exc).__name__)
            await notify("OneAxe Voice 未完成", message)
        finally:
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
            result = await desktop.dispatch(request["action"])
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
    finally:
        path.unlink(missing_ok=True)
        lock.close()


def control(settings: Settings, action: str) -> dict:
    """Send a short command; the shortcut can start its installed user service on demand."""
    path = settings.runtime_dir / "desktop.sock"
    for attempt in range(21):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(10)
                client.connect(str(path))
                client.sendall((json.dumps({"action": action}) + "\n").encode())
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
