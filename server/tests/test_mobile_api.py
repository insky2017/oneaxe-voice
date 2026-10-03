"""Device authorization and V1 flow isolation without models or real tokens."""

import asyncio
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from oneaxe_voice.cli import main
from oneaxe_voice.config import Settings
from oneaxe_voice.device_auth import DeviceCredentialStore, MOBILE_READ
from oneaxe_voice.mobile_api import AUDIO_FORMAT, ProtocolError, StreamLimits, V1Stream
from oneaxe_voice.server import LocalAccess, create_app


class StubSessions:
    def __init__(self):
        self.mode, self.generation, self.instance = "r2t2", "generation-1", "instance-1"
        self.loaded = True
        self.sessions = {}
        self.lock = threading.RLock()
        self.manage_calls = 0
        self.block = None
        self.entered = threading.Event()
        self.validator = lambda credential_id: True

    def set_mobile_credential_validator(self, callback):
        self.validator = callback

    def status(self):
        with self.lock:
            mobile = sum(value["mobile"] and not value["terminal_reason"]
                         for value in self.sessions.values())
            return {"state": "ready" if self.loaded else "unloaded", "model_loaded": self.loaded,
                    "mode": self.mode, "model": "Confucius4-R2T2", "max_sessions": 2,
                    "server_instance_id": self.instance,
                    "model_generation": self.generation if self.loaded else None,
                    "mobile_slots_available": 1 - mobile}

    def _begin(self, session, mobile, credential_id=None):
        binding = {"session_id": session, "server_instance_id": self.instance,
                   "model_generation": self.generation, "device": "cuda:0", "mode": self.mode}
        self.sessions[session] = {**binding, "mobile": mobile, "credential_id": credential_id,
                                  "text": "", "pending": "", "audio_processed_samples": 0,
                                  "state": "active", "terminal_reason": None}
        return binding

    def begin_mobile(self, session, instance, generation, credential_id):
        with self.lock:
            if not self.validator(credential_id):
                raise ProtocolError("UNAUTHORIZED", "revoked")
            if instance != self.instance or generation != self.generation:
                raise ProtocolError("MODEL_CHANGED", "changed")
            if not self.loaded:
                raise ProtocolError("MODEL_NOT_READY", "not ready")
            if self.mode not in ("r2t2", "qwen-stream"):
                raise ProtocolError("MODEL_UNSUPPORTED", "unsupported")
            if not self.status()["mobile_slots_available"]:
                raise ProtocolError("CAPACITY_EXCEEDED", "full", True)
            return self._begin(session, True, credential_id)

    def begin(self, mode, session):
        with self.lock:
            self.manage_calls += 1
            return self._begin(session, False)

    def feed(self, session, data):
        if self.block is not None and self.sessions[session]["mobile"]:
            self.entered.set()
            self.block.wait(3)
        with self.lock:
            value = self.sessions[session]
            if value["terminal_reason"]:
                raise ProtocolError(value["terminal_reason"], "ended")
            value["audio_processed_samples"] += len(data) // 2
            if any(data):
                value["text"] += "x"
            return dict(value)

    def flush(self, session):
        return self.session_status(session)

    def finish(self, session):
        return self.session_status(session)

    def session_status(self, session):
        with self.lock:
            return dict(self.sessions[session])

    def cancel(self, session, reason="cancelled"):
        with self.lock:
            if session in self.sessions:
                self.sessions[session]["state"] = "cancelled"
                self.sessions[session]["terminal_reason"] = reason

    def end(self, session, abort=False):
        with self.lock:
            self.sessions[session]["state"] = "ended"

    def revoke_credential_sessions(self, credential_id):
        with self.lock:
            for session, value in self.sessions.items():
                if value["credential_id"] == credential_id:
                    self.cancel(session, "UNAUTHORIZED")

    def unload_if_idle(self, force=False):
        return False


class TailnetSurface:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            scope = {**scope, "server": ("100.76.106.96", 8097)}
        await self.app(scope, receive, send)


