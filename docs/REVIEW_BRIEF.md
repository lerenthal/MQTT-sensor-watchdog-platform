# REVIEW BRIEF — MQTT Sensor Watchdog Platform

**Purpose of this document:** hand it to Claude (or any reviewer) with the
repository attached. It should be sufficient to (a) find real bugs, (b) propose
concrete improvements, and (c) plan the remaining work toward the production
deployment described in §2.

**Repository:** https://github.com/lerenthal/MQTT-sensor-watchdog-platform
**Branch:** `main` (all work pushed; nothing is uncommitted)
**License:** GPL-3.0
**Reviewed state:** commit `2e6b972` (post-review fixes applied; see
`docs/CHANGES_FOR_REVIEW.md`)

---

## 1. What this system is

### 1.1 Objective

A **standalone monitoring and alarming system** for an environmental sensor
fleet in a mushroom cultivation facility. It is an independent MQTT
subscriber: it does **not** depend on, integrate with, or require Home
Assistant, though it is designed to coexist with it.

It exists to answer one operational question continuously and to escalate
when the answer is bad:

> *Is every sensor in the facility actually reporting, and is every reported
> value physically plausible?*

Three concrete jobs:

1. **Detect** device faults — `OFFLINE` (device stopped publishing),
   `STALLED` (heartbeat counter frozen), `SUSPECTED_STALL` (a value frozen
   while packets keep flowing), `OUT_OF_RANGE` (a value outside its
   configured target range), and `DEGRADED` (device up, one channel bad).
2. **Alarm** — locally on a wall-mounted display in the facility (visual
   flashing + audible tone), and remotely to administrators' phones via
   Telegram.
3. **Account** — every operator action is written to an append-only audit
   trail with the username, because the facility operates under GMP and
   "who acknowledged this" is a question that must be answerable.

### 1.2 Non-goals

- It is not a historian. `sensor_readings` holds the **latest value per
  channel**, not a time series. InfluxDB is the system of record for trends;
  this app is the alarm layer.
- It does not control the facility. It can publish a restart command to a
  device's MQTT button topic, but it does not actuate anything else.
- It is not multi-tenant, not federated, and not horizontally scalable
  (single SQLite file, single process).

### 1.3 How it fits with the rest of the facility

```
   ESPHome nodes (30+)                Mosquitto broker
   sht41-x, scd30_x, scd41_x,  ────▶  10.10.17.11:1883   (Docker container)
   maxim3/4/5, bme680-1,                   │
   airmon-1, ens160-1, pt100-x             │
   portable-sense1, dual_temp              │
                                           │
                        ┌──────────────────┼───────────────────┐
                        │                  │                   │
              ┌─────────▼────────┐  ┌──────▼───────┐  ┌────────▼────────┐
              │ SENSOR WATCHDOG  │  │  Telegraf    │  │ HA Mosquitto    │
              │ (this app)       │  │  → InfluxDB  │  │  bridge → HA    │
              │ container:5050   │  │  (GMP record)│  │                 │
              └─────────┬────────┘  └──────────────┘  └─────────────────┘
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
   wall display    Telegram         browser (LAN)
   /wallboard      (admin phones)   localhost:5050
```

**The watchdog and InfluxDB are independent consumers of the same broker.**
A fault in one does not affect the other. This independence was load-bearing
during development: a broker outage silently stopped CO₂ delivery to the
watchdog for ~3 hours while Home Assistant showed retained (stale) values
and looked healthy. See §7.

---

## 2. Deployment target (the goal)

The reviewer should treat this as the acceptance criteria for the remaining
work.

| # | Requirement | Status |
|---|---|---|
| D1 | Single Docker container, independent of the MQTT broker container | ✅ done — `Dockerfile` + `docker-compose.yml` |
| D2 | Own dedicated port, not colliding with anything else on the host | ✅ 5050 published as `0.0.0.0:5050`; configurable via `web.port` |
| D3 | Reachable from any LAN client via browser | ✅ `web.host: 0.0.0.0`; **not yet verified from a second host** |
| D4 | Wall-display mode: full-screen, big type, flashing on new error, audible buzzer | ⚠️ **implemented, never visually verified** — see §6 |
| D5 | Telegram to remote phones for every new problem | ⚠️ **code path exists and is unit-reachable, but no live token/chat_id has ever been configured** — see §6 |
| D6 | Runs unattended on a production host, survives reboot | ✅ `restart: unless-stopped`; **not yet tested on the production host** |
| D7 | Config + DB survive container replacement | ✅ bind mounts `./config`, `./data` |
| D8 | Documented install on a fresh production host | ⚠️ `docs/install.md` written against the *pre-container* layout; needs rewriting for the container workflow |

