"""SQLite dedupe layer for the paperclip notify webhook.

Suppresses duplicate Telegram alerts when paperclip re-fires an identical
content_hash. State changes (previous_status != current_status) bypass dedupe
so recoveries always reach the user.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS paperclip_notify_dedupe (
    check_name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    last_sent_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (check_name, content_hash)
);
"""


class Dedupe:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def should_send(
        self,
        check: str,
        content_hash: str,
        previous_status: Optional[str],
        current_status: str,
    ) -> bool:
        if previous_status is not None and previous_status != current_status:
            return True
        try:
            cur = self._conn.execute(
                "SELECT 1 FROM paperclip_notify_dedupe WHERE check_name=? AND content_hash=?",
                (check, content_hash),
            )
            return cur.fetchone() is None
        except sqlite3.DatabaseError as e:
            logger.error("dedupe DB read error: %s — fallback to send", e)
            return True

    def record(self, check: str, content_hash: str) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO paperclip_notify_dedupe "
                "(check_name, content_hash, last_sent_at) VALUES (?, ?, datetime('now'))",
                (check, content_hash),
            )
            self._conn.commit()
        except sqlite3.DatabaseError as e:
            logger.error("dedupe DB write error: %s — continuing without record", e)
