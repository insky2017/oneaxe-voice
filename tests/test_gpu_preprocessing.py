"""CPU checks for the isolated GPU feature request path and warmup probe."""

import asyncio
import io
import json
import socket
from contextlib import redirect_stderr
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from oneaxe_voice.concurrent_worker import (
    FeatureDeviceProbe, engine_configuration, experimental_feature_configuration,
    load_model, run, warm_model,
)
from oneaxe_voice.r2t2_async import AsyncR2T2Adapter, AsyncR2T2Decoder, SessionCancelled
from tests.test_r2t2_async import FakeEngine, cpu_official, speech


def feature_adapter(device="cpu", engine=None):
    official, helpers, _ = cpu_official()
    return AsyncR2T2Adapter(engine or FakeEngine(), official, helpers,
                           lambda count: SimpleNamespace(max_tokens=count), feature_device=device)


class FeatureRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_flush_finish_and_warmup_use_same_gpu_kwargs(self):
        model = feature_adapter("cuda:0")
        decoder = AsyncR2T2Decoder(model, "session")
        await decoder.feed(speech())
        await decoder.feed(speech(2560))
        await decoder.flush()
        await decoder.feed(speech())
        await decoder.finish()
        await warm_model(model)
        self.assertEqual([call[1].max_tokens for call in model.engine.calls],
                         [4, 2, 64, 4, 64, 4, 4, 4, 2, 2])
        self.assertEqual([len(call[0]["multi_modal_data"]["audio"][0]) for call in model.engine.calls],
                         [5120, 7680, 8960, 5120, 6400, 5120, 5120, 5120, 256000, 256000])
        for inputs, _, _ in model.engine.calls:
            self.assertEqual(inputs["mm_processor_kwargs"], {"audio_kwargs": {"device": "cuda:0"}})
            self.assertNotIn("device", inputs)
        self.assertTrue(all(call[0]["multi_modal_data"]["audio"][0].dtype == np.float32
                            for call in model.engine.calls))

    async def test_default_cpu_does_not_change_inputs_or_state_algorithm(self):
        model = feature_adapter()
        decoder = AsyncR2T2Decoder(model, "session")
        await decoder.feed(speech())
        await decoder.finish()
        await warm_model(model)
        self.assertEqual(model.feature_device, "cpu")
        self.assertEqual(len(model.engine.calls), 7)
        for inputs, _, _ in model.engine.calls:
            self.assertEqual(set(inputs), {"prompt", "multi_modal_data"})

    async def test_kwargs_are_per_request_and_do_not_mutate_prepared_step(self):
        model = feature_adapter("cuda:0")
        state = model.init_streaming_state(language="Chinese", chunk_size_sec=.32, unfixed_token_num=1)
        step = model.prepare_normal(np.ones(5120, dtype=np.int16), state, max_tokens=4)
        await model.generate(step, "test:session:0:1")
        model.engine.calls[0][0]["mm_processor_kwargs"]["audio_kwargs"]["device"] = "cpu"
        await model.generate(step, "test:session:0:2")
        self.assertNotIn("mm_processor_kwargs", step.input())
        self.assertIs(model.engine.calls[1][0]["multi_modal_data"]["audio"][0], step.audio)
        self.assertEqual(model.engine.calls[1][0]["mm_processor_kwargs"],
                         {"audio_kwargs": {"device": "cuda:0"}})

    async def test_gpu_kwargs_preserve_cancel_and_late_result_guard(self):
        gate = asyncio.Event()
        engine = FakeEngine(["late output"], {"session": gate})
        decoder = AsyncR2T2Decoder(feature_adapter("cuda:0", engine), "session")
        task = asyncio.create_task(decoder.feed(speech()))
        await engine.entered.wait()
        await decoder.cancel()
        gate.set()
        with self.assertRaises(SessionCancelled):
            await task
        self.assertEqual(decoder.committed, "")
        self.assertEqual(len(engine.aborted), 1)
        self.assertEqual(engine.shutdown_count, 0)
        self.assertEqual(engine.calls[0][0]["mm_processor_kwargs"],
                         {"audio_kwargs": {"device": "cuda:0"}})

    async def test_invalid_environment_fails_before_backend_or_model_load(self):
        with patch.dict("os.environ", {"ONEAXE_VOICE_EXPERIMENT_FEATURE_DEVICE": "cuda:1"}, clear=True), \
                patch("oneaxe_voice.concurrent_worker.register_qwen_backend") as register:
            with self.assertRaises(ValueError):
                await load_model("unused")
            register.assert_not_called()

    async def test_warmup_logs_only_probe_counts_and_closes_probe_before_serving(self):
        parent, child = socket.socketpair()
        worker_fd = child.detach()
        model = feature_adapter("cuda:0")
        model.configuration = engine_configuration("unused")
        evidence = {"scope": "warmup", "expected_device": "cuda:0", "feature_calls": 5,
                    "argument_devices": {"cuda:0": 5}, "fft_tensor_devices": {"cuda:0": 5},
                    "mel_tensor_devices": {"cuda:0": 10}}
        probe = SimpleNamespace(snapshot=lambda: dict(evidence), closed=False)
        probe.close = lambda: setattr(probe, "closed", True)
        model.feature_probe = probe
        parent.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=parent, limit=200000)

        async def serve_after_probe_close(*args):
            self.assertTrue(probe.closed)

        logs = io.StringIO()
        try:
            with patch.dict("os.environ", {"ONEAXE_WORKER_FD": str(worker_fd)}), \
                    patch("oneaxe_voice.concurrent_worker.sys.argv", ["worker", "r2t2", "unused"]), \
                    patch("oneaxe_voice.concurrent_worker.load_model", AsyncMock(return_value=(model, 2))), \
                    patch("oneaxe_voice.concurrent_worker.serve", serve_after_probe_close), \
                    redirect_stderr(logs):
                await run()
            message = json.loads(await reader.readline())
            self.assertEqual(message["feature_device"], "cuda:0")
            self.assertEqual(message["feature_probe"], evidence)
            marker, data = logs.getvalue().strip().split(" ", 1)
            self.assertEqual(marker, "feature_device_probe")
            self.assertEqual(json.loads(data), evidence)
            self.assertTrue(probe.closed)
            self.assertEqual(model.engine.shutdown_count, 1)
        finally:
            writer.close()
            await writer.wait_closed()