**Planned production shape:**

```
production-host (Ubuntu 22.04/24.04)
├── docker compose -f sensor-watchdog/docker-compose.yml up -d
│     └── sensor_watchdog  →  0.0.0.0:5050 → 5050
├── /opt/sensor-watchdog/config/config.yaml   (edit via /admin/config or directly)
├── /opt/sensor-watchdog/data/sensor_watchdog.db
└── /opt/sensor-watchdog/.env                  (WATCHDOG_TG_TOKEN, WATCHDOG_TG_CHAT_ID, …)
```

Wall display: any browser on the LAN pointed at `http://<production-host>:5050/wallboard`,
ideally in kiosk mode (`chromium --kiosk --app=http://…/wallboard`).

---

## 3. Local test installation (current, working)

**Path:** `/media/yaniv/1tb_WDC/hermes_agent_projects/sensor_gaurd_rails/sensor_watchdog`

This is the live development instance. It is running now and is the
reference the reviewer can reproduce against.

### 3.1 Host

| Item | Value |
|---|---|
| OS | Ubuntu 24.04.4 LTS |
| Docker | Engine 29.7.2, Compose V2 plugin |
| Project root | `/media/yaniv/1tb_WDC/hermes_agent_projects/sensor_gaurd_rails/sensor_watchdog` |
| Git remote | `git@github.com-mqtt-watchdog:lerenthal/MQTT-sensor-watchdog-platform.git` |
| SSH deploy key | `~/.ssh/id_ed25519_mqtt_watchdog`, pinned via `~/.ssh/config` host alias `github.com-mqtt-watchdog` |

### 3.2 Running it

```bash
cd /media/yaniv/1tb_WDC/hermes_agent_projects/sensor_gaurd_rails/sensor_watchdog
HOST_UID=$(id -u) HOST_GID=$(id -g) ./ops.sh start
./ops.sh status      # container health + /health + recent audit rows
./ops.sh logs        # last 200 lines
./ops.sh logs follow # live tail
./ops.sh backup      # consistent SQLite snapshot into ./backups/
./ops.sh validate    # parse config.yaml + .env + compose file
```

**Login:** `yaniv` / `mushroom` (admin), `operator` / `operator`.
Passwords are auto-hashed to salted sha256 on first container start.

**Dashboard:** `http://localhost:5050/` — **wall display:** `http://localhost:5050/wallboard`

### 3.3 Live state at the time of writing

| Metric | Value |
|---|---|
| `schema_version` | 7 |
| Devices tracked | 31 |
| Sensor channels | 133 rows across 15 metric types |
| Metric types captured | battery, co2, heartbeat, humidity, illuminance, light, particle, pm1, pm10, pm25, pm4, pressure, temperature, tvoc, voltage |
| Device status | 29 ONLINE, 2 OFFLINE |
| Open alerts | 4 (2 OFFLINE, 2 STALLED — both genuine) |
| Audit rows | 53 |
| Target ranges defined | 1 (`sht41-1_temperature` 18–24 °C CRITICAL) |
| Known-degraded suppressions | 0 |
| Newest packet | 0 s old (live) |
| MQTT broker | `10.10.17.11:1883`, connected |

### 3.4 MQTT topic conventions (observed on the real broker)

The parser was reverse-engineered from live traffic; the original spec in
the task brief did not match reality.

```
facility/<device>/env_sensor/status                              → "online" / "offline"
facility/<device>/env_sensor/heartbeat_counter/state             → integer counter
facility/<device>/env_sensor/sensor/<sensor_name>/state          → float (or text)
```

`<sensor_name>` carries the metric type in the name:
`maxim5_temperature`, `airmon-1_pm2_5_mass_concentration`,
`bme680-1_pressure`, `sht41-4_heartbeat_counter`.

**Device-ID quirk:** some devices publish under an ID that differs from
their display name — `scd30-2` publishes under `scd30_2` (hyphen vs
underscore). The watchdog derives `device_id` from the topic and
`sensor_name` from the channel, so both spellings appear in the DB.

