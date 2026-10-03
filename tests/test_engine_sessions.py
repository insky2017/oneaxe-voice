"""Model leases and PC/mobile isolation with CPU-only fake workers."""

import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from oneaxe_voice.backend import BusyError
from oneaxe_voice.config import Settings
from oneaxe_voice.engines import EngineRouter, SessionError
from oneaxe_voice.worker_client import WorkerRPCError


class FakeWorker:
    def __init__(self):
        self.process = SimpleNamespace(pid=123)
        self.sessions = {}
        self.calls = []
        self.closed = False
        self.block_session = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.failure = None
        self.block_start = False
        self.start_entered = threading.Event()
        self.start_release = threading.Event()

    def call(self, op, session=None, **values):
        self.calls.append((op, session))
        if self.failure is not None and op == "audio":
            raise self.failure
        if op == "start":
            self.sessions[session] = {"text": "", "pending": "", "audio_processed_samples": 0}
            if self.block_start:
                self.start_entered.set()
                if not self.start_release.wait(3):
                    raise RuntimeError("test start timed out")
            return {"ready": True}
        if op in ("cancel", "end"):
            return self.sessions.pop(session, {})
        if op == "audio":
            result = self.sessions[session]
            result["audio_processed_samples"] += len(base64.b64decode(values["pcm"])) // 2
            result["text"] += session
            if session == self.block_session:
                self.entered.set()
                if not self.release.wait(3):
                    raise RuntimeError("test timed out")
            return dict(result)
        return dict(self.sessions[session])

    def close(self):
        self.closed = True


class EngineSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = EngineRouter(Settings(runtime_dir=Path(self.temp.name)))
        self.router.offline = MagicMock()
        self.router.offline.status.return_value = {"model_loaded": False}
        self.workers = []

        def create(*args):
            worker = FakeWorker()
            self.workers.append(worker)
            return worker

        self.factory = patch("oneaxe_voice.engines.Worker", side_effect=create).start()
        self.addCleanup(patch.stopall)

    def mobile(self, session="mobile", credential="device"):
        status = self.router.status()
        return self.router.begin_mobile(session, status["server_instance_id"],
                                        status["model_generation"], credential)

    def assert_code(self, code, method, *args):
        with self.assertRaises(SessionError) as raised:
            method(*args)
        self.assertEqual(raised.exception.code, code)

    def test_mobile_never_loads_and_stale_bindings_fail(self):
        self.assert_code("MODEL_NOT_READY", self.mobile)
        self.factory.assert_not_called()
        self.router.prepare("r2t2")
        status = self.router.status()
        self.assert_code("MODEL_CHANGED", self.router.begin_mobile, "mobile", "stale",
                         status["model_generation"], "device")
        self.assert_code("MODEL_CHANGED", self.router.begin_mobile, "mobile", status["server_instance_id"],
                         "stale", "device")
        self.assertEqual(self.workers[0].calls, [])
        self.assertNotEqual(self.router.server_instance_id, EngineRouter(self.router.settings).server_instance_id)

    def test_mobile_and_pc_have_reserved_capacity_and_independent_results(self):
        self.router.prepare("r2t2")
        mobile = self.mobile()
        pc = self.router.begin("r2t2", "pc")
        self.assertEqual(mobile["model_generation"], pc["model_generation"])
        self.assert_code("CAPACITY_EXCEEDED", self.mobile, "mobile2")
        self.assertEqual(self.router.status()["mobile_slots_available"], 0)
        self.assertEqual(len(self.router.status()["active_sessions"]), 2)
        self.assertEqual(self.router.feed("pc", b"\1\0" * 10)["text"], "pc")
        self.assertEqual(self.router.feed("mobile", b"\1\0" * 20)["text"], "mobile")
        self.assertEqual(self.router.flush("mobile")["audio_processed_samples"], 20)
        self.router.cancel("pc")
        self.assertFalse(self.workers[0].closed)
        self.assertEqual(self.router.feed("mobile", b"\1\0" * 5)["audio_processed_samples"], 25)
        self.router.begin("r2t2", "pc2")
        self.router.finish("mobile")
        self.router.end("mobile")
        self.assertEqual(self.router.status()["mobile_slots_available"], 1)
        self.assertEqual(self.router.feed("pc2", b"\1\0")["text"], "pc2")

    def test_same_mode_prepare_is_idempotent_while_mobile_is_active(self):
        self.router.prepare("r2t2")
        binding = self.mobile()
        for _ in range(2):
            self.assertEqual(self.router.prepare("r2t2")["model_generation"], binding["model_generation"])
        self.factory.assert_called_once()
        self.assertEqual(self.router.session_status("mobile")["state"], "active")
        self.assertFalse(self.workers[0].closed)

    def test_blocked_mobile_start_does_not_block_status_cancel_or_model_switch(self):
        for switching in (False, True):
            with self.subTest(switching=switching):
                self.router.prepare("r2t2")
                worker = self.workers[-1]
                worker.block_start = True
                worker.start_entered.clear()
                worker.start_release.clear()
                name = "mobile" + str(switching)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    start = pool.submit(self.mobile, name)
                    self.assertTrue(worker.start_entered.wait(3))
                    status = pool.submit(self.router.status).result(timeout=1)
                    self.assertTrue(status["busy"])
                    self.assertFalse(status["pc_busy"])
                    self.assertEqual(status["active_sessions"][0]["state"], "starting")
                    if switching:
                        pool.submit(self.router.prepare, "qwen-stream").result(timeout=1)
                    else:
                        pool.submit(self.router.cancel, name).result(timeout=1)
                    worker.start_release.set()
                    with self.assertRaises(SessionError) as raised:
                        start.result(timeout=1)
                    self.assertEqual(raised.exception.code, "MODEL_CHANGED" if switching else "cancelled")
                self.assertEqual(worker.sessions, {})
                worker.block_start = False

    def test_blocked_pc_start_can_be_cancelled_without_holding_lifecycle_lock(self):
        self.router.prepare("r2t2")
        worker = self.workers[0]
        worker.block_start = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(self.router.begin, "r2t2", "pc")
            self.assertTrue(worker.start_entered.wait(3))
            self.assertTrue(pool.submit(self.router.status).result(timeout=1)["pc_busy"])
            pool.submit(self.router.cancel, "pc").result(timeout=1)
            worker.start_release.set()
            with self.assertRaises(SessionError):
                start.result(timeout=1)
        self.assertFalse(self.router.gate.locked())
        self.assertFalse(worker.closed)

    def test_pc_recording_protects_model_management_and_old_transcribe(self):
        self.router.begin("r2t2", "pc")
        self.mobile()
        for method, args in ((self.router.prepare, ("vad",)), (self.router.unload, ()),
                             (self.router.transcribe, (b"wav", "request"))):
            with self.assertRaises(BusyError):
                method(*args)
        self.assertFalse(self.router.unload_if_idle(force=True))
        self.assertFalse(self.workers[0].closed)

    def test_pc_model_switch_terminates_mobile_with_fixed_snapshot(self):
        self.router.prepare("r2t2")
        old = self.mobile()
        self.router.feed("mobile", b"\1\0")
        self.router.prepare("qwen-stream")
        final = self.router.session_status("mobile")
        self.assertEqual((final["terminal_reason"], final["text"], final["pending"]),
                         ("MODEL_CHANGED", "mobile", ""))
        self.assertEqual(final["model_generation"], old["model_generation"])
        self.assertNotEqual(self.router.status()["model_generation"], old["model_generation"])
        self.assertEqual(self.workers[0].calls[-1], ("cancel", "mobile"))
        self.assertTrue(self.workers[0].closed)
        self.assert_code("MODEL_CHANGED", self.router.feed, "mobile", b"\1\0")
        self.assertEqual(self.mobile("another")["mode"], "qwen-stream")

    def test_unload_and_reload_have_distinct_generations(self):
        self.router.prepare("r2t2")
        old = self.mobile()
        self.router.unload()
        self.assertEqual(self.router.session_status("mobile")["terminal_reason"], "MODEL_NOT_READY")
        self.assertIsNone(self.router.status()["model_generation"])
        self.router.prepare("r2t2")
        self.assertNotEqual(self.router.status()["model_generation"], old["model_generation"])

    def test_idle_unload_waits_for_all_sessions_and_starts_clock_at_final_release(self):
        self.router.set_auto_unload(True)
        self.router.begin("r2t2", "pc")
        self.mobile()
        self.router.last_used = time.monotonic() - 121
        self.router.end("pc")
        self.assertFalse(self.router.unload_if_idle())
        self.assertFalse(self.router.unload_if_idle(force=True))
        self.router.cancel("mobile")
        self.assertLess(time.monotonic() - self.router.last_used, 1)
        self.assertFalse(self.router.unload_if_idle())
        self.router.last_used -= 121
        self.assertTrue(self.router.unload_if_idle())

    def test_cancel_discards_late_result_without_blocking_other_session(self):
        self.router.begin("r2t2", "pc")
        self.mobile()
        worker = self.workers[0]
        worker.block_session = "mobile"
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(self.router.feed, "mobile", b"\1\0")
            self.assertTrue(worker.entered.wait(3))
            self.assertEqual(self.router.feed("pc", b"\1\0")["text"], "pc")
            self.router.cancel("mobile")
            worker.release.set()
            with self.assertRaises(SessionError):
                call.result(timeout=3)
        self.assertEqual(self.router.session_status("mobile")["text"], "")
        self.assertFalse(worker.closed)
        self.assertTrue(self.router.gate.locked())

    def test_device_revoke_only_ends_that_mobile_session(self):
        self.router.begin("r2t2", "pc")
        active = {"device": True}
        self.router.set_mobile_credential_validator(lambda credential: active.get(credential, False))
        self.mobile()
        self.assertEqual(self.router.revoke_credential_sessions("other"), 0)
        active["device"] = False
        self.assertEqual(self.router.revoke_credential_sessions("device"), 1)
        self.assertEqual(self.router.session_status("mobile")["terminal_reason"], "UNAUTHORIZED")
        self.assert_code("UNAUTHORIZED", self.mobile, "mobile2")
        self.assertEqual(self.router.feed("pc", b"\1\0")["text"], "pc")

    def test_revoke_while_start_is_pending_prevents_late_activation(self):
        self.router.prepare("r2t2")
        active = {"device": True}
        self.router.set_mobile_credential_validator(lambda credential: active.get(credential, False))
        worker = self.workers[0]
        worker.block_start = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            start = pool.submit(self.mobile)
            self.assertTrue(worker.start_entered.wait(3))
            active["device"] = False
            self.assertEqual(self.router.revoke_credential_sessions("device"), 1)
            worker.start_release.set()
            with self.assertRaises(SessionError) as raised:
                start.result(timeout=1)
            self.assertEqual(raised.exception.code, "UNAUTHORIZED")
        self.assertEqual(worker.sessions, {})
        self.assertEqual(self.router.status()["mobile_slots_available"], 1)

    def test_worker_exit_is_reported_without_loading_a_new_model(self):
        self.router.begin("r2t2", "pc")
        self.mobile()
        worker = self.workers[0]
        worker.is_ready = lambda: False
        status = self.router.status()
        self.assertEqual(status["state"], "error")
        self.assertIsNone(status["model_generation"])
        self.assertFalse(worker.closed)
        self.assertEqual(self.router.session_status("mobile")["terminal_reason"], "SERVICE_UNAVAILABLE")
        self.assertEqual(self.router.session_status("pc")["terminal_reason"], "SERVICE_UNAVAILABLE")
        self.assertTrue(worker.closed)
        self.factory.assert_called_once()
        self.assertFalse(self.router.gate.locked())

    def test_qwen_cancel_keeps_worker_and_other_session_resident(self):
        binding = self.router.begin("qwen-stream", "pc")
        worker = self.workers[0]
        mobile = self.mobile()
        self.assertEqual(mobile["mode"], "qwen-stream")
        self.assert_code("CAPACITY_EXCEEDED", self.mobile, "third")
        self.router.feed("mobile", b"\1\0" * 20)
        self.router.end("pc", abort=True)
        self.assertFalse(worker.closed)
        self.assertEqual(worker.calls[-1], ("cancel", "pc"))
        self.assertEqual(self.router.feed("mobile", b"\1\0" * 5)["audio_processed_samples"], 25)
        self.assertEqual(self.router.finish("mobile")["text"], "mobilemobile")
        self.router.end("mobile")
        self.assertEqual(worker.sessions, {})
        self.assertEqual(self.router.status()["model_generation"], binding["model_generation"])
        self.assertFalse(self.router.gate.locked())

    def test_offline_lazy_reload_creates_a_new_generation(self):
        self.router.model_generation = "old"
        self.router.offline.transcribe.side_effect = lambda *args: self.router.offline.status.return_value.update(
            model_loaded=True) or {"text": ""}
        self.router.transcribe(b"wav", "request")
        self.assertNotEqual(self.router.model_generation, "old")

    def test_session_fault_is_isolated_and_global_fault_releases_both(self):
        for fatal in (False, True):
            with self.subTest(fatal=fatal):
                if self.router.active_stream:
                    self.router.end(self.router.active_stream)
                self.router.begin("r2t2", "pc" + str(fatal))
                self.mobile("mobile" + str(fatal))
                worker = self.workers[-1]
                worker.failure = WorkerRPCError("SERVICE_UNAVAILABLE", "failed", fatal=fatal)
                self.assert_code("SERVICE_UNAVAILABLE", self.router.feed, "mobile" + str(fatal), b"\1\0")
                self.assertEqual(worker.closed, fatal)
                self.assertEqual(self.router.gate.locked(), not fatal)
                if fatal:
                    self.assertIsNone(self.router.status()["model_generation"])
                    self.assertEqual(self.router.session_status("pcTrue")["terminal_reason"], "SERVICE_UNAVAILABLE")
                else:
                    worker.failure = None
                    self.assertEqual(self.router.feed("pcFalse", b"\1\0")["text"], "pcFalse")


if __name__ == "__main__":
    unittest.main()
