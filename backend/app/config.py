"""Settings, read from environment variables (or backend/.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def _load_dotenv() -> None:
    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


@dataclass
class Settings:
    ollama_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    gemma_model: str = field(default_factory=lambda: os.environ.get("GEMMA_MODEL", "gemma4:latest"))
    mock_gemma: bool = field(default_factory=lambda: _bool("MOCK_GEMMA", False))
    mock_delay_seconds: float = field(default_factory=lambda: float(os.environ.get("MOCK_DELAY_SECONDS", "2")))
    think: bool = field(default_factory=lambda: _bool("GEMMA_THINK", False))
    ollama_timeout: float = field(default_factory=lambda: float(os.environ.get("OLLAMA_TIMEOUT", "600")))
    num_ctx: int = field(default_factory=lambda: int(os.environ.get("GEMMA_NUM_CTX", "8192")))
    num_thread: int = field(default_factory=lambda: int(os.environ.get("GEMMA_NUM_THREAD", "0")))  # 0 = Ollama decides
    # Google-hosted Gemma 4 (fast). "auto" = use it when an API key is set, otherwise the local copy.
    gemma_provider: str = field(default_factory=lambda: os.environ.get("GEMMA_PROVIDER", "auto").strip().lower())
    google_api_key: str = field(
        default_factory=lambda: (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    )
    google_model: str = field(default_factory=lambda: os.environ.get("GOOGLE_GEMMA_MODEL", "gemma-4-26b-a4b-it"))
    google_timeout: float = field(default_factory=lambda: float(os.environ.get("GOOGLE_TIMEOUT", "90")))
    fallback_to_local: bool = field(default_factory=lambda: _bool("FALLBACK_TO_LOCAL", True))
    # Abuse protection now that the API can be public.
    analyze_per_minute: int = field(default_factory=lambda: int(os.environ.get("ANALYZE_PER_MINUTE", "8")))
    max_waiting_jobs: int = field(default_factory=lambda: int(os.environ.get("MAX_WAITING_JOBS", "10")))
    reference_dir: Path = field(default_factory=lambda: _path("REFERENCE_DIR", BACKEND_DIR / "data" / "reference"))
    max_reference_cases: int = field(default_factory=lambda: int(os.environ.get("MAX_REFERENCE_CASES", "2")))
    send_reference_images: bool = field(default_factory=lambda: _bool("SEND_REFERENCE_IMAGES", True))
    max_upload_mb: float = field(default_factory=lambda: float(os.environ.get("MAX_UPLOAD_MB", "25")))
    max_image_side: int = field(default_factory=lambda: int(os.environ.get("MAX_IMAGE_SIDE", "1024")))
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))
    cors_origins: list[str] = field(
        default_factory=lambda: [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
    )
    frontend_dir: Path | None = field(default_factory=lambda: _frontend_dir())


def _frontend_dir() -> Path | None:
    configured = os.environ.get("FRONTEND_DIR")
    candidates = [Path(configured)] if configured else [REPO_DIR / "frontend" / "dist", REPO_DIR / "frontend"]
    for c in candidates:
        c = c if c.is_absolute() else (REPO_DIR / c)
        if (c / "index.html").exists():
            return c
    return None


settings = Settings()