### 3.5 Metric classification

`_classify_sensor()` in `app.py` maps `<sensor_name>` → metric type by
ordered substring patterns. Two rules matter:

- **Diagnostics are rejected first.** `uptime`, `calibration_revision`,
  `mac_address`, `wifi_signal`, etc. are not environmental readings.
  Rejecting them first is essential: `dual_temp_uptime` contains "temp",
  and without the diagnostic guard it is typed as a temperature, holds
  constant forever, and generates a permanent false stall.
- **Order matters.** `co2` is matched before `co`; `temperature` is
  matched last so `ground_temperature` still resolves to temperature.

---

## 4. Codebase map

```
sensor_watchdog/
├── app.py                 3007 lines   ← everything server-side
├── dashboard.js            417 lines   ← operator dashboard (sort/search/expand)
├── wallboard.js            168 lines   ← wall display (flash/buzzer)
├── schema.sql              109 lines   ← SQLite schema, idempotent
├── ops.sh                  279 lines   ← operator CLI (start/stop/logs/backup/…)
├── Dockerfile               49 lines
├── docker-compose.yml       51 lines
├── config.yaml              86 lines   ← committed TEMPLATE (CHANGE_ME placeholders)
├── requirements.txt          5 lines
├── .env.example             30 lines   ← committed template
├── .dockerignore / .gitignore
├── README.md               132 lines
├── CHANGELOG.md             74 lines
└── docs/                 1776 lines
    ├── install.md          171   ← STALE: written for the pre-container layout
    ├── configuration.md    239
    ├── operations.md       221
    ├── user-guide.md       295
    ├── security.md         212
    ├── architecture.md     317
    ├── development.md      202
    ├── CONTRIBUTING.md     104
    └── README.md            15
```

### 4.1 `app.py` internal structure

`app.py` is deliberately a single file. Sections, in order:

Line numbers verified against commit `68efef7`.

| Line | Section | Notes |
|---|---|---|
| 1–60 | imports, logging, constants | stdlib + flask, paho-mqtt, APScheduler, requests, PyYAML |
| 62 | env-var override block | `WATCHDOG_*` applied **after** the YAML is parsed; env wins |
| 98 | `get_db()` | WAL mode, `busy_timeout=15000` |
| 119 | `_apply_migrations()` | idempotent, versioned; runs BEFORE `schema.sql` |
| 237 | `audit()` | append-only; fault-tolerant — never raises into the caller |
| 299 | `login_required` | sha256+salt password verify |
| 312 | `TelegramEngine` | `send_nag_alert()`, `send_message()`, `answer_callback()` |
| 406 | `_SENSOR_METRIC_PATTERNS` | ordered substring list for metric typing |
| 441 | `_DIAGNOSTIC_NAME_MARKERS` | rejected **before** metric matching |
| 479 | `_classify_sensor()` | name → metric type, or `None` for diagnostics |
| 573 | `on_mqtt_message()` | parse topic → classify → upsert → roll up to `devices` |
| 867 | `_detect_systemic_faults()` | cluster same-metric/same-window failures |
| 942 | `watchdog_eval_loop()` | the core detection loop; commits per device |
| 1264 | `telegram_polling_thread()` | long-poll loop, callback handling |
| 1683 | `/` dashboard | |
| 1729 | `/api/devices` | devices + per-sensor health + `reason` |
| 1929 | `/snooze/<device_id>/<hours>` | |
| 1951 | `/device/<device_id>/reset_stale` | POST |
| 1980 | `/api/restart/<device_id>` | POST |
| 1999 | `/audit` | |
| 2124 | `/admin/sensors/csv` | POST — CSV upload, incl. target ranges |
| 2187 | `/admin/degraded` | GET + POST — known-degraded suppression |
| 2382 | `/admin/config` | GET + POST |
| 2615 | `/print` | printable status report |
| 2781 | `/wallboard` | wall-display mode |
| 2801 | `/api/wallboard` | JSON feed for the wall board |
| 2849 | `/health` | liveness, not session state |
| 2898 | `_watchdog_check_health()` | broker-down + no-data alarms |
| 2982 | `main()` | entrypoint |

**Complete route inventory (21):**

