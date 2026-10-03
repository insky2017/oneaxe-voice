"""Multiplexed streaming AsyncLLM worker; private JSON RPC never logs speech."""

import asyncio
import base64
from contextlib import redirect_stdout, suppress
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
from types import SimpleNamespace
import uuid

from .r2t2_async import AsyncR2T2Adapter, AsyncR2T2Decoder, FatalEngineError, SessionCancelled
from .qwen_async import AsyncQwenAdapter, AsyncQwenDecoder


def register_qwen_backend():
    from qwen_asr.core.transformers_backend import (
        Qwen3ASRConfig, Qwen3ASRForConditionalGeneration, Qwen3ASRProcessor,
    )
    from transformers import AutoConfig, AutoModel, AutoProcessor
    from vllm import ModelRegistry
    from qwen_asr.core.vllm_backend import Qwen3ASRForConditionalGeneration as Backend

    AutoConfig.register("qwen3_asr", Qwen3ASRConfig, exist_ok=True)
    AutoModel.register(Qwen3ASRConfig, Qwen3ASRForConditionalGeneration, exist_ok=True)
    AutoProcessor.register(Qwen3ASRConfig, Qwen3ASRProcessor, exist_ok=True)
    ModelRegistry.register_model("Qwen3ASRForConditionalGeneration",
                                 "qwen_asr.core.vllm_backend.qwen3_asr:Qwen3ASRForConditionalGeneration")
    if Backend.__name__ not in ModelRegistry.get_supported_archs():
        raise RuntimeError("Qwen ASR backend registration failed")


# spawn reimports the entry module before EngineCore/GPU worker construction.
if os.environ.get("ONEAXE_VOICE_REGISTER_QWEN") == "1":
    register_qwen_backend()


@dataclass
class Session:
    decoder: AsyncR2T2Decoder | AsyncQwenDecoder
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: asyncio.Task | None = None
    queued: int = 0
    finished: bool = False


class ConcurrentSessions:
    def __init__(self, model, *, mode="r2t2", capacity=2, max_queued=64):
        if mode not in {"r2t2", "qwen-stream"}:
            raise ValueError("Unknown concurrent streaming mode")
        self.model, self.capacity, self.max_queued = model, capacity, max_queued
        self.decoder_type = AsyncR2T2Decoder if mode == "r2t2" else AsyncQwenDecoder
        self.sessions = {}
        self.instance = uuid.uuid4().hex

    async def dispatch(self, request, *, received=None):
        rpc_id, session_id = request.get("rpc_id"), request.get("session")
        received = time.monotonic() if received is None else received
        response = {"rpc_id": rpc_id, "session": session_id}
        entry = None
        try:
            if not isinstance(rpc_id, str) or not rpc_id or not isinstance(session_id, str) or not session_id:
                raise ValueError("RPC and session identifiers are required")
            if len(rpc_id) > 160 or len(session_id) > 160:
                raise ValueError("RPC and session identifiers exceed their limits")
            op = request.get("op")
            if op == "start":
                if session_id in self.sessions:
                    raise ValueError("Session already exists")
                if len(self.sessions) >= self.capacity:
                    return {**response, "ok": False, "error": "CapacityError", "code": "CAPACITY_EXCEEDED",
                            "message": "Concurrent session capacity reached", "retryable": True, "fatal": False}
                entry = Session(self.decoder_type(self.model, session_id,
                                                 instance=self.instance + "-" + uuid.uuid4().hex))
                self.sessions[session_id] = entry
                return {**response, "ok": True, "ready": True, **entry.decoder.result(), "inference_ms": 0}
            if op in {"cancel", "end"}:
                entry = self.sessions.pop(session_id, None)
                if entry is not None:
                    await entry.decoder.cancel()
                    if entry.active is not None:
                        entry.active.cancel()
                    result = entry.decoder.result()
                else:
                    result = {}
                return {**response, "ok": True, **result, "cancelled": op == "cancel", "ended": op == "end"}
            if op not in {"audio", "flush", "finish"}:
                raise ValueError("Unknown streaming operation")
            entry = self.sessions.get(session_id)
            if entry is None:
                raise SessionCancelled("Session ended")
            if entry.queued >= self.max_queued:
                return {**response, "ok": False, "error": "CapacityError", "code": "CAPACITY_EXCEEDED",
                        "message": "Session input queue is full", "retryable": False, "fatal": False}
            entry.queued += 1
            try:
                async with entry.lock:
                    entry.decoder._check()
                    if entry.finished:
                        if op == "finish":
                            return {**response, "ok": True, **entry.decoder.result(), "inference_ms": 0}
                        raise SessionCancelled("Session finished")
                    started = time.monotonic()
                    entry.active = asyncio.current_task()
                    try:
                        if op == "audio":
                            pcm = base64.b64decode(request.get("pcm", ""), validate=True)
                            result = await entry.decoder.feed(pcm)
                        elif op == "flush":
                            result = await entry.decoder.flush()
                        else:
                            result = await entry.decoder.finish()
                            entry.finished = True
                        return {**response, "ok": True, **result,
                                "queued_at": received, "queue_ms": round((started - received) * 1000, 3),
                                "inference_ms": round((time.monotonic() - started) * 1000, 3)}
                    finally:
                        entry.active = None
            finally:
                entry.queued -= 1
        except (asyncio.CancelledError, SessionCancelled):
            return {**response, "ok": False, "error": "SessionCancelled", "code": "CANCELLED",
                    "message": "Session ended", "retryable": False, "fatal": False}
        except FatalEngineError:
            return {**response, "ok": False, "error": "FatalEngineError", "code": "SERVICE_UNAVAILABLE",
                    "message": "Shared model engine failed", "retryable": False, "fatal": True}
        except Exception as exc:
            if entry is not None:
                if self.sessions.get(session_id) is entry:
                    self.sessions.pop(session_id)
                with suppress(Exception):
                    await entry.decoder.cancel()
            return {**response, "ok": False, "error": type(exc).__name__,
                    "code": "INVALID_AUDIO" if isinstance(exc, ValueError) else "SESSION_FAILED",
                    "message": "Streaming session failed", "retryable": False, "fatal": False}

    async def close(self):
        entries, self.sessions = list(self.sessions.values()), {}
        for entry in entries:
            with suppress(Exception):
                await entry.decoder.cancel()
            if entry.active is not None:
                entry.active.cancel()


