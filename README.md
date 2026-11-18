# Home Assistant Telegram Bot Add-on

A personal control panel for Home Assistant inside Telegram. Version 2.4 keeps the existing add-on and `/data` database, adds independent screens for each user and forum topic, and provides a compact inline interface with an optional HTML Widget renderer.

**Inline menus are the default.** Ordinary bots currently receive `INPUT_MEDIA_WIDGET_NOT_ALLOWED` from Telegram. Widgets are disabled by default and automatically fall back to inline menus. See [Widgets and preview](ha-telegram-bot/docs/WIDGETS.md).

## Everyday use

- **Дом** shows active entities, lights, temperature/humidity with observation age, availability, and up to three favorites with direct on/off buttons.
- **Свет / Климат** lead straight to relevant entities. **Комнаты** follows HA floors, areas and devices; unassigned entities remain reachable.
- **Сценарии**, **Избранное**, **Активные** and **Поиск** provide alternate routes to the same device cards.
- **Ещё** contains radio, to-do lists, notifications, schedules, automation cards, snapshots, saved actions and help. Administrators also see diagnostics, registry sync and whole-home light colors.
- Device cards adapt to HA capabilities. They support lights, switches, fans, covers, climate, humidifiers, water heaters, media players, vacuums, locks, buttons, selects, numbers, scenes, scripts, automations, sensors and binary sensors. HA to-do lists have their own editor.
- Favorites, notification preferences, pins, schedules, search, radio selection and navigation remain personal. Two people in one supergroup edit **different messages**. Other members can still read a group message: session isolation is not confidential messaging.
- `/menu` always opens the inline home screen. Obsolete or another person's buttons cannot operate the home; open your own `/menu`.

The bot shows HA's reported state. A successful service request means **HA accepted the command**, not proof that the physical device changed. External automations and switches can change it again. Use **Обновить** for the latest state; subscribe to notifications for changes.

## Installation and upgrade

