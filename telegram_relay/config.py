from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
load_dotenv(PROJECT_DIR / ".env")
load_dotenv()  # also allow explicitly supplied process environment variables.


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_source_channel_id: int

    bale_token: str | None = None
    bale_chat_id: str | int | None = None

    rubika_token: str | None = None
    rubika_chat_id: str | None = None

    telegram_api_base: str = "https://api.telegram.org"
    poll_timeout: int = 30
    request_timeout: int = 60
    retry_count: int = 4
    retry_max_delay: int = 60
    album_wait_seconds: float = 1.5
    worker_interval: float = 1.0
    database_path: str = "data/relay.db"
    media_dir: str = "data/media"
    transform_telegram_links: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
        source = os.getenv("TELEGRAM_SOURCE_CHANNEL_ID") or os.getenv("SOURCE_CHANNEL_ID")
        if not telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        if not source:
            raise RuntimeError("TELEGRAM_SOURCE_CHANNEL_ID is not set")
        try:
            source_id = int(source)
        except ValueError as exc:
            raise RuntimeError("TELEGRAM_SOURCE_CHANNEL_ID must be an integer") from exc

        return cls(
            telegram_bot_token=telegram_token.strip(),
            telegram_source_channel_id=source_id,
            bale_token=(os.getenv("BALE_BOT_TOKEN") or os.getenv("BALE_TOKEN") or "").strip() or None,
            bale_chat_id=(os.getenv("BALE_CHAT_ID") or "").strip() or None,
            rubika_token=(os.getenv("RUBIKA_TOKEN") or "").strip() or None,
            rubika_chat_id=(os.getenv("RUBIKA_CHAT_ID") or "").strip() or None,
            telegram_api_base=(os.getenv("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/"),
            poll_timeout=_env_int("POLL_TIMEOUT", 30),
            request_timeout=_env_int("REQUEST_TIMEOUT", 60),
            retry_count=_env_int("RETRY_COUNT", 4),
            retry_max_delay=_env_int("RETRY_MAX_DELAY", 60),
            album_wait_seconds=float(os.getenv("ALBUM_WAIT_SECONDS", "1.5")),
            worker_interval=float(os.getenv("WORKER_INTERVAL", "1.0")),
            database_path=os.getenv("DATABASE_PATH", "data/relay.db"),
            media_dir=os.getenv("MEDIA_DIR", "data/media"),
            transform_telegram_links=_env_bool("TRANSFORM_TELEGRAM_LINKS", False),
        )
