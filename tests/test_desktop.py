"""Desktop state transitions and the boundary between dictation and X11 input."""

import asyncio
from dataclasses import replace
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from oneaxe_voice.capture import CaptureError, Recording, pack_recording, select_source
from oneaxe_voice.config import Settings
from oneaxe_voice.desktop import Desktop
from oneaxe_voice.paste import Target, append_delta, deliver, paste_key, plain_text
from oneaxe_voice.vad import Segment


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
    def test_appended_english_keeps_word_boundary(self):
        self.assertEqual(append_delta("Hello", "world"), " world")
        self.assertEqual(append_delta("你好。", "下一句"), "下一句")
        with patch("oneaxe_voice.paste.subprocess.run") as run, patch(
            "oneaxe_voice.paste.current_target", return_value=TARGET
        ):
            deliver("world", TARGET, prefix=" ")
            self.assertEqual(run.call_args_list[0].kwargs["input"], b" world")

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
        self.capture_closed = asyncio.Event()
        self.inference_entered = asyncio.Event()
        self.allow_inference = asyncio.Event()
        self.allow_inference.set()
        self.allow_warmup = asyncio.Event()
        self.allow_warmup.set()
        self.segments = asyncio.Queue()
        self.outputs = []
        self.calls = []
        self.final_segment = None
        self.http_error = False

        async def recording(source, stop, config, progress):
            self.capture_entered.set()
            try:
                while not stop.is_set():
                    take = asyncio.create_task(self.segments.get())
                    stopping = asyncio.create_task(stop.wait())
                    try:
                        done, _ = await asyncio.wait([take, stopping], return_when=asyncio.FIRST_COMPLETED)
                        if take in done:
                            yield take.result()
                        if stopping in done:
                            break
                    finally:
                        take.cancel()
                        stopping.cancel()
                        await asyncio.gather(take, stopping, return_exceptions=True)
                if self.final_segment:
                    yield self.final_segment
            finally:
                self.capture_closed.set()

        async def post(path, **kwargs):
            self.calls.append(path)
            if path.endswith('/warmup'):
                await self.allow_warmup.wait()
                return httpx.Response(200, json={"model_loaded": True, "device": "cuda:0"})
            self.inference_entered.set()
            await self.allow_inference.wait()
            if self.http_error:
                return httpx.Response(503, json={"detail": "GPU unavailable"})
            index = len([value for value in self.calls if value.endswith('/transcribe')])
            return httpx.Response(200, json={"request_id": str(index),
                "text": f"嗯，就是，就是，第{index}段。", "device": "cuda:0"})

        def deliver_text(text, target, clipboard_only, **kwargs):
            self.outputs.append((text, clipboard_only, kwargs.get('prefix')))
            return 'pasted'

        client = AsyncMock()
        client.post.side_effect = post
        client.__aenter__.return_value = client
        self.mocks = {}
        values = {
            "current_target": {"return_value": TARGET},
            "sources": {"return_value": [DJI]},
            "segment_recordings": {"side_effect": recording},
            "notify": {"new_callable": AsyncMock},
            "deliver": {"side_effect": deliver_text},
            "copy_text": {},
            "httpx.AsyncClient": {"return_value": client},
        }
        for name, options in values.items():
            patcher = patch("oneaxe_voice.desktop." + name, **options)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        self.allow_inference.set()
        self.allow_warmup.set()
        if self.desktop.task:
            await self.desktop.dispatch("cancel")
        self.temp.cleanup()

    def segment(self, reason='pause'):
        return Segment(struct.pack('<h', 1000) * 3200, reason, 1.0)

    async def eventually(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail('condition did not become true')

    async def start_recording(self):
        await self.desktop.dispatch("toggle")
        await asyncio.wait_for(self.capture_entered.wait(), 2)
        self.desktop.last_toggle = 0

    async def finish(self):
        task = self.desktop.task
        self.desktop.last_toggle = 0
        await self.desktop.dispatch('toggle')
        if task:
            await asyncio.wait_for(task, 2)

    async def test_outputs_before_stop_in_order_without_filler_cleanup(self):
        await self.start_recording()
        await self.segments.put(self.segment())
        await self.eventually(lambda: len(self.outputs) == 1)
        self.assertTrue(self.desktop.state['capture_active'])
        self.assertFalse(self.desktop.stop.is_set())
        await self.segments.put(self.segment())
        await self.eventually(lambda: len(self.outputs) == 2)
        await self.finish()
        expected = ['嗯，就是，就是，第1段。', '嗯，就是，就是，第2段。']
        self.assertEqual([item[0] for item in self.outputs], expected)
        self.assertEqual((self.settings.runtime_dir / 'last-transcript.txt').read_text(), ''.join(expected))

    async def test_mode_switch_during_recording_applies_to_next_session(self):
        await self.start_recording()
        await self.desktop.dispatch('configure', mode='r2t2')
        self.assertEqual(self.desktop.status()['selected_mode'], 'r2t2')
        self.assertEqual(self.desktop.status()['mode'], 'vad')
        self.assertIsNone(self.desktop.prepare_task)
        await self.segments.put(self.segment())
        await self.eventually(lambda: len(self.outputs) == 1)
        await self.finish()

    async def test_menu_pauses_delivery_then_resumes_without_changing_target(self):
        await self.start_recording()
        await self.desktop.dispatch('menu', opened=True)
        await self.segments.put(self.segment())
        await asyncio.sleep(.1)
        self.assertFalse(self.outputs)
        await self.desktop.dispatch('menu', opened=False)
        await self.eventually(lambda: len(self.outputs) == 1)
        self.assertFalse(self.desktop.only_copy)
        await self.finish()

    async def test_status_hides_preview_and_invalid_settings_do_not_persist(self):
        self.desktop.preview = 'private transcript'
        self.assertNotIn('preview', self.desktop.status())
        self.assertEqual((await self.desktop.dispatch('ui'))['preview'], 'private transcript')
        with self.assertRaises(ValueError):
            await self.desktop.dispatch('configure', mode='unknown')
        with self.assertRaises(ValueError):
            await self.desktop.dispatch('configure', pause_ms=0)
        self.assertFalse((self.settings.runtime_dir / 'desktop.json').exists())

    async def test_warmup_does_not_block_capture(self):
        self.allow_warmup.clear()
        await self.start_recording()
        await self.segments.put(self.segment())
        await self.segments.put(self.segment())
        await asyncio.sleep(.05)
        self.assertFalse(self.inference_entered.is_set())
        self.assertTrue(self.desktop.state['capture_active'])
        self.allow_warmup.set()
        await self.eventually(lambda: len(self.outputs) == 2)
        await self.finish()

    async def test_stop_flushes_last_phrase(self):
        self.final_segment = self.segment('stop')
        await self.start_recording()
        await self.finish()
        self.assertEqual(len(self.outputs), 1)
        import json
        audit = json.loads((self.settings.runtime_dir / 'last-session.json').read_text())
        self.assertEqual(audit['segments'][0]['reason'], 'stop')

    async def test_cancel_capture_never_transcribes_or_pastes(self):
        await self.start_recording()
        await self.desktop.dispatch('cancel')
        self.assertTrue(self.capture_closed.is_set())
        self.assertEqual(self.desktop.state['last_action'], 'cancelled')
        self.assertFalse(any(path.endswith('/transcribe') for path in self.calls))
        self.assertFalse(self.outputs)

    async def test_cancel_inference_stops_capture_and_never_pastes(self):
        self.allow_inference.clear()
        await self.start_recording()
        await self.segments.put(self.segment())
        await asyncio.wait_for(self.inference_entered.wait(), 2)
        await self.desktop.dispatch('cancel')
        self.assertTrue(self.capture_closed.is_set())
        self.assertFalse(self.outputs)

    async def test_cancel_waits_for_dispatched_paste_and_drops_future_segments(self):
        entered, release = threading.Event(), threading.Event()

        def slow_delivery(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test delivery blocked")
            return "pasted"

        self.mocks['deliver'].side_effect = slow_delivery
        await self.start_recording()
        await self.segments.put(self.segment())
        await self.segments.put(self.segment())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            cancellation = asyncio.create_task(self.desktop.dispatch('cancel'))
            await asyncio.sleep(.05)
            self.assertFalse(cancellation.done())
            release.set()
            await asyncio.wait_for(cancellation, 2)
            self.assertEqual(self.mocks['deliver'].call_count, 1)
            self.assertEqual(self.desktop.state['last_action'], 'cancelled')
        finally:
            release.set()

    async def test_silent_session_does_not_transcribe(self):
        await self.start_recording()
        await self.finish()
        self.assertEqual(self.desktop.state['last_action'], 'silence')
        self.assertFalse(self.outputs)
        self.assertFalse(any(path.endswith('/transcribe') for path in self.calls))

    async def test_focus_change_is_sticky_and_clipboard_accumulates(self):
        self.mocks['deliver'].side_effect = ['focus_changed', 'copied']
        await self.start_recording()
        await self.segments.put(self.segment())
        await self.eventually(lambda: self.desktop.state.get('paste_paused'))
        await self.segments.put(self.segment())
        await self.eventually(lambda: self.desktop.state['segments_done'] == 2)
        await self.finish()
        calls = self.mocks['deliver'].call_args_list
        self.assertFalse(calls[0].args[2])
        self.assertTrue(calls[1].args[2])
        self.assertIn('第1段。嗯，就是，就是，第2段。', calls[1].args[0])
        self.assertEqual(self.mocks['copy_text'].call_count, 1)

    async def test_full_queue_stops_capture_but_preserves_last_segment(self):
        (self.settings.runtime_dir / 'desktop.json').write_text('{"queue_size": 1}')
        self.allow_inference.clear()
        await self.start_recording()
        await self.segments.put(self.segment())
        await asyncio.wait_for(self.inference_entered.wait(), 2)
        await self.segments.put(self.segment())
        await self.segments.put(self.segment())
        await asyncio.wait_for(self.capture_closed.wait(), 2)
        self.assertEqual(self.desktop.state['stopped_reason'], 'backlog')
        self.allow_inference.set()
        await asyncio.wait_for(self.desktop.task, 2)
        self.assertEqual(len(self.outputs), 3)

    async def test_api_error_reaps_capture(self):
        self.http_error = True
        await self.start_recording()
        await self.segments.put(self.segment())
        task = self.desktop.task
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.desktop.state['state'], 'error')
        self.assertTrue(self.capture_closed.is_set())
        self.assertFalse(self.outputs)


if __name__ == "__main__":
    unittest.main()
