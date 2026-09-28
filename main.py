from __future__ import annotations

import logging
import sys
import time

import requests

from telegram_relay.analyzer import analyze_message
from telegram_relay.config import Config
from telegram_relay.destinations import BaleDestination, RubikaDestination
from telegram_relay.dispatcher import Dispatcher
from telegram_relay.media import MediaManager
from telegram_relay.storage import Storage
from telegram_relay.telegram_client import TelegramClient
from telegram_relay.transformers import RemoveTelegramLinksTransformer, TransformerPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("relay")


def build_pipeline(config: Config) -> TransformerPipeline:
    transformers = []
    if config.transform_telegram_links:
        transformers.append(RemoveTelegramLinksTransformer())
    return TransformerPipeline(transformers)


def build_destinations(config: Config, session: requests.Session):
    destinations = []
    if config.bale_token and config.bale_chat_id:
        destinations.append(BaleDestination(config.bale_token, config.bale_chat_id, session, config.request_timeout))
    if config.rubika_token and config.rubika_chat_id:
        destinations.append(RubikaDestination(config.rubika_token, config.rubika_chat_id, session, config.request_timeout))
    return destinations


def ingest_update(storage: Storage, destinations, update: dict, config_source_chat_id: int) -> bool:
    if "channel_post" in update:
        event_type = "new"
        message = update["channel_post"]
    elif "edited_channel_post" in update:
        event_type = "edit"
        message = update["edited_channel_post"]
    else:
        return False

    chat_id = message.get("chat", {}).get("id")
    if chat_id is None or int(chat_id) != config_source_chat_id:
        return False
    analyzed = analyze_message(message, update_id=update.get("update_id"), event_type=event_type)
    if event_type == "new":
        log.info("Source message %s (%s)", analyzed.message_id, analyzed.content_type)
    else:
        log.info("Source edit %s (%s)", analyzed.message_id, analyzed.content_type)

    storage.ingest_message(
        int(chat_id), analyzed.message_id, update.get("update_id"), event_type,
        analyzed.media_group_id, analyzed.raw_message,
    )
    for destination in destinations:
        storage.ensure_delivery_for_message(destination.name, int(chat_id), analyzed.message_id, edit=event_type == "edit")
    return True


def main() -> None:
    config = Config.from_env()
    session = requests.Session()
    storage = Storage(config.database_path)
    recovered = storage.recover_processing()
    if recovered:
        log.warning("Recovered %d in-flight delivery jobs after restart", recovered)
    destinations = build_destinations(config, session)
    if not destinations:
        raise RuntimeError("Configure at least one destination: BALE_* or RUBIKA_*")

    telegram = TelegramClient(config)
    telegram.validate()
    telegram.disable_webhook()

    for destination in destinations:
        try:
            destination.validate()
            log.info("Destination %s is reachable", destination.name)
        except Exception as exc:
            raise RuntimeError(f"Destination {destination.name} validation failed: {exc}") from exc

    pipeline = build_pipeline(config)
    media = MediaManager(telegram, config.media_dir)
    dispatcher = Dispatcher(
        destinations, storage, media, pipeline,
        config.retry_count, config.retry_max_delay, config.album_wait_seconds,
    )

    offset_value = storage.get_state("telegram_update_offset")
    offset = int(offset_value) if offset_value is not None else None

    log.info("Relay is running. Source channel: %s", config.telegram_source_channel_id)
    try:
        while True:
            # First drain anything already persisted locally. The source can be
            # unreachable while destination retries continue independently.
            dispatcher.process_pending()

            try:
                updates = telegram.get_updates_batch(offset)
            except Exception as exc:
                log.warning("Telegram polling failed; retrying in 3s: %s", exc)
                time.sleep(3)
                continue

            if not updates:
                continue

            for update in updates:
                update_id = int(update["update_id"])
                ingest_update(storage, destinations, update, config.telegram_source_channel_id)
                # ACK/advance Telegram only after the raw update is durably stored.
                # This is the critical separation between ingestion and delivery.
                if update_id >= (offset or update_id):
                    offset = update_id + 1
                    storage.set_state("telegram_update_offset", str(offset))

            # Process newly ingested jobs without waiting for another poll.
            dispatcher.process_pending()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        storage.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.getLogger("relay").exception("Fatal error: %s", exc)
        sys.exit(1)
