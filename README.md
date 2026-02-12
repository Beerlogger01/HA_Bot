# Home Assistant Telegram Bot Add-on

A secure, interactive Telegram bot for controlling Home Assistant devices. Features floors/areas navigation, vacuum room targeting, Roborock routines, per-user favorites, entity notifications, and dynamic device discovery.

## Features

- **Floors & Areas navigation**: Automatic sync of HA Floor/Area/Device/Entity registries via WebSocket API
- **Dynamic device discovery**: Entities organized by floors -> areas -> devices with domain filtering
- **Vacuum room targeting**: Segment-based cleaning with auto-detection + configurable presets
- **Roborock routines**: Automatic discovery of routine button entities on vacuum devices
- **Per-user favorites**: Star entities for quick access
- **Entity notifications**: Subscribe to state changes with throttling and mode selection
- **Clean message behavior**: Edit-in-place menus, no message clutter
- **Security**: Whitelist-based chat and user authorization, deny-by-default
- **Rate limiting**: Per-user cooldowns and global rate limits
- **Audit logging**: Full action history in SQLite + structured JSON stdout logs
- **HA API resilience**: Retries with exponential backoff, timeout handling
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
[INFO] Bot initialized — @your_bot (id=123456789)
[INFO] Bot polling started
```

## How Menus Work

### Commands

- `/start` — Show main menu
- `/menu` — Show main menu (alias for /start)
- `/status` — Show status of configured entities
- `/ping` — Liveness check (pong + HA version + timestamp)

### Main Menu

The main menu provides five categories:

- **Управление** — Browse devices by Floors -> Areas -> Entities with domain-specific controls
- **Избранное** — Quick access to starred entities
- **Уведомления** — Manage state change subscriptions
- **Обновить** — Re-sync floors, areas, devices, entities from HA
- **Статус** — View current state of configured entities

### Navigation Flow

```
Main Menu
├── Управление
│   ├── Пространства (if floors exist)
│   │   ├── Floor 1 -> Areas -> Entities -> Controls
│   │   └── Floor 2 -> Areas -> Entities -> Controls
│   └── Комнаты (if no floors)
│       ├── Area 1 -> Entities -> Controls
│       └── Без комнаты -> Unassigned entities
├── Избранное -> Starred entities -> Controls
├── Уведомления -> Subscribed entities -> Toggle/Mode
├── Обновить -> Re-sync registries
└── Статус -> Status entities
```

All menus use **edit-in-place**: pressing a button updates the existing message instead of sending a new one.

### Entity Controls

Each entity type gets domain-specific controls:

| Domain | Available Actions |
|--------|------------------|
| `light` | ON / OFF / Toggle / Brightness +/- |
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
- **Throttling**: Minimum 60 seconds between notifications per entity per user
- Notifications are sent as a separate message (not in the menu)
- Persistent WebSocket connection to HA for real-time event processing

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
  app.py           — Main entrypoint, config loading, bot lifecycle
  api.py           — Home Assistant REST API client with retries
  registry.py      — HA WebSocket registry sync (floors/areas/devices/entities)
  notifications.py — State change notification system (WS subscribe + dispatch)
  storage.py       — SQLite database (8 tables)
  handlers.py      — Telegram command and callback handlers with route table
  ui.py            — Inline keyboard menu builders
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
- **Timeout**: HA is slow or restarting - the bot retries automatically

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
