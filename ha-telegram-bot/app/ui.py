"""Inline keyboard UI builders for multi-level menus.

All builders return (text, InlineKeyboardMarkup) tuples.
Callback data format: "prefix:payload" (max 64 bytes per Telegram limit).

Menu tree:
  Main -> Devices -> (Floors?) -> Areas -> Entities -> Controls
  Main -> Scenarios -> Scenes/Scripts/Automations -> Controls
  Main -> Active -> Entity -> Controls
  Main -> Radio -> Station -> Output device -> Controls
  Main -> Global Light Scenes -> Color picker
  Main -> Status / Update-SYNC
"""

from __future__ import annotations

import math
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from state_mapping import map_state

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOMAIN_ICONS: dict[str, str] = {
    "light": "\U0001f4a1",
    "switch": "\U0001f50c",
    "vacuum": "\U0001f9f9",
    "scene": "\U0001f3ac",
    "script": "\u25b6\ufe0f",
    "climate": "\U0001f321\ufe0f",
    "fan": "\U0001f32c\ufe0f",
    "cover": "\U0001f6aa",
    "sensor": "\U0001f4ca",
    "binary_sensor": "\U0001f534",
    "automation": "\u2699\ufe0f",
    "input_boolean": "\U0001f518",
    "media_player": "\U0001f4fa",
    "camera": "\U0001f4f7",
    "lock": "\U0001f512",
    "number": "\U0001f522",
    "select": "\U0001f4cb",
    "button": "\U0001f518",
    "water_heater": "\U0001f6bf",
}

DOMAIN_LABELS: dict[str, str] = {
    "light": "Свет",
    "switch": "Выключатели",
    "vacuum": "Пылесосы",
    "scene": "Сцены",
    "script": "Скрипты",
    "climate": "Климат",
    "fan": "Вентиляторы",
    "cover": "Шторы/Ворота",
    "sensor": "Датчики",
    "binary_sensor": "Бин. датчики",
    "lock": "Замки",
    "media_player": "Медиа",
    "button": "Кнопки",
    "select": "Выбор",
    "number": "Числа",
    "water_heater": "Водонагреватели",
}

# Color presets: (label, rgb_color)
COLOR_PRESETS: list[tuple[str, tuple[int, int, int]]] = [
    ("\U0001f534 Красный", (255, 0, 0)),
    ("\U0001f7e0 Оранжевый", (255, 165, 0)),
    ("\U0001f7e1 Жёлтый", (255, 255, 0)),
    ("\U0001f7e2 Зелёный", (0, 255, 0)),
    ("\U0001f535 Синий", (0, 0, 255)),
    ("\U0001f7e3 Фиолетовый", (128, 0, 255)),
    ("\u26aa Тёплый белый", (255, 200, 120)),
]


def _sanitize(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _state_icon(domain: str, state: str) -> str:
    # Read-only sensors: blue for "on" (informational), not green
    if domain in ("binary_sensor", "event", "sensor"):
        if state in ("unavailable", "unknown"):
            return "\U0001f534"
        if state == "on":
            return "\U0001f535"
        return "\u26aa"
    # Climate: active modes are green, idle/off is white
    if domain == "climate":
        if state in ("heating", "cooling", "drying", "heat", "cool",
                      "heat_cool", "auto"):
            return "\U0001f7e2"
        if state in ("off", "idle"):
            return "\u26aa"
        if state in ("unavailable", "unknown"):
            return "\U0001f534"
        return "\U0001f535"
    # Default (light, switch, vacuum, media_player, etc.)
    if state in ("on", "open", "cleaning", "playing", "home"):
        return "\U0001f7e2"
    if state in ("off", "closed", "docked", "idle", "paused", "standby"):
        return "\u26aa"
    if state in ("unavailable", "unknown"):
        return "\U0001f534"
    return "\U0001f535"


def _trunc(text: str, max_len: int = 28) -> str:
    return text[:max_len] if len(text) > max_len else text


def _home_btn() -> InlineKeyboardButton:
    """Home button to return to main menu."""
    return InlineKeyboardButton(text="\U0001f3e0 Меню", callback_data="nav:main")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------


def build_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "\U0001f3e0 <b>Home Assistant Bot</b>\n\n"
        "Выберите действие:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="\U0001f3db Устройства", callback_data="menu:devices"),
            InlineKeyboardButton(text="\U0001f3ac Сценарии", callback_data="menu:scenarios"),
        ],
        [
            InlineKeyboardButton(text="\U0001f7e2 Активные", callback_data="menu:active"),
            InlineKeyboardButton(text="\U0001f4fb Радио \U0001f1f7\U0001f1fa", callback_data="menu:radio"),
        ],
        [
            InlineKeyboardButton(text="\U0001f3a8 Цвет света", callback_data="menu:gcolor"),
            InlineKeyboardButton(text="\u2b50 Избранное", callback_data="menu:favorites"),
        ],
        [
            InlineKeyboardButton(text="\U0001f514 Уведомления", callback_data="menu:notif"),
            InlineKeyboardButton(text="\U0001f916 Автоматизации", callback_data="menu:automations"),
        ],
        [
            InlineKeyboardButton(text="\U0001f4cb Списки дел", callback_data="menu:todo"),
            InlineKeyboardButton(text="\u2139\ufe0f Статус", callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton(text="\U0001f504 SYNC", callback_data="menu:refresh"),
        ],
    ])
    return text, kb


# Active states that indicate the entity is "on" / working
_ACTIVE_STATES: frozenset[str] = frozenset({
    "on", "open", "cleaning", "playing", "home", "unlocked",
    "heating", "cooling", "drying", "returning",
})


