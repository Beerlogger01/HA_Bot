# Home Assistant Telegram Bot Add-on

A secure, feature-rich Telegram bot for controlling Home Assistant devices. Includes rate limiting, audit logging, and user authorization.

## Features

- 🔒 **Security**: Whitelist-based chat and user authorization
- ⏱️ **Rate Limiting**: Per-user cooldowns and global rate limits
- 📝 **Audit Logging**: Full action history in SQLite + structured stdout logs
- 🏠 **Home Assistant Integration**: Direct API access via Supervisor proxy
- 🌐 **Multi-arch Support**: Works on Raspberry Pi (aarch64, armv7)
- ⚡ **Inline Controls**: Easy-to-use button interface

## Installation

### 1. Add Custom Repository

In Home Assistant:

1. Navigate to **Settings** → **Add-ons** → **Add-on Store**
2. Click the **⋮** menu (top right) → **Repositories**
3. Add this repository URL:
   ```
   https://github.com/Beerlogger01/HA_Bot
   ```
4. Click **Add** → **Close**

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

**Note**: Supergroup IDs always start with `-100`.

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
  - 123456789    # Your user ID
  - 987654321    # Girlfriend's user ID
cooldown_seconds: 2
global_rate_limit_actions: 10
global_rate_limit_window: 5
status_entities:
  - sensor.temperature_living_room
  - sensor.humidity_bedroom
  - binary_sensor.front_door
