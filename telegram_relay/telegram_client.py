from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

from .config import Config

log = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    def __init__(self, message: str, *, error_code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"{config.telegram_api_base}/bot{config.telegram_bot_token}"
        self.session = requests.Session()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(self.config.retry_count + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/{method}",
                    json=params or {},
                    timeout=self.config.request_timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("ok"):
                    return payload.get("result")
                code = int(payload.get("error_code", 0) or 0)
                retry_after = payload.get("parameters", {}).get("retry_after")
                error = TelegramAPIError(
                    f"Telegram {method} failed: {payload}",
                    error_code=code,
                    retry_after=retry_after,
                )
                if attempt < self.config.retry_count and (retry_after or code >= 500):
                    delay = float(retry_after or min(2 ** attempt, self.config.retry_max_delay))
                    time.sleep(delay)
                    continue
                raise error
            except requests.RequestException:
                if attempt >= self.config.retry_count:
                    raise
                time.sleep(min(2 ** attempt, self.config.retry_max_delay))
        raise RuntimeError(f"Telegram {method} failed")

    def validate(self) -> None:
        bot = self.call("getMe")
        log.info("Connected to Telegram as @%s (id=%s)", bot.get("username"), bot.get("id"))

    def disable_webhook(self) -> None:
        self.call("deleteWebhook", {"drop_pending_updates": False})

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_id: str, destination: str) -> str:
        info = self.get_file(file_id)
        path = info.get("file_path")
        if not path:
            raise TelegramAPIError("Telegram getFile returned no file_path")
        url = f"{self.config.telegram_api_base}/file/bot{self.config.telegram_bot_token}/{path}"
        try:
            with self.session.get(url, stream=True, timeout=self.config.request_timeout) as response:
                response.raise_for_status()
                with open(destination, "wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) in {400, 404}:
                raise TelegramAPIError(
                    f"Telegram file download failed for {file_id}: {exc}",
                    error_code=getattr(exc.response, "status_code", None),
                ) from exc
            raise
        return destination

    def get_updates_batch(self, offset: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": self.config.poll_timeout,
            "limit": 100,
            "allowed_updates": ["channel_post", "edited_channel_post"],
        }
        if offset is not None:
            params["offset"] = offset
        return list(self.call("getUpdates", params) or [])
