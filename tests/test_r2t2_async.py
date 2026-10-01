"""CPU differential checks against the unmodified pinned streaming algorithm."""

import ast
import asyncio
from collections import deque
from contextlib import redirect_stdout
from dataclasses import dataclass
import importlib.util
import io
from pathlib import Path
import re
from types import MethodType, SimpleNamespace
from typing import Any, List, Optional, Tuple, Union
import unittest

import numpy as np

from oneaxe_voice.r2t2_async import AsyncR2T2Adapter, AsyncR2T2Decoder, FatalEngineError, SessionCancelled
from oneaxe_voice.stream_worker import StreamDecoder


def cpu_official(outputs=()):
    root = Path(__file__).resolve().parents[1]
    vendor = ast.parse((root / "vendor/r2t2/r2t2_asr.py").read_text())
    package = importlib.util.find_spec("qwen_asr")
    utils = ast.parse((Path(package.origin).parent / "inference/utils.py").read_text())
    selected = []
    for node in vendor.body:
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id in {
                "_EN2ZH_PUNCT", "_ZH2EN_PUNCT", "_ALL_PUNCT_PAT"} for x in node.targets):
            selected.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in {"_normalize_punct_by_context", "parse_language_output"}:
            selected.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "ASRStreamingState":
            selected.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "R2T2ASRModel":
            selected.extend(x for x in node.body if isinstance(x, ast.FunctionDef) and x.name in {
                "init_streaming_state", "streaming_transcribe_no_reset", "finish_streaming_transcribe_no_reset"})
    selected.extend(x for x in utils.body if isinstance(x, ast.FunctionDef) and x.name in {
        "detect_and_fix_repetitions", "parse_asr_output"})
    namespace = dict(globals(), SAMPLE_RATE=16000, _ASR_TEXT_TAG="<asr_text>", _LANG_PREFIX="language ",
                     normalize_language_name=lambda value: value, validate_language=lambda value: None,
                     SamplingParams=lambda **values: SimpleNamespace(**values))

    class CpuImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node) if node.module == "vllm" else node

    module = CpuImports().visit(ast.Module(body=selected, type_ignores=[]))
    exec(compile(ast.fix_missing_locations(module), "<pinned-r2t2-cpu>", "exec"), namespace)
    tokens = SimpleNamespace(encode=list, decode="".join)
    queue = deque(outputs)
    calls = []

    def generate(inputs, sampling_params=None, use_tqdm=False):
        calls.append(inputs)
        text = queue.popleft() if queue else chr(0x4E00 + len(calls) * 2) + chr(0x4E01 + len(calls) * 2)
        return [SimpleNamespace(outputs=[SimpleNamespace(text=text)])]

    model = SimpleNamespace(backend="vllm", processor=SimpleNamespace(tokenizer=tokens),
                            sampling_params=None, model=SimpleNamespace(generate=generate),
                            _build_text_prompt=lambda context, force_language: "prompt:" + context)
    for name in ("init_streaming_state", "streaming_transcribe_no_reset", "finish_streaming_transcribe_no_reset"):
        setattr(model, name, MethodType(namespace[name], model))
    helpers = SimpleNamespace(normalize_punctuation=namespace["_normalize_punct_by_context"],
                              parse_language=namespace["parse_language_output"], parse_output=namespace["parse_asr_output"])
    return model, helpers, calls


class FakeEngine:
    def __init__(self, outputs=(), gates=None):
        self.outputs = deque(outputs)
        self.gates = gates or {}
        self.calls = []
        self.aborted = []
        self.errored = False
        self.entered = asyncio.Event()
        self.shutdown_count = 0

    async def generate(self, inp, params, request_id):
        index = len(self.calls) + 1
        self.calls.append((inp, params, request_id))
        text = self.outputs.popleft() if self.outputs else chr(0x4E00 + index * 2) + chr(0x4E01 + index * 2)
        self.entered.set()
        session = request_id.split(":")[1]
        if session in self.gates:
            await self.gates[session].wait()
        await asyncio.sleep(0)
        yield SimpleNamespace(finished=True, prompt_token_ids=[1, 2, 3],
                              outputs=[SimpleNamespace(text=text, token_ids=list(text))])

    async def abort(self, request_id):
        self.aborted.append(request_id)

    def shutdown(self):
        self.shutdown_count += 1


def adapter(engine=None):
    official, helpers, _ = cpu_official()
    return AsyncR2T2Adapter(engine or FakeEngine(), official, helpers,
                           lambda count: SimpleNamespace(max_tokens=count, output_kind="FINAL_ONLY"))


def speech(samples=5120):
    return np.ones(samples, dtype="<i2").tobytes()


