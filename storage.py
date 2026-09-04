from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_chat_time
                    ON conversation_messages(chat_id, id DESC);
                """
            )

    def was_processed(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def mark_processed(self, message_id: str, chat_id: str, content: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_messages(message_id, chat_id, content, created_at) VALUES (?, ?, ?, ?)",
                (message_id, chat_id, content, now),
            )

    def add_message(self, chat_id: str, role: str, content: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversation_messages(chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, now),
            )

    def recent_messages(self, chat_id: str, limit: int) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM conversation_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def last_assistant_at(self, chat_id: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM conversation_messages WHERE chat_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["created_at"])

