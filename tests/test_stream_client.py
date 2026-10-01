"""CPU regressions for full duplex V1 credit, final text and cancellation."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from oneaxe_voice.capture import CaptureError
from oneaxe_voice.stream_client import StreamClient, StreamInput


def event(kind, seq, **fields):
    return {"type": kind, "server_instance_id": "boot", "model_generation": "load",
            "session_id": "session", "seq": seq, **fields}


class Socket:
    def __init__(self, *, finish=True, limit=32000):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.samples = 0
        self.finish = finish
        self.next_seq = 2
        self.push(event("ready", 1, audio_received_samples=0,
                        audio_processed_samples=0, audio_send_limit=limit))

    def push(self, value):
        self.incoming.put_nowait(json.dumps(value))
        self.next_seq = max(self.next_seq, value.get("seq", 0) + 1)

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, bytes):
            self.samples += len(value) // 2
        elif json.loads(value)["type"] == "finish" and self.finish:
            self.push(event("final", self.next_seq, text="完成。", pending="",
                            audio_processed_samples=self.samples, reason="finished", complete=True))

    async def recv(self):
        return await self.incoming.get()

    @property
    def controls(self):
        return [json.loads(value) for value in self.sent if isinstance(value, str)]


class StreamInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_credit_blocked_frame_stays_in_buffer_budget(self):
        stream = StreamInput()
        for _ in range(12):
            stream.put_nowait(b"\0" * 5120)
        await stream.get()
        self.assertEqual(stream.buffered_samples, 30720)
        with self.assertRaises(asyncio.QueueFull):
            stream.put_nowait(b"\0" * 5120)
        stream.sent(2560)
        stream.put_nowait(b"\0" * 5120)
        self.assertEqual(stream.buffered_samples, 30720)


class StreamClientTests(unittest.IsolatedAsyncioTestCase):
    async def eventually(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.005)
        self.fail("condition did not become true")

    def connection(self, ws):
        connection = AsyncMock()
        connection.__aenter__.return_value = ws
        return patch("websockets.asyncio.client.connect", return_value=connection)

    async def test_audio_does_not_wait_for_a_recognition_reply(self):
        ws, stream, snapshots = Socket(), StreamInput(), []
        for _ in range(5):
            stream.put_nowait(b"\0" * 5120)
        stream.put_nowait("flush")
        stream.put_nowait(None)
        client = StreamClient("http://127.0.0.1:8097", "private", on_event=snapshots.append)
        with self.connection(ws) as connect:
            final = await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual(len([item for item in ws.sent if isinstance(item, bytes)]), 5)
        self.assertEqual(stream.buffered_samples, 0)
        self.assertEqual([value["type"] for value in ws.controls], ["start", "flush", "finish"])
        self.assertEqual(ws.controls[1]["after_audio_samples"], 12800)
        self.assertEqual(ws.controls[2]["after_audio_samples"], 12800)
        self.assertEqual(ws.controls[0]["protocol_version"], 1)
        self.assertEqual(ws.controls[0]["mode"], "r2t2")
        self.assertNotIn("expected_model_generation", ws.controls[0])
        self.assertEqual(connect.call_args.args[0], "ws://127.0.0.1:8097/api/dictation/v1/stream")
        self.assertEqual(connect.call_args.kwargs["additional_headers"], {"Authorization": "Bearer private"})
        self.assertEqual(final["text"], "完成。")
        self.assertEqual(snapshots[-1]["type"], "final")

    async def test_repeated_flow_does_not_add_credit_and_new_flow_unblocks_audio(self):
        ws, stream = Socket(), asyncio.Queue()
        for _ in range(13):
            stream.put_nowait(b"\0" * 5120)
        stream.put_nowait(None)
        client = StreamClient("http://localhost", "private")
        with self.connection(ws):
            task = asyncio.create_task(client.run(stream))
            try:
                await self.eventually(lambda: ws.samples == 30720)
                ws.push(event("flow", 2, audio_received_samples=30720,
                              audio_processed_samples=0, audio_send_limit=32000))
                ws.push(event("flow", 2, audio_received_samples=30720,
                              audio_processed_samples=0, audio_send_limit=32000))
                await asyncio.sleep(.02)
                self.assertEqual(ws.samples, 30720)
                ws.push(event("flow", 3, audio_received_samples=30720,
                              audio_processed_samples=2560, audio_send_limit=34560))
                await asyncio.wait_for(task, 1)
                self.assertEqual(ws.samples, 33280)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_keepalive_and_cancel_work_while_sender_has_no_credit(self):
        ws, stream, aborted = Socket(limit=0), StreamInput(), []
        stream.put_nowait(b"\0" * 5120)
        client = StreamClient("https://localhost", "private", keepalive_seconds=.01,
                              on_abort=lambda: aborted.append(True))
        with self.connection(ws) as connect:
            task = asyncio.create_task(client.run(stream))
            await self.eventually(lambda: any(value["type"] == "keepalive" for value in ws.controls))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        self.assertEqual(ws.samples, 0)
        self.assertEqual(ws.controls[-1], {"type": "cancel"})
        self.assertEqual(aborted, [True])
        self.assertTrue(connect.call_args.args[0].startswith("wss://"))

    async def test_late_duplicate_is_ignored_and_final_tail_is_received(self):
        ws, stream, snapshots = Socket(finish=False), StreamInput(), []
        stream.put_nowait(None)
        ws.push(event("partial", 2, text="Hello", pending=" wor"))
        ws.push(event("partial", 2, text="changed", pending=""))
        ws.push(event("final", 3, text="Hello world", pending="", complete=True, reason="finished"))
        client = StreamClient("http://localhost", "private", on_event=snapshots.append)
        with self.connection(ws):
            await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual([value["seq"] for value in snapshots], [2, 3])
        self.assertEqual(client.text, "Hello world")

    async def test_fixed_prefix_change_stops_client_and_cancels_this_session(self):
        ws, stream = Socket(finish=False), StreamInput()
        ws.push(event("partial", 2, text="原文", pending=""))
        ws.push(event("partial", 3, text="修改", pending=""))
        client = StreamClient("http://localhost", "private")
        with self.connection(ws), self.assertRaisesRegex(CaptureError, "修改"):
            await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual(client.text, "原文")
        self.assertEqual(ws.controls[-1]["type"], "cancel")

    async def test_changed_model_identity_stops_before_delivering_new_text(self):
        ws, stream, snapshots = Socket(finish=False), StreamInput(), []
        ws.push(event("partial", 2, text="旧文", pending=""))
        ws.push(event("partial", 3, text="新文", pending="", model_generation="different"))
        client = StreamClient("http://localhost", "private", on_event=snapshots.append)
        with self.connection(ws), self.assertRaisesRegex(CaptureError, "标识"):
            await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual([value["text"] for value in snapshots], ["旧文"])

    async def test_error_retains_fixed_text_and_does_not_report_success(self):
        ws, stream, snapshots = Socket(finish=False), StreamInput(), []
        ws.push(event("partial", 2, text="保留", pending="候选"))
        ws.push(event("error", 3, text="保留", pending="", code="MODEL_CHANGED", message="模型变化"))
        client = StreamClient("http://localhost", "private", on_event=snapshots.append)
        with self.connection(ws), self.assertRaisesRegex(CaptureError, "模型变化"):
            await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual(client.text, "保留")
        self.assertEqual(snapshots[-1]["type"], "error")

    async def test_finish_without_final_fails_with_bounded_wait(self):
        ws, stream = Socket(finish=False), StreamInput()
        stream.put_nowait(None)
        client = StreamClient("http://localhost", "private", finish_timeout=.02)
        with self.connection(ws), self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual(ws.controls[-1]["type"], "cancel")

    async def test_malformed_ready_and_odd_pcm_fail_explicitly(self):
        for malformed in (True, False):
            with self.subTest(malformed=malformed):
                ws, stream = Socket(), StreamInput()
                if malformed:
                    ws.incoming.get_nowait()
                    ws.push({"type": "ready", "seq": 1})
                else:
                    stream.put_nowait(b"odd")
                client = StreamClient("http://localhost", "private")
                with self.connection(ws), self.assertRaises(CaptureError):
                    await asyncio.wait_for(client.run(stream), 1)

    async def test_start_failure_is_reported_without_sending_audio(self):
        ws, stream = Socket(), StreamInput()
        ws.incoming.get_nowait()
        ws.push({'type': 'error', 'seq': 0, 'session_id': None,
                 'code': 'CAPACITY_EXCEEDED', 'message': '暂无PC名额'})
        stream.put_nowait(b'\0' * 5120)
        client = StreamClient('http://localhost', 'private')
        with self.connection(ws), self.assertRaisesRegex(CaptureError, '暂无PC名额'):
            await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual(ws.samples, 0)

    async def test_invalid_progress_stops_before_using_unearned_credit(self):
        ws, stream = Socket(), StreamInput()
        ws.push(event('flow', 2, audio_received_samples=0,
                      audio_processed_samples=1000, audio_send_limit=33000))
        client = StreamClient('http://localhost', 'private')
        with self.connection(ws), self.assertRaisesRegex(CaptureError, '进度'):
            await asyncio.wait_for(client.run(stream), 1)
        self.assertEqual(ws.controls[-1]['type'], 'cancel')


if __name__ == "__main__":
    unittest.main()
