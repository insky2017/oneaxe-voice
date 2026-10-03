"""CPU golden checks against installed qwen-asr 0.0.6 streaming methods."""

import ast
import asyncio
from collections import deque
from dataclasses import dataclass
import importlib.util
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Optional, Tuple
import unittest
from unittest.mock import patch

import numpy as np

from oneaxe_voice.concurrent_worker import engine_configuration, warm_model
from oneaxe_voice.qwen_async import AsyncQwenAdapter, AsyncQwenDecoder
from oneaxe_voice.r2t2_async import SessionCancelled
from oneaxe_voice.stream_worker import StreamDecoder
from tests.test_r2t2_async import FakeEngine, speech


def cpu_qwen(outputs=(), tokenizer=None):
    package = importlib.util.find_spec("qwen_asr")
    source = Path(package.origin).parent / "inference"
    official = ast.parse((source / "qwen3_asr.py").read_text())
    utils = ast.parse((source / "utils.py").read_text())
    selected = []
    names = {"init_streaming_state", "streaming_transcribe", "finish_streaming_transcribe"}
    for node in official.body:
        if isinstance(node, ast.ClassDef) and node.name == "ASRStreamingState":
            selected.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "Qwen3ASRModel":
            selected.extend(method for method in node.body
                            if isinstance(method, ast.FunctionDef) and method.name in names)
    selected.extend(node for node in utils.body if isinstance(node, ast.FunctionDef)
                    and node.name in {"detect_and_fix_repetitions", "parse_asr_output"})
    namespace = dict(globals(), SAMPLE_RATE=16000, _ASR_TEXT_TAG="<asr_text>", _LANG_PREFIX="language ",
                     normalize_language_name=lambda value: value, validate_language=lambda value: None)
    exec(compile(ast.Module(body=selected, type_ignores=[]), "<official-qwen-cpu>", "exec"), namespace)
    queue, calls = deque(outputs), []

    def generate(inputs, sampling_params=None, use_tqdm=False):
        calls.append((inputs, sampling_params))
        text = queue.popleft() if queue else chr(0x4E00 + len(calls) * 2) + chr(0x4E01 + len(calls) * 2)
        return [SimpleNamespace(outputs=[SimpleNamespace(text=text)])]

    model = SimpleNamespace(backend="vllm", processor=SimpleNamespace(
        tokenizer=tokenizer or SimpleNamespace(encode=list, decode="".join)),
        sampling_params=SimpleNamespace(temperature=0.0, max_tokens=256),
        model=SimpleNamespace(generate=generate),
        _build_text_prompt=lambda context, force_language: "prompt:" + context)
    for name in names:
        setattr(model, name, MethodType(namespace[name], model))
    return model, SimpleNamespace(parse_output=namespace["parse_asr_output"]), calls


def qwen_adapter(engine=None, tokenizer=None):
    official, helpers, _ = cpu_qwen(tokenizer=tokenizer)
    return AsyncQwenAdapter(engine or FakeEngine(), official, helpers,
                            lambda count: SimpleNamespace(max_tokens=count, output_kind="FINAL_ONLY"))