def build_active_now_menu(
    entities: list[dict[str, Any]],
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show entities that are currently in an active state."""
    if not entities:
        text = "\U0001f7e2 <b>Активные сейчас</b>\n\nВсё выключено."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f504 Обновить", callback_data="menu:active")],
            [_home_btn()],
        ])
        return text, kb

    return _build_active_entity_list(
        entities, page, page_size,
        title=f"\U0001f7e2 <b>Активные сейчас</b> ({len(entities)})",
        back_cb="nav:main",
        page_cb_prefix="actp",
    )


def _build_active_entity_list(
    entities: list[dict[str, Any]],
    page: int,
    page_size: int,
    title: str,
    back_cb: str,
    page_cb_prefix: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Entity list for Active menu — uses dev: callback for device-grouped navigation."""
    total = len(entities)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_ents = entities[start:end]

    text = f"{title} (стр. {page + 1}/{total_pages})"
    rows: list[list[InlineKeyboardButton]] = []

    for ent in page_ents:
        eid = ent["entity_id"]
        name = ent.get("friendly_name", eid)
        state = ent.get("state", "")
        domain = eid.split(".", 1)[0]
        si = _state_icon(domain, state)
        icon = DOMAIN_ICONS.get(domain, "")
        # Use ent: callback to open entity detail (not close)
        rows.append([InlineKeyboardButton(
            text=f"{si}{icon} {_trunc(name)}",
            callback_data=f"ent:{eid}",
        )])

    # Pagination
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="\u25c0", callback_data=f"{page_cb_prefix}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="\u25b6", callback_data=f"{page_cb_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="\U0001f504 Обновить", callback_data="menu:active"),
    ])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------


def build_floors_menu(
    floors: list[dict[str, Any]],
    unassigned_count: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """floors: [{"floor_id", "name", "area_count"}, ...]"""
    text = "\U0001f3db <b>Устройства</b>\n\nВыберите этаж:"
    rows: list[list[InlineKeyboardButton]] = []
    for fl in floors:
        label = f"{fl['name']} ({fl['area_count']})"
        rows.append([InlineKeyboardButton(
            text=label, callback_data=f"fl:{fl['floor_id']}",
        )])
    if unassigned_count > 0:
        rows.append([InlineKeyboardButton(
            text=f"\U0001f4e6 Без комнаты ({unassigned_count})",
            callback_data="fl:__none__",
        )])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Areas (rooms)
# ---------------------------------------------------------------------------


def build_areas_menu(
    areas: list[dict[str, Any]],
    back_target: str = "nav:main",
    title: str = "\U0001f3e0 <b>Комнаты</b>",
    unassigned_entity_count: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """areas: [{"area_id", "name", "entity_count"}, ...]"""
    text = f"{title}\n\nВыберите комнату:"
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(areas), 2):
        row: list[InlineKeyboardButton] = []
        for a in areas[i:i + 2]:
            label = f"{_trunc(a['name'], 20)} ({a['entity_count']})"
            row.append(InlineKeyboardButton(
                text=label, callback_data=f"ar:{a['area_id']}",
            ))
        rows.append(row)
    if unassigned_entity_count > 0:
        rows.append([InlineKeyboardButton(
            text=f"\U0001f4e6 Без комнаты ({unassigned_entity_count})",
            callback_data="ar:__none__",
        )])
    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=back_target)])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Entity list (inside area or domain or favorites)
# ---------------------------------------------------------------------------


def build_entity_list(
    entities: list[dict[str, Any]],
    page: int,
    page_size: int,
    title: str,
    back_cb: str,
    page_cb_prefix: str = "pg",
    show_fav_btn: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """entities: [{"entity_id", "friendly_name", "state", "domain"}, ...]"""
    total = len(entities)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_ents = entities[start:end]

    text = f"{title} (стр. {page + 1}/{total_pages})"
    rows: list[list[InlineKeyboardButton]] = []

    for ent in page_ents:
        eid = ent["entity_id"]
        name = ent.get("friendly_name", eid)
        state = ent.get("state", "")
        domain = eid.split(".", 1)[0]
        si = _state_icon(domain, state)
        icon = DOMAIN_ICONS.get(domain, "")
        rows.append([InlineKeyboardButton(
            text=f"{si}{icon} {_trunc(name)}",
            callback_data=f"ent:{eid}",
        )])

    # Pagination
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="\u25c0", callback_data=f"{page_cb_prefix}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="\u25b6", callback_data=f"{page_cb_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=back_cb)])
    if back_cb != "nav:main":
        rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Device list (inside area — groups entities by device)
# ---------------------------------------------------------------------------


def build_device_list(
    devices: list[dict[str, Any]],
    page: int,
    page_size: int,
    title: str,
    back_cb: str,
    page_cb_prefix: str = "arp",
    pin_btn: InlineKeyboardButton | None = None,
    scenes: list[dict[str, Any]] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """devices: [{"device_id", "name", "entity_ids", "primary_entity_id", "primary_domain", "is_vacuum"}, ...]
    scenes: [{"entity_id", "friendly_name"}, ...] — quick scenes for this area
    """
    total = len(devices)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_devs = devices[start:end]

    text = f"{title} (стр. {page + 1}/{total_pages})"
    rows: list[list[InlineKeyboardButton]] = []

    # Quick scenes section (before devices, only on first page)
    if scenes and page == 0:
        for sc in scenes[:4]:
            sc_eid = sc["entity_id"]
            sc_name = sc.get("friendly_name", sc_eid)
            sc_domain = sc_eid.split(".", 1)[0]
            sc_icon = DOMAIN_ICONS.get(sc_domain, "\U0001f3ac")
            cb = f"qsc:{sc_eid}"
            if len(cb) <= 64:
                rows.append([InlineKeyboardButton(
                    text=f"{sc_icon} {_trunc(sc_name, 25)}",
                    callback_data=cb,
                )])

    for dev in page_devs:
        did = dev["device_id"]
        name = dev.get("name", did)
        domain = dev.get("primary_domain", "unknown")
        icon = DOMAIN_ICONS.get(domain, "\U0001f4e6")
        count = len(dev.get("entity_ids", []))
        suffix = f" ({count})" if count > 1 else ""
        cb = f"dev:{did}"
        if len(cb) > 64:
            cb = f"ent:{dev['primary_entity_id']}"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {_trunc(name, 25)}{suffix}",
            callback_data=cb,
        )])

    # Pagination
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="\u25c0", callback_data=f"{page_cb_prefix}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="\u25b6", callback_data=f"{page_cb_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)

    if pin_btn:
        rows.append([pin_btn])

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=back_cb)])
    if back_cb != "nav:main":
        rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Entity control