class CredentialTests(unittest.TestCase):
    def test_digest_only_private_atomic_rotation_and_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "mobile-devices.json"
            store = DeviceCredentialStore(path)
            issued = store.issue("test device")
            principal = store.authenticate(issued["token"])
            self.assertEqual(principal.credential_id, issued["credential_id"])
            self.assertNotIn(issued["token"], path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("token_sha256", store.list_devices()[0])
            other = DeviceCredentialStore(path)
            rotated = store.rotate(issued["credential_id"])
            self.assertEqual(rotated["device_id"], issued["device_id"])
            self.assertIsNone(other.authenticate(issued["token"]))
            self.assertIsNotNone(other.authenticate(rotated["token"]))
            store.revoke(rotated["credential_id"])
            self.assertIsNone(other.authenticate(rotated["token"]))


class MobileAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(Settings(), runtime_dir=Path(self.temp.name))
        self.settings.token_path.write_text("p" * 43)
        self.store = DeviceCredentialStore(self.settings.runtime_dir / "mobile-devices.json")
        self.device = self.store.issue("test phone")
        self.engine = StubSessions()
        self.app = create_app(self.settings, self.engine, self.store)
        self.client = TestClient(self.app, client=("127.0.0.1", 1234))
        self.addCleanup(self.client.close)
        self.pc_headers = {"Authorization": "Bearer " + "p" * 43}
        self.mobile_headers = {"Authorization": "Bearer " + self.device["token"]}

    def start(self):
        return {"type": "start", "protocol_version": 1, "audio": AUDIO_FORMAT,
                "expected_server_instance_id": self.engine.instance,
                "expected_model_generation": self.engine.generation}

    def connect(self, client=None, headers=None):
        return (client or self.client).websocket_connect("/api/mobile/v1/dictation/stream",
                                                       headers=headers or self.mobile_headers)

    def until(self, ws, kind, limit=30):
        for _ in range(limit):
            event = ws.receive_json()
            if event["type"] == kind:
                return event
        self.fail("expected event was not delivered")

    def test_capabilities_is_read_only_and_distinguishes_readiness(self):
        self.assertEqual(self.client.get("/api/mobile/v1/capabilities").status_code, 401)
        self.assertEqual(self.client.get("/api/mobile/v1/capabilities", headers=self.pc_headers).status_code, 401)
        response = self.client.get("/api/mobile/v1/capabilities", headers=self.mobile_headers)
        self.assertTrue(response.json()["can_start"])
        self.assertEqual(self.engine.manage_calls, 0)
        self.engine.mode = "qwen-stream"
        value = self.client.get("/api/mobile/v1/capabilities", headers=self.mobile_headers).json()
        self.assertTrue(value["can_start"])
        self.assertTrue(value["stream_supported"])
        self.assertEqual(value["mode"], "qwen-stream")
        self.engine.mode = "vad"
        value = self.client.get("/api/mobile/v1/capabilities", headers=self.mobile_headers).json()
        self.assertTrue(value["ready"])
        self.assertEqual(value["unavailable_reason"], "MODEL_UNSUPPORTED")
        self.engine.loaded = False
        value = self.client.get("/api/mobile/v1/capabilities", headers=self.mobile_headers).json()
        self.assertIsNone(value["model_generation"])
        self.assertEqual(value["unavailable_reason"], "MODEL_NOT_READY")

    def test_mobile_credentials_cannot_use_any_old_or_admin_route(self):
        for path in ("status", "prepare", "warmup", "unload", "policy", "transcribe"):
            with self.subTest(path=path):
                response = self.client.request("GET" if path == "status" else "POST",
                                               "/api/dictation/" + path, headers=self.mobile_headers)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/devices", headers=self.mobile_headers).status_code, 403)
        for path in ("/api/dictation/stream", "/api/dictation/v1/stream"):
            with self.assertRaises(WebSocketDisconnect) as rejected:
                with self.client.websocket_connect(path, headers=self.mobile_headers):
                    pass
            self.assertEqual(rejected.exception.code, 1008)
        self.assertEqual(self.engine.manage_calls, 0)

    def test_tailnet_surface_hides_local_health_and_management(self):
        with TestClient(TailnetSurface(self.app), client=("100.64.0.2", 1234),
                        base_url="https://voice.test") as client:
            self.assertEqual(client.get("/health").status_code, 403)
            self.assertEqual(client.get("/api/dictation/status", headers=self.pc_headers).status_code, 403)
            self.assertEqual(client.get("/api/devices", headers=self.pc_headers).status_code, 403)
            self.assertEqual(client.get("/api/mobile/v1/capabilities", headers=self.mobile_headers).status_code, 200)

    def test_stream_scope_and_origin_are_enforced_before_start(self):
        read_only = self.store.issue("read only", scopes=(MOBILE_READ,))
        headers = {"Authorization": "Bearer " + read_only["token"]}
        self.assertEqual(self.client.get("/api/mobile/v1/capabilities", headers=headers).status_code, 200)
        for headers, status in ((headers, 403),
                                ({**self.mobile_headers, "Origin": "https://example.test"}, 403),
                                ({}, 401)):
            with self.assertRaises(WebSocketDenialResponse) as rejected:
                with self.client.websocket_connect("/api/mobile/v1/dictation/stream", headers=headers):
                    pass
            self.assertEqual(rejected.exception.status_code, status)
            self.assertFalse(rejected.exception.json()["retryable"])

    def test_start_management_fields_and_stale_generation_are_rejected(self):
        for change, expected in (({"mode": "r2t2"}, "INVALID_MESSAGE"),
                                 ({"prepare": True}, "INVALID_MESSAGE"),
                                 ({"expected_model_generation": "old"}, "MODEL_CHANGED"),
                                 ({"expected_server_instance_id": "old"}, "MODEL_CHANGED")):
            with self.subTest(change=change), self.connect() as ws:
                ws.send_json({**self.start(), **change})
                event = ws.receive_json()
                self.assertEqual((event["code"], event["session_id"], event["seq"]), (expected, None, 0))
        self.assertEqual(self.engine.manage_calls, 0)

    def test_silence_progress_final_snapshot_and_monotonic_events(self):
        with self.connect() as ws:
            ws.send_json(self.start())
            ready = ws.receive_json()
            self.assertEqual(ready["audio_send_limit"], 32000)
            ws.send_bytes(b"\0\0" * 2560)
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "partial":
                    break
            self.assertEqual(events[-1]["text"], "")
            self.assertEqual(events[-1]["audio_processed_samples"], 2560)
            self.assertEqual(events[-1]["audio_send_limit"], 34560)
            ws.send_json({"type": "finish", "after_audio_samples": 2560})
            final = self.until(ws, "final")
            self.assertTrue(final["complete"])
            seqs = [ready["seq"], *[item["seq"] for item in events], final["seq"]]
            self.assertEqual(seqs, sorted(set(seqs)))
            self.assertEqual(self.engine.manage_calls, 0)

    def test_controls_do_not_wait_for_blocked_mobile_inference_and_pc_survives(self):
        self.engine.block = threading.Event()
        with self.client as client:
            with client.websocket_connect("/api/dictation/v1/stream", headers=self.pc_headers) as pc:
                pc.send_json({"type": "start", "protocol_version": 1, "mode": "r2t2", "audio": AUDIO_FORMAT})
                pc_ready = pc.receive_json()
                with self.connect(client) as mobile:
                    mobile.send_json(self.start())
                    mobile_ready = mobile.receive_json()
                    mobile.send_bytes(b"\1\0" * 2560)
                    self.assertTrue(self.engine.entered.wait(2))
                    mobile.send_json({"type": "keepalive"})
                    self.assertEqual(self.until(mobile, "keepalive")["session_id"], mobile_ready["session_id"])
                    pc.send_bytes(b"\1\0" * 2560)
                    self.assertEqual(self.until(pc, "partial")["text"], "x")
                    mobile.send_json({"type": "cancel"})
                    self.assertFalse(self.until(mobile, "final")["complete"])
                    self.engine.block.set()
                self.assertFalse(self.engine.session_status(pc_ready["session_id"])["terminal_reason"])
                pc.send_json({"type": "finish", "after_audio_samples": 2560})
                self.assertTrue(self.until(pc, "final")["complete"])

    def test_qwen_full_frames_cross_decode_boundary_without_credit_deadlock(self):
        self.engine.mode = "qwen-stream"
        original_feed = self.engine.feed

        def chunked_feed(session, data):
            value = original_feed(session, data)
            return {**value, "audio_processed_samples": value["audio_processed_samples"] // 32000 * 32000}

        self.engine.feed = chunked_feed
        caps = self.client.get("/api/mobile/v1/capabilities", headers=self.mobile_headers).json()
        with self.connect() as ws:
            ws.send_json(self.start())
            ready = ws.receive_json()
            self.assertEqual(ready["audio_send_limit"], caps["flow"]["window_samples"])
            self.assertGreaterEqual(ready["audio_send_limit"], 13 * 2560)
            for _ in range(13):
                ws.send_bytes(b"\1\0" * 2560)
            event = self.until(ws, "partial")
            while event["audio_processed_samples"] < 32000:
                event = self.until(ws, "partial")
            self.assertEqual(event["audio_received_samples"], 13 * 2560)
            for _ in range(12):
                ws.send_bytes(b"\1\0" * 2560)
            ws.send_json({"type": "finish", "after_audio_samples": 64000})
            final = self.until(ws, "final", limit=64)
            self.assertEqual(final["audio_processed_samples"], 64000)
            self.assertTrue(final["complete"])
            self.assertEqual(self.engine.manage_calls, 0)

    def test_flow_limit_is_enforced_before_extra_audio_enters_engine(self):
        self.engine.block = threading.Event()
        with self.connect() as ws:
            ws.send_json(self.start())
            ws.receive_json()
            for _ in range(13):
                ws.send_bytes(b"\1\0" * 2560)
            event = self.until(ws, "error")
            self.assertEqual(event["code"], "FLOW_CONTROL_EXCEEDED")
            self.assertEqual(event["audio_received_samples"], 30720)
            self.engine.block.set()

    def test_sample_duration_limit_rejects_before_receiving_extra_audio(self):
        app = create_app(self.settings, self.engine, self.store,
                         StreamLimits(session_seconds=1, monitor_interval=2))
        with TestClient(app, client=("127.0.0.1", 1234)) as client, self.connect(client) as ws:
            ws.send_json(self.start())
            ws.receive_json()
            for _ in range(6):
                ws.send_bytes(b"\0\0" * 2560)
                self.until(ws, "partial")
            ws.send_bytes(b"\0\0" * 640)
            self.until(ws, "partial")
            ws.send_bytes(b"\0\0")
            event = self.until(ws, "error")
            self.assertEqual(event["code"], "SESSION_LIMIT")
            self.assertEqual(event["audio_received_samples"], 16000)

    def test_revoke_terminates_live_device_and_rotation_keeps_old_token_denied(self):
        with self.client as client, self.connect(client) as ws:
            ws.send_json(self.start())
            ws.receive_json()
            result = client.post(f"/api/devices/{self.device['credential_id']}/revoke", headers=self.pc_headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.until(ws, "error")["code"], "UNAUTHORIZED")
            self.assertEqual(client.get("/api/mobile/v1/capabilities", headers=self.mobile_headers).status_code, 401)
            self.assertEqual(client.get("/api/dictation/status", headers=self.mobile_headers).status_code, 403)

    def test_cli_prints_token_only_for_explicit_issue_or_rotation(self):
        issued = {"credential_id": "test-id", "token": "synthetic-test-value"}
        responses = [issued, {"devices": [{"credential_id": "test-id"}]}]
        with patch("oneaxe_voice.cli.Settings.from_env", return_value=self.settings), \
                patch("oneaxe_voice.cli.httpx.Client") as client, patch("sys.stdout", new_callable=io.StringIO) as output:
            client.return_value.__enter__.return_value.post.return_value.json.return_value = responses[0]
            client.return_value.__enter__.return_value.post.return_value.is_error = False
            self.assertEqual(main(["device", "issue", "test phone"]), 0)
            self.assertIn(issued["token"], output.getvalue())
            output.seek(0)
            output.truncate()
            client.return_value.__enter__.return_value.get.return_value.json.return_value = responses[1]
            client.return_value.__enter__.return_value.get.return_value.is_error = False
            self.assertEqual(main(["device", "list"]), 0)
            self.assertNotIn(issued["token"], output.getvalue())

    def test_cli_token_file_is_private_and_stdout_has_only_metadata(self):
        credential_id = "00000000-0000-0000-0000-000000000001"
        token = "synthetic-device-token-" + "a" * 32
        for action, value in (("issue", "test phone"), ("rotate", credential_id)):
            target = Path(self.temp.name) / (action + ".token")
            with self.subTest(action=action), \
                    patch("oneaxe_voice.cli.Settings.from_env", return_value=self.settings), \
                    patch("oneaxe_voice.cli.httpx.Client") as client, \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                response = client.return_value.__enter__.return_value.post.return_value
                response.json.return_value = {"credential_id": credential_id, "token": token}
                response.is_error = False
                self.assertEqual(main(["device", action, value, "--token-file", str(target)]), 0)
                self.assertEqual(target.read_text(), token + "\n")
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(token, output.getvalue())
                self.assertNotIn("token", json.loads(output.getvalue()))

    def test_cli_token_file_rejects_overwrite_and_removes_failed_reservation(self):
        target = Path(self.temp.name) / "private.token"
        target.write_text("preserve existing value")
        with patch("oneaxe_voice.cli.Settings.from_env", return_value=self.settings), \
                patch("oneaxe_voice.cli.httpx.Client") as client, \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["device", "issue", "test", "--token-file", str(target)]), 2)
            client.assert_not_called()
            self.assertEqual(target.read_text(), "preserve existing value")
            target.unlink()
            response = client.return_value.__enter__.return_value.post.return_value
            response.is_error = True
            response.status_code = 503
            response.json.return_value = {"detail": "synthetic unavailable"}
            self.assertEqual(main(["device", "issue", "test", "--token-file", str(target)]), 2)
            self.assertFalse(target.exists())


