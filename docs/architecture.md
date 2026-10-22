# Architecture

A code-level tour. If you're a senior engineer reading `app.py` for the
first time, this should make the structure obvious. If you're new to the
project, read this top-to-bottom before opening `app.py`.

## Big picture

```
┌─────────────────┐    MQTT publishes    ┌─────────────────┐
│  ESPHome nodes  │ ───────────────────► │  Mosquitto broker│
│  (facility floor)│  facility/+/env_sensor/# │   (10.10.17.11) │
└─────────────────┘                       └────────┬────────┘
                                                   │ subscribes
                                                   ▼
            ┌──────────────────────────────────────────────┐
            │  sensor_watchdog (single Flask process)     │
            │                                              │
            │  MQTT thread ───► on_mqtt_message ───┐      │
            │                                       │      │
            │  watchdog scheduler  ───► eval loop ─┤      │
            │                                       ▼      │
            │                              ┌─────────────┐ │
            │                              │  SQLite WAL │◄┤
            │                              │  sensor_watchdog.db
            │                              └────┬────────┘ │
            │                                   │         │
            │  Telegram thread ─► long-poll    │         │
            │            for callback presses  │         │
            └────────────────┬──────────────────┘         │
                             │                            │
                             ▼                            │
                    ┌─────────────────┐                   │
                    │  Flask app      │ ◄─────────────────┘
                    │  - /  (dashboard, JS poll /api/devices)
                    │  - /api/devices (JSON)
                    │  - /api/restart/<id>
                    │  - /admin/* (auth + admin role)
                    │  - /login /logout
                    │  - /audit (read-only)
                    │  - /snooze/* /unsnooze/*
                    │  - /print
                    │  - /health
                    └────────┬────────┘
                             │ user opens a browser
                             ▼
                       (operator)
```

Single process. Three threads of execution inside it:
- **MQTT thread** — paho-mqtt's network loop, calling `on_mqtt_message`
  on each packet.
- **Watchdog scheduler** — APScheduler fires `watchdog_eval_loop` every
  30 seconds.
- **Telegram polling thread** — long-polls `getUpdates` every ~25s.
- **Flask** — runs synchronously inside `app.run()` on the main thread.

The Flask debug mode is **always off** (`debug=False`); all four are
running because of Flask's thread model (`werkzeug` does spawn worker
threads for requests, but we avoid that).

## File-by-file

### `app.py` (single-file)

Top of file is the configuration load: env-var overrides on top of
`config.yaml`. Then the database + auth helpers. Then the four engines
(MQTT, Telegram, watchdog, Flask). Within Flask, the routes are grouped by
section: auth, dashboard, audit, admin, print.

If you want to add a route, find the nearest `# ----` banner and add it.
If you want to change MQTT topic parsing, edit `on_mqtt_message`. If you
want to change the watchdog detection rules, edit `watchdog_eval_loop`
and the constants near `_raise_or_nag`.

### `dashboard.js`

Client-side JS that the dashboard HTML embeds. Look here for:

- **Sort behaviour** — `applySort()` and `handleSortClick()`. State lives
  in `localStorage` (`watchdog-sort-key`, `watchdog-sort-dir`).
- **Search** — `applyFilter()` runs over each row's `data-*` attributes
  plus the rendered row text.
- **Per-sensor drilldown** — `sensorCell()` and `toggleSensors()`.

If you find yourself rewriting CSV upload logic, fix it in `app.py`,
not `dashboard.js`.

### `schema.sql`

Three application tables plus two auxiliary:

| Table | Purpose |
|---|---|
| `devices` | Latest known state per device. |
| `sensor_readings` | Latest known reading per (device, sensor, type). UNIQUE constraint enforces one-row-per-triple. |
| `alerts` | Active fault alerts; created on detection, marked resolved when fault clears. |
| `audit_logs` | Append-only history of operator actions. |
| `device_metadata` | CSV-uploaded location + role per device. |
| `csv_uploads` | One row per CSV upload (filename, row count, applied flag). |
| `config_history` | One row per config save, with the full YAML content. |
| `schema_meta` | Schema version + future migration flags. |

### `Dockerfile`

Two-stage trick:
1. `COPY requirements.txt` and `pip install` first — so a code-only change
   doesn't trigger a full re-download of wheels.
