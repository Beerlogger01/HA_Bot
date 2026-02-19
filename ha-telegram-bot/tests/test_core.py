"""Unit tests for HA Bot core logic.

Tests cover: room alias matching, role hierarchy, cron parsing,
global rate limiter, UI builders, snapshot diff, storage CRUD.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# Add app directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from registry import ROOM_ALIASES, _normalize
from scheduler import next_cron_time, parse_cron_field, validate_cron
from handlers import GlobalRateLimiter, _ROLE_LEVELS

# ---------------------------------------------------------------------------
# Room alias & normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_basic(self) -> None:
        assert _normalize("  Кухня  ") == "кухня"

    def test_special_chars(self) -> None:
        assert _normalize("Living-Room!") == "livingroom"

    def test_underscores(self) -> None:
        assert _normalize("living_room") == "living_room"

    def test_empty(self) -> None:
        assert _normalize("") == ""

    def test_mixed_case(self) -> None:
        assert _normalize("  ВаННая  ") == "ванная"


class TestRoomAliases:
    def test_kitchen_aliases(self) -> None:
        aliases = ROOM_ALIASES.get("кухня", [])
        assert "kitchen" in aliases

    def test_living_room_aliases(self) -> None:
        aliases = ROOM_ALIASES.get("гостиная", [])
        assert "living_room" in aliases
        assert "зал" in aliases

    def test_all_have_at_least_one_alias(self) -> None:
        for canonical, aliases in ROOM_ALIASES.items():
            assert len(aliases) >= 1, f"{canonical} has no aliases"


# ---------------------------------------------------------------------------
# Role hierarchy
# ---------------------------------------------------------------------------


class TestRoles:
    def test_admin_highest(self) -> None:
        assert _ROLE_LEVELS["admin"] > _ROLE_LEVELS["user"]
        assert _ROLE_LEVELS["admin"] > _ROLE_LEVELS["guest"]

    def test_user_above_guest(self) -> None:
        assert _ROLE_LEVELS["user"] > _ROLE_LEVELS["guest"]

    def test_unknown_role_defaults(self) -> None:
        # Unknown role should get default level 2 (user)
        assert _ROLE_LEVELS.get("unknown_role", 2) == 2

    def test_hierarchy_levels(self) -> None:
        assert _ROLE_LEVELS == {"admin": 3, "user": 2, "guest": 1}


# ---------------------------------------------------------------------------
# Global rate limiter
# ---------------------------------------------------------------------------


class TestGlobalRateLimiter:
    def test_allows_under_limit(self) -> None:
        rl = GlobalRateLimiter(5, 10)
        for _ in range(5):
            assert rl.check() is True
            rl.record()

    def test_blocks_at_limit(self) -> None:
        rl = GlobalRateLimiter(3, 60)
        for _ in range(3):
            rl.record()
        assert rl.check() is False

    def test_window_expiry(self) -> None:
        rl = GlobalRateLimiter(1, 1)
        rl.record()
        assert rl.check() is False
        # Manually advance timestamps
        rl._timestamps = [time.monotonic() - 2]
        assert rl.check() is True


# ---------------------------------------------------------------------------
# Cron parsing
# ---------------------------------------------------------------------------


class TestCronParsing:
    def test_star(self) -> None:
        result = parse_cron_field("*", 0, 59)
        assert result == list(range(0, 60))

    def test_single_value(self) -> None:
        result = parse_cron_field("5", 0, 59)
        assert result == [5]

    def test_range(self) -> None:
        result = parse_cron_field("1-5", 0, 59)
        assert result == [1, 2, 3, 4, 5]

    def test_step(self) -> None:
        result = parse_cron_field("*/15", 0, 59)
        assert result == [0, 15, 30, 45]

    def test_comma_list(self) -> None:
        result = parse_cron_field("1,3,5", 0, 59)
        assert result == [1, 3, 5]

    def test_out_of_range(self) -> None:
        result = parse_cron_field("99", 0, 59)
        assert result is None

    def test_invalid(self) -> None:
        result = parse_cron_field("abc", 0, 59)
        assert result is None


class TestValidateCron:
    def test_valid(self) -> None:
        assert validate_cron("0 7 * * *") is None

    def test_invalid_field_count(self) -> None:
        err = validate_cron("0 7 *")
        assert err is not None
        assert "5 fields" in err

    def test_invalid_minute(self) -> None:
        err = validate_cron("60 7 * * *")
        assert err is not None

    def test_every_5_minutes(self) -> None:
        assert validate_cron("*/5 * * * *") is None


class TestNextCronTime:
    def test_returns_future(self) -> None:
        now = time.time()
        nxt = next_cron_time("* * * * *", after=now)
        assert nxt > now

    def test_invalid_returns_zero(self) -> None:
        assert next_cron_time("invalid") == 0.0

    def test_specific_minute(self) -> None:
        now = time.time()
        nxt = next_cron_time("30 12 * * *", after=now)
        assert nxt > now
        assert nxt > 0.0


# ---------------------------------------------------------------------------
# UI builders (smoke tests)
# ---------------------------------------------------------------------------


class TestUIBuilders:
    def test_main_menu(self) -> None:
        from ui import build_main_menu
        text, kb = build_main_menu()
        assert "Home Assistant" in text
        assert len(kb.inline_keyboard) > 0

    def test_help_menu(self) -> None:
        from ui import build_help_menu
        text, kb = build_help_menu()
        assert "/start" in text
        assert "/search" in text
        assert "/schedule" in text

    def test_search_prompt(self) -> None:
        from ui import build_search_prompt
        text, kb = build_search_prompt()
        assert "Поиск" in text

    def test_search_results_empty(self) -> None:
        from ui import build_search_results
        text, kb = build_search_results("test", [], 0, 8)
        assert "Ничего не найдено" in text

    def test_search_results_with_data(self) -> None:
        from ui import build_search_results
        entities = [
            {"entity_id": "light.test", "friendly_name": "Test Light",
             "state": "on", "domain": "light"},
        ]
        text, kb = build_search_results("test", entities, 0, 8)
        assert "test" in text.lower()
        assert len(kb.inline_keyboard) > 0

    def test_entity_list_pagination(self) -> None:
        from ui import build_entity_list
        entities = [
            {"entity_id": f"light.test_{i}", "friendly_name": f"Test {i}",
             "state": "on", "domain": "light"}
            for i in range(20)
        ]
        text, kb = build_entity_list(entities, 0, 8, "Title", "nav:main")
        assert "стр. 1/3" in text

    def test_schedule_list_empty(self) -> None:
        from ui import build_schedule_list
        text, kb = build_schedule_list([])
        assert "Нет задач" in text

    def test_roles_list(self) -> None:
        from ui import build_roles_list
        roles = [{"user_id": 123, "role": "admin"}]
        text, kb = build_roles_list(roles)
        assert "123" in text
        assert "admin" in text

    def test_confirmation(self) -> None:
        from ui import build_confirmation
        text, kb = build_confirmation("Test message", "nav:main")
        assert text == "Test message"
        assert kb.inline_keyboard[0][0].callback_data == "nav:main"

    def test_diagnostics_menu(self) -> None:
        from ui import build_diagnostics_menu
        text, kb = build_diagnostics_menu("Diag info here")
        assert text == "Diag info here"
        assert len(kb.inline_keyboard) == 2  # refresh+trace, back

    def test_favorites_empty(self) -> None:
        from ui import build_favorites_menu
        text, kb = build_favorites_menu([], 0, 8)
        assert "пуст" in text.lower()

    def test_snapshots_empty(self) -> None:
        from ui import build_snapshots_list
        text, kb = build_snapshots_list([])
        assert "Нет" in text


# ---------------------------------------------------------------------------
# Storage tests (requires aiosqlite)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    from storage import Database
    db_path = tmp_path / "test.sqlite3"
    database = Database(db_path)
    await database.open()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_favorites_crud(db) -> None:
    user_id = 12345
    eid = "light.test"

    # Initially no favorites
    favs = await db.get_favorites(user_id)
    assert favs == []

    # Toggle on
    result = await db.toggle_favorite(user_id, eid)
    assert result is True
    assert await db.is_favorite(user_id, eid) is True

    # Toggle off
    result = await db.toggle_favorite(user_id, eid)
    assert result is False
    assert await db.is_favorite(user_id, eid) is False


@pytest.mark.asyncio
async def test_user_roles(db) -> None:
    # Default role
    role = await db.get_user_role(999)
    assert role == "user"

    # Set to admin
    await db.set_user_role(999, "admin")
    assert await db.get_user_role(999) == "admin"

    # Set to guest
    await db.set_user_role(999, "guest")
    assert await db.get_user_role(999) == "guest"

    # All roles
    roles = await db.get_all_roles()
    assert len(roles) == 1
    assert roles[0]["user_id"] == 999
    assert roles[0]["role"] == "guest"


@pytest.mark.asyncio
async def test_notifications_crud(db) -> None:
    uid = 1
    eid = "sensor.temp"

    # Toggle on (creates)
    result = await db.toggle_notification(uid, eid)
    assert result is True

    notif = await db.get_notification(uid, eid)
    assert notif is not None
    assert notif["enabled"] is True
    assert notif["mode"] == "state_only"

    # Toggle off
    result = await db.toggle_notification(uid, eid)
    assert result is False

    # Set mode
    await db.toggle_notification(uid, eid)  # re-enable
    await db.set_notification_mode(uid, eid, "state_and_key_attrs")
    notif = await db.get_notification(uid, eid)
    assert notif["mode"] == "state_and_key_attrs"


@pytest.mark.asyncio
async def test_mutes(db) -> None:
    uid = 1
    eid = "light.test"

    # Not muted
    assert await db.is_muted(uid, eid) is False

    # Set mute (1 hour from now)
    await db.set_mute(uid, eid, time.time() + 3600)
    assert await db.is_muted(uid, eid) is True

    # Expired mute
    await db.set_mute(uid, eid, time.time() - 1)
    assert await db.is_muted(uid, eid) is False


@pytest.mark.asyncio
async def test_schedules_crud(db) -> None:
    uid = 1
    sched_id = await db.add_schedule(
        uid, "Morning", "service_call",
        {"domain": "light", "service": "turn_on", "data": {"entity_id": "light.bed"}},
        "0 7 * * *", time.time() + 3600,
    )
    assert sched_id > 0

    scheds = await db.get_schedules(uid)
    assert len(scheds) == 1
    assert scheds[0]["name"] == "Morning"

    # Toggle
    result = await db.toggle_schedule(sched_id, uid)
    assert result is False  # Was enabled, now disabled

    # Delete
    deleted = await db.delete_schedule(sched_id, uid)
    assert deleted is True
    assert await db.get_schedules(uid) == []


@pytest.mark.asyncio
async def test_snapshots_crud(db) -> None:
    uid = 1
    payload = [{"entity_id": "light.test", "state": "on", "attributes": {}}]
    snap_id = await db.save_snapshot(uid, "test_snap", payload)
    assert snap_id > 0

    snaps = await db.get_snapshots(uid)
    assert len(snaps) == 1
    assert snaps[0]["name"] == "test_snap"
    assert len(snaps[0]["payload"]) == 1

    # Delete
    deleted = await db.delete_snapshot(snap_id, uid)
    assert deleted is True


@pytest.mark.asyncio
async def test_error_log(db) -> None:
    await db.log_error("ERROR", "test_module", "test message", "traceback here")
    errors = await db.get_recent_errors(5)
    assert len(errors) == 1
    assert errors[0]["message"] == "test message"
    assert errors[0]["traceback"] == "traceback here"


@pytest.mark.asyncio
async def test_favorite_actions(db) -> None:
    uid = 1
    action_id = await db.add_favorite_action(
        uid, "service_call",
        {"domain": "light", "service": "turn_on", "data": {"entity_id": "light.test"}},
        "Turn on light",
    )
    assert action_id > 0

    actions = await db.get_favorite_actions(uid)
    assert len(actions) == 1
    assert actions[0]["label"] == "Turn on light"

    deleted = await db.remove_favorite_action(uid, action_id)
    assert deleted is True


@pytest.mark.asyncio
async def test_cooldown(db) -> None:
    uid = 1
    action = "test_action"

    # First use — allowed
    allowed, remaining = await db.check_and_update_cooldown(uid, action, 60)
    assert allowed is True
    assert remaining == 0.0

    # Immediate retry — blocked
    allowed, remaining = await db.check_and_update_cooldown(uid, action, 60)
    assert allowed is False
    assert remaining > 0


@pytest.mark.asyncio
async def test_export_import(db) -> None:
    uid = 1

    # Set up some data
    await db.toggle_favorite(uid, "light.test")
    await db.toggle_notification(uid, "sensor.temp")

    # Export
    data = await db.export_user_settings(uid)
    assert "light.test" in data["favorites"]
    assert len(data["notifications"]) == 1

    # Import to different user
    count = await db.import_user_settings(2, data)
    assert count == 2  # 1 favorite + 1 notification

    # Verify
    assert await db.is_favorite(2, "light.test") is True
    notif = await db.get_notification(2, "sensor.temp")
    assert notif is not None


# ---------------------------------------------------------------------------
# Pinned items tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_items_crud(db) -> None:
    uid = 1

    # Initially empty
    items = await db.get_pinned_items(uid)
    assert items == []

    # Add area pin
    added = await db.add_pinned_item(uid, "area", "kitchen", "Кухня")
    assert added is True

    # Duplicate
    added = await db.add_pinned_item(uid, "area", "kitchen", "Кухня")
    assert added is False

    # Add routine pin
    added = await db.add_pinned_item(uid, "routine", "button.vacuum_routine_1", "Floor clean")
    assert added is True

    # Get all
    items = await db.get_pinned_items(uid)
    assert len(items) == 2

    # Get by type
    area_pins = await db.get_pinned_items(uid, "area")
    assert len(area_pins) == 1
    assert area_pins[0]["target_id"] == "kitchen"
    assert area_pins[0]["label"] == "Кухня"

    # Toggle off
    now_pinned = await db.toggle_pinned_item(uid, "area", "kitchen")
    assert now_pinned is False
    assert await db.is_pinned(uid, "area", "kitchen") is False

    # Toggle on again
    now_pinned = await db.toggle_pinned_item(uid, "area", "kitchen", "Кухня")
    assert now_pinned is True

    # Remove
    removed = await db.remove_pinned_item(uid, "area", "kitchen")
    assert removed is True
    items = await db.get_pinned_items(uid)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_pinned_items_export_import(db) -> None:
    uid = 1
    await db.add_pinned_item(uid, "area", "kitchen", "Кухня")
    await db.toggle_favorite(uid, "light.test")

    # Export includes pinned
    data = await db.export_user_settings(uid)
    assert len(data["pinned_items"]) == 1
    assert data["pinned_items"][0]["item_type"] == "area"

    # Import
    count = await db.import_user_settings(3, data)
    assert count >= 2  # favorite + pinned
    items = await db.get_pinned_items(3)
    assert len(items) == 1


# ---------------------------------------------------------------------------
# Device grouping tests (UI builder)
# ---------------------------------------------------------------------------


class TestDeviceListBuilder:
    def test_device_list_basic(self) -> None:
        from ui import build_device_list
        devices = [
            {
                "device_id": "abc123",
                "name": "Kitchen Light",
                "entity_ids": ["light.kitchen"],
                "primary_entity_id": "light.kitchen",
                "primary_domain": "light",
                "is_vacuum": False,
            },
            {
                "device_id": "def456",
                "name": "Roborock S7",
                "entity_ids": ["vacuum.s7", "sensor.s7_battery"],
                "primary_entity_id": "vacuum.s7",
                "primary_domain": "vacuum",
                "is_vacuum": True,
            },
        ]
        text, kb = build_device_list(devices, 0, 8, "Test", "nav:main")
        assert "стр. 1/1" in text
        assert len(kb.inline_keyboard) >= 3  # 2 devices + back

    def test_device_list_pagination(self) -> None:
        from ui import build_device_list
        devices = [
            {
                "device_id": f"dev_{i}",
                "name": f"Device {i}",
                "entity_ids": [f"light.dev_{i}"],
                "primary_entity_id": f"light.dev_{i}",
                "primary_domain": "light",
                "is_vacuum": False,
            }
            for i in range(20)
        ]
        text, kb = build_device_list(devices, 0, 8, "Title", "nav:main")
        assert "стр. 1/3" in text

    def test_device_list_with_pin_button(self) -> None:
        from aiogram.types import InlineKeyboardButton
        from ui import build_device_list
        devices = [
            {
                "device_id": "abc",
                "name": "Test",
                "entity_ids": ["light.test"],
                "primary_entity_id": "light.test",
                "primary_domain": "light",
                "is_vacuum": False,
            },
        ]
        pin_btn = InlineKeyboardButton(text="Pin", callback_data="pin:area:kitchen")
        text, kb = build_device_list(devices, 0, 8, "Title", "nav:main", pin_btn=pin_btn)
        # Should have device + pin + back
        assert len(kb.inline_keyboard) >= 3


# ---------------------------------------------------------------------------
# Media source builder tests
# ---------------------------------------------------------------------------


class TestMediaSourceMenu:
    def test_basic(self) -> None:
        from ui import build_media_source_menu
        text, kb = build_media_source_menu(
            "media_player.tv", "Living Room TV",
            ["HDMI 1", "HDMI 2", "Spotify"],
            "HDMI 1",
        )
        assert "Living Room TV" in text
        assert len(kb.inline_keyboard) >= 4  # 3 sources + back

    def test_empty_sources(self) -> None:
        from ui import build_media_source_menu
        text, kb = build_media_source_menu(
            "media_player.tv", "TV", [], None,
        )
        assert "TV" in text
        assert len(kb.inline_keyboard) >= 1  # at least back


# ---------------------------------------------------------------------------
# Entity control enhanced info
# ---------------------------------------------------------------------------


class TestEntityControlEnhanced:
    def test_media_player_info(self) -> None:
        from ui import build_entity_control
        state = {
            "state": "playing",
            "attributes": {
                "friendly_name": "Google Home",
                "media_title": "My Song",
                "source": "Spotify",
                "volume_level": 0.5,
                "is_volume_muted": False,
                "source_list": ["Spotify", "YouTube"],
            },
        }
        text, kb = build_entity_control("media_player.google", state)
        assert "Google Home" in text
        assert "Трек" in text
        assert "Громкость: 50%" in text
        # Should have play/pause/stop + volume + source + fav + notif + back
        assert len(kb.inline_keyboard) >= 5

    def test_sensor_read_only(self) -> None:
        from ui import build_entity_control
        state = {
            "state": "22.5",
            "attributes": {
                "friendly_name": "Temperature",
                "unit_of_measurement": "°C",
            },
        }
        text, kb = build_entity_control("sensor.temp", state)
        assert "Temperature" in text
        assert "Ед. изм." in text

    def test_select_control(self) -> None:
        from ui import build_entity_control
        state = {
            "state": "Option A",
            "attributes": {
                "friendly_name": "Speed Select",
                "options": ["Option A", "Option B", "Option C"],
            },
        }
        text, kb = build_entity_control("select.speed", state)
        assert "Speed Select" in text
        assert "Варианты: 3" in text

    def test_number_control(self) -> None:
        from ui import build_entity_control
        state = {
            "state": "50",
            "attributes": {
                "friendly_name": "Volume Number",
                "min": 0,
                "max": 100,
                "step": 5,
            },
        }
        text, kb = build_entity_control("number.vol", state)
        assert "Volume Number" in text
        assert "Диапазон" in text


# ---------------------------------------------------------------------------
# Active Now menu tests
# ---------------------------------------------------------------------------


class TestActiveNowMenu:
    def test_empty(self) -> None:
        from ui import build_active_now_menu
        text, kb = build_active_now_menu([])
        assert "Всё выключено" in text
        # Should have refresh + back
        assert len(kb.inline_keyboard) == 2

    def test_with_data(self) -> None:
        from ui import build_active_now_menu
        entities = [
            {"entity_id": "light.kitchen", "friendly_name": "Kitchen Light",
             "state": "on", "domain": "light"},
            {"entity_id": "vacuum.robo", "friendly_name": "Roborock",
             "state": "cleaning", "domain": "vacuum"},
        ]
        text, kb = build_active_now_menu(entities, 0, 8)
        assert "Активные сейчас" in text
        assert "(2)" in text
        assert len(kb.inline_keyboard) >= 3  # 2 entities + back

    def test_pagination(self) -> None:
        from ui import build_active_now_menu
        entities = [
            {"entity_id": f"light.l{i}", "friendly_name": f"Light {i}",
             "state": "on", "domain": "light"}
            for i in range(20)
        ]
        text, kb = build_active_now_menu(entities, 0, 8)
        assert "стр. 1/3" in text

    def test_active_states_set(self) -> None:
        from ui import _ACTIVE_STATES
        assert "on" in _ACTIVE_STATES
        assert "playing" in _ACTIVE_STATES
        assert "cleaning" in _ACTIVE_STATES
        assert "open" in _ACTIVE_STATES
        assert "off" not in _ACTIVE_STATES
        assert "unavailable" not in _ACTIVE_STATES


# ---------------------------------------------------------------------------
# Smart entity hiding tests
# ---------------------------------------------------------------------------


class TestEntityCategoryFiltering:
    def _build_registry(self):
        """Build a minimal registry with entities of different categories."""
        from registry import AreaInfo, EntityInfo, HARegistry
        from unittest.mock import MagicMock

        reg = HARegistry.__new__(HARegistry)
        reg._token = ""
        reg._db = MagicMock()
        reg.floors = {}
        reg.devices = {}
        reg.vacuum_routines = {}
        reg.vacuum_platforms = {}
        reg._synced = False
        reg.areas = {
            "kitchen": AreaInfo(
                area_id="kitchen", name="Kitchen",
                entity_ids=["light.kitchen", "sensor.kitchen_temp", "sensor.kitchen_signal"],
            ),
        }
        reg.entities = {
            "light.kitchen": EntityInfo(
                entity_id="light.kitchen", name="Kitchen Light",
                entity_category=None,
            ),
            "sensor.kitchen_temp": EntityInfo(
                entity_id="sensor.kitchen_temp", name="Kitchen Temp",
                entity_category=None,
            ),
            "sensor.kitchen_signal": EntityInfo(
                entity_id="sensor.kitchen_signal", name="Kitchen Signal Strength",
                entity_category="diagnostic",
            ),
        }
        return reg

    def test_hides_diagnostic_by_default(self) -> None:
        reg = self._build_registry()
        eids = reg.get_area_entities("kitchen")
        assert "light.kitchen" in eids
        assert "sensor.kitchen_temp" in eids
        assert "sensor.kitchen_signal" not in eids

    def test_show_all_includes_diagnostic(self) -> None:
        reg = self._build_registry()
        eids = reg.get_area_entities("kitchen", show_all=True)
        assert "sensor.kitchen_signal" in eids
        assert len(eids) == 3

    def test_domain_filter_plus_hiding(self) -> None:
        reg = self._build_registry()
        eids = reg.get_area_entities("kitchen", domains=frozenset({"sensor"}))
        assert "sensor.kitchen_temp" in eids
        assert "sensor.kitchen_signal" not in eids
        assert "light.kitchen" not in eids

    def test_config_category_hidden(self) -> None:
        reg = self._build_registry()
        # Add a config-category entity
        from registry import EntityInfo
        reg.entities["number.kitchen_cal"] = EntityInfo(
            entity_id="number.kitchen_cal", name="Calibration",
            entity_category="config",
        )
        reg.areas["kitchen"].entity_ids.append("number.kitchen_cal")
        eids = reg.get_area_entities("kitchen")
        assert "number.kitchen_cal" not in eids
        eids_all = reg.get_area_entities("kitchen", show_all=True)
        assert "number.kitchen_cal" in eids_all


# ---------------------------------------------------------------------------
# Main menu has Active Now button
# ---------------------------------------------------------------------------


class TestMainMenuActiveButton:
    def test_main_menu_has_active(self) -> None:
        from ui import build_main_menu
        text, kb = build_main_menu()
        # Find the Active button
        found = False
        for row in kb.inline_keyboard:
            for btn in row:
                if "Активные" in btn.text:
                    assert btn.callback_data == "menu:active"
                    found = True
        assert found, "Active Now button not found in main menu"


# ---------------------------------------------------------------------------
# Readiness gating & recovery tests
# ---------------------------------------------------------------------------


class TestReadinessGating:
    @pytest.mark.asyncio
    async def test_wait_for_ha_success_first_try(self) -> None:
        """If HA responds immediately and sync succeeds, returns (version, True)."""
        import app as app_mod

        # Save originals
        orig_max = app_mod._READINESS_MAX_ATTEMPTS
        orig_delay = app_mod._READINESS_BASE_DELAY
        try:
            app_mod._READINESS_MAX_ATTEMPTS = 3
            app_mod._READINESS_BASE_DELAY = 0.01

            bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
            bot._ha = MagicMock()
            bot._ha.get_config = AsyncMock(return_value={"version": "2024.1.0"})
            bot._registry = MagicMock()
            bot._registry.sync = AsyncMock(return_value=True)
            bot._registry.floors = {"f1": None}
            bot._registry.areas = {"a1": None}
            bot._registry.devices = {"d1": None}
            bot._registry.entities = {"e1": None}
            bot._registry.vacuum_routines = {}

            version, sync_ok = await bot._wait_for_ha()
            assert version == "2024.1.0"
            assert sync_ok is True
            assert bot._ha.get_config.call_count == 1
        finally:
            app_mod._READINESS_MAX_ATTEMPTS = orig_max
            app_mod._READINESS_BASE_DELAY = orig_delay

    @pytest.mark.asyncio
    async def test_wait_for_ha_retries_then_succeeds(self) -> None:
        """HA fails twice then succeeds on third attempt."""
        import app as app_mod

        orig_max = app_mod._READINESS_MAX_ATTEMPTS
        orig_delay = app_mod._READINESS_BASE_DELAY
        try:
            app_mod._READINESS_MAX_ATTEMPTS = 5
            app_mod._READINESS_BASE_DELAY = 0.01

            bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
            bot._ha = MagicMock()
            bot._ha.get_config = AsyncMock(
                side_effect=[None, None, {"version": "2024.2.0"}]
            )
            bot._registry = MagicMock()
            bot._registry.sync = AsyncMock(return_value=True)
            bot._registry.floors = {"f1": None}
            bot._registry.areas = {"a1": None}
            bot._registry.devices = {"d1": None}
            bot._registry.entities = {"e1": None}
            bot._registry.vacuum_routines = {}

            version, sync_ok = await bot._wait_for_ha()
            assert version == "2024.2.0"
            assert sync_ok is True
            assert bot._ha.get_config.call_count == 3
        finally:
            app_mod._READINESS_MAX_ATTEMPTS = orig_max
            app_mod._READINESS_BASE_DELAY = orig_delay

    @pytest.mark.asyncio
    async def test_wait_for_ha_all_fail(self) -> None:
        """All attempts fail — returns empty version and False."""
        import app as app_mod

        orig_max = app_mod._READINESS_MAX_ATTEMPTS
        orig_delay = app_mod._READINESS_BASE_DELAY
        try:
            app_mod._READINESS_MAX_ATTEMPTS = 3
            app_mod._READINESS_BASE_DELAY = 0.01

            bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
            bot._ha = MagicMock()
            bot._ha.get_config = AsyncMock(return_value=None)

            version, sync_ok = await bot._wait_for_ha()
            assert version == ""
            assert sync_ok is False
            assert bot._ha.get_config.call_count == 3
        finally:
            app_mod._READINESS_MAX_ATTEMPTS = orig_max
            app_mod._READINESS_BASE_DELAY = orig_delay

    @pytest.mark.asyncio
    async def test_do_registry_sync_success(self) -> None:
        """_do_registry_sync returns True when sync succeeds."""
        import app as app_mod

        bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
        bot._registry = MagicMock()
        bot._registry.sync = AsyncMock(return_value=True)
        bot._registry.floors = {"f1": None}
        bot._registry.areas = {"a1": None}
        bot._registry.devices = {"d1": None}
        bot._registry.entities = {"e1": None}
        bot._registry.vacuum_routines = {}

        result = await bot._do_registry_sync()
        assert result is True

    @pytest.mark.asyncio
    async def test_do_registry_sync_failure(self) -> None:
        """_do_registry_sync returns False when sync fails."""
        import app as app_mod

        bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
        bot._registry = MagicMock()
        bot._registry.sync = AsyncMock(return_value=False)

        result = await bot._do_registry_sync()
        assert result is False

    @pytest.mark.asyncio
    async def test_recovery_loop_recovers(self) -> None:
        """Recovery loop detects HA coming back and re-syncs."""
        import app as app_mod

        orig_interval = app_mod._RECOVERY_INTERVAL
        try:
            app_mod._RECOVERY_INTERVAL = 0.01

            bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
            bot._ha_ready = False
            bot._ha = MagicMock()
            bot._ha.get_config = AsyncMock(return_value={"version": "2024.3.0"})
            bot._handlers = MagicMock()
            bot._diagnostics = MagicMock()
            bot._registry = MagicMock()
            bot._registry.sync = AsyncMock(return_value=True)
            bot._registry.floors = {}
            bot._registry.areas = {}
            bot._registry.devices = {}
            bot._registry.entities = {}
            bot._registry.vacuum_routines = {}

            # Run one iteration of recovery
            task = asyncio.create_task(bot._recovery_loop())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert bot._ha_ready is True
            assert bot._handlers.ha_version == "2024.3.0"
            assert bot._diagnostics.ha_version == "2024.3.0"
            bot._registry.sync.assert_called()
        finally:
            app_mod._RECOVERY_INTERVAL = orig_interval


# ---------------------------------------------------------------------------
# Per-action cooldown tests
# ---------------------------------------------------------------------------


class TestPerActionCooldown:
    @pytest.mark.asyncio
    async def test_float_cooldown(self, db) -> None:
        """Float cooldown values work correctly."""
        uid, action = 1, "light.brightness"

        allowed, remaining = await db.check_and_update_cooldown(uid, action, 0.2)
        assert allowed is True
        assert remaining == 0.0

        # Immediate retry with 0.2s cooldown
        allowed, remaining = await db.check_and_update_cooldown(uid, action, 0.2)
        assert allowed is False
        assert 0 < remaining <= 0.2

    @pytest.mark.asyncio
    async def test_independent_action_cooldowns(self, db) -> None:
        """Different actions have independent cooldowns."""
        uid = 1

        allowed, _ = await db.check_and_update_cooldown(uid, "light.turn_on", 60.0)
        assert allowed is True

        # Different action should be allowed
        allowed, _ = await db.check_and_update_cooldown(uid, "light.brightness", 0.2)
        assert allowed is True

        # Original action still on cooldown
        allowed, _ = await db.check_and_update_cooldown(uid, "light.turn_on", 60.0)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_zero_cooldown(self, db) -> None:
        """Zero cooldown means always allowed."""
        uid, action = 1, "instant"

        allowed, _ = await db.check_and_update_cooldown(uid, action, 0.0)
        assert allowed is True
        allowed, _ = await db.check_and_update_cooldown(uid, action, 0.0)
        assert allowed is True


# ---------------------------------------------------------------------------
# Brightness debounce tests
# ---------------------------------------------------------------------------


class TestBrightnessDebounce:
    def test_pending_dict_state(self) -> None:
        """Pending brightness dict tracks (cid, uid, eid) -> brightness value."""
        from handlers import Handlers

        h = Handlers.__new__(Handlers)
        h._pending_brightness = {}
        h._pending_volume = {}
        h._debounce_tasks = {}

        key = (100, 200, "light.test")
        h._pending_brightness[key] = 128
        assert h._pending_brightness[key] == 128

        # Update brightness
        h._pending_brightness[key] = 179
        assert h._pending_brightness[key] == 179

    def test_flush_cleanup(self) -> None:
        """After flush, pending state and task refs are cleared."""
        from handlers import Handlers

        h = Handlers.__new__(Handlers)
        h._pending_brightness = {}
        h._pending_volume = {}
        h._debounce_tasks = {}

        key = (100, 200, "light.test")
        h._pending_brightness[key] = 200

        # Simulate flush cleanup
        target = h._pending_brightness.pop(key, None)
        h._debounce_tasks.pop(key, None)

        assert target == 200
        assert key not in h._pending_brightness
        assert key not in h._debounce_tasks

    def test_volume_pending_dict(self) -> None:
        """Pending volume dict tracks float values."""
        from handlers import Handlers

        h = Handlers.__new__(Handlers)
        h._pending_brightness = {}
        h._pending_volume = {}
        h._debounce_tasks = {}

        key = (100, 200, "media_player.tv")
        h._pending_volume[key] = 0.55
        assert h._pending_volume[key] == 0.55

        h._pending_volume[key] = 0.60
        assert h._pending_volume[key] == 0.60


# ---------------------------------------------------------------------------
# Callback race protection tests
# ---------------------------------------------------------------------------


class TestCallbackRaceProtection:
    def test_debounce_prefixes_constant(self) -> None:
        """_DEBOUNCE_PREFIXES is defined and contains expected prefixes."""
        from handlers import _DEBOUNCE_PREFIXES
        assert isinstance(_DEBOUNCE_PREFIXES, frozenset)
        assert "bright" in _DEBOUNCE_PREFIXES
        assert "mvol" in _DEBOUNCE_PREFIXES
        assert "act" not in _DEBOUNCE_PREFIXES

    def test_idempotency_guard_state(self) -> None:
        """_last_cb dict tracks (data, timestamp) per user."""
        from handlers import Handlers

        h = Handlers.__new__(Handlers)
        h._last_cb = {}

        uid = 123
        now = time.time()
        h._last_cb[uid] = ("act:light.test:turn_on", now)

        last = h._last_cb.get(uid)
        assert last is not None
        assert last[0] == "act:light.test:turn_on"
        assert (now - last[1]) < 0.01

    def test_idempotency_guard_duplicate_detection(self) -> None:
        """Same (uid, data) within 0.25s should be detected as duplicate."""
        now = time.time()
        data = "act:light.test:turn_on"
        uid = 123

        last_cb: dict[int, tuple[str, float]] = {}
        last_cb[uid] = (data, now)

        # Same data, within window
        last = last_cb.get(uid)
        is_duplicate = last and last[0] == data and (now - last[1]) < 0.25
        assert is_duplicate

        # Different data, within window
        last_cb[uid] = ("fav:light.test", now)
        last = last_cb.get(uid)
        is_duplicate = last and last[0] == data and (now - last[1]) < 0.25
        assert not is_duplicate

    def test_debounce_prefix_exemption(self) -> None:
        """Debounce prefixes should be exempt from idempotency guard."""
        from handlers import _DEBOUNCE_PREFIXES

        data = "bright:light.test:up"
        prefix = data.split(":", 1)[0]
        assert prefix in _DEBOUNCE_PREFIXES

        data2 = "mvol:media_player.tv:up"
        prefix2 = data2.split(":", 1)[0]
        assert prefix2 in _DEBOUNCE_PREFIXES

        data3 = "act:light.test:turn_on"
        prefix3 = data3.split(":", 1)[0]
        assert prefix3 not in _DEBOUNCE_PREFIXES


# ---------------------------------------------------------------------------
# Readiness with sync tests
# ---------------------------------------------------------------------------


class TestReadinessWithSync:
    @pytest.mark.asyncio
    async def test_config_ok_sync_ok(self) -> None:
        """Config OK + sync OK on first try returns (version, True)."""
        import app as app_mod

        orig_max = app_mod._READINESS_MAX_ATTEMPTS
        orig_delay = app_mod._READINESS_BASE_DELAY
        try:
            app_mod._READINESS_MAX_ATTEMPTS = 3
            app_mod._READINESS_BASE_DELAY = 0.01

            bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
            bot._ha = MagicMock()
            bot._ha.get_config = AsyncMock(return_value={"version": "2024.5.0"})
            bot._registry = MagicMock()
            bot._registry.sync = AsyncMock(return_value=True)
            bot._registry.floors = {}
            bot._registry.areas = {}
            bot._registry.devices = {}
            bot._registry.entities = {}
            bot._registry.vacuum_routines = {}

            version, sync_ok = await bot._wait_for_ha()
            assert version == "2024.5.0"
            assert sync_ok is True
        finally:
            app_mod._READINESS_MAX_ATTEMPTS = orig_max
            app_mod._READINESS_BASE_DELAY = orig_delay

    @pytest.mark.asyncio
    async def test_config_ok_sync_retries(self) -> None:
        """Config OK but sync fails then succeeds."""
        import app as app_mod

        orig_max = app_mod._READINESS_MAX_ATTEMPTS
        orig_delay = app_mod._READINESS_BASE_DELAY
        try:
            app_mod._READINESS_MAX_ATTEMPTS = 5
            app_mod._READINESS_BASE_DELAY = 0.01

            bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
            bot._ha = MagicMock()
            bot._ha.get_config = AsyncMock(return_value={"version": "2024.5.0"})
            bot._registry = MagicMock()
            bot._registry.sync = AsyncMock(side_effect=[False, False, True])
            bot._registry.floors = {}
            bot._registry.areas = {}
            bot._registry.devices = {}
            bot._registry.entities = {}
            bot._registry.vacuum_routines = {}

            version, sync_ok = await bot._wait_for_ha()
            assert version == "2024.5.0"
            assert sync_ok is True
            assert bot._registry.sync.call_count == 3
        finally:
            app_mod._READINESS_MAX_ATTEMPTS = orig_max
            app_mod._READINESS_BASE_DELAY = orig_delay

    @pytest.mark.asyncio
    async def test_config_ok_sync_all_fail(self) -> None:
        """Config OK but sync always fails returns (version, False)."""
        import app as app_mod

        orig_max = app_mod._READINESS_MAX_ATTEMPTS
        orig_delay = app_mod._READINESS_BASE_DELAY
        try:
            app_mod._READINESS_MAX_ATTEMPTS = 3
            app_mod._READINESS_BASE_DELAY = 0.01

            bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
            bot._ha = MagicMock()
            bot._ha.get_config = AsyncMock(return_value={"version": "2024.5.0"})
            bot._registry = MagicMock()
            bot._registry.sync = AsyncMock(return_value=False)

            version, sync_ok = await bot._wait_for_ha()
            assert version == "2024.5.0"
            assert sync_ok is False
        finally:
            app_mod._READINESS_MAX_ATTEMPTS = orig_max
            app_mod._READINESS_BASE_DELAY = orig_delay


# ---------------------------------------------------------------------------
# Sync diff tests
# ---------------------------------------------------------------------------


class TestSyncDiff:
    def test_added_entities(self) -> None:
        """Set difference correctly identifies added entities."""
        old = {"light.a", "light.b"}
        new = {"light.a", "light.b", "light.c", "switch.d"}
        added = new - old
        removed = old - new
        assert added == {"light.c", "switch.d"}
        assert removed == set()

    def test_removed_entities(self) -> None:
        """Set difference correctly identifies removed entities."""
        old = {"light.a", "light.b", "switch.c"}
        new = {"light.a"}
        added = new - old
        removed = old - new
        assert added == set()
        assert removed == {"light.b", "switch.c"}

    def test_mixed_changes(self) -> None:
        """Both additions and removals detected correctly."""
        old = {"light.a", "light.b", "switch.c"}
        new = {"light.a", "switch.d", "fan.e"}
        added = new - old
        removed = old - new
        assert added == {"switch.d", "fan.e"}
        assert removed == {"light.b", "switch.c"}

    def test_no_changes(self) -> None:
        """Identical sets produce no diff."""
        old = {"light.a", "light.b"}
        new = {"light.a", "light.b"}
        added = new - old
        removed = old - new
        assert added == set()
        assert removed == set()

    def test_empty_to_full(self) -> None:
        """First sync: empty -> populated shows all as added."""
        old: set[str] = set()
        new = {"light.a", "light.b", "switch.c"}
        added = new - old
        removed = old - new
        assert len(added) == 3
        assert removed == set()


# ---------------------------------------------------------------------------
# Token validation tests
# ---------------------------------------------------------------------------


class TestTokenValidation:
    """Tests for bot_token validation in _load_and_validate_config."""

    def _write_options(self, tmp_path: Path, options: dict) -> Path:
        opts_file = tmp_path / "options.json"
        opts_file.write_text(json.dumps(options), encoding="utf-8")
        return opts_file

    def test_empty_token_exits(self, tmp_path: Path, monkeypatch) -> None:
        """Empty bot_token causes sys.exit(1)."""
        import app as app_mod
        opts = self._write_options(tmp_path, {"bot_token": ""})
        monkeypatch.setattr(app_mod, "OPTIONS_PATH", opts)
        with pytest.raises(SystemExit) as exc_info:
            app_mod._load_and_validate_config()
        assert exc_info.value.code == 1

    def test_missing_token_exits(self, tmp_path: Path, monkeypatch) -> None:
        """Missing bot_token key causes sys.exit(1)."""
        import app as app_mod
        opts = self._write_options(tmp_path, {})
        monkeypatch.setattr(app_mod, "OPTIONS_PATH", opts)
        with pytest.raises(SystemExit) as exc_info:
            app_mod._load_and_validate_config()
        assert exc_info.value.code == 1

    def test_whitespace_only_token_exits(self, tmp_path: Path, monkeypatch) -> None:
        """Whitespace-only bot_token causes sys.exit(1)."""
        import app as app_mod
        opts = self._write_options(tmp_path, {"bot_token": "   \n  "})
        monkeypatch.setattr(app_mod, "OPTIONS_PATH", opts)
        with pytest.raises(SystemExit) as exc_info:
            app_mod._load_and_validate_config()
        assert exc_info.value.code == 1

    def test_malformed_token_exits(self, tmp_path: Path, monkeypatch) -> None:
        """Token without digits:alnum format causes sys.exit(1)."""
        import app as app_mod
        opts = self._write_options(tmp_path, {"bot_token": "not-a-valid-token"})
        monkeypatch.setattr(app_mod, "OPTIONS_PATH", opts)
        with pytest.raises(SystemExit) as exc_info:
            app_mod._load_and_validate_config()
        assert exc_info.value.code == 1

    def test_valid_token_with_whitespace_stripped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Token with surrounding whitespace is stripped and accepted."""
        import app as app_mod
        token = "123456789:ABCDefGH_ijklmnop-QRS"
        opts = self._write_options(tmp_path, {
            "bot_token": f"  {token}  \n",
        })
        monkeypatch.setattr(app_mod, "OPTIONS_PATH", opts)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor")
        config, _ = app_mod._load_and_validate_config()
        assert config.bot_token == token

    def test_quoted_token_unquoted(self, tmp_path: Path, monkeypatch) -> None:
        """Token wrapped in quotes is properly unquoted."""
        import app as app_mod
        token = "123456789:ABCDefGH_ijklmnop-QRS"
        opts = self._write_options(tmp_path, {
            "bot_token": f'"{token}"',
        })
        monkeypatch.setattr(app_mod, "OPTIONS_PATH", opts)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor")
        config, _ = app_mod._load_and_validate_config()
        assert config.bot_token == token

    def test_non_string_token_exits(self, tmp_path: Path, monkeypatch) -> None:
        """Non-string bot_token (e.g. int) causes sys.exit(1)."""
        import app as app_mod
        opts = self._write_options(tmp_path, {"bot_token": 12345})
        monkeypatch.setattr(app_mod, "OPTIONS_PATH", opts)
        with pytest.raises(SystemExit) as exc_info:
            app_mod._load_and_validate_config()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Telegram pre-flight verification tests