```
GET  /login                                POST /login
GET  /logout
GET  /
GET  /api/devices
GET  /api/devices/<device_id>/sensors
GET  /snooze/<device_id>/<int:hours>
GET  /unsnooze/<device_id>
POST /device/<device_id>/reset_stale
POST /api/restart/<device_id>
GET  /audit
POST /admin/sensors/csv
GET  /admin/degraded                        POST /admin/degraded
GET  /admin/sensors/history
GET  /admin/config                          POST /admin/config
GET  /admin/config/download/<int:version_id>
GET  /admin/config/restore/<int:version_id>
GET  /print
GET  /wallboard
GET  /api/wallboard
GET  /health
```

### 4.2 Data model (schema v7)

```
devices               one row per physical ESPHome node
  device_id PK, status, location, role, last_seen, last_heartbeat_time,
  last_heartbeat_value, last_temp, last_hum, last_co2, snooze_until

sensor_readings       one row per (device_id, sensor_name, sensor_type)
  UNIQUE(device_id, sensor_name, sensor_type)
  value, ts, last_change_ts
  ← ts = last packet of any kind
  ← last_change_ts = last packet whose VALUE differed

alerts                one row per active fault
  device_id, alert_type, message, triggered_at, last_nagged_at,
  acknowledged, resolved

audit_logs            append-only operator actions
  device_id, username, action, details, timestamp

device_metadata       location + role per device, from CSV upload
sensor_thresholds     per-entity target ranges (min, max, severity)
out_of_range          current range breaches with onset/resolve times
known_degraded        operator-acknowledged suppressions
config_history        every saved config version (full YAML)
csv_uploads           CSV upload audit trail
schema_meta           schema_version
```

### 4.3 Detection rules

Evaluated in strict priority order; the first match wins.

```
1. OFFLINE     status_topic == "offline"
            OR (now − last_seen) > default_offline_timeout_seconds (120)

2. STALLED     heartbeat counter value unchanged for longer than
               default_heartbeat_timeout_seconds (180)

3. SUSPECTED_STALL   a channel publishes but its value has not moved
               beyond sensor_flatline_min_change (0.0 units) for longer
               than sensor_flatline_seconds (900).
               ⚠ OFF BY DEFAULT — see §5.1

4. DEGRADED    device is publishing, but >=1 channel failed a check

5. ONLINE      everything flowing

6. OUT_OF_RANGE   a fresh reading outside its configured target range
               (only evaluated against fresh readings, so a dead channel
               reports SENSOR_SILENT, never a misleading range breach)
```

**Suppression:** sensors in `known_degraded` are excluded from
`SENSOR_SILENT` / `SUSPECTED_STALL` and from systemic clustering. They keep
reporting and stay visible, struck through and badged as suppressed.

**Systemic rollup:** when the same metric goes quiet on ≥3 devices inside
the same 5-minute bucket, the individual per-device alerts are suppressed
and one cluster banner is raised. Rationale: ten chips in ten rooms do not
die simultaneously — that pattern is broker, ACL or network.

### 4.4 Configuration precedence

```
.env  >  WATCHDOG_* env vars  >  config.yaml
```

`WATCHDOG_*` overrides in `app.py` are applied **after** the YAML is
parsed. Supported: `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`,
`MQTT_PASSWORD`, `TG_TOKEN`, `TG_CHAT_ID`, `TG_ENABLED`, `SECRET_KEY`,
`FACILITY_NAME`, `DB`, `CONFIG`.

### 4.5 Threading model

One process, four concurrent activities:

| Thread | Work | Cadence |
|---|---|---|
| main | Flask request handling | on demand |
| paho MQTT network loop | `on_mqtt_message()` → SQLite upsert | per packet |
| APScheduler | `watchdog_eval_loop()` | every 30 s |
| custom thread | Telegram `getUpdates` long-poll | continuous |

---

## 5. Known issues and design debt

**These are the reviewer's targets. Ordered by how much they matter.**

### 5.1 Value-flatline detection is disabled — a genuine frozen sensor is invisible

The `SUSPECTED_STALL` rule compares a channel's current value against its
last *changed* value. On this fleet it produced 10 false positives and 0
true positives: devices publish the same number every cycle because the
room is genuinely steady. A reading that holds one value is what a stable
environment looks like.

The authoritative signal exists on the device. The firmware publishes a
`Value Stall` binary sensor computed on-device against its own previous
reading with a 30-minute threshold:

