"""Home Assistant registry sync via WebSocket API.

Fetches floors, areas, devices, and entity registries from HA Core
through the Supervisor WebSocket proxy.  Builds in-memory mappings
for the bot's menu navigation (floors -> areas -> entities).

Also detects vacuum segments/rooms and Roborock routine button entities.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from storage import Database

logger = logging.getLogger("ha_bot.registry")

HA_WS_URL = "ws://supervisor/core/websocket"

# ---------------------------------------------------------------------------
# Room name aliases for RU/EN matching
# ---------------------------------------------------------------------------

ROOM_ALIASES: dict[str, list[str]] = {
    "кухня": ["kitchen", "кухни"],
    "гостиная": ["living_room", "living room", "гостинная", "зал"],
    "спальня": ["bedroom", "спальни"],
    "прихожка": ["прихожая", "коридор", "hallway", "corridor", "hall"],
    "ванная": ["bathroom", "ванна", "ванной", "bath"],
    "детская": ["nursery", "children", "child_room"],
    "кабинет": ["office", "study"],
    "балкон": ["balcony"],
    "туалет": ["toilet", "wc"],
    "столовая": ["dining_room", "dining"],
}


def _normalize(name: str) -> str:
    """Lowercase, strip, remove special chars for name matching."""
    name = name.lower().strip()
    name = re.sub(r"[^a-zа-яё0-9\s_]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FloorInfo:
    floor_id: str
    name: str
    level: int = 0
    area_ids: list[str] = field(default_factory=list)


@dataclass
class AreaInfo:
    area_id: str
    name: str
    floor_id: str | None = None
    entity_ids: list[str] = field(default_factory=list)


@dataclass
class DeviceInfo:
    device_id: str
    name: str
    area_id: str | None = None
    manufacturer: str = ""
    model: str = ""


@dataclass
class EntityInfo:
    entity_id: str
    name: str | None = None
    original_name: str | None = None
    platform: str = ""
    device_id: str | None = None
    area_id: str | None = None
    disabled_by: str | None = None
    hidden_by: str | None = None
    translation_key: str | None = None
    entity_category: str | None = None  # "diagnostic", "config", or None (primary)


# ---------------------------------------------------------------------------
# Registry class
# ---------------------------------------------------------------------------


class HARegistry:
    """Fetches and caches HA registry data via WebSocket."""

    def __init__(self, supervisor_token: str, db: Database) -> None:
        self._token = supervisor_token
        self._db = db

        # In-memory caches
        self.floors: dict[str, FloorInfo] = {}
        self.areas: dict[str, AreaInfo] = {}
        self.devices: dict[str, DeviceInfo] = {}
        self.entities: dict[str, EntityInfo] = {}

        # Vacuum-specific
        self.vacuum_routines: dict[str, list[str]] = {}   # vacuum_eid -> [button_eid, ...]
        self.vacuum_platforms: dict[str, str] = {}          # vacuum_eid -> platform

        self._synced = False

    @property
    def has_floors(self) -> bool:
        return bool(self.floors)

    @property
    def synced(self) -> bool:
        return self._synced

    # -------------------------------------------------------------------
    # WebSocket helpers
    # -------------------------------------------------------------------

    async def _ws_command(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        cmd_id: int,
        cmd_type: str,
    ) -> list[dict[str, Any]]:
        """Send a WS command and return result list."""
        await ws.send_json({"id": cmd_id, "type": cmd_type})
        for _ in range(20):  # tolerate interleaved messages
            msg = await asyncio.wait_for(ws.receive(), timeout=15)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("id") == cmd_id:
                    if data.get("success"):
                        result = data.get("result", [])
                        return result if isinstance(result, list) else []
                    logger.warning("WS cmd %s failed: %s", cmd_type, data.get("error"))
                    return []
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                logger.error("WS closed unexpectedly during %s", cmd_type)
                return []
        logger.warning("WS cmd %s: no matching response", cmd_type)
        return []

    # -------------------------------------------------------------------
    # Main sync
    # -------------------------------------------------------------------

    async def sync(self) -> bool:
        """Connect to HA WS, fetch registries, build all mappings."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(HA_WS_URL, timeout=aiohttp.ClientTimeout(total=30)) as ws:
                    # --- auth handshake ---
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        logger.error("WS: unexpected msg type on connect: %s", msg.type)
                        return False
                    data = json.loads(msg.data)
                    if data.get("type") != "auth_required":
                        logger.error("WS: expected auth_required, got %s", data.get("type"))
                        return False

                    await ws.send_json({"type": "auth", "access_token": self._token})
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    data = json.loads(msg.data)
                    if data.get("type") != "auth_ok":
                        logger.error("WS auth failed: %s", data)
                        return False

                    logger.info("WS authenticated, fetching registries...")
                    cmd_id = 1

                    # Floors (may fail on older HA)
                    floors_raw = await self._ws_command(ws, cmd_id, "config/floor_registry/list")
                    cmd_id += 1

                    areas_raw = await self._ws_command(ws, cmd_id, "config/area_registry/list")
                    cmd_id += 1

                    devices_raw = await self._ws_command(ws, cmd_id, "config/device_registry/list")
                    cmd_id += 1

                    entities_raw = await self._ws_command(ws, cmd_id, "config/entity_registry/list")
                    cmd_id += 1

            # --- process outside WS connection ---
            self._process_floors(floors_raw)
            self._process_areas(areas_raw)
            self._process_devices(devices_raw)
            self._process_entities(entities_raw)
            self._build_cross_refs()
            self._detect_vacuum_routines()
            await self._populate_entity_area_cache()
            await self._detect_vacuum_segments()

            self._synced = True
            logger.info(
                "Registry sync OK: %d floors, %d areas, %d devices, %d entities",
                len(self.floors), len(self.areas), len(self.devices), len(self.entities),
            )
            return True

        except asyncio.TimeoutError:
            logger.error("Registry sync timed out")
            return False
        except Exception:
            logger.exception("Registry sync failed")
            return False

    # -------------------------------------------------------------------
    # Processing raw data
    # -------------------------------------------------------------------

    def _process_floors(self, data: list[dict]) -> None:
        self.floors.clear()
        for item in data:
            fid = item.get("floor_id", "")
            if fid:
                self.floors[fid] = FloorInfo(
                    floor_id=fid,
                    name=item.get("name", fid),
                    level=item.get("level") or 0,
                )

    def _process_areas(self, data: list[dict]) -> None:
        self.areas.clear()
        for item in data:
            aid = item.get("area_id", "")
            if aid:
                self.areas[aid] = AreaInfo(
                    area_id=aid,
                    name=item.get("name", aid),
                    floor_id=item.get("floor_id"),
                )

    def _process_devices(self, data: list[dict]) -> None:
        self.devices.clear()
        for item in data:
            did = item.get("id", "")
            if did:
                self.devices[did] = DeviceInfo(
                    device_id=did,
                    name=item.get("name_by_user") or item.get("name", did),
                    area_id=item.get("area_id"),
                    manufacturer=item.get("manufacturer") or "",
                    model=item.get("model") or "",
                )

    def _process_entities(self, data: list[dict]) -> None:
        self.entities.clear()
        for item in data:
            eid = item.get("entity_id", "")
            if eid:
                self.entities[eid] = EntityInfo(
                    entity_id=eid,
                    name=item.get("name"),
                    original_name=item.get("original_name"),
                    platform=item.get("platform", ""),
                    device_id=item.get("device_id"),
                    area_id=item.get("area_id"),
                    disabled_by=item.get("disabled_by"),
                    hidden_by=item.get("hidden_by"),
                    translation_key=item.get("translation_key"),
                    entity_category=item.get("entity_category"),
                )

    # -------------------------------------------------------------------
    # Cross-references
    # -------------------------------------------------------------------

    def _build_cross_refs(self) -> None:
        """Link areas to floors and entities to areas."""
        # Reset lists
        for f in self.floors.values():
            f.area_ids = []
        for a in self.areas.values():
            a.entity_ids = []

        # Areas -> floors
        for area in self.areas.values():
            if area.floor_id and area.floor_id in self.floors:
                self.floors[area.floor_id].area_ids.append(area.area_id)

        # Entities -> areas (entity.area_id takes priority, else device.area_id)
        for ent in self.entities.values():
            if ent.disabled_by:
                continue
            area_id = ent.area_id
            if not area_id and ent.device_id:
                dev = self.devices.get(ent.device_id)
                if dev:
                    area_id = dev.area_id
            if area_id and area_id in self.areas:
                self.areas[area_id].entity_ids.append(ent.entity_id)

    # -------------------------------------------------------------------
    # Vacuum: detect routines (button entities on same device)
    # -------------------------------------------------------------------

    def _detect_vacuum_routines(self) -> None:
        self.vacuum_routines.clear()
        self.vacuum_platforms.clear()

        # Map device_id -> vacuum_entity_id
        dev_to_vacuum: dict[str, str] = {}
        for ent in self.entities.values():
            if ent.entity_id.startswith("vacuum.") and ent.device_id and not ent.disabled_by:
                dev_to_vacuum[ent.device_id] = ent.entity_id
                self.vacuum_platforms[ent.entity_id] = ent.platform

        # Find button entities on vacuum devices
        _SYSTEM_KW = frozenset({
            "identify", "reset", "restart", "update", "firmware",
            "обновлен", "сброс", "перезагр", "calibrat",
        })
        _ROUTINE_KW = frozenset({
            "routine", "scenario", "сценарий", "рутин", "scene",
            "clean", "уборк", "mop", "program", "schedule",
        })

        for ent in self.entities.values():
            if not ent.entity_id.startswith("button."):
                continue
            if ent.disabled_by:
                continue
            if ent.device_id not in dev_to_vacuum:
                continue

            vacuum_eid = dev_to_vacuum[ent.device_id]
            name_lower = _normalize(ent.original_name or ent.name or ent.entity_id)
            tk = (ent.translation_key or "").lower()

            is_system = any(kw in name_lower for kw in _SYSTEM_KW)
            is_routine = (
                any(kw in name_lower for kw in _ROUTINE_KW)
                or any(kw in tk for kw in _ROUTINE_KW)
            )

            # Include non-system buttons and all routine-heuristic buttons
            if is_routine or not is_system:
                self.vacuum_routines.setdefault(vacuum_eid, []).append(ent.entity_id)

        for vac_eid, btns in self.vacuum_routines.items():
            logger.info("Vacuum %s: %d routine button(s) found", vac_eid, len(btns))

    # -------------------------------------------------------------------
    # Vacuum: detect segments / rooms
    # -------------------------------------------------------------------

    async def _detect_vacuum_segments(self) -> None:
        """Try to import vacuum room segments.  Best-effort, never crashes."""
        for vac_eid, platform in self.vacuum_platforms.items():
            logger.info(
                "Vacuum %s platform=%s — attempting segment import",
                vac_eid, platform,
            )
            # For Roborock integration, segments may come from select entities
            # on the same device, or from vacuum attributes.
            # We look for number/select entities with 'segment' or 'room' in name.
            device_id = self.entities.get(vac_eid, EntityInfo(entity_id=vac_eid)).device_id
            if not device_id:
                logger.info("Vacuum %s has no device_id, skipping segment import", vac_eid)
                continue

            segments: list[dict[str, Any]] = []

            # Heuristic 1: look for entities with rooms/segments info in the entity_id
            # e.g. select.roborock_s7_rooms, number.roborock_s7_room_*
            for ent in self.entities.values():
                if ent.device_id != device_id:
                    continue
                if ent.disabled_by:
                    continue
                eid_lower = ent.entity_id.lower()
                name_lower = _normalize(ent.original_name or ent.name or "")
                # Roborock often has entities like sensor.roborock_*_room_*
                # or the vacuum attributes contain room info
                # We collect "room" related entities but this is best-effort
                if "room" in eid_lower or "segment" in eid_lower:
                    logger.debug("Segment candidate entity: %s", ent.entity_id)

            # Heuristic 2: use configured vacuum_room_presets (from config)
            # This is handled at the handler level, not here

            if segments:
                await self._db.save_vacuum_room_map(vac_eid, segments)
                logger.info("Vacuum %s: imported %d segments", vac_eid, len(segments))
            else:
                logger.info(
                    "Vacuum %s: no segments auto-detected (room cleaning via presets or unavailable)",
                    vac_eid,
                )

    # -------------------------------------------------------------------
    # Persist entity -> area cache in DB
    # -------------------------------------------------------------------

    async def _populate_entity_area_cache(self) -> None:
        """Write entity->area->floor mapping to DB cache."""
        await self._db.flush_entity_area_cache()
        for ent in self.entities.values():
            if ent.disabled_by:
                continue
            area_id = ent.area_id
            if not area_id and ent.device_id:
                dev = self.devices.get(ent.device_id)
                if dev:
                    area_id = dev.area_id

            area_name: str | None = None
            floor_id: str | None = None
            floor_name: str | None = None
            if area_id:
                area_obj = self.areas.get(area_id)
                if area_obj:
                    area_name = area_obj.name
                    floor_id = area_obj.floor_id
                    if floor_id:
                        floor_obj = self.floors.get(floor_id)
                        if floor_obj:
                            floor_name = floor_obj.name

            await self._db.cache_entity_area(
                ent.entity_id, area_id, area_name, floor_id, floor_name, ent.device_id,
            )
        await self._db.commit_entity_area_cache()

    # -------------------------------------------------------------------
    # Query helpers
    # -------------------------------------------------------------------

    def get_entity_area_id(self, entity_id: str) -> str | None:
        """Get area_id for entity (entity direct or via device)."""
        ent = self.entities.get(entity_id)
        if not ent:
            return None
        if ent.area_id:
            return ent.area_id
        if ent.device_id:
            dev = self.devices.get(ent.device_id)
            if dev:
                return dev.area_id
        return None

    def get_floors_sorted(self) -> list[FloorInfo]:
        return sorted(self.floors.values(), key=lambda f: (f.level, f.name))

    def get_areas_for_floor(self, floor_id: str) -> list[AreaInfo]:
        floor = self.floors.get(floor_id)
        if not floor:
            return []
        return sorted(
            [self.areas[aid] for aid in floor.area_ids if aid in self.areas],
            key=lambda a: a.name,
        )

    def get_unassigned_areas(self) -> list[AreaInfo]:
        """Areas not assigned to any floor."""
        assigned = set()
        for f in self.floors.values():
            assigned.update(f.area_ids)
        return sorted(
            [a for a in self.areas.values() if a.area_id not in assigned],
            key=lambda a: a.name,
        )

    def get_all_areas_sorted(self) -> list[AreaInfo]:
        return sorted(self.areas.values(), key=lambda a: a.name)

    def get_area_entities(
        self, area_id: str, domains: frozenset[str] | None = None,
        show_all: bool = False,
    ) -> list[str]:
        """Entity IDs in an area, optionally filtered by domain.

        When show_all is False, hides entities with entity_category
        "diagnostic" or "config" (these are usually junk sensors).
        """
        area = self.areas.get(area_id)
        if not area:
            return []
        eids = area.entity_ids
        if domains:
            eids = [e for e in eids if e.split(".", 1)[0] in domains]
        if not show_all:
            eids = [
                e for e in eids
                if not (self.entities.get(e) and self.entities[e].entity_category in ("diagnostic", "config"))
            ]
        return sorted(eids)

    def get_unassigned_entities(
        self, domains: frozenset[str] | None = None, show_all: bool = False,
    ) -> list[str]:
        """Entities not in any area."""
        assigned: set[str] = set()
        for area in self.areas.values():
            assigned.update(area.entity_ids)
        result = []
        for ent in self.entities.values():
            if ent.entity_id not in assigned and not ent.disabled_by:
                if domains and ent.entity_id.split(".", 1)[0] not in domains:
                    continue
                if not show_all and ent.entity_category in ("diagnostic", "config"):
                    continue
                result.append(ent.entity_id)
        return sorted(result)

    def get_entity_display_name(self, entity_id: str) -> str:
        """Best available display name for an entity."""
        ent = self.entities.get(entity_id)
        if ent:
            return ent.name or ent.original_name or entity_id
        return entity_id

    def match_segment_to_area(self, segment_name: str) -> str | None:
        """Try to match a vacuum segment name to an HA area_id via aliases."""
        norm = _normalize(segment_name)

        # Direct match
        for area in self.areas.values():
            if _normalize(area.name) == norm:
                return area.area_id

        # Alias match
        for _canonical, aliases in ROOM_ALIASES.items():
            all_names = [_canonical] + aliases
            all_norm = {_normalize(n) for n in all_names}
            if norm in all_norm:
                for area in self.areas.values():
                    if _normalize(area.name) in all_norm:
                        return area.area_id
        return None

    # -------------------------------------------------------------------
    # Device grouping for area menus
    # -------------------------------------------------------------------

    _DOMAIN_PRIORITY: list[str] = [
        "vacuum", "media_player", "climate", "light", "cover", "fan",
        "switch", "lock", "water_heater", "scene", "script", "select",
        "number", "button", "sensor", "binary_sensor",
    ]
    _DOMAIN_RANK: dict[str, int] = {d: i for i, d in enumerate(_DOMAIN_PRIORITY)}

    _JUNK_SUFFIXES: tuple[str, ...] = (
        "_signal_strength", "_linkquality", "_link_quality",
        "_connectivity", "_rssi", "_battery_level",
        "_ip_address", "_mac_address", "_firmware",
    )

    def _pick_primary_domain(self, entity_ids: list[str]) -> str:
        """Pick the most relevant domain from a list of entity IDs for icon display."""
        domains = {eid.split(".", 1)[0] for eid in entity_ids}
        for d in self._DOMAIN_PRIORITY:
            if d in domains:
                return d
        return next(iter(domains), "unknown")

    def _pick_primary_entity(self, eids: list[str]) -> str:
        """Pick the most representative entity for a device.

        Uses domain priority, penalises junk suffixes and diagnostic entities.
        """
        if len(eids) <= 1:
            return eids[0] if eids else ""

        rank_limit = len(self._DOMAIN_PRIORITY)

        def _key(eid: str) -> tuple[int, int, str]:
            domain = eid.split(".", 1)[0]
            domain_rank = self._DOMAIN_RANK.get(domain, rank_limit)
            # Penalise junk entities (connectivity, signal, etc.)
            suffix = eid.split(".", 1)[1] if "." in eid else ""
            penalty = 0
            if any(suffix.endswith(j) for j in self._JUNK_SUFFIXES):
                penalty = 1
            ent = self.entities.get(eid)
            if ent and ent.entity_category in ("diagnostic", "config"):
                penalty = 2
            return (domain_rank, penalty, eid)

        return min(eids, key=_key)

    def get_devices_for_area(
        self, area_id: str, domains: frozenset[str] | None = None,
        show_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Group entities in an area by device_id for display.

        Returns sorted list of dicts:
          {device_id, name, entity_ids, primary_entity_id, primary_domain, is_vacuum}

        Entities without a device_id get a virtual device_id = entity_id.
        """
        area = self.areas.get(area_id)
        if not area:
            return []

        raw_eids = area.entity_ids
        if domains:
            raw_eids = [e for e in raw_eids if e.split(".", 1)[0] in domains]
        if not show_all:
            raw_eids = [
                e for e in raw_eids
                if not (self.entities.get(e) and self.entities[e].entity_category in ("diagnostic", "config"))
            ]

        # Group by device_id
        device_entities: dict[str, list[str]] = {}
        for eid in raw_eids:
            ent = self.entities.get(eid)
            dev_id = ent.device_id if ent and ent.device_id else eid
            device_entities.setdefault(dev_id, []).append(eid)

        result: list[dict[str, Any]] = []
        for dev_id, eids in device_entities.items():
            dev = self.devices.get(dev_id)
            is_vacuum = any(e.startswith("vacuum.") for e in eids)

            # For vacuum devices, filter out companion entities from the visible list
            # but keep them internally for the device view
            primary = self._pick_primary_entity(eids)

            name = dev.name if dev else self.get_entity_display_name(primary)
            primary_domain = self._pick_primary_domain(eids)

            result.append({
                "device_id": dev_id,
                "name": name,
                "entity_ids": sorted(eids),
                "primary_entity_id": primary,
                "primary_domain": primary_domain,
                "is_vacuum": is_vacuum,
            })

        # Sort: vacuums first, then by name
        result.sort(key=lambda d: (not d["is_vacuum"], d["name"].lower()))
        return result

    def get_unassigned_devices(
        self, domains: frozenset[str] | None = None,
        show_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Group unassigned entities by device_id (same format as get_devices_for_area)."""
        unassigned_eids = self.get_unassigned_entities(domains, show_all=show_all)
        if not unassigned_eids:
            return []

        device_entities: dict[str, list[str]] = {}
        for eid in unassigned_eids:
            ent = self.entities.get(eid)
            dev_id = ent.device_id if ent and ent.device_id else eid
            device_entities.setdefault(dev_id, []).append(eid)

        result: list[dict[str, Any]] = []
        for dev_id, eids in device_entities.items():
            dev = self.devices.get(dev_id)
            is_vacuum = any(e.startswith("vacuum.") for e in eids)
            primary = self._pick_primary_entity(eids)
            name = dev.name if dev else self.get_entity_display_name(primary)
            primary_domain = self._pick_primary_domain(eids)
            result.append({
                "device_id": dev_id,
                "name": name,
                "entity_ids": sorted(eids),
                "primary_entity_id": primary,
                "primary_domain": primary_domain,
                "is_vacuum": is_vacuum,
            })
        result.sort(key=lambda d: (not d["is_vacuum"], d["name"].lower()))
        return result

    def get_device_entity_ids(
        self, device_id: str, domains: frozenset[str] | None = None,
    ) -> list[str]:
        """Get entity IDs belonging to a device, filtered by domains."""
        eids: list[str] = []
        for ent in self.entities.values():
            if ent.disabled_by:
                continue
            if ent.device_id == device_id:
                if domains and ent.entity_id.split(".", 1)[0] not in domains:
                    continue
                eids.append(ent.entity_id)
        return sorted(eids)

    def is_vacuum_device(self, device_id: str) -> bool:
        """Check if device has a vacuum entity."""
        for ent in self.entities.values():
            if ent.device_id == device_id and ent.entity_id.startswith("vacuum.") and not ent.disabled_by:
                return True
        return False

    def get_vacuum_entity_for_device(self, device_id: str) -> str | None:
        """Get the vacuum entity_id for a device, if any."""
        for ent in self.entities.values():
            if ent.device_id == device_id and ent.entity_id.startswith("vacuum.") and not ent.disabled_by:
                return ent.entity_id
        return None
