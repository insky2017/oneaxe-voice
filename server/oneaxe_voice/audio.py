"""Validate short PCM WAV input before allocating GPU memory."""

from dataclasses import dataclass
import io
from pathlib import Path
import subprocess
import wave

from .config import Settings


class AudioError(ValueError):
    """The submitted audio cannot be safely used for this dictation API."""


@dataclass(frozen=True)
class AudioInfo:
    """Properties checked against the actual decoded PCM payload."""

    seconds: float
    sample_rate: int
    channels: int
    silent: bool


def validate_wav(data: bytes, settings: Settings) -> AudioInfo:
    """Reject malformed, truncated, oversized, and unsupported WAV files."""
    if len(data) > settings.max_audio_bytes:
        raise AudioError("音频文件超过 12 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            rate, channels = audio.getframerate(), audio.getnchannels()
            frames, width = audio.getnframes(), audio.getsampwidth()
            if audio.getcomptype() != "NONE" or width != 2:
                raise AudioError("目前仅支持 16-bit PCM WAV")
            if channels not in (1, 2) or not 8000 <= rate <= 48000:
                raise AudioError("WAV 须为单声道或双声道，采样率 8000–48000 Hz")
            seconds = frames / rate
            if not 0.1 <= seconds <= settings.max_audio_seconds:
                raise AudioError("录音时长须为 0.1–60 秒")
            pcm = audio.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise AudioError("WAV 音频数据不完整")
            return AudioInfo(seconds, rate, channels, not any(pcm))
    except (wave.Error, EOFError) as exc:
        raise AudioError("无法读取 WAV；请提供有效的 16-bit PCM WAV") from exc


def normalize_wav(source: Path, target: Path) -> None:
    """Convert the validated recording to 16 kHz mono PCM for Qwen3-ASR."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
             str(target)],
            capture_output=True, timeout=15, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("音频格式转换超时") from exc
    if result.returncode:
        raise AudioError("音频格式转换失败")