class AsyncAlgorithmTests(unittest.IsolatedAsyncioTestCase):
    def assert_states_equal(self, left, right):
        for key, value in vars(left).items():
            if isinstance(value, np.ndarray):
                np.testing.assert_array_equal(value, getattr(right, key), err_msg=key)
            else:
                self.assertEqual(value, getattr(right, key), key)

    async def test_normal_and_final_match_pinned_algorithm_across_window_moves(self):
        official, helpers, _ = cpu_official()
        engine = FakeEngine()
        model = AsyncR2T2Adapter(engine, official, helpers, lambda count: count)
        left = official.init_streaming_state(language="Chinese", chunk_size_sec=.32, unfixed_token_num=1)
        right = model.init_streaming_state(language="Chinese", chunk_size_sec=.32, unfixed_token_num=1)
        for index in range(220):
            count = 5120 if not index else 2560
            pcm = np.ones(count, dtype=np.int16)
            expected = official.streaming_transcribe_no_reset(pcm, left, rollback_punctuation=True)
            step = model.prepare_normal(pcm, right, max_tokens=4 if not index else 2)
            text, _ = await model.generate(step, f"test:session:0:{index}")
            actual = model.apply_normal(right, step, text)
            self.assertEqual(expected, actual)
            self.assert_states_equal(left, right)
            for state in (left, right):
                state.chunk_size_sec, state.chunk_size_samples = .16, 2560
        for state in (left, right):
            state.buffer = np.zeros(1280, dtype=np.float32)
        with redirect_stdout(io.StringIO()):
            expected = official.finish_streaming_transcribe_no_reset(left)
        step = model.prepare_final(right)
        text, _ = await model.generate(step, "test:session:0:final")
        self.assertEqual(expected, model.apply_final(right, step, text))
        self.assert_states_equal(left, right)
        self.assertTrue(right._first_chunk_discarded)

    async def test_decoder_pcm_splits_flush_and_real_sample_count(self):
        outputs = ["Hello", "o", "world.", ""]
        sync_model, _, _ = cpu_official(outputs)
        async_model = adapter(FakeEngine(outputs))
        old, new = StreamDecoder(sync_model, "r2t2"), AsyncR2T2Decoder(async_model, "session")
        chunks = [speech(70), speech(5050), speech(123)]
        for chunk in chunks:
            expected, actual = old.feed(chunk), await new.feed(chunk)
            for key in expected:
                self.assertEqual(expected[key], actual[key], key)
        with redirect_stdout(io.StringIO()):
            expected = old.flush()
        actual = await new.flush()
        self.assertEqual(expected["text"], actual["text"])
        self.assertEqual(actual["audio_processed_samples"], 5243)
        self.assertEqual(new.state.chunk_size_samples, 5120)
        self.assertEqual((await new.flush())["text"], actual["text"])
        self.assertEqual((await new.finish())["text"], actual["text"])
        self.assertEqual(len(async_model.engine.calls), 2)
        self.assertEqual((await new.feed(speech()))["preview"], "Hello world.")

    async def test_punctuation_and_final_tail_keep_prefix_and_params(self):
        engine = FakeEngine(["speech,", "tail", "done"])
        decoder = AsyncR2T2Decoder(adapter(engine), "session")
        self.assertEqual((await decoder.feed(speech()))["pending"], "")
        await decoder.feed(speech(2560))
        result = await decoder.finish()
        self.assertEqual([call[1].max_tokens for call in engine.calls], [4, 2, 64])
        self.assertEqual(result["audio_processed_samples"], 7680)
        self.assertEqual(len(engine.calls[-1][0]["multi_modal_data"]["audio"][0]), 8960)
        self.assertEqual(result["step_metrics"][0]["prompt_tokens"], 3)
        self.assertEqual(result["step_metrics"][0]["generated_tokens"], 4)

    async def test_digital_silence_is_counted_without_inference(self):
        engine = FakeEngine()
        decoder = AsyncR2T2Decoder(adapter(engine), "session")
        await decoder.feed(np.zeros(6000, dtype="<i2").tobytes())
        result = await decoder.flush()
        self.assertEqual(result["audio_processed_samples"], 6000)
        self.assertEqual(engine.calls, [])
        self.assertEqual(result["text"], "")

    async def test_late_completion_after_cancel_cannot_commit(self):
        gate = asyncio.Event()
        engine = FakeEngine(["late output"], {"session": gate})
        decoder = AsyncR2T2Decoder(adapter(engine), "session")
        task = asyncio.create_task(decoder.feed(speech()))
        await engine.entered.wait()
        await decoder.cancel()
        gate.set()
        with self.assertRaises(SessionCancelled):
            await task
        self.assertEqual(decoder.committed, "")
        self.assertEqual(decoder.audio_processed_samples, 0)
        self.assertEqual(len(engine.aborted), 1)
        self.assertEqual(engine.shutdown_count, 0)

    async def test_first_window_moves_only_above_16_seconds(self):
        model = adapter()
        state = model.init_streaming_state(language="Chinese", chunk_size_sec=.16, unfixed_token_num=1)
        state.audio_accum = np.zeros(16 * 16000 - 2560, dtype=np.float32)
        state.chunk_text = [str(index) for index in range(100)]
        model.prepare_normal(np.ones(2560, dtype=np.int16), state, max_tokens=2)
        self.assertFalse(state._first_chunk_discarded)
        model.prepare_normal(np.ones(2560, dtype=np.int16), state, max_tokens=2)
        self.assertTrue(state._first_chunk_discarded)
        self.assertEqual(state.chunk_text[0], "49")

    async def test_recoverable_generation_error_does_not_mark_engine_fatal(self):
        recoverable = type("EngineGenerateError", (RuntimeError,), {})
        model = adapter()
        self.assertFalse(model._fatal(recoverable()))
        fatal = recoverable()
        fatal.__cause__ = type("OutOfMemoryError", (RuntimeError,), {})()
        self.assertTrue(model._fatal(fatal))
        model.engine.errored = True
        self.assertTrue(model._fatal(RuntimeError()))

    async def test_model_failure_classification_on_async_output(self):
        dead = type("EngineDeadError", (RuntimeError,), {})
        model = adapter()
        async def failed(*args):
            raise dead()
            yield
        model.engine.generate = failed
        state = model.init_streaming_state(language="Chinese", chunk_size_sec=.32, unfixed_token_num=1)
        step = model.prepare_normal(np.ones(5120, dtype=np.int16), state, max_tokens=4)
        with self.assertRaises(FatalEngineError):
            await model.generate(step, "test:session:0:1")


if __name__ == "__main__":
    unittest.main()
