#!/usr/bin/with-contenv bashio

bashio::log.info "Starting Home Assistant Telegram Bot..."

# Check if bot token is configured
if ! bashio::config.has_value 'bot_token'; then
    bashio::log.fatal "No bot_token configured! Please configure the add-on."
    exit 1
fi

# Check if allowed_chat_id is configured
if bashio::config.equals 'allowed_chat_id' '0'; then
    bashio::log.warning "allowed_chat_id is 0. Bot will not accept commands until configured."
fi

# Check if SUPERVISOR_TOKEN exists
if [ -z "${SUPERVISOR_TOKEN}" ]; then
    bashio::log.fatal "SUPERVISOR_TOKEN not found! Cannot communicate with Home Assistant."
    exit 1
fi

bashio::log.info "Configuration loaded successfully"
bashio::log.info "Starting bot application..."

# Run the Python bot
cd /app
exec python3 -u app.py