class AsyncQwenAlgorithmTests(unittest.IsolatedAsyncioTestCase):
    def assert_states_equal(self, left, right):
        for key, value in vars(left).items():
            if isinstance(value, np.ndarray):
                np.testing.assert_array_equal(value, getattr(right, key), err_msg=key)
            else:
                self.assertEqual(value, getattr(right, key), key)

    async def test_official_normal_and_final_prefixes_and_state_match(self):
        outputs = ["first hypothesis", "second hypothesis", " tail", " end", " done"]
        official, helpers, calls = cpu_qwen(outputs)
        engine = FakeEngine(outputs)
        model = AsyncQwenAdapter(engine, official, helpers, lambda count: count)
        left = official.init_streaming_state(language="Chinese")
        right = model.init_streaming_state(language="Chinese")
        for index in range(4):
            pcm = np.ones(32000, dtype=np.int16) if index % 2 else np.ones(32000, dtype=np.float32)
            official.streaming_transcribe(pcm, left)
            step = model.prepare_normal(pcm, right)
            generated, _ = await model.generate(step, f"test:session:0:{index}")
            model.apply_normal(right, step, generated)
            self.assert_states_equal(left, right)
            self.assertEqual(calls[-1][0][0]["prompt"], engine.calls[-1][0]["prompt"])
            np.testing.assert_array_equal(calls[-1][0][0]["multi_modal_data"]["audio"][0], step.audio)
            self.assertEqual(step.max_tokens, 256)
        for state in (left, right):
            state.buffer = np.ones(731, dtype=np.float32)
        official.finish_streaming_transcribe(left)
        step = model.prepare_final(right)
        generated, _ = await model.generate(step, "test:session:0:final")
        model.apply_final(right, step, generated)
        self.assert_states_equal(left, right)
        self.assertEqual(calls[-1][0][0]["prompt"], step.prompt)
        self.assertEqual(step.max_tokens, 256)

    async def test_official_normal_unicode_rollback_and_final_minimum_token_match(self):
        tokenizer = SimpleNamespace(encode=list, decode=lambda ids: "\ufffd" if len(ids) == 3 else "".join(ids))
        official, helpers, calls = cpu_qwen(["normal", "final"], tokenizer)
        model = AsyncQwenAdapter(FakeEngine(["normal", "final"]), official, helpers, lambda count: count)
        left, right = (official.init_streaming_state(language="Chinese") for _ in range(2))
        for state in (left, right):
            state.chunk_id, state._raw_decoded = 2, "abcdefgh"
        official.streaming_transcribe(np.ones(32000, dtype=np.int16), left)
        step = model.prepare_normal(np.ones(32000, dtype=np.int16), right)
        self.assertEqual(step.prefix, "ab")
        model.apply_normal(right, step, "normal")
        self.assert_states_equal(left, right)
        for state in (left, right):
            state._raw_decoded = "a"
            state.buffer = np.ones(1, dtype=np.float32)
        official.finish_streaming_transcribe(left)
        step = model.prepare_final(right)
        self.assertEqual(step.prefix, "a")
        model.apply_final(right, step, "final")
        self.assert_states_equal(left, right)
        self.assertEqual(calls[-1][0][0]["prompt"], step.prompt)

    async def test_decoder_pcm_splits_and_flush_match_original_semantics(self):
        outputs = ["Hello", "world", ".", "next", "done"]
        sync_model, _, _ = cpu_qwen(outputs)
        engine = FakeEngine(outputs)
        old, new = StreamDecoder(sync_model, "qwen-stream"), AsyncQwenDecoder(qwen_adapter(engine), "session")
        for chunk in (speech(71), speech(31929), speech(32000), speech(731)):
            expected, actual = old.feed(chunk), await new.feed(chunk)
            self.assertEqual(expected, {key: actual[key] for key in expected})
        expected, actual = old.flush(), await new.flush()
        self.assertEqual(expected, {key: actual[key] for key in expected})
        self.assertEqual(actual["audio_processed_samples"], 64731)
        self.assertEqual((await new.flush())["audio_processed_samples"], 64731)
        self.assertEqual((await new.finish())["audio_processed_samples"], 64731)
        for chunk in (speech(17), speech(31983)):
            expected, actual = old.feed(chunk), await new.feed(chunk)
            self.assertEqual(expected, {key: actual[key] for key in expected})
        expected, actual = old.finish(), await new.finish()
        self.assertEqual(expected, {key: actual[key] for key in expected})
        self.assertEqual(actual["audio_processed_samples"], 96731)
        self.assertTrue(all(call[1].max_tokens == 256 for call in engine.calls))
        self.assertEqual(new.state.chunk_size_samples, 32000)

    async def test_decoder_window_rotation_matches_original_without_counting_padding(self):
        outputs = []
        for window, chunks in enumerate((15, 15, 1)):
            previous = ""
            for chunk in range(chunks):
                text = (previous or f"Acoustic window {window}") + f" phrase {chunk:02}"
                prefix = previous[:-5] if chunk >= 2 else ""
                outputs.append(text[len(prefix):])
                previous = text
            prefix = previous[:-5] if chunks >= 2 else ""
            outputs.append(previous[len(prefix):] + ".")
        official, _, _ = cpu_qwen(outputs)
        engine = FakeEngine(outputs)
        old, new = StreamDecoder(official, "qwen-stream"), AsyncQwenDecoder(qwen_adapter(engine), "session")
        committed = ""
        for index in range(31):
            expected, actual = old.feed(speech(32000)), await new.feed(speech(32000))
            self.assertEqual(expected, {key: actual[key] for key in expected})
            self.assertEqual(actual["audio_processed_samples"], (index + 1) * 32000)
            self.assertTrue(actual["text"].startswith(committed))
            committed = actual["text"]
        expected, actual = old.flush(), await new.flush()
        self.assertEqual(expected, {key: actual[key] for key in expected})
        self.assertEqual(actual["audio_processed_samples"], 31 * 32000)
        self.assertEqual(actual["window_seconds"], 30)
        self.assertEqual(sum(len(call[0]["multi_modal_data"]["audio"][0]) == 481280
                             for call in engine.calls), 2)
        self.assertTrue(actual["text"].startswith(committed))
        self.assertGreater(len(actual["text"]), 300)

    async def test_digital_silence_counts_and_keeps_flush_session_total(self):
        engine = FakeEngine()
        decoder = AsyncQwenDecoder(qwen_adapter(engine), "session")
        for count in (32000, 777):
            await decoder.feed(np.zeros(count, dtype="<i2").tobytes())
        self.assertEqual((await decoder.flush())["audio_processed_samples"], 32777)
        self.assertEqual(engine.calls, [])
        await decoder.feed(speech(19))
        final = await decoder.flush()
        self.assertEqual(final["audio_processed_samples"], 32796)
        self.assertEqual(len(engine.calls[0][0]["multi_modal_data"]["audio"][0]), 1299)
        await decoder.feed(np.zeros(32000, dtype="<i2").tobytes())
        self.assertEqual((await decoder.finish())["audio_processed_samples"], 64796)
        self.assertEqual(len(engine.calls), 1)

    async def test_cancelled_generation_cannot_commit_late_output(self):
        gate = asyncio.Event()
        engine = FakeEngine(["late output"], {"session": gate})
        decoder = AsyncQwenDecoder(qwen_adapter(engine), "session")
        task = asyncio.create_task(decoder.feed(speech(32000)))
        await engine.entered.wait()
        await decoder.cancel()
        gate.set()
        with self.assertRaises(SessionCancelled):
            await task
        self.assertEqual(decoder.committed, "")
        self.assertEqual(decoder.audio_processed_samples, 0)
        self.assertEqual(len(engine.aborted), 1)
        self.assertEqual(engine.shutdown_count, 0)

    async def test_committed_prefix_keeps_conservative_horizon_and_rejects_revision(self):
        engine = FakeEngine(["ABCDE", "ABCDEFG", "CDEFGHIJK", "incorrect"])
        decoder = AsyncQwenDecoder(qwen_adapter(engine), "session")
        with patch.object(decoder, "_fixed_qwen", side_effect=["", "ABCDE", "ABC", "wrong"]):
            first = await decoder.feed(speech(32000))
            self.assertEqual((first["text"], first["pending"]), ("", "ABCDE"))
            second = await decoder.feed(speech(32000))
            self.assertEqual((second["text"], second["pending"]), ("ABCDE", "FG"))
            stable = await decoder.feed(speech(32000))
            self.assertEqual(stable["text"], "ABCDE")
            with self.assertRaises(ValueError):
                await decoder.feed(speech(32000))
            self.assertEqual(decoder.committed, "ABCDE")

    async def test_warmup_retains_qwen_decode_budget_and_covers_two_full_windows(self):
        engine = FakeEngine()
        await warm_model(qwen_adapter(engine), "qwen-stream")
        self.assertEqual([len(call[0]["multi_modal_data"]["audio"][0]) for call in engine.calls],
                         [32000, 32000, 32000, 480000, 480000])
        self.assertEqual([call[1].max_tokens for call in engine.calls], [256] * 5)
        with patch.dict("os.environ", {}, clear=True):
            config = engine_configuration("model", "qwen-stream")
            self.assertEqual(config["kv_cache_memory_bytes"], 512 * 1024**2)
            self.assertEqual(config["max_num_seqs"], 2)
            self.assertEqual(config["compilation_config"]["cudagraph_capture_sizes"], [1, 2])


if __name__ == "__main__":
    unittest.main()
