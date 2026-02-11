"""SQLite storage — single persistent connection, WAL mode, atomic operations.

Tables:
- cooldowns: per-user action cooldown tracking
- audit: action audit trail
- menu_state: per-chat menu message tracking for edit-in-place
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger("ha_bot.storage")


class Database:
    """Manages a persistent SQLite connection with WAL journal mode."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS cooldowns (
                   user_id   INTEGER NOT NULL,
                   action    TEXT    NOT NULL,
                   last_used REAL    NOT NULL,
                   PRIMARY KEY (user_id, action)
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS audit (
                   id        INTEGER PRIMARY KEY AUTOINCREMENT,
                   timestamp TEXT    NOT NULL,
                   chat_id   INTEGER NOT NULL,
                   user_id   INTEGER NOT NULL,
                   username  TEXT    NOT NULL,
                   action    TEXT    NOT NULL,
                   entity_id TEXT,
                   success   INTEGER NOT NULL,
                   error     TEXT
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS menu_state (
                   chat_id         INTEGER PRIMARY KEY,
                   message_id      INTEGER NOT NULL,
                   current_menu    TEXT    NOT NULL DEFAULT 'main',
                   selected_entity TEXT,
                   selected_room   TEXT,
                   updated_at      REAL    NOT NULL
               )"""
        )
        await self._db.commit()
        logger.info("Database opened: %s (WAL mode)", self._path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- cooldown ---

    async def check_and_update_cooldown(
        self, user_id: int, action: str, cooldown_seconds: int
    ) -> tuple[bool, float]:
        """Atomically check cooldown and update timestamp if allowed.

        Returns (is_allowed, remaining_seconds).
        """
        assert self._db is not None
        now = time.time()

        async with self._db.execute(
            "SELECT last_used FROM cooldowns WHERE user_id = ? AND action = ?",
            (user_id, action),
        ) as cur:
            row = await cur.fetchone()

        if row is not None:
            elapsed = now - row[0]
            if elapsed < cooldown_seconds:
                return False, cooldown_seconds - elapsed

        await self._db.execute(
            "INSERT INTO cooldowns (user_id, action, last_used) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, action) DO UPDATE SET last_used = excluded.last_used",
            (user_id, action, now),
        )
        await self._db.commit()
        return True, 0.0

    # --- audit ---

    async def write_audit(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: str,
        action: str,
        entity_id: str | None = None,
        success: bool,
        error: str | None = None,
    ) -> None:
        assert self._db is not None
        ts = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO audit (timestamp, chat_id, user_id, username, action, entity_id, success, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, chat_id, user_id, username, action, entity_id, 1 if success else 0, error),
        )
        await self._db.commit()

    # --- menu state ---

    async def get_menu_message_id(self, chat_id: int) -> int | None:
        """Return the tracked menu message_id for a chat, or None."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT message_id FROM menu_state WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_menu_state(self, chat_id: int) -> dict[str, Any] | None:
        """Return full menu state for a chat, or None."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT message_id, current_menu, selected_entity, selected_room, updated_at "
            "FROM menu_state WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "message_id": row[0],
            "current_menu": row[1],
            "selected_entity": row[2],
            "selected_room": row[3],
            "updated_at": row[4],
        }

    async def save_menu_state(
        self,
        chat_id: int,
        message_id: int,
        current_menu: str = "main",
        selected_entity: str | None = None,
        selected_room: str | None = None,
    ) -> None:
        """Upsert menu state for a chat."""
        assert self._db is not None
        now = time.time()
        await self._db.execute(
            "INSERT INTO menu_state (chat_id, message_id, current_menu, selected_entity, selected_room, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "message_id=excluded.message_id, current_menu=excluded.current_menu, "
            "selected_entity=excluded.selected_entity, selected_room=excluded.selected_room, "
            "updated_at=excluded.updated_at",
            (chat_id, message_id, current_menu, selected_entity, selected_room, now),
        )
        await self._db.commit()

    async def clear_menu_state(self, chat_id: int) -> None:
        """Remove menu state for a chat."""
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM menu_state WHERE chat_id = ?", (chat_id,)
        )
        await self._db.commit()
