# Tests and release checks

Run from the repository root with Python 3.12:

```bash
python3 -m venv .venv
.venv/bin/pip install -r ha-telegram-bot/requirements-dev.txt
.venv/bin/python -m pytest ha-telegram-bot/tests -q
.venv/bin/python ha-telegram-bot/tools/check_widgets.py
```

`test_core.py` retains regression coverage of configuration, registry/state mapping, UI, persistence, startup/recovery, scheduler and existing features. `test_personal_ui.py` uses actual aiogram models, a fake Telegram session, fake HA, real temporary SQLite and production handlers. It covers:

- Concurrent users in one supergroup, multiple private chats and separate forum topics.
- Ownership/stale callbacks, independent breadcrumbs, radio/search/input state and restart persistence.
- Additive legacy migration, atomic concurrent cooldowns, deferred slider refresh and serialization of common-device commands.
- Guest/user/admin restrictions, revoked schedule roles, lock confirmation and terminal denial.
- Native Widget updates/methods, UTF-8/zlib limits, escaped JSON, query rate limits, early acknowledgement, fallback, persistent prohibition cooldown and unsupported shared queries.
- HA outage, POST no-retry semantics, coalesced state reads, Telegram missing/edit/flood errors.
- Honest observation ages, humidity limits, automation conditions, explicit group membership, verified vacuum area cleaning and legacy numeric mapping, no full-clean fallback.
- Validated settings roundtrip, disabled imported schedules, malformed imports and rejected arbitrary services.

No test needs a Telegram bot token, HA credential, real device or installation export. Fixtures are fictional. The HTML preview is a browser/mock check; it is not a Telegram client emulator.

## Container

```bash
docker build -t ha-bot-review ha-telegram-bot
docker run --rm --network none --entrypoint python3 ha-bot-review \
  -c 'import app, widget_ui, sqlite3; from widget_transport import validate_html; print(sqlite3.sqlite_version, validate_html(widget_ui.HTML))'
```

The smoke command imports the app without starting polling, connecting to HA or executing `/run.sh`. Building on a managed network may require its trusted proxy CA; use a temporary build secret, never disable TLS verification or commit a local CA.

The workflow runs Python tests and Widget validation, then builds `amd64`, `aarch64`, `armv7` and runs the offline import smoke test using QEMU. PR images are loaded locally on runners and never pushed to GHCR. Main/tag release publishing remains separate. Cross-architecture container success is not a hardware test on a Raspberry Pi.

## Installation review (after owner approval)

Use a test bot and isolated HA instance/helper entities for interaction checks. Verify `/menu` for two users and two topics, role restrictions, favorites/radio/to-do, old buttons, reconnect, restart persistence and unavailable entities. Confirm that an external HA update is displayed as an observation rather than credited to Telegram. Check that a newly configured room mapping is discovered on registry sync.

On the target HA OS add-on, the remaining checks are Supervisor token/permissions, `/data` persistence, actual integration capabilities and Raspberry Pi resource use. Do not stop production HA, modify automations or actuate real devices just to run automated tests. Widget production checks remain limited by Telegram; see [WIDGETS.md](WIDGETS.md).

The migration keeps legacy data. Take an add-on backup first; to roll back restore it together with the previous image. New version screens require `/menu`; stale legacy buttons are expected to be rejected.
