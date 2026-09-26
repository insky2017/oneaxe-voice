"""An independent, lazy-loaded, GPU-only Qwen3-ASR worker."""

import gc
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

from .audio import normalize_wav, validate_wav
from .config import Settings

LOGGER = logging.getLogger("uvicorn.error")


class BusyError(RuntimeError):
    """A single inference is already running."""


class GPUError(RuntimeError):
    """CUDA is unavailable or this worker cannot fit within its GPU budget."""


class QwenEngine:
    """Own exactly one model instance without importing any VPlus code."""

    def __init__(self, settings: Settings) -> None:
        """Initialize bookkeeping while leaving the model unloaded."""
        self.settings = settings
        self._gate = threading.Lock()
        self._state_lock = threading.Lock()
        self._model = None
        self._torch = None
        self._last_used = time.monotonic()
        self._state: dict[str, Any] = {
            "state": "unloaded", "busy": False, "model_loaded": False,
            "model": settings.model_dir.name, "device": None, "gpu_name": None,
            "pid": os.getpid(), "last_error": None,
            "memory_fraction": settings.memory_fraction,
            "idle_unload_seconds": settings.idle_seconds,
        }

    def _update(self, **values: Any) -> None:
        """Publish a small status snapshot without blocking on inference."""
        with self._state_lock:
            self._state.update(values)

    def status(self) -> dict[str, Any]:
        """Read readiness independently of GPU initialization or generation."""
        with self._state_lock:
            return dict(self._state)

    def _load(self) -> None:
        """Load local weights entirely on CUDA and verify all parameter devices."""
        if self._model is not None:
            return
        self._update(state="loading")
        if not self.settings.model_dir.is_dir():
            raise GPUError("本地 Qwen3-ASR 模型目录不存在")
        import torch
        self._torch = torch
        if not torch.cuda.is_available():
            raise GPUError("CUDA 不可用；本服务要求使用 GPU")
        device = self.settings.cuda_device
        if device >= torch.cuda.device_count():
            raise GPUError("配置的 CUDA 设备不存在")
        torch.set_num_threads(4)
        torch.cuda.set_device(device)
        torch.cuda.set_per_process_memory_fraction(self.settings.memory_fraction, device)
        free, total = torch.cuda.mem_get_info(device)
        if free < 5 * 1024**3:
            raise GPUError("可用显存不足 5 GiB，请稍后重试")
        if total * self.settings.memory_fraction < 4 * 1024**3:
            raise GPUError("本服务的显存分配上限不足 4 GiB")
        from qwen_asr import Qwen3ASRModel
        model = Qwen3ASRModel.from_pretrained(
            str(self.settings.model_dir),
            dtype=torch.float16,
            device_map=f"cuda:{device}",
            local_files_only=True,
            max_inference_batch_size=1,
            max_new_tokens=self.settings.max_new_tokens,
        )
        devices = {str(parameter.device) for parameter in model.model.parameters()}
        if devices != {f"cuda:{device}"}:
            raise GPUError("模型未全部加载到指定 GPU")
        self._model = model
        self._update(
            state="ready", model_loaded=True, device=f"cuda:{device}",
            gpu_name=torch.cuda.get_device_name(device),
            memory_limit_mib=round(total * self.settings.memory_fraction / 1024**2),
        )

    def _release(self) -> None:
        """Release model and cuBLAS workspaces that can pin large split blocks."""
        self._model = None
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_initialized():
            self._torch.cuda.synchronize(self.settings.cuda_device)
            # PyTorch 2.10 caches a workspace per thread/handle. Small retained
            # workspaces can pin GiBs of split allocator blocks after model GC.
            # This private API is version dependent and affects only our process.
            clear_workspaces = getattr(
                getattr(self._torch, "_C", None), "_cuda_clearCublasWorkspaces", None,
            )
            if clear_workspaces is not None:
                clear_workspaces()
            else:
                LOGGER.warning("cuBLAS workspace cleanup unavailable; GPU cache may remain")
            self._torch.cuda.empty_cache()
            self._update(**self._memory_snapshot())
        self._update(state="unloaded", model_loaded=False, device=None)
        LOGGER.info("model_unloaded pid=%s", os.getpid())

    def _memory_snapshot(self) -> dict[str, float]:
        """Sample this process's allocator after inference or unloading."""
        device = self.settings.cuda_device
        return {
            "allocated_mib": round(self._torch.cuda.memory_allocated(device) / 1024**2, 1),
            "reserved_mib": round(self._torch.cuda.memory_reserved(device) / 1024**2, 1),
        }

    def unload_if_idle(self, force: bool = False) -> bool:
        """Unload only when no request owns the worker."""
        if not self._gate.acquire(blocking=False):
            return False
        try:
            expired = (
                self.settings.idle_seconds > 0
                and time.monotonic() - self._last_used >= self.settings.idle_seconds
            )
            if self._model is None or not (force or expired):
                return False
            self._release()
            return True
        finally:
            self._gate.release()

    def transcribe(self, data: bytes, request_id: str) -> dict[str, Any]:
        """Validate, normalize, and transcribe one recording with honest timings."""
        if not self._gate.acquire(blocking=False):
            raise BusyError("已有一段录音正在识别，请稍后重试")
        started = time.perf_counter()
        self._update(busy=True, last_error=None)
        try:
            info = validate_wav(data, self.settings)
            if info.silent:
                return {
                    "request_id": request_id, "text": "", "language": "Chinese",
                    "model": self.settings.model_dir.name, "device": None,
                    "audio_seconds": info.seconds, "inference_performed": False,
                    "skipped_reason": "digital_silence",
                    "timing_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
                }
            temp_root = self.settings.runtime_dir / "tmp"
            temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="voice-", dir=temp_root) as temp:
                source, normalized = Path(temp) / "input.wav", Path(temp) / "mono.wav"
                source.write_bytes(data)
                normalize_wav(source, normalized)
                normalized_at = time.perf_counter()
                self._load()
                torch = self._torch
                device = self.settings.cuda_device
                torch.cuda.synchronize(device)
                loaded_at = time.perf_counter()
                torch.cuda.reset_peak_memory_stats(device)
                self._update(state="transcribing")
                with torch.inference_mode():
                    result = self._model.transcribe(audio=str(normalized), language="Chinese")
                torch.cuda.synchronize(device)
                finished = time.perf_counter()
                if not result or not hasattr(result[0], "text"):
                    raise RuntimeError("识别引擎没有返回有效结果")
                text = result[0].text.strip()
                peak = round(torch.cuda.max_memory_allocated(device) / 1024**2, 1)
                self._update(state="ready", peak_allocated_mib=peak, **self._memory_snapshot())
                return {
                    "request_id": request_id, "text": text, "language": "Chinese",
                    "model": self.settings.model_dir.name,
                    "device": f"cuda:{device}", "gpu_name": torch.cuda.get_device_name(device),
                    "pid": os.getpid(), "audio_seconds": round(info.seconds, 3),
                    "inference_performed": True, "peak_allocated_mib": peak,
                    "timing_ms": {
                        "normalize": round((normalized_at - started) * 1000, 2),
                        "model_load": round((loaded_at - normalized_at) * 1000, 2),
                        "inference": round((finished - loaded_at) * 1000, 2),
                        "total": round((finished - started) * 1000, 2),
                    },
                }
        except Exception as exc:
            if self._torch is not None and isinstance(exc, self._torch.cuda.OutOfMemoryError):
                self._release()
                message = "GPU 显存不足或达到本服务的分配上限"
                self._update(last_error=message)
                raise GPUError(message) from exc
            self._update(
                state="ready" if self._model is not None else "unloaded",
                last_error=str(exc) if isinstance(exc, GPUError) else type(exc).__name__,
            )
            raise
        finally:
            self._last_used = time.monotonic()
            self._update(busy=False)
            self._gate.release()
