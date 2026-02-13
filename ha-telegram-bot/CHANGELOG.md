# Changelog

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
