#!/usr/bin/env python3
"""Disposable loopback bridge for ADB-reverse Voice Lab tests."""

import http.server
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.request


VOICE_ROOT = Path.home() / "tools" / "oneaxe-voice"
VOICE_URL = "http://127.0.0.1:8097"
PORT = 18097
MAX_BODY = 12 * 1024 * 1024 + 4096


def desktop_idle():
    result = subprocess.run(
        [str(VOICE_ROOT / "bin" / "oneaxe-voice"), "desktop-status"],
        cwd=VOICE_ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    status = json.loads(result.stdout)
    return (
        status.get("state") == "idle"
        and not status.get("capture_active")
        and not status.get("recognizing")
        and not status.get("preparing")
    )


def handler_type(lab_token):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not log headers, audio, transcripts or credentials.
            pass

        def reply(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/health":
                self.reply(404, {"detail": "unsupported"})
                return
            self.reply(200, {"status": "lab bridge ready"})

        def do_POST(self):
            if self.path != "/api/dictation/transcribe":
                self.reply(404, {"detail": "unsupported"})
                return
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + lab_token):
                self.reply(401, {"detail": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.reply(400, {"detail": "invalid length"})
                return
            if not 0 < length <= MAX_BODY:
                self.reply(413, {"detail": "invalid size"})
                return
            try:
                if not desktop_idle():
                    self.reply(409, {"detail": "desktop dictation is active"})
                    return
            except Exception:
                self.reply(503, {"detail": "desktop status unavailable"})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self.reply(400, {"detail": "incomplete body"})
                return
            try:
                request = urllib.request.Request(
                    VOICE_URL + self.path,
                    data=body,
                    headers={
                        "Authorization": "Bearer " + (VOICE_ROOT / "runtime" / "client.token").read_text().strip(),
                        "Content-Type": self.headers.get("Content-Type", ""),
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = response.read()
                    self.send_response(response.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(result)))
                    self.end_headers()
                    self.wfile.write(result)
            except urllib.error.HTTPError as error:
                self.reply(error.code, {"detail": "Voice rejected the request"})
            except Exception:
                self.reply(502, {"detail": "Voice unavailable"})

    return Handler


def main():
    with tempfile.TemporaryDirectory(prefix="voice-lab-", dir="/tmp") as runtime:
        os.chmod(runtime, 0o700)
        lab_token = secrets.token_urlsafe(32)
        pairing = Path(runtime) / "pairing.json"
        pairing.write_text(json.dumps({"url": f"http://127.0.0.1:{PORT}", "token": lab_token}))
        pairing.chmod(0o600)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler_type(lab_token))
        print(f"pairing file: {pairing}", flush=True)
        print(f"bridge: 127.0.0.1:{PORT}; Ctrl-C revokes lab token", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
