# Home Assistant Telegram Bot Add-on

A secure, interactive Telegram bot for controlling Home Assistant devices. Features floors/areas navigation, vacuum room targeting, Roborock routines, per-user favorites, entity notifications, global search, state snapshots with diff, cron-based scheduler, diagnostics, role-based access control, and dynamic device discovery.

## Features

- **Floors & Areas navigation**: Automatic sync of HA Floor/Area/Device/Entity registries via WebSocket API
- **Dynamic device discovery**: Entities organized by floors -> areas -> devices with domain filtering
- **Devices / Scenarios split**: Separate menu categories for physical devices and scenes/scripts
- **Active Now**: Quick view of all currently active entities, deduplicated by device
- **Light color presets**: 7 preset colors (Red, Orange, Yellow, Green, Blue, Purple, Warm White) for color-capable lights
- **Global Light Color scenes**: Apply a color to ALL lights at once from main menu
- **Radio menu**: Stream internet radio stations to any media_player with output selection
- **Quick Room Scenes**: Scene/script entities at top of room menus for quick access
- **Vacuum room targeting**: Segment-based cleaning with auto-detection + configurable presets
- **Roborock routines**: Automatic discovery of routine button entities on vacuum devices
- **Per-user favorites**: Star entities and save quick actions for instant access
- **Entity notifications**: Subscribe to state changes with actionable inline buttons and per-entity mute
- **Global search**: Find entities by name or ID via `/search` command or inline menu
- **State snapshots**: Save and compare system state with `/snapshot` and diff view
- **Cron scheduler**: Schedule periodic HA service calls with cron expressions
- **Diagnostics**: `/health`, `/diag`, `/trace` commands for system monitoring
- **Role-based access control**: Admin / User / Guest hierarchy with write-action guards
- **Settings export/import**: Backup and restore per-user favorites, notifications, and actions
- **Forum supergroup support**: Proper message_thread_id handling for topic-based chats
- **Breadcrumb navigation**: Back button uses per-chat stack; Home button on all submenus
- **Clean message behavior**: Edit-in-place menus, no message clutter
- **Security**: Whitelist-based chat and user authorization, deny-by-default
- **Rate limiting**: Per-user cooldowns and global rate limits
- **Audit logging**: Full action history in SQLite + structured JSON stdout logs
- **HA API resilience**: Retries with exponential backoff, timeout handling, error hardening
- **Multi-arch support**: Works on Raspberry Pi (aarch64, armv7)

## Installation

### 1. Add Custom Repository

In Home Assistant:

1. Navigate to **Settings** > **Add-ons** > **Add-on Store**
2. Click the **three dots** menu (top right) > **Repositories**
3. Add this repository URL:
   ```
   https://github.com/Beerlogger01/HA_Bot
   ```
4. Click **Add** > **Close**

### 2. Install the Add-on

1. Refresh the add-on store page
2. Find **"Home Assistant Telegram Bot"** in the list
3. Click on it and press **Install**
4. Wait for installation to complete

### 3. Get Your Telegram Bot Token

