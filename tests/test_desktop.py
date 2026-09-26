"""Desktop state transitions and the boundary between dictation and X11 input."""

import asyncio
from dataclasses import replace
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from oneaxe_voice.capture import CaptureError, Recording, pack_recording, select_source
from oneaxe_voice.config import Settings
from oneaxe_voice.desktop import Desktop
from oneaxe_voice.paste import Target, deliver, paste_key, plain_text


TARGET = Target("101", "102", '"gnome-terminal-server", "Gnome-terminal"', "103")
DJI = {"name": "input.usb-DJI-Rx", "description": "DJI Mic", "mute": False}


class CaptureTests(unittest.TestCase):
    def test_device_requires_unique_dji_and_rejects_muted(self):
        self.assertEqual(select_source([DJI]), DJI)
        for items in ([], [{"name": "builtin"}], [DJI, DJI], [{**DJI, "mute": True}]):
            with self.subTest(items=items), self.assertRaises(CaptureError):
                select_source(items)

    def test_explicit_device_selection(self):
        other = {"name": "external", "mute": False}
        self.assertEqual(select_source([DJI, other], "external"), other)
        with self.assertRaises(CaptureError):
            select_source([DJI], "missing")

    def test_pcm_bounds_and_signal_levels(self):
        recording = pack_recording(struct.pack("<h", 1000) * 16000)
        self.assertEqual(recording.seconds, 1)
        self.assertAlmostEqual(recording.rms_dbfs, -30.3, places=1)
        self.assertTrue(recording.wav.startswith(b"RIFF"))
        self.assertLess(pack_recording(b"\x00\x00" * 1600).loudest_frame_dbfs, -100)
        with self.assertRaises(CaptureError):
            pack_recording(b"\x00\x00" * 10)


class PasteTests(unittest.TestCase):
    def test_no_enter_or_control_characters(self):
        self.assertEqual(plain_text("你好\r\n世界\x00\x1b\t结束\n"), "你好 世界 结束")
        self.assertEqual(paste_key(TARGET.wm_class), "ctrl+shift+v")
        self.assertEqual(paste_key('"code", "Code"'), "shift+Insert")
        self.assertEqual(paste_key('"chromium", "Chromium"'), "ctrl+v")

    def test_focus_change_leaves_clipboard_without_keystroke(self):
        with patch("oneaxe_voice.paste.subprocess.run") as run, patch(
            "oneaxe_voice.paste.current_target", return_value=replace(TARGET, focus="other")
        ):
            self.assertEqual(deliver("识别结果", TARGET), "focus_changed")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["input"], "识别结果".encode())

    def test_matching_terminal_pastes_without_return(self):
        with patch("oneaxe_voice.paste.subprocess.run") as run, patch(
            "oneaxe_voice.paste.current_target", return_value=TARGET
        ):
            self.assertEqual(deliver("你好\n", TARGET), "pasted")
            self.assertEqual(run.call_args.args[0], ["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"])
            self.assertEqual(run.call_args_list[0].kwargs["input"], "你好".encode())


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), runtime_dir=Path(self.temp.name))
        self.settings.token_path.write_text("a" * 43)
        self.desktop = Desktop(self.settings)
        self.capture_entered = asyncio.Event()
        self.inference_entered = asyncio.Event()
        self.allow_inference = asyncio.Event()
        self.allow_inference.set()
        self.volume = -10

        async def recording(source, stop, seconds):
            self.capture_entered.set()
            await stop.wait()
            return Recording(b"wav", 1, self.volume, self.volume)

        async def post(*args, **kwargs):
            self.inference_entered.set()
            await self.allow_inference.wait()
            return httpx.Response(200, json={"request_id": "test", "text": "听写测试\n", "device": "cuda:0"})

        client = AsyncMock()
        client.post.side_effect = post
        client.__aenter__.return_value = client
        self.mocks = {}
        values = {
            "current_target": {"return_value": TARGET},
            "sources": {"return_value": [DJI]},
            "record": {"side_effect": recording},
            "notify": {"new_callable": AsyncMock},
            "deliver": {"return_value": "pasted"},
            "httpx.AsyncClient": {"return_value": client},
        }
        for name, options in values.items():
            patcher = patch("oneaxe_voice.desktop." + name, **options)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        if self.desktop.task:
            await self.desktop.dispatch("cancel")
        self.temp.cleanup()

    async def start_recording(self):
        await self.desktop.dispatch("toggle")
        await asyncio.wait_for(self.capture_entered.wait(), 2)
        self.desktop.last_toggle = 0

    async def test_toggle_record_transcribe_paste(self):
        await self.start_recording()
        self.assertEqual(self.desktop.state["state"], "recording")
        task = self.desktop.task
        await self.desktop.dispatch("toggle")
        await task
        self.assertEqual(self.desktop.state["last_action"], "pasted")
        self.mocks["deliver"].assert_called_once_with("听写测试", TARGET, False)
        self.assertEqual((self.settings.runtime_dir / "last-transcript.txt").read_text(), "听写测试")

    async def test_cancel_capture_never_calls_model_or_pastes(self):
        await self.start_recording()
        await self.desktop.dispatch("cancel")
        self.assertEqual(self.desktop.state["last_action"], "cancelled")
        self.mocks["httpx.AsyncClient"].assert_not_called()
        self.mocks["deliver"].assert_not_called()

    async def test_cancel_inference_never_pastes(self):
        self.allow_inference.clear()
        await self.start_recording()
        await self.desktop.dispatch("toggle")
        await asyncio.wait_for(self.inference_entered.wait(), 2)
        await self.desktop.dispatch("cancel")
        self.mocks["deliver"].assert_not_called()
        self.assertFalse((self.settings.runtime_dir / "last-transcript.txt").exists())

    async def test_silence_skips_model_and_preserves_clipboard(self):
        self.volume = -90
        await self.start_recording()
        task = self.desktop.task
        await self.desktop.dispatch("toggle")
        await task
        self.assertEqual(self.desktop.state["last_action"], "silence")
        self.mocks["httpx.AsyncClient"].assert_not_called()
        self.mocks["deliver"].assert_not_called()

    async def test_busy_toggle_does_not_create_second_recording(self):
        self.allow_inference.clear()
        await self.start_recording()
        await self.desktop.dispatch("toggle")
        await asyncio.wait_for(self.inference_entered.wait(), 2)
        self.desktop.last_toggle = 0
        await self.desktop.dispatch("toggle")
        self.assertEqual(self.desktop.state["state"], "transcribing")
        self.assertEqual(self.mocks["record"].call_count, 1)
        self.allow_inference.set()
        await self.desktop.task


if __name__ == "__main__":
    unittest.main()
