from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_messages (
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                source_update_id INTEGER,
                event_type TEXT NOT NULL,
                media_group_id TEXT,
                raw_json TEXT NOT NULL,
                ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source_chat_id, source_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_source_media_group
                ON source_messages(source_chat_id, media_group_id, event_type, source_message_id);

            CREATE TABLE IF NOT EXISTS deliveries (
                destination TEXT NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                destination_message_id TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                error TEXT,
                PRIMARY KEY(destination, source_chat_id, source_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_deliveries_queue
                ON deliveries(destination, status, next_attempt_at, source_message_id);
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(deliveries)")}
        if "attempts" not in columns:
            self.conn.execute("ALTER TABLE deliveries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "next_attempt_at" not in columns:
            self.conn.execute("ALTER TABLE deliveries ADD COLUMN next_attempt_at TEXT")
        self.conn.commit()

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def ingest_message(self, source_chat_id: int, message_id: int, update_id: int | None,
                       event_type: str, media_group_id: str | None,
                       raw_message: dict[str, Any]) -> None:
        payload = json.dumps(raw_message, ensure_ascii=False, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO source_messages(
                source_chat_id, source_message_id, source_update_id,
                event_type, media_group_id, raw_json, ingested_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(source_chat_id,source_message_id) DO UPDATE SET
                source_update_id=excluded.source_update_id,
                event_type=excluded.event_type,
                media_group_id=excluded.media_group_id,
                raw_json=excluded.raw_json,
                ingested_at=excluded.ingested_at
            """,
            (source_chat_id, message_id, update_id, event_type, media_group_id, payload, now),
        )
        self.conn.commit()

    def ensure_delivery_for_message(self, destination: str, chat_id: int, message_id: int,
                                    *, edit: bool = False) -> None:
        existing = self.delivery_status(destination, chat_id, message_id)
        if existing is None:
            status = "edit_pending" if edit else "pending"
            self.conn.execute(
                "INSERT INTO deliveries(destination,source_chat_id,source_message_id,status) VALUES(?,?,?,?)",
                (destination, chat_id, message_id, status),
            )
        elif edit:
            status = "edit_pending" if existing["destination_message_id"] else "pending"
            self.conn.execute(
                "UPDATE deliveries SET status=?, next_attempt_at=NULL, error=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE destination=? AND source_chat_id=? AND source_message_id=?",
                (status, destination, chat_id, message_id),
            )
        self.conn.commit()

    def queue_existing_source(self, destinations: list[str]) -> None:
        rows = self.conn.execute(
            "SELECT source_chat_id, source_message_id, event_type FROM source_messages"
        ).fetchall()
        for row in rows:
            for destination in destinations:
                self.ensure_delivery_for_message(
                    destination,
                    row["source_chat_id"],
                    row["source_message_id"],
                    edit=row["event_type"] == "edit",
                )

    def get_source_message(self, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM source_messages WHERE source_chat_id=? AND source_message_id=?",
            (chat_id, message_id),
        ).fetchone()

    def get_source_group(self, chat_id: int, media_group_id: str) -> list[sqlite3.Row]:
        # Edits replace the stored event_type for an existing message. An album
        # must still include an edited member, so grouping is based on media_group_id
        # rather than event_type.
        return self.conn.execute(
            """
            SELECT * FROM source_messages
            WHERE source_chat_id=? AND media_group_id=?
            ORDER BY source_message_id
            """,
            (chat_id, media_group_id),
        ).fetchall()

    def group_ready(self, chat_id: int, media_group_id: str, wait_seconds: float) -> bool:
        row = self.conn.execute(
            "SELECT MAX(ingested_at) AS latest FROM source_messages WHERE source_chat_id=? AND media_group_id=?",
            (chat_id, media_group_id),
        ).fetchone()
        if not row or not row["latest"]:
            return False
        latest = datetime.fromisoformat(row["latest"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - latest).total_seconds()
        return age >= wait_seconds

    def delivery_status(self, destination: str, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE destination=? AND source_chat_id=? AND source_message_id=?",
            (destination, chat_id, message_id),
        ).fetchone()

    def pending(self, destination: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM deliveries
            WHERE destination=?
              AND status IN ('pending','retry','edit_pending')
              AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
            ORDER BY source_chat_id, source_message_id
            LIMIT ?
            """,
            (destination, limit),
        ).fetchall()

    def mark_processing(self, destination: str, chat_id: int, message_id: int) -> int:
        row = self.delivery_status(destination, chat_id, message_id)
        attempts = int(row["attempts"]) + 1 if row else 1
        self.conn.execute(
            """
            UPDATE deliveries
            SET status='processing', attempts=?, next_attempt_at=NULL,
                error=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE destination=? AND source_chat_id=? AND source_message_id=?
            """,
            (attempts, destination, chat_id, message_id),
        )
        self.conn.commit()
        return attempts

    def mark_sent(self, destination: str, chat_id: int, message_id: int, destination_message_id: str) -> None:
        self.conn.execute(
            """
            UPDATE deliveries
            SET destination_message_id=?, status='sent', next_attempt_at=NULL,
                error=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE destination=? AND source_chat_id=? AND source_message_id=?
            """,
            (destination_message_id, destination, chat_id, message_id),
        )
        self.conn.commit()

    def set_delivery(
        self,
        destination: str,
        chat_id: int,
        message_id: int,
        destination_message_id: str | None,
        status: str,
        error: str | None = None,
        *,
        attempts: int | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        """Administrative state override used by manage.py."""
        fields = ["status = ?", "destination_message_id = ?", "error = ?", "next_attempt_at = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = [status, destination_message_id, error, next_attempt_at]
        if attempts is not None:
            fields.insert(1, "attempts = ?")
            params.insert(1, attempts)
        params.extend([destination, chat_id, message_id])
        self.conn.execute(
            f"UPDATE deliveries SET {', '.join(fields)} WHERE destination=? AND source_chat_id=? AND source_message_id=?",
            params,
        )
        self.conn.commit()

    def mark_retry(self, destination: str, chat_id: int, message_id: int, error: str, delay: float) -> None:
        self.conn.execute(
            """
            UPDATE deliveries
            SET status='retry', next_attempt_at=datetime('now', ?), error=?, updated_at=CURRENT_TIMESTAMP
            WHERE destination=? AND source_chat_id=? AND source_message_id=?
            """,
            (f"+{max(0.0, delay)} seconds", error, destination, chat_id, message_id),
        )
        self.conn.commit()

    def mark_failed_permanent(self, destination: str, chat_id: int, message_id: int, error: str) -> None:
        self.conn.execute(
            """
            UPDATE deliveries
            SET status='failed_permanent', next_attempt_at=NULL, error=?, updated_at=CURRENT_TIMESTAMP
            WHERE destination=? AND source_chat_id=? AND source_message_id=?
            """,
            (error, destination, chat_id, message_id),
        )
        self.conn.commit()

    def mark_cancelled(self, destination: str, chat_id: int, message_id: int, reason: str = "cancelled") -> None:
        self.conn.execute(
            """
            UPDATE deliveries SET status='cancelled', error=?, next_attempt_at=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE destination=? AND source_chat_id=? AND source_message_id=?
            """,
            (reason, destination, chat_id, message_id),
        )
        self.conn.commit()

    def recover_processing(self) -> int:
        """Recover jobs left in 'processing' after an unclean shutdown."""
        cursor = self.conn.execute(
            """
            UPDATE deliveries
            SET status='retry', next_attempt_at=CURRENT_TIMESTAMP,
                error=CASE
                    WHEN error IS NULL OR error='' THEN 'Recovered after process restart'
                    ELSE error
                END,
                updated_at=CURRENT_TIMESTAMP
            WHERE status='processing'
            """
        )
        self.conn.commit()
        return cursor.rowcount

    def recent_deliveries(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deliveries ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
