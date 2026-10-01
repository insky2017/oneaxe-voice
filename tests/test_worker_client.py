"""CPU socket tests for interleaved worker requests and failure isolation."""

from concurrent.futures import ThreadPoolExecutor
import json
import socket
import unittest

from oneaxe_voice.worker_client import WorkerClient, WorkerRPCError


class WorkerClientTests(unittest.TestCase):
    def setUp(self):
        parent, self.peer = socket.socketpair()
        self.peer.settimeout(3)
        self.stream = self.peer.makefile("rb")
        self.client = WorkerClient(parent, timeout=3)
        self.addCleanup(self.peer.close)
        self.addCleanup(self.stream.close)
        self.addCleanup(self.client.close)
        self.send({"ready": True, "device": "cuda:0", "mode": "r2t2", "capacity": 2})
        self.assertEqual(self.client.wait_ready()["capacity"], 2)

    def send(self, value):
        self.peer.sendall((json.dumps(value) + "\n").encode())

    def receive(self):
        return json.loads(self.stream.readline())

    def test_out_of_order_responses_are_delivered_to_the_correct_caller(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = {name: pool.submit(self.client.call, "audio", session=name, pcm=name)
                     for name in ("pc", "mobile")}
            requests = [self.receive(), self.receive()]
            self.assertEqual({item["session"] for item in requests}, {"pc", "mobile"})
            for request in reversed(requests):
                self.send({"rpc_id": request["rpc_id"], "session": request["session"],
                           "ok": True, "text": request["session"]})
            for name, call in calls.items():
                self.assertEqual(call.result(timeout=3)["text"], name)

    def test_one_session_error_does_not_fail_the_other_request(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = {name: pool.submit(self.client.call, "audio", session=name)
                     for name in ("pc", "mobile")}
            requests = {item["session"]: item for item in (self.receive(), self.receive())}
            self.send({"rpc_id": requests["mobile"]["rpc_id"], "session": "mobile", "ok": False,
                       "error": "ValueError", "code": "INVALID_MESSAGE", "message": "invalid", "fatal": False})
            self.send({"rpc_id": requests["pc"]["rpc_id"], "session": "pc", "ok": True, "text": "pc"})
            with self.assertRaises(WorkerRPCError) as raised:
                calls["mobile"].result(timeout=3)
            self.assertEqual(raised.exception.code, "INVALID_MESSAGE")
            self.assertFalse(raised.exception.fatal)
            self.assertEqual(calls["pc"].result(timeout=3)["text"], "pc")

    def test_global_failure_releases_all_pending_and_rejects_new_calls(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.client.call, "audio", session=name) for name in ("pc", "mobile")]
            request, _ = self.receive(), self.receive()
            self.send({"rpc_id": request["rpc_id"], "session": request["session"], "ok": False, "error": "RuntimeError",
                       "code": "SERVICE_UNAVAILABLE", "message": "failed", "fatal": True})
            for call in calls:
                with self.assertRaises(WorkerRPCError) as raised:
                    call.result(timeout=3)
                self.assertTrue(raised.exception.fatal)
        with self.assertRaises(WorkerRPCError):
            self.client.call("start", session="new")

    def test_eof_and_explicit_close_wake_waiting_callers(self):
        for explicit in (False, True):
            if explicit:
                self.setUp()
            with ThreadPoolExecutor(max_workers=1) as pool:
                call = pool.submit(self.client.call, "audio", session="pc")
                self.receive()
                if explicit:
                    self.client.close()
                else:
                    self.peer.shutdown(socket.SHUT_RDWR)
                with self.assertRaises(WorkerRPCError) as raised:
                    call.result(timeout=3)
                self.assertTrue(raised.exception.fatal)

    def test_response_with_a_different_session_fails_without_delivering_text(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(self.client.call, "audio", session="pc")
            request = self.receive()
            self.send({"rpc_id": request["rpc_id"], "session": "mobile", "ok": True, "text": "wrong"})
            with self.assertRaises(WorkerRPCError) as raised:
                call.result(timeout=3)
            self.assertTrue(raised.exception.fatal)


if __name__ == "__main__":
    unittest.main()
