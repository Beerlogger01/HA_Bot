# Changelog

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
