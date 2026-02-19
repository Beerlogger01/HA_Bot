"""Domain-aware state mapping for entity state interpretation.

Maps raw HA state strings to normalized UI states with domain-specific rules.
Supports optional per-entity overrides from config.

No imports from other project modules — pure functions only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MappedState:
    """Normalized state for UI display."""

    ui_state: str       # "ON" | "OFF" | "IDLE" | "RUNNING" | "UNAVAILABLE" | raw
    ui_label: str       # Human-readable label
    is_active: bool     # Should this show as "active" in Active Now?
    is_read_only: bool  # No action buttons should be rendered


# ---------------------------------------------------------------------------
# Per-domain definitions
# ---------------------------------------------------------------------------

# States that mean the entity is actively doing something
_DOMAIN_ACTIVE: dict[str, frozenset[str]] = {
    "light":          frozenset({"on"}),
    "switch":         frozenset({"on"}),
    "fan":            frozenset({"on"}),
    "input_boolean":  frozenset({"on"}),
    "cover":          frozenset({"open", "opening", "closing"}),
    "lock":           frozenset({"unlocked"}),
    "vacuum":         frozenset({"cleaning", "returning"}),
    "media_player":   frozenset({"playing", "buffering"}),
    "climate":        frozenset({"heating", "cooling", "drying",
                                 "heat", "cool", "heat_cool", "auto"}),
    "water_heater":   frozenset({"heating", "on"}),
    "binary_sensor":  frozenset({"on"}),
}

_INACTIVE_STATES: frozenset[str] = frozenset({
    "off", "closed", "docked", "idle", "standby", "paused",
    "locked", "not_home", "below_horizon",
})

_UNAVAILABLE_STATES: frozenset[str] = frozenset({"unavailable", "unknown"})

_READ_ONLY_DOMAINS: frozenset[str] = frozenset({
    "sensor", "binary_sensor", "event",
})

# Russian labels for common HA state strings
_STATE_LABELS: dict[str, str] = {
    "on": "Вкл",
    "off": "Выкл",
    "unavailable": "Недоступен",
    "unknown": "Неизвестно",
    "cleaning": "Уборка",
    "returning": "Возврат",
    "docked": "На базе",
    "idle": "Простой",
    "paused": "Пауза",
    "playing": "Воспроизведение",
    "buffering": "Буферизация",
    "standby": "Ожидание",
    "heating": "Нагрев",
    "cooling": "Охлаждение",
    "drying": "Сушка",
    "open": "Открыто",
    "opening": "Открывается",
    "closed": "Закрыто",
    "closing": "Закрывается",
    "locked": "Заблокирован",
    "unlocked": "Разблокирован",
    "home": "Дома",
    "not_home": "Нет дома",
    "heat": "Обогрев",
    "cool": "Охлаждение",
    "auto": "Авто",
}


# ---------------------------------------------------------------------------
# Primary entity junk detection
# ---------------------------------------------------------------------------

_JUNK_SUFFIXES: tuple[str, ...] = (
    "_signal_strength", "_linkquality", "_link_quality",
    "_connectivity", "_rssi", "_battery_level",
    "_ip_address", "_mac_address", "_firmware",
)


def is_junk_primary(entity_id: str) -> bool:
    """Return True if entity is a connectivity/diagnostic sensor
    that should not be a device's primary representative."""
    suffix = entity_id.split(".", 1)[1] if "." in entity_id else ""
    return any(suffix.endswith(j) for j in _JUNK_SUFFIXES)


# ---------------------------------------------------------------------------
# Main mapping function
# ---------------------------------------------------------------------------


def map_state(
    entity_id: str,
    state: str,
    attrs: dict[str, Any] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> MappedState:
    """Map raw HA entity state to a normalized MappedState.

    Args:
        entity_id: Full entity ID (e.g. "switch.kettle").
        state: Raw state string from HA.
        attrs: Entity attributes dict (optional).
        overrides: Per-entity override config keyed by entity_id (optional).

    Returns:
        MappedState with normalized fields.
    """
    domain = entity_id.split(".", 1)[0]
    attrs = attrs or {}

    # --- unavailable / unknown first ---
    if state in _UNAVAILABLE_STATES:
        return MappedState(
            ui_state="UNAVAILABLE",
            ui_label=_STATE_LABELS.get(state, state),
            is_active=False,
            is_read_only=domain in _READ_ONLY_DOMAINS,
        )

    # --- per-entity overrides ---
    if overrides and entity_id in overrides:
        ovr = overrides[entity_id]

        # Power threshold override (for dishwasher-style sensors)
        threshold = ovr.get("running_threshold_watts")
        if threshold is not None:
            power = _extract_power(state, attrs)
            if power is not None:
                is_running = power > float(threshold)
                label = f"{power:.0f} W" if is_running else _STATE_LABELS.get(state, state)
                return MappedState(
                    "RUNNING" if is_running else "IDLE",
                    label, is_running,
                    domain in _READ_ONLY_DOMAINS,
                )

        # Explicit active/idle state lists
        active_states = ovr.get("active_states")
        idle_states = ovr.get("idle_states")
        if active_states and state in active_states:
            return MappedState("ON", _STATE_LABELS.get(state, state), True, False)
        if idle_states and state in idle_states:
            return MappedState("IDLE", _STATE_LABELS.get(state, state), False, False)

    # --- domain-specific active check ---
    domain_active = _DOMAIN_ACTIVE.get(domain)
    if domain_active is not None:
        is_active = state in domain_active
    else:
        # Unknown domain: only "on" is active
        is_active = state == "on"

    # --- derive ui_state ---
    if is_active:
        ui_state = "ON"
    elif state in _INACTIVE_STATES:
        ui_state = "OFF"
    else:
        ui_state = state.upper()

    return MappedState(
        ui_state=ui_state,
        ui_label=_STATE_LABELS.get(state, state),
        is_active=is_active,
        is_read_only=domain in _READ_ONLY_DOMAINS,
    )


def _extract_power(state: str, attrs: dict[str, Any]) -> float | None:
    """Try to extract numeric power value from state or attributes."""
    # For sensor entities, state IS the numeric value
    try:
        return float(state)
    except (ValueError, TypeError):
        pass
    for key in ("current_power_w", "power", "current_power"):
        val = attrs.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None