1. In HA OS, open **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add `https://github.com/Beerlogger01/HA_Bot`.
2. Install **Home Assistant Telegram Bot**. Create a bot with [@BotFather](https://t.me/BotFather) and keep its token private.
3. Obtain your Telegram user ID and chat ID from a bot update. A private chat normally has your positive user ID; supergroups have a negative ID. Stop polling before inspecting `getUpdates` yourself. Do not paste token-bearing URLs into issues or logs.
4. Fill in configuration, start the add-on, and send `/menu`. Supervisor supplies the HA token; no separately hosted server is required.
5. For forum topics, send `/menu` in each desired topic. Each `(chat, user, topic)` has its own screen. In groups with privacy mode, use `/search text` and reply to the bot's prompt when adding a to-do item.

Before upgrading, make an HA add-on backup including `/data`. The SQLite migration is additive and idempotent: favorites, roles, notifications, schedules, snapshots and legacy `menu_state` are retained. Old shared menu messages are deliberately invalidated because their owner cannot be established safely. Everyone opens `/menu` once. No HA configuration or device state is changed by the migration.

The image remains on GHCR with `amd64`, `aarch64` and `armv7` builds. Raspberry Pi 4 uses `aarch64` on 64-bit HA OS. Configuration and SQLite remain at `/data/options.json` and `/data/bot.sqlite3`; WAL is retained. For a rollback, restore the add-on backup rather than editing SQLite manually.

## Configuration

Minimal example (IDs are placeholders):

```yaml
bot_token: "YOUR_BOT_TOKEN"
allowed_chat_id: 0
allowed_user_ids: [123456789, 987654321]
admin_user_ids: [123456789]
widgets_enabled: false
widgets_retry_seconds: 3600
terminal_enabled: false
```

`allowed_chat_id: 0` permits multiple private chats and groups, but `allowed_user_ids` still restricts who may operate the bot. **Both empty user allowlist and chat ID 0 mean open access**, retained for backward compatibility. Configure the allowlist before starting a real installation.

| Option | Behavior / default |
| --- | --- |
| `bot_token` | Required Telegram token |
| `allowed_chat_id` | One allowed chat, or `0` for any chat |
| `allowed_user_ids` | Allowed users, `[]` for any user; enforced on commands and callbacks |
| `admin_user_ids` | `[]`; explicit admin bootstrap, applied on every restart |
| `widgets_enabled` | `false`; enables `/widgets` and Widget-first `/start` |
| `widgets_retry_seconds` | `3600`, range 60–86400; persisted retry delay after Telegram rejects Widgets |
| `terminal_enabled` | `false`; `/terminal` additionally requires admin |
| `cooldown_seconds` / `cooldown_seconds_default` | Existing per-user cooldown options, default 2 s |
| `cooldown_overrides` | Per-action cooldowns, including brightness/volume |
| `global_rate_limit_actions` / `global_rate_limit_window` | 10 service attempts per 5 s, including failed attempts |
| `status_entities` | `[]`; explicit status selection and priority for dashboard measurements |
| `menu_domains_allowlist` | Domains shown by room/device navigation; see [config.yaml](ha-telegram-bot/config.yaml) |
| `menu_page_size` | 8, range 1–20 |
| `show_all_enabled` | `false`; existing extended domain view |
| `device_overrides` | Per-entity activity rules and optional `max_report_age_seconds` |
| `vacuum_room_strategy` | `service_data` or `script`; see below |
| `vacuum_room_script_entity_id` | Explicit script for named room presets |
| `vacuum_room_presets` | Names passed only to an explicitly configured script; never guessed device segments |
| `radio_stations` | Objects with `name` and HTTP(S) `url`; existing stations retained |

`light_entity_id`, `vacuum_entity_id` and `goodnight_scene_id` remain accepted legacy options. Discovery does not depend on them. Adding a domain to the menu list does not authorize arbitrary HA service calls.

For a sensor with a known reporting interval, optionally set:

```yaml
device_overrides:
  - entity_id: sensor.example_temperature
    max_report_age_seconds: 7200
```

The card warns only when HA `last_reported` exceeds that threshold. Without a threshold it shows the age without declaring a fault. `last_updated` is labelled **Данные изменены**; `last_changed` is never used as a heartbeat. Missing timestamps are explicit. An unavailable light is not automatically described as broken or switched off. Battery percentage is not interpreted as a confirmed battery failure.

## Commands and preserved tools

| Command | Purpose |
| --- | --- |
| `/start`, `/menu`, `/widgets` | Home, guaranteed inline home, optional Widget UI |
| `/status`, `/ping`, `/health` | Status, connectivity, health |
| `/search <text>` | Search by name or entity ID |
| `/snapshot [name]`, `/snapshots` | State snapshots, compare and delete |
| `/schedule` | List, enable/disable and delete scheduled actions |
| `/schedule add Morning \| 0 7 * * * \| light.turn_on \| light.example` | Create a schedule |
| `/diag`, `/trace_last_error` | Admin diagnostics and recent error |
| `/role <user_id> <admin/user/guest>` | Admin role management |
| `/export_settings` | Complete JSON document, without truncation |
| `/import_settings <json>` | Merge settings; also accepts a JSON document with this command as caption |
| `/notify_test` | Test notification delivery |
| `/terminal <command>` | Admin only, explicitly enabled only |

Schedules use UTC and the existing weekday numbering **0=Monday … 6=Sunday**, retained so upgrades do not shift existing schedules. Each execution rechecks the owner's current role and allowlist. HA service writes are not automatically retried. Imported schedules are disabled until explicitly enabled; importing never grants roles or overwrites identity. Imported actions are validated against the same service policy as live actions.

Notifications retain per-user subscriptions, key-attribute mode, throttling and one-hour mute. They report observed transitions without attributing them to Telegram. A vacuum reaching `docked`/`idle` does not prove a successful complete clean. Notification action buttons are bound to their actual message and recipient and expire after seven days.

## Vacuums and Roborock

On HA versions exposing `vacuum.clean_area`, map vacuum segments to HA areas in the vacuum entity's settings. The bot reads the explicit `options.vacuum.area_mapping` from Entity Registry and uses `cleaning_area_id`, after checking the `CLEAN_AREA` capability. It does not derive segment IDs from room names or floorplan geometry.

Older Roborock/Xiaomi installations retain explicit numeric segment mappings already saved in SQLite and use `app_segment_clean` only with `SEND_COMMAND` support. Named presets require `vacuum_room_strategy: script` and a configured script accepting `vacuum_entity` and `room`. **An unknown room never starts a full clean.** Existing unsupported named `service_data` presets now show no room action rather than pretending to work.

Routines are identified from routine metadata on the same vacuum device. Maintenance/reset buttons are not labelled as cleaning routines. **Датчики и настройки устройства** opens the actual HA device entities: cleaning modes, battery, dock status and maintenance sensors where exposed. A separately registered dock is reachable through normal device navigation. Availability and operations depend on the integration, not the model name.

See [official Roborock documentation](https://www.home-assistant.io/integrations/roborock/). No cloud coordinate polling, simulated map or additional Roborock integration is introduced.

## Authorization

| Role | Access |
| --- | --- |
| Guest | Read screens; no device writes |
| User | Ordinary controls, personal favorites/notifications, to-do and allowed schedules |
| Admin | Also locks, automation changes, whole-home color actions, diagnostics, roles and optional terminal |

New authorized users retain the existing `user` default. Configured `admin_user_ids` bootstrap admin roles on every start; removing an ID from that option does not erase its stored role. To revoke it, remove it from the option and use `/role ID user` (or guest). Allowlist restrictions always apply, including to admins.

Unlock, automation changes, whole-home light colors, scenes/scripts, vacuum room/routine launches, saved actions and schedule toggles require a screen-bound confirmation. Automation one-shot runs preserve HA conditions (`skip_condition: false`). Existing schedules are reauthorized at execution. The service boundary rejects arbitrary service names, broad HA targets and mismatched domains. All service attempts are audited. `/terminal` executes inside the add-on container and remains disabled by default.

## Development and validation

```bash
python3 -m venv .venv
.venv/bin/pip install -r ha-telegram-bot/requirements-dev.txt
.venv/bin/python -m pytest ha-telegram-bot/tests -q
.venv/bin/python ha-telegram-bot/tools/check_widgets.py
.venv/bin/python ha-telegram-bot/tools/preview_widgets.py
```

Tests use mocked Telegram/HA APIs and temporary SQLite. They do not control devices. CI runs tests and builds all three architectures on pull requests; PR builds do not publish images. GHCR publication remains limited to existing main/tag push events.

- [Architecture, migration and concurrency](ha-telegram-bot/docs/ARCHITECTURE.md)
- [Widgets, limitations, activation and local preview](ha-telegram-bot/docs/WIDGETS.md)
- [Testing and deployment checks](ha-telegram-bot/docs/TESTING.md)
- [Changelog](ha-telegram-bot/CHANGELOG.md)

## Troubleshooting

- **No response:** check token, allowed chat/user, logs and topic; open `/menu` to replace an obsolete screen.
- **No devices:** use admin **Ещё → Синхронизация**. Check HA registries and the menu domain allowlist. HA startup failures enter degraded mode; the existing recovery loop retries discovery.
- **Old measurements:** inspect timestamps and integration reporting frequency. The bot does not reload integrations or rewrite HA configuration.
- **Room clean unavailable:** configure area mapping in HA or an explicit room script; inspect actual capabilities. Nothing is inferred from a floorplan.
- **Widgets rejected:** continue with inline; the persisted cooldown prevents repeated forbidden sends. `/menu` remains available.
- **Notifications missing:** start a private chat with the bot; check subscription, mute and throttle settings.
- **Ambiguous service timeout:** refresh the state before repeating an action. A timeout does not prove the device ignored it.

Support: [GitHub Issues](https://github.com/Beerlogger01/HA_Bot/issues). Do not include tokens, private configurations or database backups in public reports. License: MIT.