class FeatureConfigurationTests(unittest.TestCase):
    def test_default_cpu_and_explicit_cuda_probe_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(experimental_feature_configuration(), ("cpu", False))
        with patch.dict("os.environ", {"ONEAXE_VOICE_EXPERIMENT_FEATURE_DEVICE": "cuda:0"}, clear=True):
            self.assertEqual(experimental_feature_configuration(), ("cuda:0", True))
            with patch.dict("os.environ", {"ONEAXE_VOICE_EXPERIMENT_FEATURE_PROBE": "0"}):
                self.assertEqual(experimental_feature_configuration(), ("cuda:0", False))
        with patch.dict("os.environ", {"ONEAXE_VOICE_EXPERIMENT_FEATURE_PROBE": "1"}, clear=True):
            self.assertEqual(experimental_feature_configuration(), ("cpu", True))

    def test_invalid_devices_and_probe_values_fail_closed(self):
        for device in ("", "cuda", "cuda:1", "CUDA:0", " cpu"):
            with self.subTest(device=device):
                with patch.dict("os.environ", {"ONEAXE_VOICE_EXPERIMENT_FEATURE_DEVICE": device}, clear=True):
                    with self.assertRaises(ValueError):
                        experimental_feature_configuration()
                with self.assertRaises(ValueError):
                    feature_adapter(device)
        with patch.dict("os.environ", {"ONEAXE_VOICE_EXPERIMENT_FEATURE_PROBE": "true"}, clear=True):
            with self.assertRaises(ValueError):
                experimental_feature_configuration()


class FeatureProbeTests(unittest.TestCase):
    def setUp(self):
        import torch
        from transformers import WhisperFeatureExtractor

        self.torch = torch
        self.extractor_class = WhisperFeatureExtractor
        self.extractor = WhisperFeatureExtractor(feature_size=128)
        self.audio = np.zeros((1, 5120), dtype=np.float32)
        self.original = WhisperFeatureExtractor._torch_extract_fbank_features
        self.original_stft = torch.stft

    def test_probe_observes_real_cpu_fft_and_mel_tensors_without_cuda_initialization(self):
        probe = FeatureDeviceProbe("cpu").install()
        try:
            with patch.object(self.torch.cuda, "_lazy_init", side_effect=AssertionError("CPU test used CUDA")):
                output = self.extractor._torch_extract_fbank_features(self.audio, "cpu")
                reference = self.original(self.extractor, self.audio, "cpu")
            np.testing.assert_array_equal(output, reference)
            evidence = probe.snapshot()
            self.assertEqual(evidence["feature_calls"], 1)
            self.assertEqual(evidence["argument_devices"], {"cpu": 1})
            self.assertGreater(evidence["fft_tensor_devices"]["cpu"], 0)
            self.assertGreater(evidence["mel_tensor_devices"]["cpu"], 0)
            self.assertEqual(output.shape, (1, 128, 32))
            self.assertIs(self.torch.stft, self.original_stft)
        finally:
            probe.close()
        self.assertIs(self.extractor_class._torch_extract_fbank_features, self.original)

    def test_probe_rejects_cpu_tensors_even_when_device_argument_claims_cuda(self):
        def ignores_device(extractor, waveform, device):
            return self.torch.stft(self.torch.from_numpy(waveform), extractor.n_fft, extractor.hop_length,
                                   window=self.torch.hann_window(extractor.n_fft), return_complex=True)

        with patch.object(self.extractor_class, "_torch_extract_fbank_features", ignores_device):
            probe = FeatureDeviceProbe("cuda:0").install()
            try:
                with patch.object(self.torch.cuda, "_lazy_init", side_effect=AssertionError("CPU test used CUDA")):
                    with self.assertRaisesRegex(RuntimeError, "tensor device mismatch"):
                        self.extractor._torch_extract_fbank_features(self.audio, "cuda:0")
                self.assertEqual(probe.snapshot()["argument_devices"], {"cuda:0": 1})
                self.assertEqual(probe.snapshot()["fft_tensor_devices"], {"cpu": 1})
            finally:
                probe.close()
            self.assertIs(self.extractor_class._torch_extract_fbank_features, ignores_device)
        self.assertIs(self.extractor_class._torch_extract_fbank_features, self.original)

    def test_probe_rejects_missing_tensor_evidence_and_restores_after_error(self):
        with patch.object(self.extractor_class, "_torch_extract_fbank_features", return_value=np.zeros(1)) as original:
            probe = FeatureDeviceProbe("cpu").install()
            try:
                with self.assertRaisesRegex(RuntimeError, "verification unavailable"):
                    self.extractor._torch_extract_fbank_features(self.audio, "cpu")
            finally:
                probe.close()
            self.assertIs(self.extractor_class._torch_extract_fbank_features, original)
        self.assertIs(self.extractor_class._torch_extract_fbank_features, self.original)
        self.assertIs(self.torch.stft, self.original_stft)


if __name__ == "__main__":
    unittest.main()
