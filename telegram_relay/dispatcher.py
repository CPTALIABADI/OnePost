from __future__ import annotations

import json
import logging
from pathlib import Path

from .analyzer import analyze_message
from .destinations import Destination, DestinationError
from .media import MediaError, MediaManager
from .models import AlbumInfo, MessageInfo
from .storage import Storage
from .transformers import TransformerPipeline

log = logging.getLogger(__name__)


class Dispatcher:
    """Drains the persistent delivery queue without blocking Telegram ingestion."""

    def __init__(
        self,
        destinations: list[Destination],
        storage: Storage,
        media: MediaManager,
        pipeline: TransformerPipeline | None = None,
        retry_count: int = 4,
        retry_max_delay: int = 60,
        album_wait_seconds: float = 1.5,
    ):
        self.destinations = destinations
        self.storage = storage
        self.media = media
        self.pipeline = pipeline or TransformerPipeline()
        self.retry_count = max(0, retry_count)
        self.retry_max_delay = max(1, retry_max_delay)
        self.album_wait_seconds = max(0.0, album_wait_seconds)

    @staticmethod
    def _message_from_row(row) -> MessageInfo | None:
        if row is None:
            return None
        raw = json.loads(row["raw_json"])
        return analyze_message(
            raw,
            update_id=row["source_update_id"],
            event_type=row["event_type"],
        )

    def _materialize(self, message: MessageInfo) -> Path | None:
        return self.media.materialize(message.media, message.message_id) if message.media else None

    def process_pending(self) -> None:
        for destination in self.destinations:
            # A bounded number of passes prevents one permanently failing or
            # constantly changing source from starving other work.
            for _ in range(3):
                pending = self.storage.pending(destination.name, limit=50)
                if not pending:
                    break
                progressed = False
                for row in pending:
                    if self._process_row(destination, row):
                        progressed = True
                if not progressed:
                    break

    def _process_row(self, destination: Destination, row) -> bool:
        chat_id = int(row["source_chat_id"])
        message_id = int(row["source_message_id"])
        message = self._message_from_row(self.storage.get_source_message(chat_id, message_id))
        if message is None:
            self.storage.mark_failed_permanent(
                destination.name,
                chat_id,
                message_id,
                "Source message is missing from local inbox",
            )
            log.error(
                "Permanent failure for %s message %s: source message missing",
                destination.name,
                message_id,
            )
            return True

        # Edits must be processed as a single message operation. Never feed an
        # edit through the album-send path, otherwise an edited album member
        # could be resent as a new item.
        if message.event_type == "edit":
            return self._process_single(destination, message, is_edit=True)

        if message.media_group_id:
            if not self.storage.group_ready(chat_id, message.media_group_id, self.album_wait_seconds):
                return False
            return self._process_album(destination, chat_id, message.media_group_id)

        return self._process_single(destination, message, is_edit=False)

    def _process_single(self, destination: Destination, message: MessageInfo, *, is_edit: bool) -> bool:
        chat_id = message.chat_id
        message_id = message.message_id
        transformed = self.pipeline.process(message)

        try:
            self.storage.mark_processing(destination.name, chat_id, message_id)
            media_path = self._materialize(transformed)
            if is_edit:
                self._edit(destination, transformed, media_path)
            else:
                result = destination.send(transformed, media_path)
                self.storage.mark_sent(destination.name, chat_id, message_id, result.message_id)
                log.info(
                    "Delivered message %s -> %s:%s",
                    message_id,
                    destination.name,
                    result.message_id,
                )
            return True
        except MediaError as exc:
            return self._handle_failure(
                destination,
                chat_id,
                message_id,
                DestinationError(str(exc), retryable=exc.retryable),
            )
        except DestinationError as exc:
            return self._handle_failure(destination, chat_id, message_id, exc)
        except Exception as exc:
            return self._handle_failure(
                destination,
                chat_id,
                message_id,
                DestinationError(f"Unexpected delivery error: {exc}", retryable=True),
            )

    def _process_album(self, destination: Destination, chat_id: int, group_id: str) -> bool:
        rows = self.storage.get_source_group(chat_id, group_id)
        items = [self.pipeline.process(self._message_from_row(row)) for row in rows]
        items = [item for item in items if item is not None]
        if not items:
            return False

        for item in items:
            self.storage.ensure_delivery_for_message(destination.name, chat_id, item.message_id)

        statuses = [
            self.storage.delivery_status(destination.name, chat_id, item.message_id)
            for item in items
        ]
        if all(status and status["status"] == "sent" for status in statuses):
            return True

        # A remote album call can succeed and then the process can crash before
        # SQLite records every result. If some members are known sent, never send
        # the whole group again. Complete only the missing members individually.
        if any(status and status["status"] == "sent" for status in statuses):
            log.warning(
                "Partial album delivery on %s for group %s; completing remaining items individually",
                destination.name,
                group_id,
            )
            for item, status in zip(items, statuses):
                if status and status["status"] == "sent":
                    continue
                self._process_single(destination, item, is_edit=False)
            return True

        try:
            paths = [self._materialize(item) for item in items]
            if any(path is None for path in paths):
                raise DestinationError("Album contains an item without media", retryable=False)

            if destination.supports_albums:
                for item in items:
                    self.storage.mark_processing(destination.name, chat_id, item.message_id)
                results = destination.send_album(
                    AlbumInfo(chat_id, group_id, items),
                    [path for path in paths if path is not None],
                )
                if len(results) != len(items):
                    raise DestinationError(
                        f"Destination returned {len(results)} album results for {len(items)} items",
                        retryable=False,
                    )
                for item, result in zip(items, results):
                    self.storage.mark_sent(destination.name, chat_id, item.message_id, result.message_id)
                    log.info(
                        "Delivered album %s item %s -> %s:%s",
                        group_id,
                        item.message_id,
                        destination.name,
                        result.message_id,
                    )
            else:
                log.info(
                    "Destination %s has no album API; delivering album %s item-by-item",
                    destination.name,
                    group_id,
                )
                for item, path in zip(items, paths):
                    self._process_single_with_materialized_path(destination, item, path)
            return True
        except MediaError as exc:
            wrapped = DestinationError(str(exc), retryable=exc.retryable)
            for item in items:
                self._handle_unsent_album_failure(destination, item, wrapped)
            return True
        except DestinationError as exc:
            for item in items:
                self._handle_unsent_album_failure(destination, item, exc)
            return True
        except Exception as exc:
            wrapped = DestinationError(f"Unexpected album delivery error: {exc}", retryable=True)
            for item in items:
                self._handle_unsent_album_failure(destination, item, wrapped)
            return True

    def _process_single_with_materialized_path(
        self,
        destination: Destination,
        message: MessageInfo,
        media_path: Path,
    ) -> None:
        chat_id = message.chat_id
        message_id = message.message_id
        status = self.storage.delivery_status(destination.name, chat_id, message_id)
        if status and status["status"] == "sent":
            return
        try:
            self.storage.mark_processing(destination.name, chat_id, message_id)
            result = destination.send(message, media_path)
            self.storage.mark_sent(destination.name, chat_id, message_id, result.message_id)
            log.info("Delivered message %s -> %s:%s", message_id, destination.name, result.message_id)
        except DestinationError as exc:
            self._handle_failure(destination, chat_id, message_id, exc)
        except MediaError as exc:
            self._handle_failure(
                destination,
                chat_id,
                message_id,
                DestinationError(str(exc), retryable=exc.retryable),
            )
        except Exception as exc:
            self._handle_failure(
                destination,
                chat_id,
                message_id,
                DestinationError(f"Unexpected delivery error: {exc}", retryable=True),
            )

    def _handle_unsent_album_failure(
        self,
        destination: Destination,
        item: MessageInfo,
        exc: DestinationError,
    ) -> None:
        row = self.storage.delivery_status(destination.name, item.chat_id, item.message_id)
        if row and row["status"] != "sent":
            self._handle_failure(destination, item.chat_id, item.message_id, exc)

    def _handle_failure(
        self,
        destination: Destination,
        chat_id: int,
        message_id: int,
        exc: DestinationError,
    ) -> bool:
        row = self.storage.delivery_status(destination.name, chat_id, message_id)
        attempts = int(row["attempts"]) if row else 0

        if not exc.retryable:
            self.storage.mark_failed_permanent(destination.name, chat_id, message_id, str(exc))
            log.error(
                "Permanent failure for %s message %s: %s",
                destination.name,
                message_id,
                exc,
            )
            return True

        # attempts is incremented by mark_processing before reaching here.
        if attempts >= self.retry_count + 1:
            self.storage.mark_failed_permanent(destination.name, chat_id, message_id, str(exc))
            log.error(
                "Retry limit reached for %s message %s: %s",
                destination.name,
                message_id,
                exc,
            )
            return True

        delay = min(2 ** max(0, attempts - 1), self.retry_max_delay)
        self.storage.mark_retry(destination.name, chat_id, message_id, str(exc), delay)
        log.warning(
            "Delivery failed for %s message %s; retry scheduled in %ss: %s",
            destination.name,
            message_id,
            delay,
            exc,
        )
        return True

    def _edit(self, destination: Destination, message: MessageInfo, media_path: Path | None) -> None:
        existing = self.storage.delivery_status(destination.name, message.chat_id, message.message_id)
        if existing and existing["destination_message_id"]:
            try:
                result = destination.edit(message, existing["destination_message_id"], media_path)
                self.storage.mark_sent(
                    destination.name,
                    message.chat_id,
                    message.message_id,
                    result.message_id,
                )
                log.info(
                    "Edited message %s -> %s:%s",
                    message.message_id,
                    destination.name,
                    result.message_id,
                )
                return
            except DestinationError as exc:
                if exc.retryable:
                    raise
                log.warning(
                    "Edit unsupported for %s message %s; replacing",
                    destination.name,
                    message.message_id,
                )
                try:
                    destination.delete(existing["destination_message_id"])
                except Exception:
                    log.exception(
                        "Could not delete stale destination message %s",
                        existing["destination_message_id"],
                    )

        result = destination.send(message, media_path)
        self.storage.mark_sent(
            destination.name,
            message.chat_id,
            message.message_id,
            result.message_id,
        )
        log.info(
            "Replaced edited message %s -> %s:%s",
            message.message_id,
            destination.name,
            result.message_id,
        )
