"""Inline keyboard UI builders for multi-level menus.

All menus return (text, InlineKeyboardMarkup) tuples.
Callback data format: "prefix:payload" where prefix identifies the menu action.
"""

from __future__ import annotations

import math
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DOMAINS = ["light", "switch", "vacuum", "scene", "script", "climate", "fan", "cover"]

DOMAIN_ICONS: dict[str, str] = {
    "light": "\U0001f4a1",       # light bulb
    "switch": "\U0001f50c",      # plug
    "vacuum": "\U0001f916",      # robot
    "scene": "\U0001f3ac",       # clapper
    "script": "\u25b6\ufe0f",    # play
    "climate": "\U0001f321\ufe0f",  # thermometer
    "fan": "\U0001f32c\ufe0f",   # wind
    "cover": "\U0001f6aa",       # door
    "sensor": "\U0001f4ca",      # chart
    "binary_sensor": "\U0001f534",  # red circle
    "automation": "\u2699\ufe0f",   # gear
    "input_boolean": "\U0001f518",  # radio button
    "media_player": "\U0001f3b5",   # music note
    "camera": "\U0001f4f7",      # camera
    "lock": "\U0001f512",        # lock
    "number": "\U0001f522",      # numbers
    "select": "\U0001f4cb",      # clipboard
    "button": "\U0001f518",      # radio button
    "water_heater": "\U0001f6bf",  # shower
}

DOMAIN_LABELS: dict[str, str] = {
    "light": "Lights",
    "switch": "Switches",
    "vacuum": "Vacuums",
    "scene": "Scenes",
    "script": "Scripts",
    "climate": "Climate",
    "fan": "Fans",
    "cover": "Covers",
}


# ---------------------------------------------------------------------------
# Menu builders
# ---------------------------------------------------------------------------


def build_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Return main menu text and keyboard."""
    text = (
        "\U0001f3e0 <b>Home Assistant Bot</b>\n\n"
        "Select a category:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="\U0001f4a1 Devices", callback_data="menu:devices"),
            InlineKeyboardButton(text="\U0001f3ac Scenes", callback_data="menu:scenes"),
        ],
        [
            InlineKeyboardButton(text="\U0001f916 Robots", callback_data="menu:robots"),
            InlineKeyboardButton(text="\U0001f4ca Status", callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton(text="\u2753 Help", callback_data="menu:help"),
        ],
    ])
    return text, keyboard


def build_help_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Return help text and back button."""
    text = (
        "\u2753 <b>Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start \u2014 Show main menu\n"
        "/menu \u2014 Show main menu\n\n"
        "<b>Navigation:</b>\n"
        "\u2022 <b>Devices</b> \u2014 Browse and control HA entities by domain\n"
        "\u2022 <b>Scenes</b> \u2014 Activate HA scenes\n"
        "\u2022 <b>Robots</b> \u2014 Control vacuums with room targeting\n"
        "\u2022 <b>Status</b> \u2014 View entity states\n\n"
        "All menus support pagination and back navigation."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2b05 Back", callback_data="nav:main")],
    ])
    return text, keyboard


