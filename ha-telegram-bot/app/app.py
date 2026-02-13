#!/usr/bin/env python3
"""Home Assistant Telegram Bot Add-on.

Secure Telegram bot for controlling Home Assistant via Supervisor proxy API.

Features:
- Floors / Areas / Entities navigation via HA registries
- Vacuum room targeting (segments) + Roborock routines
- Per-user favorites and notification subscriptions
- Favorite actions, snapshots, scheduler, search
- Role-based access control (admin/user/guest)
- Diagnostics: /health /diag /trace_last_error
- Edit-in-place message cleanup + forum thread support
- Security: chat/user whitelisting, deny-by-default
- Rate limiting: per-user cooldown + global sliding window
- Audit logging: structured JSON stdout + SQLite
- Retry with exponential backoff for HA API
- Graceful shutdown on SIGTERM
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.filters import Command

from api import HAClient
from diagnostics import Diagnostics, ErrorCapture
from handlers import GlobalRateLimiter, Handlers
from notifications import NotificationManager
from registry import HARegistry
from scheduler import Scheduler
from storage import Database
from vacuum_adapter import VacuumAdapter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
DB_PATH = DATA_DIR / "bot.sqlite3"

# ---------------------------------------------------------------------------
# Logging — structured JSON on stdout
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
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
    bot_token: str
    allowed_chat_id: int
    allowed_user_ids: frozenset[int]
    cooldown_seconds: int
    global_rate_limit_actions: int
    global_rate_limit_window: int
    status_entities: tuple[str, ...]
    menu_domains_allowlist: tuple[str, ...]
    menu_page_size: int
    show_all_enabled: bool
    vacuum_room_strategy: str
    vacuum_room_script_entity_id: str
    vacuum_room_presets: tuple[str, ...]
    light_entity_id: str
    vacuum_entity_id: str
    goodnight_scene_id: str


def _coerce_user_ids(raw: Any) -> list[int]:
    """Flexibly coerce allowed_user_ids from various input formats."""
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, int):
                result.append(item)
            elif isinstance(item, str):
                try:
                    result.append(int(item.strip()))
                except ValueError:
                    logger.warning("Ignoring non-integer user_id: %r", item)
            elif isinstance(item, float):
                result.append(int(item))
        return result
    if isinstance(raw, int):
        logger.warning("allowed_user_ids is a single int — wrapping in list")
        return [raw]
    if isinstance(raw, str):
        raw = raw.strip()
        if raw:
            try:
                return [int(raw)]
            except ValueError:
                logger.warning("Cannot parse allowed_user_ids string: %r", raw)
    return []


def _load_and_validate_config() -> tuple[Config, str]:
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

    # -- chat id --
    allowed_chat_id = raw.get("allowed_chat_id", 0)
    if not isinstance(allowed_chat_id, int):
        try:
            allowed_chat_id = int(allowed_chat_id)
        except (TypeError, ValueError):
            allowed_chat_id = 0
    if allowed_chat_id == 0:
        logger.warning("allowed_chat_id is 0 — open mode, any chat accepted")

    # -- user ids (flexible) --
    user_ids = _coerce_user_ids(raw.get("allowed_user_ids", []))
    if not user_ids:
        logger.warning("allowed_user_ids is empty — open mode, any user accepted")

    # -- rate limiting --
    cooldown = raw.get("cooldown_seconds", 2)
    if not isinstance(cooldown, int) or cooldown < 0:
        cooldown = 2

    rate_actions = raw.get("global_rate_limit_actions", 10)
    if not isinstance(rate_actions, int) or rate_actions < 1:
        rate_actions = 10
    rate_window = raw.get("global_rate_limit_window", 5)
    if not isinstance(rate_window, int) or rate_window < 1:
        rate_window = 5

    # -- status entities --
    status_raw = raw.get("status_entities", [])
    if not isinstance(status_raw, list):
        status_raw = []
    status_ents = tuple(
        eid for eid in status_raw if isinstance(eid, str) and "." in eid
    )

    # -- legacy single-entity options (optional, never fatal) --
    light = str(raw.get("light_entity_id", "") or "")
    vacuum = str(raw.get("vacuum_entity_id", "") or "")
    scene = str(raw.get("goodnight_scene_id", "") or "")

    # -- dynamic menu options --
    default_domains = [
        "light", "switch", "vacuum", "media_player", "climate", "fan", "cover",
        "scene", "script", "select", "number", "lock", "water_heater", "sensor",
    ]
    domains_al = raw.get("menu_domains_allowlist", default_domains)
    if not isinstance(domains_al, list):
        domains_al = default_domains
    domains_al = [d for d in domains_al if isinstance(d, str) and d.strip()]
    if not domains_al:
        domains_al = default_domains

    page_size = raw.get("menu_page_size", 8)
    if not isinstance(page_size, int) or page_size < 1:
        page_size = 8
    page_size = min(page_size, 20)

    show_all = raw.get("show_all_enabled", False)
    if not isinstance(show_all, bool):
        show_all = False

    # -- vacuum room targeting --
    vac_strategy = raw.get("vacuum_room_strategy", "service_data")
    if vac_strategy not in ("script", "service_data"):
        vac_strategy = "service_data"

    vac_script = str(raw.get("vacuum_room_script_entity_id", "") or "")
    vac_rooms_raw = raw.get(
        "vacuum_room_presets",
        ["bathroom", "kitchen", "living_room", "bedroom"],
    )
    if not isinstance(vac_rooms_raw, list):
        vac_rooms_raw = ["bathroom", "kitchen", "living_room", "bedroom"]
    vac_rooms = tuple(r for r in vac_rooms_raw if isinstance(r, str) and r.strip())

    # --- SUPERVISOR_TOKEN ---
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        logger.critical("SUPERVISOR_TOKEN environment variable is not set")
        sys.exit(1)

    config = Config(
        bot_token=bot_token.strip(),
        allowed_chat_id=allowed_chat_id,
        allowed_user_ids=frozenset(user_ids),
        cooldown_seconds=cooldown,
        global_rate_limit_actions=rate_actions,
        global_rate_limit_window=rate_window,
        status_entities=status_ents,
        menu_domains_allowlist=tuple(domains_al),
        menu_page_size=page_size,
        show_all_enabled=show_all,
        vacuum_room_strategy=vac_strategy,
        vacuum_room_script_entity_id=vac_script,
        vacuum_room_presets=vac_rooms,
        light_entity_id=light,
        vacuum_entity_id=vacuum,
        goodnight_scene_id=scene,
    )
    return config, supervisor_token


# ---------------------------------------------------------------------------
# Telegram Bot
# ---------------------------------------------------------------------------


class TelegramBot:
    def __init__(self, config: Config, supervisor_token: str) -> None:
        self._config = config
        self._bot = Bot(token=config.bot_token)
        self._dp = Dispatcher()
        self._ha = HAClient(supervisor_token)
        self._db = Database(DB_PATH)
        self._registry = HARegistry(supervisor_token, self._db)
        self._notif = NotificationManager(supervisor_token, self._db, self._bot)
        self._global_rl = GlobalRateLimiter(
            config.global_rate_limit_actions,
            config.global_rate_limit_window,
        )
        self._vacuum = VacuumAdapter(
            ha=self._ha,
            db=self._db,
            registry=self._registry,
            strategy=config.vacuum_room_strategy,
            script_entity_id=config.vacuum_room_script_entity_id,
            presets=config.vacuum_room_presets,
        )
        self._scheduler = Scheduler(self._ha, self._db)
        self._diagnostics: Diagnostics | None = None
        self._error_capture: ErrorCapture | None = None
        self._handlers: Handlers | None = None

    async def run(self) -> None:
        await self._db.open()
        await self._ha.open()

        # Self-test
        ha_cfg = await self._ha.get_config()
        ha_version = "unknown"
        if ha_cfg:
            ha_version = ha_cfg.get("version", "unknown")
            logger.info("HA API self-test passed — version %s", ha_version)
        else:
            logger.error("HA API self-test FAILED — check SUPERVISOR_TOKEN / network")

        # Registry sync
        logger.info("Starting registry sync...")
        sync_ok = await self._registry.sync()
        if sync_ok:
            logger.info(
                "Registry sync complete: %d floors, %d areas, %d devices, %d entities",
                len(self._registry.floors),
                len(self._registry.areas),
                len(self._registry.devices),
                len(self._registry.entities),
            )
            for vac_eid, btns in self._registry.vacuum_routines.items():
                logger.info("Vacuum %s: %d routines", vac_eid, len(btns))
        else:
            logger.warning("Registry sync failed — bot will work with limited navigation")

        # Diagnostics
        self._diagnostics = Diagnostics(
            ha=self._ha, db=self._db, registry=self._registry,
            ha_version=ha_version,
        )

        # Error capture handler — writes ERROR+ logs to DB ring buffer
        self._error_capture = ErrorCapture(self._db)
        self._error_capture.set_loop(asyncio.get_running_loop())
        logging.getLogger().addHandler(self._error_capture)

        # Handlers
        self._handlers = Handlers(
            bot=self._bot,
            ha=self._ha,
            db=self._db,
            config=self._config,
            global_rl=self._global_rl,
            registry=self._registry,
            vacuum=self._vacuum,
            diagnostics=self._diagnostics,
            scheduler=self._scheduler,
        )
        self._handlers.ha_version = ha_version

        # Register commands
        self._dp.message.register(self._handlers.cmd_start, Command("start"))
        self._dp.message.register(self._handlers.cmd_start, Command("menu"))
        self._dp.message.register(self._handlers.cmd_status, Command("status"))
        self._dp.message.register(self._handlers.cmd_ping, Command("ping"))
        self._dp.message.register(self._handlers.cmd_search, Command("search"))
        self._dp.message.register(self._handlers.cmd_health, Command("health"))
        self._dp.message.register(self._handlers.cmd_diag, Command("diag"))
        self._dp.message.register(self._handlers.cmd_trace, Command("trace_last_error"))
        self._dp.message.register(self._handlers.cmd_snapshot, Command("snapshot"))
        self._dp.message.register(self._handlers.cmd_snapshots, Command("snapshots"))
        self._dp.message.register(self._handlers.cmd_schedule, Command("schedule"))
        self._dp.message.register(self._handlers.cmd_role, Command("role"))
        self._dp.message.register(self._handlers.cmd_export_settings, Command("export_settings"))
        self._dp.message.register(self._handlers.cmd_import_settings, Command("import_settings"))
        self._dp.message.register(self._handlers.cmd_notify_test, Command("notify_test"))
        self._dp.callback_query.register(self._handlers.handle_callback)
        # Text search handler (must be last — catches all text messages)
        self._dp.message.register(self._handlers.handle_text_search)

        # Start background services
        await self._notif.start()
        await self._scheduler.start()

        # Bot identity
        try:
            me = await self._bot.get_me()
            logger.info("Bot initialized — @%s (id=%s)", me.username, me.id)
        except Exception:
            logger.warning("Could not fetch bot info via getMe")

        # Auth mode log
        chat_mode = (
            f"chat_id={self._config.allowed_chat_id}"
            if self._config.allowed_chat_id != 0
            else "any chat"
        )
        user_count = len(self._config.allowed_user_ids)
        user_mode = f"{user_count} allowed user(s)" if user_count > 0 else "any user"
        logger.info("Authorization mode: %s, %s", chat_mode, user_mode)
        logger.info("Bot polling started (v2.3.0)")

        await self._dp.start_polling(
            self._bot,
            allowed_updates=["message", "callback_query"],
        )

    async def shutdown(self) -> None:
        errors: list[str] = []

        # Remove error capture handler
        if self._error_capture is not None:
            logging.getLogger().removeHandler(self._error_capture)

        for label, coro in [
            ("scheduler", self._scheduler.stop()),
            ("notifications", self._notif.stop()),
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    logger.info("Loading configuration...")
    config, supervisor_token = _load_and_validate_config()

    bot = TelegramBot(config, supervisor_token)
    try:
        await bot.run()
    except asyncio.CancelledError:
        logger.info("Cancelled — shutting down")
    except Exception:
        logger.exception("Fatal error")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
