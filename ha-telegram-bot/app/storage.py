"""SQLite storage — single persistent connection, WAL mode, atomic operations.

Tables:
- cooldowns: per-user action cooldown tracking
- audit: action audit trail
- menu_state: per-chat menu message tracking for edit-in-place
- favorites: per-user entity bookmarks
- rooms: canonical room names with HA area mapping and aliases
- entity_area_cache: cached entity->area->floor mapping from registry
- vacuum_room_map: vacuum segment_id <-> area mapping
- notifications: per-user entity notification preferences
- user_roles: role-based access control (admin/user/guest)
- favorites_actions: per-user favorite actions (not just entities)
- snapshots: entity state snapshots for diff
- schedules: scheduled actions with cron expressions
- error_log: ring buffer for /trace_last_error diagnostics
- mutes: per-user per-entity notification muting
"""

from __future__ import annotations

import json
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
        await self._create_tables()
        await self._db.commit()
        logger.info("Database opened: %s (WAL mode)", self._path)

    async def _create_tables(self) -> None:
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ui_sessions (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL, PRIMARY KEY(chat_id,user_id,thread_id)
            );
            CREATE TABLE IF NOT EXISTS ui_notices (
                user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                actions TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(user_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
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
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS favorites (
                   user_id   INTEGER NOT NULL,
                   entity_id TEXT    NOT NULL,
                   PRIMARY KEY (user_id, entity_id)
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS rooms (
                   id             INTEGER PRIMARY KEY AUTOINCREMENT,
                   canonical_name TEXT    NOT NULL,
                   ha_area_id     TEXT,
                   aliases_json   TEXT    NOT NULL DEFAULT '[]'
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS entity_area_cache (
                   entity_id  TEXT PRIMARY KEY,
                   area_id    TEXT,
                   area_name  TEXT,
                   floor_id   TEXT,
                   floor_name TEXT,
                   device_id  TEXT,
                   updated_at REAL NOT NULL
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS vacuum_room_map (
                   vacuum_entity_id TEXT NOT NULL,
                   segment_id       TEXT NOT NULL,
                   segment_name     TEXT NOT NULL DEFAULT '',
                   area_id          TEXT,
                   PRIMARY KEY (vacuum_entity_id, segment_id)
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS notifications (
                   user_id          INTEGER NOT NULL,
                   entity_id        TEXT    NOT NULL,
                   enabled          INTEGER NOT NULL DEFAULT 1,
                   mode             TEXT    NOT NULL DEFAULT 'state_only',
                   throttle_seconds INTEGER NOT NULL DEFAULT 60,
                   last_sent_ts     REAL    NOT NULL DEFAULT 0,
                   PRIMARY KEY (user_id, entity_id)
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS user_roles (
                   user_id INTEGER PRIMARY KEY,
                   role    TEXT NOT NULL DEFAULT 'user'
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS favorites_actions (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id      INTEGER NOT NULL,
                   action_type  TEXT    NOT NULL,
                   payload_json TEXT    NOT NULL DEFAULT '{}',
                   label        TEXT    NOT NULL DEFAULT '',
                   created_at   REAL   NOT NULL
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS snapshots (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id      INTEGER NOT NULL,
                   name         TEXT    NOT NULL,
                   created_at   TEXT    NOT NULL,
                   payload_json TEXT    NOT NULL DEFAULT '[]'
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS schedules (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id      INTEGER NOT NULL,
                   name         TEXT    NOT NULL,
                   action_type  TEXT    NOT NULL,
                   payload_json TEXT    NOT NULL DEFAULT '{}',
                   cron_expr    TEXT    NOT NULL,
                   next_run     REAL   NOT NULL DEFAULT 0,
                   enabled      INTEGER NOT NULL DEFAULT 1,
                   last_run     REAL,
                   last_result  TEXT
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS error_log (
                   id        INTEGER PRIMARY KEY AUTOINCREMENT,
                   timestamp TEXT    NOT NULL,
                   level     TEXT    NOT NULL,
                   module    TEXT    NOT NULL DEFAULT '',
                   message   TEXT    NOT NULL,
                   traceback TEXT
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS mutes (
                   user_id    INTEGER NOT NULL,
                   entity_id  TEXT    NOT NULL,
                   mute_until REAL   NOT NULL,
                   PRIMARY KEY (user_id, entity_id)
               )"""
        )
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS pinned_items (
                   user_id   INTEGER NOT NULL,
                   item_type TEXT    NOT NULL,
                   target_id TEXT    NOT NULL,
                   label     TEXT    NOT NULL DEFAULT '',
                   PRIMARY KEY (user_id, item_type, target_id)
               )"""
        )

    async def load_ui_session(self, chat_id: int, user_id: int, thread_id: int = 0) -> dict:
        assert self._db is not None
        async with self._db.execute(
            "SELECT payload FROM ui_sessions WHERE chat_id=? AND user_id=? AND thread_id=?",
            (chat_id, user_id, thread_id),
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else {}

    async def save_ui_session(self, chat_id: int, user_id: int, thread_id: int, payload: dict) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO ui_sessions VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id,user_id,thread_id) DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (chat_id, user_id, thread_id, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        await self._db.commit()

    async def find_widget_session(self, chat_id: int, user_id: int, message_id: int) -> tuple[int, dict] | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT thread_id, payload FROM ui_sessions WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
        for thread, raw in rows:
            data = json.loads(raw)
            if data.get("widget_message_id") == message_id:
                return thread, data
        return None

    async def save_notice(self, user_id: int, message_id: int, actions: list[str]) -> None:
        assert self._db is not None
        await self._db.execute("INSERT OR REPLACE INTO ui_notices VALUES (?, ?, ?, ?)",
                               (user_id, message_id, json.dumps(actions), time.time()))
        await self._db.execute("DELETE FROM ui_notices WHERE created_at < ?", (time.time() - 86400 * 7,))
        await self._db.commit()

    async def notice_allows(self, user_id: int, message_id: int, action: str) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "SELECT actions FROM ui_notices WHERE user_id=? AND message_id=? AND created_at>?",
            (user_id, message_id, time.time() - 86400 * 7),
        ) as cur:
            row = await cur.fetchone()
        return bool(row and action in json.loads(row[0]))

    async def get_runtime_value(self, key: str) -> str | None:
        assert self._db is not None
        async with self._db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_runtime_value(self, key: str, value: str) -> None:
        assert self._db is not None
        await self._db.execute("INSERT OR REPLACE INTO runtime_state VALUES (?, ?)", (key, value))
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- cooldown ---

    async def check_and_update_cooldown(
        self, user_id: int, action: str, cooldown_seconds: float
    ) -> tuple[bool, float]:
        assert self._db is not None
        now = time.time()
        async with self._db.execute(
            "INSERT INTO cooldowns(user_id,action,last_used) VALUES (?,?,?) "
            "ON CONFLICT(user_id,action) DO UPDATE SET last_used=excluded.last_used "
            "WHERE cooldowns.last_used <= ?",
            (user_id, action, now, now - cooldown_seconds),
        ) as cur:
            accepted = cur.rowcount == 1
        await self._db.commit()
        if accepted:
            return True, 0.0
        async with self._db.execute(
            "SELECT last_used FROM cooldowns WHERE user_id=? AND action=?", (user_id, action),
        ) as cur:
            row = await cur.fetchone()
        return False, max(0.0, cooldown_seconds - (now - row[0]))

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
        assert self._db is not None
        async with self._db.execute(
            "SELECT message_id FROM menu_state WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_menu_state(self, chat_id: int) -> dict[str, Any] | None:
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
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM menu_state WHERE chat_id = ?", (chat_id,)
        )
        await self._db.commit()

    # --- favorites ---

    async def get_favorites(self, user_id: int) -> list[str]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT entity_id FROM favorites WHERE user_id = ? ORDER BY entity_id",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def is_favorite(self, user_id: int, entity_id: str) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND entity_id = ?",
            (user_id, entity_id),
        ) as cur:
            return (await cur.fetchone()) is not None

    async def toggle_favorite(self, user_id: int, entity_id: str) -> bool:
        """Toggle favorite. Returns True if now favorited, False if removed."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND entity_id = ?",
            (user_id, entity_id),
        ) as cur:
            exists = (await cur.fetchone()) is not None
        if exists:
            await self._db.execute(
                "DELETE FROM favorites WHERE user_id = ? AND entity_id = ?",
                (user_id, entity_id),
            )
            await self._db.commit()
            return False
        else:
            await self._db.execute(
                "INSERT INTO favorites (user_id, entity_id) VALUES (?, ?)",
                (user_id, entity_id),
            )
            await self._db.commit()
            return True

    # --- entity_area_cache ---

    async def cache_entity_area(
        self,
        entity_id: str,
        area_id: str | None,
        area_name: str | None,
        floor_id: str | None,
        floor_name: str | None,
        device_id: str | None,
    ) -> None:
        assert self._db is not None
        now = time.time()
        await self._db.execute(
            "INSERT INTO entity_area_cache "
            "(entity_id, area_id, area_name, floor_id, floor_name, device_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET "
            "area_id=excluded.area_id, area_name=excluded.area_name, "
            "floor_id=excluded.floor_id, floor_name=excluded.floor_name, "
            "device_id=excluded.device_id, updated_at=excluded.updated_at",
            (entity_id, area_id, area_name, floor_id, floor_name, device_id, now),
        )

    async def flush_entity_area_cache(self) -> None:
        assert self._db is not None
        await self._db.execute("DELETE FROM entity_area_cache")
        await self._db.commit()

    async def commit_entity_area_cache(self) -> None:
        assert self._db is not None
        await self._db.commit()

    async def get_entity_area(self, entity_id: str) -> dict[str, Any] | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT area_id, area_name, floor_id, floor_name, device_id "
            "FROM entity_area_cache WHERE entity_id = ?",
            (entity_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "area_id": row[0],
            "area_name": row[1],
            "floor_id": row[2],
            "floor_name": row[3],
            "device_id": row[4],
        }

    # --- vacuum_room_map ---

    async def save_vacuum_room_map(
        self, vacuum_entity_id: str, segments: list[dict[str, Any]]
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM vacuum_room_map WHERE vacuum_entity_id = ?",
            (vacuum_entity_id,),
        )
        for seg in segments:
            await self._db.execute(
                "INSERT INTO vacuum_room_map (vacuum_entity_id, segment_id, segment_name, area_id) "
                "VALUES (?, ?, ?, ?)",
                (vacuum_entity_id, str(seg["segment_id"]), seg.get("segment_name", ""), seg.get("area_id")),
            )
        await self._db.commit()

    async def get_vacuum_room_map(self, vacuum_entity_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT segment_id, segment_name, area_id FROM vacuum_room_map "
            "WHERE vacuum_entity_id = ? ORDER BY segment_name",
            (vacuum_entity_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"segment_id": r[0], "segment_name": r[1], "area_id": r[2]}
            for r in rows
        ]

    # --- notifications ---

    async def get_notification(self, user_id: int, entity_id: str) -> dict[str, Any] | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT enabled, mode, throttle_seconds, last_sent_ts "
            "FROM notifications WHERE user_id = ? AND entity_id = ?",
            (user_id, entity_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "enabled": bool(row[0]),
            "mode": row[1],
            "throttle_seconds": row[2],
            "last_sent_ts": row[3],
        }

    async def toggle_notification(self, user_id: int, entity_id: str) -> bool:
        """Toggle notification. Returns True if now enabled."""
        assert self._db is not None
        existing = await self.get_notification(user_id, entity_id)
        if existing is None:
            await self._db.execute(
                "INSERT INTO notifications (user_id, entity_id, enabled, mode, throttle_seconds, last_sent_ts) "
                "VALUES (?, ?, 1, 'state_only', 60, 0)",
                (user_id, entity_id),
            )
            await self._db.commit()
            return True
        new_enabled = 0 if existing["enabled"] else 1
        await self._db.execute(
            "UPDATE notifications SET enabled = ? WHERE user_id = ? AND entity_id = ?",
            (new_enabled, user_id, entity_id),
        )
        await self._db.commit()
        return bool(new_enabled)

    async def set_notification_mode(self, user_id: int, entity_id: str, mode: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE notifications SET mode = ? WHERE user_id = ? AND entity_id = ?",
            (mode, user_id, entity_id),
        )
        await self._db.commit()

    async def get_all_active_notifications(self) -> list[dict[str, Any]]:
        """Get all enabled notifications for the state_changed listener."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT user_id, entity_id, mode, throttle_seconds, last_sent_ts "
            "FROM notifications WHERE enabled = 1"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "user_id": r[0],
                "entity_id": r[1],
                "mode": r[2],
                "throttle_seconds": r[3],
                "last_sent_ts": r[4],
            }
            for r in rows
        ]

    async def update_notification_sent(self, user_id: int, entity_id: str) -> None:
        assert self._db is not None
        now = time.time()
        await self._db.execute(
            "UPDATE notifications SET last_sent_ts = ? WHERE user_id = ? AND entity_id = ?",
            (now, user_id, entity_id),
        )
        await self._db.commit()

    async def get_user_notifications(self, user_id: int) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT entity_id, enabled, mode, throttle_seconds "
            "FROM notifications WHERE user_id = ? ORDER BY entity_id",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"entity_id": r[0], "enabled": bool(r[1]), "mode": r[2], "throttle_seconds": r[3]}
            for r in rows
        ]

    # --- rooms ---

    async def upsert_room(self, canonical_name: str, ha_area_id: str | None, aliases: list[str]) -> None:
        assert self._db is not None
        aliases_json = json.dumps(aliases, ensure_ascii=False)
        async with self._db.execute(
            "SELECT id FROM rooms WHERE canonical_name = ?", (canonical_name,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await self._db.execute(
                "UPDATE rooms SET ha_area_id = ?, aliases_json = ? WHERE id = ?",
                (ha_area_id, aliases_json, row[0]),
            )
        else:
            await self._db.execute(
                "INSERT INTO rooms (canonical_name, ha_area_id, aliases_json) VALUES (?, ?, ?)",
                (canonical_name, ha_area_id, aliases_json),
            )
        await self._db.commit()

    async def get_rooms(self) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, canonical_name, ha_area_id, aliases_json FROM rooms ORDER BY canonical_name"
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            try:
                aliases = json.loads(r[3])
            except (json.JSONDecodeError, TypeError):
                aliases = []
            result.append({
                "id": r[0],
                "canonical_name": r[1],
                "ha_area_id": r[2],
                "aliases": aliases,
            })
        return result

    # --- user_roles ---

    async def get_user_role(self, user_id: int) -> str:
        assert self._db is not None
        async with self._db.execute(
            "SELECT role FROM user_roles WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else "user"

    async def set_user_role(self, user_id: int, role: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO user_roles (user_id, role) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET role = excluded.role",
            (user_id, role),
        )
        await self._db.commit()

    async def get_all_roles(self) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT user_id, role FROM user_roles ORDER BY user_id"
        ) as cur:
            rows = await cur.fetchall()
        return [{"user_id": r[0], "role": r[1]} for r in rows]

    # --- favorites_actions ---

    async def add_favorite_action(
        self, user_id: int, action_type: str, payload: dict[str, Any], label: str,
    ) -> int:
        assert self._db is not None
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False)
        async with self._db.execute(
            "INSERT INTO favorites_actions (user_id, action_type, payload_json, label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, action_type, payload_json, label, now),
        ) as cur:
            row_id = cur.lastrowid
        await self._db.commit()
        return row_id or 0

    async def get_favorite_actions(self, user_id: int) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, action_type, payload_json, label, created_at "
            "FROM favorites_actions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            try:
                payload = json.loads(r[2])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            result.append({
                "id": r[0], "action_type": r[1], "payload": payload,
                "label": r[3], "created_at": r[4],
            })
        return result

    async def remove_favorite_action(self, user_id: int, action_id: int) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "DELETE FROM favorites_actions WHERE id = ? AND user_id = ?",
            (action_id, user_id),
        ) as cur:
            deleted = cur.rowcount > 0
        await self._db.commit()
        return deleted

    # --- snapshots ---

    async def save_snapshot(
        self, user_id: int, name: str, payload: list[dict[str, Any]],
    ) -> int:
        assert self._db is not None
        ts = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False)
        async with self._db.execute(
            "INSERT INTO snapshots (user_id, name, created_at, payload_json) VALUES (?, ?, ?, ?)",
            (user_id, name, ts, payload_json),
        ) as cur:
            row_id = cur.lastrowid
        await self._db.commit()
        return row_id or 0

    async def get_snapshots(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, name, created_at, payload_json FROM snapshots "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            try:
                payload = json.loads(r[3])
            except (json.JSONDecodeError, TypeError):
                payload = []
            result.append({
                "id": r[0], "name": r[1], "created_at": r[2], "payload": payload,
            })
        return result

    async def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, user_id, name, created_at, payload_json FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[4])
        except (json.JSONDecodeError, TypeError):
            payload = []
        return {
            "id": row[0], "user_id": row[1], "name": row[2],
            "created_at": row[3], "payload": payload,
        }

    async def delete_snapshot(self, snapshot_id: int, user_id: int) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "DELETE FROM snapshots WHERE id = ? AND user_id = ?",
            (snapshot_id, user_id),
        ) as cur:
            deleted = cur.rowcount > 0
        await self._db.commit()
        return deleted

    # --- schedules ---

    async def add_schedule(
        self, user_id: int, name: str, action_type: str,
        payload: dict[str, Any], cron_expr: str, next_run: float,
    ) -> int:
        assert self._db is not None
        payload_json = json.dumps(payload, ensure_ascii=False)
        async with self._db.execute(
            "INSERT INTO schedules (user_id, name, action_type, payload_json, cron_expr, next_run) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, action_type, payload_json, cron_expr, next_run),
        ) as cur:
            row_id = cur.lastrowid
        await self._db.commit()
        return row_id or 0

    async def get_schedules(self, user_id: int | None = None) -> list[dict[str, Any]]:
        assert self._db is not None
        if user_id is not None:
            sql = ("SELECT id, user_id, name, action_type, payload_json, cron_expr, "
                   "next_run, enabled, last_run, last_result "
                   "FROM schedules WHERE user_id = ? ORDER BY next_run")
            params: tuple[Any, ...] = (user_id,)
        else:
            sql = ("SELECT id, user_id, name, action_type, payload_json, cron_expr, "
                   "next_run, enabled, last_run, last_result "
                   "FROM schedules WHERE enabled = 1 ORDER BY next_run")
            params = ()
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            try:
                payload = json.loads(r[4])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            result.append({
                "id": r[0], "user_id": r[1], "name": r[2], "action_type": r[3],
                "payload": payload, "cron_expr": r[5], "next_run": r[6],
                "enabled": bool(r[7]), "last_run": r[8], "last_result": r[9],
            })
        return result

    async def get_due_schedules(self, now: float) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, user_id, name, action_type, payload_json, cron_expr, next_run "
            "FROM schedules WHERE enabled = 1 AND next_run <= ? ORDER BY next_run",
            (now,),
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            try:
                payload = json.loads(r[4])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            result.append({
                "id": r[0], "user_id": r[1], "name": r[2], "action_type": r[3],
                "payload": payload, "cron_expr": r[5], "next_run": r[6],
            })
        return result

    async def update_schedule_run(
        self, schedule_id: int, next_run: float, result_text: str,
    ) -> None:
        assert self._db is not None
        now = time.time()
        await self._db.execute(
            "UPDATE schedules SET last_run = ?, last_result = ?, next_run = ? WHERE id = ?",
            (now, result_text, next_run, schedule_id),
        )
        await self._db.commit()

    async def toggle_schedule(self, schedule_id: int, user_id: int) -> bool | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT enabled FROM schedules WHERE id = ? AND user_id = ?",
            (schedule_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        new_val = 0 if row[0] else 1
        await self._db.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ?",
            (new_val, schedule_id),
        )
        await self._db.commit()
        return bool(new_val)

    async def delete_schedule(self, schedule_id: int, user_id: int) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "DELETE FROM schedules WHERE id = ? AND user_id = ?",
            (schedule_id, user_id),
        ) as cur:
            deleted = cur.rowcount > 0
        await self._db.commit()
        return deleted

    # --- error_log ---

    _ERROR_LOG_MAX = 200

    async def log_error(
        self, level: str, module: str, message: str, traceback_str: str | None = None,
    ) -> None:
        assert self._db is not None
        ts = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO error_log (timestamp, level, module, message, traceback) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, level, module, message, traceback_str),
        )
        # Trim ring buffer
        await self._db.execute(
            "DELETE FROM error_log WHERE id NOT IN "
            "(SELECT id FROM error_log ORDER BY id DESC LIMIT ?)",
            (self._ERROR_LOG_MAX,),
        )
        await self._db.commit()

    async def get_recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, timestamp, level, module, message, traceback "
            "FROM error_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "timestamp": r[1], "level": r[2],
                "module": r[3], "message": r[4], "traceback": r[5],
            }
            for r in rows
        ]

    # --- mutes ---

    async def set_mute(self, user_id: int, entity_id: str, mute_until: float) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO mutes (user_id, entity_id, mute_until) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, entity_id) DO UPDATE SET mute_until = excluded.mute_until",
            (user_id, entity_id, mute_until),
        )
        await self._db.commit()

    async def is_muted(self, user_id: int, entity_id: str) -> bool:
        assert self._db is not None
        now = time.time()
        async with self._db.execute(
            "SELECT mute_until FROM mutes WHERE user_id = ? AND entity_id = ?",
            (user_id, entity_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        if row[0] <= now:
            # Mute expired, clean up
            await self._db.execute(
                "DELETE FROM mutes WHERE user_id = ? AND entity_id = ?",
                (user_id, entity_id),
            )
            await self._db.commit()
            return False
        return True

    async def remove_mute(self, user_id: int, entity_id: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM mutes WHERE user_id = ? AND entity_id = ?",
            (user_id, entity_id),
        )
        await self._db.commit()

    async def get_user_mutes(self, user_id: int) -> list[dict[str, Any]]:
        assert self._db is not None
        now = time.time()
        # Clean expired mutes first
        await self._db.execute(
            "DELETE FROM mutes WHERE user_id = ? AND mute_until <= ?",
            (user_id, now),
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT entity_id, mute_until FROM mutes WHERE user_id = ? ORDER BY entity_id",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"entity_id": r[0], "mute_until": r[1]} for r in rows]

    # --- pinned_items (area/routine favorites) ---

    async def add_pinned_item(
        self, user_id: int, item_type: str, target_id: str, label: str = "",
    ) -> bool:
        """Pin an item (area, routine). Returns True if newly added."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM pinned_items WHERE user_id = ? AND item_type = ? AND target_id = ?",
            (user_id, item_type, target_id),
        ) as cur:
            exists = (await cur.fetchone()) is not None
        if exists:
            return False
        await self._db.execute(
            "INSERT INTO pinned_items (user_id, item_type, target_id, label) VALUES (?, ?, ?, ?)",
            (user_id, item_type, target_id, label),
        )
        await self._db.commit()
        return True

    async def remove_pinned_item(
        self, user_id: int, item_type: str, target_id: str,
    ) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "DELETE FROM pinned_items WHERE user_id = ? AND item_type = ? AND target_id = ?",
            (user_id, item_type, target_id),
        ) as cur:
            deleted = cur.rowcount > 0
        await self._db.commit()
        return deleted

    async def toggle_pinned_item(
        self, user_id: int, item_type: str, target_id: str, label: str = "",
    ) -> bool:
        """Toggle pin. Returns True if now pinned."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM pinned_items WHERE user_id = ? AND item_type = ? AND target_id = ?",
            (user_id, item_type, target_id),
        ) as cur:
            exists = (await cur.fetchone()) is not None
        if exists:
            await self._db.execute(
                "DELETE FROM pinned_items WHERE user_id = ? AND item_type = ? AND target_id = ?",
                (user_id, item_type, target_id),
            )
            await self._db.commit()
            return False
        await self._db.execute(
            "INSERT INTO pinned_items (user_id, item_type, target_id, label) VALUES (?, ?, ?, ?)",
            (user_id, item_type, target_id, label),
        )
        await self._db.commit()
        return True

    async def is_pinned(self, user_id: int, item_type: str, target_id: str) -> bool:
        assert self._db is not None
        async with self._db.execute(
            "SELECT 1 FROM pinned_items WHERE user_id = ? AND item_type = ? AND target_id = ?",
            (user_id, item_type, target_id),
        ) as cur:
            return (await cur.fetchone()) is not None

    async def get_pinned_items(
        self, user_id: int, item_type: str | None = None,
    ) -> list[dict[str, Any]]:
        assert self._db is not None
        if item_type:
            sql = "SELECT item_type, target_id, label FROM pinned_items WHERE user_id = ? AND item_type = ? ORDER BY label"
            params: tuple[Any, ...] = (user_id, item_type)
        else:
            sql = "SELECT item_type, target_id, label FROM pinned_items WHERE user_id = ? ORDER BY item_type, label"
            params = (user_id,)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [{"item_type": r[0], "target_id": r[1], "label": r[2]} for r in rows]

    # --- export/import helpers ---

    async def export_user_settings(self, user_id: int) -> dict[str, Any]:
        """Export all user-specific settings for backup."""
        favorites = await self.get_favorites(user_id)
        fav_actions = await self.get_favorite_actions(user_id)
        notifs = await self.get_user_notifications(user_id)
        role = await self.get_user_role(user_id)
        schedules = await self.get_schedules(user_id)
        pinned = await self.get_pinned_items(user_id)
        return {
            "user_id": user_id,
            "role": role,
            "favorites": favorites,
            "favorite_actions": fav_actions,
            "notifications": notifs,
            "pinned_items": pinned,
            "schedules": [
                {"name": s["name"], "action_type": s["action_type"],
                 "payload": s["payload"], "cron_expr": s["cron_expr"],
                 "enabled": s["enabled"]}
                for s in schedules
            ],
        }

    async def import_user_settings(self, user_id: int, data: dict[str, Any]) -> int:
        """Merge a bounded backup; never import identity/roles or activate schedules."""
        from actions import ENTITY_ID, validate_service
        from scheduler import validate_cron, next_cron_time
        assert self._db is not None
        count = 0
        role = await self.get_user_role(user_id)
        if role not in {"admin", "user"}:
            return 0
        def items(key):
            value = data.get(key, [])
            return value[:1000] if isinstance(value, list) else []
        for eid in items("favorites"):
            if isinstance(eid, str) and ENTITY_ID.fullmatch(eid) and not await self.is_favorite(user_id, eid):
                await self._db.execute("INSERT OR IGNORE INTO favorites (user_id, entity_id) VALUES (?, ?)", (user_id, eid))
                count += 1
        for notif in items("notifications"):
            if not isinstance(notif, dict):
                continue
            eid = notif.get("entity_id")
            if not isinstance(eid, str) or not ENTITY_ID.fullmatch(eid) or await self.get_notification(user_id, eid):
                continue
            mode = notif.get("mode", "state_only")
            if mode not in {"state_only", "state_and_key_attrs"}:
                continue
            throttle = notif.get("throttle_seconds", 60)
            throttle = max(10, min(86400, throttle)) if type(throttle) is int else 60
            await self._db.execute(
                "INSERT INTO notifications (user_id, entity_id, enabled, mode, throttle_seconds, last_sent_ts) VALUES (?, ?, ?, ?, ?, 0)",
                (user_id, eid, int(bool(notif.get("enabled", True))), mode, throttle))
            count += 1
        for pin in items("pinned_items"):
            if isinstance(pin, dict) and pin.get("item_type") in {"area", "routine"} and isinstance(pin.get("target_id"), str):
                count += bool(await self.add_pinned_item(user_id, pin["item_type"], pin["target_id"], str(pin.get("label", ""))[:200]))
        existing_actions = await self.get_favorite_actions(user_id)
        existing_schedules = await self.get_schedules(user_id)
        for key in ("favorite_actions", "schedules"):
            for item in items(key):
                if not isinstance(item, dict) or item.get("action_type") not in {"service_call", "ha_automation"}:
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                if item["action_type"] == "ha_automation":
                    payload = {"domain": "automation", "service": "trigger", "data": {"entity_id": payload.get("entity_id"), "skip_condition": False}}
                if validate_service(payload.get("domain"), payload.get("service"), payload.get("data"), role=role):
                    continue
                if key == "favorite_actions":
                    label = str(item.get("label", "Действие"))[:200]
                    if any(a["payload"] == payload and a["label"] == label for a in existing_actions):
                        continue
                    await self.add_favorite_action(user_id, "service_call", payload, label)
                    existing_actions.append({"payload": payload, "label": label})
                else:
                    cron, name = item.get("cron_expr"), str(item.get("name", "Расписание"))[:200]
                    if not isinstance(cron, str) or validate_cron(cron):
                        continue
                    if any(a["payload"] == payload and a["name"] == name and a["cron_expr"] == cron for a in existing_schedules):
                        continue
                    schedule_id = await self.add_schedule(user_id, name, "service_call", payload, cron, next_cron_time(cron))
                    await self.toggle_schedule(schedule_id, user_id)  # Imported schedules are always disabled.
                    existing_schedules.append({"payload": payload, "name": name, "cron_expr": cron})
                count += 1
        await self._db.commit()
        return count