def build_devices_menu(
    domains_with_counts: list[tuple[str, int]],
    show_all_enabled: bool = False,
    showing_all: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build device domain selector."""
    text = "\U0001f4a1 <b>Devices</b>\n\nSelect a device type:"
    rows: list[list[InlineKeyboardButton]] = []

    # 2 domains per row
    for i in range(0, len(domains_with_counts), 2):
        row: list[InlineKeyboardButton] = []
        for domain, count in domains_with_counts[i:i+2]:
            icon = DOMAIN_ICONS.get(domain, "\U0001f4e6")
            label = DOMAIN_LABELS.get(domain, domain.replace("_", " ").title())
            row.append(InlineKeyboardButton(
                text=f"{icon} {label} ({count})",
                callback_data=f"domain:{domain}:0",
            ))
        rows.append(row)

    # Show All toggle
    if show_all_enabled:
        if showing_all:
            rows.append([InlineKeyboardButton(
                text="\U0001f50d Show Default", callback_data="toggle:default"
            )])
        else:
            rows.append([InlineKeyboardButton(
                text="\U0001f50d Show All", callback_data="toggle:all"
            )])

    rows.append([InlineKeyboardButton(text="\u2b05 Back", callback_data="nav:main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, keyboard


def build_entity_list(
    domain: str,
    entities: list[dict[str, Any]],
    page: int,
    page_size: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build paginated entity list for a domain."""
    total = len(entities)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_entities = entities[start:end]

    icon = DOMAIN_ICONS.get(domain, "\U0001f4e6")
    label = DOMAIN_LABELS.get(domain, domain.replace("_", " ").title())
    text = f"{icon} <b>{label}</b> (page {page + 1}/{total_pages})\n\nSelect an entity:"

    rows: list[list[InlineKeyboardButton]] = []
    for ent in page_entities:
        eid = ent.get("entity_id", "")
        name = ent.get("attributes", {}).get("friendly_name", eid)
        state = ent.get("state", "unknown")
        # Truncate name if too long for button
        display = name[:28] if len(name) > 28 else name
        state_icon = _state_icon(domain, state)
        rows.append([InlineKeyboardButton(
            text=f"{state_icon} {display}",
            callback_data=f"entity:{eid}",
        )])

    # Pagination
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text="\u25c0 Prev", callback_data=f"domain:{domain}:{page - 1}"
        ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            text="Next \u25b6", callback_data=f"domain:{domain}:{page + 1}"
        ))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="\u2b05 Back", callback_data="nav:devices")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, keyboard