# ---------------------------------------------------------------------------


def build_entity_control(
    entity_id: str,
    state_data: dict[str, Any],
    is_fav: bool = False,
    is_notif: bool = False,
    back_cb: str = "nav:main",
) -> tuple[str, InlineKeyboardMarkup]:
    domain = entity_id.split(".", 1)[0]
    attrs = state_data.get("attributes", {})
    name = attrs.get("friendly_name", entity_id)
    raw_state = state_data.get("state", "unknown")
    icon = DOMAIN_ICONS.get(domain, "\U0001f4e6")

    mapped = map_state(entity_id, raw_state, attrs)
    state_label = mapped.ui_label if mapped.ui_label != raw_state else raw_state
    text = f"{icon} <b>{_sanitize(name)}</b>\n\nСостояние: <code>{_sanitize(state_label)}</code>"
    state = raw_state  # keep raw for _control_buttons

    extra: list[str] = []
    if domain == "climate":
        ct = attrs.get("current_temperature")
        tt = attrs.get("temperature")
        if ct is not None:
            extra.append(f"\U0001f321 Текущая: {ct}\u00b0")
        if tt is not None:
            extra.append(f"\U0001f3af Целевая: {tt}\u00b0")
    elif domain == "light":
        br = attrs.get("brightness")
        if br is not None:
            extra.append(f"\U0001f506 Яркость: {round(br / 255 * 100)}%")
        ct = attrs.get("color_temp")
        if ct is not None:
            extra.append(f"\U0001f321 Цвет. темп.: {ct}")
    elif domain == "vacuum":
        bat = attrs.get("battery_level")
        if bat is not None:
            extra.append(f"\U0001f50b Батарея: {bat}%")
        fs = attrs.get("fan_speed")
        if fs is not None:
            extra.append(f"\U0001f32c Мощность: {fs}")
    elif domain == "cover":
        pos = attrs.get("current_position")
        if pos is not None:
            extra.append(f"\U0001f4cf Позиция: {pos}%")
    elif domain == "media_player":
        mt = attrs.get("media_title")
        if mt:
            extra.append(f"\U0001f3b5 Трек: {_sanitize(str(mt)[:60])}")
        src = attrs.get("source")
        if src:
            extra.append(f"\U0001f4fb Источник: {_sanitize(str(src)[:40])}")
        vol = attrs.get("volume_level")
        if vol is not None:
            extra.append(f"\U0001f50a Громкость: {round(float(vol) * 100)}%")
        muted = attrs.get("is_volume_muted")
        if muted:
            extra.append("\U0001f507 Звук выключен")
    elif domain == "sensor":
        unit = attrs.get("unit_of_measurement", "")
        device_class = attrs.get("device_class", "")
        if device_class:
            extra.append(f"Тип: {_sanitize(str(device_class))}")
        if unit:
            extra.append(f"Ед. изм.: {_sanitize(str(unit))}")
    elif domain == "select":
        options = attrs.get("options", [])
        if options:
            extra.append(f"Варианты: {len(options)}")
    elif domain == "number":
        mn = attrs.get("min")
        mx = attrs.get("max")
        step = attrs.get("step")
        if mn is not None and mx is not None:
            extra.append(f"Диапазон: {mn} — {mx}")
        if step is not None:
            extra.append(f"Шаг: {step}")
    elif domain == "water_heater":
        ct = attrs.get("current_temperature")
        tt = attrs.get("temperature")
        if ct is not None:
            extra.append(f"\U0001f321 Текущая: {ct}\u00b0")
        if tt is not None:
            extra.append(f"\U0001f3af Целевая: {tt}\u00b0")
    elif domain == "binary_sensor":
        device_class = attrs.get("device_class", "")
        if device_class:
            extra.append(f"Тип: {_sanitize(str(device_class))}")
        extra.append("(только чтение)")
    elif domain == "event":
        event_types = attrs.get("event_types", [])
        if event_types:
            extra.append(f"События: {', '.join(str(e) for e in event_types[:5])}")
        last = attrs.get("event_type")
        if last:
            extra.append(f"Последнее: {_sanitize(str(last))}")
        extra.append("(только чтение)")

    if extra:
        text += "\n" + "\n".join(extra)

    rows = _control_buttons(domain, entity_id, state, attrs)

    # Star / Bell row
    util_row: list[InlineKeyboardButton] = []
    fav_label = "\u2b50 Убрать" if is_fav else "\u2b50 В избранное"
    util_row.append(InlineKeyboardButton(text=fav_label, callback_data=f"fav:{entity_id}"))
    notif_label = "\U0001f515 Отписаться" if is_notif else "\U0001f514 Подписаться"
    util_row.append(InlineKeyboardButton(text=notif_label, callback_data=f"ntog:{entity_id}"))
    rows.append(util_row)

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=back_cb)])
    if back_cb != "nav:main":
        rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _control_buttons(
    domain: str, entity_id: str, state: str, attrs: dict[str, Any],
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []

    if domain in ("light", "switch", "input_boolean", "fan"):
        rows.append([
            InlineKeyboardButton(text="\U0001f7e2 ON", callback_data=f"act:{entity_id}:turn_on"),
            InlineKeyboardButton(text="\u26aa OFF", callback_data=f"act:{entity_id}:turn_off"),
            InlineKeyboardButton(text="\U0001f504 Toggle", callback_data=f"act:{entity_id}:toggle"),
        ])
        if domain == "light":
            supported = attrs.get("supported_color_modes", [])
            has_br = any(m != "onoff" for m in supported) if supported else False
            if has_br or attrs.get("brightness") is not None:
                rows.append([
                    InlineKeyboardButton(text="\U0001f505 \u2212", callback_data=f"bright:{entity_id}:down"),
                    InlineKeyboardButton(text="\U0001f506 +", callback_data=f"bright:{entity_id}:up"),
                ])
            # Color button for color-capable lights
            has_color = any(
                m in ("rgb", "rgbw", "rgbww", "hs", "xy") for m in supported
            ) if supported else False
            if has_color:
                rows.append([InlineKeyboardButton(
                    text="\U0001f3a8 Цвет",
                    callback_data=f"lclr:{entity_id}",
                )])

    elif domain == "cover":
        rows.append([
            InlineKeyboardButton(text="\u2b06 Открыть", callback_data=f"act:{entity_id}:open_cover"),
            InlineKeyboardButton(text="\u23f9 Стоп", callback_data=f"act:{entity_id}:stop_cover"),
            InlineKeyboardButton(text="\u2b07 Закрыть", callback_data=f"act:{entity_id}:close_cover"),
        ])

    elif domain == "climate":
        rows.append([
            InlineKeyboardButton(text="\u2b06 Темп+", callback_data=f"clim:{entity_id}:up"),
            InlineKeyboardButton(text="\u2b07 Темп\u2212", callback_data=f"clim:{entity_id}:down"),
        ])

    elif domain == "vacuum":
        rows.append([
            InlineKeyboardButton(text="\u25b6 Старт", callback_data=f"act:{entity_id}:start"),
            InlineKeyboardButton(text="\u23f8 Пауза", callback_data=f"act:{entity_id}:pause"),
            InlineKeyboardButton(text="\u23f9 Стоп", callback_data=f"act:{entity_id}:stop"),
        ])
        rows.append([
            InlineKeyboardButton(text="\U0001f3e0 Док", callback_data=f"act:{entity_id}:return_to_base"),
            InlineKeyboardButton(text="\U0001f4cd Найти", callback_data=f"act:{entity_id}:locate"),
        ])

    elif domain in ("scene", "script"):
        rows.append([InlineKeyboardButton(
            text="\u25b6\ufe0f Активировать", callback_data=f"act:{entity_id}:turn_on",
        )])

    elif domain == "lock":
        rows.append([
            InlineKeyboardButton(text="\U0001f513 Открыть", callback_data=f"act:{entity_id}:unlock"),
            InlineKeyboardButton(text="\U0001f512 Закрыть", callback_data=f"act:{entity_id}:lock"),
        ])

    elif domain == "button":
        rows.append([InlineKeyboardButton(
            text="\U0001f518 Нажать", callback_data=f"act:{entity_id}:press",
        )])

    elif domain == "media_player":
        rows.append([
            InlineKeyboardButton(text="\U0001f7e2 ON", callback_data=f"act:{entity_id}:turn_on"),
            InlineKeyboardButton(text="\u26aa OFF", callback_data=f"act:{entity_id}:turn_off"),
        ])
        rows.append([
            InlineKeyboardButton(text="\u25b6 Play", callback_data=f"act:{entity_id}:media_play"),
            InlineKeyboardButton(text="\u23f8 Pause", callback_data=f"act:{entity_id}:media_pause"),
            InlineKeyboardButton(text="\u23f9 Stop", callback_data=f"act:{entity_id}:media_stop"),
        ])
        rows.append([
            InlineKeyboardButton(text="\U0001f509 Vol\u2212", callback_data=f"mvol:{entity_id}:dn"),
            InlineKeyboardButton(text="\U0001f50a Vol+", callback_data=f"mvol:{entity_id}:up"),
            InlineKeyboardButton(
                text="\U0001f507 Mute" if not attrs.get("is_volume_muted") else "\U0001f508 Unmute",
                callback_data=f"mmut:{entity_id}",
            ),
        ])
        sources = attrs.get("source_list")
        if sources and len(sources) > 0:
            rows.append([InlineKeyboardButton(
                text="\U0001f4fb Источник", callback_data=f"msrc:{entity_id}",
            )])

    elif domain == "select":
        options = attrs.get("options", [])
        for opt in options[:6]:
            cb = f"ssel:{entity_id}:{opt}"
            if len(cb) <= 64:
                marker = "\u2705 " if state == opt else ""
                rows.append([InlineKeyboardButton(
                    text=f"{marker}{_trunc(str(opt), 28)}",
                    callback_data=cb,
                )])

    elif domain == "number":
        rows.append([
            InlineKeyboardButton(text="\u2b06 +", callback_data=f"nval:{entity_id}:up"),
            InlineKeyboardButton(text="\u2b07 \u2212", callback_data=f"nval:{entity_id}:dn"),
        ])

    elif domain == "water_heater":
        rows.append([
            InlineKeyboardButton(text="\U0001f7e2 ON", callback_data=f"act:{entity_id}:turn_on"),
            InlineKeyboardButton(text="\u26aa OFF", callback_data=f"act:{entity_id}:turn_off"),
        ])
        rows.append([
            InlineKeyboardButton(text="\u2b06 Темп+", callback_data=f"clim:{entity_id}:up"),
            InlineKeyboardButton(text="\u2b07 Темп\u2212", callback_data=f"clim:{entity_id}:down"),
        ])

    elif domain == "sensor":
        pass  # read-only, no controls

    elif domain == "binary_sensor":
        pass  # read-only, no controls

    elif domain == "event":
        pass  # event entities are read-only triggers

    else:
        rows.append([
            InlineKeyboardButton(text="\U0001f7e2 ON", callback_data=f"act:{entity_id}:turn_on"),
            InlineKeyboardButton(text="\u26aa OFF", callback_data=f"act:{entity_id}:turn_off"),
        ])

    return rows


# ---------------------------------------------------------------------------
# Light color picker
# ---------------------------------------------------------------------------


def build_light_color_menu(
    entity_id: str,
    name: str,
    back_cb: str = "nav:main",
) -> tuple[str, InlineKeyboardMarkup]:
    """Color preset picker for a single light."""
    text = f"\U0001f3a8 <b>Цвет — {_sanitize(name)}</b>\n\nВыберите цвет:"
    rows: list[list[InlineKeyboardButton]] = []

    for i, (label, rgb) in enumerate(COLOR_PRESETS):
        cb = f"lcs:{entity_id}:{i}"
        if len(cb) <= 64:
            rows.append([InlineKeyboardButton(text=label, callback_data=cb)])

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=back_cb)])
    if back_cb != "nav:main":
        rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Global light color scenes