1. Open Telegram and find [@BotFather](https://t.me/botfather)
2. Send `/newbot` and follow the instructions
3. Choose a name and username for your bot
4. Copy the token (looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 4. Get Your Chat ID

To find your **supergroup chat_id**:

1. Add your bot to the supergroup
2. Send any message in the group
3. Visit this URL in your browser (replace `<TOKEN>` with your bot token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. Look for `"chat":{"id":-100XXXXXXXXX}` in the JSON response
5. Copy the negative number (e.g., `-1001234567890`)

**Note**: Supergroup IDs always start with `-100`. For private chats, use your personal user ID (positive number). You can also use the bot in a private chat by setting `allowed_chat_id` to your user ID.

### 5. Get User IDs

To find **user_id** for yourself and others:

**Method 1** - Use a bot:
1. Find [@userinfobot](https://t.me/userinfobot) on Telegram
2. Send any message to it
3. It will reply with your user ID (a positive number like `123456789`)

**Method 2** - Use your bot:
1. Configure your bot with `allowed_chat_id` but leave `allowed_user_ids` empty
2. Start the bot temporarily
3. Try to use a button - check the logs for denied user_ids
4. Add those IDs to the configuration

### 6. Configure the Add-on

In the add-on **Configuration** tab:

```yaml
bot_token: "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
allowed_chat_id: -1001234567890
allowed_user_ids:
  - 123456789
  - 987654321
cooldown_seconds: 2
global_rate_limit_actions: 10
global_rate_limit_window: 5
status_entities:
  - sensor.temperature_living_room
  - sensor.humidity_bedroom
  - binary_sensor.front_door
menu_domains_allowlist:
  - light
  - switch
  - vacuum
  - scene
  - script
  - climate
  - fan
  - cover
menu_page_size: 8
show_all_enabled: false
vacuum_room_strategy: "service_data"
vacuum_room_script_entity_id: ""
vacuum_room_presets:
  - bathroom
  - kitchen
  - living_room
  - bedroom
radio_stations:
  - name: "Lounge FM"
    url: "https://cast.loungefm.com.ua/lounge"
  - name: "Radio Record"
    url: "https://radiorecord.hostingradio.ru/rr_main96.aacp"
  - name: "Europa Plus"
    url: "https://ep256.hostingradio.ru:8052/europaplus256.mp3"
```

#### Configuration Options

| Option | Description | Required | Default |
|--------|-------------|----------|---------|
| `bot_token` | Telegram bot token from BotFather | Yes | - |
| `allowed_chat_id` | Chat ID (negative for supergroup, positive for private). `0` = any chat. | No | 0 |
| `allowed_user_ids` | List of authorized Telegram user IDs. Empty = any user. Accepts single int too. | No | `[]` |
| `cooldown_seconds` | Per-user action cooldown in seconds | No | 2 |
| `global_rate_limit_actions` | Max actions per time window | No | 10 |
| `global_rate_limit_window` | Time window in seconds | No | 5 |
| `status_entities` | Entities to show in Status menu | No | `[]` |
| `menu_domains_allowlist` | Entity domains shown in menus | No | see above |
| `menu_page_size` | Entities per page (1-20) | No | 8 |
| `show_all_enabled` | Allow users to toggle "Show All" domains | No | `false` |
| `vacuum_room_strategy` | Room targeting mode: `script` or `service_data` | No | `service_data` |
| `vacuum_room_script_entity_id` | Script entity for room cleaning (script mode) | No | `""` |
| `vacuum_room_presets` | Predefined room names for vacuum targeting | No | see above |
| `radio_stations` | List of radio stations (`name` + `url` objects) | No | 3 default stations |

Legacy options (`light_entity_id`, `vacuum_entity_id`, `goodnight_scene_id`) are kept for backward compatibility but are optional and not required for any functionality.

### 7. Start the Add-on

1. Click **Start** in the add-on page
2. Enable **Start on boot** (recommended)
3. Enable **Watchdog** (optional, restarts if crashed)
4. Check the **Log** tab to verify startup

Expected log output:
```
[INFO] Loading configuration...
[INFO] HA API self-test passed — version 2024.x.x
[INFO] Starting registry sync...
[INFO] WS authenticated, fetching registries...
[INFO] Registry sync OK: N floors, N areas, N devices, N entities
[INFO] Vacuum vacuum.roborock: N routines
[INFO] Notification listener started
[INFO] Scheduler started (check every 30s)
[INFO] Bot initialized — @your_bot (id=123456789)
[INFO] Bot polling started
```

## How Menus Work

### Commands

- `/start` — Show main menu
- `/menu` — Show main menu (alias for /start)
- `/status` — Show status of configured entities
- `/ping` — Liveness check (pong + HA version + timestamp)
- `/search <query>` — Search entities by name or ID
- `/snapshot [name]` — Save a state snapshot of all entities
- `/snapshots` — List saved snapshots
- `/schedule` — List scheduled tasks
- `/schedule add <name> | <cron> | <domain.service> | <entity_id>` — Create scheduled task
- `/health` — Quick system health check
- `/diag` — Full diagnostics (admin only)
- `/trace` — Last error traceback (admin only)
- `/role [user_id] [role]` — Manage user roles (admin only)
- `/export_settings` — Export your favorites and notification settings as JSON
- `/import_settings <json>` — Import settings from JSON
- `/notify_test` — Test notification delivery

### Main Menu

The main menu provides these categories:

- **Устройства** — Browse devices by Floors -> Areas -> Entities with domain-specific controls
- **Сценарии** — All scene and script entities in one place
- **Активные** — Quick view of currently active entities (on, playing, cleaning, etc.)
- **Радио** — Stream internet radio to any media player
- **Цвет света** — Apply a color preset to all lights at once
- **Избранное** — Quick access to starred entities and saved quick actions
- **Статус** — View current state of configured entities
- **SYNC** — Re-sync floors, areas, devices, entities from HA

### Navigation Flow

```
Main Menu
├── Устройства
│   ├── Пространства (if floors exist)
│   │   ├── Floor 1 -> Areas -> Quick Scenes + Devices -> Controls
│   │   └── Floor 2 -> Areas -> Quick Scenes + Devices -> Controls
│   └── Комнаты (if no floors)
│       ├── Area 1 -> Quick Scenes + Devices -> Controls
│       └── Без комнаты -> Unassigned entities
├── Сценарии -> All scenes/scripts -> Activate
├── Активные -> Currently on entities -> Entity controls
├── Радио -> Station picker -> Play/Stop/Next/Prev -> Output select
├── Цвет света -> Color presets -> Apply to all lights
├── Избранное -> Starred entities -> Controls
│   └── Быстрые действия -> Run / Delete saved actions
├── Статус -> Status entities
└── SYNC -> Re-sync registries (friendly summary)
```

All menus use **edit-in-place**: pressing a button updates the existing message instead of sending a new one.

### Entity Controls

Each entity type gets domain-specific controls:

| Domain | Available Actions |
|--------|------------------|
| `light` | ON / OFF / Toggle / Brightness +/- / Color presets |
| `switch` | ON / OFF / Toggle |
| `cover` | Open / Stop / Close |
| `climate` | Temperature +/- |
| `vacuum` | Start / Pause / Stop / Dock / Locate / Room Cleaning / Routines |
| `scene` | Activate |
| `script` | Activate |
| `fan` | ON / OFF / Toggle |
| `lock` | Lock / Unlock |
| `button` | Press |
| `media_player` | Play / Pause / Stop |

Every entity card includes:
- **Star** button — Add/remove from favorites
- **Bell** button — Subscribe/unsubscribe to state change notifications

## Floors & Areas

The bot automatically syncs with Home Assistant's Floor, Area, Device, and Entity registries via WebSocket API.

- **Floors/Spaces** group Areas (e.g., "First Floor", "Second Floor")
- **Areas/Rooms** group entities (e.g., "Kitchen", "Bedroom")
- Entities are mapped to areas through the entity registry or device registry
- If no floors exist, the bot skips directly to the areas list
- Entities without an assigned area appear under "Без комнаты"

Default rooms (Kitchen, Living Room, Bedroom, Hallway, Bathroom) are matched to HA areas using RU/EN aliases.

## Vacuum Room Targeting

The bot supports room-based vacuum cleaning with two strategies:

### Strategy: `service_data` (default)

Sends a `vacuum.send_command` service call with segment IDs:
```yaml
service: vacuum.send_command
data:
  entity_id: vacuum.roborock
  command: app_segment_clean
  params:
    rooms:
      - bathroom
```

### Strategy: `script`

Calls a Home Assistant script with vacuum entity and room as variables:
```yaml
service: script.turn_on
data:
  entity_id: script.vacuum_clean_room
  variables:
    vacuum_entity: vacuum.roborock
    room: bathroom
```

### Roborock Routines

The bot automatically discovers Roborock routine button entities by finding `button.*` entities that belong to the same device as `vacuum.*` entities. Routines appear as a "Сценарии" menu item in the vacuum control card.

## Favorites

- Add any entity to favorites using the Star button in its control card
- Access favorites from the main menu
- Per-user storage (each user has their own favorites list)
- Paginated list with quick access to entity controls

## Notifications

- Subscribe to state changes for any entity using the Bell button
- Per-user, per-entity subscriptions stored in SQLite
- **Modes**:
  - `state_only` (default) — Only notify when the state value changes
  - `state_and_key_attrs` — Also notify on key attribute changes (battery, temperature, etc.)
- **Actionable buttons**: Notification messages include domain-specific action buttons (e.g., ON/OFF for lights, Dock/Locate/Pause for vacuums)
- **Mute**: 1-hour per-entity mute button on each notification message
- **Throttling**: Minimum 60 seconds between notifications per entity per user
- Notifications are sent as a separate message (not in the menu)
- Persistent WebSocket connection to HA with exponential backoff reconnect

## Search

- Search for any entity by name or entity_id
- Accessible via `/search <query>` command or the Search button in main menu
- In search mode, plain text messages are treated as search queries
- Results are paginated and each entity links to its full control card

## State Snapshots

- Save a point-in-time snapshot of all entity states with `/snapshot [name]`
- List saved snapshots with `/snapshots` or the Snapshots menu
- View snapshot details including entity count and creation time
- **Diff**: Compare a snapshot against current live state to see what changed
- Delete snapshots when no longer needed

## Scheduler

- Schedule periodic HA service calls using cron expressions
- Create: `/schedule add Morning | 0 7 * * * | light.turn_on | light.bedroom`
- List, toggle, and delete schedules from the menu or with `/schedule`
- Cron format: `minute hour day month weekday` (5 fields)
- Background task checks due schedules every 30 seconds
- Invalid cron expressions auto-disable the schedule

## Diagnostics

- `/health` — Quick system health check (HA reachable, registry synced, uptime)
- `/diag` — Full diagnostics panel with refresh and last error trace (admin only)
- `/trace` — View the last application error with full traceback (admin only)
- `/notify_test` — Test notification delivery to your chat
- Errors are captured from the application logger and stored in SQLite

## Role-Based Access Control

Three roles with hierarchical permissions:

| Role | Level | Permissions |
|------|-------|-------------|
| `admin` | 3 | All commands including `/diag`, `/trace`, `/role` |
| `user` | 2 | All read + write actions (toggle, control, favorites) |
| `guest` | 1 | Read-only access (browse menus, view status) |

- Default role for new users: `user`
- Admins manage roles via `/role <user_id> <role>`
- Write actions (device control, favorites, notifications) require at least `user` role
- Guests can browse and view but cannot control devices

## Settings Export/Import

- Export all your favorites, notification subscriptions, and quick actions as JSON
- Import settings to a new account or restore from backup
- Commands: `/export_settings` and `/import_settings <json>`

## Security Features

### Chat Whitelisting

If `allowed_chat_id` is set to `0` (the default), the bot accepts commands from **any chat** (open mode). Set it to a specific chat ID to restrict access to that chat only.

### User Authorization

If `allowed_user_ids` is empty `[]` (the default), the bot accepts commands from **any user** (open mode). Add specific user IDs to restrict access.

The bot flexibly handles `allowed_user_ids` input — if a single integer is provided instead of a list, it is automatically wrapped into a list.

### Rate Limiting

Two layers of protection:

1. **Per-user cooldown**: Checked first. Prevents spam (default 2 seconds per action per user)
2. **Global rate limit**: Checked second. Only counts successful attempts (default 10 actions per 5 seconds)

## Architecture

The bot is built with a modular architecture:

```
app/
  app.py              — Main entrypoint, config loading, bot lifecycle
  api.py              — Home Assistant REST API client with retries
  registry.py         — HA WebSocket registry sync (floors/areas/devices/entities)
  handlers.py         — Telegram command and callback handlers with route table
  ui.py               — Inline keyboard menu builders
  notifications.py    — State change notifications (WS subscribe + actionable alerts)
  storage.py          — SQLite database (14 tables)
  scheduler.py        — Cron-based periodic task execution
  diagnostics.py      — Health checks, debug info, error tracing
  vacuum_adapter.py   — Vacuum capabilities, segment cleaning, routines

tests/
  test_core.py        — Unit tests (pytest + pytest-asyncio)
```

## Database Tables

| Table | Purpose |
|-------|---------|
| `cooldowns` | Per-user action cooldown tracking |
| `audit` | Action audit trail |
| `menu_state` | Per-chat menu message tracking for edit-in-place |
| `favorites` | Per-user entity bookmarks |
| `rooms` | Canonical room names with HA area mapping and aliases |
| `entity_area_cache` | Cached entity->area->floor mapping from registry |
| `vacuum_room_map` | Vacuum segment_id to area mapping |
| `notifications` | Per-user entity notification preferences |
| `user_roles` | Role assignments (admin / user / guest) |
| `favorites_actions` | Saved quick actions (service calls) |
| `snapshots` | State snapshots with JSON payload |
| `schedules` | Cron-based scheduled tasks |
| `error_log` | Application error history for diagnostics |
| `mutes` | Per-user per-entity notification mutes with expiry |

## Troubleshooting

### Bot doesn't respond

1. Check the Log tab for errors
2. Verify bot token is correct
3. Ensure `allowed_chat_id` matches your chat ID
4. Make sure your user ID is in `allowed_user_ids`

### No floors/areas shown

- Ensure you have areas configured in Home Assistant (Settings -> Areas)
- Floors require HA 2024.x+
- Press "Обновить" in the bot to re-sync registries
- Check logs for "Registry sync" messages

### No Roborock routines found

- Routines must be enabled in the Roborock app and synced to HA
- They appear as `button.*` entities on the vacuum device
- Check that button entities are not disabled in HA entity registry

### Vacuum room cleaning doesn't work

- Verify your vacuum supports segment/room cleaning
- If using `script` strategy: ensure the script entity exists and handles your room names
- If using `service_data` strategy: the default sends `app_segment_clean` which works with Roborock/Xiaomi
- Check HA logs for service call errors

### Notifications not working

- Ensure the bot can send messages to you (start a private chat with the bot first)
- Check add-on logs for "Notification WS" messages
- Verify the entity exists and changes state
- Default throttle is 60 seconds per entity

### Home Assistant API errors

Check logs for HTTP error codes:

- **404**: Entity doesn't exist - check entity ID spelling
- **401**: Authorization issue (rare with Supervisor proxy)
- **500**: Home Assistant internal error - check HA logs
- **502**: HA Core is booting or restarting - see below
- **Timeout**: HA is slow or restarting - the bot retries automatically

### 502 errors at startup

This is **normal** when the add-on starts before HA Core has finished booting (common after a full system restart or HA update). The bot now handles this automatically:

1. **Readiness gating**: On startup the bot tries to reach HA up to 10 times with exponential backoff (3s, 6s, 12s, ..., up to 30s between attempts).
2. **Degraded mode**: If HA is still not reachable, the bot starts Telegram polling anyway. Menus will work but show empty data until HA becomes available.
3. **Auto-recovery**: A background task checks HA every 30 seconds. When HA becomes available, the bot automatically syncs the registry and restores full functionality.
4. **Periodic re-sync**: Even after recovery, the registry is refreshed every 5 minutes to pick up entity/device changes.

You'll see these log messages during the process:
```
[WARNING] HA not ready (attempt 1/10), retrying in 3s...
[WARNING] HA not reachable after 10 attempts — starting in degraded mode
[INFO] Bot polling started (v2.3.3, mode=degraded)
...
[INFO] HA API recovered — version 2024.x.x
[INFO] Registry sync complete: N floors, N areas, N devices, N entities
[INFO] Recovery complete — full functionality restored
```

No user action is required — the bot recovers on its own.

## Manual Verification Checklist

Use this checklist to verify resilient startup behavior after installation or upgrade:

- [ ] **Normal startup**: Start the add-on when HA Core is already running. Logs should show `HA API self-test passed` followed by `Registry sync complete` and `Bot polling started (v2.3.3, mode=full)`.
- [ ] **Start during HA boot**: Restart Home Assistant (Settings > System > Restart) and immediately start the add-on. Logs should show `HA not ready (attempt N/10)` messages, then either recovery or degraded mode.
- [ ] **Degraded mode**: Stop HA Core, start the add-on. Confirm it shows `mode=degraded` in the startup log. Send `/start` to the bot — it should respond with the main menu (menus will show no devices until HA recovers).
- [ ] **Auto-recovery**: With the bot running in degraded mode, start HA Core. Within 30 seconds, logs should show `HA API recovered` and `Recovery complete — full functionality restored`. Bot menus should now show devices.
- [ ] **Notifications reconnect**: After recovery, verify that the notification WebSocket reconnects (look for `Notification WS subscribed to state_changed` in logs). Toggle a subscribed entity and confirm notifications arrive.

## Persistence

All persistent data is stored in `/data/`:

- `/data/options.json` - Add-on configuration (managed by Supervisor)
- `/data/bot.sqlite3` - All database tables

Data survives add-on restarts and updates.

## Support

For issues, questions, or feature requests:

- GitHub Issues: [https://github.com/Beerlogger01/HA_Bot/issues](https://github.com/Beerlogger01/HA_Bot/issues)

## License

MIT License - feel free to modify and distribute.