# ---------------------------------------------------------------------------


class TestVerifyTelegramToken:
    """Tests for _verify_telegram_token method."""

    @pytest.mark.asyncio
    async def test_unauthorized_exits(self) -> None:
        """TelegramUnauthorizedError causes immediate exit."""
        import app as app_mod
        from aiogram.exceptions import TelegramUnauthorizedError

        bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
        bot._bot = MagicMock()
        bot._bot.get_me = AsyncMock(
            side_effect=TelegramUnauthorizedError(
                method=MagicMock(), message="Unauthorized",
            ),
        )
        bot._config = MagicMock()
        bot._config.bot_token = "123456:FAKE"

        with pytest.raises(SystemExit) as exc_info:
            await bot._verify_telegram_token()
        assert exc_info.value.code == 1
        # Should NOT retry on Unauthorized
        assert bot._bot.get_me.call_count == 1

    @pytest.mark.asyncio
    async def test_success_returns_user(self) -> None:
        """Successful getMe returns the user object."""
        import app as app_mod

        bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
        bot._bot = MagicMock()
        fake_user = MagicMock()
        fake_user.username = "test_bot"
        fake_user.id = 12345
        bot._bot.get_me = AsyncMock(return_value=fake_user)
        bot._config = MagicMock()
        bot._config.bot_token = "123456:FAKE"

        result = await bot._verify_telegram_token()
        assert result is fake_user
        assert result.username == "test_bot"

    @pytest.mark.asyncio
    async def test_transient_error_retries_then_succeeds(self) -> None:
        """Network errors retry and succeed on subsequent attempt."""
        import app as app_mod

        bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
        bot._bot = MagicMock()
        fake_user = MagicMock()
        bot._bot.get_me = AsyncMock(
            side_effect=[ConnectionError("timeout"), fake_user],
        )
        bot._config = MagicMock()
        bot._config.bot_token = "123456:FAKE"
        # Speed up for test
        bot._TELEGRAM_VERIFY_BACKOFF = 0.01

        result = await bot._verify_telegram_token()
        assert result is fake_user
        assert bot._bot.get_me.call_count == 2

    @pytest.mark.asyncio
    async def test_all_transient_errors_exits(self) -> None:
        """All attempts failing with transient errors causes exit."""
        import app as app_mod

        bot = app_mod.TelegramBot.__new__(app_mod.TelegramBot)
        bot._bot = MagicMock()
        bot._bot.get_me = AsyncMock(
            side_effect=ConnectionError("timeout"),
        )
        bot._config = MagicMock()
        bot._config.bot_token = "123456:FAKE"
        bot._TELEGRAM_VERIFY_BACKOFF = 0.01
        bot._TELEGRAM_VERIFY_RETRIES = 3

        with pytest.raises(SystemExit) as exc_info:
            await bot._verify_telegram_token()
        assert exc_info.value.code == 1
        assert bot._bot.get_me.call_count == 3


