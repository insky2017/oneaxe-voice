"""Capacity configuration and worker handshake checks without inference."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from oneaxe_voice.backend import GPUError
from oneaxe_voice.config import Settings
from oneaxe_voice.engines import Worker


class CapacitySettingsTests(unittest.TestCase):
    def test_default_and_bounds_are_valid(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings.from_env().stream_max_num_seqs, 2)
        for capacity in (2, 4, 16):
            with self.subTest(capacity=capacity):
                self.assertEqual(Settings(stream_max_num_seqs=capacity).stream_max_num_seqs, capacity)
                with patch.dict(os.environ, {"ONEAXE_VOICE_STREAM_MAX_NUM_SEQS": str(capacity)}):
                    self.assertEqual(Settings.from_env().stream_max_num_seqs, capacity)

    def test_invalid_capacity_is_rejected_for_direct_and_environment_settings(self):
        for capacity in (None, True, 4.0, "4", 1, 17):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                Settings(stream_max_num_seqs=capacity)
        for capacity in ("", "x", "4.0", "1", "17"):
            with self.subTest(environment=capacity):
                with patch.dict(os.environ, {"ONEAXE_VOICE_STREAM_MAX_NUM_SEQS": capacity}):
                    with self.assertRaises(ValueError):
                        Settings.from_env()


class CapacityHandshakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        interpreter = root / "synthetic-python"
        interpreter.write_text("")
        model = root / "synthetic-model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        self.settings = Settings(runtime_dir=root, stream_max_num_seqs=4)
        self.environment = patch.dict(os.environ, {
            "ONEAXE_VOICE_STREAM_PYTHON": str(interpreter),
            "ONEAXE_VOICE_R2T2_MODEL_DIR": str(model),
            "ONEAXE_VOICE_STREAM_MAX_NUM_SEQS": "16",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.process = MagicMock()
        self.process.pid = 99999999
        self.process.poll.return_value = None
        self.popen = patch("oneaxe_voice.engines.subprocess.Popen", return_value=self.process).start()
        self.client = MagicMock()
        self.client.failure = None
        patch("oneaxe_voice.engines.WorkerClient", return_value=self.client).start()
        patch("oneaxe_voice.engines.os.killpg").start()
        self.addCleanup(patch.stopall)

    def test_settings_override_ambient_capacity_and_ready_metadata_is_saved_without_paths(self):
        self.client.wait_ready.return_value = {
            "ready": True, "capacity": 4,
            "engine_config": {
                "max_num_seqs": 4, "kv_cache_memory_bytes": 2 * 1024 ** 3,
                "enable_log_requests": False, "model": "/private/model",
                "compilation_config": {"mode": 0, "cudagraph_mode": "NONE",
                                       "cudagraph_capture_sizes": [], "model": "/private/model"},
            },
            "warmup": {"capacity": 4, "request_count": 8, "window_samples": 32000,
                       "duration_ms": 10.5, "text": "private text"},
        }
        worker = Worker(self.settings, "r2t2")
        self.addCleanup(worker.close)
        self.assertEqual(self.popen.call_args.kwargs["env"]["ONEAXE_VOICE_STREAM_MAX_NUM_SEQS"], "4")
        self.assertEqual(worker.capacity, 4)
        self.assertEqual(worker.engine_config["max_num_seqs"], 4)
        self.assertFalse(worker.engine_config["enable_log_requests"])
        self.assertEqual(worker.warmup["request_count"], 8)
        self.assertNotIn("model", worker.engine_config)
        self.assertNotIn("model", worker.engine_config["compilation_config"])
        self.assertNotIn("text", worker.warmup)
        self.assertTrue(worker.is_ready())

    def test_missing_invalid_or_mismatched_ready_capacity_closes_the_child(self):
        for capacity in (None, True, 4.0, "4", 1, 17, 2):
            with self.subTest(capacity=capacity):
                self.client.reset_mock()
                self.process.reset_mock()
                self.client.wait_ready.return_value = {"ready": True, "capacity": capacity}
                with self.assertRaises(GPUError):
                    Worker(self.settings, "r2t2")
                self.client.close.assert_called_once()
                self.process.wait.assert_called()


if __name__ == "__main__":
    unittest.main()
