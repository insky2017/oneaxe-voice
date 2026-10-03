"""Bounded PulseAudio/PipeWire capture with explicit microphone selection."""

import array
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import io
import json
import math
import subprocess
import sys
import time
import wave


class CaptureError(RuntimeError):
    """The selected microphone cannot provide a usable recording."""


def sources() -> list[dict]:
    """List physical capture sources without changing the system default."""
    result = subprocess.run(
        ["pactl", "--format=json", "list", "sources"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    return [item for item in json.loads(result.stdout)
            if not item["name"].endswith(".monitor") and not item.get("monitor_source")]


def select_source(items: list[dict], requested: str | None = None) -> dict:
    """Require the configured source or a unique DJI device, never a fallback."""
    if requested:
        matches = [item for item in items if item["name"] == requested]
    else:
        matches = [item for item in items if "dji" in json.dumps(item).lower()]
    if len(matches) != 1:
        raise CaptureError("未找到唯一的 DJI 麦克风；请连接设备，或配置 source 后重试")
    if matches[0].get("mute"):
        raise CaptureError("所选麦克风已静音，请先在系统声音设置中取消静音")
    return matches[0]


@dataclass
class Recording:
    """PCM16 mono recording and level evidence; audio stays in memory."""

    wav: bytes
    seconds: float
    rms_dbfs: float
    loudest_frame_dbfs: float


def pack_recording(pcm: bytes) -> Recording:
    """Build a valid WAV and calculate levels without importing the GPU stack."""
    pcm = pcm[:len(pcm) // 2 * 2]
    samples = array.array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) < 1600:
        raise CaptureError("录音不足 0.1 秒，请重新录制")

    def level(values):
        power = sum(value * value for value in values) / len(values)
        return 10 * math.log10(max(power, 1e-12) / 32768**2)

    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return Recording(
        target.getvalue(), len(samples) / 16000, round(level(samples), 1),
        round(max(level(samples[i:i+320]) for i in range(0, len(samples), 320)), 1),
    )


async def pcm_chunks(source: str, stop: asyncio.Event, seconds: float):
    """Read continuously while another task performs ASR; always reap parec."""
    process = await asyncio.create_subprocess_exec(
        "parec", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
        "--latency-msec=50", "--client-name=OneAxe Voice",
        "--stream-name=Dictation", "--device=" + source,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    received = 0
    maximum = int(seconds * 16000) * 2
    deadline = time.monotonic() + seconds
    stopped = asyncio.create_task(stop.wait())
    reading = None
    try:
        while received < maximum and not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            reading = asyncio.create_task(process.stdout.read(min(4096, maximum - received)))
            done, _ = await asyncio.wait(
                [reading, stopped], timeout=min(3, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopped in done and reading not in done:
                break
            if reading not in done:
                if time.monotonic() >= deadline:
                    break
                raise CaptureError("麦克风超过 3 秒未提供音频，请检查连接")
            chunk = reading.result()
            if not chunk:
                raise CaptureError("麦克风录音意外中断，请检查设备连接")
            received += len(chunk)
            yield chunk
    finally:
        for task in (reading, stopped):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (reading, stopped) if t is not None), return_exceptions=True)
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def record(source: str, stop: asyncio.Event, seconds: float = 60) -> Recording:
    """Capture one bounded WAV for diagnostics or a file transcription."""
    if not 0.1 <= seconds <= 60:
        raise ValueError("录音上限须为 0.1–60 秒")
    pcm = bytearray()
    async for chunk in pcm_chunks(source, stop, seconds):
        pcm.extend(chunk)
    return pack_recording(bytes(pcm))


async def segment_recordings(source: str, stop: asyncio.Event, config: dict, progress):
    """Yield VAD-delimited PCM while the recording device remains open."""
    from contextlib import aclosing
    from .vad import Segmenter
    segmenter = Segmenter(
        config["pause_ms"], config["segment_seconds"], config["vad_mode"], config["vad_min_dbfs"],
    )
    async with aclosing(pcm_chunks(source, stop, config["max_session_seconds"])) as stream:
        async for chunk in stream:
            for segment in segmenter.feed(chunk):
                yield segment
            progress(segmenter.active, segmenter.total_frames * 0.02)
    for segment in segmenter.finish():
        yield segment
