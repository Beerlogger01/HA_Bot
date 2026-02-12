"""Inline keyboard UI builders for multi-level menus.

All builders return (text, InlineKeyboardMarkup) tuples.
Callback data format: "prefix:payload" (max 64 bytes per Telegram limit).

Menu tree:
  Main -> Управление -> (Floors?) -> Areas -> Entities -> Controls
  Main -> Избранное -> Entities -> Controls
  Main -> Уведомления -> Entities -> toggle/mode
  Main -> Обновить / Статус
"""

from __future__ import annotations

import math
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOMAIN_ICONS: dict[str, str] = {
    "light": "\U0001f4a1",
    "switch": "\U0001f50c",
    "vacuum": "\U0001f916",
    "scene": "\U0001f3ac",
    "script": "\u25b6\ufe0f",
    "climate": "\U0001f321\ufe0f",
    "fan": "\U0001f32c\ufe0f",
    "cover": "\U0001f6aa",
    "sensor": "\U0001f4ca",
    "binary_sensor": "\U0001f534",
    "automation": "\u2699\ufe0f",
    "input_boolean": "\U0001f518",
    "media_player": "\U0001f3b5",
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
}


def _sanitize(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _state_icon(domain: str, state: str) -> str:
    if state in ("on", "open", "cleaning", "playing", "home"):
        return "\U0001f7e2"
    if state in ("off", "closed", "docked", "idle", "paused", "standby"):
        return "\u26aa"
    if state in ("unavailable", "unknown"):
        return "\U0001f534"
    return "\U0001f535"


def _trunc(text: str, max_len: int = 28) -> str:
    return text[:max_len] if len(text) > max_len else text


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------


def build_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "\U0001f3e0 <b>Home Assistant Bot</b>\n\n"
        "Выберите действие:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f3db Управление", callback_data="menu:manage")],
        [
            InlineKeyboardButton(text="\u2b50 Избранное", callback_data="menu:favorites"),
            InlineKeyboardButton(text="\U0001f514 Уведомления", callback_data="menu:notif"),
        ],
        [
            InlineKeyboardButton(text="\U0001f504 Обновить", callback_data="menu:refresh"),
            InlineKeyboardButton(text="\u2139\ufe0f Статус", callback_data="menu:status"),
        ],
    ])
    return text, kb


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------


def build_floors_menu(
    floors: list[dict[str, Any]],
    unassigned_count: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """floors: [{"floor_id", "name", "area_count"}, ...]"""
    text = "\U0001f3db <b>Пространства</b>\n\nВыберите этаж/пространство:"
    rows: list[list[InlineKeyboardButton]] = []
    for fl in floors:
        label = f"{fl['name']} ({fl['area_count']})"
        rows.append([InlineKeyboardButton(
            text=label, callback_data=f"fl:{fl['floor_id']}",
        )])
    if unassigned_count > 0:
        rows.append([InlineKeyboardButton(
            text=f"Без пространства ({unassigned_count})",
            callback_data="fl:__none__",
        )])
    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data="nav:main")])
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

    text = f"{title} (стр. {page + 1}/{total_pages})\n\nВыберите устройство:"
    rows: list[list[InlineKeyboardButton]] = []

    for ent in page_ents:
        eid = ent["entity_id"]
        name = ent.get("friendly_name", eid)
        state = ent.get("state", "")
        domain = eid.split(".", 1)[0]
        si = _state_icon(domain, state)
        rows.append([InlineKeyboardButton(
            text=f"{si} {_trunc(name)}",
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
    state = state_data.get("state", "unknown")
    icon = DOMAIN_ICONS.get(domain, "\U0001f4e6")

    text = f"{icon} <b>{_sanitize(name)}</b>\n\nСостояние: <code>{_sanitize(state)}</code>"

    extra: list[str] = []
    if domain == "climate":
        ct = attrs.get("current_temperature")
        tt = attrs.get("temperature")
        if ct is not None:
            extra.append(f"Текущая: {ct}\u00b0")
        if tt is not None:
            extra.append(f"Целевая: {tt}\u00b0")
    elif domain == "light":
        br = attrs.get("brightness")
        if br is not None:
            extra.append(f"Яркость: {round(br / 255 * 100)}%")
    elif domain == "vacuum":
        bat = attrs.get("battery_level")
        if bat is not None:
            extra.append(f"Батарея: {bat}%")
        fs = attrs.get("fan_speed")
        if fs is not None:
            extra.append(f"Мощность: {fs}")
    elif domain == "cover":
        pos = attrs.get("current_position")
        if pos is not None:
            extra.append(f"Позиция: {pos}%")

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
        # Room cleaning + routines — added via handler, not here (needs state)

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
            InlineKeyboardButton(text="\u25b6 Play", callback_data=f"act:{entity_id}:media_play"),
            InlineKeyboardButton(text="\u23f8 Pause", callback_data=f"act:{entity_id}:media_pause"),
            InlineKeyboardButton(text="\u23f9 Stop", callback_data=f"act:{entity_id}:media_stop"),
        ])

    else:
        rows.append([
            InlineKeyboardButton(text="\U0001f7e2 ON", callback_data=f"act:{entity_id}:turn_on"),
            InlineKeyboardButton(text="\u26aa OFF", callback_data=f"act:{entity_id}:turn_off"),
        ])

    return rows


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
    text = f"\U0001f916 <b>{_sanitize(name)}</b>\n\nВыберите комнату для уборки:"
    if selected_room:
        for r in rooms:
            if r["segment_id"] == selected_room:
                text = (
                    f"\U0001f916 <b>{_sanitize(name)}</b>\n"
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
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------


def build_favorites_menu(
    entities: list[dict[str, Any]],
    page: int,
    page_size: int,
) -> tuple[str, InlineKeyboardMarkup]:
    if not entities:
        text = "\u2b50 <b>Избранное</b>\n\nСписок пуст."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2b05 Назад", callback_data="nav:main")],
        ])
        return text, kb

    return build_entity_list(
        entities, page, page_size,
        title="\u2b50 <b>Избранное</b>",
        back_cb="nav:main",
        page_cb_prefix="favp",
    )


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
            [InlineKeyboardButton(text="\u2b05 Назад", callback_data="nav:main")],
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

    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data="nav:main")])
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
            unit = ent.get("attributes", {}).get("unit_of_measurement", "")
            unit_str = f" {_sanitize(str(unit))}" if unit else ""
            lines.append(f"\u2022 {_sanitize(name)}: <code>{_sanitize(state)}{unit_str}</code>")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f504 Обновить", callback_data="menu:status")],
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="nav:main")],
    ])
    return text, kb


# ---------------------------------------------------------------------------
# Confirmation / Result
# ---------------------------------------------------------------------------


def build_confirmation(
    message: str,
    back_cb: str = "nav:main",
) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data=back_cb)],
    ])
    return message, kb


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
        "/status \u2014 Статус устройств\n\n"
        "<b>Навигация:</b>\n"
        "\u2022 <b>Управление</b> \u2014 Пространства \u2192 Комнаты \u2192 Устройства \u2192 Функции\n"
        "\u2022 <b>Избранное</b> \u2014 Быстрый доступ к часто используемым\n"
        "\u2022 <b>Уведомления</b> \u2014 Подписки на изменения состояний\n\n"
        "Все меню обновляются в одном сообщении."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2b05 Назад", callback_data="nav:main")],
    ])
    return text, kb