def engine_configuration(model_dir, mode="r2t2"):
    if mode not in {"r2t2", "qwen-stream"}:
        raise ValueError("Unknown concurrent streaming mode")
    sequences = int(os.environ.get("ONEAXE_VOICE_STREAM_MAX_NUM_SEQS", "2"))
    default_cache = 512 * 1024**2 if mode == "qwen-stream" else 1024**3
    cache = int(os.environ.get("ONEAXE_VOICE_STREAM_KV_CACHE_BYTES", str(default_cache)))
    if not 2 <= sequences <= 16 or cache < 512 * 1024**2:
        raise ValueError("Invalid streaming model capacity configuration")
    return {"model": str(model_dir), "dtype": "float16", "gpu_memory_utilization": .30,
            "kv_cache_memory_bytes": cache, "max_model_len": 4096, "max_num_seqs": sequences,
            "enforce_eager": False, "enable_prefix_caching": False, "mm_processor_cache_gb": 0,
            "enable_log_requests": False, "disable_log_stats": True,
            "compilation_config": {"mode": 0, "cudagraph_mode": "FULL_DECODE_ONLY",
                                   "cudagraph_capture_sizes": [1, 2]}}


async def load_model(model_dir, mode="r2t2"):
    os.environ["ONEAXE_VOICE_REGISTER_QWEN"] = "1"
    register_qwen_backend()
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    if torch.cuda.mem_get_info()[0] < 7 * 1024**3:
        raise RuntimeError("Streaming model requires at least 7 GiB free GPU memory")
    torch.set_num_threads(4)
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.sampling_params import RequestOutputKind, SamplingParams
    from vllm.v1.engine.async_llm import AsyncLLM
    from qwen_asr.core.transformers_backend import Qwen3ASRProcessor
    from qwen_asr.inference.utils import parse_asr_output

    config = engine_configuration(model_dir, mode)
    engine = AsyncLLM.from_engine_args(AsyncEngineArgs(**config))
    try:
        processor = Qwen3ASRProcessor.from_pretrained(str(model_dir), fix_mistral_regex=True, local_files_only=True)
        if mode == "qwen-stream":
            from qwen_asr import Qwen3ASRModel
            official = Qwen3ASRModel(backend="vllm", model=engine, processor=processor, max_new_tokens=256)
            helpers = SimpleNamespace(parse_output=parse_asr_output)
            samples = lambda count: SamplingParams(temperature=0.0, max_tokens=count,
                                                  output_kind=RequestOutputKind.FINAL_ONLY)
            adapter = AsyncQwenAdapter(engine, official, helpers, samples)
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
            from r2t2 import R2T2ASRModel
            from r2t2.r2t2_asr import _normalize_punct_by_context, parse_language_output
            official = R2T2ASRModel(backend="vllm", model=engine, processor=processor)
            helpers = SimpleNamespace(normalize_punctuation=_normalize_punct_by_context,
                                      parse_language=parse_language_output, parse_output=parse_asr_output)
            samples = lambda count: SamplingParams(temperature=0.0, max_tokens=count, skip_special_tokens=True,
                                                  output_kind=RequestOutputKind.FINAL_ONLY)
            adapter = AsyncR2T2Adapter(engine, official, helpers, samples)
        return adapter, 2 if mode == "qwen-stream" else config["max_num_seqs"]
    except BaseException:
        engine.shutdown()
        raise


