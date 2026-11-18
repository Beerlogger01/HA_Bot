"""Profile 1 renderer and capability-gated widget queries using the inline routes."""
from __future__ import annotations

import asyncio
import html
import json
import math
from pathlib import Path
import re
import secrets
import time
from types import SimpleNamespace

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramAPIError

from sessions import UISession, current_session
from widget_transport import WidgetTransport, check_text, validate_html

HTML = (Path(__file__).parent / "widgets" / "home.html").read_text(encoding="utf-8")
validate_html(HTML)


def encode(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def plain(text):
    return html.unescape(re.sub(r"<[^>]*>", "", text))


def chunks(text, limit=220):
    """Bounded UTF-8 chunks so every part of a long screen remains reachable."""
    result, part = [], ""
    for char in text:
        if len((part + char).encode()) > limit:
            result.append(part)
            part = ""
        part += char
    return result + [part]


class WidgetCallback:
    def __init__(self, query, session, action):
        self.data = action
        self.from_user = SimpleNamespace(id=session.user_id, username="", first_name="")
        self.message = SimpleNamespace(chat=SimpleNamespace(id=session.chat_id))
        self.notice = ""

    async def answer(self, text="", **_):
        if text:
            self.notice = text


class WidgetUI:
    def __init__(self, bot, db, config, handlers):
        self.api = WidgetTransport(bot)
        self.db, self.cfg, self.handlers = db, config, handlers
        self._probe_lock = asyncio.Lock()
        self._requests: dict[int, list[float]] = {}
        self._seen: dict[str, float] = {}

    def _page(self, session, page=0):
        view = session.data["widget_view"]
        parts = chunks(view["body"])
        count = max(1, math.ceil(len(view["buttons"]) / 4), len(parts))
        page = min(max(page, 0), count - 1)
        nonce = secrets.token_hex(4)
        actions, buttons = {}, []
        for label, action in view["buttons"][page * 4:page * 4 + 4]:
            key = f"w:{nonce}:{len(actions)}"
            actions[key] = action
            buttons.append([chunks(label, 64)[0], key])
        for label, target in [("‹ Назад", page - 1), ("Далее ›", page + 1)]:
            if 0 <= target < count:
                key = f"w:{nonce}:{len(actions)}"
                actions[key] = f"wpage:{target}"
                buttons.append([label, key])
        inline_key = f"w:{nonce}:inline"
        actions[inline_key] = "ui:inline"
        data = {"r": session.data.get("widget_revision", 0) + 1, "u": session.user_id,
                "t": chunks(view["title"], 90)[0], "b": parts[page] if page < len(parts) else "",
                "a": buttons, "n": f"{page + 1} / {count}" if count > 1 else ""}
        # JSON escaping can expand quotes/backslashes sixfold. Preserve all
        # actions and shrink labels/body, then validate the actual wire bytes.
        while len(encode(data).encode()) > 1024:
            if data["b"]:
                data["b"] = data["b"][:-1]
            else:
                longest = max(data["a"], key=lambda item: len(item[0]))
                longest[0] = longest[0][:-1]
        encoded = check_text(encode(data), 1024)
        return data, encoded, actions, inline_key

    async def render(self, session, text, keyboard, *, page=0, new_view=True):
        if not getattr(self.cfg, "widgets_enabled", False):
            return False
        async with self._probe_lock:
            blocked = float(await self.db.get_runtime_value("widgets_retry_at") or 0)
            if time.time() < blocked:
                return False
            if new_view:
                lines = plain(text).split("\n", 1)
                session.data["widget_view"] = {
                    "title": lines[0], "body": lines[1].strip() if len(lines) > 1 else "",
                    "buttons": [[b.text, b.callback_data] for row in keyboard.inline_keyboard for b in row if b.callback_data],
                }
            data, encoded, actions, inline_key = self._page(session, page)
            try:
                mid = session.data.get("widget_message_id")
                # The ordinary menu is always reachable, including old clients.
                markup = {"inline_keyboard": [[{"text": "Обычное меню", "callback_data": "ui:inline"}]]}
                if mid:
                    await self.api.edit(session.chat_id, mid, chat_data=encoded)
                else:
                    sent = await self.api.send(chat_id=session.chat_id, html=HTML, chat_data=encoded,
                                               public_data="", message_thread_id=session.thread_id or None,
                                               caption="Мой дом · Личный экран. /menu — обычное меню",
                                               reply_markup=markup)
                    mid = sent["message_id"]
                session.data.update(widget_message_id=mid, widget_actions=actions,
                                    widget_revision=data["r"], widget_data=data, actions={})
                await self.db.save_ui_session(*session.key, session.data)
                return True
            except TelegramRetryAfter as exc:
                delay = max(60, exc.retry_after)
            except TelegramBadRequest as exc:
                if "message to edit not found" in str(exc).lower():
                    session.data.pop("widget_message_id", None)
                # Includes INPUT_MEDIA_WIDGET_NOT_ALLOWED; never bypass restrictions.
                delay = getattr(self.cfg, "widgets_retry_seconds", 3600)
            except (TelegramAPIError, OSError, asyncio.TimeoutError):
                delay = 60
            await self.db.set_runtime_value("widgets_retry_at", str(time.time() + delay))
            return False

    async def query(self, query, *, shared=False):
        qid = query.get("id")
        if not isinstance(qid, str):
            return
        async def answer(message):
            await self.api.answer(qid, encode({"message": message}))
        # Forwarded shared queries have no trusted chat context. They cannot
        # authorize access to a home, regardless of viewer or public data.
        if shared:
            await answer("Управление доступно только в личном экране бота: /menu")
            return
        if not getattr(self.cfg, "widgets_enabled", False):
            await answer("Widgets отключены. Откройте /menu")
            return
        try:
            uid = query["from"]["id"]
            cid = query["chat"]["id"]
            mid = query["message_id"]
            if not all(type(x) is int for x in (uid, cid, mid)):
                raise ValueError
            data = check_text(query["data"], 64, empty=False)
            if query.get("type", {}).get("type") != "data":
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            await answer("Некорректный запрос. Откройте /menu")
            return
        if not self.handlers._is_authorized_chat(cid) or not self.handlers._is_authorized_user(uid):
            await answer("Нет доступа")
            return
        now = time.monotonic()
        self._requests = {u: [t for t in times if now - t < 5] for u, times in self._requests.items() if times and now - times[-1] < 5}
        self._seen = {key: ts for key, ts in self._seen.items() if now - ts < 30}
        times = self._requests.setdefault(uid, [])
        if len(times) >= 5 or qid in self._seen:
            await answer("Слишком часто. Подождите 5 секунд")
            return
        times.append(now)
        self._seen[qid] = now
        lock = self.handlers._user_locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            await answer("Предыдущее действие ещё выполняется")
            return
        async with lock:
            found = await self.db.find_widget_session(cid, uid, mid)
            if not found:
                await answer("Это чужой или устаревший экран. Откройте /menu")
                return
            tid, state = found
            action = state.get("widget_actions", {}).get(data)
            if action is None:
                await answer("Экран изменился. Нажмите актуальную кнопку")
                return
            # Acknowledge BEFORE any HA I/O. Service completion is delivered by
            # a state edit. A slow HA never makes answerWidgetQuery miss 15s.
            await answer("Принято. Выполняю…")
            session = UISession(cid, uid, tid, state)
            token = current_session.set(session)
            callback = WidgetCallback(query, session, action)
            try:
                if action.startswith("wpage:"):
                    success = await self.render(session, "", None, page=int(action.split(":")[1]), new_view=False)
                    if not success:
                        await self._fallback(session)
                else:
                    await self.handlers.handle_callback.__wrapped__(self.handlers, callback)
                if session.data.get("mode") == "widget" and callback.notice:
                    updated = dict(session.data.get("widget_data", {}))
                    updated["n"] = chunks(callback.notice, 100)[0]
                    updated["r"] = session.data.get("widget_revision", 0) + 1
                    # Make room for the result without exceeding state size.
                    while len(encode(updated).encode()) > 1024 and updated.get("b"):
                        updated["b"] = updated["b"][:-1]
                    try:
                        await self.api.edit(cid, mid, chat_data=encode(updated))
                        session.data.update(widget_data=updated, widget_revision=updated["r"])
                    except (TelegramAPIError, OSError, asyncio.TimeoutError):
                        await self._fallback(session)
            finally:
                try:
                    await self.db.save_ui_session(*session.key, session.data)
                finally:
                    current_session.reset(token)

    async def _fallback(self, session):
        session.data["mode"] = "inline"
        session.data.pop("widget_actions", None)
        t, k = await self.handlers._home_view(session.user_id)
        await self.handlers._send_or_edit(session.chat_id, t, k, menu="main")


class WidgetUpdates(BaseMiddleware):
    def __init__(self, ui):
        self.ui = ui

    async def __call__(self, handler, event, data):
        extra = event.model_extra or {}
        for name in ("widget_query", "shared_widget_query"):
            query = getattr(event, name, None) or extra.get(name)
            if query is not None:
                if hasattr(query, "model_dump"):
                    query = query.model_dump(by_alias=True)
                try:
                    await self.ui.query(query, shared=name == "shared_widget_query")
                except (TelegramAPIError, OSError, asyncio.TimeoutError):
                    # Expired query/network loss must not kill polling or leak tokens.
                    pass
                return
        return await handler(event, data)