```yaml
binary_sensor:
  - platform: template
    name: "${device_label} Value Stall"
    id: value_stall
    publish_initial_state: true
    lambda: |-
      const uint32_t now = millis();
      if (id(last_change_ms) == 0) return false;
      if (id(last_scd_ms) == 0 || (now - id(last_scd_ms)) > ${stale_timeout_ms}) return false;
      return (now - id(last_change_ms) > ${stall_timeout_ms});
```

**RESOLVED in review commit `2e6b972`.** ESPHome's `binary_sensor`
platform publishes to `binary_sensor/<slug>/state` (not `sensor/<name>/state`)
with an `ON`/`OFF` text payload. The ingest now handles that shape
separately, decodes the text to 1.0/0.0, and stores it as `diagnostic`.
Diagnostics from **22 devices** now reach the watchdog.

Every `value_stall` entity reads clear, which confirms that disabling the
server-side flatline heuristic removed only false positives. Three real
hardware faults were found that the watchdog could not have detected on its
own: `airmon-1_scd30_fault`, `airquality1_particulate_fault` and
`airquality2_particulate_fault`.

**Open question for the reviewer:** is this a watchdog parsing gap (fixable
in Python) or does the ESPHome YAML need one line so the entity publishes
to a topic the watchdog subscribes to? The firmware author's intent is
clearly that this entity should be alarmed on — see the comment *"These
are the entities the monitoring system should alarm on."*

### 5.2 Two devices have a permanently frozen heartbeat counter

```
scd30_2    heartbeat frozen 85853 s (~24 h)  — temp/humidity publishing fine
airmon-1   heartbeat frozen  8502 s (~2.4 h)
```

Both devices publish temperature/humidity normally; only the
`heartbeat_counter` template sensor has stopped advancing. The watchdog
correctly flags these as `STALLED`. Either the firmware's heartbeat
sensor stopped, or the counter template is not being re-published. Worth
checking on the device before production — a fleet-wide heartbeat failure
would raise 30 alarms at once.

### 5.3 `sensor_readings` is latest-value only

No history. Fine for alarming, useless for trending. If the reviewer
suggests retaining a time series, note the volume: 31 devices × ~6
channels × 0.05 Hz ≈ 800 rows/day — trivial for SQLite. A `sensor_readings_history`
table with periodic downsampling would enable "was this sensor moving
before it froze?" without much cost.

### 5.4 The webhook/Telegram path has never been exercised live

`telegram.enabled` is `false` in `.env`; `bot_token` and `chat_id` are
still the `YOUR_TELEGRAM_BOT_TOKEN_HERE` placeholders. The code path is
exercised by `_raise_or_nag()` → `send_nag_alert()` → `sendMessage`, and
by the polling thread's callback parsing. **No message has ever been
delivered.** The inline-button callbacks (`ack:<device_id>`,
`snooze:<hours>:<device_id>`) have never been round-tripped.

This is requirement **D5** — the remote-phone alerting path. It is the
single largest untested surface.

### 5.5 The wall board has never been visually verified

`/wallboard` returns HTTP 200 and `/api/wallboard` returns correct JSON.
But it has never been rendered in a browser at 1920×1080, never seen
flashing, and the Web Audio buzzer has never been heard. Two specific
risks:

- **Browsers block audio until a user gesture.** On a kiosk TV with no
  interaction, `AudioContext` may stay suspended and the buzzer will be
  silent. The wall board degrades to visual-only, but if a facility TV has
  no speakers the alarm is purely visual and may be missed.
- **`show_alert` / page title** — the title changes to `!! N FAULTS` which
  is good for a taskbar glance, but full-screen kiosk mode may hide it.

### 5.6 Wall board auto-dismiss is 60 s

`ALARM.autoDismissSeconds = 60`. After 60 s the flashing stops (the list
stays readable). For a facility TV that nobody is watching, this may mean
an error becomes visually quiet. Consider: re-flash periodically while the
fault is unacknowledged, or make the interval configurable.

### 5.7 `docs/install.md` is stale

It documents the pre-container workflow (`python3 -m venv`, `python
app.py`). The container + `ops.sh` workflow is the real one. Needs rewrite.

### 5.8 No TLS

Flask speaks plain HTTP. Production deployment needs a reverse proxy
(nginx/Caddy/traefik) or a cloud LB terminating TLS. The image contains
no proxy — deliberately, to avoid baking infra choices into the app image.

