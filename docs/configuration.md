# Configuration

The watchdog is configured by two files:

- **`config/config.yaml`** — primary configuration. Bind-mounted into the
  container at `/app/config/config.yaml`. Editable via the web UI at
  `/admin/config`.
- **`.env`** — overrides for secrets. Read by `docker compose` and injected
  as environment variables into the container. See precedence rules below.

There is no other way to configure the running application. Editing `app.py`
requires a container rebuild.

## Precedence (highest first)

```
.env   >  WATCHDOG_* environment variables  >  config.yaml file values
```

For each `WATCHDOG_*` env var that is **set**, the corresponding `config.yaml`
value is **ignored** for that key:

| Environment variable | Overrides config.yaml path | Type |
|---|---|---|
| `WATCHDOG_MQTT_BROKER` | `mqtt.broker` | str |
| `WATCHDOG_MQTT_PORT` | `mqtt.port` | int |
| `WATCHDOG_MQTT_USERNAME` | `mqtt.username` | str |
| `WATCHDOG_MQTT_PASSWORD` | `mqtt.password` | str |
| `WATCHDOG_TG_TOKEN` | `telegram.bot_token` | str |
| `WATCHDOG_TG_CHAT_ID` | `telegram.chat_id` | str |
| `WATCHDOG_TG_ENABLED` | `telegram.enabled` | bool |
| `WATCHDOG_SECRET_KEY` | `web.secret_key` | str |
| `WATCHDOG_FACILITY_NAME` | `facility.name` | str |
| `WATCHDOG_DB` | SQLite file path | str |
| `WATCHDOG_CONFIG` | path to the config YAML | str |

`config.yaml` is the **default** for everything not listed above
(timeouts, snooze presets, auth users, log filenames, etc.). When you update
those via the web UI, the file is rewritten and the in-process configuration
reloads live — **no restart needed**, except for `web.host`, `web.port`,
`web.secret_key`, `mqtt.broker`, `mqtt.port`, which require a container
restart to take effect.

## `config.yaml` reference

```yaml
mqtt:                     # Broker connection
  broker: "10.10.17.11"
  port: 1883
  username: "yaniv"
  password: "…"
  topic_subscription: "facility/+/env_sensor/#"
  keepalive_seconds: 60
  reconnect_min_delay_seconds: 1
  reconnect_max_delay_seconds: 30

telegram:                 # Nagging engine
  enabled: true
  bot_token: "…"          # from @BotFather
  chat_id: "…"            # your chat or group ID
  nagging_interval_seconds: 180    # 3 min between repeats
  poll_timeout_seconds: 25         # long-poll for button callbacks

watchdog:                 # Detection thresholds
  check_interval_seconds: 30       # how often to evaluate
  default_heartbeat_timeout_seconds: 180   # 3 min → STALLED
  default_offline_timeout_seconds: 120     # 2 min → OFFLINE

web:                      # Flask server + dashboard
  host: "0.0.0.0"
  port: 5050
  secret_key: "…"         # 32+ random bytes; rotate to invalidate sessions
  dashboard_refresh_seconds: 10    # JS poll interval

auth:                     # User accounts
  users:
    yaniv:
      password: "…"       # upgraded to salted sha256 on first start
      role: "admin"
    operator:
      password: "…"
      role: "operator"
  config_history_max_versions: 50

facility:                 # Used by the print report header
  name: "Mushroom Cultivation Facility"
  location: "Israel"
  report_title: "Sensor Watchdog Status Report"

snooze_presets:           # Buttons in the device-table dropdown
  - label: "1 Hour"
    hours: 1
  - label: "8 Hours"
    hours: 8
  - label: "24 Hours"
    hours: 24
  - label: "7 Days"
    hours: 168
```

### Detection rules — derivation

Let `t_now` be current time, `t_last_seen` the last MQTT packet received
for a device, and `t_last_heartbeat` the timestamp when `heartbeat_counter`
last **changed value**:

