"""Notifications — subscribe to HA state_changed events via WebSocket.

Runs a persistent background WS connection.  When a subscribed entity
changes state, sends a Telegram message to the user (respecting throttle).

Features:
- Two modes: state_only and state_and_key_attrs
- Actionable inline buttons on notifications (Dock/Locate/Pause/Mute)
- Per-user per-entity mute with expiry
- Resilient WS reconnect with exponential backoff
- Single-subscription guard (only one WS subscribe per connection)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from storage import Database

logger = logging.getLogger("ha_bot.notifications")

HA_WS_URL = "ws://supervisor/core/websocket"

# Key attributes for mode="state_and_key_attrs" per domain
_KEY_ATTRS: dict[str, frozenset[str]] = {
    "vacuum": frozenset({"status", "battery_level", "error", "fan_speed"}),
    "climate": frozenset({"current_temperature", "temperature", "hvac_action"}),
    "media_player": frozenset({"media_title", "source"}),
}

# Actionable buttons per domain for notification messages
_NOTIF_ACTIONS: dict[str, list[tuple[str, str, str]]] = {
    # (label, callback_prefix, service)
    "vacuum": [
        ("\U0001f3e0 Dock", "nact", "return_to_base"),
        ("\U0001f4cd Locate", "nact", "locate"),
        ("\u23f8 Pause", "nact", "pause"),
    ],
    "light": [
        ("\U0001f7e2 ON", "nact", "turn_on"),
        ("\u26aa OFF", "nact", "turn_off"),
    ],
    "switch": [
        ("\U0001f7e2 ON", "nact", "turn_on"),
        ("\u26aa OFF", "nact", "turn_off"),
    ],
    "cover": [
        ("\u2b06 Open", "nact", "open_cover"),
        ("\u23f9 Stop", "nact", "stop_cover"),
        ("\u2b07 Close", "nact", "close_cover"),
    ],
}

# Reconnect backoff config
_RECONNECT_BASE = 5
_RECONNECT_MAX = 120


def _sanitize(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_notif_buttons(
    entity_id: str, domain: str, user_id: int,
) -> InlineKeyboardMarkup | None:
    """Build actionable inline keyboard for notification message."""
    actions = _NOTIF_ACTIONS.get(domain)
    rows: list[list[InlineKeyboardButton]] = []

    if actions:
        action_row = []
        for label, prefix, service in actions:
            cb = f"{prefix}:{entity_id}:{service}"
            if len(cb) <= 64:
                action_row.append(InlineKeyboardButton(text=label, callback_data=cb))
        if action_row:
            rows.append(action_row)

    # Mute button (1h)
    mute_cb = f"nmute:{entity_id}:{user_id}"
    if len(mute_cb) <= 64:
        rows.append([
            InlineKeyboardButton(text="\U0001f515 Mute 1h", callback_data=mute_cb),
            InlineKeyboardButton(text="\u2192 Open", callback_data=f"ent:{entity_id}"),
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


class NotificationManager:
    """Background WebSocket listener for state_changed events."""

    def __init__(self, supervisor_token: str, db: Database, bot: Bot) -> None:
        self._token = supervisor_token
        self._db = db
        self._bot = bot
        self._task: asyncio.Task[None] | None = None
        self._stop = False
        self._subscribed = False

    async def start(self) -> None:
        """Start the background listener."""
        self._stop = False
        self._subscribed = False
        self._task = asyncio.create_task(self._run_loop(), name="notifications")
        logger.info("Notification listener started")

    async def stop(self) -> None:
        """Stop the background listener."""
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Notification listener stopped")

    async def _run_loop(self) -> None:
        """Reconnecting event loop with exponential backoff."""
        backoff = _RECONNECT_BASE
        while not self._stop:
            try:
                await self._listen()
                backoff = _RECONNECT_BASE  # Reset on clean exit
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Notification listener error, reconnecting in %ds", backoff)
            if not self._stop:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX)

    async def _listen(self) -> None:
        """Single WebSocket session: auth, subscribe, process events."""
        self._subscribed = False
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                HA_WS_URL, timeout=aiohttp.ClientTimeout(total=0), heartbeat=30
            ) as ws:
                # Auth
                msg = await asyncio.wait_for(ws.receive(), timeout=10)
                data = json.loads(msg.data)
                if data.get("type") != "auth_required":
                    logger.error("Notif WS: expected auth_required, got %s", data.get("type"))
                    return

                await ws.send_json({"type": "auth", "access_token": self._token})
                msg = await asyncio.wait_for(ws.receive(), timeout=10)
                data = json.loads(msg.data)
                if data.get("type") != "auth_ok":
                    logger.error("Notif WS: auth failed")
                    return

                # Subscribe to state_changed (single-subscription guard)
                if not self._subscribed:
                    sub_id = 1
                    await ws.send_json({
                        "id": sub_id,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    })
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    data = json.loads(msg.data)
                    if not data.get("success"):
                        logger.error("Notif WS: subscribe failed: %s", data)
                        return
                    self._subscribed = True

                logger.info("Notification WS subscribed to state_changed")

                # Process events
                async for msg in ws:
                    if self._stop:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            await self._handle_event(json.loads(msg.data))
                        except Exception:
                            logger.exception("Error processing state_changed event")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        logger.warning("Notif WS connection closed")
                        break

        self._subscribed = False

    async def _handle_event(self, data: dict[str, Any]) -> None:
        """Process a single state_changed event."""
        if data.get("type") != "event":
            return
        event = data.get("event", {})
        if event.get("event_type") != "state_changed":
            return

        event_data = event.get("data", {})
        entity_id = event_data.get("entity_id", "")
        if not entity_id:
            return

        old_state = event_data.get("old_state") or {}
        new_state = event_data.get("new_state") or {}
        old_val = old_state.get("state", "")
        new_val = new_state.get("state", "")

        # Get all active subscriptions for this entity
        subs = await self._db.get_all_active_notifications()
        now = time.time()

        for sub in subs:
            if sub["entity_id"] != entity_id:
                continue

            user_id = sub["user_id"]

            # Check mute
            if await self._db.is_muted(user_id, entity_id):
                continue

            # Throttle check
            if now - sub["last_sent_ts"] < sub["throttle_seconds"]:
                continue

            mode = sub["mode"]

            # mode=state_only: only notify if state value changed
            if mode == "state_only":
                if old_val == new_val:
                    continue

            # mode=state_and_key_attrs: also check key attributes
            elif mode == "state_and_key_attrs":
                domain = entity_id.split(".", 1)[0]
                key_attrs = _KEY_ATTRS.get(domain, frozenset())
                state_changed = old_val != new_val
                attrs_changed = False
                if key_attrs:
                    old_attrs = old_state.get("attributes", {})
                    new_attrs = new_state.get("attributes", {})
                    for attr in key_attrs:
                        if old_attrs.get(attr) != new_attrs.get(attr):
                            attrs_changed = True
                            break
                if not state_changed and not attrs_changed:
                    continue

            # Build notification text
            friendly = new_state.get("attributes", {}).get("friendly_name", entity_id)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            text = (
                f"\U0001f514 <b>{_sanitize(friendly)}</b>\n"
                f"{_sanitize(old_val)} \u2192 {_sanitize(new_val)}\n"
                f"<i>{ts} UTC</i>"
            )

            # Add key attr changes if relevant
            if mode == "state_and_key_attrs":
                domain = entity_id.split(".", 1)[0]
                key_attrs = _KEY_ATTRS.get(domain, frozenset())
                old_attrs = old_state.get("attributes", {})
                new_attrs = new_state.get("attributes", {})
                changes = []
                for attr in sorted(key_attrs):
                    ov = old_attrs.get(attr)
                    nv = new_attrs.get(attr)
                    if ov != nv:
                        changes.append(f"  {attr}: {ov} \u2192 {nv}")
                if changes:
                    text += "\n" + "\n".join(changes)

            # Build actionable buttons
            domain = entity_id.split(".", 1)[0]
            kb = _build_notif_buttons(entity_id, domain, user_id)

            await self._send_notification(user_id, text, kb)
            await self._db.update_notification_sent(user_id, entity_id)

    async def _send_notification(
        self, user_id: int, text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Send notification message to user."""
        try:
            await self._bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramRetryAfter as e:
            logger.warning("Notification rate limited, wait %ss for user %s", e.retry_after, user_id)
            await asyncio.sleep(e.retry_after)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("Cannot send notification to %s: %s", user_id, exc)
        except Exception:
            logger.exception("Failed to send notification to %s", user_id)
