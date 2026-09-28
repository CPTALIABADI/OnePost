from __future__ import annotations

from pathlib import Path
import mimetypes
import re

from .models import MediaInfo
from .telegram_client import TelegramClient


EXTENSION_BY_KIND = {
    "photo": ".jpg",
    "video": ".mp4",
    "audio": ".bin",
    "voice": ".ogg",
    "animation": ".mp4",
    "document": "",
    "sticker": ".webp",
    "video_note": ".mp4",
}

TELEGRAM_CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024


class MediaError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def safe_filename(name: str | None, fallback: str) -> str:
    value = name or fallback
    value = re.sub(r"[^\w.\- ()\[\]]+", "_", value, flags=re.UNICODE).strip(" .")
    return value or fallback


class MediaManager:
    def __init__(self, telegram: TelegramClient, directory: str):
        self.telegram = telegram
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def materialize(self, media: MediaInfo, source_message_id: int) -> Path:
        if not media.file_id:
            raise MediaError(f"Media {media.kind!r} has no Telegram file_id")
        # Telegram's hosted Bot API currently caps getFile downloads at 20 MB.
        # A Local Bot API server can remove this limit; its base URL is configurable.
        if (
            media.file_size is not None
            and media.file_size > TELEGRAM_CLOUD_DOWNLOAD_LIMIT
            and self.telegram.config.telegram_api_base == "https://api.telegram.org"
        ):
            raise MediaError(
                f"Telegram cloud Bot API cannot download this file ({media.file_size} bytes) via getFile; "
                "use a Local Bot API server for larger files."
            )
        suffix = Path(media.file_name or "").suffix or EXTENSION_BY_KIND.get(media.kind, "")
        fallback = f"telegram_{source_message_id}_{media.kind}{suffix}"
        name = safe_filename(media.file_name, fallback)
        target = self.directory / f"{source_message_id}_{name}"
        if target.exists() and (media.file_size is None or target.stat().st_size == media.file_size):
            return target
        try:
            self.telegram.download_file(media.file_id, str(target))
        except Exception as exc:
            raise MediaError(f"Failed to materialize Telegram media {media.file_id}: {exc}", retryable=True) from exc
        return target


def guess_mime(path: Path, media: MediaInfo) -> str:
    return media.mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
