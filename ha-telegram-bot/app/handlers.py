"""Telegram callback and command handlers.

Full menu navigation: Floors -> Areas -> Entities -> Controls.
Favorites, notifications, vacuum routines, segment cleaning.
Search, snapshots, scheduler, diagnostics, roles, export/import.
Edit-in-place message management, security checks, rate limiting.
Role-based access control (admin / user / guest).
Forum supergroup thread support (message_thread_id).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from api import HAClient
from diagnostics import Diagnostics
from registry import HARegistry
from scheduler import Scheduler, next_cron_time, validate_cron
from state_mapping import map_state
from storage import Database
from ui import (
    COLOR_PRESETS,
    build_active_now_menu,
    build_areas_menu,
    build_automations_menu,
    build_confirmation,
    build_device_list,
    build_diagnostics_menu,
    build_entity_control,
    build_entity_list,
    build_fav_actions_menu,
    build_favorites_menu,
    build_floors_menu,
    build_global_color_menu,
    build_global_color_result,
    build_help_menu,
    build_light_color_menu,
    build_main_menu,
    build_media_source_menu,
    build_notif_list,
    build_radio_menu,
    build_roles_list,
    build_scenarios_menu,
    build_schedule_list,
    build_search_prompt,
    build_search_results,
    build_snapshot_detail,
    build_snapshots_list,
    build_status_menu,
    build_todo_items_menu,
    build_todo_lists_menu,
    build_vacuum_rooms,
    build_vacuum_routines,
    group_sensor_entities,
    sort_entities_for_device,
)
from vacuum_adapter import VacuumAdapter

logger = logging.getLogger("ha_bot.handlers")

_ALLOWED_SERVICES: frozenset[str] = frozenset({
    "turn_on", "turn_off", "toggle",
    "open_cover", "close_cover", "stop_cover",
    "start", "stop", "pause", "return_to_base", "locate",
    "lock", "unlock",
    "press",
    "media_play", "media_pause", "media_stop",
    "volume_up", "volume_down", "volume_mute", "volume_set",
    "select_source",
    "select_option",
    "set_value",
    "set_temperature",
})

_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9_\-]*$")

# Role hierarchy levels
_ROLE_LEVELS: dict[str, int] = {"admin": 3, "user": 2, "guest": 1}

# Write-action callback prefixes that require at least "user" role
_WRITE_PREFIXES: frozenset[str] = frozenset({
    "act", "bright", "clim", "fav", "ntog", "vseg", "vcmd", "rtn",
    "nact", "nmute", "fa_run", "fa_del", "schtog", "schdel", "snapdel",
    "mvol", "mmut", "msrs", "ssel", "nval", "pin",
    "qsc", "lcs", "gcl", "rad", "rout",
    "atog", "atrig",  # automations
    "tdc", "tdd",     # to-do complete/delete
})

# Prefixes exempt from idempotency guard (debounce-eligible rapid taps)
_DEBOUNCE_PREFIXES: frozenset[str] = frozenset({"bright", "mvol"})


# ---------------------------------------------------------------------------
# Global rate limiter
# ---------------------------------------------------------------------------


class GlobalRateLimiter:
    def __init__(self, max_actions: int, window_seconds: int) -> None:
        self._max = max_actions
        self._window = window_seconds
        self._timestamps: list[float] = []

    def check(self) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        return len(self._timestamps) < self._max

    def record(self) -> None:
        self._timestamps.append(time.monotonic())


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


async def _audit(
    db: Database, *, chat_id: int, user_id: int, username: str,
    action: str, entity_id: str | None = None,
    success: bool, error: str | None = None,
) -> None:
    logger.info(
        "AUDIT",
        extra={
            "chat_id": chat_id, "user_id": user_id, "username": username,
            "action": action, "ok": success, "error_detail": error,
        },
    )
    try:
        await db.write_audit(
            chat_id=chat_id, user_id=user_id, username=username,
            action=action, entity_id=entity_id, success=success, error=error,
        )
    except Exception:
        logger.exception("Failed to persist audit record")


# ---------------------------------------------------------------------------
# Handler class
# ---------------------------------------------------------------------------


class Handlers:
    def __init__(
        self, *, bot: Bot, ha: HAClient, db: Database,
        config: Any, global_rl: GlobalRateLimiter, registry: HARegistry,
        vacuum: VacuumAdapter, diagnostics: Diagnostics, scheduler: Scheduler,
    ) -> None:
        self._bot = bot
        self._ha = ha
        self._db = db
        self._cfg = config
        self._rl = global_rl
        self._reg = registry
        self._vac = vacuum
        self._diag = diagnostics
        self._sched = scheduler
        self._device_overrides = getattr(config, "device_overrides", {})
        self.ha_version: str = "unknown"

        # In-memory search result cache: chat_id -> entity list
        self._search_cache: dict[int, list[dict[str, Any]]] = {}
        # Navigation breadcrumb stack: chat_id -> [callback, ...]
        self._nav_stack: dict[int, list[str]] = {}
        # Radio state per chat: chat_id -> {station_idx, player_eid, playing}
        self._radio_state: dict[int, dict[str, Any]] = {}

        # Per-user callback serialization lock
        self._user_locks: dict[int, asyncio.Lock] = {}
        # Idempotency guard: uid -> (callback_data, timestamp)
        self._last_cb: dict[int, tuple[str, float]] = {}

        # Debounce state for brightness / volume
        self._pending_brightness: dict[tuple[int, int, str], int] = {}
        self._pending_volume: dict[tuple[int, int, str], float] = {}
        self._debounce_tasks: dict[tuple[int, int, str], asyncio.Task[None]] = {}

        # To-do: pending add item state: chat_id -> list_entity_id
        self._todo_add_pending: dict[int, str] = {}

    # -----------------------------------------------------------------------
    # Security
    # -----------------------------------------------------------------------

    def _is_authorized_chat(self, chat_id: int) -> bool:
        return self._cfg.allowed_chat_id == 0 or chat_id == self._cfg.allowed_chat_id

    def _is_authorized_user(self, user_id: int) -> bool:
        return not self._cfg.allowed_user_ids or user_id in self._cfg.allowed_user_ids

    async def _check_role(self, user_id: int, min_role: str) -> bool:
        """Check if user has at least min_role level."""
        role = await self._db.get_user_role(user_id)
        return _ROLE_LEVELS.get(role, 2) >= _ROLE_LEVELS.get(min_role, 2)

    @staticmethod
    def _extract_user(obj: Message | CallbackQuery) -> tuple[int | None, str]:
        user = obj.from_user
        if user is None:
            return None, "<unknown>"
        return user.id, user.username or user.first_name or str(user.id)

    @staticmethod
    def _get_thread_id(msg: Message) -> int | None:
        """Extract thread_id for forum supergroups."""
        if msg.is_topic_message and msg.message_thread_id:
            return msg.message_thread_id
        return None

    # -----------------------------------------------------------------------
    # Message management
    # -----------------------------------------------------------------------

    async def _send_or_edit(
        self, chat_id: int, text: str, kb: InlineKeyboardMarkup, *,
        source: Message | None = None,
        menu: str = "main", entity: str | None = None, room: str | None = None,
        thread_id: int | None = None,
    ) -> None:
        msg_id = await self._db.get_menu_message_id(chat_id)

        if msg_id is not None:
            try:
                await self._bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=msg_id,
                    parse_mode="HTML", reply_markup=kb,
                )
                await self._db.save_menu_state(chat_id, msg_id, menu, entity, room)
                return
            except TelegramRetryAfter as e:
                logger.warning("Rate limited %ss", e.retry_after)
                return
            except (TelegramBadRequest, TelegramForbiddenError):
                try:
                    await self._bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass

        if source is not None:
            try:
                await source.delete()
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id, "text": text,
                "parse_mode": "HTML", "reply_markup": kb,
            }
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            sent = await self._bot.send_message(**kwargs)
            await self._db.save_menu_state(chat_id, sent.message_id, menu, entity, room)
        except TelegramRetryAfter as e:
            logger.warning("Rate limited on send %ss", e.retry_after)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.error("Failed to send menu: %s", exc)

    # -----------------------------------------------------------------------
    # Rate limits
    # -----------------------------------------------------------------------

    async def _check_rl(self, user_id: int, action: str, cb: CallbackQuery) -> bool:
        cd = self._cfg.cooldown_overrides.get(action, self._cfg.cooldown_seconds_default)
        allowed, remaining = await self._db.check_and_update_cooldown(
            user_id, action, cd,
        )
        if not allowed:
            await cb.answer(f"\u23f1\ufe0f Подождите {remaining:.1f}с.", show_alert=True)
            return False
        if not self._rl.check():
            await cb.answer("\U0001f6a6 Лимит. Подождите.", show_alert=True)
            return False
        return True

    # -----------------------------------------------------------------------
    # Navigation breadcrumb stack
    # -----------------------------------------------------------------------

    def _push_nav(self, cid: int, cb: str) -> None:
        """Push a callback onto the navigation stack for this chat."""
        stack = self._nav_stack.setdefault(cid, [])
        if not stack or stack[-1] != cb:
            stack.append(cb)
        if len(stack) > 20:
            stack[:] = stack[-20:]

    def _pop_nav(self, cid: int) -> str:
        """Pop the navigation stack and return the previous callback."""
        stack = self._nav_stack.get(cid, [])
        if stack:
            stack.pop()  # remove current
        if stack:
            return stack.pop()  # return previous
        return "nav:main"

    def _clear_nav(self, cid: int) -> None:
        """Clear the navigation stack for this chat."""
        self._nav_stack.pop(cid, None)

    # -----------------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------------

    async def cmd_start(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="/start", success=False, error="Unauth chat")
            await message.answer("\u26d4 Неавторизованный чат.")
            return
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="/start", success=True)
        self._clear_nav(cid)
        text, kb = build_main_menu()
        tid = self._get_thread_id(message)
        await self._send_or_edit(cid, text, kb, source=message, menu="main", thread_id=tid)

    async def cmd_status(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            await message.answer("\u26d4 Неавторизованный чат.")
            return
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="/status", success=True)
        entities = await self._fetch_status_entities()
        text, kb = build_status_menu(entities)
        tid = self._get_thread_id(message)
        await self._send_or_edit(cid, text, kb, source=message, menu="status", thread_id=tid)

    async def cmd_ping(self, message: Message) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await message.answer(f"pong  |  HA {self.ha_version}  |  {ts}")

    async def cmd_search(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            await message.answer("\u26d4 Неавторизованный чат.")
            return

        # Extract query from /search <query>
        parts = (message.text or "").split(maxsplit=1)
        query = parts[1].strip() if len(parts) > 1 else ""

        if not query:
            text, kb = build_search_prompt()
            tid = self._get_thread_id(message)
            await self._send_or_edit(cid, text, kb, source=message, menu="search", thread_id=tid)
            return

        await self._do_search(cid, uid, query, message=message)

    async def cmd_health(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        health = await self._diag.health_check()
        status = health["status"]
        icon = "\u2705" if status == "ok" else "\u26a0\ufe0f"
        lines = [
            f"{icon} <b>Health: {status}</b>",
            f"HA: {health['ha_version']} ({'OK' if health['ha_reachable'] else 'FAIL'})",
            f"Registry: {'synced' if health['registry_synced'] else 'NOT synced'}",
            f"Uptime: {health['uptime']}",
            f"Entities: {health['entities']}",
        ]
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_diag(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        if not await self._check_role(uid, "admin"):
            await message.answer("\u26d4 Требуются права администратора.")
            return
        diag_text = await self._diag.get_diagnostics_text()
        text, kb = build_diagnostics_menu(diag_text)
        tid = self._get_thread_id(message)
        await self._send_or_edit(cid, text, kb, source=message, menu="diag", thread_id=tid)

    async def cmd_trace(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        if not await self._check_role(uid, "admin"):
            await message.answer("\u26d4 Требуются права администратора.")
            return
        trace = await self._diag.trace_last_error()
        await message.answer(trace, parse_mode="HTML")

    async def cmd_snapshot(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return

        parts = (message.text or "").split(maxsplit=1)
        snap_name = parts[1].strip() if len(parts) > 1 else ""
        if not snap_name:
            snap_name = datetime.now(timezone.utc).strftime("snap_%Y%m%d_%H%M%S")

        states = await self._ha.list_states()
        payload = [
            {"entity_id": s.get("entity_id", ""), "state": s.get("state", ""),
             "attributes": s.get("attributes", {})}
            for s in states if s.get("entity_id")
        ]
        snap_id = await self._db.save_snapshot(uid, snap_name, payload)
        await message.answer(
            f"\U0001f4f8 Снимок <b>{snap_name}</b> сохранён (#{snap_id}, {len(payload)} сущностей).",
            parse_mode="HTML",
        )

    async def cmd_snapshots(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        snaps = await self._db.get_snapshots(uid)
        text, kb = build_snapshots_list(snaps)
        tid = self._get_thread_id(message)
        await self._send_or_edit(cid, text, kb, source=message, menu="snapshots", thread_id=tid)

    async def cmd_schedule(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return

        parts = (message.text or "").split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        if not args or args == "list":
            scheds = await self._db.get_schedules(uid)
            text, kb = build_schedule_list(scheds)
            tid = self._get_thread_id(message)
            await self._send_or_edit(cid, text, kb, source=message, menu="schedule", thread_id=tid)
            return

        if args.startswith("add "):
            await self._schedule_add(message, uid, args[4:].strip())
            return

        await message.answer(
            "\u23f0 Использование:\n"
            "/schedule — список\n"
            '/schedule add &lt;name&gt; | &lt;cron&gt; | &lt;domain.service&gt; | &lt;entity_id&gt;\n'
            "Пример: /schedule add Morning | 0 7 * * * | light.turn_on | light.bedroom",
            parse_mode="HTML",
        )

    async def _schedule_add(self, message: Message, uid: int, args: str) -> None:
        parts = [p.strip() for p in args.split("|")]
        if len(parts) < 4:
            await message.answer(
                "\u274c Формат: name | cron | domain.service | entity_id",
                parse_mode="HTML",
            )
            return

        name, cron_expr, service_str, entity_id = parts[0], parts[1], parts[2], parts[3]

        err = validate_cron(cron_expr)
        if err:
            await message.answer(f"\u274c Некорректный cron: {err}")
            return

        if "." not in service_str:
            await message.answer("\u274c Сервис должен быть в формате domain.service")
            return

        domain, service = service_str.split(".", 1)
        nr = next_cron_time(cron_expr)
        if nr == 0.0:
            await message.answer("\u274c Не удалось вычислить следующий запуск.")
            return

        payload = {"domain": domain, "service": service, "data": {"entity_id": entity_id}}
        sched_id = await self._db.add_schedule(uid, name, "service_call", payload, cron_expr, nr)
        await message.answer(
            f"\u2705 Расписание #{sched_id} <b>{name}</b> создано.\n"
            f"Cron: <code>{cron_expr}</code>",
            parse_mode="HTML",
        )

    async def cmd_role(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        if not await self._check_role(uid, "admin"):
            await message.answer("\u26d4 Требуются права администратора.")
            return

        parts = (message.text or "").split()
        if len(parts) == 3:
            try:
                target_uid = int(parts[1])
            except ValueError:
                await message.answer("\u274c user_id должен быть числом.")
                return
            role = parts[2].lower()
            if role not in ("admin", "user", "guest"):
                await message.answer("\u274c Роль: admin / user / guest")
                return
            await self._db.set_user_role(target_uid, role)
            await message.answer(f"\u2705 Пользователь {target_uid} → {role}")
            return

        roles = await self._db.get_all_roles()
        text, kb = build_roles_list(roles)
        tid = self._get_thread_id(message)
        await self._send_or_edit(cid, text, kb, source=message, menu="roles", thread_id=tid)

    async def cmd_export_settings(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        data = await self._db.export_user_settings(uid)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"
        await message.answer(f"<pre>{text}</pre>", parse_mode="HTML")

    async def cmd_import_settings(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        parts = (message.text or "").split(maxsplit=1)
        json_str = parts[1].strip() if len(parts) > 1 else ""
        if not json_str:
            await message.answer(
                "Использование: /import_settings {json}\nJSON из /export_settings",
            )
            return
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            await message.answer(f"\u274c Некорректный JSON: {exc}")
            return
        count = await self._db.import_user_settings(uid, data)
        await message.answer(f"\u2705 Импортировано: {count} записей.")

    async def cmd_notify_test(self, message: Message) -> None:
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        result = await self._diag.notify_test(self._bot, cid)
        await message.answer(result)

    async def handle_text_search(self, message: Message) -> None:
        """Handle plain text messages as search queries or to-do add input."""
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            return
        if not self._is_authorized_user(uid):
            return

        # To-do add mode: pending add item
        if cid in self._todo_add_pending:
            list_eid = self._todo_add_pending.pop(cid)
            summary = (message.text or "").strip()
            if not summary:
                return
            try:
                ok, err = await self._ha.call_service(
                    "todo", "add_item",
                    {"entity_id": list_eid, "item": summary},
                )
            except Exception as exc:
                logger.exception("Todo add item failed")
                ok, err = False, str(exc)[:200]
            if ok:
                await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                             action="todo.add", entity_id=list_eid, success=True)
            else:
                logger.warning("Failed to add todo item: %s", err)
            await self._show_todo_items(cid, list_eid, 0)
            return

        menu_state = await self._db.get_menu_state(cid)
        if not menu_state or menu_state.get("current_menu") != "search":
            return

        query = (message.text or "").strip()
        if not query:
            return

        await self._do_search(cid, uid, query, message=message)

    # -----------------------------------------------------------------------
    # Callback dispatcher
    # -----------------------------------------------------------------------

    async def handle_callback(self, callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer("Сообщение устарело.", show_alert=True)
            return
        cid = callback.message.chat.id
        uid, uname = self._extract_user(callback)
        if uid is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        data = callback.data or ""

        if not self._is_authorized_chat(cid):
            await callback.answer("\u26d4 Неавторизованный чат.", show_alert=True)
            return
        if not self._is_authorized_user(uid):
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action=data, success=False, error="Unauth user")
            await callback.answer("\u26d4 Нет доступа.", show_alert=True)
            return

        # Idempotency guard: reject duplicate (uid, data) within 0.25s
        prefix = data.split(":", 1)[0] if ":" in data else data
        now = time.time()
        last = self._last_cb.get(uid)
        if last and last[0] == data and (now - last[1]) < 0.25 and prefix not in _DEBOUNCE_PREFIXES:
            await callback.answer()
            return
        self._last_cb[uid] = (data, now)

        # Role check for write actions
        if prefix in _WRITE_PREFIXES:
            if not await self._check_role(uid, "user"):
                await callback.answer("\u26d4 Недостаточно прав (guest).", show_alert=True)
                return

        # Per-user lock to serialize callback handling
        lock = self._user_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            try:
                handler = self._ROUTES.get(prefix)
                if handler:
                    await handler(self, cid, uid, uname, data, callback)
                else:
                    await callback.answer("Неизвестное действие.", show_alert=True)
            except Exception:
                logger.exception("Callback error: %s", data)
                await callback.answer("Произошла ошибка.", show_alert=True)

    # -----------------------------------------------------------------------
    # nav: — back navigation
    # -----------------------------------------------------------------------

    async def _nav(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        target = data.split(":", 1)[1] if ":" in data else "main"
        await cb.answer()
        if target == "main":
            self._clear_nav(cid)
            t, k = build_main_menu()
            await self._send_or_edit(cid, t, k, menu="main")
        elif target == "manage" or target == "devices":
            await self._show_manage(cid)
        elif target == "floors":
            await self._show_floors(cid)
        elif target == "back":
            # Breadcrumb: pop the stack and navigate to previous
            prev = self._pop_nav(cid)
            # Re-dispatch the previous callback
            if prev == "nav:main":
                self._clear_nav(cid)
                t, k = build_main_menu()
                await self._send_or_edit(cid, t, k, menu="main")
            else:
                # Simulate the callback by re-dispatching
                prefix = prev.split(":", 1)[0] if ":" in prev else prev
                handler = self._ROUTES.get(prefix)
                if handler:
                    await handler(self, cid, uid, uname, prev, cb)
                else:
                    t, k = build_main_menu()
                    await self._send_or_edit(cid, t, k, menu="main")
        else:
            t, k = build_main_menu()
            await self._send_or_edit(cid, t, k, menu="main")

    # -----------------------------------------------------------------------
    # menu: — main menu buttons
    # -----------------------------------------------------------------------

    async def _menu(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        target = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        if target == "manage" or target == "devices":
            self._push_nav(cid, "nav:main")
            await self._show_manage(cid)
        elif target == "scenarios":
            self._push_nav(cid, "nav:main")
            await self._show_scenarios(cid)
        elif target == "favorites":
            self._push_nav(cid, "nav:main")
            await self._show_favorites(cid, uid, 0)
        elif target == "fav_actions":
            await self._show_fav_actions(cid, uid, 0)
        elif target == "notif":
            await self._show_notif_list(cid, uid, 0)
        elif target == "search":
            t, k = build_search_prompt()
            await self._send_or_edit(cid, t, k, menu="search")
        elif target == "schedule":
            scheds = await self._db.get_schedules(uid)
            t, k = build_schedule_list(scheds)
            await self._send_or_edit(cid, t, k, menu="schedule")
        elif target == "refresh":
            await self._do_refresh(cid)
        elif target == "diag":
            diag_text = await self._diag.get_diagnostics_text()
            t, k = build_diagnostics_menu(diag_text)
            await self._send_or_edit(cid, t, k, menu="diag")
        elif target == "status":
            ents = await self._fetch_status_entities()
            t, k = build_status_menu(ents)
            await self._send_or_edit(cid, t, k, menu="status")
        elif target == "snapshots":
            snaps = await self._db.get_snapshots(uid)
            t, k = build_snapshots_list(snaps)
            await self._send_or_edit(cid, t, k, menu="snapshots")
        elif target == "help":
            t, k = build_help_menu()
            await self._send_or_edit(cid, t, k, menu="help")
        elif target == "active":
            self._push_nav(cid, "nav:main")
            await self._show_active_now(cid, uid, 0)
        elif target == "radio":
            self._push_nav(cid, "nav:main")
            await self._show_radio(cid)
        elif target == "gcolor":
            self._push_nav(cid, "nav:main")
            t, k = build_global_color_menu()
            await self._send_or_edit(cid, t, k, menu="gcolor")
        elif target == "automations":
            self._push_nav(cid, "nav:main")
            await self._show_automations(cid, 0)
        elif target == "todo":
            self._push_nav(cid, "nav:main")
            await self._show_todo_lists(cid)

    # -----------------------------------------------------------------------
    # fl: — floor selected
    # -----------------------------------------------------------------------

    async def _floor(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        floor_id = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        domains = frozenset(self._cfg.menu_domains_allowlist)
        sa = self._cfg.show_all_enabled

        if floor_id == "__none__":
            areas = self._reg.get_unassigned_areas()
        else:
            areas = self._reg.get_areas_for_floor(floor_id)

        area_dicts = []
        for a in areas:
            eids = self._reg.get_area_entities(a.area_id, domains, show_all=sa)
            if eids:
                area_dicts.append({"area_id": a.area_id, "name": a.name, "entity_count": len(eids)})

        unassigned = self._reg.get_unassigned_entities(domains, show_all=sa)

        back = "nav:manage" if self._reg.has_floors else "nav:main"
        floor_name = ""
        if floor_id and floor_id != "__none__":
            fl = self._reg.floors.get(floor_id)
            floor_name = fl.name if fl else floor_id

        t, k = build_areas_menu(
            area_dicts,
            back_target=back,
            title=f"\U0001f3e0 <b>{floor_name or 'Комнаты'}</b>",
            unassigned_entity_count=len(unassigned),
        )
        await self._send_or_edit(cid, t, k, menu=f"floor:{floor_id}")

    # -----------------------------------------------------------------------
    # ar: — area selected -> device list (grouped by device_id)
    # -----------------------------------------------------------------------

    async def _area(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        area_id = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()
        self._push_nav(cid, "nav:manage")

        domains = frozenset(self._cfg.menu_domains_allowlist)
        sa = self._cfg.show_all_enabled
        if area_id == "__none__":
            devices = self._reg.get_unassigned_devices(domains, show_all=sa)
            title = "\U0001f4e6 <b>Без комнаты</b>"
        else:
            devices = self._reg.get_devices_for_area(area_id, domains, show_all=sa)
            area = self._reg.areas.get(area_id)
            title = f"\U0001f3e0 <b>{area.name if area else area_id}</b>"

        if not devices:
            t, k = build_confirmation(f"{title}\n\nНет устройств.", "nav:manage")
            await self._send_or_edit(cid, t, k, menu=f"area:{area_id}")
            return

        # If there's only one device with one entity, go straight to entity control
        if len(devices) == 1 and len(devices[0]["entity_ids"]) == 1:
            await self._show_entity_control(cid, uid, devices[0]["primary_entity_id"])
            return

        # Collect quick scenes for this area (scene/script entities)
        quick_scenes: list[dict[str, Any]] = []
        if area_id and area_id != "__none__":
            area_obj = self._reg.areas.get(area_id)
            if area_obj:
                for eid in area_obj.entity_ids:
                    d = eid.split(".", 1)[0]
                    if d in ("scene", "script"):
                        ent_info = self._reg.entities.get(eid)
                        if ent_info and not ent_info.disabled_by:
                            name = ent_info.name or ent_info.original_name or eid
                            quick_scenes.append({"entity_id": eid, "friendly_name": name})

        pin_btn = None
        if area_id and area_id != "__none__":
            is_pinned = await self._db.is_pinned(uid, "area", area_id)
            pin_label = "\U0001f4cc Убрать" if is_pinned else "\U0001f4cc Закрепить"
            pin_btn = InlineKeyboardButton(text=pin_label, callback_data=f"pin:area:{area_id}")

        t, k = build_device_list(
            devices, 0, self._cfg.menu_page_size,
            title=title, back_cb="nav:manage",
            page_cb_prefix=f"arp:{area_id}",
            pin_btn=pin_btn,
            scenes=quick_scenes if quick_scenes else None,
        )
        await self._send_or_edit(cid, t, k, menu=f"area:{area_id}")

    # -----------------------------------------------------------------------
    # arp: — area device pagination
    # -----------------------------------------------------------------------

    async def _area_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        # arp:area_id:page
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        area_id = parts[1]
        try:
            page = int(parts[2])
        except ValueError:
            page = 0
        await cb.answer()

        domains = frozenset(self._cfg.menu_domains_allowlist)
        sa = self._cfg.show_all_enabled
        if area_id == "__none__":
            devices = self._reg.get_unassigned_devices(domains, show_all=sa)
            title = "\U0001f4e6 <b>Без комнаты</b>"
        else:
            devices = self._reg.get_devices_for_area(area_id, domains, show_all=sa)
            area = self._reg.areas.get(area_id)
            title = f"\U0001f3e0 <b>{area.name if area else area_id}</b>"

        pin_btn = None
        if area_id and area_id != "__none__":
            is_pinned = await self._db.is_pinned(uid, "area", area_id)
            pin_label = "\U0001f4cc Убрать" if is_pinned else "\U0001f4cc Закрепить"
            pin_btn = InlineKeyboardButton(text=pin_label, callback_data=f"pin:area:{area_id}")

        t, k = build_device_list(
            devices, page, self._cfg.menu_page_size,
            title=title, back_cb="nav:manage",
            page_cb_prefix=f"arp:{area_id}",
            pin_btn=pin_btn,
        )
        await self._send_or_edit(cid, t, k, menu=f"area:{area_id}:{page}")

    # -----------------------------------------------------------------------
    # ent: — entity control
    # -----------------------------------------------------------------------

    async def _entity(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        if not eid:
            await cb.answer("Некорректная сущность.", show_alert=True)
            return
        await cb.answer()
        await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # dev: — device selected -> show entities or entity control
    # -----------------------------------------------------------------------

    async def _device(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        device_id = data.split(":", 1)[1] if ":" in data else ""
        if not device_id:
            await cb.answer()
            return
        await cb.answer()

        domains = frozenset(self._cfg.menu_domains_allowlist)

        # Check if it's a vacuum device → go to vacuum entity control
        vac_eid = self._reg.get_vacuum_entity_for_device(device_id)
        if vac_eid:
            await self._show_entity_control(cid, uid, vac_eid)
            return

        # Get entities for this device
        eids = self._reg.get_device_entity_ids(device_id, domains)

        # If device_id is actually an entity_id (virtual device), show control directly
        if not eids and "." in device_id:
            await self._show_entity_control(cid, uid, device_id)
            return

        # Single entity → direct control
        if len(eids) == 1:
            await self._show_entity_control(cid, uid, eids[0])
            return

        # Multiple entities → show entity list
        ent_list = await self._enrich_entities(eids)
        dev = self._reg.devices.get(device_id)
        dev_name = dev.name if dev else device_id

        # Find area_id for back navigation
        area_id = dev.area_id if dev else None
        back_cb = f"ar:{area_id}" if area_id else "nav:manage"

        t, k = build_entity_list(
            ent_list, 0, self._cfg.menu_page_size,
            title=f"\U0001f4e6 <b>{dev_name}</b>",
            back_cb=back_cb,
            page_cb_prefix=f"dvp:{device_id}",
        )
        await self._send_or_edit(cid, t, k, menu=f"device:{device_id}")

    # -----------------------------------------------------------------------
    # dvp: — device entity pagination
    # -----------------------------------------------------------------------

    async def _device_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        device_id = parts[1]
        try:
            page = int(parts[2])
        except ValueError:
            page = 0
        await cb.answer()

        domains = frozenset(self._cfg.menu_domains_allowlist)
        eids = self._reg.get_device_entity_ids(device_id, domains)
        ent_list = await self._enrich_entities(eids)

        dev = self._reg.devices.get(device_id)
        dev_name = dev.name if dev else device_id
        area_id = dev.area_id if dev else None
        back_cb = f"ar:{area_id}" if area_id else "nav:manage"

        t, k = build_entity_list(
            ent_list, page, self._cfg.menu_page_size,
            title=f"\U0001f4e6 <b>{dev_name}</b>",
            back_cb=back_cb,
            page_cb_prefix=f"dvp:{device_id}",
        )
        await self._send_or_edit(cid, t, k, menu=f"device:{device_id}:{page}")

    async def _show_entity_control(self, cid: int, uid: int, eid: str) -> None:
        try:
            state = await self._ha.get_state(eid)
        except Exception:
            logger.exception("Failed to fetch state for %s", eid)
            state = None
        if state is None:
            t, k = build_confirmation("\U0001f534 Сущность не найдена.", "nav:main")
            await self._send_or_edit(cid, t, k, menu="entity_err")
            return

        is_fav = await self._db.is_favorite(uid, eid)
        notif = await self._db.get_notification(uid, eid)
        is_notif = notif is not None and notif["enabled"]

        # Determine best back callback
        area_info = await self._db.get_entity_area(eid)
        if area_info and area_info.get("area_id"):
            back = f"ar:{area_info['area_id']}"
        else:
            back = "nav:manage"

        self._push_nav(cid, back)

        t, k = build_entity_control(eid, state, is_fav, is_notif, back)

        domain = eid.split(".", 1)[0]

        # For lights that support color, add color preset button
        if domain == "light":
            attrs = state.get("attributes", {})
            color_modes = attrs.get("supported_color_modes", [])
            if any(m in ("rgb", "rgbw", "rgbww", "hs", "xy") for m in color_modes):
                rows = k.inline_keyboard[:]
                insert_pos = max(0, len(rows) - 2)
                rows.insert(insert_pos, [InlineKeyboardButton(
                    text="\U0001f3a8 Цвет",
                    callback_data=f"lclr:{eid}",
                )])
                k = InlineKeyboardMarkup(inline_keyboard=rows)

        # For vacuum, add extra buttons (rooms, routines)
        if domain == "vacuum":
            extra_rows: list[list[InlineKeyboardButton]] = []
            try:
                caps = await self._vac.get_capabilities(eid)
            except Exception:
                logger.exception("Failed to get vacuum capabilities for %s", eid)
                caps = None
            if caps:
                if caps.supports_segment_clean:
                    extra_rows.append([InlineKeyboardButton(
                        text="\U0001f3e0 Уборка по комнатам",
                        callback_data=f"vrooms:{eid}",
                    )])
                if caps.supports_routines:
                    extra_rows.append([InlineKeyboardButton(
                        text=f"\U0001f3ac Сценарии ({caps.routine_count})",
                        callback_data=f"vrtn:{eid}",
                    )])
            if extra_rows:
                rows = k.inline_keyboard[:]
                insert_pos = max(0, len(rows) - 2)
                for er in reversed(extra_rows):
                    rows.insert(insert_pos, er)
                k = InlineKeyboardMarkup(inline_keyboard=rows)

        await self._send_or_edit(cid, t, k, menu="entity", entity=eid)

    # -----------------------------------------------------------------------
    # act: — execute service call
    # -----------------------------------------------------------------------

    async def _action(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer("Некорректное действие.", show_alert=True)
            return
        eid = parts[1]
        service = parts[2]
        domain = eid.split(".", 1)[0]

        if not _ENTITY_ID_RE.match(eid):
            await cb.answer("Некорректная сущность.", show_alert=True)
            return
        if service not in _ALLOWED_SERVICES:
            await cb.answer("Недопустимый сервис.", show_alert=True)
            return
        if not await self._check_rl(uid, f"{domain}.{service}", cb):
            return

        await cb.answer("\u2699\ufe0f Выполняю...")
        try:
            ok, err = await self._ha.call_service(domain, service, {"entity_id": eid})
        except Exception as exc:
            logger.exception("Service call %s.%s failed for %s", domain, service, eid)
            ok, err = False, str(exc)[:200]
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action=f"{domain}.{service}", entity_id=eid,
                     success=ok, error=err if not ok else None)

        if ok:
            await self._show_entity_control(cid, uid, eid)
        else:
            # Explicit error — never silently ignore (especially for button.press / doorbell)
            err_msg = (err or "неизвестная ошибка")[:180]
            logger.warning(
                "Service call %s.%s failed for %s: %s",
                domain, service, eid, err_msg,
            )
            await cb.answer(f"Ошибка: {err_msg}", show_alert=True)

    # -----------------------------------------------------------------------
    # bright: — brightness
    # -----------------------------------------------------------------------

    async def _brightness(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, direction = parts[1], parts[2]
        if not await self._check_rl(uid, "light.brightness", cb):
            return

        key = (cid, uid, eid)

        # Use pending value if mid-debounce, otherwise fetch HA state
        if key in self._pending_brightness:
            current = self._pending_brightness[key]
        else:
            state = await self._ha.get_state(eid)
            current = (state.get("attributes", {}).get("brightness", 128) or 128) if state else 128

        step = 51
        new_br = min(255, current + step) if direction == "up" else max(1, current - step)
        self._pending_brightness[key] = new_br

        pct = round(new_br / 255 * 100)
        await cb.answer(f"\U0001f506 {pct}%")

        # Cancel existing debounce task, schedule new one
        existing = self._debounce_tasks.get(key)
        if existing and not existing.done():
            existing.cancel()
        self._debounce_tasks[key] = asyncio.create_task(
            self._flush_brightness(key, cid, uid, uname, eid),
        )

    async def _flush_brightness(
        self, key: tuple[int, int, str], cid: int, uid: int, uname: str, eid: str,
    ) -> None:
        """Debounce flush: wait, then send ONE HA call with the latest brightness."""
        try:
            await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return
        target = self._pending_brightness.pop(key, None)
        self._debounce_tasks.pop(key, None)
        if target is None:
            return

        ok, err = await self._ha.call_service(
            "light", "turn_on", {"entity_id": eid, "brightness": target},
        )
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="light.brightness", entity_id=eid,
                     success=ok, error=err if not ok else None)
        if ok:
            await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # clim: — climate temperature
    # -----------------------------------------------------------------------

    async def _climate(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, direction = parts[1], parts[2]
        if not await self._check_rl(uid, "climate.set_temperature", cb):
            return

        state = await self._ha.get_state(eid)
        temp = 20.0
        if state:
            try:
                temp = float(state.get("attributes", {}).get("temperature", 20.0) or 20.0)
            except (TypeError, ValueError):
                temp = 20.0
        new_temp = temp + 0.5 if direction == "up" else temp - 0.5

        await cb.answer("\U0001f321\ufe0f Температура...")
        ok, err = await self._ha.call_service(
            "climate", "set_temperature", {"entity_id": eid, "temperature": new_temp},
        )
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="climate.set_temperature", entity_id=eid,
                     success=ok, error=err if not ok else None)
        if ok:
            await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # mvol: — media player volume
    # -----------------------------------------------------------------------

    async def _media_vol(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, direction = parts[1], parts[2]
        if not await self._check_rl(uid, "media_player.volume", cb):
            return

        key = (cid, uid, eid)
        vol_step = 0.05

        # Use pending value if mid-debounce, otherwise fetch HA state
        if key in self._pending_volume:
            current_vol = self._pending_volume[key]
        else:
            state = await self._ha.get_state(eid)
            raw_vol = state.get("attributes", {}).get("volume_level", 0.5) if state else 0.5
            try:
                current_vol = float(raw_vol or 0.5)
            except (TypeError, ValueError):
                current_vol = 0.5

        new_vol = min(1.0, current_vol + vol_step) if direction == "up" else max(0.0, current_vol - vol_step)
        self._pending_volume[key] = new_vol

        pct = round(new_vol * 100)
        await cb.answer(f"\U0001f50a {pct}%")

        # Cancel existing debounce task, schedule new one
        existing = self._debounce_tasks.get(key)
        if existing and not existing.done():
            existing.cancel()
        self._debounce_tasks[key] = asyncio.create_task(
            self._flush_volume(key, cid, uid, uname, eid),
        )

    async def _flush_volume(
        self, key: tuple[int, int, str], cid: int, uid: int, uname: str, eid: str,
    ) -> None:
        """Debounce flush: wait, then send ONE volume_set call."""
        try:
            await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return
        target = self._pending_volume.pop(key, None)
        self._debounce_tasks.pop(key, None)
        if target is None:
            return

        ok, err = await self._ha.call_service(
            "media_player", "volume_set",
            {"entity_id": eid, "volume_level": round(target, 2)},
        )
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="media_player.volume_set", entity_id=eid,
                     success=ok, error=err if not ok else None)
        if ok:
            await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # mmut: — media player mute toggle
    # -----------------------------------------------------------------------

    async def _media_mute(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        if not eid:
            await cb.answer()
            return
        if not await self._check_rl(uid, "media_player.volume_mute", cb):
            return

        state = await self._ha.get_state(eid)
        is_muted = state.get("attributes", {}).get("is_volume_muted", False) if state else False

        await cb.answer("\U0001f507 Mute...")
        ok, err = await self._ha.call_service(
            "media_player", "volume_mute",
            {"entity_id": eid, "is_volume_muted": not is_muted},
        )
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="media_player.volume_mute", entity_id=eid,
                     success=ok, error=err if not ok else None)
        if ok:
            await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # msrc: — media player source menu
    # -----------------------------------------------------------------------

    async def _media_source(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        if not eid:
            await cb.answer()
            return
        await cb.answer()

        state = await self._ha.get_state(eid)
        if not state:
            t, k = build_confirmation("\U0001f534 Не удалось получить состояние.", f"ent:{eid}")
            await self._send_or_edit(cid, t, k, menu="media_src_err")
            return

        attrs = state.get("attributes", {})
        name = attrs.get("friendly_name", eid)
        sources = attrs.get("source_list", [])
        current = attrs.get("source")

        t, k = build_media_source_menu(eid, name, sources, current)
        await self._send_or_edit(cid, t, k, menu="media_src", entity=eid)

    # -----------------------------------------------------------------------
    # msrs: — media player select source
    # -----------------------------------------------------------------------

    async def _media_source_sel(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, source = parts[1], parts[2]
        if not await self._check_rl(uid, "media_player.select_source", cb):
            return

        await cb.answer("\U0001f4fb Выбираю источник...")
        ok, err = await self._ha.call_service(
            "media_player", "select_source",
            {"entity_id": eid, "source": source},
        )
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="media_player.select_source", entity_id=eid,
                     success=ok, error=err if not ok else None)
        if ok:
            await self._show_entity_control(cid, uid, eid)
        else:
            await cb.answer(f"Ошибка: {(err or '')[:180]}", show_alert=True)

    # -----------------------------------------------------------------------
    # ssel: — select entity option
    # -----------------------------------------------------------------------

    async def _select_option(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, option = parts[1], parts[2]
        if not await self._check_rl(uid, "select.select_option", cb):
            return

        await cb.answer("\u2699\ufe0f Выбираю...")
        ok, err = await self._ha.call_service(
            "select", "select_option",
            {"entity_id": eid, "option": option},
        )
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="select.select_option", entity_id=eid,
                     success=ok, error=err if not ok else None)
        if ok:
            await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # nval: — number entity value adjustment
    # -----------------------------------------------------------------------

    async def _number_val(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, direction = parts[1], parts[2]
        if not await self._check_rl(uid, "number.set_value", cb):
            return

        state = await self._ha.get_state(eid)
        attrs = state.get("attributes", {}) if state else {}
        try:
            current = float(state.get("state", 0) if state else 0)
        except (TypeError, ValueError):
            current = 0.0

        step = float(attrs.get("step", 1))
        mn = float(attrs.get("min", 0))
        mx = float(attrs.get("max", 100))

        new_val = current + step if direction == "up" else current - step
        new_val = max(mn, min(mx, new_val))

        await cb.answer(f"Значение: {new_val}")
        ok, err = await self._ha.call_service(
            "number", "set_value",
            {"entity_id": eid, "value": new_val},
        )
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="number.set_value", entity_id=eid,
                     success=ok, error=err if not ok else None)
        if ok:
            await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # pin: — toggle pinned item (area/routine)
    # -----------------------------------------------------------------------

    async def _pin_toggle(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        # pin:item_type:target_id
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        item_type, target_id = parts[1], parts[2]

        # Resolve label
        label = target_id
        if item_type == "area":
            area = self._reg.areas.get(target_id)
            if area:
                label = area.name
        elif item_type == "routine":
            state = await self._ha.get_state(target_id)
            if state:
                label = state.get("attributes", {}).get("friendly_name", target_id)

        now_pinned = await self._db.toggle_pinned_item(uid, item_type, target_id, label)
        msg = "Закреплено" if now_pinned else "Откреплено"
        await cb.answer(f"\U0001f4cc {msg}")

        # Refresh the current view
        menu_state = await self._db.get_menu_state(cid)
        current_menu = menu_state.get("current_menu", "") if menu_state else ""
        if current_menu.startswith("area:"):
            area_id = current_menu.split(":", 1)[1].split(":", 1)[0]
            # Re-render the area page
            domains = frozenset(self._cfg.menu_domains_allowlist)
            sa = self._cfg.show_all_enabled
            devices = self._reg.get_devices_for_area(area_id, domains, show_all=sa)
            area = self._reg.areas.get(area_id)
            title = f"\U0001f3e0 <b>{area.name if area else area_id}</b>"
            is_pinned = await self._db.is_pinned(uid, "area", area_id)
            pin_label = "\U0001f4cc Убрать" if is_pinned else "\U0001f4cc Закрепить"
            pin_btn = InlineKeyboardButton(text=pin_label, callback_data=f"pin:area:{area_id}")
            t, k = build_device_list(
                devices, 0, self._cfg.menu_page_size,
                title=title, back_cb="nav:manage",
                page_cb_prefix=f"arp:{area_id}",
                pin_btn=pin_btn,
            )
            await self._send_or_edit(cid, t, k, menu=f"area:{area_id}")
        elif current_menu == "favorites":
            await self._show_favorites(cid, uid, 0)

    # -----------------------------------------------------------------------
    # fav: — toggle favorite
    # -----------------------------------------------------------------------

    async def _fav_toggle(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        if not eid:
            await cb.answer()
            return
        now_fav = await self._db.toggle_favorite(uid, eid)
        label = "Добавлено в избранное" if now_fav else "Удалено из избранного"
        await cb.answer(f"\u2b50 {label}")
        await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # ntog: — toggle notification
    # -----------------------------------------------------------------------

    async def _notif_toggle(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        if not eid:
            await cb.answer()
            return
        now_on = await self._db.toggle_notification(uid, eid)
        label = "Уведомления включены" if now_on else "Уведомления отключены"
        await cb.answer(f"\U0001f514 {label}")
        # Refresh the current view
        menu_state = await self._db.get_menu_state(cid)
        current_menu = menu_state.get("current_menu", "") if menu_state else ""
        if current_menu == "notif":
            await self._show_notif_list(cid, uid, 0)
        else:
            await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # favp: — favorites pagination
    # -----------------------------------------------------------------------

    async def _fav_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await cb.answer()
        await self._show_favorites(cid, uid, page)

    # -----------------------------------------------------------------------
    # nfp: — notifications list pagination
    # -----------------------------------------------------------------------

    async def _notif_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await cb.answer()
        await self._show_notif_list(cid, uid, page)

    # -----------------------------------------------------------------------
    # nact: — notification action (from actionable notification buttons)
    # -----------------------------------------------------------------------

    async def _notif_action(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        # nact:entity_id:service
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, service = parts[1], parts[2]
        domain = eid.split(".", 1)[0]
        if service not in _ALLOWED_SERVICES:
            await cb.answer("Недопустимый сервис.", show_alert=True)
            return

        await cb.answer("\u2699\ufe0f Выполняю...")
        try:
            ok, err = await self._ha.call_service(domain, service, {"entity_id": eid})
        except Exception as exc:
            logger.exception("Notification action %s.%s failed for %s", domain, service, eid)
            ok, err = False, str(exc)[:200]
        if ok:
            self._rl.record()
            await cb.answer(f"\u2705 {service}", show_alert=False)
        else:
            await cb.answer(f"\u274c {(err or '')[:180]}", show_alert=True)

        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action=f"nact.{domain}.{service}", entity_id=eid,
                     success=ok, error=err if not ok else None)

    # -----------------------------------------------------------------------
    # nmute: — mute notifications from notification message
    # -----------------------------------------------------------------------

    async def _notif_mute(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        # nmute:entity_id:target_user_id
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid = parts[1]
        try:
            target_uid = int(parts[2])
        except ValueError:
            await cb.answer()
            return

        # Only the target user can mute their own notifications
        if uid != target_uid:
            await cb.answer("\u26d4 Можно отключить только свои уведомления.", show_alert=True)
            return

        mute_until = time.time() + 3600  # 1 hour
        await self._db.set_mute(uid, eid, mute_until)
        await cb.answer("\U0001f515 Уведомления отключены на 1 час.", show_alert=False)

    # -----------------------------------------------------------------------
    # fap: — favorite actions pagination
    # -----------------------------------------------------------------------

    async def _fav_actions_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await cb.answer()
        await self._show_fav_actions(cid, uid, page)

    # -----------------------------------------------------------------------
    # fa_run: — run favorite action
    # -----------------------------------------------------------------------

    async def _fav_action_run(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            action_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Некорректное действие.", show_alert=True)
            return

        actions = await self._db.get_favorite_actions(uid)
        action = next((a for a in actions if a["id"] == action_id), None)
        if not action:
            await cb.answer("Действие не найдено.", show_alert=True)
            return

        if not await self._check_rl(uid, "fav_action", cb):
            return

        await cb.answer("\u2699\ufe0f Выполняю...")
        payload = action["payload"]
        domain = payload.get("domain", "")
        service = payload.get("service", "")
        svc_data = payload.get("data", {})
        if not domain or not service:
            await cb.answer("\u274c Некорректные данные действия.", show_alert=True)
            return

        ok, err = await self._ha.call_service(domain, service, svc_data)
        if ok:
            self._rl.record()
            await cb.answer(f"\u2705 {action.get('label', 'OK')}", show_alert=False)
        else:
            await cb.answer(f"\u274c {(err or '')[:180]}", show_alert=True)

        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action=f"fav_action.{domain}.{service}",
                     entity_id=svc_data.get("entity_id"),
                     success=ok, error=err if not ok else None)

    # -----------------------------------------------------------------------
    # fa_del: — delete favorite action
    # -----------------------------------------------------------------------

    async def _fav_action_del(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            action_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer()
            return

        deleted = await self._db.remove_favorite_action(uid, action_id)
        if deleted:
            await cb.answer("\U0001f5d1 Удалено.")
        else:
            await cb.answer("Не найдено.", show_alert=True)
        await self._show_fav_actions(cid, uid, 0)

    # -----------------------------------------------------------------------
    # srp: — search results pagination
    # -----------------------------------------------------------------------

    async def _search_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await cb.answer()

        cached = self._search_cache.get(cid, [])
        if not cached:
            t, k = build_search_prompt()
            await self._send_or_edit(cid, t, k, menu="search")
            return

        t, k = build_search_results("...", cached, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu="search_results")

    # -----------------------------------------------------------------------
    # actp: — active now pagination
    # -----------------------------------------------------------------------

    async def _active_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await cb.answer()

        cached = self._search_cache.get(cid, [])
        if not cached:
            await self._show_active_now(cid, uid, 0)
            return

        t, k = build_active_now_menu(cached, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu="active")

    # -----------------------------------------------------------------------
    # diag: — diagnostics callbacks
    # -----------------------------------------------------------------------

    async def _diag_cb(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        target = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        if not await self._check_role(uid, "admin"):
            t, k = build_confirmation("\u26d4 Требуются права администратора.", "nav:main")
            await self._send_or_edit(cid, t, k, menu="diag_err")
            return

        if target == "refresh":
            diag_text = await self._diag.get_diagnostics_text()
            t, k = build_diagnostics_menu(diag_text)
            await self._send_or_edit(cid, t, k, menu="diag")
        elif target == "trace":
            trace = await self._diag.trace_last_error()
            t, k = build_confirmation(trace, "nav:main")
            await self._send_or_edit(cid, t, k, menu="diag_trace")

    # -----------------------------------------------------------------------
    # snap: — snapshot detail
    # -----------------------------------------------------------------------

    async def _snap_detail(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            snap_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer()
            return
        await cb.answer()

        snap = await self._db.get_snapshot(snap_id)
        if not snap:
            t, k = build_confirmation("\u274c Снимок не найден.", "menu:snapshots")
            await self._send_or_edit(cid, t, k, menu="snap_err")
            return

        t, k = build_snapshot_detail(snap)
        await self._send_or_edit(cid, t, k, menu="snap_detail")

    # -----------------------------------------------------------------------
    # snapdiff: — snapshot diff
    # -----------------------------------------------------------------------

    async def _snap_diff(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            snap_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer()
            return
        await cb.answer("\U0001f504 Сравниваю...")

        snap = await self._db.get_snapshot(snap_id)
        if not snap:
            t, k = build_confirmation("\u274c Снимок не найден.", "menu:snapshots")
            await self._send_or_edit(cid, t, k, menu="snap_err")
            return

        diff_lines: list[str] = []
        snap_entities = {e["entity_id"]: e for e in snap.get("payload", [])}

        for eid, snap_ent in list(snap_entities.items())[:50]:
            current = await self._ha.get_state(eid)
            if current is None:
                diff_lines.append(f"\u2796 {eid}: removed")
                continue
            old_state = snap_ent.get("state", "?")
            new_state = current.get("state", "?")
            if old_state != new_state:
                diff_lines.append(f"\u2022 {eid}: {old_state} \u2192 {new_state}")

        diff_text = "\n".join(diff_lines[:30]) if diff_lines else "No changes detected."
        t, k = build_snapshot_detail(snap, diff_text)
        await self._send_or_edit(cid, t, k, menu="snap_diff")

    # -----------------------------------------------------------------------
    # snapdel: — delete snapshot
    # -----------------------------------------------------------------------

    async def _snap_del(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            snap_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer()
            return

        deleted = await self._db.delete_snapshot(snap_id, uid)
        if deleted:
            await cb.answer("\U0001f5d1 Снимок удалён.")
        else:
            await cb.answer("Не найдено.", show_alert=True)

        snaps = await self._db.get_snapshots(uid)
        t, k = build_snapshots_list(snaps)
        await self._send_or_edit(cid, t, k, menu="snapshots")

    # -----------------------------------------------------------------------
    # schtog: — toggle schedule
    # -----------------------------------------------------------------------

    async def _sched_toggle(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            sched_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer()
            return

        result = await self._db.toggle_schedule(sched_id, uid)
        if result is None:
            await cb.answer("Не найдено.", show_alert=True)
            return
        label = "включено" if result else "отключено"
        await cb.answer(f"\u23f0 Расписание {label}.")

        scheds = await self._db.get_schedules(uid)
        t, k = build_schedule_list(scheds)
        await self._send_or_edit(cid, t, k, menu="schedule")

    # -----------------------------------------------------------------------
    # schdel: — delete schedule
    # -----------------------------------------------------------------------

    async def _sched_del(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        try:
            sched_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer()
            return

        deleted = await self._db.delete_schedule(sched_id, uid)
        if deleted:
            await cb.answer("\U0001f5d1 Удалено.")
        else:
            await cb.answer("Не найдено.", show_alert=True)

        scheds = await self._db.get_schedules(uid)
        t, k = build_schedule_list(scheds)
        await self._send_or_edit(cid, t, k, menu="schedule")

    # -----------------------------------------------------------------------
    # vrooms: — vacuum room selection menu
    # -----------------------------------------------------------------------

    async def _vac_rooms(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        state = await self._ha.get_state(eid)
        name = state.get("attributes", {}).get("friendly_name", eid) if state else eid

        segments = await self._vac.get_rooms(eid)
        if not segments:
            t, k = build_confirmation(
                "\U0001f916 Уборка по комнатам недоступна.\nНет сегментов/комнат.",
                f"ent:{eid}",
            )
            await self._send_or_edit(cid, t, k, menu="vac_rooms", entity=eid)
            return

        t, k = build_vacuum_rooms(eid, name, segments)
        await self._send_or_edit(cid, t, k, menu="vac_rooms", entity=eid)

    # -----------------------------------------------------------------------
    # vroom: — select vacuum room
    # -----------------------------------------------------------------------

    async def _vac_room_sel(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, seg_id = parts[1], parts[2]
        await cb.answer()

        state = await self._ha.get_state(eid)
        name = state.get("attributes", {}).get("friendly_name", eid) if state else eid

        segments = await self._vac.get_rooms(eid)
        t, k = build_vacuum_rooms(eid, name, segments, selected_room=seg_id)
        await self._send_or_edit(cid, t, k, menu="vac_room_sel", entity=eid, room=seg_id)

    # -----------------------------------------------------------------------
    # vseg: — start segment clean
    # -----------------------------------------------------------------------

    async def _vac_seg_clean(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer("Некорректные данные.", show_alert=True)
            return
        eid, seg_id = parts[1], parts[2]
        if not await self._check_rl(uid, "vacuum.segment_clean", cb):
            return

        await cb.answer("\u2699\ufe0f Запускаю уборку...")

        ok, err = await self._vac.clean_segment(eid, seg_id)
        if ok:
            self._rl.record()

        segments = await self._vac.get_rooms(eid)
        seg_display = self._vac.get_segment_display_name(seg_id, segments)

        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="vacuum.segment_clean", entity_id=eid,
                     success=ok, error=err if not ok else None)

        if ok:
            t, k = build_confirmation(
                f"\u2705 Уборка начата: {seg_display}", f"ent:{eid}",
            )
        else:
            t, k = build_confirmation(
                f"\u274c Ошибка: {(err or 'Unknown')[:200]}", f"ent:{eid}",
            )
        await self._send_or_edit(cid, t, k, menu="vac_result", entity=eid)

    # -----------------------------------------------------------------------
    # vcmd: — vacuum simple commands (stop, return_to_base)
    # -----------------------------------------------------------------------

    async def _vac_cmd(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        eid, command = parts[1], parts[2]
        if command not in ("stop", "return_to_base"):
            await cb.answer("Недопустимая команда.", show_alert=True)
            return
        if not await self._check_rl(uid, f"vacuum.{command}", cb):
            return

        await cb.answer("\u2699\ufe0f Выполняю...")
        ok, err = await self._vac.execute_command(eid, command)
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action=f"vacuum.{command}", entity_id=eid,
                     success=ok, error=err if not ok else None)

        label = command.replace("_", " ").title()
        if ok:
            t, k = build_confirmation(f"\u2705 Vacuum: {label}", f"ent:{eid}")
        else:
            t, k = build_confirmation(f"\u274c {label}: {(err or '')[:200]}", f"ent:{eid}")
        await self._send_or_edit(cid, t, k, menu="vac_result", entity=eid)

    # -----------------------------------------------------------------------
    # vrtn: — vacuum routines list
    # -----------------------------------------------------------------------

    async def _vac_routines(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        state = await self._ha.get_state(eid)
        name = state.get("attributes", {}).get("friendly_name", eid) if state else eid

        routines = await self._vac.get_routines(eid)
        t, k = build_vacuum_routines(eid, name, routines)
        await self._send_or_edit(cid, t, k, menu="vac_routines", entity=eid)

    # -----------------------------------------------------------------------
    # rtn: — press routine button
    # -----------------------------------------------------------------------

    async def _routine_press(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        btn_eid = data.split(":", 1)[1] if ":" in data else ""
        if not btn_eid:
            await cb.answer()
            return
        if not await self._check_rl(uid, "button.press", cb):
            return

        await cb.answer("\u2699\ufe0f Запускаю сценарий...")
        ok, err = await self._vac.press_routine(btn_eid)
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="button.press", entity_id=btn_eid,
                     success=ok, error=err if not ok else None)

        # Find parent vacuum for back button
        parent_vac = ""
        for vac_eid, btns in self._reg.vacuum_routines.items():
            if btn_eid in btns:
                parent_vac = vac_eid
                break

        back = f"vrtn:{parent_vac}" if parent_vac else "nav:main"
        if ok:
            t, k = build_confirmation(f"\u2705 Сценарий запущен!", back)
        else:
            t, k = build_confirmation(f"\u274c Ошибка: {(err or '')[:200]}", back)
        await self._send_or_edit(cid, t, k, menu="rtn_result")

    # -----------------------------------------------------------------------
    # Manage / Floors / Areas display
    # -----------------------------------------------------------------------

    async def _show_manage(self, cid: int) -> None:
        """Show floors if available, else show areas directly."""
        if self._reg.has_floors:
            await self._show_floors(cid)
        else:
            await self._show_areas_direct(cid)

    async def _show_floors(self, cid: int) -> None:
        domains = frozenset(self._cfg.menu_domains_allowlist)
        sa = self._cfg.show_all_enabled
        floors_sorted = self._reg.get_floors_sorted()
        floor_dicts = []
        for f in floors_sorted:
            areas = self._reg.get_areas_for_floor(f.floor_id)
            area_count = 0
            for a in areas:
                if self._reg.get_area_entities(a.area_id, domains, show_all=sa):
                    area_count += 1
            if area_count > 0:
                floor_dicts.append({
                    "floor_id": f.floor_id,
                    "name": f.name,
                    "area_count": area_count,
                })

        unassigned_areas = self._reg.get_unassigned_areas()
        ua_count = sum(
            1 for a in unassigned_areas
            if self._reg.get_area_entities(a.area_id, domains, show_all=sa)
        )

        t, k = build_floors_menu(floor_dicts, ua_count)
        await self._send_or_edit(cid, t, k, menu="floors")

    async def _show_areas_direct(self, cid: int) -> None:
        """Show all areas without floor grouping."""
        domains = frozenset(self._cfg.menu_domains_allowlist)
        sa = self._cfg.show_all_enabled
        all_areas = self._reg.get_all_areas_sorted()
        area_dicts = []
        for a in all_areas:
            eids = self._reg.get_area_entities(a.area_id, domains, show_all=sa)
            if eids:
                area_dicts.append({"area_id": a.area_id, "name": a.name, "entity_count": len(eids)})

        unassigned = self._reg.get_unassigned_entities(domains, show_all=sa)
        t, k = build_areas_menu(
            area_dicts, back_target="nav:main",
            unassigned_entity_count=len(unassigned),
        )
        await self._send_or_edit(cid, t, k, menu="areas")

    async def _show_favorites(self, cid: int, uid: int, page: int) -> None:
        fav_eids = await self._db.get_favorites(uid)
        ent_list = await self._enrich_entities(fav_eids)
        fav_actions = await self._db.get_favorite_actions(uid)
        pinned = await self._db.get_pinned_items(uid)
        t, k = build_favorites_menu(ent_list, page, self._cfg.menu_page_size, fav_actions, pinned)
        await self._send_or_edit(cid, t, k, menu="favorites")

    async def _show_active_now(self, cid: int, uid: int, page: int) -> None:
        """Show all entities that are currently in an active state, deduplicated by device."""
        _DOMAIN_RANK = {
            "vacuum": 0, "media_player": 1, "climate": 2, "light": 3,
            "cover": 4, "fan": 5, "switch": 6, "lock": 7, "water_heater": 8,
        }
        domains = frozenset(self._cfg.menu_domains_allowlist)
        try:
            all_states = await self._ha.list_states()
        except Exception:
            logger.exception("Failed to fetch states for Active Now")
            all_states = []
        # Collect best active entity per device, prioritising by domain rank
        device_best: dict[str, tuple[int, dict[str, Any]]] = {}
        no_device: list[dict[str, Any]] = []
        for s in all_states:
            eid = s.get("entity_id", "")
            if not eid:
                continue
            domain = eid.split(".", 1)[0]
            if domain not in domains:
                continue
            state_val = s.get("state", "")
            mapped = map_state(eid, state_val, s.get("attributes", {}),
                               self._device_overrides)
            if not mapped.is_active:
                continue
            fname = s.get("attributes", {}).get("friendly_name", eid)
            entry = {
                "entity_id": eid,
                "friendly_name": fname,
                "state": state_val,
                "domain": domain,
            }
            ent_info = self._reg.entities.get(eid)
            dev_id = ent_info.device_id if ent_info and ent_info.device_id else None
            if dev_id:
                rank = _DOMAIN_RANK.get(domain, 99)
                existing = device_best.get(dev_id)
                if existing is None or rank < existing[0]:
                    device_best[dev_id] = (rank, entry)
            else:
                no_device.append(entry)
        active = [ent for _, ent in device_best.values()] + no_device
        # Cache for pagination
        self._search_cache[cid] = active
        t, k = build_active_now_menu(active, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu="active")

    async def _show_fav_actions(self, cid: int, uid: int, page: int) -> None:
        actions = await self._db.get_favorite_actions(uid)
        t, k = build_fav_actions_menu(actions, page)
        await self._send_or_edit(cid, t, k, menu="fav_actions")

    async def _show_notif_list(self, cid: int, uid: int, page: int) -> None:
        subs = await self._db.get_user_notifications(uid)
        enriched = []
        for sub in subs:
            state = await self._ha.get_state(sub["entity_id"])
            fname = sub["entity_id"]
            if state:
                fname = state.get("attributes", {}).get("friendly_name", sub["entity_id"])
            enriched.append({**sub, "friendly_name": fname})
        t, k = build_notif_list(enriched, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu="notif")

    async def _do_refresh(self, cid: int) -> None:
        t, k = build_confirmation("\U0001f504 Синхронизация с Home Assistant...", "nav:main")
        await self._send_or_edit(cid, t, k, menu="refreshing")

        # Snapshot before sync for diff
        old_areas = set(self._reg.areas.keys())
        old_devices = set(self._reg.devices.keys())
        old_entities = set(self._reg.entities.keys())

        try:
            ok = await self._reg.sync()
        except Exception:
            logger.exception("Registry sync error during refresh")
            ok = False
        if ok:
            # Friendly summary with counts
            nf = len(self._reg.floors)
            na = len(self._reg.areas)
            nd = len(self._reg.devices)
            ne = len(self._reg.entities)
            lines = [
                "\u2705 <b>Синхронизация завершена!</b>",
                "",
                f"\U0001f3e2 Этажей: <b>{nf}</b>",
                f"\U0001f3e0 Комнат: <b>{na}</b>",
                f"\U0001f4f1 Устройств: <b>{nd}</b>",
                f"\U0001f50c Сущностей: <b>{ne}</b>",
            ]

            # Diff summary
            new_areas = set(self._reg.areas.keys())
            new_devices = set(self._reg.devices.keys())
            new_entities = set(self._reg.entities.keys())

            diff_parts: list[str] = []
            added_a = len(new_areas - old_areas)
            removed_a = len(old_areas - new_areas)
            added_d = len(new_devices - old_devices)
            removed_d = len(old_devices - new_devices)
            added_e = len(new_entities - old_entities)
            removed_e = len(old_entities - new_entities)

            if added_a:
                diff_parts.append(f"+{added_a} комнат")
            if removed_a:
                diff_parts.append(f"-{removed_a} комнат")
            if added_d:
                diff_parts.append(f"+{added_d} устройств")
            if removed_d:
                diff_parts.append(f"-{removed_d} устройств")
            if added_e:
                diff_parts.append(f"+{added_e} сущностей")
            if removed_e:
                diff_parts.append(f"-{removed_e} сущностей")

            if diff_parts:
                lines.append("")
                lines.append(f"\U0001f504 Изменения: {', '.join(diff_parts)}")
            else:
                lines.append("")
                lines.append("\u2714 Изменений не обнаружено.")

            msg = "\n".join(lines)
        else:
            msg = "\u274c Ошибка синхронизации. Проверьте логи."
        t, k = build_confirmation(msg, "nav:main")
        await self._send_or_edit(cid, t, k, menu="refreshed")

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    async def _do_search(
        self, cid: int, uid: int, query: str,
        message: Message | None = None,
    ) -> None:
        query_lower = query.lower()
        all_states = await self._ha.list_states()
        results: list[dict[str, Any]] = []

        for s in all_states:
            eid = s.get("entity_id", "")
            fname = s.get("attributes", {}).get("friendly_name", eid)
            if query_lower in eid.lower() or query_lower in fname.lower():
                results.append({
                    "entity_id": eid,
                    "friendly_name": fname,
                    "state": s.get("state", "unknown"),
                    "domain": eid.split(".", 1)[0],
                })

        self._search_cache[cid] = results

        t, k = build_search_results(query, results, 0, self._cfg.menu_page_size)
        tid = self._get_thread_id(message) if message else None
        await self._send_or_edit(
            cid, t, k, source=message, menu="search_results", thread_id=tid,
        )

    # -----------------------------------------------------------------------
    # Data helpers
    # -----------------------------------------------------------------------

    async def _enrich_entities(self, eids: list[str]) -> list[dict[str, Any]]:
        """Fetch state for entity IDs, return enriched dicts for UI."""
        result: list[dict[str, Any]] = []
        for eid in eids:
            state = await self._ha.get_state(eid)
            if state and isinstance(state, dict):
                result.append({
                    "entity_id": eid,
                    "friendly_name": state.get("attributes", {}).get("friendly_name", eid),
                    "state": state.get("state", "unknown"),
                    "domain": eid.split(".", 1)[0],
                })
            else:
                result.append({
                    "entity_id": eid,
                    "friendly_name": eid,
                    "state": "unavailable",
                    "domain": eid.split(".", 1)[0],
                })
        return result

    async def _fetch_status_entities(self) -> list[dict[str, Any]]:
        if self._cfg.status_entities:
            entities: list[dict[str, Any]] = []
            for eid in self._cfg.status_entities:
                state = await self._ha.get_state(eid)
                if state and isinstance(state, dict):
                    entities.append(state)
                else:
                    entities.append({"entity_id": eid, "state": "unavailable", "attributes": {}})
            return entities
        # No status_entities configured — show currently active entities
        try:
            all_states = await self._ha.list_states()
        except Exception:
            logger.exception("Failed to fetch states for Status")
            return []
        domains = frozenset(self._cfg.menu_domains_allowlist)
        active: list[dict[str, Any]] = []
        for s in all_states:
            eid = s.get("entity_id", "")
            if not eid:
                continue
            domain = eid.split(".", 1)[0]
            if domain not in domains:
                continue
            state_val = s.get("state", "")
            mapped = map_state(eid, state_val, s.get("attributes", {}),
                               self._device_overrides)
            if mapped.is_active:
                active.append(s)
        if not active:
            # Nothing active — show summary of all entities by domain count
            domain_counts: dict[str, int] = {}
            for s in all_states:
                eid = s.get("entity_id", "")
                if not eid:
                    continue
                domain = eid.split(".", 1)[0]
                if domain in domains:
                    domain_counts[domain] = domain_counts.get(domain, 0) + 1
            summary: list[dict[str, Any]] = []
            for d, cnt in sorted(domain_counts.items()):
                summary.append({
                    "entity_id": f"{d}._summary",
                    "state": str(cnt),
                    "attributes": {"friendly_name": f"{d} ({cnt})", "unit_of_measurement": "шт."},
                })
            return summary
        return active

    # -----------------------------------------------------------------------
    # Route table (prefix -> handler)
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Scenarios menu (unassigned scene/script entities)
    # -----------------------------------------------------------------------

    async def _show_scenarios(self, cid: int, page: int = 0) -> None:
        """Show all scene and script entities grouped as scenarios."""
        scenarios: list[dict[str, Any]] = []
        for eid, ent in self._reg.entities.items():
            if ent.disabled_by:
                continue
            domain = eid.split(".", 1)[0]
            if domain in ("scene", "script"):
                name = ent.name or ent.original_name or eid
                scenarios.append({"entity_id": eid, "friendly_name": name, "domain": domain})
        scenarios.sort(key=lambda x: x["friendly_name"].lower())
        t, k = build_scenarios_menu(scenarios, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu="scenarios")

    async def _scenario_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        await cb.answer()
        await self._show_scenarios(cid, page)

    # -----------------------------------------------------------------------
    # Quick scene activation (qsc:entity_id)
    # -----------------------------------------------------------------------

    async def _quick_scene(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        _, eid = data.split(":", 1) if ":" in data else ("", "")
        if not eid:
            await cb.answer("\u274c Не найден сценарий")
            return
        domain = eid.split(".", 1)[0]
        try:
            ok, _err = await self._ha.call_service(domain, "turn_on", {"entity_id": eid})
        except Exception:
            logger.exception("Quick scene call failed for %s", eid)
            ok = False
        if ok:
            fname = self._reg.get_entity_display_name(eid)
            await cb.answer(f"\u2705 {fname}")
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="quick_scene", entity_id=eid, success=True)
        else:
            await cb.answer("\u274c Ошибка вызова", show_alert=True)
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="quick_scene", entity_id=eid, success=False, error="call failed")

    # -----------------------------------------------------------------------
    # Light color presets (lclr:entity_id, lcs:entity_id:color_name)
    # -----------------------------------------------------------------------

    async def _light_color_menu(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        _, eid = data.split(":", 1) if ":" in data else ("", "")
        await cb.answer()
        if not eid:
            return
        fname = self._reg.get_entity_display_name(eid)
        back_cb = f"ent:{eid}"
        t, k = build_light_color_menu(eid, fname, back_cb)
        await self._send_or_edit(cid, t, k, menu=f"lclr:{eid}")

    async def _light_color_set(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            await cb.answer("\u274c Неверный формат")
            return
        eid = parts[1]
        try:
            color_idx = int(parts[2])
        except ValueError:
            await cb.answer("\u274c Неверный индекс цвета")
            return
        if color_idx < 0 or color_idx >= len(COLOR_PRESETS):
            await cb.answer("\u274c Цвет не найден")
            return
        label, rgb = COLOR_PRESETS[color_idx]
        try:
            ok, _err = await self._ha.call_service(
                "light", "turn_on",
                {"entity_id": eid, "rgb_color": list(rgb)},
            )
        except Exception:
            logger.exception("Light color set failed for %s", eid)
            ok = False
        if ok:
            await cb.answer(f"\u2705 {label}")
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="light_color", entity_id=eid, success=True)
        else:
            await cb.answer("\u274c Ошибка", show_alert=True)
        # Refresh entity control
        await self._show_entity_control(cid, uid, eid)

    # -----------------------------------------------------------------------
    # Global color scenes (gcl:color_name)
    # -----------------------------------------------------------------------

    async def _global_color(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":")
        try:
            color_idx = int(parts[1]) if len(parts) > 1 else -1
        except ValueError:
            color_idx = -1
        if color_idx < 0 or color_idx >= len(COLOR_PRESETS):
            await cb.answer("\u274c Цвет не найден")
            return
        label, rgb = COLOR_PRESETS[color_idx]

        # Find all light entities that support color (rgb)
        try:
            all_states = await self._ha.list_states()
        except Exception:
            logger.exception("Failed to fetch states for global color")
            await cb.answer("\u274c Ошибка получения состояний", show_alert=True)
            return

        count = 0
        for s in all_states:
            eid = s.get("entity_id", "")
            if not eid.startswith("light."):
                continue
            attrs = s.get("attributes", {})
            color_modes = attrs.get("supported_color_modes", [])
            if any(m in ("rgb", "rgbw", "rgbww", "hs", "xy") for m in color_modes):
                try:
                    ok, _err = await self._ha.call_service(
                        "light", "turn_on",
                        {"entity_id": eid, "rgb_color": list(rgb)},
                    )
                    if ok:
                        count += 1
                except Exception:
                    logger.warning("Failed to set color on %s", eid)

        await cb.answer(f"\u2705 {label} — {count} ламп")
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="global_color", entity_id=label, success=True)
        t, k = build_global_color_result(label, count)
        await self._send_or_edit(cid, t, k, menu="gcolor_result")

    # -----------------------------------------------------------------------
    # Radio controls (rad:action, rout:entity_id)
    # -----------------------------------------------------------------------

    async def _show_radio(self, cid: int) -> None:
        """Show radio menu with current state."""
        state = self._radio_state.get(cid, {})
        # Find available media_player entities
        players: list[dict[str, Any]] = []
        try:
            all_states = await self._ha.list_states()
        except Exception:
            logger.exception("Failed to fetch states for radio")
            all_states = []
        for s in all_states:
            eid = s.get("entity_id", "")
            if eid.startswith("media_player."):
                fname = s.get("attributes", {}).get("friendly_name", eid)
                players.append({"entity_id": eid, "friendly_name": fname})

        # Get configured stations from config
        stations = list(self._cfg.radio_stations)

        current_station = state.get("station_idx", 0)
        current_player = state.get("player_eid", "")
        is_playing = state.get("playing", False)

        t, k = build_radio_menu(
            stations=stations,
            players=players,
            current_idx=current_station,
            current_player=current_player,
            is_playing=is_playing,
        )
        await self._send_or_edit(cid, t, k, menu="radio")

    async def _radio_control(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        state = self._radio_state.setdefault(cid, {"station_idx": 0, "player_eid": "", "playing": False})

        stations = list(self._cfg.radio_stations)

        if action == "play":
            player = state.get("player_eid", "")
            if not player:
                await cb.answer("\u26a0 Выберите устройство вывода", show_alert=True)
                return
            idx = state.get("station_idx", 0)
            if idx < len(stations):
                station = stations[idx]
                try:
                    ok, _err = await self._ha.call_service(
                        "media_player", "play_media",
                        {
                            "entity_id": player,
                            "media_content_id": station["url"],
                            "media_content_type": "music",
                        },
                    )
                except Exception:
                    logger.exception("Radio play failed")
                    ok = False
                if ok:
                    state["playing"] = True
                    await cb.answer(f"\u25b6 {station['name']}")
                    await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                                 action="radio_play", entity_id=player, success=True)
                else:
                    await cb.answer("\u274c Ошибка воспроизведения", show_alert=True)
            await self._show_radio(cid)

        elif action == "stop":
            player = state.get("player_eid", "")
            if player:
                try:
                    await self._ha.call_service("media_player", "media_stop", {"entity_id": player})
                except Exception:
                    logger.warning("Radio stop failed for %s", player)
            state["playing"] = False
            await cb.answer("\u23f9 Остановлено")
            await self._show_radio(cid)

        elif action == "next":
            idx = state.get("station_idx", 0)
            state["station_idx"] = (idx + 1) % max(len(stations), 1)
            await cb.answer()
            if state.get("playing"):
                # Auto-play next station
                new_idx = state["station_idx"]
                player = state.get("player_eid", "")
                if player and new_idx < len(stations):
                    try:
                        await self._ha.call_service(
                            "media_player", "play_media",
                            {
                                "entity_id": player,
                                "media_content_id": stations[new_idx]["url"],
                                "media_content_type": "music",
                            },
                        )
                    except Exception:
                        pass
            await self._show_radio(cid)

        elif action == "prev":
            idx = state.get("station_idx", 0)
            state["station_idx"] = (idx - 1) % max(len(stations), 1)
            await cb.answer()
            if state.get("playing"):
                new_idx = state["station_idx"]
                player = state.get("player_eid", "")
                if player and new_idx < len(stations):
                    try:
                        await self._ha.call_service(
                            "media_player", "play_media",
                            {
                                "entity_id": player,
                                "media_content_id": stations[new_idx]["url"],
                                "media_content_type": "music",
                            },
                        )
                    except Exception:
                        pass
            await self._show_radio(cid)
        else:
            await cb.answer()

    async def _radio_output(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":", 1)
        player_eid = parts[1] if len(parts) > 1 else ""
        state = self._radio_state.setdefault(cid, {"station_idx": 0, "player_eid": "", "playing": False})
        state["player_eid"] = player_eid
        fname = self._reg.get_entity_display_name(player_eid)
        await cb.answer(f"\U0001f50a {fname}")
        await self._show_radio(cid)

    # -----------------------------------------------------------------------
    # Automations
    # -----------------------------------------------------------------------

    async def _show_automations(self, cid: int, page: int = 0) -> None:
        """List all automation entities with state."""
        try:
            all_states = await self._ha.list_states()
        except Exception:
            logger.exception("Failed to fetch states for Automations")
            all_states = []
        automations: list[dict[str, Any]] = []
        for s in all_states:
            eid = s.get("entity_id", "")
            if eid.startswith("automation."):
                fname = s.get("attributes", {}).get("friendly_name", eid)
                automations.append({
                    "entity_id": eid,
                    "friendly_name": fname,
                    "state": s.get("state", "off"),
                })
        automations.sort(key=lambda x: x["friendly_name"].lower())
        t, k = build_automations_menu(automations, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu="automations")

    async def _automation_toggle(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        """Toggle automation on/off."""
        parts = data.split(":", 1)
        eid = parts[1] if len(parts) > 1 else ""
        if not eid:
            await cb.answer("Некорректная автоматизация.", show_alert=True)
            return
        # Get current state to decide enable/disable
        state = await self._ha.get_state(eid)
        current = state.get("state", "off") if state else "off"
        service = "turn_off" if current == "on" else "turn_on"
        try:
            ok, err = await self._ha.call_service("automation", service, {"entity_id": eid})
        except Exception as exc:
            logger.exception("Automation toggle failed for %s", eid)
            ok, err = False, str(exc)[:200]
        if ok:
            new_state = "выкл" if service == "turn_off" else "вкл"
            await cb.answer(f"\U0001f916 {new_state}")
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action=f"automation.{service}", entity_id=eid, success=True)
        else:
            await cb.answer(f"Ошибка: {(err or '')[:180]}", show_alert=True)
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action=f"automation.{service}", entity_id=eid, success=False, error=err)
        await self._show_automations(cid)

    async def _automation_trigger(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        """Trigger an automation."""
        parts = data.split(":", 1)
        eid = parts[1] if len(parts) > 1 else ""
        if not eid:
            await cb.answer("Некорректная автоматизация.", show_alert=True)
            return
        try:
            ok, err = await self._ha.call_service("automation", "trigger", {"entity_id": eid})
        except Exception as exc:
            logger.exception("Automation trigger failed for %s", eid)
            ok, err = False, str(exc)[:200]
        if ok:
            await cb.answer("\u25b6 Запущено")
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="automation.trigger", entity_id=eid, success=True)
        else:
            await cb.answer(f"Ошибка: {(err or '')[:180]}", show_alert=True)
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="automation.trigger", entity_id=eid, success=False, error=err)

    async def _automation_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        parts = data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        await cb.answer()
        await self._show_automations(cid, page)

    # -----------------------------------------------------------------------
    # To-Do lists
    # -----------------------------------------------------------------------

    async def _show_todo_lists(self, cid: int) -> None:
        """List all to-do list entities from HA."""
        try:
            all_states = await self._ha.list_states()
        except Exception:
            logger.exception("Failed to fetch states for To-Do")
            all_states = []
        todo_lists: list[dict[str, Any]] = []
        for s in all_states:
            eid = s.get("entity_id", "")
            if eid.startswith("todo."):
                fname = s.get("attributes", {}).get("friendly_name", eid)
                todo_lists.append({"entity_id": eid, "friendly_name": fname})
        todo_lists.sort(key=lambda x: x["friendly_name"].lower())
        t, k = build_todo_lists_menu(todo_lists)
        await self._send_or_edit(cid, t, k, menu="todo")

    async def _todo_list_items(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        """Show items in a selected to-do list."""
        parts = data.split(":", 1)
        list_eid = parts[1] if len(parts) > 1 else ""
        if not list_eid:
            await cb.answer("Некорректный список.", show_alert=True)
            return
        await cb.answer()
        await self._show_todo_items(cid, list_eid, 0)

    async def _show_todo_items(self, cid: int, list_eid: str, page: int) -> None:
        """Fetch and display items from a to-do list."""
        # Get list name
        state = await self._ha.get_state(list_eid)
        list_name = state.get("attributes", {}).get("friendly_name", list_eid) if state else list_eid
        # Fetch items via todo.get_items service
        try:
            ok, result = await self._ha.call_service(
                "todo", "get_items", {"entity_id": list_eid},
            )
        except Exception:
            logger.exception("Failed to get todo items for %s", list_eid)
            ok, result = False, "Ошибка загрузки"
        items: list[dict[str, Any]] = []
        if ok and isinstance(result, dict):
            # HA returns items under the entity_id key in response
            items = result.get(list_eid, {}).get("items", [])
        elif ok and isinstance(result, list):
            items = result
        t, k = build_todo_items_menu(list_name, list_eid, items, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu=f"todo:{list_eid}")

    async def _todo_complete_item(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        """Mark a to-do item as completed."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer("Некорректные данные.", show_alert=True)
            return
        list_eid, item_uid = parts[1], parts[2]
        try:
            ok, err = await self._ha.call_service(
                "todo", "update_item",
                {"entity_id": list_eid, "item": item_uid, "status": "completed"},
            )
        except Exception as exc:
            logger.exception("Todo complete failed")
            ok, err = False, str(exc)[:200]
        if ok:
            await cb.answer("\u2705 Выполнено")
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="todo.complete", entity_id=list_eid, success=True)
        else:
            await cb.answer(f"Ошибка: {(err or '')[:180]}", show_alert=True)
        await self._show_todo_items(cid, list_eid, 0)

    async def _todo_delete_item(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        """Delete a to-do item."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer("Некорректные данные.", show_alert=True)
            return
        list_eid, item_uid = parts[1], parts[2]
        try:
            ok, err = await self._ha.call_service(
                "todo", "remove_item",
                {"entity_id": list_eid, "item": item_uid},
            )
        except Exception as exc:
            logger.exception("Todo delete failed")
            ok, err = False, str(exc)[:200]
        if ok:
            await cb.answer("\U0001f5d1 Удалено")
            await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                         action="todo.delete", entity_id=list_eid, success=True)
        else:
            await cb.answer(f"Ошибка: {(err or '')[:180]}", show_alert=True)
        await self._show_todo_items(cid, list_eid, 0)

    async def _todo_add_start(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        """Enter add-item mode: next text message will be the item summary."""
        parts = data.split(":", 1)
        list_eid = parts[1] if len(parts) > 1 else ""
        if not list_eid:
            await cb.answer("Некорректный список.", show_alert=True)
            return
        self._todo_add_pending[cid] = list_eid
        await cb.answer()
        t = "\U0001f4cb Введите текст новой задачи в чат:"
        k = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u274c Отмена", callback_data=f"menu:todo")],
        ])
        await self._send_or_edit(cid, t, k, menu="todo_add")

    async def _todo_page(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        """Paginate to-do items."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await cb.answer()
            return
        list_eid = parts[1]
        page = int(parts[2]) if parts[2].isdigit() else 0
        await cb.answer()
        await self._show_todo_items(cid, list_eid, page)

    # -----------------------------------------------------------------------
    # /terminal command
    # -----------------------------------------------------------------------

    async def cmd_terminal(self, message: Message) -> None:
        """Execute a shell command (local container only, restricted)."""
        uid, uname = self._extract_user(message)
        if uid is None:
            return
        cid = message.chat.id
        if not self._is_authorized_chat(cid):
            await message.answer("\u26d4 Неавторизованный чат.")
            return
        if not self._is_authorized_user(uid):
            await message.answer("\u26d4 Неавторизованный пользователь.")
            return
        # Terminal must be explicitly enabled in config
        if not getattr(self._cfg, "terminal_enabled", False):
            await message.answer(
                "\u26d4 Терминал отключён.\n"
                "Включите <code>terminal_enabled: true</code> в настройках add-on.",
                parse_mode="HTML",
            )
            return
        # Admin only
        role = await self._db.get_user_role(uid)
        if role != "admin":
            await message.answer("\u26d4 Только для администраторов.")
            return

        parts = (message.text or "").split(maxsplit=1)
        command = parts[1].strip() if len(parts) > 1 else ""
        if not command:
            await message.answer(
                "\U0001f4bb <b>Терминал</b>\n\n"
                "Использование: /terminal <code>команда</code>\n"
                "Пример: /terminal ls -la /app",
                parse_mode="HTML",
            )
            return

        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="terminal", entity_id=None, success=True)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd="/",
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await message.answer("\u23f0 Таймаут (30с). Процесс завершён.")
                return

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            # Limit output length
            if len(output) > 3500:
                output = output[:3500] + "\n... (обрезано)"
            exit_code = proc.returncode or 0
            text = (
                f"\U0001f4bb <b>Терминал</b>\n"
                f"<code>$ {self._sanitize(command[:100])}</code>\n"
                f"Exit: {exit_code}\n\n"
                f"<pre>{self._sanitize(output)}</pre>"
            )
        except Exception as exc:
            text = f"\u274c Ошибка: {self._sanitize(str(exc)[:200])}"

        tid = self._get_thread_id(message)
        try:
            await self._bot.send_message(
                chat_id=cid, text=text, parse_mode="HTML",
                message_thread_id=tid,
            )
        except Exception:
            # Fallback without HTML if parsing fails
            await self._bot.send_message(
                chat_id=cid, text=text[:4000],
                message_thread_id=tid,
            )

    @staticmethod
    def _sanitize(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    _ROUTES: dict[str, Any] = {
        "nav": _nav,
        "menu": _menu,
        "fl": _floor,
        "ar": _area,
        "arp": _area_page,
        "ent": _entity,
        "dev": _device,
        "dvp": _device_page,
        "act": _action,
        "bright": _brightness,
        "clim": _climate,
        "mvol": _media_vol,
        "mmut": _media_mute,
        "msrc": _media_source,
        "msrs": _media_source_sel,
        "ssel": _select_option,
        "nval": _number_val,
        "pin": _pin_toggle,
        "fav": _fav_toggle,
        "ntog": _notif_toggle,
        "favp": _fav_page,
        "nfp": _notif_page,
        "nact": _notif_action,
        "nmute": _notif_mute,
        "fap": _fav_actions_page,
        "fa_run": _fav_action_run,
        "fa_del": _fav_action_del,
        "srp": _search_page,
        "actp": _active_page,
        "diag": _diag_cb,
        "snap": _snap_detail,
        "snapdiff": _snap_diff,
        "snapdel": _snap_del,
        "schtog": _sched_toggle,
        "schdel": _sched_del,
        "vrooms": _vac_rooms,
        "vroom": _vac_room_sel,
        "vseg": _vac_seg_clean,
        "vcmd": _vac_cmd,
        "vrtn": _vac_routines,
        "rtn": _routine_press,
        # New routes
        "qsc": _quick_scene,
        "lclr": _light_color_menu,
        "lcs": _light_color_set,
        "gcl": _global_color,
        "rad": _radio_control,
        "rout": _radio_output,
        "scp": _scenario_page,
        # Automations
        "atog": _automation_toggle,
        "atrig": _automation_trigger,
        "autp": _automation_page,
        # To-Do
        "tdl": _todo_list_items,
        "tdc": _todo_complete_item,
        "tdd": _todo_delete_item,
        "tda": _todo_add_start,
        "tdp": _todo_page,
    }
