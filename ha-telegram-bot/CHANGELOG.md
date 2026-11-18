# Changelog

## 2.4.0

- Personal persistent UI sessions by chat, user and forum topic; message-bound opaque buttons reject stale/foreign screens. Legacy SQLite data remains intact.
- Compact home overview, direct favorite actions, capability-aware cards, climate/humidity observation ages, device companion entities and explicit automation enable/run flows.
- Common authorized, audited HA action layer for inline, Widgets and schedules; serialized writes, atomic cooldowns, no automatic service POST retry, safe confirmations and admin bootstrap.
- Native Bot API 10.4 Widget transport and Profile 1 UI using aiogram 3.32.0; Widgets disabled by default, persisted restriction cooldown, automatic inline fallback and offline preview.
- Verified HA vacuum area mapping and preserved numeric legacy segments; unknown rooms never fall back to full cleaning. Maintenance buttons are not inferred to be routines.
- Distinguish service acceptance from physical state, avoid false vacuum completion announcements and duplicate explicit light-group commands.
- Complete JSON settings export, validated merge import, preserved roles and disabled restored schedules.
- Updated aiohttp 3.14.5 / aiosqlite 0.22.1, adapted WebSocket timeouts, expanded integration tests and PR-only multi-architecture build/import checks without publication.
- Added architecture, upgrade, Widget/fallback and testing documentation. Real Telegram Widget sending remains unverified while ordinary bots receive INPUT_MEDIA_WIDGET_NOT_ALLOWED.


## 2.3.7

- **Radio fix** — fixed TypeError crash when opening Radio menu (parameter name mismatch `current_station_idx` vs `current_idx`)
- **Status screen** — when no `status_entities` configured, now shows currently active entities instead of "no entities to display"; shows domain summary when nothing is active
- **Sync screen** — removed vacuum-only section from sync results; shows general registry counts only
- **DND entity deprioritization** — audio devices no longer pick DND/mute entities as primary control; added `_DND_SUFFIXES` penalty in entity selection
- **Notifications button restored** — re-added to main menu with correct `menu:notif` callback
- **Automations screen** — new menu to list, enable/disable, and trigger HA automations
- **To-Do lists** — new menu to browse HA to-do lists, view/add/complete/delete items
- **/terminal command** — execute shell commands in the add-on container (admin only, disabled by default via `terminal_enabled` config)
- **Doorbell error handling** — explicit error message and logging on button.press failures; never silently ignores
- **Registry logging** — added cross-ref assignment counts and primary entity selection debug logging
- **Config** — added `terminal_enabled` (bool, default false) option
- Version bump to 2.3.7

## 2.3.4

- **Per-action rate limit overrides** — `cooldown_seconds_default` (float) + `cooldown_overrides` dict for fine-grained control; defaults 0.2s for brightness/volume, backward-compatible with old `cooldown_seconds`
- **Brightness/volume debounce** — rapid taps coalesce into a single HA service call (0.25s window); light uses absolute brightness, volume uses `volume_set` with numeric level
- **Callback race protection** — per-user `asyncio.Lock` serializes callback handling; idempotency guard rejects duplicate callbacks within 0.25s (exempts debounce controls)
- **HA readiness gating improved** — `_wait_for_ha` now requires both `get_config()` AND `registry.sync()` success before declaring ready; keeps degraded mode fallback
- **SYNC diff summary** — refresh button shows contextual diff (added/removed areas, devices, entities) alongside counts
- Version bump to 2.3.4

## 2.3.3

- **Devices / Scenarios split** — main menu now separates physical Devices from Scenes/Scripts
- **Active Now** — device deduplication by device_id; tapping opens entity detail
- **Quick Room Scenes** — scene/script entities appear at top of room device list
- **Light color presets** — 7 color presets (Red, Orange, Yellow, Green, Blue, Purple, Warm White) on color-capable lights
- **Global Light Color** — apply a color preset to ALL lights at once from main menu
- **Radio menu** — stream internet radio to any media_player, with station switching and output selection
- **Breadcrumb navigation** — Back button uses per-chat stack instead of hardcoded targets
- **Home button** — 🏠 Меню button on all non-root submenus for instant return
- **Improved SYNC** — refresh shows friendly summary with floor/area/device/entity counts and vacuum stats
- **Error hardening** — try/except around all HA API calls in handlers, graceful fallback on failure
- **Menu readability** — icons on main menu items, compact formatting
- **Radio config** — `radio_stations` option in config.yaml for custom station list
- Version bump to 2.3.3

## 2.3.2

- **Resilient startup** — readiness gating with exponential backoff waits for HA Core to become available (handles 502 during boot)
- **Degraded mode** — bot starts Telegram polling even when HA is unreachable; menus work but show empty data
- **Auto-recovery** — background task detects when HA becomes available, re-syncs registry, and restores full functionality
- **Periodic re-sync** — registry is refreshed every 5 minutes to pick up entity/device changes after HA restarts
- Longer backoff for 502/503 errors in HA API client
- Recovery task cleanly cancelled on shutdown
- Version bump to 2.3.2

## 2.3.1

- **Active Now** button on main menu — shows all entities currently on/active (lights, media players, vacuums, etc.)
- **Smart entity hiding** — filters out diagnostic and config entities (`entity_category`) from area/device menus; respects `show_all_enabled` config toggle
- Notifications remain default OFF, per-entity opt-in via device card toggle
- Version bump to 2.3.1
- New tests for Active Now menu builder and entity category filtering

## 2.3.0

- Full HA registry sync (floors, areas, devices, entities) via WebSocket
- Device-level grouping in area menus
- Vacuum room targeting (segments) and Roborock routine buttons
- Media player controls: play/pause/stop, volume, mute, source picker
- Full 14-domain support (light, switch, vacuum, media_player, climate, fan, cover, scene, script, select, number, lock, water_heater, sensor)
- Favorites with pinned items (areas, routines)
- Actionable notification buttons with mute support
- Search, snapshots, scheduler, diagnostics
- Role-based access control (admin/user/guest)
- Export/import user settings
- Edit-in-place message management
- Forum supergroup thread support
