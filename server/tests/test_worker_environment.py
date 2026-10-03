"""Worker launch environments with the real socket readiness protocol."""

import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oneaxe_voice.engines import Worker


class WorkerEnvironmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        model = root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}", encoding="ascii")
        self.settings = SimpleNamespace(model_dir=model, runtime_dir=root, cuda_device=0)
        environment = patch.dict(os.environ, {
            "ONEAXE_VOICE_STREAM_PYTHON": sys.executable,
            "ONEAXE_VOICE_R2T2_MODEL_DIR": str(model),
            "OMP_NUM_THREADS": "11",
        })
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("OMP_WAIT_POLICY", None)
        self.launches = []
        launcher = patch("oneaxe_voice.engines.subprocess.Popen", side_effect=self.launch)
        launcher.start()
        self.addCleanup(launcher.stop)
        signals = patch("oneaxe_voice.engines.os.killpg")
        signals.start()
        self.addCleanup(signals.stop)

    def launch(self, command, **kwargs):
        self.launches.append({
            "command": list(command),
            "env": dict(kwargs["env"]),
            "parent_env": dict(os.environ),
        })
        peer = socket.socket(fileno=os.dup(kwargs["pass_fds"][0]))
        self.addCleanup(peer.close)
        peer.sendall(b'{"ready": true}\n')
        process = Mock()
        process.pid = 123456
        process.poll.return_value = None
        process.wait.return_value = 0
        return process

    def start(self, mode):
        parent_environment = dict(os.environ)
        worker = Worker(self.settings, mode)
        self.addCleanup(worker.close)
        launch = self.launches[-1]
        self.assertEqual(launch["parent_env"], parent_environment)
        self.assertEqual(dict(os.environ), parent_environment)
        self.assertTrue(worker.is_ready())
        self.assertEqual(launch["env"]["OMP_NUM_THREADS"], "4")
        self.assertEqual(os.environ["OMP_NUM_THREADS"], "11")
        return launch

    def test_r2t2_defaults_to_passive_at_launch(self):
        launch = self.start("r2t2")
        self.assertEqual(launch["env"]["OMP_WAIT_POLICY"], "PASSIVE")
        self.assertEqual(launch["command"][2], "oneaxe_voice.concurrent_worker")
        self.assertNotIn("OMP_WAIT_POLICY", os.environ)

    def test_r2t2_overrides_parent_active_at_launch(self):
        os.environ["OMP_WAIT_POLICY"] = "ACTIVE"
        launch = self.start("r2t2")
        self.assertEqual(launch["env"]["OMP_WAIT_POLICY"], "PASSIVE")
        self.assertEqual(os.environ["OMP_WAIT_POLICY"], "ACTIVE")

    def test_qwen_uses_passive_without_changing_parent_wait_policy(self):
        for policy in (None, "ACTIVE", "PASSIVE"):
            with self.subTest(policy=policy):
                if policy is None:
                    os.environ.pop("OMP_WAIT_POLICY", None)
                else:
                    os.environ["OMP_WAIT_POLICY"] = policy
                launch = self.start("qwen-stream")
                self.assertEqual(launch["command"][2], "oneaxe_voice.concurrent_worker")
                self.assertEqual(launch["env"]["OMP_WAIT_POLICY"], "PASSIVE")
                if policy is None:
                    self.assertNotIn("OMP_WAIT_POLICY", os.environ)
                else:
                    self.assertEqual(os.environ["OMP_WAIT_POLICY"], policy)


if __name__ == "__main__":
    unittest.main()
