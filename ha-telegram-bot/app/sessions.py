"""Persistent personal screens. Telegram chat state is never private in a group."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any


@dataclass
class UISession:
    chat_id: int
    user_id: int
    thread_id: int
    data: dict[str, Any]

    @property
    def key(self) -> tuple[int, int, int]:
        return self.chat_id, self.user_id, self.thread_id


current_session: ContextVar[UISession | None] = ContextVar("ui_session", default=None)


class SessionState:
    """Small compatibility facade for existing navigation helpers; persisted as JSON."""
    def __init__(self, field: str):
        self.field = field

    def _data(self) -> dict:
        session = current_session.get()
        if session is None:
            raise RuntimeError("UI operation outside a personal session")
        return session.data.setdefault("state", {})

    def get(self, _chat, default=None):
        return self._data().get(self.field, default)

    def setdefault(self, _chat, default):
        return self._data().setdefault(self.field, default)

    def pop(self, _chat, default=None):
        return self._data().pop(self.field, default)

    def __contains__(self, _chat):
        return self.field in self._data()

    def __setitem__(self, _chat, value):
        self._data()[self.field] = value


def personal_entry(func):
    """Authorize every entry point and retain existing per-user serialization.

    ContextVar is task local: concurrent users and delayed actions cannot inherit
    another user's identity. All commands, callbacks and text input use this gate.
    """
    @wraps(func)
    async def wrapped(self, event):
        is_callback = func.__name__ == "handle_callback"
        message = event.message if is_callback else event
        user = event.from_user
        if message is None or user is None:
            if is_callback:
                await event.answer("Откройте новое меню: /menu", show_alert=True)
            return
        cid, uid = message.chat.id, user.id
        if not self._is_authorized_chat(cid) or not self._is_authorized_user(uid):
            await event.answer("Нет доступа.", **({"show_alert": True} if is_callback else {}))
            await self._db.write_audit(chat_id=cid, user_id=uid, username="",
                                       action=func.__name__, success=False, error="Access denied")
            return
        lock = self._user_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            tid = message.message_thread_id or 0
            state = await self._db.load_ui_session(cid, uid, tid)
            session = UISession(cid, uid, tid, state)
            token = current_session.set(session)
            try:
                if is_callback:
                    data = event.data or ""
                    action = None
                    if message.message_id == state.get("message_id"):
                        action = state.get("actions", {}).get(data)
                    if message.message_id == state.get("widget_message_id") and data == "ui:inline":
                        action = "ui:inline"
                    # Notifications have their own message-bound capabilities.
                    if action is None and cid == uid and await self._db.notice_allows(uid, message.message_id, data):
                        action = data
                    if action is None:
                        await event.answer("Это чужой или устаревший экран. Откройте свой: /menu", show_alert=True)
                        return
                    event = event.model_copy(update={"data": action})
                return await func(self, event)
            finally:
                try:
                    await self._db.save_ui_session(*session.key, session.data)
                finally:
                    current_session.reset(token)
    return wrapped
