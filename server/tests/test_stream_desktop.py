"""Streaming candidates, endpoints and successful delivery are separate states."""

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from oneaxe_voice.config import Settings
from oneaxe_voice.desktop import Desktop
from oneaxe_voice.capture import CaptureError
from oneaxe_voice.stream_client import StreamInput


class StreamDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_forces_batched_tail_and_finish_does_not_repeat_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(runtime_dir=Path(directory))
            settings.token_path.write_text('a' * 40)
            desktop = Desktop(settings)
            desktop.model_error = 'prior preload failed'
            desktop.started = time.monotonic()
            desktop.state.update(mode='qwen-stream', segments_done=0, segments_pasted=0)
            queue = asyncio.Queue()
            for value in (b'audio', b'audio', 'flush', None):
                queue.put_nowait(value)
            texts = [('涉及到', '体验'), ('涉及到体', '验'), ('涉及到体验。', ''), ('涉及到体验。', '')]
            replies = [{'type': 'ready'}] + [
                {'type': 'partial', 'text': text, 'pending': pending, 'preview': text+pending,
                 'device': 'cuda:0', 'request_id': 'test', 'audio_seconds': 2}
                for text, pending in texts
            ]
            ws = AsyncMock()
            ws.recv.side_effect = [json.dumps(value) for value in replies]
            connection = AsyncMock()
            connection.__aenter__.return_value = ws
            delivered = []
            def deliver(text, *args, **kwargs):
                delivered.append(text)
                return 'pasted'
            with patch('websockets.asyncio.client.connect', return_value=connection), patch(
                'oneaxe_voice.desktop.deliver', side_effect=deliver
            ):
                await desktop._consume_stream(queue, None, 'qwen-stream')
            self.assertEqual(''.join(delivered), '涉及到体验。')
            self.assertEqual(delivered, ['涉及到', '体验。'])
            self.assertIsNone(desktop.model_error)
            self.assertEqual(desktop.state['pause_flushes'], 1)
            self.assertEqual(desktop.records[-1]['reason'], 'pause')
            ui = await desktop.dispatch('ui')
            self.assertEqual(ui['committed_text'], '涉及到体验。')
            self.assertEqual(ui['pending_text'], '')
            self.assertEqual(ui['delivery_state'], 'pasted')
            self.assertEqual([call.args[0] for call in ws.send.call_args_list][1:],
                             [b'audio', b'audio', 'flush', 'finish'])


