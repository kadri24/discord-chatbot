"""SQLite persistence for conversations and messages.

All conversation state lives here so history survives bot restarts.
Every Discord conversation channel has one row in `conversations` keyed by
its Discord channel ID. Messages carry a `state` ('complete' or 'interrupted');
only 'complete' messages are ever used as LLM context.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_CONN = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    channel_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    username    TEXT,
    user_id     TEXT,
    content     TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'complete',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(path: str) -> None:
    """Open the database and create the schema if necessary."""
    global _CONN
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _CONN = sqlite3.connect(path, check_same_thread=False)
    _CONN.row_factory = sqlite3.Row
    with _LOCK, _CONN:
        _CONN.executescript(_SCHEMA)


def create_conversation(channel_id) -> bool:
    """Create a conversation row; returns False if it already exists."""
    with _LOCK, _CONN:
        cursor = _CONN.execute(
            "INSERT OR IGNORE INTO conversations (channel_id, created_at) VALUES (?, ?)",
            (str(channel_id), _now()),
        )
        return cursor.rowcount > 0


def conversation_exists(channel_id) -> bool:
    with _LOCK:
        row = _CONN.execute(
            "SELECT 1 FROM conversations WHERE channel_id = ?", (str(channel_id),)
        ).fetchone()
        return row is not None


def add_message(channel_id, role, content, username=None, user_id=None, state="complete") -> None:
    with _LOCK, _CONN:
        _CONN.execute(
            "INSERT INTO messages (channel_id, role, username, user_id, content, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(channel_id), role, username, user_id, content, state, _now()),
        )


def get_completed_messages(channel_id):
    """All 'complete' messages of a conversation, oldest first."""
    with _LOCK:
        return _CONN.execute(
            "SELECT role, username, content FROM messages "
            "WHERE channel_id = ? AND state = 'complete' ORDER BY id",
            (str(channel_id),),
        ).fetchall()


def all_conversation_ids():
    with _LOCK:
        return [row[0] for row in _CONN.execute("SELECT channel_id FROM conversations")]


def delete_conversation(channel_id) -> None:
    with _LOCK, _CONN:
        _CONN.execute("DELETE FROM messages WHERE channel_id = ?", (str(channel_id),))
        _CONN.execute("DELETE FROM conversations WHERE channel_id = ?", (str(channel_id),))


def delete_all_conversations() -> None:
    with _LOCK, _CONN:
        _CONN.execute("DELETE FROM messages")
        _CONN.execute("DELETE FROM conversations")


def close() -> None:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
