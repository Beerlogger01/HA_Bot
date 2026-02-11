"""Telegram callback and command handlers.

Handles multi-level menu navigation, device control, vacuum room targeting,
message cleanup (edit-in-place), security checks, and rate limiting.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from api import HAClient
from storage import Database
from ui import (
    build_confirmation,
    build_devices_menu,
    build_entity_control,
    build_entity_list,
    build_help_menu,
    build_main_menu,
    build_robots_menu,
    build_scenes_menu,
    build_status_menu,
    build_vacuum_rooms,
)

logger = logging.getLogger("ha_bot.handlers")

# Allowed service names for security validation (prevent arbitrary service calls)
_ALLOWED_SERVICES: frozenset[str] = frozenset({
    "turn_on", "turn_off", "toggle",
    "open_cover", "close_cover", "stop_cover",
    "start", "stop", "return_to_base",
    "lock", "unlock",
    "press",
    "media_play", "media_pause", "media_stop",
    "set_temperature",
})

_ALLOWED_VACUUM_SERVICES: frozenset[str] = frozenset({
    "stop", "return_to_base",
})

# Valid entity_id pattern: domain.object_id (alphanumeric, underscore, hyphen)
_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9_\-]*$")


# ---------------------------------------------------------------------------
# In-memory global rate limiter (sliding window)
# ---------------------------------------------------------------------------


class GlobalRateLimiter:
    """Simple in-memory sliding-window counter.

    Only counts successful action attempts (not navigation).
    Operates within single asyncio event loop — no lock required.
    """

    def __init__(self, max_actions: int, window_seconds: int) -> None:
        self._max = max_actions
        self._window = window_seconds
        self._timestamps: list[float] = []

    def check(self) -> bool:
        """Return True if under the limit (does NOT record)."""
        now = time.monotonic()
        cutoff = now - self._window
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        return len(self._timestamps) < self._max

    def record(self) -> None:
        """Record a successful action."""
        self._timestamps.append(time.monotonic())


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


async def _audit(
    db: Database,
    *,
    chat_id: int,
    user_id: int,
    username: str,
    action: str,
    entity_id: str | None = None,
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
            entity_id=entity_id,
            success=success,
            error=error,
        )
    except Exception:
        logger.exception("Failed to persist audit record")


# ---------------------------------------------------------------------------
# Config dataclass (imported from app.py, redeclared here for type hints)
# ---------------------------------------------------------------------------

# We use a dict-like config to avoid circular imports.
# The main app passes a Config object that has these attributes.


# ---------------------------------------------------------------------------
# Handler class
# ---------------------------------------------------------------------------


class Handlers:
    """Registers and dispatches all Telegram handlers."""

    def __init__(
        self,
        bot: Bot,
        ha: HAClient,
        db: Database,
        config: Any,
        global_rl: GlobalRateLimiter,
    ) -> None:
        self._bot = bot
        self._ha = ha
        self._db = db
        self._cfg = config
        self._rl = global_rl

    # -----------------------------------------------------------------------
    # Security helpers
    # -----------------------------------------------------------------------

    def _is_authorized_chat(self, chat_id: int) -> bool:
        return chat_id == self._cfg.allowed_chat_id

    def _is_authorized_user(self, user_id: int) -> bool:
        return bool(self._cfg.allowed_user_ids) and (
            user_id in self._cfg.allowed_user_ids
        )

    @staticmethod
    def _extract_user(obj: Message | CallbackQuery) -> tuple[int | None, str]:
        """Return (user_id, username) — safe when from_user is None."""
        user = obj.from_user
        if user is None:
            return None, "<unknown>"
        name = user.username or user.first_name or str(user.id)
        return user.id, name

    # -----------------------------------------------------------------------
    # Message management — edit-in-place or delete + resend
    # -----------------------------------------------------------------------

    async def _send_or_edit_menu(
        self,
        chat_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup,
        *,
        source_message: Message | None = None,
        current_menu: str = "main",
        selected_entity: str | None = None,
        selected_room: str | None = None,
    ) -> None:
        """Edit existing menu message or send a new one. Track in DB."""
        menu_msg_id = await self._db.get_menu_message_id(chat_id)

        # Try editing existing message
        if menu_msg_id is not None:
            try:
                await self._bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=menu_msg_id,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                await self._db.save_menu_state(
                    chat_id, menu_msg_id, current_menu, selected_entity, selected_room
                )
                return
            except TelegramRetryAfter as e:
                logger.warning("Telegram rate limited, retry after %s seconds", e.retry_after)
                return
            except (TelegramBadRequest, TelegramForbiddenError):
                # Message too old, deleted, or bot blocked — try delete + resend
                try:
                    await self._bot.delete_message(chat_id=chat_id, message_id=menu_msg_id)
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass  # Already gone

        # Delete the command message if present (keeps chat clean)
        if source_message is not None:
            try:
                await source_message.delete()
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

        # Send new message
        try:
            sent = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            await self._db.save_menu_state(
                chat_id, sent.message_id, current_menu, selected_entity, selected_room
            )
        except TelegramRetryAfter as e:
            logger.warning("Telegram rate limited on send, retry after %s", e.retry_after)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.error("Failed to send menu message: %s", exc)

    # -----------------------------------------------------------------------
    # Rate limit check (per-user first, then global)
    # -----------------------------------------------------------------------

    async def _check_rate_limits(
        self, user_id: int, action: str, callback: CallbackQuery
    ) -> bool:
        """Check per-user cooldown first, then global rate limit.

        Returns True if action is allowed.
        """
        # Per-user cooldown first
        allowed, remaining = await self._db.check_and_update_cooldown(
            user_id, action, self._cfg.cooldown_seconds
        )
        if not allowed:
            await callback.answer(
                f"\u23f1\ufe0f Wait {remaining:.1f}s.", show_alert=True
            )
            return False

        # Global rate limit (check only, record on success)
        if not self._rl.check():
            await callback.answer(
                "\U0001f6a6 Rate limit reached. Wait a moment.", show_alert=True
            )
            return False

        return True

    # -----------------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------------

    async def cmd_start(self, message: Message) -> None:
        """Handle /start and /menu commands."""
        user_id, username = self._extract_user(message)
        if user_id is None:
            return
        chat_id = message.chat.id

        if not self._is_authorized_chat(chat_id):
            await _audit(
                self._db, chat_id=chat_id, user_id=user_id, username=username,
                action="/start", success=False, error="Unauthorized chat",
            )
            await message.answer("\u26d4 Unauthorized chat.")
            return

        await _audit(
            self._db, chat_id=chat_id, user_id=user_id, username=username,
            action="/start", success=True,
        )

        text, keyboard = build_main_menu()
        await self._send_or_edit_menu(
            chat_id, text, keyboard, source_message=message, current_menu="main"
        )

    async def cmd_status(self, message: Message) -> None:
        """Handle /status command."""
        user_id, username = self._extract_user(message)
        if user_id is None:
            return
        chat_id = message.chat.id

        if not self._is_authorized_chat(chat_id):
            await _audit(
                self._db, chat_id=chat_id, user_id=user_id, username=username,
                action="/status", success=False, error="Unauthorized chat",
            )
            await message.answer("\u26d4 Unauthorized chat.")
            return

        await _audit(
            self._db, chat_id=chat_id, user_id=user_id, username=username,
            action="/status", success=True,
        )

        entities = await self._fetch_status_entities()
        text, keyboard = build_status_menu(entities)
        await self._send_or_edit_menu(
            chat_id, text, keyboard, source_message=message, current_menu="status"
        )

    # -----------------------------------------------------------------------
    # Callback query handler (main dispatcher)
    # -----------------------------------------------------------------------

    async def handle_callback(self, callback: CallbackQuery) -> None:
        """Main callback dispatcher for all inline button presses."""
        if callback.message is None:
            await callback.answer("Message expired.", show_alert=True)
            return

        chat_id = callback.message.chat.id
        user_id, username = self._extract_user(callback)
        if user_id is None:
            await callback.answer("Cannot identify user.", show_alert=True)
            return

        data = callback.data or ""

        # Authorization
        if not self._is_authorized_chat(chat_id):
            await callback.answer("\u26d4 Unauthorized chat.", show_alert=True)
            return

        if not self._is_authorized_user(user_id):
            await _audit(
                self._db, chat_id=chat_id, user_id=user_id, username=username,
                action=data, success=False, error="Unauthorized user",
            )
            await callback.answer("\u26d4 You are not authorized.", show_alert=True)
            return

        # Route by callback data prefix
        try:
            if data.startswith("nav:"):
                await self._handle_navigation(chat_id, data, callback)
            elif data.startswith("menu:"):
                await self._handle_menu(chat_id, user_id, username, data, callback)
            elif data.startswith("domain:"):
                await self._handle_domain(chat_id, data, callback)
            elif data.startswith("entity:"):
                await self._handle_entity(chat_id, data, callback)
            elif data.startswith("act:"):
                await self._handle_action(chat_id, user_id, username, data, callback)
            elif data.startswith("bright:"):
                await self._handle_brightness(chat_id, user_id, username, data, callback)
            elif data.startswith("climate:"):
                await self._handle_climate(chat_id, user_id, username, data, callback)
            elif data.startswith("vacuum:"):
                await self._handle_vacuum_select(chat_id, data, callback)
            elif data.startswith("vroom:"):
                await self._handle_vacuum_room(chat_id, data, callback)
            elif data.startswith("vstart:"):
                await self._handle_vacuum_start(chat_id, user_id, username, data, callback)
            elif data.startswith("vcmd:"):
                await self._handle_vacuum_cmd(chat_id, user_id, username, data, callback)
            elif data.startswith("toggle:"):
                await self._handle_toggle_view(chat_id, data, callback)
            elif data.startswith("scenes_page:"):
                await self._handle_scenes_page(chat_id, data, callback)
            else:
                await callback.answer("Unknown action.", show_alert=True)
        except Exception:
            logger.exception("Error handling callback: %s", data)
            await callback.answer("An error occurred.", show_alert=True)

    # -----------------------------------------------------------------------
    # Navigation handlers
    # -----------------------------------------------------------------------

    async def _handle_navigation(
        self, chat_id: int, data: str, callback: CallbackQuery
    ) -> None:
        """Handle nav: prefix — back buttons."""
        target = data.split(":", 1)[1] if ":" in data else "main"
        await callback.answer()

        if target == "main":
            text, keyboard = build_main_menu()
            await self._send_or_edit_menu(chat_id, text, keyboard, current_menu="main")
        elif target == "devices":
            await self._show_devices_menu(chat_id, showing_all=False)
        elif target == "robots":
            await self._show_robots_menu(chat_id)
        else:
            text, keyboard = build_main_menu()
            await self._send_or_edit_menu(chat_id, text, keyboard, current_menu="main")

    async def _handle_menu(
        self, chat_id: int, user_id: int, username: str, data: str, callback: CallbackQuery
    ) -> None:
        """Handle menu: prefix — main menu buttons."""
        target = data.split(":", 1)[1] if ":" in data else ""
        await callback.answer()

        if target == "devices":
            await self._show_devices_menu(chat_id, showing_all=False)
        elif target == "scenes":
            await self._show_scenes_menu(chat_id, page=0)
        elif target == "robots":
            await self._show_robots_menu(chat_id)
        elif target == "status":
            entities = await self._fetch_status_entities()
            text, keyboard = build_status_menu(entities)
            await self._send_or_edit_menu(chat_id, text, keyboard, current_menu="status")
        elif target == "help":
            text, keyboard = build_help_menu()
            await self._send_or_edit_menu(chat_id, text, keyboard, current_menu="help")

    async def _handle_domain(
        self, chat_id: int, data: str, callback: CallbackQuery
    ) -> None:
        """Handle domain: prefix — entity list for a domain."""
        parts = data.split(":")
        if len(parts) < 3:
            await callback.answer("Invalid data.", show_alert=True)
            return
        domain = parts[1]
        try:
            page = int(parts[2])
        except ValueError:
            page = 0
        await callback.answer()

        entities = await self._fetch_entities_by_domain(domain)
        text, keyboard = build_entity_list(
            domain, entities, page, self._cfg.menu_page_size
        )
        await self._send_or_edit_menu(
            chat_id, text, keyboard, current_menu=f"domain:{domain}:{page}"
        )

    async def _handle_entity(
        self, chat_id: int, data: str, callback: CallbackQuery
    ) -> None:
        """Handle entity: prefix — entity control menu."""
        entity_id = data.split(":", 1)[1] if ":" in data else ""
        if not entity_id:
            await callback.answer("Invalid entity.", show_alert=True)
            return
        await callback.answer()

        state = await self._ha.get_state(entity_id)
        if state is None:
            text, keyboard = build_confirmation(
                "\U0001f534 Entity not found or unavailable.",
                back_callback="nav:devices",
            )
        else:
            text, keyboard = build_entity_control(entity_id, state)

        await self._send_or_edit_menu(
            chat_id, text, keyboard,
            current_menu="entity_control", selected_entity=entity_id,
        )

    async def _handle_action(
        self, chat_id: int, user_id: int, username: str, data: str, callback: CallbackQuery
    ) -> None:
        """Handle act: prefix — execute a service call."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid action.", show_alert=True)
            return
        entity_id = parts[1]
        service = parts[2]
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        # Security: validate entity_id format and service name
        if not _ENTITY_ID_RE.match(entity_id):
            await callback.answer("Invalid entity.", show_alert=True)
            return
        if service not in _ALLOWED_SERVICES:
            await callback.answer("Invalid service.", show_alert=True)
            return

        if not await self._check_rate_limits(user_id, f"{domain}.{service}", callback):
            return

        await callback.answer("\u2699\ufe0f Executing...")

        ok, err = await self._ha.call_service(domain, service, {"entity_id": entity_id})

        if ok:
            self._rl.record()

        await _audit(
            self._db, chat_id=chat_id, user_id=user_id, username=username,
            action=f"{domain}.{service}", entity_id=entity_id,
            success=ok, error=err if not ok else None,
        )

        if ok:
            # Refresh entity state and rebuild control menu
            state = await self._ha.get_state(entity_id)
            if state is not None:
                text, keyboard = build_entity_control(entity_id, state)
                text = f"\u2705 Done!\n\n{text}"
            else:
                text, keyboard = build_confirmation(
                    f"\u2705 {domain}.{service} executed.",
                    back_callback="nav:devices",
                )
            await self._send_or_edit_menu(
                chat_id, text, keyboard,
                current_menu="entity_control", selected_entity=entity_id,
            )
        else:
            safe_err = err[:200] if err else "Unknown error"
            await callback.answer(f"Error: {safe_err}", show_alert=True)

    async def _handle_brightness(
        self, chat_id: int, user_id: int, username: str, data: str, callback: CallbackQuery
    ) -> None:
        """Handle bright: prefix — adjust light brightness."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid data.", show_alert=True)
            return
        entity_id = parts[1]
        direction = parts[2]  # "up" or "down"

        if not await self._check_rate_limits(user_id, "light.brightness", callback):
            return

        # Get current brightness
        state = await self._ha.get_state(entity_id)
        current = 128
        if state:
            current = state.get("attributes", {}).get("brightness", 128) or 128

        step = 51  # ~20% of 255
        if direction == "up":
            new_brightness = min(255, current + step)
        else:
            new_brightness = max(1, current - step)

        await callback.answer(f"\U0001f506 Setting brightness...")

        ok, err = await self._ha.call_service(
            "light", "turn_on", {"entity_id": entity_id, "brightness": new_brightness}
        )

        if ok:
            self._rl.record()

        await _audit(
            self._db, chat_id=chat_id, user_id=user_id, username=username,
            action="light.brightness", entity_id=entity_id,
            success=ok, error=err if not ok else None,
        )

        if ok:
            state = await self._ha.get_state(entity_id)
            if state:
                text, keyboard = build_entity_control(entity_id, state)
                text = f"\u2705 Brightness adjusted\n\n{text}"
                await self._send_or_edit_menu(
                    chat_id, text, keyboard,
                    current_menu="entity_control", selected_entity=entity_id,
                )
        else:
            await callback.answer(f"Error: {(err or '')[:180]}", show_alert=True)

    async def _handle_climate(
        self, chat_id: int, user_id: int, username: str, data: str, callback: CallbackQuery
    ) -> None:
        """Handle climate: prefix — adjust temperature."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid data.", show_alert=True)
            return
        entity_id = parts[1]
        direction = parts[2]

        if not await self._check_rate_limits(user_id, "climate.set_temperature", callback):
            return

        state = await self._ha.get_state(entity_id)
        current_temp = 20.0
        if state:
            current_temp = state.get("attributes", {}).get("temperature", 20.0) or 20.0
            try:
                current_temp = float(current_temp)
            except (TypeError, ValueError):
                current_temp = 20.0

        step = 0.5
        if direction == "up":
            new_temp = current_temp + step
        else:
            new_temp = current_temp - step

        await callback.answer(f"\U0001f321\ufe0f Setting temperature...")

        ok, err = await self._ha.call_service(
            "climate", "set_temperature", {"entity_id": entity_id, "temperature": new_temp}
        )

        if ok:
            self._rl.record()

        await _audit(
            self._db, chat_id=chat_id, user_id=user_id, username=username,
            action="climate.set_temperature", entity_id=entity_id,
            success=ok, error=err if not ok else None,
        )

        if ok:
            state = await self._ha.get_state(entity_id)
            if state:
                text, keyboard = build_entity_control(entity_id, state)
                text = f"\u2705 Temperature adjusted\n\n{text}"
                await self._send_or_edit_menu(
                    chat_id, text, keyboard,
                    current_menu="entity_control", selected_entity=entity_id,
                )
        else:
            await callback.answer(f"Error: {(err or '')[:180]}", show_alert=True)

    async def _handle_vacuum_select(
        self, chat_id: int, data: str, callback: CallbackQuery
    ) -> None:
        """Handle vacuum: prefix — show vacuum room menu."""
        entity_id = data.split(":", 1)[1] if ":" in data else ""
        if not entity_id:
            await callback.answer("Invalid vacuum.", show_alert=True)
            return
        await callback.answer()

        state = await self._ha.get_state(entity_id)
        name = entity_id
        if state:
            name = state.get("attributes", {}).get("friendly_name", entity_id)

        rooms = list(self._cfg.vacuum_room_presets)

        text, keyboard = build_vacuum_rooms(entity_id, name, rooms)
        await self._send_or_edit_menu(
            chat_id, text, keyboard,
            current_menu="vacuum_rooms", selected_entity=entity_id,
        )

    async def _handle_vacuum_room(
        self, chat_id: int, data: str, callback: CallbackQuery
    ) -> None:
        """Handle vroom: prefix — select a room for vacuum."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid data.", show_alert=True)
            return
        entity_id = parts[1]
        room = parts[2]
        await callback.answer()

        state = await self._ha.get_state(entity_id)
        name = entity_id
        if state:
            name = state.get("attributes", {}).get("friendly_name", entity_id)

        rooms = list(self._cfg.vacuum_room_presets)

        text, keyboard = build_vacuum_rooms(entity_id, name, rooms, selected_room=room)
        await self._send_or_edit_menu(
            chat_id, text, keyboard,
            current_menu="vacuum_rooms", selected_entity=entity_id, selected_room=room,
        )

    async def _handle_vacuum_start(
        self, chat_id: int, user_id: int, username: str, data: str, callback: CallbackQuery
    ) -> None:
        """Handle vstart: prefix — start vacuum cleaning."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid data.", show_alert=True)
            return
        entity_id = parts[1]
        room = parts[2]

        if not await self._check_rate_limits(user_id, "vacuum.start", callback):
            return

        await callback.answer("\u2699\ufe0f Starting...")

        strategy = self._cfg.vacuum_room_strategy
        ok = False
        err = ""

        if room and strategy == "script" and self._cfg.vacuum_room_script_entity_id:
            # Script mode: call a script with vacuum + room parameters
            ok, err = await self._ha.call_service(
                "script", "turn_on",
                {
                    "entity_id": self._cfg.vacuum_room_script_entity_id,
                    "variables": {
                        "vacuum_entity": entity_id,
                        "room": room,
                    },
                },
            )
        elif room and strategy == "service_data":
            # Service data mode: send command with room info
            ok, err = await self._ha.call_service(
                "vacuum", "send_command",
                {
                    "entity_id": entity_id,
                    "command": "app_segment_clean",
                    "params": {"rooms": [room]},
                },
            )
        else:
            # No room selected or no room support — just start
            ok, err = await self._ha.call_service(
                "vacuum", "start", {"entity_id": entity_id}
            )

        if ok:
            self._rl.record()

        await _audit(
            self._db, chat_id=chat_id, user_id=user_id, username=username,
            action="vacuum.start", entity_id=entity_id,
            success=ok, error=err if not ok else None,
        )

        room_display = room.replace("_", " ").title() if room else "All"
        if ok:
            text, keyboard = build_confirmation(
                f"\u2705 Started cleaning: {room_display}",
                back_callback=f"vacuum:{entity_id}",
            )
        else:
            safe_err = (err or "Unknown error")[:200]
            text, keyboard = build_confirmation(
                f"\u274c Failed to start vacuum: {safe_err}",
                back_callback=f"vacuum:{entity_id}",
            )

        await self._send_or_edit_menu(
            chat_id, text, keyboard,
            current_menu="vacuum_result", selected_entity=entity_id,
        )

    async def _handle_vacuum_cmd(
        self, chat_id: int, user_id: int, username: str, data: str, callback: CallbackQuery
    ) -> None:
        """Handle vcmd: prefix — vacuum stop/return_to_base."""
        parts = data.split(":", 2)
        if len(parts) < 3:
            await callback.answer("Invalid data.", show_alert=True)
            return
        entity_id = parts[1]
        service = parts[2]

        # Security: validate service against allowed vacuum commands
        if service not in _ALLOWED_VACUUM_SERVICES:
            await callback.answer("Invalid vacuum command.", show_alert=True)
            return

        if not await self._check_rate_limits(user_id, f"vacuum.{service}", callback):
            return

        await callback.answer("\u2699\ufe0f Executing...")

        ok, err = await self._ha.call_service(
            "vacuum", service, {"entity_id": entity_id}
        )

        if ok:
            self._rl.record()

        await _audit(
            self._db, chat_id=chat_id, user_id=user_id, username=username,
            action=f"vacuum.{service}", entity_id=entity_id,
            success=ok, error=err if not ok else None,
        )

        label = service.replace("_", " ").title()
        if ok:
            text, keyboard = build_confirmation(
                f"\u2705 Vacuum: {label}",
                back_callback=f"vacuum:{entity_id}",
            )
        else:
            safe_err = (err or "Unknown error")[:200]
            text, keyboard = build_confirmation(
                f"\u274c Vacuum {label} failed: {safe_err}",
                back_callback=f"vacuum:{entity_id}",
            )

        await self._send_or_edit_menu(
            chat_id, text, keyboard,
            current_menu="vacuum_result", selected_entity=entity_id,
        )

    async def _handle_toggle_view(
        self, chat_id: int, data: str, callback: CallbackQuery
    ) -> None:
        """Handle toggle: prefix — switch between default/all domain views."""
        mode = data.split(":", 1)[1] if ":" in data else "default"
        await callback.answer()
        await self._show_devices_menu(chat_id, showing_all=(mode == "all"))

    async def _handle_scenes_page(
        self, chat_id: int, data: str, callback: CallbackQuery
    ) -> None:
        """Handle scenes_page: prefix — paginate scenes."""
        try:
            page = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            page = 0
        await callback.answer()
        await self._show_scenes_menu(chat_id, page=page)

    # -----------------------------------------------------------------------
    # Data fetching helpers
    # -----------------------------------------------------------------------

    async def _fetch_entities_by_domain(self, domain: str) -> list[dict[str, Any]]:
        """Fetch and filter entities for a specific domain."""
        all_states = await self._ha.list_states()
        entities = [
            s for s in all_states
            if isinstance(s, dict)
            and isinstance(s.get("entity_id"), str)
            and s["entity_id"].startswith(f"{domain}.")
        ]
        # Sort by friendly_name
        entities.sort(
            key=lambda e: (e.get("attributes") or {}).get("friendly_name", e.get("entity_id", ""))
        )
        return entities

    async def _get_domains_with_counts(
        self, showing_all: bool = False
    ) -> list[tuple[str, int]]:
        """Get domains and their entity counts."""
        all_states = await self._ha.list_states()
        domain_counts: dict[str, int] = {}
        allowed = set(self._cfg.menu_domains_allowlist) if not showing_all else None

        for s in all_states:
            if not isinstance(s, dict):
                continue
            eid = s.get("entity_id", "")
            if not isinstance(eid, str) or "." not in eid:
                continue
            domain = eid.split(".")[0]
            if allowed is not None and domain not in allowed:
                continue
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        result = sorted(domain_counts.items(), key=lambda x: x[0])
        return result

    async def _fetch_status_entities(self) -> list[dict[str, Any]]:
        """Fetch states for configured status entities."""
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
    # Menu display helpers
    # -----------------------------------------------------------------------

    async def _show_devices_menu(self, chat_id: int, showing_all: bool) -> None:
        """Build and display the devices domain menu."""
        domains = await self._get_domains_with_counts(showing_all=showing_all)
        text, keyboard = build_devices_menu(
            domains,
            show_all_enabled=self._cfg.show_all_enabled,
            showing_all=showing_all,
        )
        await self._send_or_edit_menu(
            chat_id, text, keyboard,
            current_menu=f"devices:{'all' if showing_all else 'default'}",
        )

    async def _show_robots_menu(self, chat_id: int) -> None:
        """Build and display the robots (vacuum) menu."""
        vacuums = await self._fetch_entities_by_domain("vacuum")
        text, keyboard = build_robots_menu(vacuums)
        await self._send_or_edit_menu(chat_id, text, keyboard, current_menu="robots")

    async def _show_scenes_menu(self, chat_id: int, page: int) -> None:
        """Build and display the scenes menu."""
        scenes = await self._fetch_entities_by_domain("scene")
        text, keyboard = build_scenes_menu(scenes, page, self._cfg.menu_page_size)
        await self._send_or_edit_menu(chat_id, text, keyboard, current_menu=f"scenes:{page}")