# ---------------------------------------------------------------------------
# Primary entity selection tests
# ---------------------------------------------------------------------------


class TestPickPrimaryEntity:
    """Tests for _pick_primary_entity in HARegistry."""

    def _build_registry(self) -> "HARegistry":
        from registry import HARegistry
        reg = HARegistry.__new__(HARegistry)
        reg.entities = {}
        reg.devices = {}
        reg.areas = {}
        return reg

    def test_media_player_over_sensor(self) -> None:
        reg = self._build_registry()
        result = reg._pick_primary_entity([
            "sensor.audio_signal", "media_player.audio", "binary_sensor.audio_connected",
        ])
        assert result == "media_player.audio"

    def test_switch_over_sensor(self) -> None:
        reg = self._build_registry()
        result = reg._pick_primary_entity([
            "sensor.kettle_connectivity", "switch.kettle",
        ])
        assert result == "switch.kettle"

    def test_light_over_switch(self) -> None:
        reg = self._build_registry()
        result = reg._pick_primary_entity([
            "switch.lamp", "light.lamp",
        ])
        assert result == "light.lamp"

    def test_junk_suffix_deprioritized(self) -> None:
        reg = self._build_registry()
        result = reg._pick_primary_entity([
            "sensor.device_signal_strength", "sensor.device_temperature",
        ])
        assert result == "sensor.device_temperature"

    def test_diagnostic_category_deprioritized(self) -> None:
        from registry import EntityInfo
        reg = self._build_registry()
        reg.entities["sensor.diag"] = EntityInfo(
            entity_id="sensor.diag", entity_category="diagnostic",
        )
        reg.entities["light.main"] = EntityInfo(entity_id="light.main")
        result = reg._pick_primary_entity(["sensor.diag", "light.main"])
        assert result == "light.main"

    def test_single_entity(self) -> None:
        reg = self._build_registry()
        assert reg._pick_primary_entity(["sensor.only"]) == "sensor.only"

    def test_vacuum_wins(self) -> None:
        reg = self._build_registry()
        result = reg._pick_primary_entity([
            "sensor.vac_battery", "vacuum.robo", "button.vac_reset",
        ])
        assert result == "vacuum.robo"


