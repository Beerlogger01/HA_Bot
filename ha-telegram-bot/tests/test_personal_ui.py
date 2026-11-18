"""Integration tests at the real dispatcher/SQLite boundary; no external devices."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message, Update, InlineKeyboardButton, InlineKeyboardMarkup
from actions import ActionService, validate_service
from api import HAClient
from handlers import Handlers, GlobalRateLimiter
from registry import HARegistry
from sessions import UISession, current_session
from storage import Database
from widget_transport import WidgetTransport, validate_html, check_text
from widget_ui import HTML, WidgetUpdates, encode
from ui import build_entity_control, build_main_menu
from scheduler import Scheduler, validate_cron


class TelegramMock(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.mid = 100
        self.fail = {}

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        self.calls.append((name, method.model_dump(exclude_none=True)))
        if name in self.fail:
            raise self.fail[name](method)
        if name in {"sendMessage", "sendWidget"}:
            self.mid += 1
            return Message(message_id=self.mid, date=datetime.now(timezone.utc),
                           chat={"id": method.chat_id, "type": "private" if method.chat_id > 0 else "supergroup"},
                           message_thread_id=method.message_thread_id, text=getattr(method, "text", None)).as_(bot)
        return True


class HomeMock:
    def __init__(self):
        self.states = {
            "light.kitchen": {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Кухня", "supported_color_modes": ["brightness"], "brightness": 128}},
            "climate.bedroom": {"entity_id": "climate.bedroom", "state": "heat", "attributes": {"friendly_name": "Спальня", "temperature": 20, "supported_features": 1}},
            "todo.home": {"entity_id": "todo.home", "state": "0", "attributes": {}},
        }
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self.offline = False

    async def list_states(self):
        return deepcopy(list(self.states.values())) if not self.offline else []

    async def get_state(self, eid):
        return deepcopy(self.states.get(eid)) if not self.offline else None

    async def call_service(self, domain, service, data):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        await asyncio.sleep(.005)
        self.calls.append((domain, service, deepcopy(data)))
        self.inflight -= 1
        if self.offline:
            return False, "HA offline"
        state = self.states.get(data["entity_id"])
        if state and service in {"turn_on", "turn_off"}:
            state["state"] = "on" if service == "turn_on" else "off"
        if state and service == "set_temperature":
            state["attributes"]["temperature"] = data["temperature"]
        if service == "get_items":
            return True, {data["entity_id"]: {"items": []}}
        return True, ""


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    await db.open()
    telegram = TelegramMock()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL", session=telegram)
    cfg = SimpleNamespace(allowed_chat_id=0, allowed_user_ids=frozenset(), cooldown_seconds_default=0,
                          cooldown_overrides={}, widgets_enabled=False, widgets_retry_seconds=3600,
                          menu_page_size=6, menu_domains_allowlist=("light", "climate", "todo"),
                          show_all_enabled=False, radio_stations=(), terminal_enabled=False,
                          device_overrides={}, status_entities=())
    ha = HomeMock()
    rl = GlobalRateLimiter(100, 5)
    actions = ActionService(ha, db, cfg, rl)
    reg = HARegistry("fake", db)
    h = Handlers(bot=bot, ha=actions, db=db, config=cfg, global_rl=rl, registry=reg,
                 vacuum=SimpleNamespace(), diagnostics=SimpleNamespace(), scheduler=SimpleNamespace())
    yield SimpleNamespace(db=db, tg=telegram, bot=bot, cfg=cfg, ha=ha, actions=actions, h=h)
    await h.close()
    await db.close()
    await bot.session.close()


def message(env, uid=1, cid=-100, tid=0, text="/menu", mid=1):
    return Message(message_id=mid, date=datetime.now(timezone.utc),
                   chat={"id": cid, "type": "private" if cid > 0 else "supergroup"},
                   from_user={"id": uid, "is_bot": False, "first_name": f"User {uid}"},
                   message_thread_id=tid or None, is_topic_message=bool(tid), text=text).as_(env.bot)


def callback(env, uid, cid, mid, data, tid=0):
    return CallbackQuery(id=f"cb{uid}{mid}{data}", from_user={"id": uid, "is_bot": False, "first_name": "Test"},
                         chat_instance="test", message=message(env, uid, cid, tid, mid=mid), data=data).as_(env.bot)


async def click(env, action, uid=1, cid=-100, tid=0):
    state = await env.db.load_ui_session(cid, uid, tid)
    key = next(k for k, v in state["actions"].items() if v == action)
    await env.h.handle_callback(callback(env, uid, cid, state["message_id"], key, tid))


async def show(env, action, uid=1, cid=-100, tid=0):
    """Expose a server-generated screen action using the production renderer."""
    state = await env.db.load_ui_session(cid, uid, tid)
    token = current_session.set(UISession(cid, uid, tid, state))
    try:
        await env.h._send_or_edit(cid, "Экран", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть", callback_data=action)]]))
    finally:
        current_session.reset(token)


@pytest.mark.parametrize("chats", [(-100, -100), (1, 2)])
async def test_two_users_concurrent_navigation_and_actions(env, chats):
    await asyncio.gather(*(env.h.cmd_start(message(env, uid, cid)) for uid, cid in zip((1, 2), chats)))
    first = await env.db.load_ui_session(chats[0], 1)
    second = await env.db.load_ui_session(chats[1], 2)
    assert first["message_id"] != second["message_id"]
    await asyncio.gather(show(env, "ent:light.kitchen", 1, chats[0]), show(env, "ent:climate.bedroom", 2, chats[1]))
    await asyncio.gather(click(env, "ent:light.kitchen", 1, chats[0]), click(env, "ent:climate.bedroom", 2, chats[1]))
    await asyncio.gather(click(env, "act:light.kitchen:turn_on", 1, chats[0]), click(env, "clim:climate.bedroom:up", 2, chats[1]))
    a, b = await env.db.load_ui_session(chats[0], 1), await env.db.load_ui_session(chats[1], 2)
    assert a["selected_entity"] == "light.kitchen"
    assert b["selected_entity"] == "climate.bedroom"
    assert (a["message_id"], b["message_id"]) == (first["message_id"], second["message_id"])
    assert env.ha.states["light.kitchen"]["state"] == "on"
    assert env.ha.states["climate.bedroom"]["attributes"]["temperature"] == 20.5
    assert env.ha.max_inflight == 1
    assert len([c for c in env.tg.calls if c[0] == "sendMessage"]) == 2


async def test_foreign_and_stale_buttons_never_change_screen(env):
    await env.h.cmd_start(message(env))
    old = await env.db.load_ui_session(-100, 1)
    key = next(iter(old["actions"]))
    before = len([x for x in env.tg.calls if x[0] == "editMessageText"])
    await env.h.handle_callback(callback(env, 2, -100, old["message_id"], key))
    assert len([x for x in env.tg.calls if x[0] == "editMessageText"]) == before
    await click(env, "menu:search")
    await env.h.handle_callback(callback(env, 1, -100, old["message_id"], key))
    assert (await env.db.load_ui_session(-100, 1))["current_menu"] == "search"
    assert not env.ha.calls


async def test_topics_and_restart_preserve_ownership_and_navigation(env):
    await asyncio.gather(env.h.cmd_start(message(env, tid=10)), env.h.cmd_start(message(env, tid=20)))
    await click(env, "menu:search", tid=10)
    a, b = await env.db.load_ui_session(-100, 1, 10), await env.db.load_ui_session(-100, 1, 20)
    assert a["message_id"] != b["message_id"]
    assert a["current_menu"] == "search" and b["current_menu"] == "main"
    await env.db.close()
    await env.db.open()
    await env.h.handle_text_search(message(env, tid=10, text="Кухня"))
    assert (await env.db.load_ui_session(-100, 1, 20)) == b
    assert all(c[1].get("message_thread_id") in {10, 20} for c in env.tg.calls if c[0] == "sendMessage")


async def test_group_input_is_personal_and_cancelled(env):
    await env.h.cmd_start(message(env))
    await show(env, "tda:todo.home")
    await click(env, "tda:todo.home")
    await env.h.handle_text_search(message(env, uid=2, text="Не моя задача"))
    assert not env.ha.calls
    await click(env, "menu:todo")
    await env.h.handle_text_search(message(env, text="Отменено"))
    assert not any(c[1] == "add_item" for c in env.ha.calls)


async def test_unlisted_user_cannot_use_commands(env):
    env.cfg.allowed_user_ids = frozenset({99})
    for command in (env.h.cmd_start, env.h.cmd_schedule, env.h.cmd_terminal, env.h.cmd_snapshot):
        await command(message(env, text="/schedule add X | * * * * * | light.turn_on | light.kitchen"))
    assert not env.ha.calls
    assert not await env.db.get_schedules(1)
    assert not await env.db.load_ui_session(-100, 1)


async def test_guest_and_role_revocation(env):
    await env.h.cmd_start(message(env))
    await show(env, "act:light.kitchen:turn_on")
    await env.db.set_user_role(1, "guest")
    await click(env, "act:light.kitchen:turn_on")
    assert not env.ha.calls
    await env.h.cmd_schedule(message(env, text="/schedule add X | * * * * * | light.turn_on | light.kitchen"))
    assert not await env.db.get_schedules(1)


async def test_schedule_arbitrary_service_denied(env):
    await env.h.cmd_schedule(message(env, text="/schedule add bad | * * * * * | shell_command.run | shell_command.run"))
    assert not await env.db.get_schedules(1)
    assert validate_cron("*/0 * * * *") is not None


async def test_sqlite_additive_migration(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE menu_state(chat_id INTEGER PRIMARY KEY,message_id INTEGER,current_menu TEXT, selected_entity TEXT,selected_room TEXT,updated_at REAL); INSERT INTO menu_state VALUES(-100,42,'main',NULL,NULL,1); CREATE TABLE favorites(user_id INTEGER,entity_id TEXT,PRIMARY KEY(user_id,entity_id)); INSERT INTO favorites VALUES(1,'light.kitchen');")
    conn.close()
    db = Database(path)
    await db.open()
    assert await db.get_favorites(1) == ["light.kitchen"]
    assert (await db.get_menu_state(-100))["message_id"] == 42
    assert not await db.load_ui_session(-100, 1)
    await db.save_ui_session(-100, 1, 20, {"message_id": 43})
    await db.close()
    await db.open()
    assert (await db.load_ui_session(-100, 1, 20))["message_id"] == 43
    await db.close()


async def test_not_modified_does_not_delete_or_send_new_message(env):
    await env.h.cmd_start(message(env))
    env.tg.fail["editMessageText"] = lambda m: TelegramBadRequest(method=m, message="message is not modified")
    await click(env, "menu:search")
    assert len([x for x in env.tg.calls if x[0] == "sendMessage"]) == 1
    assert not any(x[0] == "deleteMessage" for x in env.tg.calls)


async def test_long_entity_id_uses_short_capability(env):
    eid = "light." + "kitchen_" * 30
    await show(env, f"act:{eid}:turn_on")
    state = await env.db.load_ui_session(-100, 1)
    assert all(len(k.encode()) <= 64 for k in state["actions"])
    assert f"act:{eid}:turn_on" in state["actions"].values()


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_unavailable_card_has_no_write_controls(state):
    text, kb = build_entity_control("light.x", {"state": state, "attributes": {"brightness": 50}})
    assert "⚠" in text
    assert not any(b.callback_data.startswith(("act:", "bright:")) for r in kb.inline_keyboard for b in r)


def test_capabilities_and_escape_and_home():
    text, kb = build_entity_control("cover.x", {"state": "closed", "attributes": {"friendly_name": "<Garage>", "supported_features": 1}})
    assert "&lt;Garage&gt;" in text
    assert [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data.startswith("act:")] == ["act:cover.x:open_cover"]
    text, kb = build_main_menu(HomeMock().states.values(), ["light.kitchen"])
    assert "quick:light.kitchen:turn_on" in [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "Свет" in text


async def test_deferred_slider_does_not_reopen_after_navigation(env):
    await env.h.cmd_start(message(env))
    await show(env, "ent:light.kitchen")
    await click(env, "ent:light.kitchen")
    await click(env, "bright:light.kitchen:up")
    await click(env, "nav:main")
    await asyncio.sleep(.4)
    assert (await env.db.load_ui_session(-100, 1))["current_menu"] == "main"
    assert len(env.ha.calls) == 1


async def test_read_modify_write_concurrent_temperature(env):
    for uid in (1, 2):
        await env.h.cmd_start(message(env, uid=uid))
        await show(env, "ent:climate.bedroom", uid=uid)
        await click(env, "ent:climate.bedroom", uid=uid)
    await asyncio.gather(click(env, "clim:climate.bedroom:up", uid=1), click(env, "clim:climate.bedroom:up", uid=2))
    assert env.ha.states["climate.bedroom"]["attributes"]["temperature"] == 21


async def test_ha_offline_inline_still_opens(env):
    env.ha.offline = True
    await env.h.cmd_start(message(env))
    assert "недоступен" in next(x[1]["text"] for x in env.tg.calls if x[0] == "sendMessage")
    assert not any("Widget" in x[0] for x in env.tg.calls)


def widget_query(state, key, uid=1, cid=-100, qid="q1"):
    return {"id": qid, "from": {"id": uid, "is_bot": False, "first_name": "Test"},
            "chat": {"id": cid, "type": "supergroup"}, "message_id": state["widget_message_id"],
            "chat_instance": "test", "data": key, "type": {"type": "data"}}


async def test_widget_dispatcher_uses_same_routes(env):
    env.cfg.widgets_enabled = True
    await env.h.cmd_start(message(env, text="/start"))
    state = await env.db.load_ui_session(-100, 1)
    assert state["widget_message_id"]
    key = next(k for k, v in state["widget_actions"].items() if v == "domain:light:0")
    dp = Dispatcher()
    dp.update.outer_middleware(WidgetUpdates(env.h.widgets))
    await dp.feed_update(env.bot, Update.model_validate({"update_id": 10, "widget_query": widget_query(state, key)}))
    updated = await env.db.load_ui_session(-100, 1)
    assert updated["current_menu"] == "domain:light"
    assert any(x[0] == "answerWidgetQuery" for x in env.tg.calls)
    assert any(x[0] == "editMessageWidgetData" for x in env.tg.calls)
    assert not any(x[0] == "sendMessage" for x in env.tg.calls)


async def test_widget_forbidden_falls_back_and_circuit_survives_restart(env):
    env.cfg.widgets_enabled = True
    env.tg.fail["sendWidget"] = lambda m: TelegramBadRequest(method=m, message="INPUT_MEDIA_WIDGET_NOT_ALLOWED")
    await env.h.cmd_start(message(env, text="/start"))
    await env.db.close()
    await env.db.open()
    await env.h.cmd_start(message(env, uid=2, text="/start"))
    assert len([x for x in env.tg.calls if x[0] == "sendWidget"]) == 1
    assert len([x for x in env.tg.calls if x[0] == "sendMessage"]) == 2
    assert (await env.db.load_ui_session(-100, 1))["mode"] == "inline"
    await env.db.set_runtime_value("widgets_retry_at", "0")
    del env.tg.fail["sendWidget"]
    await env.h.cmd_widgets(message(env, text="/widgets"))
    assert (await env.db.load_ui_session(-100, 1))["mode"] == "widget"


async def test_foreign_widget_and_shared_query_never_act(env):
    env.cfg.widgets_enabled = True
    await env.h.cmd_start(message(env, text="/start"))
    state = await env.db.load_ui_session(-100, 1)
    key = next(iter(state["widget_actions"]))
    await env.h.widgets.query(widget_query(state, key, uid=2))
    await env.h.widgets.query({"id": "shared", "data": "anything", "widget_id": "forward"}, shared=True)
    assert not env.ha.calls
    assert not any(x[0] == "editMessageWidgetData" for x in env.tg.calls)


async def test_widget_fallback_button_and_menu_command(env):
    env.cfg.widgets_enabled = True
    await env.h.cmd_start(message(env, text="/start"))
    state = await env.db.load_ui_session(-100, 1)
    await env.h.handle_callback(callback(env, 1, -100, state["widget_message_id"], "ui:inline"))
    assert (await env.db.load_ui_session(-100, 1))["mode"] == "inline"
    await env.h.cmd_widgets(message(env, text="/widgets"))
    await env.h.cmd_start(message(env, text="/menu"))
    assert not (await env.db.load_ui_session(-100, 1)).get("widget_actions")


def test_widget_limits_utf8_and_compression():
    raw, packed = validate_html(HTML)
    assert raw <= 32768 and packed <= 4096
    for value in ("я" * 513, "x\0"):
        with pytest.raises(ValueError):
            check_text(value, 1024)
    for value in ("x" * 32769, "\0"):
        with pytest.raises(ValueError):
            validate_html(value)
    import random, string
    random.seed(1)
    with pytest.raises(ValueError):
        validate_html("".join(random.choices(string.ascii_letters, k=12000)))


async def test_widget_all_methods_and_state_sizes(env):
    api = WidgetTransport(env.bot)
    await api.send(chat_id=1, html=HTML, chat_data="{}")
    await api.answer("q", "Принято")
    await api.edit(1, 101, chat_data="{}")
    await api.edit_shared("id", "{}", 30)
    assert {x[0] for x in env.tg.calls} == {"sendWidget", "answerWidgetQuery", "editMessageWidgetData", "editSharedWidgetData"}
    with pytest.raises(ValueError):
        await api.answer("q", "я" * 513)
    with pytest.raises(ValueError):
        await api.edit_shared("id", "{}", 1)


@pytest.mark.parametrize("domain,service,data,role", [
    ("shell_command", "run", {"entity_id": "shell_command.run"}, "admin"),
    ("light", "turn_on", {"entity_id": "all"}, "admin"),
    ("light", "turn_on", {"entity_id": "switch.x"}, "admin"),
    ("light", "turn_on", {"entity_id": "light.x", "area_id": "home"}, "admin"),
    ("lock", "unlock", {"entity_id": "lock.door"}, "user"),
    ("automation", "trigger", {"entity_id": "automation.x"}, "user"),
    ("light", "turn_on", {"entity_id": "light.x"}, "guest"),
])
def test_service_boundary_rejects_unsafe_payload(domain, service, data, role):
    assert validate_service(domain, service, data, role=role)


async def test_cooldown_is_atomic_with_concurrent_session_commits(env):
    outcomes = await asyncio.gather(
        *(env.db.check_and_update_cooldown(1, "test", 10) for _ in range(20)),
        *(env.db.save_ui_session(-100, uid, 0, {"message_id": uid}) for uid in range(2, 10)),
    )
    assert sum(x[0] for x in outcomes[:20]) == 1


async def test_parallel_same_callback_executes_once(env):
    await env.h.cmd_start(message(env))
    await show(env, "act:light.kitchen:turn_on")
    state = await env.db.load_ui_session(-100, 1)
    cb = callback(env, 1, -100, state["message_id"], next(iter(state["actions"])))
    await asyncio.gather(env.h.handle_callback(cb), env.h.handle_callback(cb))
    assert len(env.ha.calls) == 1


async def test_no_automatic_retry_of_service_post(env):
    # Exercise the real retry policy, without a network request.
    class Response:
        status = 503
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def text(self): return "Unavailable"
    class HTTP:
        def __init__(self): self.count = 0
        def request(self, *args, **kwargs):
            self.count += 1
            return Response()
    client = HAClient("fake")
    client._session = HTTP()
    ok, error = await client.call_service("script", "turn_on", {"entity_id": "script.night"})
    assert not ok and client._session.count == 1


async def test_todo_response_is_preserved():
    client = HAClient("fake")
    client._request = AsyncMock(return_value=(True, {"service_response": {"todo.home": {"items": [{"summary": "Tea"}]}}}))
    ok, data = await client.call_service("todo", "get_items", {"entity_id": "todo.home"})
    assert ok and data["todo.home"]["items"][0]["summary"] == "Tea"
    assert client._request.call_args.args[1].endswith("?return_response")


async def test_list_states_coalesces_and_invalidates_on_write():
    client = HAClient("fake")
    client._request = AsyncMock(return_value=(True, list(HomeMock().states.values())))
    await asyncio.gather(*(client.list_states() for _ in range(10)))
    assert client._request.await_count == 1
    client._request.return_value = (True, [])
    await client.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
    await client.list_states()
    assert client._request.await_count == 3


async def test_schedule_rechecks_role_after_creation(env):
    sched = {"id": 1, "user_id": 1, "name": "Night", "action_type": "service_call",
             "payload": {"domain": "light", "service": "turn_on", "data": {"entity_id": "light.kitchen"}}}
    await env.db.set_user_role(1, "guest")
    scheduler = Scheduler(env.actions, env.db, env.cfg)
    assert (await scheduler._execute(sched)).startswith("DENIED")
    assert not env.ha.calls


async def test_notification_capability_checks_message_and_owner(env):
    await env.db.save_notice(1, 500, ["nact:light.kitchen:turn_on"])
    await env.h.handle_callback(callback(env, 2, 1, 500, "nact:light.kitchen:turn_on"))
    assert not env.ha.calls
    await env.h.handle_callback(callback(env, 1, 1, 500, "nact:light.kitchen:turn_on"))
    assert len(env.ha.calls) == 1


async def test_radio_preferences_and_breadcrumbs_are_independent(env):
    for uid in (1, 2):
        await env.h.cmd_start(message(env, uid=uid))
        state = await env.db.load_ui_session(-100, uid)
        token = current_session.set(UISession(-100, uid, 0, state))
        try:
            env.h._push_nav(-100, f"ar:room{uid}")
            env.h._radio_state[-100] = {"player_eid": f"media_player.room{uid}"}
            await env.db.save_ui_session(-100, uid, 0, state)
        finally:
            current_session.reset(token)
    first, second = await env.db.load_ui_session(-100, 1), await env.db.load_ui_session(-100, 2)
    assert first["state"]["navigation"] != second["state"]["navigation"]
    assert first["state"]["radio"] != second["state"]["radio"]


async def test_widget_limits_per_user_across_messages(env):
    env.cfg.widgets_enabled = True
    await env.h.cmd_start(message(env, text="/start"))
    state = await env.db.load_ui_session(-100, 1)
    for i in range(6):
        # Invalid actions count too; a forged payload cannot skip the rate gate.
        await env.h.widgets.query(widget_query(state, "forged", qid=f"limit{i}"))
    last = [x[1]["data"] for x in env.tg.calls if x[0] == "answerWidgetQuery"][-1]
    assert "Слишком часто" in last
    assert not env.ha.calls


async def test_widget_ack_precedes_slow_home_assistant(env):
    env.cfg.widgets_enabled = True
    await env.h.cmd_start(message(env, text="/start"))
    await show(env, "act:light.kitchen:turn_on")
    state = await env.db.load_ui_session(-100, 1)
    key = next(k for k, v in state["widget_actions"].items() if v == "act:light.kitchen:turn_on")
    original = env.ha.call_service
    async def slow(*args):
        assert any(x[0] == "answerWidgetQuery" for x in env.tg.calls)
        await asyncio.sleep(.02)
        return await original(*args)
    env.ha.call_service = slow
    await env.h.widgets.query(widget_query(state, key))
    assert env.ha.states["light.kitchen"]["state"] == "on"
    assert all(len(x[1].get("data", "").encode()) <= 1024 for x in env.tg.calls if x[0] == "answerWidgetQuery")


async def test_widget_pages_reach_every_action_with_bounded_state(env):
    env.cfg.widgets_enabled = True
    buttons = [InlineKeyboardButton(text="Очень длинное название " * 4, callback_data=f"ent:light.long_{i}") for i in range(30)]
    session = UISession(-100, 1, 0, {"mode": "widget"})
    token = current_session.set(session)
    try:
        await env.h._send_or_edit(-100, "<b>Устройства</b>\n" + "Информация " * 100,
                                  InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons]))
        reached = set()
        for page in range(20):
            data, encoded, actions, _ = env.h.widgets._page(session, page)
            assert len(encoded.encode()) <= 1024
            assert len(data["a"]) <= 6
            reached.update(actions.values())
        assert all(b.callback_data in reached for b in buttons)
    finally:
        current_session.reset(token)


async def test_widget_edit_error_restores_inline(env):
    env.cfg.widgets_enabled = True
    await env.h.cmd_start(message(env, text="/start"))
    state = await env.db.load_ui_session(-100, 1)
    key = next(k for k, v in state["widget_actions"].items() if v == "domain:light:0")
    env.tg.fail["editMessageWidgetData"] = lambda m: TelegramRetryAfter(method=m, message="retry", retry_after=30)
    await env.h.widgets.query(widget_query(state, key))
    assert (await env.db.load_ui_session(-100, 1))["mode"] == "inline"
    assert any(x[0] == "sendMessage" for x in env.tg.calls)


async def test_inline_flood_wait_keeps_old_capabilities(env):
    await env.h.cmd_start(message(env))
    state = await env.db.load_ui_session(-100, 1)
    env.tg.fail["editMessageText"] = lambda m: TelegramRetryAfter(method=m, message="retry", retry_after=30)
    await click(env, "menu:search")
    after = await env.db.load_ui_session(-100, 1)
    assert after["actions"] == state["actions"]
    assert after["current_menu"] == "main"


async def test_lock_unlock_requires_admin_and_confirmation(env):
    env.ha.states["lock.door"] = {"entity_id": "lock.door", "state": "locked", "attributes": {}}
    await env.db.set_user_role(1, "admin")
    await env.h.cmd_start(message(env))
    await show(env, "act:lock.door:unlock")
    await click(env, "act:lock.door:unlock")
    assert not env.ha.calls
    await click(env, "confirm:act:lock.door:unlock")
    assert env.ha.calls[0][:2] == ("lock", "unlock")


async def test_terminal_never_runs_for_user(env, monkeypatch):
    env.cfg.terminal_enabled = True
    shell = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_shell", shell)
    await env.h.cmd_terminal(message(env, text="/terminal echo test"))
    shell.assert_not_awaited()


def test_observation_age_does_not_treat_state_change_as_heartbeat():
    from observations import observation_age
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert observation_age({"last_changed": now.isoformat()}, now=now) == "Давность данных неизвестна"
    assert "Данные изменены: 1 дн" in observation_age({"last_updated": "2026-01-01T00:00:00Z"}, now=now, max_age=60)
    assert "давно нет данных" in observation_age({"last_reported": "2026-01-01T00:00:00Z"}, now=now, max_age=3600)
    assert "давно нет данных" not in observation_age({"last_reported": "2026-01-01T00:00:00Z"}, now=now)
    assert "неизвестна" in observation_age({"last_reported": "bad"}, now=now)


async def test_home_uses_environment_sensors_and_freshness(env):
    from registry import EntityInfo
    for eid, kind in [("sensor.cpu", "temperature"), ("sensor.room", "temperature"), ("sensor.humidity", "humidity")]:
        env.ha.states[eid] = {"entity_id": eid, "state": "42", "last_updated": "2020-01-01T00:00:00Z",
                              "attributes": {"device_class": kind, "friendly_name": eid}}
        env.h._reg.entities[eid] = EntityInfo(entity_id=eid, entity_category="diagnostic" if eid == "sensor.cpu" else None)
    text, _ = await env.h._home_view(1)
    assert "sensor.cpu" not in text
    assert "sensor.room" in text and "sensor.humidity" in text
    assert "Данные изменены" in text


async def test_humidifier_capability_and_bounded_target(env):
    eid = "humidifier.room"
    env.ha.states[eid] = {"entity_id": eid, "state": "on", "attributes": {"humidity": 60, "max_humidity": 60,
                                                                                 "supported_features": 0, "available_modes": ["auto"]}}
    _, keyboard = build_entity_control(eid, env.ha.states[eid])
    assert not any(b.callback_data.startswith("hmode:") for row in keyboard.inline_keyboard for b in row)
    await env.h.cmd_start(message(env))
    await show(env, f"humi:{eid}:up")
    await click(env, f"humi:{eid}:up")
    assert env.ha.calls[-1] == ("humidifier", "set_humidity", {"entity_id": eid, "humidity": 60})


async def test_automation_view_enable_and_run_are_distinct(env):
    eid = "automation.test"
    env.ha.states[eid] = {"entity_id": eid, "state": "on", "attributes": {"description": "<script>unsafe</script>"}}
    await env.db.set_user_role(1, "admin")
    text, keyboard = build_entity_control(eid, env.ha.states[eid])
    assert "&lt;script&gt;" in text
    actions = {b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert f"atog:{eid}:turn_off" in actions and f"atrig:{eid}" in actions
    await env.h.cmd_start(message(env))
    await show(env, f"atrig:{eid}")
    await click(env, f"atrig:{eid}")
    assert not env.ha.calls
    await click(env, f"confirm:atrig:{eid}")
    assert env.ha.calls[-1] == ("automation", "trigger", {"entity_id": eid, "skip_condition": False})
    assert validate_service("automation", "trigger", {"entity_id": eid, "skip_condition": True}, role="admin")


async def test_service_exception_is_audited_without_claiming_success(env):
    env.ha.get_state = AsyncMock(side_effect=OSError("offline"))
    token = current_session.set(UISession(-100, 1, 0, {}))
    try:
        ok, error = await env.actions.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
        assert not ok and "недоступно" in error
        assert "не подтверждена" in current_session.get().data["service_receipt"]
        assert not env.ha.calls
    finally:
        current_session.reset(token)


def test_global_lights_use_explicit_membership_only():
    from observations import independent_lights
    leaf = {"entity_id": "light.a", "attributes": {}}
    other = {"entity_id": "light.b", "attributes": {"friendly_name": "Group"}}
    group = {"entity_id": "light.group", "attributes": {"entity_id": ["light.a", "light.b"]}}
    assert independent_lights([group, leaf, other]) == [leaf, other]
    assert independent_lights([leaf, other]) == [leaf, other]


async def test_vacuum_room_capability_no_guessed_names_or_full_clean(env):
    from vacuum_adapter import VacuumAdapter
    from registry import EntityInfo, AreaInfo
    eid = "vacuum.test"
    env.ha.states[eid] = {"entity_id": eid, "state": "docked", "attributes": {"supported_features": 16384 | 256}}
    reg = env.h._reg
    reg.entities[eid] = EntityInfo(entity_id=eid, platform="roborock", device_id="device",
                                   options={"vacuum": {"area_mapping": {"kitchen": ["16"]}}})
    reg.areas["kitchen"] = AreaInfo(area_id="kitchen", name="Kitchen")
    reg.vacuum_platforms[eid] = "roborock"
    vacuum = VacuumAdapter(env.actions, env.db, reg, "service_data", "", ("guessed_room",))
    assert (await vacuum.get_rooms(eid))[0]["segment_id"] == "area:kitchen"
    token = current_session.set(UISession(-100, 1, 0, {}))
    try:
        assert (await vacuum.clean_segment(eid, "area:kitchen"))[0]
        assert env.ha.calls[-1] == ("vacuum", "clean_area", {"entity_id": eid, "cleaning_area_id": ["kitchen"]})
        before = len(env.ha.calls)
        assert not (await vacuum.clean_segment(eid, "guessed_room"))[0]
        reg.entities[eid].options = {}
        assert not (await vacuum.clean_segment(eid, "area:kitchen"))[0]
        assert len(env.ha.calls) == before
        assert not (await vacuum.get_capabilities(eid)).supports_segment_clean
    finally:
        current_session.reset(token)


async def test_vacuum_legacy_numeric_map_is_preserved(env):
    from vacuum_adapter import VacuumAdapter
    from registry import EntityInfo
    eid = "vacuum.old"
    env.h._reg.entities[eid] = EntityInfo(entity_id=eid, platform="roborock")
    env.h._reg.vacuum_platforms[eid] = "roborock"
    env.ha.states[eid] = {"entity_id": eid, "state": "docked", "attributes": {"supported_features": 256}}
    await env.db.save_vacuum_room_map(eid, [{"segment_id": "16", "segment_name": "Kitchen"}])
    vacuum = VacuumAdapter(env.actions, env.db, env.h._reg, "service_data", "", ())
    assert len(await vacuum.get_rooms(eid)) == 1
    token = current_session.set(UISession(-100, 1, 0, {}))
    try:
        assert (await vacuum.clean_segment(eid, "16"))[0]
        assert env.ha.calls[-1][2]["params"] == [16]
    finally:
        current_session.reset(token)


def test_vacuum_maintenance_button_is_not_a_routine(env):
    from registry import EntityInfo
    reg = env.h._reg
    reg.entities = {
        "vacuum.test": EntityInfo(entity_id="vacuum.test", device_id="device"),
        "button.reset": EntityInfo(entity_id="button.reset", device_id="device", original_name="Reset mop", entity_category="config"),
        "button.routine": EntityInfo(entity_id="button.routine", device_id="device", translation_key="routine"),
    }
    reg._detect_vacuum_routines()
    assert reg.vacuum_routines["vacuum.test"] == ["button.routine"]


async def test_widget_escaped_labels_stay_inside_state_limit(env):
    state = UISession(-100, 1, 0, {"widget_view": {"title": '"' * 90, "body": '\\' * 220,
                                                "buttons": [('"' * 100, f"ent:light.a{i}") for i in range(6)]}})
    data, encoded, actions, _ = env.h.widgets._page(state)
    assert len(encoded.encode()) <= 1024
    assert len(actions) >= 4


async def test_settings_roundtrip_does_not_escalate_or_execute(env):
    payload = {"domain": "light", "service": "turn_on", "data": {"entity_id": "light.kitchen"}}
    await env.db.add_favorite_action(1, "service_call", payload, "Morning")
    await env.db.add_schedule(1, "Morning", "service_call", payload, "0 7 * * *", 0)
    exported = await env.db.export_user_settings(1)
    exported["role"] = "admin"
    assert await env.db.import_user_settings(2, exported) == 2
    assert await env.db.get_user_role(2) == "user"
    assert (await env.db.get_favorite_actions(2))[0]["payload"] == payload
    assert not (await env.db.get_schedules(2))[0]["enabled"]
    assert await env.db.import_user_settings(2, exported) == 0
    assert not env.ha.calls


async def test_settings_import_rejects_arbitrary_services_and_bad_shapes(env):
    data = {"favorites": None, "notifications": [{"entity_id": ["bad"]}], "pinned_items": 5,
            "favorite_actions": [{"action_type": "service_call", "payload": {"domain": "shell_command", "service": "run", "data": {"entity_id": "shell_command.run"}}}]}
    assert await env.db.import_user_settings(1, data) == 0
    assert not await env.db.get_favorite_actions(1)


async def test_vacuum_transition_is_not_claimed_as_completed_and_revoked_users_not_notified(env):
    from notifications import NotificationManager
    await env.db.toggle_notification(1, "vacuum.test")
    await env.db.toggle_notification(2, "vacuum.test")
    env.cfg.allowed_user_ids = frozenset({1})
    manager = NotificationManager("fake", env.db, env.bot, env.cfg)
    await manager._handle_event({"type": "event", "event": {"event_type": "state_changed", "data": {
        "entity_id": "vacuum.test", "old_state": {"state": "returning"},
        "new_state": {"state": "docked", "attributes": {"friendly_name": "Vacuum"}}}}})
    sends = [payload for method, payload in env.tg.calls if method == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == 1
    assert "Уборка завершена" not in sends[0]["text"]
    assert "Home Assistant сообщает" in sends[0]["text"]


async def test_shared_widget_edit_pacing(env, monkeypatch):
    import widget_transport
    sleep = AsyncMock()
    monkeypatch.setattr(widget_transport.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(widget_transport.asyncio, "sleep", sleep)
    api = WidgetTransport(env.bot)
    await api.edit_shared("widget1", "{}")
    await api.edit_shared("widget1", "{}")
    sleep.assert_awaited_once_with(1.0)


async def test_scene_launch_requires_confirmation(env):
    env.ha.states["scene.evening"] = {"entity_id": "scene.evening", "state": "unknown", "attributes": {"friendly_name": "Evening"}}
    await env.h.cmd_start(message(env))
    await show(env, "qsc:scene.evening")
    await click(env, "qsc:scene.evening")
    assert not env.ha.calls
    assert "Evening" in [x[1]["text"] for x in env.tg.calls if x[0] == "editMessageText"][-1]
    await click(env, "confirm:qsc:scene.evening")
    assert env.ha.calls[-1][:2] == ("scene", "turn_on")


async def test_hidden_only_home_does_not_claim_ha_is_offline(env):
    from registry import EntityInfo
    for eid in env.ha.states:
        env.h._reg.entities[eid] = EntityInfo(entity_id=eid, hidden_by="user")
    text, _ = await env.h._home_view(1)
    assert "Home Assistant недоступен" not in text
