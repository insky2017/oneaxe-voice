"""Streaming boundaries, engine leases, and private WebSocket transport."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from oneaxe_voice.backend import BusyError, GPUError
from oneaxe_voice.config import Settings
from oneaxe_voice.engines import EngineRouter
from oneaxe_voice.server import create_app
from oneaxe_voice.stream_worker import StreamDecoder


class DecoderTests(unittest.TestCase):
    def model(self):
        model = MagicMock()
        model.init_streaming_state.side_effect = lambda **kw: SimpleNamespace(
            **kw, chunk_size_samples=int(kw['chunk_size_sec'] * 16000),
            chunk_id=0, audio_accum=np.zeros(0, dtype=np.float32),
            buffer=np.zeros(0, dtype=np.float32), text='', _raw_decoded='',
        )
        model.processor.tokenizer.encode.side_effect = lambda value: list(value)
        model.processor.tokenizer.decode.side_effect = lambda value: ''.join(value)
        return model

    def test_r2t2_preserves_official_initial_lookahead_and_partial_pcm(self):
        model = self.model()
        model.streaming_transcribe_no_reset.side_effect = [('今天说', '今天'), ('今天说话', '今天说')]
        decoder = StreamDecoder(model, 'r2t2')
        decoder.feed(b'\1' * 100)
        model.streaming_transcribe_no_reset.assert_not_called()
        self.assertEqual(decoder.feed(b'\1' * (10240 - 100))['text'], '今天')
        self.assertEqual(len(model.streaming_transcribe_no_reset.call_args.args[0]), 5120)
        self.assertEqual(decoder.feed(b'\1' * 5120)['text'], '今天说')
        self.assertEqual(len(model.streaming_transcribe_no_reset.call_args.args[0]), 2560)

    def test_exact_chunk_stop_flushes_withheld_r2t2_token(self):
        model = self.model()
        model.streaming_transcribe_no_reset.return_value = ('中文尾音', '中文')
        def finish(state, **kwargs):
            self.assertGreater(len(state.buffer), 0)
            return '中文尾音'
        model.finish_streaming_transcribe_no_reset.side_effect = finish
        decoder = StreamDecoder(model, 'r2t2')
        decoder.feed(b'\1' * 10240)
        self.assertEqual(decoder.finish()['text'], '中文尾音')

    def test_revised_committed_prefix_is_not_emitted(self):
        model = self.model()
        model.streaming_transcribe_no_reset.side_effect = [('甲乙', '甲'), ('丙乙', '丙')]
        decoder = StreamDecoder(model, 'r2t2')
        decoder.feed(b'\1' * 10240)
        with self.assertRaisesRegex(ValueError, '已提交前缀'):
            decoder.feed(b'\1' * 5120)

    def test_qwen_context_is_bounded_and_keeps_session_prefix(self):
        model = self.model()
        counter = 0
        def transcribe(audio, state):
            nonlocal counter
            counter += 1
            state.audio_accum = np.concatenate([state.audio_accum, audio])
            state.chunk_id += 1
            state._raw_decoded += f'第{counter:03d}段内容。'
            state.text = state._raw_decoded
        model.streaming_transcribe.side_effect = transcribe
        def finish(state):
            state.text = state._raw_decoded
        model.finish_streaming_transcribe.side_effect = finish
        decoder = StreamDecoder(model, 'qwen-stream')
        previous = ''
        for _ in range(40):
            result = decoder.feed(b'\1' * 64000)
            self.assertTrue(result['text'].startswith(previous))
            self.assertLessEqual(len(decoder.state.audio_accum), 30 * 16000)
            previous = result['text']
        text = decoder.finish()['text']
        self.assertEqual(text, ''.join(f'第{i:03d}段内容。' for i in range(1, 41)))

    def test_retreating_commit_horizon_keeps_agreed_prefix(self):
        model = self.model()
        def transcribe(audio, state):
            state.chunk_id += 1
            state.text = state._raw_decoded = 'ABCDEFGHIJK'
        model.streaming_transcribe.side_effect = transcribe
        decoder = StreamDecoder(model, 'qwen-stream')
        decoder._fixed_qwen = MagicMock(side_effect=['ABCDE', 'ABC', 'ABCDEFG'])
        values = [decoder.feed(b'\1' * 64000)['text'] for _ in range(3)]
        self.assertEqual(values, ['ABCDE', 'ABCDE', 'ABCDEFG'])


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = EngineRouter(Settings())
        self.router.offline = MagicMock()
        self.router.offline.status.return_value = {'model_loaded': False}
        self.worker = MagicMock()
        self.worker.capacity = 2
        self.worker.engine_config = self.worker.warmup = {}
        self.worker.process.pid = 123
        self.patcher = patch('oneaxe_voice.engines.Worker', return_value=self.worker)
        self.factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_stream_lease_blocks_switch_transcription_and_unload(self):
        self.router.begin('r2t2', 'session')
        with self.assertRaises(BusyError):
            self.router.prepare('vad')
        with self.assertRaises(BusyError):
            self.router.transcribe(b'wav', 'request')
        self.assertFalse(self.router.unload_if_idle(force=True))
        self.router.end('session', abort=True)
        self.worker.close.assert_not_called()
        self.worker.call.assert_any_call('cancel', session='session')
        self.assertFalse(self.router.gate.locked())

    def test_engine_switch_releases_old_worker_before_loading_next(self):
        events = []
        self.worker.close.side_effect = lambda: events.append('close')
        self.factory.side_effect = lambda *args: events.append('load') or self.worker
        self.router.prepare('qwen-stream')
        self.router.prepare('r2t2')
        self.assertEqual(events, ['load', 'close', 'load'])
        self.router.prepare('vad')
        self.assertEqual(events[-1], 'close')
        self.assertIsNone(self.router.worker)

    def test_failed_begin_and_foreign_end_do_not_release_another_lease(self):
        self.factory.side_effect = GPUError('failed')
        with self.assertRaises(GPUError):
            self.router.begin('r2t2', 'failed')
        self.assertFalse(self.router.gate.locked())
        self.factory.side_effect = None
        self.router.begin('r2t2', 'active')
        self.router.end('other', abort=True)
        self.assertTrue(self.router.gate.locked())
        self.router.end('active')

    def test_broken_start_reaps_worker_and_allows_retry(self):
        self.worker.call.side_effect = GPUError('dead worker')
        with self.assertRaises(GPUError):
            self.router.begin('r2t2', 'failed')
        self.worker.close.assert_called_once()
        self.assertIsNone(self.router.worker)
        self.assertFalse(self.router.gate.locked())
        self.worker.call.side_effect = None
        self.router.begin('r2t2', 'retry')
        self.router.end('retry')

    def test_zero_idle_timeout_keeps_worker_until_forced_unload(self):
        self.router.settings = replace(self.router.settings, idle_seconds=0)
        self.router.prepare('r2t2')
        self.router.last_used = 0
        self.assertFalse(self.router.unload_if_idle())
        self.worker.close.assert_not_called()
        self.assertTrue(self.router.unload_if_idle(force=True))
        self.worker.close.assert_called_once()


class WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = Settings(runtime_dir=Path(self.temp.name))
        settings.token_path.write_text('a' * 40)
        self.engine = MagicMock()
        self.engine.feed.return_value = {'text': '测试', 'preview': '测试文本'}
        self.engine.finish.return_value = {'text': '测试文本', 'preview': '测试文本'}
        self.engine.prepare.return_value = {'model_loaded': True}
        app = create_app(settings, self.engine)
        self.client = TestClient(app, client=('127.0.0.1', 1234))
        self.headers = {'Authorization': 'Bearer ' + 'a' * 40}

    def test_websocket_requires_token_loopback_and_no_browser_origin(self):
        for headers in ({}, {'Authorization': 'Bearer wrong'}, {**self.headers, 'Origin': 'https://example.com'}):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect('/api/dictation/stream', headers=headers):
                    pass
        remote = TestClient(self.client.app, client=('10.0.0.1', 1234))
        with self.assertRaises(WebSocketDisconnect):
            with remote.websocket_connect('/api/dictation/stream', headers=self.headers):
                pass
        self.engine.begin.assert_not_called()

    def test_stream_order_and_final_tail_keep_model_warm(self):
        with self.client.websocket_connect('/api/dictation/stream', headers=self.headers) as ws:
            ws.send_json({'mode': 'r2t2'})
            self.assertEqual(ws.receive_json()['type'], 'ready')
            ws.send_bytes(b'\0' * 5120)
            partial = ws.receive_json()
            self.assertEqual((partial['sequence'], partial['text']), (1, '测试'))
            ws.send_text('finish')
            final = ws.receive_json()
            self.assertEqual((final['sequence'], final['text'], final['type']), (2, '测试文本', 'final'))
        self.assertFalse(self.engine.end.call_args.args[1])

    def test_pause_flush_keeps_lease_and_keepalive_does_not_infer(self):
        self.engine.flush.return_value = {'text': '测试文本', 'pending': ''}
        with self.client.websocket_connect('/api/dictation/stream', headers=self.headers) as ws:
            ws.send_json({'mode': 'r2t2'})
            ws.receive_json()
            ws.send_bytes(b'\0' * 5120)
            ws.receive_json()
            ws.send_text('flush')
            result = ws.receive_json()
            self.assertEqual((result['type'], result['sequence'], result['pending']), ('partial', 2, ''))
            self.engine.end.assert_not_called()
            ws.send_text('keepalive')
            self.assertEqual(ws.receive_json(), {'type': 'keepalive'})
            self.assertEqual(self.engine.feed.call_count, 1)
            self.assertEqual(self.engine.flush.call_count, 1)
            ws.send_bytes(b'\0' * 5120)
            self.assertEqual(ws.receive_json()['sequence'], 3)
            ws.send_text('finish')
            self.assertEqual(ws.receive_json()['type'], 'final')

    def test_invalid_pcm_and_disconnect_abort_session(self):
        for pcm in [b'x', b'\0' * 64002, b'']:
            with self.client.websocket_connect('/api/dictation/stream', headers=self.headers) as ws:
                ws.send_json({'mode': 'qwen-stream'});ws.receive_json()
                ws.send_bytes(pcm)
                self.assertEqual(ws.receive_json()['type'], 'error')
            self.assertTrue(self.engine.end.call_args.args[1])
        self.engine.feed.assert_not_called()
        with self.client.websocket_connect('/api/dictation/stream', headers=self.headers) as ws:
            ws.send_json({'mode': 'r2t2'});ws.receive_json()
        self.assertTrue(self.engine.end.call_args.args[1])

    def test_prepare_rejects_unknown_mode_and_busy_engine(self):
        self.assertEqual(self.client.post('/api/dictation/prepare', json={'mode': 'no'}, headers=self.headers).status_code, 422)
        self.engine.prepare.side_effect = BusyError('busy')
        self.assertEqual(self.client.post('/api/dictation/prepare', json={'mode': 'r2t2'}, headers=self.headers).status_code, 429)


if __name__ == '__main__':
    unittest.main()