2. `COPY app.py schema.sql README.md dashboard.js /app/` — only the
   minimum runtime files. No test scripts, no `__pycache__`, no docs.

Container runs as the host UID via Compose's `user:` field (see
`HOST_UID` / `HOST_GID` env vars).

### `docker-compose.yml`

Single service. Three bind mounts (`./data`, `./config`, `./backups`).
Healthcheck points at the same `/health` endpoint humans will use.

Restart policy: `unless-stopped`. The container survives host reboots.

### `ops.sh`

Pure bash. Wraps `docker compose` with a fixed set of subcommands and
a few invariants (auto-create directories, default to `1001:1001`
UID/GID on platforms without `id` tricks, fall back to V1 compose if
the V2 plugin isn't installed). The script has no dependencies beyond
POSIX bash and `docker`.

## Key subsystems in depth

### SQLite WAL mode

The app writes from three concurrent sources:
1. **MQTT thread** — one INSERT per matching packet.
2. **Watchdog scheduler** — one transaction per cycle (reading all
   devices, updating status, possibly writing alerts).
3. **Flask handlers** — one connection per request.

Without WAL mode (the SQLite default), concurrent writers can return
`database is locked`. WAL mode allows multiple concurrent readers and one
writer without blocking each other. `get_db()` and `init_db()` set it:

```python
conn = sqlite3.connect(DB_FILE, timeout=10)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA busy_timeout=5000")
```

`synchronous=NORMAL` is a perf / durability tradeoff — fsync happens on
commit, not on every write. For a watchdog DB this is the right setting.
`busy_timeout=5000` covers any straggler that opens a connection without
WAL.

### Detection rules

Implemented in `watchdog_eval_loop`. For every device, faults are evaluated
in strict priority order — the first match wins:

1. **OFFLINE** — `status_topic == "offline"` OR no packet of any kind for
   more than `default_offline_timeout_seconds`.
2. **STALLED** — the device has published a heartbeat counter at some
   point, but its *value* hasn't changed for more than
   `default_heartbeat_timeout_seconds`.
3. **DEGRADED** — at least one sensor channel on the device has failed a
   per-sensor check (see below), while the device overall is still
   publishing.
4. **ONLINE** — everything flowing.

### Per-sensor checks (schema v5+)

Heartbeat is optional. Many ESPHome nodes never publish one, so the
watchdog also watches every individual sensor channel recorded in
`sensor_readings`. Each row carries two timestamps:

- `ts` — when this channel last sent *any* packet.
- `last_change_ts` — when this channel last sent a packet whose **value
  differed** from the previous one by more than
  `sensor_flatline_min_change_pct`.

Those two timestamps separate two genuinely different faults:

| Alert | Test | Meaning |
|---|---|---|
| `SENSOR_SILENT` | `now − ts > sensor_flatline_seconds` | This channel stopped publishing entirely while siblings on the same device keep going. Usually wiring, power, or a config problem on that one sensor. |
| `SUSPECTED_STALL` | `now − last_change_ts > sensor_flatline_seconds` (and packets are still arriving) | The channel is publishing, but the number is frozen. Either a dead sensor, or an environment that has genuinely stabilised. |

Both roll the device status up to `DEGRADED` and raise a per-device alert
naming the specific sensor.

**Why the distinction matters:** a silent channel usually means something
physical is disconnected. A frozen value usually means either a dead chip
or a room that is simply steady. The operator investigates very
differently in each case, so we don't conflate them.

The `last_change_ts` column is maintained inside the same UPSERT that
writes the reading — no separate history table is required, so this costs
no extra storage. A cold-start rebase (`_rebase_stale_readings_on_boot`)
runs once at boot so a restart doesn't immediately light up every channel
as stalled.

### Telegram callback handling

The Telegram bot has two ways to interact:

1. **Re-send / nag.** When a fault is detected or stale, the watchdog
   re-emits the alert every `nagging_interval_seconds` until either
   acknowledged or snoozed.

2. **Button presses.** The Telegram long-polling thread calls
   `getUpdates` and parses `callback_query` payloads. Format:
   `ack:<device_id>`  and  `snooze:<hours>:<device_id>`.

Both writes go through the same `audit()` helper.

### Configuration edit-and-apply

The Admin Config tab does this:

1. Render the current YAML into a textarea.
2. User clicks "Preview Diff" → server renders a unified diff (compute-only;
   nothing has been written).
3. User clicks "Save & Apply" → server:
   - Snapshots the live YAML into `config_history`.
   - Writes the new YAML to `config.yaml` (or `config/config.yaml`).
   - Reloads the relevant keys into the in-process `config` dict.
   - For fields that don't reload (port, host, secret key), logs a hint
     about needing a restart.
4. Anywhere later: "Restore" takes a snapshot of the *current* state,
   then writes a prior version into place.

### Audit row format

Every audit write goes through:

```python
def audit(action, device_id=None, details="", username=None):
    if username is None:
        username = session.get("username", "anonymous")
    conn.execute(
        "INSERT INTO audit_logs (device_id, username, action, details) "
        "VALUES (?, ?, ?, ?)",
        (device_id, username, action, details),
    )
```

The username can come from:
- The Flask session (web actions).
- The Telegram `from.username` field (Telegram button presses).
- The explicit `username=` parameter (background jobs).

Anything not sourced from either ends up as `anonymous`. That's by design;
a stray audit row labelled `anonymous` is suspicious and a clue to chase.

### Migration framework

Schema evolution uses a single function:

```python
def _apply_migrations(conn):
    # each migration is `if not _column_exists(...)` or similar
    # then issue the ALTER or equivalent
    conn.execute("INSERT OR REPLACE INTO schema_meta VALUES('schema_version', 'N')")
```

Migrations are **idempotent**. Anything checked via `_column_exists` /
`_table_exists` skips itself on a DB that's already at the target schema.

Current schema version: **5**.
- v2 → v3: added `device_metadata`, `csv_uploads`, `sensor_readings`,
  `devices.location`, `devices.role`.
- v3 → v4: renamed `sensor_readings.sensor_id` to `sensor_name` (the real
  hardware uses one device per physical node; `<name>` is an informational
  sub-label, not a sub-device).
- v4 → v5: added `sensor_readings.last_change_ts` plus the
  `idx_readings_flatline` index. This is what makes `SENSOR_SILENT` and
  `SUSPECTED_STALL` distinguishable without a history table.

Note on migration ordering: `_apply_migrations()` runs **before**
`schema.sql` in `init_db()`. This matters because `schema.sql` contains
`CREATE INDEX` statements that reference columns introduced by migrations.
On an existing database the `CREATE TABLE IF NOT EXISTS` statements are
no-ops, so those indexes would otherwise be attempted against a table that
has not yet been `ALTER`ed.

## Threading model

Inside one process:

```
main thread      → Flask
MQTT thread      → paho-mqtt's loop_forever()
Telegram thread  → our long-poll loop
APScheduler      → runs watchdog_eval_loop on the main thread? No —
                  APScheduler uses its own executor by default.
```

APScheduler BackgroundScheduler spawns executor threads for jobs. By
default these run in a `ThreadPoolExecutor` of size 10, but our
`max_instances=1, coalesce=True` settings ensure the watchdog eval
doesn't run concurrently with itself even if a cycle slips past 30s.

## When to read what

| You want to… | Read… |
|---|---|
| Add a new device status badge | `dashboard.js` `statusBadge()` plus CSS in `BASE_CSS`. |
| Add a new sensor metric type | `on_mqtt_message` in `app.py`. The keyword list is `("temperature", "humidity", "co2", "light", "pressure")`. |
| Add a new REST endpoint | `app.py` — find the right section, copy a `@login_required` route. |
| Add a new operator action | The relevant route in `app.py`, plus an `audit(...)` call. |
| Add a new config key | `config.yaml` template + `_apply_migrations` if it requires a column. |
| Add a new Telegram button | `TelegramEngine.send_nag_alert()` for the markup; `telegram_polling_thread()` for the parser. |
| Change the bundled Python deps | `requirements.txt`. |
| Change the broker topic | `config.yaml`'s `mqtt.topic_subscription` plus `on_mqtt_message` parsing. |
| Change the polling interval | `web.dashboard_refresh_seconds` (UI refresh) and `telegram.poll_timeout_seconds` (long-poll). |
| Change SQLite performance | WAL pragmas near `get_db()`. |