def build_entity_control(
    entity_id: str,
    state_data: dict[str, Any],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build control menu for a specific entity."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    attrs = state_data.get("attributes", {})
    name = attrs.get("friendly_name", entity_id)
    state = state_data.get("state", "unknown")
    icon = DOMAIN_ICONS.get(domain, "\U0001f4e6")

    text = f"{icon} <b>{_sanitize(name)}</b>\n\nState: <code>{_sanitize(state)}</code>"

    # Add relevant attributes
    extra_lines: list[str] = []
    if domain == "climate":
        temp = attrs.get("temperature")
        current = attrs.get("current_temperature")
        if current is not None:
            extra_lines.append(f"Current temp: {current}\u00b0")
        if temp is not None:
            extra_lines.append(f"Target temp: {temp}\u00b0")
        hvac_modes = attrs.get("hvac_modes", [])
        if hvac_modes:
            extra_lines.append(f"Modes: {', '.join(str(m) for m in hvac_modes)}")
    elif domain == "light":
        brightness = attrs.get("brightness")
        if brightness is not None:
            pct = round(brightness / 255 * 100)
            extra_lines.append(f"Brightness: {pct}%")
    elif domain == "cover":
        position = attrs.get("current_position")
        if position is not None:
            extra_lines.append(f"Position: {position}%")
    elif domain == "vacuum":
        battery = attrs.get("battery_level")
        if battery is not None:
            extra_lines.append(f"Battery: {battery}%")
        fan_speed = attrs.get("fan_speed")
        if fan_speed is not None:
            extra_lines.append(f"Fan speed: {fan_speed}")

    if extra_lines:
        text += "\n" + "\n".join(extra_lines)

    rows = _build_control_buttons(domain, entity_id, state, attrs)
    rows.append([InlineKeyboardButton(
        text="\u2b05 Back",
        callback_data=f"domain:{domain}:0",
    )])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, keyboard


def build_vacuum_rooms(
    entity_id: str,
    name: str,
    rooms: list[str],
    selected_room: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build room selection menu for vacuum."""
    text = f"\U0001f916 <b>{_sanitize(name)}</b>\n\nSelect a room to clean:"
    if selected_room:
        display_room = selected_room.replace("_", " ").title()
        text = (
            f"\U0001f916 <b>{_sanitize(name)}</b>\n"
            f"Target: {display_room}\n\n"
            "Press Start to begin cleaning."
        )

    rows: list[list[InlineKeyboardButton]] = []
    # Room buttons, 2 per row
    for i in range(0, len(rooms), 2):
        row: list[InlineKeyboardButton] = []
        for room in rooms[i:i+2]:
            display = room.replace("_", " ").title()
            marker = "\u2705 " if room == selected_room else ""
            row.append(InlineKeyboardButton(
                text=f"{marker}{display}",
                callback_data=f"vroom:{entity_id}:{room}",
            ))
        rows.append(row)

    # Start button (only if room selected or no rooms required)
    if selected_room or not rooms:
        rows.append([InlineKeyboardButton(
            text="\u25b6\ufe0f Start Cleaning",
            callback_data=f"vstart:{entity_id}:{selected_room or ''}",
        )])

    # Other vacuum actions
    rows.append([
        InlineKeyboardButton(
            text="\u23f9 Stop", callback_data=f"vcmd:{entity_id}:stop"
        ),
        InlineKeyboardButton(
            text="\U0001f3e0 Return to Base", callback_data=f"vcmd:{entity_id}:return_to_base"
        ),
    ])

    rows.append([InlineKeyboardButton(
        text="\u2b05 Back", callback_data="nav:robots"
    )])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, keyboard


def build_scenes_menu(
    scenes: list[dict[str, Any]],
    page: int,
    page_size: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build paginated scene list."""
    return _build_simple_entity_list("scene", scenes, page, page_size, "nav:main")


def build_robots_menu(
    vacuums: list[dict[str, Any]],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build vacuum entity list (no pagination, typically few)."""
    text = "\U0001f916 <b>Robots</b>\n\nSelect a vacuum:"

    rows: list[list[InlineKeyboardButton]] = []
    if not vacuums:
        text = "\U0001f916 <b>Robots</b>\n\nNo vacuum entities found in Home Assistant."
    else:
        for ent in vacuums:
            eid = ent.get("entity_id", "")
            name = ent.get("attributes", {}).get("friendly_name", eid)
            state = ent.get("state", "unknown")
            battery = ent.get("attributes", {}).get("battery_level")
            extra = f" ({battery}%)" if battery is not None else ""
            rows.append([InlineKeyboardButton(
                text=f"\U0001f916 {name[:28]}{extra} [{state}]",
                callback_data=f"vacuum:{eid}",
            )])

    rows.append([InlineKeyboardButton(text="\u2b05 Back", callback_data="nav:main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, keyboard


def build_status_menu(
    entities: list[dict[str, Any]],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build status display."""
    if not entities:
        text = "\U0001f4ca <b>Status</b>\n\nNo status entities configured."
    else:
        lines = ["\U0001f4ca <b>Status</b>\n"]
        for ent in entities:
            eid = ent.get("entity_id", "unknown")
            name = ent.get("attributes", {}).get("friendly_name", eid)
            state = ent.get("state", "unknown")
            unit = ent.get("attributes", {}).get("unit_of_measurement", "")
            unit_str = f" {_sanitize(str(unit))}" if unit else ""
            lines.append(f"\u2022 {_sanitize(name)}: <code>{_sanitize(state)}{unit_str}</code>")
        text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f504 Refresh", callback_data="menu:status")],
        [InlineKeyboardButton(text="\u2b05 Back", callback_data="nav:main")],
    ])
    return text, keyboard


def build_confirmation(
    message: str,
    back_callback: str = "nav:main",
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a simple confirmation/result display."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2b05 Back to Menu", callback_data=back_callback)],
    ])
    return message, keyboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_icon(domain: str, state: str) -> str:
    """Return a small status indicator."""
    if state in ("on", "open", "cleaning", "playing", "home"):
        return "\U0001f7e2"  # green circle
    if state in ("off", "closed", "docked", "idle", "paused", "standby"):
        return "\u26aa"  # white circle
    if state in ("unavailable", "unknown"):
        return "\U0001f534"  # red circle
    return "\U0001f535"  # blue circle


