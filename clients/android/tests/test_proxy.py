import http.server
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from tools import voice_lab_proxy


class ProxyBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), voice_lab_proxy.handler_type("lab-only-secret")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, token):
        request = urllib.request.Request(
            self.url + "/api/dictation/transcribe",
            data=b"test",
            headers={"Authorization": "Bearer " + token, "Content-Type": "multipart/form-data"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(request)
        return result.exception.code

    def test_wrong_token_never_reaches_desktop_or_voice(self):
        with patch.object(voice_lab_proxy, "desktop_idle") as desktop:
            self.assertEqual(self.post("wrong"), 401)
            desktop.assert_not_called()

    def test_active_desktop_blocks_transcription(self):
        with patch.object(voice_lab_proxy, "desktop_idle", return_value=False):
            self.assertEqual(self.post("lab-only-secret"), 409)

    def test_unavailable_desktop_status_fails_closed(self):
        with patch.object(voice_lab_proxy, "desktop_idle", side_effect=RuntimeError()):
            self.assertEqual(self.post("lab-only-secret"), 503)


if __name__ == "__main__":
    unittest.main()