# ---------------------------------------------------------------------------


def build_global_color_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Color picker that applies to ALL lights globally."""
    text = (
        "\U0001f3a8 <b>Цвет света — Все комнаты</b>\n\n"
        "Выберите цвет для всех ламп:"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for i, (label, _rgb) in enumerate(COLOR_PRESETS):
        rows.append([InlineKeyboardButton(text=label, callback_data=f"gcl:{i}")])

    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_global_color_result(
    color_label: str, count: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Result after applying global color."""
    text = f"\u2705 {color_label} применён к {count} светильникам"
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text="\U0001f3a8 Другой цвет", callback_data="menu:gcolor")])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Scenarios menu (scenes/scripts/automations without area)
# ---------------------------------------------------------------------------


def build_scenarios_menu(
    scenes: list[dict[str, Any]],
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build Scenarios menu for entities not assigned to rooms."""
    if not scenes:
        text = "\U0001f3ac <b>Сценарии</b>\n\nНет сценариев."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_home_btn()],
        ])
        return text, kb

    return build_entity_list(
        scenes, page, page_size,
        title=f"\U0001f3ac <b>Сценарии</b> ({len(scenes)})",
        back_cb="nav:main",
        page_cb_prefix="scp",
    )


# ---------------------------------------------------------------------------
# Radio menu
# ---------------------------------------------------------------------------


def build_radio_menu(
    stations: list[dict[str, Any]],
    current_idx: int = 0,
    current_player: str | None = None,
    players: list[dict[str, Any]] | None = None,
    is_playing: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Radio control menu."""
    if not stations:
        text = (
            "\U0001f4fb <b>Радио \U0001f1f7\U0001f1fa</b>\n\n"
            "Радио не настроено.\n\n"
            "Добавьте станции в конфигурации add-on:\n"
            "<code>radio_stations:</code>\n"
            "<code>  - name: Station</code>\n"
            "<code>    url: http://stream.url</code>\n\n"
            "Или укажите <code>radio_entity_id</code> с entity_id select-сущности."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_home_btn()],
        ])
        return text, kb

    station = stations[current_idx] if current_idx < len(stations) else stations[0]
    station_name = station.get("name", f"Станция {current_idx + 1}")
    play_icon = "\u25b6" if not is_playing else "\u23f8"

    text = (
        f"\U0001f4fb <b>Радио \U0001f1f7\U0001f1fa</b>\n\n"
        f"Станция: <b>{_sanitize(station_name)}</b> "
        f"({current_idx + 1}/{len(stations)})"
    )
    if current_player:
        text += f"\nВывод: <code>{_sanitize(current_player)}</code>"

    rows: list[list[InlineKeyboardButton]] = []

    # Transport controls
    rows.append([
        InlineKeyboardButton(text="\u23ee Пред.", callback_data="rad:prev"),
        InlineKeyboardButton(text=f"{play_icon} Play", callback_data="rad:play"),
        InlineKeyboardButton(text="\u23f9 Stop", callback_data="rad:stop"),
        InlineKeyboardButton(text="\u23ed След.", callback_data="rad:next"),
    ])

    # Output device picker
    if players:
        for pl in players[:6]:
            pl_eid = pl["entity_id"]
            pl_name = pl.get("friendly_name", pl_eid)
            marker = "\U0001f50a " if pl_eid == current_player else ""
            cb = f"rout:{pl_eid}"
            if len(cb) <= 64:
                rows.append([InlineKeyboardButton(
                    text=f"{marker}{_trunc(pl_name, 26)}",
                    callback_data=cb,
                )])

    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Vacuum extra menus