def _sanitize(text: str) -> str:
    """Escape HTML special chars in user-facing text from HA."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_control_buttons(
    domain: str,
    entity_id: str,
    state: str,
    attrs: dict[str, Any],
) -> list[list[InlineKeyboardButton]]:
    """Build domain-specific control buttons."""
    rows: list[list[InlineKeyboardButton]] = []

    if domain in ("light", "switch", "input_boolean", "fan"):
        rows.append([
            InlineKeyboardButton(
                text="\U0001f7e2 ON", callback_data=f"act:{entity_id}:turn_on"
            ),
            InlineKeyboardButton(
                text="\u26aa OFF", callback_data=f"act:{entity_id}:turn_off"
            ),
            InlineKeyboardButton(
                text="\U0001f504 Toggle", callback_data=f"act:{entity_id}:toggle"
            ),
        ])
        # Brightness for lights
        if domain == "light":
            supported = attrs.get("supported_color_modes", [])
            has_brightness = any(m != "onoff" for m in supported) if supported else False
            if has_brightness or attrs.get("brightness") is not None:
                rows.append([
                    InlineKeyboardButton(
                        text="\U0001f505 Dim", callback_data=f"bright:{entity_id}:down"
                    ),
                    InlineKeyboardButton(
                        text="\U0001f506 Bright", callback_data=f"bright:{entity_id}:up"
                    ),
                ])

    elif domain == "cover":
        rows.append([
            InlineKeyboardButton(
                text="\u2b06 Open", callback_data=f"act:{entity_id}:open_cover"
            ),
            InlineKeyboardButton(
                text="\u23f9 Stop", callback_data=f"act:{entity_id}:stop_cover"
            ),
            InlineKeyboardButton(
                text="\u2b07 Close", callback_data=f"act:{entity_id}:close_cover"
            ),
        ])

    elif domain == "climate":
        rows.append([
            InlineKeyboardButton(
                text="\u2b06 Temp +", callback_data=f"climate:{entity_id}:up"
            ),
            InlineKeyboardButton(
                text="\u2b07 Temp -", callback_data=f"climate:{entity_id}:down"
            ),
        ])

    elif domain == "vacuum":
        rows.append([
            InlineKeyboardButton(
                text="\u25b6 Start", callback_data=f"act:{entity_id}:start"
            ),
            InlineKeyboardButton(
                text="\u23f9 Stop", callback_data=f"act:{entity_id}:stop"
            ),
            InlineKeyboardButton(
                text="\U0001f3e0 Dock", callback_data=f"act:{entity_id}:return_to_base"
            ),
        ])
        # Room targeting link
        rows.append([InlineKeyboardButton(
            text="\U0001f3e0 Room Cleaning",
            callback_data=f"vacuum:{entity_id}",
        )])

    elif domain in ("scene", "script"):
        rows.append([InlineKeyboardButton(
            text="\u25b6\ufe0f Activate", callback_data=f"act:{entity_id}:turn_on"
        )])

    elif domain == "lock":
        rows.append([
            InlineKeyboardButton(
                text="\U0001f513 Unlock", callback_data=f"act:{entity_id}:unlock"
            ),
            InlineKeyboardButton(
                text="\U0001f512 Lock", callback_data=f"act:{entity_id}:lock"
            ),
        ])

    elif domain == "button":
        rows.append([InlineKeyboardButton(
            text="\U0001f518 Press", callback_data=f"act:{entity_id}:press"
        )])

    elif domain == "media_player":
        rows.append([
            InlineKeyboardButton(
                text="\u25b6 Play", callback_data=f"act:{entity_id}:media_play"
            ),
            InlineKeyboardButton(
                text="\u23f8 Pause", callback_data=f"act:{entity_id}:media_pause"
            ),
            InlineKeyboardButton(
                text="\u23f9 Stop", callback_data=f"act:{entity_id}:media_stop"
            ),
        ])

    else:
        # Generic toggle for unknown domains
        rows.append([
            InlineKeyboardButton(
                text="\U0001f7e2 ON", callback_data=f"act:{entity_id}:turn_on"
            ),
            InlineKeyboardButton(
                text="\u26aa OFF", callback_data=f"act:{entity_id}:turn_off"
            ),
        ])

    return rows


def _build_simple_entity_list(
    domain: str,
    entities: list[dict[str, Any]],
    page: int,
    page_size: int,
    back_callback: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Generic paginated entity list builder."""
    total = len(entities)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_entities = entities[start:end]

    icon = DOMAIN_ICONS.get(domain, "\U0001f4e6")
    label = DOMAIN_LABELS.get(domain, domain.replace("_", " ").title())
    text = f"{icon} <b>{label}</b> (page {page + 1}/{total_pages})\n\nSelect to activate:"

    rows: list[list[InlineKeyboardButton]] = []
    for ent in page_entities:
        eid = ent.get("entity_id", "")
        name = ent.get("attributes", {}).get("friendly_name", eid)
        display = name[:32] if len(name) > 32 else name
        rows.append([InlineKeyboardButton(
            text=f"{icon} {display}",
            callback_data=f"act:{eid}:turn_on",
        )])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text="\u25c0 Prev", callback_data=f"scenes_page:{page - 1}"
        ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            text="Next \u25b6", callback_data=f"scenes_page:{page + 1}"
        ))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="\u2b05 Back", callback_data=back_callback)])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, keyboard
