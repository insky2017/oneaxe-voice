"""Configuration confined to the OneAxe Voice project."""

from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_DIR = Path.home() / "tools" / "models" / "Qwen3-ASR-1.7B"


@dataclass(frozen=True)
class Settings:
    """Validated limits and paths for a single GPU worker."""

    model_dir: Path = DEFAULT_MODEL_DIR
    runtime_dir: Path = ROOT / "runtime"
    max_audio_bytes: int = 12 * 1024 * 1024
    max_audio_seconds: float = 60.0
    memory_fraction: float = 0.25
    idle_seconds: float = 120.0
    cuda_device: int = 0
    max_new_tokens: int = 512
    api_url: str = "http://127.0.0.1:8097"

    @property
    def token_path(self) -> Path:
        """Return the private local-client credential path."""
        return self.runtime_dir / "client.token"

    @property
    def max_body_bytes(self) -> int:
        """Allow bounded multipart overhead in addition to the audio."""
        return self.max_audio_bytes + 65536

    @classmethod
    def from_env(cls) -> "Settings":
        """Read only application-specific environment variables."""
        value = cls(
            model_dir=Path(os.getenv("ONEAXE_VOICE_MODEL_DIR", str(DEFAULT_MODEL_DIR))).expanduser(),
            runtime_dir=Path(os.getenv("ONEAXE_VOICE_RUNTIME_DIR", str(cls.runtime_dir))),
            memory_fraction=float(os.getenv("ONEAXE_VOICE_MEMORY_FRACTION", "0.25")),
            idle_seconds=float(os.getenv("ONEAXE_VOICE_IDLE_SECONDS", "120")),
            cuda_device=int(os.getenv("ONEAXE_VOICE_CUDA_DEVICE", "0")),
            api_url=os.getenv("ONEAXE_VOICE_API_URL", "http://127.0.0.1:8097"),
        )
        if not 0 < value.memory_fraction <= 1:
            raise ValueError("ONEAXE_VOICE_MEMORY_FRACTION must be in (0, 1]")
        if value.idle_seconds < 0 or value.cuda_device < 0:
            raise ValueError("Idle seconds and CUDA device must be nonnegative")
        from urllib.parse import urlsplit
        address = urlsplit(value.api_url)
        if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}
                or address.username or address.password or address.query or address.fragment
                or address.path not in {"", "/"}):
            raise ValueError("ONEAXE_VOICE_API_URL 必须是本机 HTTP 地址")
        return value