# ---------------------------------------------------------------------------


def build_vacuum_rooms(
    entity_id: str,
    name: str,
    rooms: list[dict[str, Any]],
    selected_room: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """rooms: [{"segment_id": "16", "segment_name": "Kitchen"}, ...]"""
    text = f"\U0001f9f9 <b>{_sanitize(name)}</b>\n\nВыберите комнату для уборки:"
    if selected_room:
        for r in rooms:
            if r["segment_id"] == selected_room:
                text = (
                    f"\U0001f9f9 <b>{_sanitize(name)}</b>\n"
                    f"Комната: {_sanitize(r.get('segment_name', selected_room))}\n\n"
                    "Нажмите Старт для начала уборки."
                )
                break

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(rooms), 2):
        row: list[InlineKeyboardButton] = []
        for r in rooms[i:i + 2]:
            sid = r["segment_id"]
            sname = r.get("segment_name", sid)
            marker = "\u2705 " if sid == selected_room else ""
            row.append(InlineKeyboardButton(
                text=f"{marker}{_trunc(sname, 20)}",
                callback_data=f"vroom:{entity_id}:{sid}",
            ))
        rows.append(row)

    if selected_room:
        rows.append([InlineKeyboardButton(
            text="\u25b6\ufe0f Начать уборку",
            callback_data=f"vseg:{entity_id}:{selected_room}",
        )])

    rows.append([
        InlineKeyboardButton(text="\u23f9 Стоп", callback_data=f"vcmd:{entity_id}:stop"),
        InlineKeyboardButton(text="\U0001f3e0 Док", callback_data=f"vcmd:{entity_id}:return_to_base"),
    ])
    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=f"ent:{entity_id}")])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_vacuum_routines(
    entity_id: str,
    name: str,
    routines: list[dict[str, Any]],
) -> tuple[str, InlineKeyboardMarkup]:
    """routines: [{"entity_id": "button.xxx", "name": "Routine Name"}, ...]"""
    text = f"\U0001f3ac <b>Сценарии — {_sanitize(name)}</b>\n\n"
    if not routines:
        text += "Сценариев не найдено."
    else:
        text += "Выберите сценарий:"

    rows: list[list[InlineKeyboardButton]] = []
    for rt in routines:
        rname = rt.get("name", rt["entity_id"])
        rows.append([InlineKeyboardButton(
            text=f"\U0001f3ac {_trunc(rname, 30)}",
            callback_data=f"rtn:{rt['entity_id']}",
        )])

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=f"ent:{entity_id}")])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Media player source picker
# ---------------------------------------------------------------------------


