"""Async steps derived from pinned R2T2 26d55a54, without changing vendor code.

The normal/final prepare and apply paths preserve r2t2_asr.py:628-875.
Only generation is delegated to a shared AsyncLLM. Imports here are CPU-only.
"""

import asyncio
from dataclasses import dataclass
import re
import time
import uuid

import numpy as np

from .stream_worker import StreamDecoder


class SessionCancelled(RuntimeError):
    """A result belongs to a session which can no longer commit."""


class FatalEngineError(RuntimeError):
    """The shared model engine can no longer serve any session."""


@dataclass(frozen=True)
class PreparedStep:
    kind: str
    prompt: str
    audio: np.ndarray
    prefix: str
    prefix_text: str
    max_tokens: int

    def input(self):
        return {"prompt": self.prompt, "multi_modal_data": {"audio": [self.audio]}}


class AsyncR2T2Adapter:
    def __init__(self, engine, official, helpers, sampling_factory, *, configuration=None, feature_device="cpu"):
        if feature_device not in {"cpu", "cuda:0"}:
            raise ValueError("Invalid experimental feature device")
        self.engine = engine
        self.official = official
        self.processor = official.processor
        self.helpers = helpers
        self.sampling_factory = sampling_factory
        self.configuration = dict(configuration or {})
        self.feature_device = feature_device

    def init_streaming_state(self, **kwargs):
        return self.official.init_streaming_state(**kwargs)

    @staticmethod
    def _append_window(state, audio):
        state.audio_accum = (audio if not len(state.audio_accum) else
                             np.concatenate([state.audio_accum, audio]))
        if len(state.audio_accum) > 16 * 16000:
            keep = len(state.audio_accum) - 8 * 16000
            if not state._first_chunk_discarded:
                discard_chunks = 49
                state._first_chunk_discarded = True
            else:
                discard_chunks = 50
            state.audio_accum = state.audio_accum[-keep:]
            state.chunk_text = state.chunk_text[discard_chunks:]

    @staticmethod
    def _prepared(state, kind, max_tokens):
        prefix_text = "".join(state.chunk_text)
        prefix = (f"language {state.language}<asr_text>" + prefix_text
                  if state.force_language is None and state.language else prefix_text)
        prefix = prefix.split("|")[0]
        return PreparedStep(kind, state.prompt_raw + prefix, state.audio_accum,
                            prefix, prefix_text, max_tokens)

    def prepare_normal(self, audio, state, *, max_tokens):
        x = np.asarray(audio).reshape(-1)
        x = x.astype(np.float32) / 32768 if x.dtype == np.int16 else x.astype(np.float32, copy=False)
        if len(x):
            state.buffer = np.concatenate([state.buffer, x])
        if len(state.buffer) < state.chunk_size_samples:
            return None
        chunk = state.buffer[:state.chunk_size_samples]
        state.buffer = state.buffer[state.chunk_size_samples:]
        self._append_window(state, chunk)
        return self._prepared(state, "normal", max_tokens)

    def prepare_final(self, state, *, max_tokens=64):
        if state.buffer is None or not len(state.buffer):
            return None
        tail = state.buffer
        state.buffer = np.zeros(0, dtype=np.float32)
        self._append_window(state, tail)
        return self._prepared(state, "final", max_tokens)

    def apply_normal(self, state, step, generated, *, rollback_punctuation=True):
        generated = self.helpers.normalize_punctuation(generated).replace("\ufffd", "")
        state._raw_decoded = step.prefix + generated
        lang = None
        if state.force_language is None:
            lang, _ = self.helpers.parse_language(state._raw_decoded, user_language=state.force_language)
        if state.force_language == "Chinese" or lang == "Chinese":
            state._raw_decoded = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", state._raw_decoded)
        lang, text = self.helpers.parse_output(state._raw_decoded, user_language=state.force_language)
        if "<asr_text>" in state._raw_decoded:
            state._raw_decoded = state._raw_decoded.split("<asr_text>")[0] + "<asr_text>" + text
        else:
            state._raw_decoded = text
        state._raw_decoded = state._raw_decoded.split("|")[0]
        ids = self.processor.tokenizer.encode(state._raw_decoded)
        raw_stripped = state._raw_decoded.strip()
        punctuated = bool(raw_stripped and raw_stripped[-1] in "\uFF0C\u3002\uFF01\uFF1F\u3001\uFF1B\uFF1A,.!?;:")
        k = 0 if rollback_punctuation and punctuated else int(state.unfixed_token_num)
        if "<asr_text>" in state._raw_decoded and not state._raw_decoded.split("<asr_text>")[1]:
            k = 0
        while True:
            end = max(0, len(ids) - k)
            fixed = self.processor.tokenizer.decode(ids[:end]) if end else ""
            if "\ufffd" not in fixed:
                break
            if not end:
                fixed = ""
                break
            k += 1
        if "<asr_text>" in fixed:
            _, fixed = fixed.split("<asr_text>", 1)
        if "<asr_text>" not in state._raw_decoded and state.force_language is None:
            state.text = ""
            return state.text, state.last_fixed_text
        state.language = lang
        state.text = text.split("|")[0]
        state.chunk_id += 1
        prefix, fixed = step.prefix_text.strip(), fixed.strip()
        delta = fixed[len(prefix):] if fixed.startswith(prefix) else ""
        delta = delta.split("|")[0]
        state.chunk_text.append(delta)
        state.last_fixed_text += delta
        return state.text, state.last_fixed_text

    def apply_final(self, state, step, generated):
        generated = self.helpers.normalize_punctuation(generated).replace("\ufffd", "")
        state._raw_decoded = (step.prefix + generated).split("|")[0]
        lang, text = self.helpers.parse_output(state._raw_decoded, user_language=state.force_language)
        text = text.split("|")[0]
        state.language, state.text = lang, text
        state.chunk_id += 1
        delta = text.strip()[len(step.prefix_text.strip()):]
        state.chunk_text.append(delta)
        state.last_fixed_text += delta
        return state.last_fixed_text

    def _fatal(self, exc):
        if getattr(self.engine, "errored", False) is True:
            return True
        seen = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            if type(exc).__name__ in {"EngineDeadError", "OutOfMemoryError"}:
                return True
            exc = exc.__cause__ or exc.__context__
        return False

    async def generate(self, step, request_id):
        output = None
        inputs = step.input()
        if self.feature_device != "cpu":
            # Qwen's inherited processor creates audio_kwargs before vLLM's
            # shallow config merge; the device must travel with each request.
            inputs["mm_processor_kwargs"] = {"audio_kwargs": {"device": self.feature_device}}
        try:
            async for value in self.engine.generate(inputs, self.sampling_factory(step.max_tokens), request_id):
                if value.finished:
                    output = value
        except Exception as exc:
            if self._fatal(exc):
                raise FatalEngineError("Shared inference engine failed") from exc
            raise
        if output is None or not output.outputs:
            raise RuntimeError("Inference returned no final output")
        completion = output.outputs[0]
        prompt_ids = getattr(output, "prompt_token_ids", None)
        token_ids = getattr(completion, "token_ids", None)
        counts = {"prompt_tokens": len(prompt_ids) if prompt_ids is not None else None,
                  "generated_tokens": len(token_ids) if token_ids is not None else None}
        return completion.text, counts

    async def abort(self, request_id):
        try:
            await self.engine.abort(request_id)
        except Exception as exc:
            if self._fatal(exc):
                raise FatalEngineError("Shared inference engine failed") from exc
            raise