### 5.9 Single-writer SQLite

The DB is WAL-mode with a single writer. It now performs cleanly, but a
busy fleet (or a second container for HA) would need PostgreSQL. The
data-access layer is plain SQL in `app.py`, so that migration is
mechanical but not trivial.

### 5.10 `audit_logs` has no retention policy

It grows without bound. For a GMP context, retention is a policy decision,
not a code one — but there is no way to export or prune it from the UI.

### 5.11 Config-history pruning is silent

`config_history` is capped at 50 versions (`auth.config_history_max_versions`)
and pruned oldest-first with no log entry. An operator would not know their
config history was truncated.

### 5.12 Credential exposure in `config.yaml`

The committed template has `CHANGE_ME` placeholders, which is correct.
But `mqtt.password` was a live value in the working tree earlier in
development, and `config/config.yaml` (the live, gitignored one) still
contains a real broker password. Worth confirming nothing sensitive is
staged for commit before the production push.

### 5.13 No test suite

`docs/development.md` sketches what tests would look like. There are none.
Every bug found during development was found by *watching the running
system*, not by a test. This is the highest-leverage improvement available.

---

## 6. Specific questions for the reviewer

**Bugs / correctness**

1. The value-flatline heuristic was disabled because it produced only false
   positives. Is there a server-side signal that can distinguish "stable
   room" from "frozen sensor" without relying on the firmware's own verdict?
   (Consider: variance over a window, cross-sensor correlation on the same
   device, rate-of-change vs. the device's own publish interval.)
2. The firmware `Value Stall` entity is not being captured. Is this a
   topic-shape problem the Python side can solve, or does the ESPHome YAML
   need to change? Note the entity name contains a **space**.
3. Are there remaining paths where a SQLite connection can leak, or where
   two cursors are opened on the same connection while a write transaction
   is open? Both were bugs during development (§5 history in `CHANGELOG.md`).
4. `watchdog_eval_loop()` commits per device but also opens three separate
   read connections per cycle. Is that sound, or is it thrashing?
5. `on_mqtt_message()` classifies the sensor *after* parsing the value but
   the classification is used by the sentinel filter. Verify the ordering
   is correct on every path including the diagnostic branch.

**Architecture / improvement**

6. The whole server is one 3007-line file. Would splitting it improve
   reviewability without breaking the "single-file, easy to deploy" goal?
   If so, what module boundaries make sense?
7. The `alerts` table is queried with `resolved = 0` on every dashboard
   poll (10 s). With a large fleet and many alerts, does this need an
   index or a materialised "active faults" view?
8. Telegram long-polling and Flask share one process. If the dashboard is
   being viewed constantly, does the polling thread starve?
9. Should `OUT_OF_RANGE` support hysteresis (a value must stay out of
   range for N seconds before alerting)? At present a value oscillating
   across a limit would flap the alert.

**Production readiness**

10. What is missing before this runs unattended on a facility host for
    months? (Log rotation ✓, backups — `ops.sh backup` exists but no
    scheduler, alerting on watchdog-self-failure, disk-space monitoring.)
11. The container runs as `HOST_UID:HOST_GID`. On the production host,
    what is the right ownership model for `./data` and `./config`?
12. Is `restart: unless-stopped` + a bind-mounted SQLite file safe across
    host reboots and Docker upgrades?

---

## 7. Incident history (context for the reviewer)

Three production-affecting incidents during development. Each was a real
bug in this codebase or in the infrastructure, and each produced misleading
"healthy" signals.

### 7.1 Broker outage masquerading as a healthy watchdog

At 16:25 all 31 devices stopped publishing for ~14 minutes.
`GET /health` returned `{"ok": true}` the entire time, because it only
checked whether the MQTT *session* was alive — a broker that is reachable
but not forwarding leaves the session intact.

**Fix:** `/health` now checks packet recency and returns 503 after 10
minutes of silence, and `_watchdog_check_health()` pages the operator with
*"broker reachable but not forwarding"* — a different diagnosis from a
lost session.

**Lesson for the reviewer:** "connected" and "receiving data" are
different facts. Every health signal in this system should assert the
second, not the first.

### 7.2 CO₂ silently stopped for ~3 hours

Seventeen devices stopped publishing CO₂ at 10:28 while temperature and
humidity on the same devices kept flowing. Home Assistant and InfluxDB
both showed plausible values — because ESPHome publishes with `retain: true`
and holds the last state indefinitely. The watchdog correctly detected it
as 17 simultaneous failures, and the systemic-fault rollup correctly
identified it as one shared cause rather than 17 sensor failures.

The devices recovered on their own when the broker recovered. **The fault
was the broker, not the devices.** The firmware's `return {}` range-reject
filter was suspected first and was *not* the cause.

**Still open:** the firmware's `return {}` filter means a genuine
out-of-range reading produces **no MQTT message at all**, which is
indistinguishable from a dead sensor. The firmware author should change it
to `return NAN` so the watchdog can report *"sensor reporting invalid"*
rather than *"stopped publishing"*. This is a change-controlled firmware
revision, outside this repo.

### 7.3 SQLite wedged under concurrent writes

47 open file descriptors on one file; `database is locked` returned 500 on
login; the watchdog loop crashed every cycle so alerts never resolved.
Three causes, all fixed: a correlated `NOT EXISTS` subquery opening a
second read cursor on the write connection; one transaction spanning all
31 devices and holding SQLite's single write lock for the whole pass; and
`on_mqtt_message()` leaking connections on several exit paths.

**Lesson:** WAL mode plus a `busy_timeout` is necessary but not sufficient
when one process has three writers. Commit granularity matters more than
the pragma.

---

## 8. How to run the review

### 8.1 Get the code

```bash
git clone git@github.com-mqtt-watchdog:lerenthal/MQTT-sensor-watchdog-platform.git
cd MQTT-sensor-watchdog-platform
git log --oneline          # 13 commits, all on main
```

The repository is pushed and current. The local test instance at
`/media/yaniv/1tb_WDC/hermes_agent_projects/sensor_gaurd_rails/sensor_watchdog`
has the same content plus runtime state (`data/`, `config/config.yaml`,
`.env`) which are gitignored.

### 8.2 Run it

```bash
cd MQTT-sensor-watchdog-platform
cp .env.example .env
mkdir -p config data backups
cp config.yaml config/config.yaml
# edit config/config.yaml: mqtt.broker / credentials / auth passwords
HOST_UID=$(id -u) HOST_GID=$(id -g) ./ops.sh start
./ops.sh status
```

### 8.3 Review checklist

- [ ] Read `app.py` §4.1 region by region against the structure table
- [ ] Read `docs/architecture.md` and check it matches the code — it was
      written incrementally and may have drifted
- [ ] Work through §6 (13 questions) and §5 (13 known issues)
- [ ] Check `CHANGELOG.md` — it records what was already fixed, so the
      reviewer does not re-propose solved problems
- [ ] Verify the SQLite access patterns: how many connections open per
      watchdog cycle, and are they all closed
- [ ] Look for the specific bug classes from §7: health signals that assert
      the wrong fact; write transactions spanning too much; silent drops

### 8.4 Deliverable

Please produce:

1. **Bugs found** — file, line, severity (data-loss / missed-alarm /
   false-alarm / resource-leak / security), reproduction, and a fix.
2. **Improvements** — ranked by effort vs. value, with an estimate.
3. **Answers to §6**, even where the answer is "this is fine, here is why".
4. **Anything §5 misses** — what should have been documented but was not.
5. **A production-readiness gap list** — what must be fixed before D1–D8
   are genuinely met, especially D4 (wall board) and D5 (Telegram).

---

## 9. Context on the facility

- ~31 ESPHome sensor nodes across production rooms: spawn incubators,
  fruiting rooms, drying, quarantine. Sensors are SHT41/SCD30/SCD41 CO₂,
  BME680, ENS160, MAXIM, Dallas PT100, air-quality particulates.
- MQTT broker: `10.10.17.11:1883`, Mosquitto 2.x in a Docker container on
  an Ubuntu 20.04 host. Telegraf → InfluxDB is the GMP compliance record.
  HA Mosquitto bridge → Home Assistant for operations.
- The watchdog's own host is a separate Ubuntu 24.04 machine on the same
  LAN.
- Facility operates under GMP. Auditability, attributable actions, and
  not producing false alarms are not nice-to-haves; a watchdog that cries
  wolf gets ignored, and one that stays silent during a real failure is
  worse than no watchdog at all.

---

*Prepared for review at commit `68efef7`. Runtime state quoted in §3.3 was
captured from the live instance.*
