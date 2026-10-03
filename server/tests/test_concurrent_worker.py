"""Multiplexed RPC ordering, cancellation isolation and worker configuration."""

import asyncio
import base64
import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oneaxe_voice.concurrent_worker import ConcurrentSessions, engine_configuration, register_qwen_backend, serve, warm_model
from oneaxe_voice.r2t2_async import FatalEngineError
from oneaxe_voice.worker_client import WorkerClient, WorkerRPCError
from tests.test_r2t2_async import FakeEngine, adapter, speech
from tests.test_qwen_async import qwen_adapter


def request(op, session="pc", rpc="rpc", **values):
    return {"rpc_id": rpc, "op": op, "session": session, **values}


class ConcurrentWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_sessions_run_and_cancel_affects_only_one(self):
        gates = {name: asyncio.Event() for name in ("pc", "phone")}
        engine = FakeEngine(gates=gates)
        sessions = ConcurrentSessions(adapter(engine))
        for sid in gates:
            self.assertTrue((await sessions.dispatch(request("start", sid)))["ok"])
        pcm = base64.b64encode(speech()).decode()
        pc = asyncio.create_task(sessions.dispatch(request("audio", "pc", "pc-audio", pcm=pcm)))
        phone = asyncio.create_task(sessions.dispatch(request("audio", "phone", "phone-audio", pcm=pcm)))
        async with asyncio.timeout(2):
            while len(engine.calls) != 2:
                await asyncio.sleep(0)
        cancelled = await sessions.dispatch(request("cancel", "phone", "cancel"))
        self.assertTrue(cancelled["ok"])
        self.assertEqual((await phone)["code"], "CANCELLED")
        gates["pc"].set()
        result = await pc
        self.assertTrue(result["ok"])
        self.assertEqual(result["rpc_id"], "pc-audio")
        self.assertEqual(result["audio_processed_samples"], 5120)
        self.assertGreater(len(result["text"]), 0)
        self.assertEqual(len(engine.aborted), 1)
        self.assertEqual(engine.shutdown_count, 0)
        self.assertEqual(set(sessions.sessions), {"pc"})
        more = await sessions.dispatch(request("audio", pcm=base64.b64encode(speech(2560)).decode()))
        self.assertTrue(more["text"].startswith(result["text"]))

    async def test_same_session_flush_waits_for_audio(self):
        gate = asyncio.Event()
        engine = FakeEngine(["first", "tail"], {"pc": gate})
        sessions = ConcurrentSessions(adapter(engine))
        await sessions.dispatch(request("start"))
        audio = asyncio.create_task(sessions.dispatch(request("audio", pcm=base64.b64encode(speech()).decode())))
        await engine.entered.wait()
        flushing = asyncio.create_task(sessions.dispatch(request("flush", rpc="flush")))
        await asyncio.sleep(0)
        self.assertEqual(len(engine.calls), 1)
        self.assertFalse(flushing.done())
        gate.set()
        first, final = await asyncio.gather(audio, flushing)
        self.assertTrue(final["text"].startswith(first["text"]))
        self.assertEqual([call[1].max_tokens for call in engine.calls], [4, 64])
        self.assertEqual(sessions.sessions["pc"].decoder.utterance, 1)

    async def test_bad_request_does_not_destroy_an_existing_session(self):
        sessions = ConcurrentSessions(adapter())
        await sessions.dispatch(request("start"))
        for value in (request("start"), request("bad-op"), request("audio", session=[])):
            self.assertFalse((await sessions.dispatch(value))["ok"])
            self.assertEqual(set(sessions.sessions), {"pc"})
        bad = await sessions.dispatch(request("audio", pcm="invalid base64!"))
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["fatal"])
        self.assertEqual(sessions.sessions, {})
        self.assertTrue((await sessions.dispatch(request("start", "other")))["ok"])

    async def test_capacity_finish_and_end(self):
        sessions = ConcurrentSessions(adapter())
        await sessions.dispatch(request("start", "pc"))
        await sessions.dispatch(request("start", "phone"))
        self.assertEqual((await sessions.dispatch(request("start", "third")))["code"], "CAPACITY_EXCEEDED")
        finish = await sessions.dispatch(request("finish", "phone"))
        self.assertTrue(finish["ok"])
        self.assertIn("phone", sessions.sessions)
        self.assertTrue((await sessions.dispatch(request("finish", "phone")))["ok"])
        self.assertEqual((await sessions.dispatch(request("audio", "phone", pcm="AA==")))["code"], "CANCELLED")
        for _ in range(2):
            self.assertTrue((await sessions.dispatch(request("end", "phone")))["ok"])
        self.assertIn("pc", sessions.sessions)

    async def test_shared_engine_fault_is_marked_fatal(self):
        sessions = ConcurrentSessions(adapter())
        await sessions.dispatch(request("start"))
        async def failed(*args):
            raise FatalEngineError("engine died")
        sessions.model.generate = failed
        result = await sessions.dispatch(request("audio", pcm=base64.b64encode(speech()).decode()))
        self.assertTrue(result["fatal"])
        self.assertEqual(result["code"], "SERVICE_UNAVAILABLE")

    async def test_reusing_session_id_cannot_reuse_engine_request_id(self):
        engine = FakeEngine()
        sessions = ConcurrentSessions(adapter(engine))
        pcm = base64.b64encode(speech()).decode()
        for _ in range(2):
            await sessions.dispatch(request("start"))
            await sessions.dispatch(request("audio", pcm=pcm))
            await sessions.dispatch(request("cancel"))
        self.assertNotEqual(engine.calls[0][2], engine.calls[1][2])

    async def test_warmup_covers_single_pair_and_long_pair(self):
        engine = FakeEngine()
        await warm_model(adapter(engine))
        self.assertEqual([len(call[0]["multi_modal_data"]["audio"][0]) for call in engine.calls],
                         [5120, 5120, 5120, 256000, 256000])
        self.assertEqual([call[1].max_tokens for call in engine.calls], [4, 4, 4, 2, 2])

    async def test_socket_dispatch_reads_cancel_while_audio_is_inflight(self):
        gate = asyncio.Event()
        model = adapter(FakeEngine(gates={"pc": gate}))
        reader = asyncio.StreamReader()

        class Writer:
            def __init__(self):
                self.results = []

            def write(self, data):
                self.results.append(json.loads(data))

            async def drain(self):
                await asyncio.sleep(0)

        writer = Writer()
        task = asyncio.create_task(serve(reader, writer, ConcurrentSessions(model)))
        def send(value):
            reader.feed_data((json.dumps(value) + "\n").encode())
        send(request("start", rpc="start"))
        async with asyncio.timeout(2):
            while not writer.results:
                await asyncio.sleep(0)
        send(request("audio", rpc="audio", pcm=base64.b64encode(speech()).decode()))
        await model.engine.entered.wait()
        send(request("cancel", rpc="cancel"))
        async with asyncio.timeout(2):
            while not any(value.get("rpc_id") == "cancel" for value in writer.results):
                await asyncio.sleep(0)
        reader.feed_eof()
        await task
        results = {value["rpc_id"]: value for value in writer.results}
        self.assertTrue(results["cancel"]["ok"])
        self.assertEqual(results["audio"]["code"], "CANCELLED")

    async def test_worker_client_contract_over_real_socket(self):
        parent, child = socket.socketpair()
        child.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=child, limit=200000)
        engine = FakeEngine(gates={"phone": asyncio.Event()})
        serving = asyncio.create_task(serve(reader, writer, ConcurrentSessions(adapter(engine))))
        client = WorkerClient(parent, timeout=2)
        writer.write(b'{"ready":true,"mode":"r2t2","capacity":2}\n')
        await writer.drain()
        try:
            self.assertEqual((await asyncio.to_thread(client.wait_ready))["capacity"], 2)
            for sid in ("pc", "phone"):
                self.assertTrue((await asyncio.to_thread(client.call, "start", session=sid))["ready"])
            pcm = base64.b64encode(speech()).decode()
            pc = asyncio.create_task(asyncio.to_thread(client.call, "audio", session="pc", pcm=pcm))
            phone = asyncio.create_task(asyncio.to_thread(client.call, "audio", session="phone", pcm=pcm))
            async with asyncio.timeout(2):
                while len(engine.calls) != 2:
                    await asyncio.sleep(0)
            self.assertTrue((await asyncio.to_thread(client.call, "cancel", session="phone"))["cancelled"])
            with self.assertRaises(WorkerRPCError) as cancelled:
                await phone
            self.assertEqual(cancelled.exception.code, "CANCELLED")
            self.assertTrue((await pc)["ok"])
            self.assertIsNone(client.failure)
            self.assertTrue((await asyncio.to_thread(client.call, "finish", session="pc"))["ok"])
            self.assertEqual(engine.shutdown_count, 0)
        finally:
            await asyncio.to_thread(client.close)
            await serving
            writer.close()
            await writer.wait_closed()

    async def test_qwen_worker_client_keeps_pc_running_after_remote_cancel_or_end(self):
        for op in ("cancel", "end"):
            with self.subTest(op=op):
                parent, child = socket.socketpair()
                child.setblocking(False)
                reader, writer = await asyncio.open_connection(sock=child, limit=200000)
                gates = {sid: asyncio.Event() for sid in ("pc", "phone")}
                engine = FakeEngine(gates=gates)
                sessions = ConcurrentSessions(qwen_adapter(engine), mode="qwen-stream")
                serving = asyncio.create_task(serve(reader, writer, sessions))
                client = WorkerClient(parent, timeout=2)
                writer.write(b'{"ready":true,"mode":"qwen-stream","capacity":2}\n')
                await writer.drain()
                try:
                    self.assertEqual((await asyncio.to_thread(client.wait_ready))["mode"], "qwen-stream")
                    for sid in gates:
                        await asyncio.to_thread(client.call, "start", session=sid)
                    with self.assertRaises(WorkerRPCError) as capacity:
                        await asyncio.to_thread(client.call, "start", session="third")
                    self.assertEqual(capacity.exception.code, "CAPACITY_EXCEEDED")
                    pcm = base64.b64encode(speech(32000)).decode()
                    pc = asyncio.create_task(asyncio.to_thread(client.call, "audio", session="pc", pcm=pcm))
                    phone = asyncio.create_task(asyncio.to_thread(client.call, "audio", session="phone", pcm=pcm))
                    async with asyncio.timeout(2):
                        while len(engine.calls) != 2:
                            await asyncio.sleep(0)
                    ending = await asyncio.to_thread(client.call, op, session="phone")
                    self.assertTrue(ending["cancelled" if op == "cancel" else "ended"])
                    with self.assertRaises(WorkerRPCError) as cancelled:
                        await phone
                    self.assertEqual(cancelled.exception.code, "CANCELLED")
                    self.assertFalse(pc.done())
                    gates["pc"].set()
                    first = await pc
                    self.assertEqual(first["audio_processed_samples"], 32000)
                    self.assertEqual(first["session"], "pc")
                    await asyncio.to_thread(client.call, "audio", session="pc",
                                            pcm=base64.b64encode(speech(19)).decode())
                    final = await asyncio.to_thread(client.call, "flush", session="pc")
                    self.assertEqual(final["audio_processed_samples"], 32019)
                    self.assertEqual(final["pending"], "")
                    silence = await asyncio.to_thread(client.call, "audio", session="pc",
                                                      pcm=base64.b64encode(bytes(64000)).decode())
                    self.assertEqual(silence["audio_processed_samples"], 64019)
                    finished = await asyncio.to_thread(client.call, "finish", session="pc")
                    self.assertEqual(finished["text"], final["text"])
                    self.assertEqual(finished["audio_processed_samples"], 64019)
                    self.assertIsNone(client.failure)
                    self.assertEqual(len(engine.aborted), 1)
                    self.assertEqual(engine.aborted[0].split(":")[1], "phone")
                    self.assertEqual(engine.shutdown_count, 0)
                    self.assertEqual(set(sessions.sessions), {"pc"})
                finally:
                    await asyncio.to_thread(client.close)
                    await serving
                    writer.close()
                    await writer.wait_closed()

    def test_capacity_configuration_is_tunable(self):
        with patch.dict("os.environ", {}, clear=True):
            config = engine_configuration("model")
            self.assertEqual(config["max_num_seqs"], 2)
            self.assertEqual(config["kv_cache_memory_bytes"], 1024**3)
        with patch.dict("os.environ", {"ONEAXE_VOICE_STREAM_MAX_NUM_SEQS": "4",
                                      "ONEAXE_VOICE_STREAM_KV_CACHE_BYTES": str(512 * 1024**2)}, clear=True):
            self.assertEqual(engine_configuration("model")["max_num_seqs"], 4)
        with patch.dict("os.environ", {"ONEAXE_VOICE_STREAM_MAX_NUM_SEQS": "1"}, clear=True):
            with self.assertRaises(ValueError):
                engine_configuration("model")

    def test_registration_is_explicit_repeatable_and_lazy(self):
        from unittest.mock import MagicMock
        registry = SimpleNamespace(register_model=MagicMock(),
                                   get_supported_archs=lambda: ["Qwen3ASRForConditionalGeneration"])
        backend = type("Qwen3ASRForConditionalGeneration", (), {})
        transforms = SimpleNamespace(Qwen3ASRConfig=object(), Qwen3ASRForConditionalGeneration=object(),
                                     Qwen3ASRProcessor=object())
        auto = SimpleNamespace(AutoConfig=MagicMock(), AutoModel=MagicMock(), AutoProcessor=MagicMock())
        modules = {"qwen_asr.core.transformers_backend": transforms,
                   "qwen_asr.core.vllm_backend": SimpleNamespace(Qwen3ASRForConditionalGeneration=backend),
                   "transformers": auto, "vllm": SimpleNamespace(ModelRegistry=registry)}
        with patch.dict("sys.modules", modules):
            register_qwen_backend()
            register_qwen_backend()
        self.assertEqual(registry.register_model.call_count, 2)
        self.assertIsInstance(registry.register_model.call_args.args[1], str)
        self.assertTrue(auto.AutoConfig.register.call_args.kwargs["exist_ok"])


if __name__ == "__main__":
    unittest.main()
