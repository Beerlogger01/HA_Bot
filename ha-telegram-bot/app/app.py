#!/usr/bin/env python3
"""
Home Assistant Telegram Bot Add-on.

Secure Telegram bot for controlling Home Assistant via Supervisor proxy API.

Production-hardened version:
- Single persistent SQLite connection with WAL mode
- Atomic cooldown checks (UPSERT)
- HA API retry with exponential back-off and timeouts
- Structured JSON logs (never leaks tokens)
- Graceful SIGTERM handling (via aiogram)
- Guard against from_user=None, inaccessible messages, Telegram API errors
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
DB_PATH = DATA_DIR / "bot.sqlite3"

HA_BASE_URL = "http://supervisor/core/api"
HA_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10, sock_read=20)
MAX_RETRIES = 3
RETRY_BACKOFF_BASE: float = 1.0  # seconds

KNOWN_ACTIONS: frozenset[str] = frozenset(
    {"light_on", "light_off", "vacuum_start", "vacuum_dock", "scene_goodnight"}
)

# ---------------------------------------------------------------------------
# Logging — structured JSON on stdout
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge structured extras added via `extra={…}`
        for key in ("chat_id", "user_id", "username", "action", "ok", "error_detail"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return logging.getLogger("ha_bot")


logger = _setup_logging()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Config:
    """Validated, immutable add-on configuration.

    supervisor_token is intentionally kept out of this dataclass so it can
    never be serialised or logged by accident when the config is printed.
    """

    bot_token: str
    allowed_chat_id: int
    allowed_user_ids: frozenset[int]
    cooldown_seconds: int
    global_rate_limit_actions: int
    global_rate_limit_window: int
    status_entities: tuple[str, ...]
    light_entity_id: str
    vacuum_entity_id: str
    goodnight_scene_id: str


def _load_and_validate_config() -> tuple[Config, str]:
    """Load ``/data/options.json`` and ``SUPERVISOR_TOKEN``.

    Returns ``(config, supervisor_token)``.
    Exits the process with a clear log message on any validation failure.
    """

    # --- options.json ---
    if not OPTIONS_PATH.exists():
        logger.critical("Configuration file not found: %s", OPTIONS_PATH)
        sys.exit(1)

    try:
        raw: dict[str, Any] = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.critical("Cannot read %s: %s", OPTIONS_PATH, exc)
        sys.exit(1)

    # -- required --
    bot_token = raw.get("bot_token", "")
    if not isinstance(bot_token, str) or not bot_token.strip():
        logger.critical("bot_token is missing or empty")
        sys.exit(1)

    allowed_chat_id = raw.get("allowed_chat_id", 0)
    if not isinstance(allowed_chat_id, int):
        logger.critical("allowed_chat_id must be an integer")
        sys.exit(1)
    if allowed_chat_id == 0:
        logger.warning(
            "allowed_chat_id is 0 — bot will reject every message until configured"
        )

    user_ids_raw = raw.get("allowed_user_ids", [])
    if not isinstance(user_ids_raw, list) or not all(
        isinstance(i, int) for i in user_ids_raw
    ):
        logger.critical("allowed_user_ids must be a list of integers")
        sys.exit(1)
    if not user_ids_raw:
        logger.warning("allowed_user_ids is empty — all actions will be denied")

    # -- optional with defaults --
    cooldown = raw.get("cooldown_seconds", 2)
    if not isinstance(cooldown, int) or cooldown < 0:
        logger.critical("cooldown_seconds must be a non-negative integer")
        sys.exit(1)

    rate_actions = raw.get("global_rate_limit_actions", 10)
    rate_window = raw.get("global_rate_limit_window", 5)
    if not isinstance(rate_actions, int) or rate_actions < 1:
        logger.critical("global_rate_limit_actions must be >= 1")
        sys.exit(1)
    if not isinstance(rate_window, int) or rate_window < 1:
        logger.critical("global_rate_limit_window must be >= 1")
        sys.exit(1)

    status_raw = raw.get("status_entities", [])
    if not isinstance(status_raw, list):
        status_raw = []

    light = raw.get("light_entity_id", "") or ""
    vacuum = raw.get("vacuum_entity_id", "") or ""
    scene = raw.get("goodnight_scene_id", "") or ""

    # entity_id format: must contain a dot
    for label, eid in [
        ("light_entity_id", light),
        ("vacuum_entity_id", vacuum),
        ("goodnight_scene_id", scene),
    ]:
        if eid and "." not in eid:
            logger.critical(
                "%s='%s' is not a valid entity_id (expected 'domain.name')", label, eid
            )
            sys.exit(1)

    for eid in status_raw:
        if not isinstance(eid, str) or "." not in eid:
            logger.critical("Invalid entity_id in status_entities: '%s'", eid)
            sys.exit(1)

    # --- SUPERVISOR_TOKEN ---
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        logger.critical("SUPERVISOR_TOKEN environment variable is not set")
        sys.exit(1)

    config = Config(
        bot_token=bot_token.strip(),
        allowed_chat_id=allowed_chat_id,
        allowed_user_ids=frozenset(user_ids_raw),
        cooldown_seconds=cooldown,
        global_rate_limit_actions=rate_actions,
        global_rate_limit_window=rate_window,
        status_entities=tuple(status_raw),
        light_entity_id=light,
        vacuum_entity_id=vacuum,
        goodnight_scene_id=scene,
    )
    return config, supervisor_token


# ---------------------------------------------------------------------------
# Database — single persistent connection, WAL mode, atomic cooldown
# ---------------------------------------------------------------------------


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
                   success   INTEGER NOT NULL,
                   error     TEXT
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

        Uses a single persistent connection, so all operations are serialised
        through aiosqlite's background thread — no race between SELECT and
        UPSERT.

        Returns ``(is_allowed, remaining_seconds)``.
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

        # Allowed — atomically upsert
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
        success: bool,
        error: str | None = None,
    ) -> None:
        assert self._db is not None
        ts = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO audit (timestamp, chat_id, user_id, username, action, success, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, chat_id, user_id, username, action, 1 if success else 0, error),
        )
        await self._db.commit()


# ---------------------------------------------------------------------------
# Home Assistant API client — retries, timeouts, reused session
# ---------------------------------------------------------------------------


class HAClient:
    """Communicates with Home Assistant Core API through the Supervisor proxy.

    - Reuses a single ``aiohttp.ClientSession``
    - Retries transient errors with exponential back-off
    - Never logs the Supervisor token
    """

    def __init__(self, supervisor_token: str) -> None:
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        self._session = aiohttp.ClientSession(timeout=HA_TIMEOUT)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- internal request with retry --

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        """HTTP request with retry.  Returns ``(success, data_or_error)``."""
        assert self._session is not None
        url = f"{HA_BASE_URL}/{path}"
        last_error = ""

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._session.request(
                    method, url, json=json_data, headers=self._headers
                ) as resp:
                    if resp.status in (200, 201):
                        try:
                            data = await resp.json(content_type=None)
                        except (json.JSONDecodeError, aiohttp.ContentTypeError):
                            data = {}
                        return True, data

                    body = (await resp.text())[:300]
                    last_error = f"HTTP {resp.status}: {body}"

                    # Client errors except 429 — no retry
                    if 400 <= resp.status < 500 and resp.status != 429:
                        logger.error(
                            "HA API client error (no retry): %s %s -> %s",
                            method, path, last_error,
                        )
                        return False, last_error

                    logger.warning(
                        "HA API error (attempt %d/%d): %s %s -> %s",
                        attempt, MAX_RETRIES, method, path, last_error,
                    )
            except asyncio.TimeoutError:
                last_error = "Request timed out"
                logger.warning(
                    "HA API timeout (attempt %d/%d): %s %s",
                    attempt, MAX_RETRIES, method, path,
                )
            except aiohttp.ClientError as exc:
                last_error = f"Connection error: {exc}"
                logger.warning(
                    "HA API connection error (attempt %d/%d): %s %s -> %s",
                    attempt, MAX_RETRIES, method, path, exc,
                )

            if attempt < MAX_RETRIES:
                delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        logger.error(
            "HA API failed after %d attempts: %s %s -> %s",
            MAX_RETRIES, method, path, last_error,
        )
        return False, last_error

    # -- public helpers --

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> tuple[bool, str]:
        """Call an HA service.  Returns ``(success, error_message_or_empty)``."""
        ok, result = await self._request(
            "POST", f"services/{domain}/{service}", json_data=data
        )
        if ok:
            logger.info(
                "Service called: %s.%s entity=%s",
                domain, service, data.get("entity_id", "?"),
            )
            return True, ""
        return False, str(result)

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """Return entity state dict or ``None`` on failure."""
        ok, result = await self._request("GET", f"states/{entity_id}")
        return result if ok and isinstance(result, dict) else None

    async def get_config(self) -> dict[str, Any] | None:
        """Fetch HA config (used for self-test at startup)."""
        ok, result = await self._request("GET", "config")
        return result if ok and isinstance(result, dict) else None


# ---------------------------------------------------------------------------
# In-memory global rate limiter (sliding window)
# ---------------------------------------------------------------------------


class GlobalRateLimiter:
    """Simple in-memory sliding-window counter for the whole group.

    Operates within a single asyncio event loop — no lock required.
    Non-persistent: resets on add-on restart (by design — short window).
    """

    def __init__(self, max_actions: int, window_seconds: int) -> None:
        self._max = max_actions
        self._window = window_seconds
        self._timestamps: list[float] = []

    def check(self) -> bool:
        """Return ``True`` if the action is within the rate limit."""
        now = time.monotonic()
        cutoff = now - self._window
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True


# ---------------------------------------------------------------------------
# Audit helper — logs to both stdout (JSON) and SQLite
# ---------------------------------------------------------------------------


async def _audit(
    db: Database,
    *,
    chat_id: int,
    user_id: int,
    username: str,
    action: str,
    success: bool,
    error: str | None = None,
) -> None:
    logger.info(
        "AUDIT",
        extra={
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "action": action,
            "ok": success,
            "error_detail": error,
        },
    )
    try:
        await db.write_audit(
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            action=action,
            success=success,
            error=error,
        )
    except Exception:
        logger.exception("Failed to persist audit record")


# ---------------------------------------------------------------------------
# Action dispatch table
# ---------------------------------------------------------------------------

# callback_data -> (domain, service, config_attr, success_message)
_ACTION_MAP: dict[str, tuple[str, str, str, str]] = {
    "light_on": ("light", "turn_on", "light_entity_id", "💡 Light turned ON"),
    "light_off": ("light", "turn_off", "light_entity_id", "🌑 Light turned OFF"),
    "vacuum_start": ("vacuum", "start", "vacuum_entity_id", "🤖 Vacuum started"),
    "vacuum_dock": (
        "vacuum",
        "return_to_base",
        "vacuum_entity_id",
        "🏠 Vacuum returning to dock",
    ),
    "scene_goodnight": (
        "scene",
        "turn_on",
        "goodnight_scene_id",
        "🌙 Good Night scene activated",
    ),
}

# ---------------------------------------------------------------------------
# Telegram Bot
# ---------------------------------------------------------------------------


class TelegramBot:
    """Core bot controller — wires handlers, manages lifecycle."""

    def __init__(self, config: Config, supervisor_token: str) -> None:
        self._config = config
        self._bot = Bot(token=config.bot_token)
        self._dp = Dispatcher()
        self._ha = HAClient(supervisor_token)
        self._db = Database(DB_PATH)
        self._global_rl = GlobalRateLimiter(
            config.global_rate_limit_actions,
            config.global_rate_limit_window,
        )
        self._keyboard: InlineKeyboardMarkup | None = None

        # Register handlers
        self._dp.message.register(self._cmd_start, Command("start"))
        self._dp.message.register(self._cmd_status, Command("status"))
        self._dp.callback_query.register(self._handle_callback)

    # -- lifecycle --

    async def run(self) -> None:
        """Open resources, perform self-test, then block on long-polling."""
        await self._db.open()
        await self._ha.open()

        # Self-test
        ha_cfg = await self._ha.get_config()
        if ha_cfg:
            logger.info(
                "HA API self-test passed — version %s",
                ha_cfg.get("version", "unknown"),
            )
        else:
            logger.error(
                "HA API self-test FAILED — check network / SUPERVISOR_TOKEN"
            )

        logger.info("Bot polling started")
        await self._dp.start_polling(
            self._bot,
            allowed_updates=["message", "callback_query"],
        )

    async def shutdown(self) -> None:
        """Release all resources.  Safe to call even if ``run()`` failed."""
        errors: list[str] = []
        for label, coro in [
            ("HA session", self._ha.close()),
            ("database", self._db.close()),
            ("bot session", self._bot.session.close()),
        ]:
            try:
                await coro
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        if errors:
            logger.warning("Shutdown warnings: %s", "; ".join(errors))
        logger.info("Shutdown complete")

    # -- helpers --

    def _build_keyboard(self) -> InlineKeyboardMarkup:
        """Build (and cache) the main inline keyboard from config."""
        if self._keyboard is not None:
            return self._keyboard

        rows: list[list[InlineKeyboardButton]] = []
        c = self._config

        if c.light_entity_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="💡 Light ON", callback_data="light_on"
                    ),
                    InlineKeyboardButton(
                        text="🌑 Light OFF", callback_data="light_off"
                    ),
                ]
            )
        if c.vacuum_entity_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🤖 Vacuum Start", callback_data="vacuum_start"
                    ),
                    InlineKeyboardButton(
                        text="🏠 Vacuum Dock", callback_data="vacuum_dock"
                    ),
                ]
            )
        if c.goodnight_scene_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🌙 Good Night", callback_data="scene_goodnight"
                    ),
                ]
            )

        self._keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        return self._keyboard

    def _is_authorized_chat(self, chat_id: int) -> bool:
        return chat_id == self._config.allowed_chat_id

    def _is_authorized_user(self, user_id: int) -> bool:
        return bool(self._config.allowed_user_ids) and (
            user_id in self._config.allowed_user_ids
        )

    @staticmethod
    def _extract_user(obj: Message | CallbackQuery) -> tuple[int | None, str]:
        """Return ``(user_id, username)`` — safe when ``from_user`` is None."""
        user = obj.from_user
        if user is None:
            return None, "<unknown>"
        name = user.username or user.first_name or str(user.id)
        return user.id, name

    # -- handlers --

    async def _cmd_start(self, message: Message) -> None:
        user_id, username = self._extract_user(message)
        if user_id is None:
            return  # channel post / anonymous
        chat_id = message.chat.id

        if not self._is_authorized_chat(chat_id):
            await _audit(
                self._db,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                action="/start",
                success=False,
                error="Unauthorized chat",
            )
            await message.answer("⛔ Unauthorized chat.")
            return

        await _audit(
            self._db,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            action="/start",
            success=True,
        )

        help_text = (
            "🏠 <b>Home Assistant Bot</b>\n\n"
            "Commands:\n"
            "/start — Help & main menu\n"
            "/status — Entity states\n\n"
            "Use the buttons below to control devices:"
        )
        await message.answer(
            help_text, parse_mode="HTML", reply_markup=self._build_keyboard()
        )

    async def _cmd_status(self, message: Message) -> None:
        user_id, username = self._extract_user(message)
        if user_id is None:
            return
        chat_id = message.chat.id

        if not self._is_authorized_chat(chat_id):
            await _audit(
                self._db,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                action="/status",
                success=False,
                error="Unauthorized chat",
            )
            await message.answer("⛔ Unauthorized chat.")
            return

        await _audit(
            self._db,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            action="/status",
            success=True,
        )

        if not self._config.status_entities:
            await message.answer("No entities configured for status display.")
            return

        lines: list[str] = ["<b>Entity Status:</b>\n"]
        for eid in self._config.status_entities:
            state = await self._ha.get_state(eid)
            if state:
                name = state.get("attributes", {}).get("friendly_name", eid)
                lines.append(
                    f"• {name}: <code>{state.get('state', 'unknown')}</code>"
                )
            else:
                lines.append(f"• {eid}: <code>unavailable</code>")

        await message.answer("\n".join(lines), parse_mode="HTML")

    async def _handle_callback(self, callback: CallbackQuery) -> None:
        # Guard: inaccessible / expired message
        if callback.message is None:
            await callback.answer("Message expired.", show_alert=True)
            return

        chat_id = callback.message.chat.id
        user_id, username = self._extract_user(callback)
        if user_id is None:
            await callback.answer("Cannot identify user.", show_alert=True)
            return

        action = callback.data or ""

        # Validate callback_data
        if action not in KNOWN_ACTIONS:
            await callback.answer("Unknown action.", show_alert=True)
            return

        # --- authorisation ---
        if not self._is_authorized_chat(chat_id):
            await _audit(
                self._db,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                action=action,
                success=False,
                error="Unauthorized chat",
            )
            await callback.answer("⛔ Unauthorized chat.", show_alert=True)
            return

        if not self._is_authorized_user(user_id):
            await _audit(
                self._db,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                action=action,
                success=False,
                error="Unauthorized user",
            )
            await callback.answer(
                "⛔ You are not authorized.", show_alert=True
            )
            return

        # --- rate limits (global FIRST, then per-user cooldown) ---
        if not self._global_rl.check():
            await _audit(
                self._db,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                action=action,
                success=False,
                error="Global rate limit",
            )
            await callback.answer(
                "🚦 Rate limit reached. Wait a moment.", show_alert=True
            )
            return

        allowed, remaining = await self._db.check_and_update_cooldown(
            user_id, action, self._config.cooldown_seconds
        )
        if not allowed:
            await callback.answer(
                f"⏱️ Wait {remaining:.1f}s.", show_alert=True
            )
            return

        # --- execute ---
        success, err_msg = await self._execute_action(action)
        await _audit(
            self._db,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            action=action,
            success=success,
            error=err_msg if not success else None,
        )

        if success:
            _, _, _, ok_msg = _ACTION_MAP[action]
            await callback.answer(ok_msg)
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=self._build_keyboard()
                )
            except (TelegramBadRequest, TelegramRetryAfter):
                pass  # message too old or Telegram rate-limited
        else:
            await callback.answer(
                f"Error: {err_msg[:180]}", show_alert=True
            )

    async def _execute_action(self, action: str) -> tuple[bool, str]:
        """Dispatch to HA service call.  Returns ``(success, error_or_empty)``."""
        entry = _ACTION_MAP.get(action)
        if entry is None:
            return False, f"Unknown action: {action}"

        domain, service, config_attr, _ = entry
        entity_id: str = getattr(self._config, config_attr, "")
        if not entity_id:
            return False, f"{config_attr} not configured"

        return await self._ha.call_service(
            domain, service, {"entity_id": entity_id}
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    logger.info("Loading configuration…")
    config, supervisor_token = _load_and_validate_config()

    bot = TelegramBot(config, supervisor_token)
    try:
        await bot.run()  # blocks until SIGTERM / SIGINT (handled by aiogram)
    except asyncio.CancelledError:
        logger.info("Cancelled — shutting down")
    except Exception:
        logger.exception("Fatal error")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