1. **OFFLINE** is raised when either
   `status_topic == "offline"`  OR  `(t_now − t_last_seen) > offline_timeout_seconds`
2. **STALLED** is raised when
   `(t_now − t_last_heartbeat) > heartbeat_timeout_seconds`
3. **Nagging is suppressed** when
   `t_now < snooze_until`  OR  the alert is acknowledged.

`t_last_seen` is updated on **any** packet the device sends. `t_last_heartbeat`
updates only when the heartbeat counter value actually changes — sending the
same number repeatedly does not refresh the stall timer.

### Coercion of env vars

`WATCHDOG_MQTT_PORT` is parsed as `int`. `WATCHDOG_TG_ENABLED` accepts
`true / false / yes / no / 1 / 0` (case-insensitive) and is stored as a real
boolean. All other env vars are strings.

## `.env` reference

```bash
# See .env.example for the full template.

WATCHDOG_MQTT_BROKER=10.10.17.11
WATCHDOG_MQTT_PORT=1883
WATCHDOG_MQTT_USERNAME=yaniv
WATCHDOG_MQTT_PASSWORD=…

WATCHDOG_TG_TOKEN=…
WATCHDOG_TG_CHAT_ID=…
WATCHDOG_TG_ENABLED=true

WATCHDOG_SECRET_KEY=…            # 32+ random bytes; rotate to invalidate sessions

WATCHDOG_FACILITY_NAME="Mushroom Cultivation Facility"

TZ=Asia/Jerusalem
```

Comments are fine; lines starting with `#` are ignored by `docker compose`.
Empty values mean "use config.yaml".

## Editing via the web UI

Visit `/admin/config` after signing in as an admin user. You can:

- **Edit YAML directly** in a textarea; "Preview Diff" shows what will change;
  "Save & Apply" commits, snapshots the prior version into
  `config_history` table, and reloads the running process.

  Only `auth:`, `mqtt:`, `telegram:`, `watchdog:`, `facility:`, `snooze_presets:`,
  and the non-host/non-port fields of `web:` reload live. Other changes
  (port, host, broker, secret key) need a container restart, which the UI
  flags with: *"host/port changes need a full restart"*.

- **Download any past version** as a `.yaml` file via the per-row
  **Download** button in the history table.

- **Restore** any past version to live; the current version is snapshotted
  to history before being overwritten.

- **Upload a CSV of device metadata** (`device_id, location, role`) via
  the "Sensor metadata" card on the same page. The CSV is parsed, every row
  is upserted into `device_metadata` (and into `devices.location` /
  `devices.role` for fast dashboard reads), and the full upload is recorded
  in `csv_uploads` for audit.

  Format:

  ```csv
  device_id,location,role
  sht41-1,Spawn Incubator A,producer
  sht41-2,"Fruiting Room, North",dup
  sht41-3,Fruiting Room B,
  ```

  Quoted commas and double-quotes inside fields are honored (RFC 4180).
  The header row is mandatory. An upload with no `device_id` column is
  rejected before any row is written.

## User management

There is currently no UI to add a new user. To add or rotate users:

1. Edit `config.yaml`'s `auth.users:` block. Add a new key, give it
   `password:` (plaintext — will be hashed on next start) and `role:`
   of either `admin` or `operator`.

2. Save & Apply at `/admin/config`, then `./ops.sh restart`.

3. The next time the container starts, `_ensure_password_hashes()` walks the
   user table and converts any plaintext passwords to salted sha256 hashes,
   then rewrites `config/config.yaml` without the plaintext. **The seeded
   plaintext passwords are gone from disk after first start.**

To **revoke** a user, set their `password:` to a long random string (something
they don't know) and restart. Or remove their entry entirely.

To **reset all passwords**, delete `config/config.yaml`, run
`./ops.sh reset` (destructive — wipes DB too), and start over.

## Validation

`./ops.sh validate` parses `config.yaml` and `docker-compose.yml` and reports
any structural issues. YAML is validated lazily in the running process — a
malformed file will cause the container to exit at startup with a clear
error in `./ops.sh logs`.