async def warm_model(model, mode="r2t2"):
    import numpy as np

    if mode == "qwen-stream":
        async def warm_qwen(name, samples):
            state = model.init_streaming_state(language="Chinese", chunk_size_sec=2,
                                               unfixed_chunk_num=2, unfixed_token_num=5)
            if samples > 32000:
                state.audio_accum = np.zeros(samples - 32000, dtype=np.float32)
            step = model.prepare_normal(np.zeros(32000, dtype=np.float32), state)
            await model.generate(step, "warm:" + name)

        await warm_qwen("single", 32000)
        await asyncio.gather(warm_qwen("first-a", 32000), warm_qwen("first-b", 32000))
        await asyncio.gather(warm_qwen("window-a", 30 * 16000), warm_qwen("window-b", 30 * 16000))
        return

    async def warm(name, samples, tokens):
        state = model.init_streaming_state(language="Chinese", chunk_size_sec=.32,
                                           unfixed_chunk_num=0, unfixed_token_num=1)
        if samples > 5120:
            state.audio_accum = np.zeros(samples - 5120, dtype=np.float32)
        step = model.prepare_normal(np.zeros(5120, dtype=np.float32), state, max_tokens=tokens)
        await model.generate(step, "warm:" + name)

    await warm("single", 5120, 4)
    await asyncio.gather(warm("first-a", 5120, 4), warm("first-b", 5120, 4))
    await asyncio.gather(warm("window-a", 16 * 16000, 2), warm("window-b", 16 * 16000, 2))


async def serve(reader, writer, sessions):
    sending = asyncio.Lock()
    tasks = set()
    fatal = asyncio.Event()

    async def respond(request, received):
        result = await sessions.dispatch(request, received=received)
        async with sending:
            writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        if result.get("fatal"):
            fatal.set()

    def completed(task):
        tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            fatal.set()

    try:
        while not fatal.is_set():
            reading = asyncio.create_task(reader.readline())
            failing = asyncio.create_task(fatal.wait())
            done, _ = await asyncio.wait([reading, failing], return_when=asyncio.FIRST_COMPLETED)
            failing.cancel()
            if reading not in done:
                reading.cancel()
                await asyncio.gather(reading, failing, return_exceptions=True)
                break
            await asyncio.gather(failing, return_exceptions=True)
            line = reading.result()
            received = time.monotonic()
            if not line:
                break
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("RPC must be an object")
            except (ValueError, UnicodeError):
                request = {}
            task = asyncio.create_task(respond(request, received))
            tasks.add(task)
            task.add_done_callback(completed)
    finally:
        await sessions.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run():
    connection = socket.socket(fileno=int(os.environ["ONEAXE_WORKER_FD"]))
    connection.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=connection, limit=200000)
    model = None
    try:
        mode, model_dir = sys.argv[1:3]
        if mode not in {"r2t2", "qwen-stream"}:
            raise ValueError("Unknown concurrent streaming mode")
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
            model, capacity = await load_model(model_dir, mode)
            await warm_model(model, mode)
            writer.write((json.dumps({"ready": True, "device": "cuda:0", "mode": mode,
                                      "capacity": capacity}) + "\n").encode())
            await writer.drain()
            await serve(reader, writer, ConcurrentSessions(model, mode=mode, capacity=capacity))
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        where = [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames[-5:]]
        print("concurrent_worker_failed", type(exc).__name__, ";".join(where), file=sys.stderr, flush=True)
        with suppress(Exception):
            writer.write((json.dumps({"ok": False, "error": type(exc).__name__, "code": "SERVICE_UNAVAILABLE",
                                      "message": "Shared model worker failed", "fatal": True}) + "\n").encode())
            await writer.drain()
    finally:
        if model is not None:
            model.engine.shutdown()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
