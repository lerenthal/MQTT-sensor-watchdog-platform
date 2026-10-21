# Development

Working with the code outside of Docker.

## Local development setup

You can run the watchdog without Docker. The intent is fast iteration on
`app.py` and `dashboard.js` without rebuilding a container image for
every change.

### Prerequisites

- Python 3.12 (matches the container base image).
- A running MQTT broker. The packaged config assumes `10.10.17.11:1883`;
  for dev, point `config.yaml` at `127.0.0.1:1883` and run a local
  `mosquitto` instance.

### Set up

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configure for local dev:

```bash
cp config.yaml config/config.yaml
$EDITOR config/config.yaml
# - set mqtt.broker to 127.0.0.1
# - set telegram.enabled to false (you don't need bots for dev)
# - set auth.users.*.password to whatever
```

Run:

```bash
source .venv/bin/activate
WATCHDOG_CONFIG=config/config.yaml \
WATCHDOG_DB=data/sensor_watchdog.db \
python app.py
```

Open `http://localhost:5050/` and sign in with the credentials you set.

### Live reload

`app.py` does not have hot reload enabled (`use_reloader=False`). After
editing `app.py`, restart manually. `dashboard.js`, on the other hand,
is loaded fresh on every request — just refresh the browser.

## Running tests

There is no automated test suite in the repo today. The verification path
is the running system itself, since "correct" means detecting a fault,
not satisfying a unit. If you want to add tests:

- `tests/test_topic_parser.py` — feed `on_mqtt_message` synthetic
  `(topic, payload)` tuples, assert the right DB rows appear. Subscribe
  a test client after the call.
- `tests/test_csv_upload.py` — feed `_parse_sensor_csv` good and bad
  CSV strings, assert errors list.
- `tests/test_watchdog.py` — inject a fake `devices` table with crafted
  timestamps, run `watchdog_eval_loop`, assert the resulting alert
  rows.

The pattern: open an in-memory SQLite, run a function, assert on the
DB state and exit.

## Coding style

This is a small, intentional codebase. Conform or rewrite the style guide.

- **PEP 8** with line-length 120 for all Python.
- **Single file is acceptable** — the design deliberately keeps
  `app.py` as one file. Don't fragment into a package until the
  second reason to do so appears.
- **One empty line between functions.** Two between section blocks.
- **Always** use parameterised SQL. The `audit()` helper takes named
  arguments; reach for `cursor.execute(...)` not `cursor.executescript(f"...{user_input}...")`.
- **Don't catch broad Exception unless you log + continue.** Watchdog
  loops should never crash the process.

`dashboard.js`:

- Plain ES6, no bundler.
- Two-space indent.
- Sort/filter state in `localStorage` keyed under `watchdog-*`.

## Adding a new MQTT topic variant

The broker conventions in this facility are:

```
facility/<device>/env_sensor/status                          -> "online" / "offline"
facility/<device>/env_sensor/heartbeat_counter/state         -> integer
facility/<device>/env_sensor/sensor/<sensor_name>/state      -> float / string
```

If your devices emit a different shape, edit `on_mqtt_message`'s
dispatch table. The function is heavily commented for this reason.
After changing, **add a test that subscribes to the new shape and
asserts the resulting DB row.**

## Adding a new config key

Three places, in order:

1. `config.yaml` — add the new key with a sensible default.
2. `app.py` — read it via `config.get(...)` at the top of the file,
   not lazily. The watchdog assumes the shape is known at boot.
3. If the new key requires a schema change:
   - Add the new column to `schema.sql`.
   - Add the corresponding migration block to `_apply_migrations`.
   - Bump `schema_version` in both places.

For a hot-reloadable config key (no restart needed), it must be in the
list in `admin_config_post`:

```python
for key in ("telegram", "watchdog", "facility", "snooze_presets", "auth"):
    if key in new_cfg:
        config[key] = new_cfg[key]
```

If you add a top-level section, add it here too.

## Adding a new audit action type

There's no registration list — just use the string. Convention: SCREAMING_SNAKE_CASE.

```python
audit("DEVICE_RECALIBRATED", device_id="sht41-1", details="from dashboard")
```

The `Audit Log` table doesn't carry an enum; we just match on the string
in the UI.

For a new action visible on the audit-log page, the existing display
(plain table) handles arbitrary strings.

## Adding a new role

`auth.users.<name>.role` accepts any string. Currently only `admin`
and `operator` have meaning. New roles require:

- A new helper (e.g. `@role_required("viewer")`) similar to
  `login_required`. Today there isn't one; the `admin_required` pattern
  was removed because every admin route rolls its own check.
- An entry in `admin_required` / `login_required` that gates routes by
  role.

For v1, keep the role set to `admin` and `operator`. Adding roles
without a clear use case just adds bugs.

## Releases & versions

There is no version file. The container image is tagged `latest`. The
schema version lives in `schema_meta` and is referenced from migrations.

For a cut:

1. Update `schema_version` in `schema.sql` (no-op column) and in
   `_apply_migrations` (sets the row).
2. Add an entry under "Unreleased" in `docs/CHANGELOG.md`. The format
   will be `Added / Fixed / Changed / Removed`.
3. Tag the commit with a date-based tag, e.g. `2026-10-21`.

## Things that will happen on the next refactor

In rough priority:

- **Tests.** Even a basic `tests/test_topic_parser.py` would help.
- **Per-user Telegram binding.** Currently Telegram presses are tagged
  with the chat's `@username`. Mapping chat-id → operator user would
  close the audit gap.
- **WebSocket updates.** 10-second polling on the dashboard is fine for
  30 devices; switch to SSE/WebSocket if your fleet grows.
- **Multi-process scaling.** Single SQLite + single Flask means you
  can't horizontally scale. If the facility grows past ~1000 devices,
  PostgreSQL would be a useful backend swap.
- **TLS for the dashboard.** Today it's HTTP only and assumes an
  upstream reverse proxy handles termination.

## Build artefact notes

`__pycache__/` and `*.pyc` files are gitignored but **not** git-cleaned
automatically. After running locally:

```bash
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
```

The `Dockerfile` doesn't bake these in (they live on host only).

If you change `requirements.txt`, the image **must** be rebuilt for the
changes to take effect:

```bash
./ops.sh rebuild
```
