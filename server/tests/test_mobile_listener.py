"""The mobile listener is a private TLS socket, never an all-interface admin API."""

import asyncio
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from oneaxe_voice.serve import MobileListener, renew_certificates, run as serve_run
from oneaxe_voice.setup_mobile import configure, issue_certificate, main, resolve_docker_image
from oneaxe_voice.config import Settings


DOCKER_IMAGE = "sha256:" + "a" * 64


class ListenerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cert, self.key = self.root / "test.crt", self.root / "test.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-days", "1", "-subj", "/CN=voice.example.ts.net",
                        "-addext", "subjectAltName=DNS:voice.example.ts.net", "-out", str(self.cert),
                        "-keyout", str(self.key)], check=True, capture_output=True)
        self.key.chmod(0o600)
        self.value = dict(hostname="voice.example.ts.net", bind="100.64.1.2", port=8097,
                          cert_file=str(self.cert), key_file=str(self.key), renew=False)

    def write(self):
        (self.root / "mobile-listener.json").write_text(json.dumps(self.value))

    def test_without_configuration_only_local_listener_is_available(self):
        self.assertIsNone(MobileListener.read(self.root))

    def test_valid_tailnet_address_and_matching_private_key(self):
        self.write()
        result = MobileListener.read(self.root)
        self.assertEqual((result.bind, result.port, result.renew), ("100.64.1.2", 8097, False))

    def test_wildcard_lan_loopback_and_public_addresses_are_refused(self):
        for address in ("0.0.0.0", "127.0.0.1", "192.168.1.1", "8.8.8.8", "::", "::1"):
            with self.subTest(address=address):
                self.value["bind"] = address
                self.write()
                with self.assertRaises(ValueError):
                    MobileListener.read(self.root)

    def test_readable_private_key_is_refused(self):
        self.key.chmod(0o644)
        self.write()
        with self.assertRaisesRegex(ValueError, "0600"):
            MobileListener.read(self.root)

    def test_wrong_key_fails_closed(self):
        self.key.write_text("not a key")
        self.write()
        with self.assertRaises(Exception):
            MobileListener.read(self.root)

    def test_invalid_port_and_unknown_fields_are_refused(self):
        for port in (True, 0, 65536, "8097"):
            self.value["port"] = port
            self.write()
            with self.assertRaises(ValueError):
                MobileListener.read(self.root)
        self.value["port"] = 8097
        self.value["allow_insecure"] = True
        self.write()
        with self.assertRaises(ValueError):
            MobileListener.read(self.root)

    def test_certificate_failure_does_not_publish_listener_configuration(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "BackendState": "Running", "Self": {"DNSName": "voice.example.ts.net.", "TailscaleIPs": ["100.64.1.2"]},
            "CertDomains": ["voice.example.ts.net"]}).encode())
        with patch("oneaxe_voice.setup_mobile.subprocess.run", return_value=result), \
             patch("oneaxe_voice.setup_mobile.issue_certificate", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                configure(Settings(runtime_dir=self.root))
        self.assertFalse((self.root / "mobile-listener.json").exists())

    def test_sudo_only_signs_to_stdout_and_project_files_remain_private(self):
        run = subprocess.run
        certificate, private_key = self.cert.read_bytes(), self.key.read_bytes()
        calls = []

        def fake(arguments, **kwargs):
            if arguments[0] == "sudo":
                calls.append(arguments)
                output = certificate + private_key
                return subprocess.CompletedProcess(arguments, 0, stdout=output)
            return run(arguments, **kwargs)

        target_cert, target_key = self.root / "output/cert.pem", self.root / "output/key.pem"
        with patch("oneaxe_voice.setup_mobile.subprocess.run", side_effect=fake):
            issue_certificate("voice.example.ts.net", target_cert, target_key, sudo=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(command[:3] == ["sudo", "tailscale", "cert"] for command in calls))
        self.assertIn("--cert-file=-", calls[0])
        self.assertIn("--key-file=-", calls[0])
        self.assertEqual(target_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target_cert.read_bytes(), certificate)

    def test_signing_error_shows_stderr_but_never_key_stdout(self):
        failed = subprocess.CompletedProcess([], 1, stdout=b"private-key-secret",
                                             stderr=b"certificate authority unavailable")
        with patch("oneaxe_voice.setup_mobile.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "certificate authority unavailable") as caught:
                issue_certificate("voice.example.ts.net", self.cert, self.key, sudo=True)
        self.assertNotIn("private-key-secret", str(caught.exception))

    def test_sudo_setup_explicitly_disables_unprivileged_renewal(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "BackendState": "Running", "Self": {"DNSName": "voice.example.ts.net.", "TailscaleIPs": ["100.64.1.2"]},
            "CertDomains": ["voice.example.ts.net"]}).encode())
        with patch("oneaxe_voice.setup_mobile.subprocess.run", return_value=result), \
             patch("oneaxe_voice.setup_mobile.issue_certificate") as issue:
            value = configure(Settings(runtime_dir=self.root), sudo_cert=True)
        self.assertFalse(value["renew"])
        self.assertTrue(issue.call_args.kwargs["sudo"])

    def test_docker_image_resolves_locally_without_pull(self):
        result = subprocess.CompletedProcess([], 0, stdout=(DOCKER_IMAGE + "\n").encode())
        with patch("oneaxe_voice.setup_mobile.subprocess.run", return_value=result) as run:
            self.assertEqual(resolve_docker_image("ubuntu:24.04"), DOCKER_IMAGE)
        self.assertEqual(run.call_args.args[0],
                         ["docker", "image", "inspect", "--format", "{{.Id}}", "ubuntu:24.04"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_invalid_or_missing_docker_image_does_not_sign_or_publish(self):
        for image in ("--privileged", "", "ubuntu:24.04 extra", None, True):
            with self.subTest(image=image), patch("oneaxe_voice.setup_mobile.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    resolve_docker_image(image)
                run.assert_not_called()
        for code, output in ((1, b""), (0, b"ubuntu:24.04"), (0, b"sha256:short")):
            result = subprocess.CompletedProcess([], code, stdout=output, stderr=b"missing")
            with self.subTest(code=code, output=output), \
                    patch("oneaxe_voice.setup_mobile.subprocess.run", return_value=result) as run, \
                    patch("oneaxe_voice.setup_mobile.issue_certificate") as issue:
                with self.assertRaisesRegex(RuntimeError, "不会自动拉取"):
                    configure(Settings(runtime_dir=self.root), docker_cert="ubuntu:24.04")
                self.assertEqual(run.call_count, 1)
                issue.assert_not_called()
        self.assertFalse((self.root / "mobile-listener.json").exists())

    def test_docker_signing_is_restricted_and_pem_stays_private(self):
        run = subprocess.run
        certificate, private_key = self.cert.read_bytes(), self.key.read_bytes()
        calls = []

        def fake(arguments, **kwargs):
            if arguments[0] == "docker":
                calls.append((arguments, kwargs))
                return subprocess.CompletedProcess(arguments, 0, stdout=certificate + private_key)
            return run(arguments, **kwargs)

        target_cert, target_key = self.root / "docker/cert.pem", self.root / "docker/key.pem"
        with patch("oneaxe_voice.setup_mobile.subprocess.run", side_effect=fake):
            issue_certificate("voice.example.ts.net", target_cert, target_key, docker_image=DOCKER_IMAGE)
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertEqual(command[:11], ["docker", "run", "--rm", "--pull=never", "--network=none",
                                       "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                                       "--user=0:0", "--log-driver=none", "--mount"])
        mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
        self.assertEqual(mounts, ["type=bind,src=/usr/bin/tailscale,dst=/usr/bin/tailscale,readonly",
                                 "type=bind,src=/var/run/tailscale/tailscaled.sock,"
                                 "dst=/var/run/tailscale/tailscaled.sock,readonly"])
        self.assertEqual(command[-8:], ["--entrypoint", "/usr/bin/tailscale", DOCKER_IMAGE, "cert",
                                       "--min-validity=336h", "--cert-file=-", "--key-file=-",
                                       "voice.example.ts.net"])
        self.assertTrue(options["capture_output"])
        self.assertEqual(target_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target_cert.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target_key.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(target_cert.read_bytes(), certificate)

    def test_docker_setup_saves_immutable_image_and_enables_renewal(self):
        status = json.dumps({"BackendState": "Running", "Self": {"DNSName": "voice.example.ts.net.",
                            "TailscaleIPs": ["100.64.1.2"]}, "CertDomains": ["voice.example.ts.net"]}).encode()

        def fake(arguments, **kwargs):
            output = (DOCKER_IMAGE + "\n").encode() if arguments[0] == "docker" else status
            return subprocess.CompletedProcess(arguments, 0, stdout=output)

        with patch("oneaxe_voice.setup_mobile.subprocess.run", side_effect=fake), \
                patch("oneaxe_voice.setup_mobile.issue_certificate") as issue:
            value = configure(Settings(runtime_dir=self.root), docker_cert="ubuntu:24.04")
        self.assertTrue(value["renew"])
        self.assertEqual(value["docker_image"], DOCKER_IMAGE)
        self.assertEqual(issue.call_args.kwargs, {"sudo": False, "docker_image": DOCKER_IMAGE})
        path = self.root / "mobile-listener.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["docker_image"], DOCKER_IMAGE)

    def test_docker_schema_accepts_only_immutable_ids(self):
        self.value.update(docker_image=DOCKER_IMAGE, renew=True)
        self.write()
        listener = MobileListener.read(self.root)
        self.assertEqual(listener.docker_image, DOCKER_IMAGE)
        self.assertTrue(listener.renew)
        for image in ("ubuntu:24.04", "sha256:short", "sha256:" + "A" * 64, None, True, []):
            with self.subTest(image=image):
                self.value["docker_image"] = image
                self.write()
                with self.assertRaisesRegex(ValueError, "sha256"):
                    MobileListener.read(self.root)

    def test_mutable_ids_and_conflicting_signing_modes_fail_before_writes(self):
        with patch("oneaxe_voice.setup_mobile.subprocess.run") as run:
            with self.assertRaises(ValueError):
                configure(Settings(runtime_dir=self.root), sudo_cert=True, docker_cert="ubuntu:24.04")
            for image in ("ubuntu:24.04", "sha256:short"):
                with self.subTest(image=image), self.assertRaises(ValueError):
                    issue_certificate("voice.example.ts.net", self.cert, self.key, docker_image=image)
            with self.assertRaises(ValueError):
                issue_certificate("voice.example.ts.net", self.cert, self.key, sudo=True,
                                  docker_image=DOCKER_IMAGE)
            run.assert_not_called()

    def test_cli_accepts_docker_image_and_rejects_signing_mode_conflicts(self):
        with patch("oneaxe_voice.setup_mobile.os.geteuid", return_value=1000), \
                patch("oneaxe_voice.setup_mobile.os.umask"), \
                patch("oneaxe_voice.setup_mobile.Settings.from_env", return_value=Settings(runtime_dir=self.root)), \
                patch("oneaxe_voice.setup_mobile.configure", return_value=self.value) as configure_mock, \
                patch("sys.stdout", new_callable=io.StringIO):
            main(["--port", "8098", "--docker-cert", "ubuntu:24.04"])
        self.assertEqual(configure_mock.call_args.args[1], 8098)
        self.assertEqual(configure_mock.call_args.kwargs, {"sudo_cert": False, "docker_cert": "ubuntu:24.04"})
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as rejected:
            main(["--sudo-cert", "--docker-cert", "ubuntu:24.04"])
        self.assertEqual(rejected.exception.code, 2)


class CertificateRenewalTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_renewal_uses_pinned_image_and_reload_has_minute_cadence(self):
        cert, key = MagicMock(), MagicMock()
        cert.stat.side_effect = [SimpleNamespace(st_mtime_ns=1), SimpleNamespace(st_mtime_ns=2)]
        key.stat.side_effect = [SimpleNamespace(st_mtime_ns=1), SimpleNamespace(st_mtime_ns=2)]
        listener = MobileListener("voice.example.ts.net", "100.64.1.2", 8097, cert, key,
                                  docker_image=DOCKER_IMAGE)
        config = SimpleNamespace(ssl=MagicMock())
        with patch("oneaxe_voice.serve.asyncio.sleep", new_callable=AsyncMock,
                   side_effect=[None, asyncio.CancelledError]) as sleep, \
                patch("oneaxe_voice.serve.time.monotonic", side_effect=[0, 43200, 43200]), \
                patch("oneaxe_voice.serve.asyncio.to_thread", new_callable=AsyncMock) as issue, \
                patch("oneaxe_voice.setup_mobile.issue_certificate") as signer:
            with self.assertRaises(asyncio.CancelledError):
                await renew_certificates(listener, config)
        issue.assert_awaited_once_with(signer, listener.hostname, cert, key, docker_image=DOCKER_IMAGE)
        self.assertEqual([call.args for call in sleep.call_args_list], [(60,), (60,)])
        config.ssl.load_cert_chain.assert_called_once_with(str(cert), str(key))


class SharedListenerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_failure_cleans_other_session_before_shared_lifespan_shutdown(self):
        events, servers = [], []
        active = {"pc": False}
        ready = asyncio.Event()

        @asynccontextmanager
        async def lifespan(app):
            events.append("lifespan_start")
            try:
                yield
            finally:
                events.append("unload_blocked" if active["pc"] else "model_unloaded")

        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))

        class FakeServer:
            def __init__(self, config):
                self.index = len(servers)
                self.should_exit = False
                servers.append(self)

            async def serve(self, **kwargs):
                if self.index == 1:
                    await ready.wait()
                    raise RuntimeError("mobile listener failed")
                active["pc"] = True
                events.append("pc_session_active")
                ready.set()
                while not self.should_exit:
                    await asyncio.sleep(0)
                # Session teardown may await a worker RPC after shutdown starts.
                await asyncio.sleep(.01)
                active["pc"] = False
                events.append("pc_session_cleaned")

        def configuration(*args, **kwargs):
            return SimpleNamespace(**kwargs, load=lambda: None)

        async def renewal(*args):
            await asyncio.Future()

        mobile = SimpleNamespace(bind="100.64.1.2", port=8097,
                                 cert_file="fake.crt", key_file="fake.key")
        sockets = [MagicMock(), MagicMock()]
        loop = asyncio.get_running_loop()
        with patch("oneaxe_voice.server.create_app", return_value=app) as create, \
                patch("oneaxe_voice.serve.MobileListener.read", return_value=mobile), \
                patch("oneaxe_voice.serve.uvicorn.Config", side_effect=configuration) as config, \
                patch("oneaxe_voice.serve.SharedServer", FakeServer), \
                patch("oneaxe_voice.serve.listen", side_effect=sockets), \
                patch("oneaxe_voice.serve.renew_certificates", renewal), \
                patch.object(loop, "add_signal_handler"), \
                patch.object(loop, "remove_signal_handler") as remove:
            with self.assertRaisesRegex(RuntimeError, "mobile listener failed"):
                await asyncio.wait_for(serve_run(Settings()), 1)
        self.assertEqual(events, ["lifespan_start", "pc_session_active", "pc_session_cleaned", "model_unloaded"])
        create.assert_called_once()
        self.assertEqual(config.call_count, 2)
        self.assertTrue(all(call.kwargs["lifespan"] == "off" and not call.kwargs["proxy_headers"]
                            for call in config.call_args_list))
        self.assertEqual(remove.call_count, 2)
        for sock in sockets:
            sock.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