def build_media_source_menu(
    entity_id: str,
    name: str,
    sources: list[str],
    current_source: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Source selector for media_player."""
    text = f"\U0001f4fb <b>{_sanitize(name)}</b>\n\nВыберите источник:"
    rows: list[list[InlineKeyboardButton]] = []

    for src in sources[:12]:
        marker = "\u2705 " if src == current_source else ""
        cb = f"msrs:{entity_id}:{src}"
        if len(cb) <= 64:
            rows.append([InlineKeyboardButton(
                text=f"{marker}{_trunc(str(src), 28)}",
                callback_data=cb,
            )])

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data=f"ent:{entity_id}")])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Favorites — entities + actions + pinned items
# ---------------------------------------------------------------------------


def build_favorites_menu(
    entities: list[dict[str, Any]],
    page: int,
    page_size: int,
    fav_actions: list[dict[str, Any]] | None = None,
    pinned_items: list[dict[str, Any]] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if not entities and not fav_actions and not pinned_items:
        text = "\u2b50 <b>Избранное</b>\n\nСписок пуст."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_home_btn()],
        ])
        return text, kb

    # Build pinned items rows (areas, routines) — shown before entity list
    pinned_rows: list[list[InlineKeyboardButton]] = []
    if pinned_items:
        for pin in pinned_items:
            itype = pin["item_type"]
            tid = pin["target_id"]
            label = pin.get("label", tid)
            if itype == "area":
                pinned_rows.append([InlineKeyboardButton(
                    text=f"\U0001f4cc {_trunc(label, 26)}",
                    callback_data=f"ar:{tid}",
                )])
            elif itype == "routine":
                pinned_rows.append([InlineKeyboardButton(
                    text=f"\U0001f3ac {_trunc(label, 26)}",
                    callback_data=f"rtn:{tid}",
                )])

    # If there are favorite actions, show a tab button
    extra_rows: list[list[InlineKeyboardButton]] = []
    if fav_actions:
        extra_rows.append([InlineKeyboardButton(
            text=f"\u26a1 Быстрые действия ({len(fav_actions)})",
            callback_data="menu:fav_actions",
        )])

    t, k = build_entity_list(
        entities, page, page_size,
        title="\u2b50 <b>Избранное</b>",
        back_cb="nav:main",
        page_cb_prefix="favp",
    )
    rows = k.inline_keyboard[:]

    # Insert pinned items and action buttons before the Back/Home buttons
    insert_pos = max(0, len(rows) - 1)
    for pr in pinned_rows:
        rows.insert(insert_pos, pr)
        insert_pos += 1
    for er in extra_rows:
        rows.insert(insert_pos, er)
        insert_pos += 1

    k = InlineKeyboardMarkup(inline_keyboard=rows)
    return t, k


def build_fav_actions_menu(
    actions: list[dict[str, Any]],
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build favorite actions list."""
    text = "\u26a1 <b>Быстрые действия</b>\n\n"
    if not actions:
        text += "Нет сохранённых действий."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2b05 Назад", callback_data="menu:favorites")],
            [_home_btn()],
        ])
        return text, kb

    total = len(actions)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_acts = actions[start:end]

    text += f"Стр. {page + 1}/{total_pages}\n"
    rows: list[list[InlineKeyboardButton]] = []

    for act in page_acts:
        label = act.get("label", act.get("action_type", "?"))
        rows.append([
            InlineKeyboardButton(
                text=f"\u26a1 {_trunc(label, 22)}",
                callback_data=f"fa_run:{act['id']}",
            ),
            InlineKeyboardButton(
                text="\U0001f5d1",
                callback_data=f"fa_del:{act['id']}",
            ),
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="\u25c0", callback_data=f"fap:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="\u25b6", callback_data=f"fap:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data="menu:favorites")])
    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Notifications settings
# ---------------------------------------------------------------------------


def build_notif_list(
    subs: list[dict[str, Any]],
    page: int,
    page_size: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """subs: [{"entity_id", "friendly_name", "enabled", "mode"}, ...]"""
    text = "\U0001f514 <b>Уведомления</b>\n\n"
    if not subs:
        text += "Нет подписок. Добавьте через карточку устройства."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_home_btn()],
        ])
        return text, kb

    total = len(subs)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_subs = subs[start:end]

    text += f"Стр. {page + 1}/{total_pages}\n\n"
    rows: list[list[InlineKeyboardButton]] = []

    for sub in page_subs:
        eid = sub["entity_id"]
        name = sub.get("friendly_name", eid)
        on = sub.get("enabled", False)
        icon = "\U0001f514" if on else "\U0001f515"
        mode_label = "S" if sub.get("mode") == "state_only" else "S+A"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {_trunc(name, 22)} [{mode_label}]",
            callback_data=f"ntog:{eid}",
        )])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="\u25c0", callback_data=f"nfp:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="\u25b6", callback_data=f"nfp:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def build_status_menu(entities: list[dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    if not entities:
        text = "\u2139\ufe0f <b>Статус</b>\n\nНет сущностей для отображения."
    else:
        lines = ["\u2139\ufe0f <b>Статус</b>\n"]
        for ent in entities:
            eid = ent.get("entity_id", "unknown")
            name = ent.get("attributes", {}).get("friendly_name", eid)
            state = ent.get("state", "unknown")
            domain = eid.split(".", 1)[0]
            icon = DOMAIN_ICONS.get(domain, "\u2022")
            unit = ent.get("attributes", {}).get("unit_of_measurement", "")
            unit_str = f" {_sanitize(str(unit))}" if unit else ""
            lines.append(f"{icon} {_sanitize(name)}: <code>{_sanitize(state)}{unit_str}</code>")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f504 Обновить", callback_data="menu:status")],
        [_home_btn()],
    ])
    return text, kb


# ---------------------------------------------------------------------------
# Search results
# ---------------------------------------------------------------------------


def build_search_prompt() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "\U0001f50d <b>Поиск</b>\n\n"
        "Отправьте запрос в чат для поиска устройств.\n"
        "Или используйте: /search <i>запрос</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_home_btn()],
    ])
    return text, kb


