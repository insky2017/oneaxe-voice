"""Isolated vLLM worker, using the official Qwen and pinned R2T2 APIs.

Only a private inherited socket carries results. Upstream prints containing
transcripts are suppressed; diagnostics must never become dictation logs.
"""

import base64
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback


class StreamDecoder:
    def __init__(self, model, mode):
        self.model, self.mode = model, mode
        self.reset()

    def reset(self):
        self.state = self.model.init_streaming_state(
            language="Chinese", chunk_size_sec=.32 if self.mode == "r2t2" else 2,
            unfixed_chunk_num=0 if self.mode == "r2t2" else 2,
            unfixed_token_num=1 if self.mode == "r2t2" else 5,
        )
        self.committed = ""
        self.base = ""
        self.pending = bytearray()
        self.candidate = ""
        self.max_window = 0
        self.first = True

    def _fixed_qwen(self):
        # Preserve the exact tokenizer rollback rule of the official next step.
        # Keep an additional 8 tokens until the following decode to cover token
        # resegmentation at the prefix boundary. Never paste a mutable suffix.
        state = self.state
        if state.chunk_id < state.unfixed_chunk_num:
            return ""
        ids = self.model.processor.tokenizer.encode(state._raw_decoded)
        keep = max(0, len(ids) - state.unfixed_token_num - 8)
        while keep:
            fixed = self.model.processor.tokenizer.decode(ids[:keep])
            if "\ufffd" not in fixed:
                return fixed
            keep -= 1
        return ""

    def _rotate_qwen(self):
        # Qwen has no timestamp-aligned rolling API. Finish complete 30-second
        # acoustic windows through its public API, then start a fresh state.
        # Never guess a text offset from delayed commits or deduplicate speech.
        import numpy as np
        if len(self.state.audio_accum) < 30 * 16000:
            return False
        self.state.buffer = np.zeros(1280, dtype=np.float32)
        self.model.finish_streaming_transcribe(self.state)
        final = self.base + self.state.text
        if not final.startswith(self.committed):
            raise ValueError("模型窗口尾部与已提交文字不一致")
        self.base = self.committed = self.candidate = final
        self.state = self.model.init_streaming_state(
            language="Chinese", chunk_size_sec=2, unfixed_chunk_num=2, unfixed_token_num=5,
        )
        return True

    def feed(self, pcm):
        import numpy as np
        self.pending.extend(pcm)
        changed = False
        while True:
            count = (5120 if self.first else 2560) if self.mode == "r2t2" else 32000
            if len(self.pending) < count * 2:
                break
            audio = np.frombuffer(bytes(self.pending[:count * 2]), dtype="<i2")
            del self.pending[:count * 2]
            if (self.first or self.mode == "qwen-stream" and not len(self.state.audio_accum)) and not np.any(audio):
                continue  # Digital silence only; no VAD or level-based segmentation.
            if self.mode == "r2t2":
                candidate, fixed = self.model.streaming_transcribe_no_reset(
                    audio, self.state, max_new_tokens=4 if self.first else 2,
                )
                # The official rolling API returns a local candidate and a
                # session-wide fixed prefix; preview the current acoustic window.
                self.candidate = candidate
                self.state.chunk_size_sec = .16
                self.state.chunk_size_samples = 2560
            else:
                self.model.streaming_transcribe(audio, self.state)
                self.candidate = self.base + self.state.text
                fixed = self.base + self._fixed_qwen()
            if not fixed.startswith(self.committed):
                if (self.mode == "qwen-stream" and self.committed.startswith(fixed)
                        and self.candidate.startswith(self.committed)):
                    # Tokenization may move the conservative commit horizon
                    # backwards while the actual hypothesis still agrees.
                    fixed = self.committed
                else:
                    raise ValueError("模型修订了已提交前缀，已停止输入以保留原文")
            changed |= fixed != self.committed
            self.committed = fixed
            self.first = False
            self.max_window = max(self.max_window, len(self.state.audio_accum))
            if self.mode == "qwen-stream":
                changed |= self._rotate_qwen()
        return self.result(changed)

    def finish(self):
        import numpy as np
        # Both upstream APIs skip final inference when the buffer is exactly
        # empty. Supply 80 ms of trailing context so withheld tokens are flushed.
        tail = np.frombuffer(bytes(self.pending), dtype="<i2").astype(np.float32) / 32768
        self.pending.clear()
        if (self.first or self.mode == "qwen-stream" and not len(self.state.audio_accum)) and not np.any(tail):
            return self.result()
        self.state.buffer = np.concatenate([self.state.buffer, tail, np.zeros(1280, dtype=np.float32)])
        if self.mode == "r2t2":
            final = self.model.finish_streaming_transcribe_no_reset(self.state, max_new_tokens=64)
        else:
            self.model.finish_streaming_transcribe(self.state)
            final = self.base + self.state.text
        if not final.startswith(self.committed):
            raise ValueError("模型尾部与已提交文字不一致，已停止自动输入")
        changed = final != self.committed
        self.committed = final
        self.candidate = final
        return self.result(changed)

    def result(self, changed=False):
        return {"text": self.committed, "preview": self.candidate[-600:],
                "changed": changed, "device": "cuda:0",
                "window_seconds": round(self.max_window / 16000, 2)}


