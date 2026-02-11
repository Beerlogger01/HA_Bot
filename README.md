# Home Assistant Telegram Bot Add-on

A secure, interactive Telegram bot for controlling Home Assistant devices. Features multi-level inline menus, dynamic entity discovery, vacuum room targeting, rate limiting, and audit logging.

## Features

- **Multi-level menus**: Interactive inline button navigation (Devices, Scenes, Robots, Status, Help)
- **Dynamic device discovery**: Automatically discovers entities from Home Assistant
- **Domain filtering & pagination**: Filter by domain allowlist, paginate large entity lists
- **Entity control**: Domain-specific controls (ON/OFF, brightness, temperature, covers, etc.)
- **Vacuum room targeting**: Room-based cleaning with configurable strategy (script or service_data)
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
light_entity_id: ""
vacuum_entity_id: ""
goodnight_scene_id: ""
```

#### Configuration Options

| Option | Description | Required | Default |
|--------|-------------|----------|---------|
| `bot_token` | Telegram bot token from BotFather | Yes | - |
| `allowed_chat_id` | Chat ID (negative for supergroup, positive for private) | Yes | 0 |
| `allowed_user_ids` | List of authorized Telegram user IDs | Yes | `[]` |
| `cooldown_seconds` | Per-user action cooldown in seconds | No | 2 |
| `global_rate_limit_actions` | Max actions per time window | No | 10 |
| `global_rate_limit_window` | Time window in seconds | No | 5 |
| `status_entities` | Entities to show in Status menu | No | `[]` |
| `menu_domains_allowlist` | Entity domains shown in Devices menu | No | see above |
| `menu_page_size` | Entities per page (1-20) | No | 8 |
| `show_all_enabled` | Allow users to toggle "Show All" domains | No | `false` |
| `vacuum_room_strategy` | Room targeting mode: `script` or `service_data` | No | `service_data` |
| `vacuum_room_script_entity_id` | Script entity for room cleaning (script mode) | No | `""` |
| `vacuum_room_presets` | Predefined room names for vacuum targeting | No | see above |
| `light_entity_id` | Legacy: single light entity | No | `""` |
| `vacuum_entity_id` | Legacy: single vacuum entity | No | `""` |
| `goodnight_scene_id` | Legacy: single scene entity | No | `""` |

### 7. Start the Add-on

1. Click **Start** in the add-on page
2. Enable **Start on boot** (recommended)
3. Enable **Watchdog** (optional, restarts if crashed)
4. Check the **Log** tab to verify startup

Expected log output:
```
[INFO] Starting Home Assistant Telegram Bot...
[INFO] Configuration loaded successfully
[INFO] Starting bot application...
[INFO] HA API self-test passed — version 2024.x.x
[INFO] Bot polling started
```

## How Menus Work

### Commands

- `/start` - Show welcome message and main menu
- `/menu` - Show main menu (alias for /start)
- `/status` - Show status of configured entities

### Main Menu

The main menu provides five categories:

- **Devices** - Browse all HA entities by domain (lights, switches, etc.)
- **Scenes** - List and activate HA scenes
- **Robots** - Control vacuum robots with room targeting
- **Status** - View current state of configured entities
- **Help** - Usage instructions

### Navigation

- All menus use **edit-in-place**: pressing a button updates the existing message instead of sending a new one
- Every submenu has a **Back** button for navigation
- If message editing fails (message too old), the bot deletes the old message and sends a new one
- The bot tracks the current menu message per chat in SQLite

### Entity Controls

Each entity type gets domain-specific controls:

| Domain | Available Actions |
|--------|------------------|
| `light` | ON / OFF / Toggle / Brightness +/- |
| `switch` | ON / OFF / Toggle |
| `cover` | Open / Stop / Close |
| `climate` | Temperature +/- |
| `vacuum` | Start / Stop / Return to Base / Room Cleaning |
| `scene` | Activate |
| `script` | Activate |
| `fan` | ON / OFF / Toggle |
| `lock` | Lock / Unlock |
| `media_player` | Play / Pause / Stop |

## Vacuum Room Targeting

The bot supports room-based vacuum cleaning with two strategies:

### Strategy: `service_data` (default)

Sends a `vacuum.send_command` service call with room information:
```yaml
service: vacuum.send_command
data:
  entity_id: vacuum.roborock
  command: app_segment_clean
  params:
    rooms:
      - bathroom