def build_search_results(
    query: str,
    entities: list[dict[str, Any]],
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build search results page."""
    if not entities:
        text = f"\U0001f50d <b>Поиск:</b> {_sanitize(query)}\n\nНичего не найдено."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_home_btn()],
        ])
        return text, kb

    return build_entity_list(
        entities, page, page_size,
        title=f"\U0001f50d <b>Поиск:</b> {_sanitize(query)} ({len(entities)})",
        back_cb="nav:main",
        page_cb_prefix="srp",
    )


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def build_snapshots_list(
    snapshots: list[dict[str, Any]],
) -> tuple[str, InlineKeyboardMarkup]:
    text = "\U0001f4f8 <b>Снимки состояний</b>\n\n"
    if not snapshots:
        text += "Нет сохранённых снимков.\nИспользуйте /snapshot для создания."
    else:
        text += f"Всего: {len(snapshots)}\n"

    rows: list[list[InlineKeyboardButton]] = []
    for snap in snapshots[:10]:
        label = f"{snap['name']} ({snap['created_at'][:16]})"
        rows.append([
            InlineKeyboardButton(
                text=_trunc(label, 32),
                callback_data=f"snap:{snap['id']}",
            ),
            InlineKeyboardButton(
                text="\U0001f5d1",
                callback_data=f"snapdel:{snap['id']}",
            ),
        ])

    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_snapshot_detail(
    snapshot: dict[str, Any],
    diff_text: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"\U0001f4f8 <b>{_sanitize(snapshot['name'])}</b>\n"
        f"Created: {snapshot['created_at'][:19]}\n"
        f"Entities: {len(snapshot.get('payload', []))}\n"
    )
    if diff_text:
        text += f"\n<b>Changes since snapshot:</b>\n{diff_text}"

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="\U0001f504 Diff with current",
            callback_data=f"snapdiff:{snapshot['id']}",
        )],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="menu:snapshots")],
        [_home_btn()],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def build_schedule_list(
    schedules: list[dict[str, Any]],
) -> tuple[str, InlineKeyboardMarkup]:
    text = "\u23f0 <b>Расписание</b>\n\n"
    if not schedules:
        text += "Нет задач.\nИспользуйте /schedule add для создания."
    else:
        for s in schedules[:10]:
            icon = "\u2705" if s.get("enabled") else "\u274c"
            lr = s.get("last_result") or "\u2014"
            text += f"{icon} <b>{_sanitize(s['name'])}</b> [{s['cron_expr']}]\n"
            text += f"  Результат: {_sanitize(str(lr)[:60])}\n"

    rows: list[list[InlineKeyboardButton]] = []
    for s in schedules[:10]:
        toggle_label = "\u23f8" if s.get("enabled") else "\u25b6"
        rows.append([
            InlineKeyboardButton(
                text=f"{toggle_label} {_trunc(s['name'], 18)}",
                callback_data=f"schtog:{s['id']}",
            ),
            InlineKeyboardButton(
                text="\U0001f5d1",
                callback_data=f"schdel:{s['id']}",
            ),
        ])

    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def build_diagnostics_menu(
    diag_text: str,
) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="\U0001f504 Refresh", callback_data="diag:refresh"),
            InlineKeyboardButton(text="\U0001f534 Last Error", callback_data="diag:trace"),
        ],
        [_home_btn()],
    ]
    return diag_text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def build_roles_list(
    roles: list[dict[str, Any]],
) -> tuple[str, InlineKeyboardMarkup]:
    text = "\U0001f464 <b>Роли пользователей</b>\n\n"
    if not roles:
        text += "Роли не настроены (все пользователи = user)."
    else:
        for r in roles:
            text += f"\u2022 <code>{r['user_id']}</code>: <b>{r['role']}</b>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_home_btn()],
    ])
    return text, kb


# ---------------------------------------------------------------------------
# Confirmation / Result
# ---------------------------------------------------------------------------


def build_confirmation(
    message: str,
    back_cb: str = "nav:main",
) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data=back_cb)],
    ]
    if back_cb != "nav:main":
        rows.append([_home_btn()])
    return message, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def build_help_menu() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "\u2753 <b>Помощь</b>\n\n"
        "<b>Команды:</b>\n"
        "/start \u2014 Главное меню\n"
        "/menu \u2014 Главное меню\n"
        "/ping \u2014 Проверка связи\n"
        "/status \u2014 Статус устройств\n"
        "/search <i>запрос</i> \u2014 Поиск устройств\n"
        "/snapshot \u2014 Снимок состояний\n"
        "/snapshots \u2014 Список снимков\n"
        "/schedule \u2014 Расписание\n"
        "/terminal <i>команда</i> \u2014 Терминал (admin)\n"
        "/health \u2014 Проверка здоровья\n"
        "/diag \u2014 Диагностика\n"
        "/role \u2014 Управление ролями (admin)\n"
        "/export_settings \u2014 Экспорт настроек\n\n"
        "<b>Навигация:</b>\n"
        "\u2022 <b>Устройства</b> \u2014 Комнаты \u2192 Устройства \u2192 Управление\n"
        "\u2022 <b>Сценарии</b> \u2014 Сцены/скрипты без привязки к комнате\n"
        "\u2022 <b>Активные</b> \u2014 Все включённые устройства\n"
        "\u2022 <b>Радио</b> \u2014 Радиостанции\n"
        "\u2022 <b>Цвет света</b> \u2014 Глобальные цветовые сцены\n"
        "\u2022 <b>Избранное</b> \u2014 Быстрый доступ\n"
        "\u2022 <b>Уведомления</b> \u2014 Настройка уведомлений\n"
        "\u2022 <b>Автоматизации</b> \u2014 Управление автоматизациями\n"
        "\u2022 <b>Списки дел</b> \u2014 To-Do списки HA\n\n"
        "Все меню обновляются в одном сообщении."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_home_btn()],
    ])
    return text, kb


# ---------------------------------------------------------------------------
# Sorted device detail — actions first, scenarios, then read-only
# ---------------------------------------------------------------------------


def sort_entities_for_device(
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sort entities: actionable first, then scenes/scripts, then read-only sensors."""
    action_domains = frozenset({
        "light", "switch", "fan", "cover", "vacuum", "climate", "lock",
        "media_player", "button", "input_boolean", "water_heater", "number", "select",
    })
    scenario_domains = frozenset({"scene", "script", "automation"})
    read_only_domains = frozenset({"sensor", "binary_sensor"})

    def sort_key(ent: dict[str, Any]) -> tuple[int, str]:
        domain = ent.get("domain", ent["entity_id"].split(".", 1)[0])
        if domain in action_domains:
            return (0, ent.get("friendly_name", ent["entity_id"]))
        elif domain in scenario_domains:
            return (1, ent.get("friendly_name", ent["entity_id"]))
        elif domain in read_only_domains:
            return (2, ent.get("friendly_name", ent["entity_id"]))
        return (3, ent.get("friendly_name", ent["entity_id"]))

    return sorted(entities, key=sort_key)


def group_sensor_entities(
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group similar sensor entities to reduce clutter.

    For devices with many sensors (e.g., teapot with multiple temp readings),
    prefer the primary sensor by device_class and deduplicate by unit.
    """
    sensors: list[dict[str, Any]] = []
    non_sensors: list[dict[str, Any]] = []

    for ent in entities:
        domain = ent.get("domain", ent["entity_id"].split(".", 1)[0])
        if domain in ("sensor", "binary_sensor"):
            sensors.append(ent)
        else:
            non_sensors.append(ent)

    if len(sensors) <= 3:
        return non_sensors + sensors

    # Group sensors by device_class or unit
    seen_classes: dict[str, dict[str, Any]] = {}
    remaining: list[dict[str, Any]] = []

    for s in sensors:
        attrs = s.get("attributes", {})
        dc = attrs.get("device_class", "")
        unit = attrs.get("unit_of_measurement", "")
        key = f"{dc}|{unit}" if dc else ""

        if key and key in seen_classes:
            # Skip duplicate-class sensor, keep the first one
            continue
        if key:
            seen_classes[key] = s
        remaining.append(s)

    return non_sensors + remaining


# ---------------------------------------------------------------------------
# Automations menu
# ---------------------------------------------------------------------------


def build_automations_menu(
    automations: list[dict[str, Any]],
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show list of automations with toggle and trigger buttons."""
    if not automations:
        text = "\U0001f916 <b>Автоматизации</b>\n\nАвтоматизации не найдены."
        kb = InlineKeyboardMarkup(inline_keyboard=[[_home_btn()]])
        return text, kb

    total = len(automations)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    end = start + page_size
    page_items = automations[start:end]

    text = f"\U0001f916 <b>Автоматизации</b> ({total})\n\n"
    rows: list[list[InlineKeyboardButton]] = []
    for a in page_items:
        eid = a["entity_id"]
        name = a.get("friendly_name", eid)
        state = a.get("state", "off")
        icon = "\u2705" if state == "on" else "\u274c"
        text += f"{icon} {_sanitize(_trunc(name, 40))}\n"
        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {_trunc(name, 18)}",
                callback_data=f"atog:{eid}",
            ),
            InlineKeyboardButton(
                text="\u25b6 Run",
                callback_data=f"atrig:{eid}",
            ),
        ])

    # Pagination
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="\u2b05", callback_data=f"autp:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="\u27a1", callback_data=f"autp:{page + 1}"))
        rows.append(nav)

    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# To-Do lists menu
