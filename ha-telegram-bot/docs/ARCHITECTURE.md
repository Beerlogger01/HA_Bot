# Architecture: personal screens, shared home

## Boundaries

```text
Telegram commands / callbacks / widget queries
  → authorization + UISession(chat_id, user_id, thread_id)
  → common Handlers routes + navigation
  → inline renderer OR WidgetUI renderer
  → ActionService (role, service policy, global limiter, transaction, audit)
  → HAClient → Supervisor proxy → Home Assistant

HA state_changed → notifications (observed changes, independent of commands)
Scheduler → owner's session → ActionService → HAClient
```

- `app.py`: configuration, HA readiness/recovery, native aiogram polling, shutdown.
- `sessions.py`: task-local identity, message ownership, command/text/callback serialization.
- `handlers.py`: existing navigation/business routes shared by both interfaces.
- `ui.py`: capability-aware cards and compact inline screens.
- `widget_transport.py`: four native Bot API methods, byte limits, bounded timeouts.
- `widget_ui.py`, `widgets/home.html`: pagination, query authorization, state updates and fallback.
- `actions.py`: explicit service allowlist, single-entity targets, role/allowlist checks, rate reservation and write serialization.
- `observations.py`: honest observation ages and explicit light-group membership.
- `registry.py`, `vacuum_adapter.py`: dynamic registry relationships and verified vacuum capabilities.
- `storage.py`, `notifications.py`, `scheduler.py`, `diagnostics.py`: existing persistence and services extended in place.

There is one HA operation layer, one bot polling session, one SQLite database and no web server or browser engine in production. The standalone preview is development tooling only.

## Identity and lifetime

A screen belongs to `(chat_id, Telegram user_id, message_thread_id or 0)`. Its owned inline `message_id`, Widget message ID, current route, navigation stack, radio/input/search state and active capability maps are stored as JSON in `ui_sessions`. Favorites, subscriptions, pins and roles remain keyed by user so they follow that person between chats.

Buttons contain short unpredictable tokens rather than entity IDs or arbitrary services. The persisted map resolves a token only for the owning user, chat, topic, message and current screen revision. An old or foreign button fails closed. Long entity IDs and Unicode option names no longer exceed Telegram's 64-byte callback limit. Widgets have their own revision and action map; viewer metadata in HTML is a usability aid, never the authorization boundary.

The existing per-user lock now covers commands, callbacks and text as well as the previous callback path. A `ContextVar` carries identity through async work; it is reset even if persistence fails. Independent users have different navigation data and message IDs. Within a single user's topic, `/menu` reuses the current screen; there is one active screen of each renderer type rather than an unbounded collection.

SQLite WAL and additive `CREATE TABLE IF NOT EXISTS` migration retain existing tables and user data. `ui_sessions`, `ui_notices` and `runtime_state` are new. `menu_state` is kept for rollback/history but not adopted: it records no trustworthy owner. Stored current buttons and preferences survive restart. Legacy shared buttons are rejected. Notification actions use a separate recipient/message/action record with a seven-day expiry.

## Concurrency and state truth

The existing cooldown/debounce mechanisms remain. Cooldown reservation is an atomic SQLite upsert. Pending brightness/volume updates include user, chat, topic and entity; their final refresh checks that the owner still has the same screen open. Shutdown cancels outstanding debounce tasks.

`ActionService.transaction()` conservatively serializes bot writes, including read-modify-write operations. This is intentionally one small process-wide lock for a household workload, not a distributed lock per light. The global rate limit counts reserved attempts before network I/O. HA GET retries remain; service POSTs are never automatically replayed after an ambiguous failure.

The lock cannot stop physical controls, HA automations or another application from changing a device. Controls use explicit targets where possible. Cards display HA observations and command receipts separately, with no optimistic fabricated physical state. Refresh reads HA; subscriptions observe changes without claiming Telegram caused them. A two-second coalesced list-state cache reduces repeated requests. No background dashboard polling is added.

Group operations use only explicit HA light-group membership. When all members are known, group and members are not both targeted. Unknown names never establish membership. `unavailable` does not explain why power or connectivity disappeared. Measurement age uses `last_reported`, then clearly labelled `last_updated`; `last_changed` is not a heartbeat.

## Security and persistence

Every action traverses current allowlist and role validation. Known domain/service pairs and exact entity targets are required; imported settings cannot inject arbitrary services or broad area/device targets. Automation execution checks conditions. Locks and automation modifications require admin; dangerous routes use a one-screen confirmation. Service audits record accepted/failed requests, not guaranteed physical completion.

Export is a complete JSON document. Import validates shapes/services and merges the requesting user's settings; roles and identity are never imported. Schedules import disabled, retain existing cron semantics and recheck authorization at execution. The admin terminal keeps both its admin gate and explicit configuration gate.

Widget `publicData` is empty for home controls, messages are protected from forwarding, and shared queries are answered with a safe instruction to open a personal screen. Group screens are visible to group members; private chats should be used for confidentiality. No private installation export, historical audit, entity inventory or device identifier is bundled with the app.

## Compatibility decisions

- Existing service and menu functionality remains routed through `Handlers`; no separate Widget business logic.
- Native aiogram 3.32.0 supplies Bot API 10.4 types; aiohttp 3.14.5 WebSocket timeout objects replace old deprecated arguments; aiosqlite 0.22.1 retains WAL behavior.
- HA API/Supervisor paths, add-on slug, `/data`, image naming and supported architectures remain unchanged.
- HA registry mappings take priority over guessed room names. Unsupported legacy room-clean attempts now fail explicitly instead of unexpectedly starting full cleaning.
- External automation policy, MQTT/Zigbee configuration, robot maps and floorplan calibration belong to HA and are not modified by this add-on.
