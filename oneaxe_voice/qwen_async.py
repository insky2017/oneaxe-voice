"""Async Qwen steps preserving qwen-asr 0.0.6's streaming rollback rules.

Each decoder owns its official streaming state; only AsyncLLM is shared.
Imports remain CPU-only so protocol and algorithm checks need no GPU.
"""

import asyncio
import time
import uuid

import numpy as np

from .r2t2_async import AsyncR2T2Adapter, AsyncR2T2Decoder, PreparedStep
from .stream_worker import StreamDecoder


class AsyncQwenAdapter(AsyncR2T2Adapter):
    """Reuse async generation and abort while retaining Qwen's own algorithm."""

    def _prepared(self, state, kind, max_tokens):
        prefix = ""
        if state.chunk_id >= state.unfixed_chunk_num:
            ids = self.processor.tokenizer.encode(state._raw_decoded)
            if kind == "final":
                end = max(1, len(ids) - int(state.unfixed_token_num))
                prefix = self.processor.tokenizer.decode(ids[:end])
            else:
                rollback = int(state.unfixed_token_num)
                while True:
                    end = max(0, len(ids) - rollback)
                    prefix = self.processor.tokenizer.decode(ids[:end]) if end else ""
                    if "\ufffd" not in prefix or not end:
                        break
                    rollback += 1
        return PreparedStep(kind, state.prompt_raw + prefix, state.audio_accum,
                            prefix, prefix, max_tokens)

    @staticmethod
    def _append_audio(state, audio):
        state.audio_accum = (audio if not len(state.audio_accum) else
                             np.concatenate([state.audio_accum, audio]))

    def prepare_normal(self, audio, state, *, max_tokens=256):
        x = np.asarray(audio).reshape(-1)
        x = x.astype(np.float32) / 32768 if x.dtype == np.int16 else x.astype(np.float32, copy=False)
        if len(x):
            state.buffer = np.concatenate([state.buffer, x])
        if len(state.buffer) < state.chunk_size_samples:
            return None
        chunk = state.buffer[:state.chunk_size_samples]
        state.buffer = state.buffer[state.chunk_size_samples:]
        self._append_audio(state, chunk)
        return self._prepared(state, "normal", max_tokens)

    def prepare_final(self, state, *, max_tokens=256):
        if state.buffer is None or not len(state.buffer):
            return None
        tail = state.buffer
        state.buffer = np.zeros(0, dtype=np.float32)
        self._append_audio(state, tail)
        return self._prepared(state, "final", max_tokens)

    def apply_normal(self, state, step, generated):
        state._raw_decoded = step.prefix + generated
        state.language, state.text = self.helpers.parse_output(
            state._raw_decoded, user_language=state.force_language,
        )
        state.chunk_id += 1
        return state.text

    def apply_final(self, state, step, generated):
        return self.apply_normal(state, step, generated)


class AsyncQwenDecoder(AsyncR2T2Decoder):
    def __init__(self, model, session, *, instance=None):
        StreamDecoder.__init__(self, model, "qwen-stream")
        self.session = session
        self.instance = instance or uuid.uuid4().hex
        self.utterance = 0
        self.step_id = 0
        self.inflight = None
        self.cancelled = False
        self.audio_processed_samples = 0
        self.step_metrics = []

    async def _final_text(self, tail):
        started = time.monotonic()
        self.state.buffer = np.concatenate([self.state.buffer, tail, np.zeros(1280, dtype=np.float32)])
        step = self.model.prepare_final(self.state)
        generated = await self._generate(step, started)
        final = self._global(self.model.apply_final(self.state, step, generated))
        if not final.startswith(self.committed):
            raise ValueError("Model final output revised the committed prefix")
        return final

    async def _rotate_qwen(self):
        if len(self.state.audio_accum) < 30 * 16000:
            return False
        final = await self._final_text(np.zeros(0, dtype=np.float32))
        self.base = self.committed = self.candidate = final
        self.pending_text = ""
        self.state = self._new_state()
        self.has_audio = False
        self.utterance_boundary = False
        return True

    async def feed(self, pcm):
        self._check()
        if not pcm or len(pcm) > 64000 or len(pcm) % 2:
            raise ValueError("PCM16 chunk must contain 1 to 32000 samples")
        self.step_metrics = []
        self.pending.extend(pcm)
        changed = False
        while len(self.pending) >= 64000:
            self._check()
            audio = np.frombuffer(bytes(self.pending[:64000]), dtype="<i2")
            del self.pending[:64000]
            if not len(self.state.audio_accum) and not np.any(audio):
                self.audio_processed_samples += 32000
                continue
            started = time.monotonic()
            step = self.model.prepare_normal(audio, self.state)
            generated = await self._generate(step, started)
            self.candidate = self._global(self.model.apply_normal(self.state, step, generated))
            fixed = self._global(self._fixed_qwen())
            if not fixed.startswith(self.committed):
                if self.committed.startswith(fixed) and self.candidate.startswith(self.committed):
                    fixed = self.committed
                else:
                    raise ValueError("Model revised the committed prefix")
            changed |= fixed != self.committed
            self.committed = fixed
            self.pending_text = self.candidate[len(fixed):] if self.candidate.startswith(fixed) else ""
            self.first, self.has_audio = False, True
            self.audio_processed_samples += 32000
            self.max_window = max(self.max_window, len(self.state.audio_accum))
            changed |= await self._rotate_qwen()
            await asyncio.sleep(0)
        return self.result(changed)

    async def _finalize(self, *, restart):
        self._check()
        self.step_metrics = []
        tail_samples = len(self.pending) // 2
        tail = np.frombuffer(bytes(self.pending), dtype="<i2").astype(np.float32) / 32768
        self.pending.clear()
        if not self.has_audio and not np.any(tail):
            self.audio_processed_samples += tail_samples
            return self.result()
        final = await self._final_text(tail)
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
        return self.result(changed)