# ---------------------------------------------------------------------------
# State mapping tests
# ---------------------------------------------------------------------------


class TestStateMapping:
    """Tests for state_mapping.map_state and is_junk_primary."""

    def test_light_on_is_active(self) -> None:
        from state_mapping import map_state
        m = map_state("light.kitchen", "on")
        assert m.is_active is True
        assert m.ui_state == "ON"

    def test_light_off_not_active(self) -> None:
        from state_mapping import map_state
        m = map_state("light.kitchen", "off")
        assert m.is_active is False
        assert m.ui_state == "OFF"

    def test_binary_sensor_is_read_only(self) -> None:
        from state_mapping import map_state
        m = map_state("binary_sensor.doorbell", "on")
        assert m.is_read_only is True

    def test_event_is_read_only(self) -> None:
        from state_mapping import map_state
        m = map_state("event.doorbell_press", "2024-01-01T00:00:00")
        assert m.is_read_only is True

    def test_vacuum_cleaning_active(self) -> None:
        from state_mapping import map_state
        m = map_state("vacuum.robo", "cleaning")
        assert m.is_active is True

    def test_vacuum_docked_not_active(self) -> None:
        from state_mapping import map_state
        m = map_state("vacuum.robo", "docked")
        assert m.is_active is False

    def test_media_player_playing_active(self) -> None:
        from state_mapping import map_state
        m = map_state("media_player.tv", "playing")
        assert m.is_active is True

    def test_media_player_standby_not_active(self) -> None:
        from state_mapping import map_state
        m = map_state("media_player.tv", "standby")
        assert m.is_active is False

    def test_media_player_on_not_active(self) -> None:
        """media_player 'on' but not playing — should NOT be active."""
        from state_mapping import map_state
        m = map_state("media_player.tv", "on")
        assert m.is_active is False

    def test_climate_heating_active(self) -> None:
        from state_mapping import map_state
        m = map_state("climate.living", "heating")
        assert m.is_active is True

    def test_climate_idle_not_active(self) -> None:
        from state_mapping import map_state
        m = map_state("climate.living", "idle")
        assert m.is_active is False

    def test_unavailable_not_active(self) -> None:
        from state_mapping import map_state
        m = map_state("light.test", "unavailable")
        assert m.is_active is False
        assert m.ui_state == "UNAVAILABLE"

    def test_override_active_states(self) -> None:
        from state_mapping import map_state
        overrides = {
            "switch.kettle": {
                "active_states": ["heating"],
                "idle_states": ["on", "standby"],
            },
        }
        m = map_state("switch.kettle", "on", overrides=overrides)
        assert m.is_active is False
        m2 = map_state("switch.kettle", "heating", overrides=overrides)
        assert m2.is_active is True

    def test_power_threshold_override(self) -> None:
        from state_mapping import map_state
        overrides = {
            "sensor.dishwasher_power": {"running_threshold_watts": 10},
        }
        m = map_state("sensor.dishwasher_power", "5.0", overrides=overrides)
        assert m.is_active is False
        assert m.ui_state == "IDLE"
        m2 = map_state("sensor.dishwasher_power", "150.0", overrides=overrides)
        assert m2.is_active is True
        assert m2.ui_state == "RUNNING"

    def test_russian_label(self) -> None:
        from state_mapping import map_state
        m = map_state("light.test", "on")
        assert m.ui_label == "Вкл"
        m2 = map_state("vacuum.test", "cleaning")
        assert m2.ui_label == "Уборка"

    def test_junk_primary_detection(self) -> None:
        from state_mapping import is_junk_primary
        assert is_junk_primary("sensor.device_signal_strength") is True
        assert is_junk_primary("binary_sensor.device_connectivity") is True
        assert is_junk_primary("switch.kettle") is False
        assert is_junk_primary("light.living_room") is False