class AsyncR2T2Decoder(StreamDecoder):
    def __init__(self, model, session, *, instance=None):
        super().__init__(model, "r2t2")
        self.session = session
        self.instance = instance or uuid.uuid4().hex
        self.utterance = 0
        self.step_id = 0
        self.inflight = None
        self.cancelled = False
        self.audio_processed_samples = 0
        self.reset_metrics()

    def reset_metrics(self, step_kind=None):
        """Reset RPC totals; phase times sum all steps plus buffer-only preparation."""
        self.step_metrics = []
        self.step_kind = step_kind
        self.timings = {"prepare_ms": 0.0, "generate_ms": 0.0, "apply_ms": 0.0}

    def _record_preparation(self, started):
        self.timings["prepare_ms"] += (time.monotonic() - started) * 1000

    def _record_apply(self, started):
        elapsed = (time.monotonic() - started) * 1000
        self.timings["apply_ms"] += elapsed
        self.step_metrics[-1]["apply_ms"] = round(elapsed, 3)

    def _check(self):
        if self.cancelled:
            raise SessionCancelled("Session cancelled")

    async def _generate(self, step, prepare_started):
        self._check()
        self.step_id += 1
        request_id = f"{self.instance}:{self.session}:{self.utterance}:{self.step_id}"
        submitted = time.monotonic()
        self.inflight = request_id
        try:
            text, counts = await self.model.generate(step, request_id)
            self._check()
            completed = time.monotonic()
            prepare_ms = (submitted - prepare_started) * 1000
            # This includes AsyncLLM scheduling and IPC; no CUDA synchronization is introduced.
            generate_ms = (completed - submitted) * 1000
            self.timings["prepare_ms"] += prepare_ms
            self.timings["generate_ms"] += generate_ms
            self.step_metrics.append({"step": self.step_id, "utterance": self.utterance,
                                      "kind": step.kind, "step_kind": self.step_kind,
                                      "prepared_at": prepare_started,
                                      "submitted_at": submitted, "completed_at": completed,
                                      "prepare_ms": round(prepare_ms, 3),
                                      "generate_ms": round(generate_ms, 3), "apply_ms": 0.0, **counts})
            return text
        finally:
            self.inflight = None

    async def feed(self, pcm):
        self._check()
        self.reset_metrics("audio")
        started = time.monotonic()
        if not pcm or len(pcm) > 64000 or len(pcm) % 2:
            raise ValueError("PCM16 chunk must contain 1 to 32000 samples")
        self.pending.extend(pcm)
        self._record_preparation(started)
        changed = False
        while True:
            started = time.monotonic()
            self._check()
            count = 5120 if self.first else 2560
            if len(self.pending) < count * 2:
                self._record_preparation(started)
                break
            audio = np.frombuffer(bytes(self.pending[:count * 2]), dtype="<i2")
            del self.pending[:count * 2]
            if self.first and not np.any(audio):
                self.audio_processed_samples += count
                self._record_preparation(started)
                continue
            step = self.model.prepare_normal(audio, self.state, max_tokens=4 if self.first else 2)
            generated = await self._generate(step, started)
            applied = time.monotonic()
            candidate, local_fixed = self.model.apply_normal(self.state, step, generated)
            fixed = self._global(local_fixed)
            local_pending = self._r2_pending(candidate, local_fixed)
            self.pending_text = self._global(local_fixed + local_pending)[len(fixed):]
            self.candidate = fixed + self.pending_text
            if not fixed.startswith(self.committed):
                raise ValueError("Model revised the committed prefix")
            changed |= fixed != self.committed
            self.committed = fixed
            self.first, self.has_audio = False, True
            self.state.chunk_size_sec, self.state.chunk_size_samples = .16, 2560
            self.audio_processed_samples += count
            self.max_window = max(self.max_window, len(self.state.audio_accum))
            self._record_apply(applied)
            await asyncio.sleep(0)
        return self.result(changed)

    async def _finalize(self, *, restart):
        self._check()
        self.reset_metrics("flush" if restart else "finish")
        started = time.monotonic()
        tail_samples = len(self.pending) // 2
        tail = np.frombuffer(bytes(self.pending), dtype="<i2").astype(np.float32) / 32768
        self.pending.clear()
        if not self.has_audio and not np.any(tail):
            self.audio_processed_samples += tail_samples
            self._record_preparation(started)
            return self.result()
        self.state.buffer = np.concatenate([self.state.buffer, tail, np.zeros(1280, dtype=np.float32)])
        step = self.model.prepare_final(self.state)
        generated = await self._generate(step, started)
        applied = time.monotonic()
        final = self._global(self.model.apply_final(self.state, step, generated))
        if not final.startswith(self.committed):
            raise ValueError("Model final output revised the committed prefix")
        changed = final != self.committed
        self.committed = self.candidate = final
        self.pending_text = ""
        self.has_audio = False
        self.audio_processed_samples += tail_samples
        if restart:
            self.base = final
            self.state = self._new_state()
            self.first, self.utterance_boundary = True, True
            self.utterance += 1
        self._record_apply(applied)
        return self.result(changed)

    async def flush(self):
        return await self._finalize(restart=True)

    async def finish(self):
        return await self._finalize(restart=False)

    async def cancel(self):
        self.cancelled = True
        self.pending.clear()
        self.pending_text = ""
        if self.inflight is not None:
            await self.model.abort(self.inflight)

    def result(self, changed=False):
        return {**super().result(changed), "audio_processed_samples": self.audio_processed_samples,
                "step_kind": self.step_kind, **{key: round(value, 3) for key, value in self.timings.items()},
                "step_metrics": [dict(metric) for metric in self.step_metrics]}
