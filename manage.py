from __future__ import annotations

import argparse

from telegram_relay.config import Config
from telegram_relay.storage import Storage


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Telegram Relay delivery state")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show recent delivery records")
    status.add_argument("--limit", type=int, default=30)

    for name in ("cancel", "retry"):
        cmd = sub.add_parser(name)
        cmd.add_argument("destination")
        cmd.add_argument("message_id", type=int)
        cmd.add_argument("--chat-id", type=int, default=None)

    args = parser.parse_args()
    config = Config.from_env()
    storage = Storage(config.database_path)
    try:
        if args.command == "status":
            for row in storage.recent_deliveries(args.limit):
                print(dict(row))
            return

        chat_id = args.chat_id or config.telegram_source_channel_id
        if args.command == "cancel":
            row = storage.delivery_status(args.destination, chat_id, args.message_id)
            if row is None:
                raise SystemExit(
                    f"No delivery record exists for {args.destination}:{chat_id}:{args.message_id}."
                )
            storage.mark_cancelled(args.destination, chat_id, args.message_id, "cancelled by operator")
            print("cancelled")
        elif args.command == "retry":
            source = storage.get_source_message(chat_id, args.message_id)
            if source is None:
                raise SystemExit(
                    f"Source message {args.message_id} is not stored locally; cannot retry it safely."
                )
            row = storage.delivery_status(args.destination, chat_id, args.message_id)
            if row is None:
                storage.ensure_delivery_for_message(args.destination, chat_id, args.message_id)
                row = storage.delivery_status(args.destination, chat_id, args.message_id)
            storage.set_delivery(
                args.destination, chat_id, args.message_id,
                row["destination_message_id"] if row else None,
                "retry", None, attempts=0, next_attempt_at=None,
            )
            print("queued for retry")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