class V1DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(runtime_dir=Path(self.temp.name))
        self.settings.token_path.write_text('a' * 40)
        self.desktop = Desktop(self.settings)
        self.desktop.started = time.monotonic()
        self.desktop.state.update(mode='r2t2', segments_done=0, segments_pasted=0)
        self.desktop.session_id = 'local-session'
        self.desktop.model_error = 'prior preload failed'
        self.events = asyncio.Queue()
        self.network_done = asyncio.Event()
        self.stream = StreamInput()
        self.ready = asyncio.Event()
        self.outputs = []
        self.callbacks = None
        self.consumer = None

        def factory(*args, **callbacks):
            self.callbacks = callbacks
            async def run(stream):
                callbacks['on_ready'](self.event('ready', 1, device='cuda:0'))
                try:
                    while True:
                        value = await self.events.get()
                        callbacks['on_event'](value)
                        if value['type'] == 'final':
                            return value
                        if value['type'] == 'error':
                            callbacks['on_abort']()
                            raise CaptureError(value['message'])
                finally:
                    self.network_done.set()
            client = AsyncMock()
            client.run.side_effect = run
            return client

        def delivered(text, target, only_copy, **options):
            self.outputs.append((text, only_copy, options.get('prefix')))
            return 'copied' if only_copy else 'pasted'

        self.patches = []
        for name, options in {
                'StreamClient': {'side_effect': factory},
                'deliver': {'side_effect': delivered},
                'copy_text': {}, 'notify': {'new_callable': AsyncMock},
        }.items():
            patcher = patch('oneaxe_voice.desktop.' + name, **options)
            mocked = patcher.start()
            self.patches.append(patcher)
            if name == 'deliver':
                self.deliver = mocked
            if name == 'copy_text':
                self.copy = mocked

    async def asyncTearDown(self):
        if self.consumer:
            self.consumer.cancel()
            await asyncio.gather(self.consumer, return_exceptions=True)
        for patcher in self.patches:
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def event(kind, seq, **fields):
        return {'type': kind, 'seq': seq, 'server_instance_id': 'boot',
                'model_generation': 'load', 'session_id': 'server-session',
                'text': '', 'pending': '', 'audio_processed_samples': 0, **fields}

    async def eventually(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.005)
        self.fail('condition did not become true')

    async def start(self):
        self.consumer = asyncio.create_task(self.desktop._consume_stream_v1(self.stream, None, self.ready))
        await asyncio.wait_for(self.ready.wait(), 1)

    async def test_final_tail_keeps_english_space_and_complete_saved_text(self):
        await self.start()
        await self.events.put(self.event('partial', 2, text='Hello', pending=' wor'))
        await self.eventually(lambda: self.desktop.committed_text == 'Hello')
        await self.events.put(self.event('final', 3, text='Hello world', complete=True))
        await asyncio.wait_for(self.consumer, 1)
        self.assertEqual(self.outputs, [('Hello', False, ''), (' world', False, ' ')])
        self.assertEqual(self.desktop.committed_text, 'Hello world')
        self.assertEqual(self.desktop.pending_text, '')
        self.assertEqual((self.settings.runtime_dir / 'last-transcript.txt').read_text(), 'Hello world')
        self.assertEqual(self.desktop.records[-1]['reason'], 'stop')
        self.assertIsNone(self.desktop.model_error)
        self.assertEqual(self.desktop.state['device'], 'cuda:0')
        self.assertEqual(self.desktop.state['stream_session_id'], 'server-session')

    async def test_menu_does_not_block_receiving_or_final_tail(self):
        self.desktop.menu_until = time.monotonic() + 20
        await self.start()
        for seq, text in enumerate(('A', 'AB', 'ABC'), 2):
            await self.events.put(self.event('partial', seq, text=text, pending='候选'))
        await self.events.put(self.event('final', 5, text='ABCD', complete=True))
        await asyncio.wait_for(self.network_done.wait(), 1)
        self.assertFalse(self.outputs)
        self.assertEqual(self.desktop.full_text, 'ABCD')
        self.assertEqual((await self.desktop.dispatch('ui'))['queued_text'], 'ABCD')
        self.desktop.menu_until = 0
        await asyncio.wait_for(self.consumer, 1)
        self.assertEqual(''.join(value[0] for value in self.outputs), 'ABCD')
        self.assertEqual(self.desktop.delivery_state, 'pasted')

    async def test_slow_paste_does_not_block_network_and_snapshots_coalesce(self):
        entered, released = threading.Event(), threading.Event()
        def slow(text, *args, **kwargs):
            self.outputs.append((text, False, kwargs.get('prefix')))
            entered.set()
            if not released.wait(2):
                raise TimeoutError('blocked test delivery')
            return 'pasted'
        self.deliver.side_effect = slow
        await self.start()
        await self.events.put(self.event('partial', 2, text='甲'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            for seq, text in enumerate(('甲乙', '甲乙丙', '甲乙丙丁'), 3):
                await self.events.put(self.event('partial', seq, text=text))
            await self.events.put(self.event('final', 6, text='甲乙丙丁戊', complete=True))
            await asyncio.wait_for(self.network_done.wait(), 1)
            self.assertEqual(self.desktop.full_text, '甲乙丙丁戊')
            self.assertFalse(self.consumer.done())
            released.set()
            await asyncio.wait_for(self.consumer, 1)
            self.assertEqual([value[0] for value in self.outputs], ['甲', '乙丙丁戊'])
        finally:
            released.set()

    async def test_cancel_drops_queued_and_late_text_before_menu_resumes(self):
        self.desktop.menu_until = time.monotonic() + 20
        await self.start()
        await self.events.put(self.event('partial', 2, text='保留文字'))
        await self.eventually(lambda: self.desktop.full_text == '保留文字')
        self.desktop.stream_delivery_open = False
        self.consumer.cancel()
        await asyncio.gather(self.consumer, return_exceptions=True)
        self.callbacks['on_event'](self.event('final', 3, text='保留文字迟到尾部'))
        self.desktop.menu_until = 0
        self.assertFalse(self.outputs)
        self.assertEqual(self.desktop.full_text, '保留文字')

    async def test_focus_change_keeps_full_clipboard_and_sticky_copy_only(self):
        self.deliver.side_effect = ['focus_changed', 'copied']
        await self.start()
        await self.events.put(self.event('partial', 2, text='第一句。'))
        await self.eventually(lambda: self.desktop.only_copy)
        await self.events.put(self.event('final', 3, text='第一句。第二句。'))
        await asyncio.wait_for(self.consumer, 1)
        self.assertFalse(self.deliver.call_args_list[0].args[2])
        self.assertTrue(self.deliver.call_args_list[1].args[2])
        self.assertEqual(self.deliver.call_args_list[1].args[0], '第一句。第二句。')
        self.copy.assert_called_once_with('第一句。')
        self.assertTrue(self.desktop.state['paste_paused'])

    async def test_delivery_failure_after_final_does_not_hang_queue_drain(self):
        self.deliver.side_effect = OSError('clipboard unavailable')
        await self.start()
        await self.events.put(self.event('final', 2, text='保留的最终文字'))
        with self.assertRaisesRegex(OSError, 'clipboard unavailable'):
            await asyncio.wait_for(self.consumer, 1)
        self.assertEqual((self.settings.runtime_dir / 'last-transcript.txt').read_text(), '保留的最终文字')
        self.assertFalse(self.desktop.stream_delivery_open)

    async def test_cancel_closes_network_before_dispatched_paste_finishes(self):
        entered, released = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            if not released.wait(2):
                raise TimeoutError('blocked test paste')
            return 'pasted'
        self.deliver.side_effect = slow
        await self.start()
        await self.events.put(self.event('partial', 2, text='已经派发'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.desktop.stream_delivery_open = False
            self.consumer.cancel()
            await asyncio.wait_for(self.network_done.wait(), .5)
            self.assertFalse(self.consumer.done())
            released.set()
            await asyncio.gather(self.consumer, return_exceptions=True)
            self.assertEqual(self.deliver.call_count, 1)
        finally:
            released.set()

    async def test_new_session_waits_for_model_management_before_ready(self):
        release = asyncio.Event()
        async def management():
            await release.wait()
        self.desktop.unload_task = asyncio.create_task(management())
        self.consumer = asyncio.create_task(self.desktop._consume_stream_v1(self.stream, None, self.ready))
        await asyncio.sleep(.02)
        self.assertFalse(self.ready.is_set())
        self.assertIsNone(self.callbacks)
        release.set()
        await asyncio.wait_for(self.ready.wait(), 1)
        await self.events.put(self.event('final', 2, text=''))
        await asyncio.wait_for(self.consumer, 1)

    async def test_capture_waits_for_ready_and_stops_instead_of_dropping_backlog(self):
        closed = asyncio.Event()
        started = asyncio.Event()
        async def capture(*args):
            started.set()
            try:
                for _ in range(13):
                    yield b'\0' * 5120
            finally:
                closed.set()
        class Endpoint:
            active = True
            def feed(self, chunk):
                return [chunk]
            def finish(self):
                return []
        config = {'stream_pause_ms': 1000, 'vad_mode': 2,
                  'vad_min_dbfs': -60, 'max_session_seconds': 900}
        self.desktop.stream_delivery_open = True
        with patch('oneaxe_voice.desktop.pcm_chunks', side_effect=capture), patch(
                'oneaxe_voice.desktop.StreamEndpoint', return_value=Endpoint()):
            producer = asyncio.create_task(self.desktop._produce_stream_v1('fake', config, self.stream, self.ready))
            await asyncio.sleep(.02)
            self.assertFalse(started.is_set())
            self.ready.set()
            with self.assertRaisesRegex(CaptureError, '积压'):
                await producer
        self.assertTrue(closed.is_set())
        self.assertEqual(self.stream.buffered_samples, 30720)
        self.assertEqual(self.desktop.state['stopped_reason'], 'backlog')
        self.assertFalse(self.desktop.stream_delivery_open)


if __name__ == '__main__':
    unittest.main()