# ---------------------------------------------------------------------------


def build_todo_lists_menu(
    lists: list[dict[str, Any]],
) -> tuple[str, InlineKeyboardMarkup]:
    """Show available HA to-do lists."""
    if not lists:
        text = "\U0001f4cb <b>Списки дел</b>\n\nСписки дел не найдены."
        kb = InlineKeyboardMarkup(inline_keyboard=[[_home_btn()]])
        return text, kb

    text = f"\U0001f4cb <b>Списки дел</b> ({len(lists)})\n\nВыберите список:"
    rows: list[list[InlineKeyboardButton]] = []
    for td in lists[:10]:
        eid = td["entity_id"]
        name = td.get("friendly_name", eid)
        cb = f"tdl:{eid}"
        if len(cb) <= 64:
            rows.append([InlineKeyboardButton(
                text=f"\U0001f4cb {_trunc(name, 28)}",
                callback_data=cb,
            )])

    rows.append([_home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_todo_items_menu(
    list_name: str,
    list_eid: str,
    items: list[dict[str, Any]],
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show items in a to-do list."""
    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]

    text = f"\U0001f4cb <b>{_sanitize(list_name)}</b>"
    if not items:
        text += "\n\nСписок пуст."
    else:
        text += f" ({total})\n\n"
        for item in page_items:
            summary = item.get("summary", "???")
            status = item.get("status", "needs_action")
            icon = "\u2705" if status == "completed" else "\u2b1c"
            text += f"{icon} {_sanitize(_trunc(summary, 40))}\n"

    rows: list[list[InlineKeyboardButton]] = []
    # Item action buttons (complete/delete)
    for item in page_items:
        uid = item.get("uid", "")
        summary = item.get("summary", "???")
        status = item.get("status", "needs_action")
        btns: list[InlineKeyboardButton] = []
        if status != "completed":
            cb = f"tdc:{list_eid}:{uid}"
            if len(cb) <= 64:
                btns.append(InlineKeyboardButton(text="\u2705", callback_data=cb))
        cb_del = f"tdd:{list_eid}:{uid}"
        if len(cb_del) <= 64:
            btns.append(InlineKeyboardButton(
                text=f"\U0001f5d1 {_trunc(summary, 16)}",
                callback_data=cb_del,
            ))
        if btns:
            rows.append(btns)

    # Pagination
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="\u2b05", callback_data=f"tdp:{list_eid}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="\u27a1", callback_data=f"tdp:{list_eid}:{page + 1}"))
        rows.append(nav)

    # Add item button
    rows.append([InlineKeyboardButton(text="\u2795 Добавить", callback_data=f"tda:{list_eid}")])
    rows.append([
        InlineKeyboardButton(text="\u2b05 Назад", callback_data="menu:todo"),
        _home_btn(),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)
