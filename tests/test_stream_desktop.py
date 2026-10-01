"""Streaming candidates, endpoints and successful delivery are separate states."""

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from oneaxe_voice.config import Settings
from oneaxe_voice.desktop import Desktop


class StreamDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_forces_batched_tail_and_finish_does_not_repeat_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(runtime_dir=Path(directory))
            settings.token_path.write_text('a' * 40)
            desktop = Desktop(settings)
            desktop.model_error = 'prior preload failed'
            desktop.started = time.monotonic()
            desktop.state.update(mode='r2t2', segments_done=0, segments_pasted=0)
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
                await desktop._consume_stream(queue, None, 'r2t2')
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


if __name__ == '__main__':
    unittest.main()
