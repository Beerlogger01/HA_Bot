"""Telegram callback and command handlers.

Full menu navigation: Floors -> Areas -> Entities -> Controls.
Favorites, notifications, vacuum routines, segment cleaning.
Edit-in-place message management, security checks, rate limiting.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from api import HAClient
from registry import HARegistry
from storage import Database
from ui import (
    build_areas_menu,
    build_confirmation,
    build_entity_control,
    build_entity_list,
    build_favorites_menu,
    build_floors_menu,
    build_help_menu,
    build_main_menu,
    build_notif_list,
    build_status_menu,
    build_vacuum_rooms,
    build_vacuum_routines,
)

logger = logging.getLogger("ha_bot.handlers")

_ALLOWED_SERVICES: frozenset[str] = frozenset({
    "turn_on", "turn_off", "toggle",
    "open_cover", "close_cover", "stop_cover",
    "start", "stop", "pause", "return_to_base", "locate",
    "lock", "unlock",
    "press",
    "media_play", "media_pause", "media_stop",
    "set_temperature",
})

_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9_\-]*$")


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
    ) -> None:
        self._bot = bot
        self._ha = ha
        self._db = db
        self._cfg = config
        self._rl = global_rl
        self._reg = registry
        self.ha_version: str = "unknown"

    # -----------------------------------------------------------------------
    # Security
    # -----------------------------------------------------------------------

    def _is_authorized_chat(self, chat_id: int) -> bool:
        return self._cfg.allowed_chat_id == 0 or chat_id == self._cfg.allowed_chat_id

    def _is_authorized_user(self, user_id: int) -> bool:
        return not self._cfg.allowed_user_ids or user_id in self._cfg.allowed_user_ids

    @staticmethod
    def _extract_user(obj: Message | CallbackQuery) -> tuple[int | None, str]:
        user = obj.from_user
        if user is None:
            return None, "<unknown>"
        return user.id, user.username or user.first_name or str(user.id)

    # -----------------------------------------------------------------------
    # Message management
    # -----------------------------------------------------------------------

    async def _send_or_edit(
        self, chat_id: int, text: str, kb: InlineKeyboardMarkup, *,
        source: Message | None = None,
        menu: str = "main", entity: str | None = None, room: str | None = None,
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
            sent = await self._bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb,
            )
            await self._db.save_menu_state(chat_id, sent.message_id, menu, entity, room)
        except TelegramRetryAfter as e:
            logger.warning("Rate limited on send %ss", e.retry_after)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.error("Failed to send menu: %s", exc)

    # -----------------------------------------------------------------------
    # Rate limits
    # -----------------------------------------------------------------------

    async def _check_rl(self, user_id: int, action: str, cb: CallbackQuery) -> bool:
        allowed, remaining = await self._db.check_and_update_cooldown(
            user_id, action, self._cfg.cooldown_seconds,
        )
        if not allowed:
            await cb.answer(f"\u23f1\ufe0f Подождите {remaining:.1f}с.", show_alert=True)
            return False
        if not self._rl.check():
            await cb.answer("\U0001f6a6 Лимит. Подождите.", show_alert=True)
            return False
        return True

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
        text, kb = build_main_menu()
        await self._send_or_edit(cid, text, kb, source=message, menu="main")

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
        await self._send_or_edit(cid, text, kb, source=message, menu="status")

    async def cmd_ping(self, message: Message) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await message.answer(f"pong  |  HA {self.ha_version}  |  {ts}")

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

        try:
            prefix = data.split(":", 1)[0] if ":" in data else data
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
            t, k = build_main_menu()
            await self._send_or_edit(cid, t, k, menu="main")
        elif target == "manage":
            await self._show_manage(cid)
        elif target == "floors":
            await self._show_floors(cid)
        else:
            t, k = build_main_menu()
            await self._send_or_edit(cid, t, k, menu="main")

    # -----------------------------------------------------------------------
    # menu: — main menu buttons
    # -----------------------------------------------------------------------

    async def _menu(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        target = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        if target == "manage":
            await self._show_manage(cid)
        elif target == "favorites":
            await self._show_favorites(cid, uid, 0)
        elif target == "notif":
            await self._show_notif_list(cid, uid, 0)
        elif target == "refresh":
            await self._do_refresh(cid)
        elif target == "status":
            ents = await self._fetch_status_entities()
            t, k = build_status_menu(ents)
            await self._send_or_edit(cid, t, k, menu="status")
        elif target == "help":
            t, k = build_help_menu()
            await self._send_or_edit(cid, t, k, menu="help")

    # -----------------------------------------------------------------------
    # fl: — floor selected
    # -----------------------------------------------------------------------

    async def _floor(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        floor_id = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        domains = frozenset(self._cfg.menu_domains_allowlist)

        if floor_id == "__none__":
            areas = self._reg.get_unassigned_areas()
        else:
            areas = self._reg.get_areas_for_floor(floor_id)

        area_dicts = []
        for a in areas:
            eids = self._reg.get_area_entities(a.area_id, domains)
            if eids:
                area_dicts.append({"area_id": a.area_id, "name": a.name, "entity_count": len(eids)})

        unassigned = self._reg.get_unassigned_entities(domains)

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
    # ar: — area selected -> entity list
    # -----------------------------------------------------------------------

    async def _area(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        area_id = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        domains = frozenset(self._cfg.menu_domains_allowlist)
        if area_id == "__none__":
            eids = self._reg.get_unassigned_entities(domains)
            title = "\U0001f4e6 <b>Без комнаты</b>"
        else:
            eids = self._reg.get_area_entities(area_id, domains)
            area = self._reg.areas.get(area_id)
            title = f"\U0001f3e0 <b>{area.name if area else area_id}</b>"

        ent_list = await self._enrich_entities(eids)
        t, k = build_entity_list(
            ent_list, 0, self._cfg.menu_page_size,
            title=title, back_cb=f"nav:manage",
            page_cb_prefix=f"arp:{area_id}",
        )
        await self._send_or_edit(cid, t, k, menu=f"area:{area_id}")

    # -----------------------------------------------------------------------
    # arp: — area entity pagination
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
        if area_id == "__none__":
            eids = self._reg.get_unassigned_entities(domains)
            title = "\U0001f4e6 <b>Без комнаты</b>"
        else:
            eids = self._reg.get_area_entities(area_id, domains)
            area = self._reg.areas.get(area_id)
            title = f"\U0001f3e0 <b>{area.name if area else area_id}</b>"

        ent_list = await self._enrich_entities(eids)
        t, k = build_entity_list(
            ent_list, page, self._cfg.menu_page_size,
            title=title, back_cb="nav:manage",
            page_cb_prefix=f"arp:{area_id}",
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

    async def _show_entity_control(self, cid: int, uid: int, eid: str) -> None:
        state = await self._ha.get_state(eid)
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

        t, k = build_entity_control(eid, state, is_fav, is_notif, back)

        # For vacuum, add extra buttons (rooms, routines)
        domain = eid.split(".", 1)[0]
        if domain == "vacuum":
            extra_rows: list[list[InlineKeyboardButton]] = []
            # Room cleaning button
            segments = await self._db.get_vacuum_room_map(eid)
            presets = list(self._cfg.vacuum_room_presets)
            if segments or presets:
                extra_rows.append([InlineKeyboardButton(
                    text="\U0001f3e0 Уборка по комнатам",
                    callback_data=f"vrooms:{eid}",
                )])
            # Routines button
            routines = self._reg.vacuum_routines.get(eid, [])
            if routines:
                extra_rows.append([InlineKeyboardButton(
                    text=f"\U0001f3ac Сценарии ({len(routines)})",
                    callback_data=f"vrtn:{eid}",
                )])
            if extra_rows:
                # Insert before the last two rows (fav/notif + back)
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
        ok, err = await self._ha.call_service(domain, service, {"entity_id": eid})
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action=f"{domain}.{service}", entity_id=eid,
                     success=ok, error=err if not ok else None)

        if ok:
            await self._show_entity_control(cid, uid, eid)
        else:
            await cb.answer(f"Ошибка: {(err or '')[:180]}", show_alert=True)

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

        state = await self._ha.get_state(eid)
        current = (state.get("attributes", {}).get("brightness", 128) or 128) if state else 128
        step = 51
        new_br = min(255, current + step) if direction == "up" else max(1, current - step)

        await cb.answer("\U0001f506 Яркость...")
        ok, err = await self._ha.call_service(
            "light", "turn_on", {"entity_id": eid, "brightness": new_br},
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
    # vrooms: — vacuum room selection menu
    # -----------------------------------------------------------------------

    async def _vac_rooms(self, cid: int, uid: int, uname: str, data: str, cb: CallbackQuery) -> None:
        eid = data.split(":", 1)[1] if ":" in data else ""
        await cb.answer()

        state = await self._ha.get_state(eid)
        name = state.get("attributes", {}).get("friendly_name", eid) if state else eid

        # Try DB segments first, then config presets
        segments = await self._db.get_vacuum_room_map(eid)
        if not segments:
            presets = list(self._cfg.vacuum_room_presets)
            segments = [{"segment_id": r, "segment_name": r.replace("_", " ").title()} for r in presets]

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

        segments = await self._db.get_vacuum_room_map(eid)
        if not segments:
            presets = list(self._cfg.vacuum_room_presets)
            segments = [{"segment_id": r, "segment_name": r.replace("_", " ").title()} for r in presets]

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

        # Try real segment clean (int segment ids) or fallback to presets
        ok = False
        err = ""
        strategy = self._cfg.vacuum_room_strategy

        # Check if seg_id is numeric (real segment from integration)
        try:
            seg_int = int(seg_id)
            is_real = True
        except ValueError:
            is_real = False
            seg_int = 0

        if is_real:
            # Real segment clean via roborock / vacuum.send_command
            ok, err = await self._ha.call_service(
                "vacuum", "send_command",
                {"entity_id": eid, "command": "app_segment_clean", "params": [seg_int]},
            )
        elif strategy == "script" and self._cfg.vacuum_room_script_entity_id:
            ok, err = await self._ha.call_service(
                "script", "turn_on",
                {
                    "entity_id": self._cfg.vacuum_room_script_entity_id,
                    "variables": {"vacuum_entity": eid, "room": seg_id},
                },
            )
        elif strategy == "service_data":
            ok, err = await self._ha.call_service(
                "vacuum", "send_command",
                {"entity_id": eid, "command": "app_segment_clean", "params": {"rooms": [seg_id]}},
            )
        else:
            ok, err = await self._ha.call_service("vacuum", "start", {"entity_id": eid})

        if ok:
            self._rl.record()

        seg_display = seg_id
        segments = await self._db.get_vacuum_room_map(eid)
        for s in segments:
            if s["segment_id"] == seg_id:
                seg_display = s.get("segment_name") or seg_id
                break
        if not segments:
            seg_display = seg_id.replace("_", " ").title()

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
        eid, service = parts[1], parts[2]
        if service not in ("stop", "return_to_base"):
            await cb.answer("Недопустимая команда.", show_alert=True)
            return
        if not await self._check_rl(uid, f"vacuum.{service}", cb):
            return

        await cb.answer("\u2699\ufe0f Выполняю...")
        ok, err = await self._ha.call_service("vacuum", service, {"entity_id": eid})
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action=f"vacuum.{service}", entity_id=eid,
                     success=ok, error=err if not ok else None)

        label = service.replace("_", " ").title()
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

        routine_eids = self._reg.vacuum_routines.get(eid, [])
        routines: list[dict[str, Any]] = []
        for r_eid in routine_eids:
            r_state = await self._ha.get_state(r_eid)
            r_name = r_eid
            if r_state:
                r_name = r_state.get("attributes", {}).get("friendly_name", r_eid)
            routines.append({"entity_id": r_eid, "name": r_name})

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
        ok, err = await self._ha.call_service("button", "press", {"entity_id": btn_eid})
        if ok:
            self._rl.record()
        await _audit(self._db, chat_id=cid, user_id=uid, username=uname,
                     action="button.press", entity_id=btn_eid,
                     success=ok, error=err if not ok else None)

        # Find parent vacuum for back button
        parent_vac = ""
        reg_ent = self._reg.entities.get(btn_eid)
        if reg_ent and reg_ent.device_id:
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
        floors_sorted = self._reg.get_floors_sorted()
        floor_dicts = []
        for f in floors_sorted:
            areas = self._reg.get_areas_for_floor(f.floor_id)
            area_count = 0
            for a in areas:
                if self._reg.get_area_entities(a.area_id, domains):
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
            if self._reg.get_area_entities(a.area_id, domains)
        )

        t, k = build_floors_menu(floor_dicts, ua_count)
        await self._send_or_edit(cid, t, k, menu="floors")

    async def _show_areas_direct(self, cid: int) -> None:
        """Show all areas without floor grouping."""
        domains = frozenset(self._cfg.menu_domains_allowlist)
        all_areas = self._reg.get_all_areas_sorted()
        area_dicts = []
        for a in all_areas:
            eids = self._reg.get_area_entities(a.area_id, domains)
            if eids:
                area_dicts.append({"area_id": a.area_id, "name": a.name, "entity_count": len(eids)})

        unassigned = self._reg.get_unassigned_entities(domains)
        t, k = build_areas_menu(
            area_dicts, back_target="nav:main",
            unassigned_entity_count=len(unassigned),
        )
        await self._send_or_edit(cid, t, k, menu="areas")

    async def _show_favorites(self, cid: int, uid: int, page: int) -> None:
        fav_eids = await self._db.get_favorites(uid)
        ent_list = await self._enrich_entities(fav_eids)
        t, k = build_favorites_menu(ent_list, page, self._cfg.menu_page_size)
        await self._send_or_edit(cid, t, k, menu="favorites")

    async def _show_notif_list(self, cid: int, uid: int, page: int) -> None:
        subs = await self._db.get_user_notifications(uid)
        # Enrich with friendly names
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
        ok = await self._reg.sync()
        if ok:
            msg = (
                f"\u2705 Синхронизация завершена!\n\n"
                f"Этажей: {len(self._reg.floors)}\n"
                f"Комнат: {len(self._reg.areas)}\n"
                f"Устройств: {len(self._reg.devices)}\n"
                f"Сущностей: {len(self._reg.entities)}"
            )
        else:
            msg = "\u274c Ошибка синхронизации. Проверьте логи."
        t, k = build_confirmation(msg, "nav:main")
        await self._send_or_edit(cid, t, k, menu="refreshed")

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
        if not self._cfg.status_entities:
            return []
        entities: list[dict[str, Any]] = []
        for eid in self._cfg.status_entities:
            state = await self._ha.get_state(eid)
            if state and isinstance(state, dict):
                entities.append(state)
            else:
                entities.append({"entity_id": eid, "state": "unavailable", "attributes": {}})
        return entities

    # -----------------------------------------------------------------------
    # Route table (prefix -> handler)
    # -----------------------------------------------------------------------

    _ROUTES: dict[str, Any] = {
        "nav": _nav,
        "menu": _menu,
        "fl": _floor,
        "ar": _area,
        "arp": _area_page,
        "ent": _entity,
        "act": _action,
        "bright": _brightness,
        "clim": _climate,
        "fav": _fav_toggle,
        "ntog": _notif_toggle,
        "favp": _fav_page,
        "nfp": _notif_page,
        "vrooms": _vac_rooms,
        "vroom": _vac_room_sel,
        "vseg": _vac_seg_clean,
        "vcmd": _vac_cmd,
        "vrtn": _vac_routines,
        "rtn": _routine_press,
    }
