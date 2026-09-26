"""Regression checks for the local API, bounded audio, and worker isolation."""

from contextlib import nullcontext
from dataclasses import replace
import io
import struct
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient

from oneaxe_voice.audio import AudioError, normalize_wav, validate_wav
from oneaxe_voice.backend import BusyError, GPUError, QwenEngine
from oneaxe_voice.cli import initialize, local_url
from oneaxe_voice.config import Settings
from oneaxe_voice.server import create_app


def wav_bytes(seconds=0.2, rate=16000, channels=1, silent=False):
    """Create deterministic PCM fixtures without any model or microphone."""
    target = io.BytesIO()
    with wave.open(target, "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        sample = b"\0\0" if silent else struct.pack("<h", 1200)
        recording.writeframes(sample * int(seconds * rate) * channels)
    return target.getvalue()


class AudioTests(unittest.TestCase):
    """Check content validation rather than trusting filenames or WAV headers."""

    def test_pcm_and_silence(self):
        """Identify exact digital silence and accept supported stereo audio."""
        self.assertTrue(validate_wav(wav_bytes(silent=True), Settings()).silent)
        info = validate_wav(wav_bytes(rate=48000, channels=2), Settings())
        self.assertEqual((info.sample_rate, info.channels), (48000, 2))
        self.assertFalse(info.silent)

    def test_reject_bad_truncated_short_long_and_channels(self):
        """Reject audio that cannot be safely interpreted within the API limits."""
        settings = Settings()
        cases = [
            b"not a wav", wav_bytes()[:-10], wav_bytes(seconds=0.01),
            wav_bytes(seconds=60.1), wav_bytes(channels=3),
        ]
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(AudioError):
                validate_wav(data, settings)

    def test_byte_limit(self):
        """Apply the upload limit even when the WAV duration looks acceptable."""
        with self.assertRaises(AudioError):
            validate_wav(wav_bytes(), replace(Settings(), max_audio_bytes=40))

    def test_real_normalization(self):
        """Verify the ffmpeg boundary produces the expected mono sample rate."""
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "in.wav", Path(temp) / "out.wav"
            source.write_bytes(wav_bytes(rate=48000, channels=2))
            normalize_wav(source, target)
            info = validate_wav(target.read_bytes(), Settings())
            self.assertEqual((info.sample_rate, info.channels), (16000, 1))
            self.assertAlmostEqual(info.seconds, 0.2, places=3)


class StubEngine:
    """Provide predictable HTTP behavior without loading a GPU model."""

    def __init__(self):
        """Track calls and allow explicit error injection."""
        self.calls = 0
        self.error = None

    def transcribe(self, data, request_id):
        """Validate audio and return a recognizable API fixture."""
        self.calls += 1
        validate_wav(data, Settings())
        if self.error:
            raise self.error
        return {
            "request_id": request_id, "text": "语音接口测试",
            "audio_seconds": 0.2, "device": "cuda:0", "timing_ms": {"total": 1},
        }

    def status(self):
        """Report that this test double owns no real model."""
        return {"model_loaded": False, "busy": False}

    def unload_if_idle(self, force=False):
        """Match the lifecycle interface without changing global state."""
        return False


class APITests(unittest.TestCase):
    """Exercise authentication and limits through the actual ASGI parser."""

    def setUp(self):
        """Build each application with its own temporary credential."""
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), runtime_dir=Path(self.temp.name))
        self.settings.token_path.write_text("a" * 43)
        self.engine = StubEngine()
        self.app = create_app(self.settings, self.engine)
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))
        self.headers = {"Authorization": "Bearer " + "a" * 43}

    def tearDown(self):
        """Close test resources without leaving client sessions behind."""
        self.client.close()
        self.temp.cleanup()

    def upload(self, data, headers=None):
        """Send an actual multipart recording through the API."""
        return self.client.post(
            "/api/dictation/transcribe", headers=headers or self.headers,
            files={"file": ("recording.wav", data, "audio/wav")},
        )

    def test_health_does_not_claim_loaded_model(self):
        """Keep HTTP liveness distinct from GPU readiness."""
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.client.get("/api/dictation/status").status_code, 401)

    def test_auth_required_before_transcription(self):
        """An unauthorized request cannot enter the model worker."""
        response = self.upload(wav_bytes(), {"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.engine.calls, 0)

    def test_remote_peer_denied_even_with_token(self):
        """Avoid exposing a localhost credential through an accidental LAN bind."""
        with TestClient(self.app, client=("192.168.1.2", 50000)) as remote:
            result = remote.get("/api/dictation/status", headers=self.headers)
        self.assertEqual(result.status_code, 403)

    def test_valid_transcription(self):
        """Pass the audio through the route and preserve Unicode output."""
        response = self.upload(wav_bytes())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "语音接口测试")
        self.assertEqual(self.engine.calls, 1)

    def test_invalid_audio(self):
        """Report an input error without disguising it as a server failure."""
        self.assertEqual(self.upload(b"invalid").status_code, 422)

    def test_large_sized_body_rejected_before_backend(self):
        """Reject an oversized announced request before multipart decoding."""
        response = self.client.post(
            "/api/dictation/transcribe", content=b"",
            headers={**self.headers, "Content-Length": str(self.settings.max_body_bytes + 1)},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.engine.calls, 0)

    def test_streamed_body_limit(self):
        """Enforce the limit when the caller omits Content-Length."""
        small = replace(self.settings, max_audio_bytes=100)
        app = create_app(small, self.engine)
        boundary = "voice-test"
        prefix = (
            "--voice-test\r\nContent-Disposition: form-data; name=\"file\"; "
            "filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
        ).encode()
        chunks = iter([prefix, b"x" * 70000, b"\r\n--voice-test--\r\n"])
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            response = client.post(
                "/api/dictation/transcribe", content=chunks,
                headers={**self.headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.engine.calls, 0)

    def test_busy_and_gpu_errors(self):
        """Make resource constraints actionable through distinct HTTP codes."""
        self.engine.error = BusyError("busy")
        response = self.upload(wav_bytes())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "2")
        self.engine.error = GPUError("cuda unavailable")
        self.assertEqual(self.upload(wav_bytes()).status_code, 503)


class WorkerTests(unittest.TestCase):
    """Prove exclusion and unload behavior while inference is in flight."""

    def test_cuda_required(self):
        """Fail explicitly when CUDA is absent, without loading a CPU model."""
        engine = QwenEngine(Settings())
        fake = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
        with patch.dict("sys.modules", {"torch": fake}), self.assertRaises(GPUError):
            engine._load()
        self.assertIsNone(engine._model)

    def test_busy_and_unload_guard(self):
        """A concurrent request or idle unload cannot enter an active worker."""
        entered, finish = threading.Event(), threading.Event()
        errors = []
        results = []
        workspace_clears = []

        def infer(**kwargs):
            """Hold an artificial inference until the assertion thread releases it."""
            entered.set()
            if not finish.wait(5):
                raise TimeoutError
            return [SimpleNamespace(text="测试完成")]

        cuda = SimpleNamespace(
            synchronize=lambda *args: None,
            reset_peak_memory_stats=lambda *args: None,
            max_memory_allocated=lambda *args: 1024,
            memory_allocated=lambda *args: 0,
            memory_reserved=lambda *args: 0,
            get_device_name=lambda *args: "test GPU",
            is_initialized=lambda: True,
            empty_cache=lambda: None,
            OutOfMemoryError=MemoryError,
        )
        with tempfile.TemporaryDirectory() as temp:
            settings = replace(Settings(), runtime_dir=Path(temp))
            engine = QwenEngine(settings)
            engine._model = SimpleNamespace(transcribe=infer)
            engine._torch = SimpleNamespace(
                cuda=cuda, inference_mode=nullcontext,
                _C=SimpleNamespace(
                    _cuda_clearCublasWorkspaces=lambda: workspace_clears.append(True),
                ),
            )

            def request():
                """Capture worker exceptions so thread failures fail the test."""
                try:
                    results.append(engine.transcribe(wav_bytes(), "test"))
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=request)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(BusyError):
                    engine.transcribe(wav_bytes(), "second")
                self.assertFalse(engine.unload_if_idle(force=True))
                self.assertFalse(workspace_clears)
            finally:
                finish.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(results[0]["text"], "测试完成")
            self.assertTrue(engine.unload_if_idle(force=True))
            self.assertIsNone(engine._model)
            self.assertEqual(workspace_clears, [True])
            self.assertEqual(engine.status()["reserved_mib"], 0)

    def test_invalid_audio_releases_busy_state(self):
        """Allow a subsequent request after malformed input fails validation."""
        engine = QwenEngine(Settings())
        with self.assertRaises(AudioError):
            engine.transcribe(b"bad", "bad")
        self.assertFalse(engine.status()["busy"])
        result = engine.transcribe(wav_bytes(silent=True), "silent")
        self.assertEqual(result["text"], "")
        self.assertFalse(result["inference_performed"])


class ClientTests(unittest.TestCase):
    """Keep credentials local and stable across repeated initialization."""

    def test_local_destinations_only(self):
        """Reject hosts, userinfo, and paths that could redirect client secrets."""
        self.assertEqual(local_url("http://127.0.0.1:8097/"), "http://127.0.0.1:8097")
        for value in ["https://example.com", "http://example.com", "http://x@localhost", "http://localhost/a"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_url(value)

    def test_token_is_private_and_not_replaced(self):
        """Repeated setup must preserve the credential already used by the service."""
        with tempfile.TemporaryDirectory() as temp:
            settings = replace(Settings(), runtime_dir=Path(temp))
            initialize(settings)
            original = settings.token_path.read_bytes()
            initialize(settings)
            self.assertEqual(original, settings.token_path.read_bytes())
            self.assertEqual(settings.token_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
