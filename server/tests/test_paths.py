"""Portable path and systemd unit rendering checks."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile

from oneaxe_voice.config import DEFAULT_MODEL_DIR, Settings
from oneaxe_voice import install as install_module
from oneaxe_voice.install import render_unit, systemd_path, systemd_quote


class PortablePathTests(unittest.TestCase):
    def test_default_model_path_uses_current_home(self):
        self.assertEqual(DEFAULT_MODEL_DIR, Path.home() / "tools/models/Qwen3-ASR-1.7B")

    def test_model_environment_override_expands_home(self):
        with patch.dict(os.environ, {"HOME": "/tmp/voice home", "ONEAXE_VOICE_MODEL_DIR": "~/models/asr"}):
            self.assertEqual(Settings.from_env().model_dir, Path("/tmp/voice home/models/asr"))

    def test_systemd_quote_preserves_spaces_and_escapes_specifiers(self):
        self.assertEqual(systemd_quote('/tmp/voice % $HOME "test"'), '"/tmp/voice %% $$HOME \\"test\\""')

    def test_install_replaces_old_symlink_without_overwriting_template(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "checkout"
            source_dir = root / "systemd"
            source_dir.mkdir(parents=True)
            (root / "bin").mkdir()
            source_unit = source_dir / "oneaxe-voice.service"
            source_template = (
                "[Service]\n"
                "WorkingDirectory={{PROJECT_DIR}}\n"
                "ExecStart={{SERVE_COMMAND}}\n"
            )
            source_unit.write_text(source_template, encoding="utf-8")
            config_home = Path(temp) / "config"
            unit_path = config_home / "systemd/user/oneaxe-voice.service"
            unit_path.parent.mkdir(parents=True)
            unit_path.symlink_to(source_unit)

            with (
                patch.object(install_module, "ROOT", root),
                patch.object(install_module, "TEMPLATE", source_unit),
                patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                patch.object(install_module.subprocess, "run") as run,
            ):
                installed_path = install_module.install()

            self.assertEqual(installed_path, unit_path)
            self.assertFalse(unit_path.is_symlink())
            self.assertTrue(unit_path.is_file())
            self.assertEqual(source_unit.read_text(encoding="utf-8"), source_template)
            self.assertIn(f"WorkingDirectory={install_module.systemd_path(root.resolve())}", unit_path.read_text())
            run.assert_called_once_with(["systemctl", "--user", "daemon-reload"], check=True)

    def test_template_renders_arbitrary_checkout_path(self):
        rendered = render_unit(
            "WorkingDirectory={{PROJECT_DIR}}\nExecStart={{SERVE_COMMAND}}\n",
            {
                "PROJECT_DIR": systemd_path("/tmp/OneAxe Voice % checkout"),
                "SERVE_COMMAND": systemd_quote("/tmp/OneAxe Voice % checkout/bin/serve"),
            },
        )
        self.assertIn("WorkingDirectory=/tmp/OneAxe\\x20Voice\\x20%%\\x20checkout", rendered)
        self.assertIn('ExecStart="/tmp/OneAxe Voice %% checkout/bin/serve"', rendered)
        self.assertNotIn("{{", rendered)


if __name__ == "__main__":
    unittest.main()
