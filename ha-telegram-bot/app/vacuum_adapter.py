"""Vacuum adapter — abstraction for vacuum capabilities and operations.

Detects vacuum capabilities (segment cleaning, map import, routines)
and provides a unified interface for all vacuum operations.

All public methods are wrapped in try/except to prevent HTTP 500 / stacktraces
from reaching the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from api import HAClient
from registry import HARegistry
from storage import Database

logger = logging.getLogger("ha_bot.vacuum")


@dataclass(frozen=True, slots=True)
class VacuumCapabilities:
    supports_segment_clean: bool = False
    supports_routines: bool = False
    platform: str = ""
    segment_count: int = 0
    routine_count: int = 0


class VacuumAdapter:
    """Unified vacuum interface with capability detection."""

    def __init__(
        self, ha: HAClient, db: Database, registry: HARegistry,
        strategy: str, script_entity_id: str, presets: tuple[str, ...],
    ) -> None:
        self._ha = ha
        self._db = db
        self._reg = registry
        self._strategy = strategy
        self._script_eid = script_entity_id
        self._presets = presets

    async def get_capabilities(self, vacuum_eid: str) -> VacuumCapabilities:
        """Detect capabilities for a specific vacuum entity."""
        try:
            platform = self._reg.vacuum_platforms.get(vacuum_eid, "")
            routines = self._reg.vacuum_routines.get(vacuum_eid, [])
            segments = await self._db.get_vacuum_room_map(vacuum_eid)

            seg_count = len(segments) if segments else len(self._presets)
            return VacuumCapabilities(
                supports_segment_clean=seg_count > 0 or self._strategy == "script",
                supports_routines=len(routines) > 0,
                platform=platform,
                segment_count=seg_count,
                routine_count=len(routines),
            )
        except Exception:
            logger.exception("Failed to get vacuum capabilities for %s", vacuum_eid)
            return VacuumCapabilities()

    async def get_rooms(self, vacuum_eid: str) -> list[dict[str, Any]]:
        """Get available rooms/segments for vacuum."""
        try:
            segments = await self._db.get_vacuum_room_map(vacuum_eid)
            if segments:
                return segments
            # Fall back to config presets
            return [
                {"segment_id": r, "segment_name": r.replace("_", " ").title()}
                for r in self._presets
            ]
        except Exception:
            logger.exception("Failed to get vacuum rooms for %s", vacuum_eid)
            return []

    async def clean_segment(
        self, vacuum_eid: str, segment_id: str,
    ) -> tuple[bool, str]:
        """Start segment/room cleaning. Returns (success, error)."""
        try:
            # Real segment (numeric ID from integration)
            try:
                seg_int = int(segment_id)
                ok, err = await self._ha.call_service(
                    "vacuum", "send_command",
                    {"entity_id": vacuum_eid, "command": "app_segment_clean", "params": [seg_int]},
                )
                return ok, err
            except ValueError:
                pass

            # Named segment via configured strategy
            if self._strategy == "script" and self._script_eid:
                ok, err = await self._ha.call_service(
                    "script", "turn_on",
                    {
                        "entity_id": self._script_eid,
                        "variables": {"vacuum_entity": vacuum_eid, "room": segment_id},
                    },
                )
                return ok, err

            if self._strategy == "service_data":
                ok, err = await self._ha.call_service(
                    "vacuum", "send_command",
                    {"entity_id": vacuum_eid, "command": "app_segment_clean",
                     "params": {"rooms": [segment_id]}},
                )
                return ok, err

            # Fallback: just start vacuum
            return await self._ha.call_service("vacuum", "start", {"entity_id": vacuum_eid})
        except Exception as exc:
            logger.exception("Failed to clean segment %s for %s", segment_id, vacuum_eid)
            return False, f"Ошибка уборки: {exc}"

    async def execute_command(
        self, vacuum_eid: str, command: str,
    ) -> tuple[bool, str]:
        """Execute a vacuum command (start, stop, pause, return_to_base, locate)."""
        try:
            if command not in ("start", "stop", "pause", "return_to_base", "locate"):
                return False, f"Unknown vacuum command: {command}"
            return await self._ha.call_service("vacuum", command, {"entity_id": vacuum_eid})
        except Exception as exc:
            logger.exception("Failed to execute vacuum command %s for %s", command, vacuum_eid)
            return False, f"Ошибка команды: {exc}"

    async def press_routine(self, button_eid: str) -> tuple[bool, str]:
        """Press a routine button entity."""
        try:
            return await self._ha.call_service("button", "press", {"entity_id": button_eid})
        except Exception as exc:
            logger.exception("Failed to press routine %s", button_eid)
            return False, f"Ошибка сценария: {exc}"

    async def get_routines(self, vacuum_eid: str) -> list[dict[str, Any]]:
        """Get routine button entities for a vacuum."""
        try:
            routine_eids = self._reg.vacuum_routines.get(vacuum_eid, [])
            routines: list[dict[str, Any]] = []
            for r_eid in routine_eids:
                state = await self._ha.get_state(r_eid)
                r_name = r_eid
                if state:
                    r_name = state.get("attributes", {}).get("friendly_name", r_eid)
                routines.append({"entity_id": r_eid, "name": r_name})
            return routines
        except Exception:
            logger.exception("Failed to get vacuum routines for %s", vacuum_eid)
            return []

    async def get_status(self, vacuum_eid: str) -> dict[str, Any]:
        """Get vacuum status summary. Never raises."""
        try:
            state = await self._ha.get_state(vacuum_eid)
            if not state:
                return {"state": "unavailable", "battery": "?", "error": None}
            attrs = state.get("attributes", {})
            return {
                "state": state.get("state", "unknown"),
                "battery": attrs.get("battery_level", "?"),
                "fan_speed": attrs.get("fan_speed", ""),
                "status": attrs.get("status", state.get("state", "")),
                "error": attrs.get("error"),
                "friendly_name": attrs.get("friendly_name", vacuum_eid),
            }
        except Exception:
            logger.exception("Failed to get vacuum status for %s", vacuum_eid)
            return {"state": "error", "battery": "?", "error": "Не удалось получить статус"}

    def get_segment_display_name(
        self, segment_id: str, segments: list[dict[str, Any]],
    ) -> str:
        """Get display name for a segment ID."""
        for s in segments:
            if s["segment_id"] == segment_id:
                return s.get("segment_name") or segment_id
        return segment_id.replace("_", " ").title()
