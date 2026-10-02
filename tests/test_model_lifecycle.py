"""Model residency controls without loading CUDA in the test process."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from oneaxe_voice.config import Settings
from oneaxe_voice.backend import QwenEngine
from oneaxe_voice.engines import EngineRouter
from oneaxe_voice.server import create_app


class ModelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(runtime_dir=Path(self.temp.name))
        self.settings.token_path.write_text("a" * 40)
        self.router = EngineRouter(self.settings)
        self.router.offline = MagicMock()
        self.router.offline.status.return_value = {"model_loaded": False, "state": "unloaded"}
        self.worker = MagicMock()
        self.worker.capacity = 2
        self.worker.engine_config = self.worker.warmup = {}
        self.worker.process.pid = 1234
        self.patcher = patch("oneaxe_voice.engines.Worker", return_value=self.worker)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.headers = {"Authorization": "Bearer " + "a" * 40}
        self.client = TestClient(create_app(self.settings, self.router), client=("127.0.0.1", 1234))
        self.addCleanup(self.client.close)

    def test_manual_load_unload_and_busy_session(self):
        self.assertEqual(self.client.get("/api/dictation/status", headers=self.headers).json()["state"], "unloaded")
        response = self.client.post("/api/dictation/prepare", json={"mode": "r2t2"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/dictation/status", headers=self.headers).json()["state"], "ready")
        self.router.begin("r2t2", "session")
        response = self.client.post("/api/dictation/unload", headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.worker.close.assert_not_called()
        self.router.end("session")
        response = self.client.post("/api/dictation/unload", headers=self.headers)
        self.assertEqual((response.status_code, response.json()["state"]), (200, "unloaded"))
        self.assertEqual(self.worker.close.call_count, 1)
        self.assertEqual(self.client.post("/api/dictation/unload", headers=self.headers).status_code, 200)
        self.assertEqual(self.worker.close.call_count, 1)

    def test_policy_is_private_persistent_and_idle_clock_is_read_only(self):
        self.router.prepare("r2t2")
        self.router.last_used = time.monotonic() - 121
        for _ in range(3):
            status = self.client.get("/api/dictation/status", headers=self.headers).json()
            self.assertEqual((status["auto_unload"], status["idle_seconds"]), (False, 0))
        self.assertFalse(self.router.unload_if_idle())
        self.assertEqual(self.client.post("/api/dictation/policy", json={"auto_unload": "true"}, headers=self.headers).status_code, 422)
        response = self.client.post("/api/dictation/policy", json={"auto_unload": True}, headers=self.headers)
        self.assertEqual((response.status_code, response.json()["idle_seconds"]), (200, 120))
        policy_path = self.settings.runtime_dir / "model-policy.json"
        self.assertEqual(json.loads(policy_path.read_text()), {"auto_unload": True})
        self.assertEqual(policy_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(EngineRouter(self.settings).status()["auto_unload"])
        self.assertTrue(self.router.unload_if_idle())
        self.assertEqual(self.worker.close.call_count, 1)
        response = self.client.post("/api/dictation/policy", json={"auto_unload": False}, headers=self.headers)
        self.assertEqual(response.json()["idle_seconds"], 0)
        self.assertFalse(EngineRouter(self.settings).status()["auto_unload"])

    def test_timeout_boundary_and_state_during_unload(self):
        self.router.set_auto_unload(True)
        self.router.prepare("r2t2")
        self.router.last_used = time.monotonic() - 119
        self.assertFalse(self.router.unload_if_idle())
        states = []
        self.worker.close.side_effect = lambda: states.append(self.router.status()["state"])
        self.router.last_used = time.monotonic() - 121
        self.assertTrue(self.router.unload_if_idle())
        self.assertEqual(states, ["unloading"])
        self.assertEqual(self.router.status()["state"], "unloaded")

    def test_policy_changes_while_stream_is_active_and_status_tracks_offline_load(self):
        self.router.offline.status.return_value = {"state": "loading", "model_loaded": False}
        self.assertEqual(self.router.status()["state"], "loading")
        self.router.offline.status.return_value = {"state": "transcribing", "model_loaded": True}
        self.assertEqual(self.router.status()["state"], "transcribing")
        self.router.offline.status.return_value = {"state": "unloaded", "model_loaded": False}
        self.router.begin("r2t2", "session")
        response = self.client.post("/api/dictation/policy", json={"auto_unload": True}, headers=self.headers)
        self.assertEqual((response.status_code, response.json()["auto_unload"]), (200, True))
        self.assertTrue(self.router.gate.locked())
        self.assertFalse(self.router.unload_if_idle())
        self.router.end("session")

    def test_status_worker_snapshot_survives_concurrent_clear(self):
        self.router.prepare("r2t2")
        self.worker.process = None
        self.assertIsNone(self.router.status()["worker_pid"])

    def test_explicit_unload_clears_old_offline_error_with_or_without_weights(self):
        for loaded in (False, True):
            with self.subTest(loaded=loaded):
                self.router.offline = QwenEngine(self.settings)
                self.router.offline._model = object() if loaded else None
                self.router.offline._update(model_loaded=loaded, last_error='AudioError')
                result = self.router.unload()
                self.assertEqual(result['state'], 'unloaded')
                self.assertFalse(result['model_loaded'])
                self.assertIsNone(result['last_error'])


if __name__ == "__main__":
    unittest.main()