def main():
    connection = socket.socket(fileno=int(os.environ["ONEAXE_WORKER_FD"]))
    stream = connection.makefile("rb")

    def send(value):
        connection.sendall((json.dumps(value, ensure_ascii=False) + "\n").encode())

    try:
        mode, model_dir = sys.argv[1:3]
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用")
        if torch.cuda.mem_get_info()[0] < 7 * 1024**3:
            raise RuntimeError("流式模式需要至少 7 GiB 空闲显存")
        torch.set_num_threads(4)
        if mode == "r2t2":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
            from r2t2 import R2T2ASRModel as Model
        else:
            from qwen_asr import Qwen3ASRModel as Model
        model = Model.LLM(
            model=model_dir, dtype="float16", gpu_memory_utilization=.30,
            kv_cache_memory_bytes=512 * 1024**2,
            max_model_len=4096, max_num_seqs=1, max_new_tokens=256,
            enforce_eager=False, enable_prefix_caching=False, mm_processor_cache_gb=0,
            compilation_config={"mode": 0, "cudagraph_mode": "FULL_DECODE_ONLY",
                                "cudagraph_capture_sizes": [1]},
        )
        decoder = StreamDecoder(model, mode)
        # Warm the actual acoustic path before reporting ready. The first
        # prefill otherwise stalls microphone consumption despite loaded weights.
        import numpy as np
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
            if mode == "r2t2":
                model.streaming_transcribe_no_reset(
                    np.zeros(5120, dtype=np.float32), decoder.state, max_new_tokens=4,
                )
            else:
                model.streaming_transcribe(np.zeros(32000, dtype=np.float32), decoder.state)
        decoder.reset()
        send({"ready": True, "device": "cuda:0", "mode": mode})
        with open(os.devnull, "w") as quiet:
            while line := stream.readline(200000):
                request = json.loads(line)
                started = time.monotonic()
                with redirect_stdout(quiet):
                    if request["op"] == "start":
                        decoder.reset()
                        result = {"ready": True}
                    elif request["op"] == "audio":
                        pcm = base64.b64decode(request["pcm"], validate=True)
                        if len(pcm) > 64000 or len(pcm) % 2:
                            raise ValueError("音频块须为不超过 2 秒的 PCM16")
                        result = decoder.feed(pcm)
                    elif request["op"] == "finish":
                        result = decoder.finish()
                    else:
                        raise ValueError("未知流式请求")
                result["inference_ms"] = round((time.monotonic() - started) * 1000, 2)
                send(result)
    except Exception as exc:
        # Do not leak raw upstream exception strings (they may contain prompts).
        frames = traceback.extract_tb(exc.__traceback__)
        where = [f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames[-5:]]
        print("stream_worker_failed", type(exc).__name__, ";".join(where), file=sys.stderr, flush=True)
        send({"error": type(exc).__name__, "where": where})
    finally:
        connection.close()


if __name__ == "__main__":
    main()
