# HTML Widgets: optional renderer

Inline UI is fully usable without Widgets. The fixed Profile 1 HTML renders the same home, room/device, lighting/climate, vacuum, media, favorites, automation and utility screens produced by the existing route handlers. Long screens are paginated; actions call the same authorization and HA service layer. Text entry (search/to-do/import) stays in normal Telegram messages because Profile 1 has no standard text inputs.

## Current Telegram restriction

Telegram disabled ordinary bot HTML Widget sends on 13 November 2026, including private messages: `INPUT_MEDIA_WIDGET_NOT_ALLOWED`. See [Bot API issue 947](https://github.com/tdlib/telegram-bot-api/issues/947). No alternate endpoint or workaround is used.

Readiness here means native API transport, mock-tested updates, size-checked HTML, common HA actions, local browser preview and automatic inline fallback. It does **not** mean a production Widget has been successfully sent to Telegram. That check awaits Telegram availability and supported clients.

## Activation after availability returns

```yaml
widgets_enabled: true
widgets_retry_seconds: 3600
```

Restart the add-on and use `/widgets` (or `/start`). The first authorized send probes availability. Rejection stores a retry timestamp in SQLite; all users fall back to inline without repeatedly attempting forbidden sends. After the configured cooldown a later explicit Widget request probes again. Transient network failures use a short cooldown; Telegram flood-wait is respected. There is no continuous probe loop. Existing messages can be updated without changing HTML; new HTML releases take effect on newly sent Widget messages.

`/menu` always selects inline, even when Widgets are enabled. A regular **Обычное меню** inline button is attached under each Widget so old clients retain a route back. Turning the option off stops Widget handling; stored IDs do not cause sends while disabled.

## API and Profile 1

| Feature | Implementation |
| --- | --- |
| `sendWidget` | Native aiogram, fixed local HTML, `allow_queries`, protected message, forum topic |
| `TelegramWidget.request()` | Short opaque server-issued capability; no service/entity payload trusted from JS |
| `widget_query` | Actor/chat/message/action ownership, duplicate/rate/busy checks, common handlers |
| `shared_widget_query` | Explicit safe rejection of home control without trusted chat context |
| `answerWidgetQuery` | Acknowledged before any HA I/O; 3 s transport timeout |
| `editMessageWidgetData` | Private screen state and results; 5 s timeout |
| `editSharedWidgetData` | Validated adapter, per-widget 1/s pacing, cache period 5–3600 s; no home data published |
| `chatData`, `publicData`, `viewer` | Initial draw and owner hint; empty public state for home controls |
| State/theme events | `chatDataChanged`, `publicDataChanged`, `themeChanged`; revision prevents older draws |

The artifact has no network APIs, external scripts, libraries, fonts, CDN, forms, WebGL or map polling. CSS uses the documented Telegram theme variables. The client permits one in-flight request per Widget; the server permits at most five per five seconds per user/bot, and rejects concurrent user work before waiting. An early query acknowledgement is a receipt, not completion. Later data edits carry the result. The answer path never waits on a device request.

`validate_html()` checks actual UTF-8 bytes (≤32,768) and zlib level 9 bytes (≤4,096). State/answers are checked at ≤1,024 bytes, requests at 1–64 bytes, with NUL rejection. The renderer limits buttons/text per page and validates serialized JSON, including escape expansion. The UI targets 200–600 px widths and stays below 600 px height. HTML is immutable after send; only data changes.

Official references: [Widgets guide](https://core.telegram.org/bots/widgets), [Markdown specification](https://core.telegram.org/bots/widgets.md), [Bot API](https://core.telegram.org/bots/api).

## Local preview

```bash
.venv/bin/python ha-telegram-bot/tools/check_widgets.py
.venv/bin/python ha-telegram-bot/tools/preview_widgets.py --output /tmp/ha-widget-preview.html
```

Open the generated file locally. If your browser blocks local files, serve **only a preview directory**:

```bash
mkdir -p /tmp/ha-widget-preview
cp /tmp/ha-widget-preview.html /tmp/ha-widget-preview/
python3 -m http.server 8765 --bind 127.0.0.1 --directory /tmp/ha-widget-preview
```

The preview injects a mock bridge outside the shipped HTML and uses fictional entities. It can switch theme, bubble width, viewer and errors. Optional Playwright layout verification: install Playwright/Chromium in a development environment, then run `node ha-telegram-bot/tools/check_preview.cjs http://127.0.0.1:8765/ha-widget-preview.html` (`CHROMIUM_PATH` selects Chromium).

Browser layout/mock tests and the conservative static lint cannot certify the Telegram client's QuickJS/DOM implementation. Once ordinary bot sends are allowed, verify real iOS/Android/Desktop rendering, outgoing/group bubbles, safe areas, theme switching, event timing, unsupported-client fallback and Telegram flood behavior before enabling it broadly.