class V1TransportTests(unittest.IsolatedAsyncioTestCase):
    def stream(self, engine=None, worker_call=None, mobile=False):
        ws = SimpleNamespace(scope={"voice.principal": SimpleNamespace(credential_id="test-device")})
        credentials = SimpleNamespace(is_active=lambda credential_id: True)
        return V1Stream(ws, engine, credentials, worker_call,
                        StreamLimits(output_queue_size=4), mobile)

    async def test_unsent_snapshots_coalesce_without_losing_flow_or_terminal(self):
        stream = self.stream()
        stream.emit({"type": "ready", "audio_processed_samples": 0})
        for processed in range(1, 101):
            for kind in ("flow", "partial", "keepalive"):
                stream.emit({"type": kind, "audio_processed_samples": processed})
        self.assertEqual(stream.output.qsize(), 4)
        stream.emit({"type": "final", "audio_processed_samples": 100}, 1000)
        stream.emit({"type": "keepalive", "audio_processed_samples": 101})
        queued = [stream.output.get_nowait() for _ in range(stream.output.qsize())]
        self.assertEqual([event["type"] for event, _ in queued],
                         ["ready", "flow", "partial", "final"])
        self.assertEqual(queued[1][0]["audio_processed_samples"], 100)
        self.assertEqual(queued[-1][1], 1000)
        self.assertEqual([event["audio_processed_samples"] for event, _ in queued], [0, 100, 100, 100])

    async def test_cancel_during_start_releases_late_session_for_mobile_and_pc(self):
        for mobile in (True, False):
            with self.subTest(mobile=mobile):
                entered, release = asyncio.Event(), asyncio.Event()
                engine = StubSessions()

                async def worker_call(method, *args):
                    if method.__name__ in {"begin", "begin_mobile"}:
                        entered.set()
                        await release.wait()
                    return method(*args)

                stream = self.stream(engine, worker_call, mobile)
                hello = {"type": "start", "protocol_version": 1, "audio": AUDIO_FORMAT}
                if mobile:
                    hello.update(expected_server_instance_id=engine.instance,
                                 expected_model_generation=engine.generation)
                else:
                    hello["mode"] = "r2t2"

                async def accept():
                    pass

                async def receive_json():
                    return hello

                stream.ws.accept = stream.ws.close = accept
                stream.ws.receive_json = receive_json
                task = asyncio.create_task(stream.run())
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                await asyncio.wait_for(task, 1)
                self.assertFalse(stream.bound)
                self.assertEqual(engine.sessions[stream.session]["state"], "ended")
                self.assertEqual(engine.sessions[stream.session]["terminal_reason"], "cancelled")
                self.assertEqual(engine.status()["mobile_slots_available"], 1)

    async def test_websocket_denial_falls_back_to_close_without_extension(self):
        access = LocalAccess(None, "p" * 43, 1024, None)
        messages = []

        async def send(message):
            messages.append(message)

        await access.reject({"type": "websocket", "path": "/api/mobile/v1/dictation/stream"},
                            None, send, "UNAUTHORIZED", "synthetic rejected", 401)
        self.assertEqual(messages, [{"type": "websocket.close", "code": 1008}])


if __name__ == "__main__":
    unittest.main()