light_entity_id: light.living_room
vacuum_entity_id: vacuum.roborock
goodnight_scene_id: scene.good_night
```

#### Configuration Options

| Option | Description | Required | Default |
|--------|-------------|----------|---------|
| `bot_token` | Telegram bot token from BotFather | ✅ Yes | - |
| `allowed_chat_id` | Supergroup chat ID (negative number) | ✅ Yes | 0 |
| `allowed_user_ids` | List of authorized user IDs | ✅ Yes | `[]` |
| `cooldown_seconds` | Per-user action cooldown | No | 2 |
| `global_rate_limit_actions` | Max actions per time window | No | 10 |
| `global_rate_limit_window` | Time window in seconds | No | 5 |
| `status_entities` | Entities to show in `/status` | No | `[]` |
| `light_entity_id` | Light entity for ON/OFF buttons | No | `""` |
| `vacuum_entity_id` | Vacuum entity for Start/Dock | No | `""` |
| `goodnight_scene_id` | Scene entity for Good Night button | No | `""` |

### 7. Find Entity IDs

To find entity IDs in Home Assistant:

1. Go to **Developer Tools** → **States**
2. Search for your device (e.g., "living room light")
3. Copy the entity ID (e.g., `light.living_room_ceiling`)
4. Paste into add-on configuration

**Or** use the UI:

1. Go to **Settings** → **Devices & Services**
2. Click on a device
3. Click on an entity
4. Look at the URL - entity ID is at the end

### 8. Start the Add-on

1. Click **Start** in the add-on page
2. Enable **Start on boot** (optional but recommended)
3. Enable **Watchdog** (optional, restarts if crashed)
4. Check the **Log** tab to verify startup

Expected log output:
```
[INFO] Starting Home Assistant Telegram Bot...
[INFO] Configuration loaded successfully
[INFO] Starting bot application...
[INFO] Performing self-test: checking Home Assistant API access...
[INFO] ✓ Home Assistant API accessible. Version: 2024.2.0
[INFO] Bot started and polling for messages...
```

## Usage

### Commands

- `/start` - Display help message and show control buttons
- `/status` - Show current state of configured entities

### Inline Buttons

The bot provides inline buttons based on your configuration:

- 💡 **Light ON** / 🌑 **Light OFF** - Toggle light
- 🤖 **Vacuum Start** / 🏠 **Vacuum Dock** - Control vacuum
- 🌙 **Good Night** - Activate scene

## Security Features

### Chat Whitelisting

Only the configured `allowed_chat_id` can interact with the bot. Other chats receive "⛔ Unauthorized chat."

### User Authorization

Only users in `allowed_user_ids` can perform actions. Others receive "⛔ You are not authorized to perform actions."

**Important**: If `allowed_user_ids` is empty, **all actions are denied**.

### Rate Limiting

Two layers of protection:

1. **Per-user cooldown**: Prevents spam (default 2 seconds per action per user)
2. **Global rate limit**: Prevents abuse (default 10 actions per 5 seconds for entire group)

Violators see friendly error messages instead of being blocked.

## Audit Logging

All actions are logged to:

1. **stdout** (viewable in Add-on Log tab):
   ```json
   {
     "ts": "2026-02-11T10:30:45.123456",
     "chat_id": -1001234567890,
     "user_id": 123456789,
     "username": "john_doe",
     "action": "light_on",
     "ok": true
   }
   ```

2. **SQLite database** (`/data/bot.sqlite3`):
   - Full history with timestamps
   - Includes errors for failed actions
   - Survives restarts

## Troubleshooting

### Bot doesn't respond

1. Check the Log tab for errors
2. Verify bot token is correct
3. Ensure `allowed_chat_id` matches your group ID
4. Make sure your user ID is in `allowed_user_ids`

### "Unauthorized chat" error

- Double-check your `allowed_chat_id`
- Remember: supergroups use negative IDs starting with `-100`
- Get the ID again using the `/getUpdates` method above

### "You are not authorized" error

- Verify your user ID is in `allowed_user_ids` array
- Check for typos in user IDs
- User IDs are positive integers

### Buttons don't work

- Ensure entity IDs are correct (check Developer Tools → States)
- Verify cooldown hasn't been triggered (wait 2+ seconds)
- Check if global rate limit is exceeded (wait 5+ seconds)

### Home Assistant API errors

Check logs for "HTTP 4xx" or "HTTP 5xx" errors:

- **404**: Entity doesn't exist - check entity ID spelling
- **401**: Authorization issue (rare with Supervisor proxy)
- **500**: Home Assistant error - check HA logs

### Self-test verification

To manually verify Supervisor proxy access:

1. SSH into Home Assistant OS or use the Terminal add-on
2. Run (from within the add-on container):
   ```bash
   curl -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
        http://supervisor/core/api/config
   ```
3. You should see JSON with Home Assistant config

## Persistence

All persistent data is stored in `/data/`:

- `/data/options.json` - Add-on configuration (managed by Supervisor)
- `/data/bot.sqlite3` - Audit logs and cooldown state

Data survives add-on restarts and updates.

## Advanced Configuration Examples

### Minimal Setup (Light Only)

```yaml
bot_token: "YOUR_TOKEN"
allowed_chat_id: -1001234567890
allowed_user_ids:
  - 123456789
light_entity_id: light.living_room
cooldown_seconds: 2
global_rate_limit_actions: 10
global_rate_limit_window: 5
status_entities: []
vacuum_entity_id: ""
goodnight_scene_id: ""
```

### Full Setup

```yaml
bot_token: "YOUR_TOKEN"
allowed_chat_id: -1001234567890
allowed_user_ids:
  - 123456789
  - 987654321
cooldown_seconds: 3
global_rate_limit_actions: 15
global_rate_limit_window: 10
status_entities:
  - sensor.temperature_living_room
  - sensor.humidity_bedroom
  - binary_sensor.front_door
  - light.living_room
  - vacuum.roborock
light_entity_id: light.living_room
vacuum_entity_id: vacuum.roborock
goodnight_scene_id: scene.good_night
```

## Support

For issues, questions, or feature requests:

- GitHub Issues: [https://github.com/Beerlogger01/HA_Bot/issues](https://github.com/Beerlogger01/HA_Bot/issues)

## License

MIT License - feel free to modify and distribute.