```

This works with many vacuum integrations that support segment cleaning (e.g., Roborock, Dreame, Xiaomi).

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

This is the most flexible approach. Create a script in HA that handles room-to-segment mapping for your specific vacuum:

```yaml
# configuration.yaml or scripts.yaml
script:
  vacuum_clean_room:
    alias: "Vacuum Clean Room"
    sequence:
      - choose:
          - conditions:
              - condition: template
                value_template: "{{ room == 'bathroom' }}"
            sequence:
              - service: vacuum.send_command
                target:
                  entity_id: "{{ vacuum_entity }}"
                data:
                  command: app_segment_clean
                  params:
                    segments: [16]
          - conditions:
              - condition: template
                value_template: "{{ room == 'kitchen' }}"
            sequence:
              - service: vacuum.send_command
                target:
                  entity_id: "{{ vacuum_entity }}"
                data:
                  command: app_segment_clean
                  params:
                    segments: [17]
```

### Configuration

```yaml
vacuum_room_strategy: "script"           # or "service_data"
vacuum_room_script_entity_id: "script.vacuum_clean_room"
vacuum_room_presets:
  - bathroom
  - kitchen
  - living_room
  - bedroom
  - hallway
```

### Capability Notes

- The bot discovers all vacuum entities from Home Assistant automatically
- Room presets are configurable (not auto-detected, since room names/segments vary per vendor)
- If a vacuum does not support room cleaning, the Start button still works (starts full cleaning)
- The Stop and Return to Base buttons are always available

## Security Features

### Chat Whitelisting

Only the configured `allowed_chat_id` can interact with the bot. Other chats receive an unauthorized message.

### User Authorization

Only users in `allowed_user_ids` can perform actions.

**Important**: If `allowed_user_ids` is empty, **all actions are denied** (safe default). The bot will log denied attempts with user IDs so you can find and add authorized users.

### Rate Limiting

Two layers of protection:

1. **Per-user cooldown**: Checked first. Prevents spam (default 2 seconds per action per user)
2. **Global rate limit**: Checked second. Only counts successful attempts (default 10 actions per 5 seconds)

## Audit Logging

All actions are logged to:

1. **stdout** (viewable in Add-on Log tab):
   ```json
   {
     "ts": "2026-02-11T10:30:45",
     "level": "INFO",
     "logger": "ha_bot.handlers",
     "msg": "AUDIT",
     "chat_id": -1001234567890,
     "user_id": 123456789,
     "action": "light.turn_on",
     "ok": true
   }
   ```

2. **SQLite database** (`/data/bot.sqlite3`):
   - Full history with timestamps, entity IDs, and errors
   - Survives restarts

## Troubleshooting

### Bot doesn't respond

1. Check the Log tab for errors
2. Verify bot token is correct
3. Ensure `allowed_chat_id` matches your chat ID
4. Make sure your user ID is in `allowed_user_ids`

### "Unauthorized chat" error

- Double-check your `allowed_chat_id`
- Supergroups use negative IDs starting with `-100`
- Private chats use your positive user ID
- Get the ID again using the `/getUpdates` method

### "You are not authorized" error

- Verify your user ID is in `allowed_user_ids` array
- Check for typos in user IDs
- User IDs are positive integers

### No entities shown in Devices menu

- Verify Home Assistant is running and accessible
- Check that `menu_domains_allowlist` includes the correct domains
- Try enabling `show_all_enabled: true` to see all domains
- Check add-on logs for HA API errors

### Vacuum room cleaning doesn't work

- Verify your vacuum supports segment/room cleaning
- If using `script` strategy: ensure the script entity exists and handles your room names
- If using `service_data` strategy: the default sends `app_segment_clean` which works with Roborock/Xiaomi. Other brands may need the `script` strategy with custom logic
- Check HA logs for service call errors

### Home Assistant API errors

Check logs for HTTP error codes:

- **404**: Entity doesn't exist - check entity ID spelling
- **401**: Authorization issue (rare with Supervisor proxy)
- **500**: Home Assistant internal error - check HA logs
- **Timeout**: HA is slow or restarting - the bot retries automatically

### Message editing fails

- This is normal for messages older than 48 hours
- The bot automatically falls back to delete + resend
- If the bot loses permissions in the chat, re-add it

## Architecture

The bot is built with a modular architecture:

```
app/
  app.py       - Main entrypoint, config loading, bot lifecycle
  api.py       - Home Assistant API client with retries
  storage.py   - SQLite database for cooldowns, audit, menu state
  handlers.py  - Telegram command and callback handlers
  ui.py        - Inline keyboard menu builders
```

## Persistence

All persistent data is stored in `/data/`:

- `/data/options.json` - Add-on configuration (managed by Supervisor)
- `/data/bot.sqlite3` - Audit logs, cooldown state, menu state

Data survives add-on restarts and updates.

## Support

For issues, questions, or feature requests:

- GitHub Issues: [https://github.com/Beerlogger01/HA_Bot/issues](https://github.com/Beerlogger01/HA_Bot/issues)

## License

MIT License - feel free to modify and distribute.