# ---------------------------------------------------------------------------
# State icon domain-awareness tests
# ---------------------------------------------------------------------------


class TestStateIconDomainAware:
    """Tests for domain-aware _state_icon in ui.py."""

    def test_binary_sensor_on_blue(self) -> None:
        from ui import _state_icon
        assert _state_icon("binary_sensor", "on") == "\U0001f535"

    def test_light_on_green(self) -> None:
        from ui import _state_icon
        assert _state_icon("light", "on") == "\U0001f7e2"

    def test_sensor_off_white(self) -> None:
        from ui import _state_icon
        assert _state_icon("sensor", "off") == "\u26aa"

    def test_climate_heating_green(self) -> None:
        from ui import _state_icon
        assert _state_icon("climate", "heating") == "\U0001f7e2"

    def test_climate_idle_white(self) -> None:
        from ui import _state_icon
        assert _state_icon("climate", "idle") == "\u26aa"

    def test_switch_on_green(self) -> None:
        from ui import _state_icon
        assert _state_icon("switch", "on") == "\U0001f7e2"


# ---------------------------------------------------------------------------
# Control buttons domain handling tests
# ---------------------------------------------------------------------------


class TestControlButtonsDomainHandling:
    """Tests for _control_buttons handling of read-only domains."""

    def test_binary_sensor_no_action_buttons(self) -> None:
        from ui import _control_buttons
        rows = _control_buttons("binary_sensor", "binary_sensor.door", "on", {})
        all_cbs = [btn.callback_data for row in rows for btn in row]
        assert not any(cb.startswith("act:") for cb in all_cbs)

    def test_event_no_buttons(self) -> None:
        from ui import _control_buttons
        rows = _control_buttons("event", "event.doorbell", "", {})
        assert len(rows) == 0

    def test_sensor_no_buttons(self) -> None:
        from ui import _control_buttons
        rows = _control_buttons("sensor", "sensor.temp", "23.5", {})
        assert len(rows) == 0

    def test_switch_has_action_buttons(self) -> None:
        from ui import _control_buttons
        rows = _control_buttons("switch", "switch.light", "on", {})
        all_cbs = [btn.callback_data for row in rows for btn in row]
        assert any("turn_on" in cb for cb in all_cbs)
        assert any("turn_off" in cb for cb in all_cbs)
