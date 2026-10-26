# Changelog

All notable changes to the MQTT Sensor Watchdog Platform, ordered newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows the date-stamped tag convention (`YYYY-MM-DD`); there
is no SemVer for this project.

## Unreleased

### Fixed (code review 2026-10-25)
- **Missed alarm:** per-device `SENSOR_SILENT` (a channel that stopped
  publishing) now fires again. It had been suppressed together with the
  value-flatline guess, so a single dead channel was invisible.
- **Missed alarm:** systemic clusters (same metric silent on ≥3 devices) now
  raise one persistent, Telegram-paged `SYSTEMIC` alert and clear when they
  resolve. Previously the rollup only logged/audited and paged nothing.
- **Missed alarm:** the firmware `Value Stall` / `Sensor Fault` binary sensors
  are now ingested (`binary_sensor/<name>/state`, ON/OFF payload). They were
  dropped before classification, so the authoritative stall verdict never
  reached the watchdog.
- **Missed alarm:** the broker-unreachable and no-data Telegram alerts now fire
  on a state transition (latched), not on a tick landing inside a narrow time
  window — the 60s broker-down page could previously be skipped entirely.
- **Security:** escaped DB-sourced text in `/audit` and `/print` (stored XSS via
  an unauthenticated failed-login username); refuse the committed placeholder
  Flask `secret_key` (auto-generate a strong key on first boot); added CSRF
  tokens and moved snooze/unsnooze/config-restore from GET to POST.
- **False alarm:** `OUT_OF_RANGE` now has a configurable dwell
  (`out_of_range_hysteresis_seconds`) so a value oscillating across a limit no
  longer flaps the alert.
- **Wall board:** flash + buzzer now fire on a new fault and auto-dismiss by
  timestamp (with periodic re-flash while unacknowledged); they no longer reset
  on every 5s poll (the buzzer previously never stopped).
- **Contention:** in-loop audit writes reuse the watchdog transaction's cursor
  instead of opening a second write connection; the Telegram thread closes its
  DB connection on every path.
- **Live reload:** editing Telegram settings in `/admin/config` now rebuilds the
  engine and resumes polling without a restart; the success message is accurate
  about what needs a restart.
- Dashboard per-sensor chips use the configured flatline window instead of a
  hardcoded 300s. Removed a dead/unreachable block in `on_mqtt_message`.

### Changed
- `telegram.enabled` now defaults to `false` in the committed template.
- Config key documented below corrected: the flatline tolerance is the absolute
  `watchdog.sensor_flatline_min_change` (default `0.0` units), not a percentage.

### Added
- **Per-sensor stall detection (schema v5).** New `SENSOR_SILENT` and
  `SUSPECTED_STALL` alerts that watch each individual sensor channel, not
  just the device as a whole. `SENSOR_SILENT` fires when a channel stops
  publishing entirely (config/wiring/power problem); `SUSPECTED_STALL`
  fires when a channel keeps publishing but its value is frozen (dead
  chip, or a genuinely stable room). Either one marks the device
  `DEGRADED`.
- `sensor_readings.last_change_ts` column + `idx_readings_flatline`
  index, maintained inside the existing UPSERT — no extra storage or
  history table needed.
- New config keys: `watchdog.sensor_flatline_seconds` (default 300) and
  `watchdog.sensor_flatline_min_change` (absolute units, default 0.0). Both
  hot-reloadable.
- `POST /device/<id>/reset_stale` — operator-driven reset for a device
  whose stall alert is a known false positive. Resolves open alerts,
  rebases `last_change_ts`, and clears the snooze. Audited as
  `RESET_STALE`.
- Cold-start rebase (`_rebase_stale_readings_on_boot`) so a container
  restart does not immediately mark every channel as stalled.
- `docs/` set with install, configuration, operations, user-guide,
  security, architecture, development, CONTRIBUTING.
- Per-sensor drilldown on the dashboard (`▸`/`▾` toggle per device row).
- CSV-driven sensor metadata (`device_id,location,role`) with full
  upload history at `/admin/sensors/history`.
- SQLite WAL mode + 5s busy-timeout to eliminate contention between
  MQTT thread, watchdog scheduler, and Flask handlers.
- New env-var overrides for secrets (`WATCHDOG_MQTT_BROKER` and friends).

### Changed
- Topic parser handles the actual facility shape
  (`sensor/<name>/state`) rather than an initial guess of
  (`sensor_<n>/<metric>/state`).
- `sensor_readings` was renamed from `(device_id, sensor_id, ...)` to
  `(device_id, sensor_name, ...)` to match the actual data model.
- **Heartbeat counters are now ingested.** Devices publishing
  `<device>_heartbeat_counter` (e.g. `sht41-4_heartbeat_counter`) were
  previously dropped by the metric-type filter, which caused permanent
  false `STALLED` alerts for any device whose firmware stopped emitting
  the heartbeat but kept publishing measurements.
- Dashboard table gained new columns: **Location/Role**, **Sensors**
  (with drilldown), sortable headers, search box.
- Status precedence is now OFFLINE > STALLED > DEGRADED > ONLINE.
- Passwords are upgraded to salted sha256 hashes on first container
  start and removed from `config.yaml`. The bundled commit only carries
  `CHANGE_ME` placeholders.

### Fixed
- MQTT silent death after broker restart (now has an explicit Telegram
  alarm after 60s of downtime).
- `database is locked` errors from concurrent writers.
- MQTT callback data leak (used to include the SQLite row ID; now
  matches the spec's `ack:<device_id>` / `snooze:<hours>:<device_id>`).
- Snooze button resolved **all** open alerts of a device instead of
  the silenced fault type.
- Migration ordering: `_apply_migrations()` now runs before `schema.sql`
  so `CREATE INDEX` statements that reference newly-added columns do not
  fail on existing databases.

### Fixed (follow-up review of the review)

- **Restart artefact in the systemic-fault detector.** The new SYSTEMIC
  cluster logic manufactured a fleet-wide false alarm on every container
  restart: with no packets recorded while the watchdog is down, every
  channel ages past the flatline window at the same moment, so the
  detector reported "temperature silent on 30 devices". That is our own
  downtime, not a fault, and it pages people. The watchdog now records
  which channels it has *observed alive* since it started, and a cluster
  is only reported if at least one channel in the group was observed
  going quiet. Regression test added.
- **Orphaned `/device/<id>/reset_stale` route** had no UI. Added a
  "Reset" button on DEGRADED device rows.
- **The wall board** rendered its own standalone document and so never
  received a CSRF token; it now emits one.
- **Firmware fault messages** now name the specific faulting sensor
  ("firmware reports sensor fault: airquality1_particulate_fault")
  instead of a generic string.

### Verified after the review fixes

- The ESPHome firmware diagnostics are now ingested and reaching the
  watchdog from 22 devices: `value_stall`, `sensor_fault` and per-probe
  fault entities, published as ON/OFF under `binary_sensor/<name>/state`.
  All `value_stall` entities read clear, confirming that disabling the
  server-side flatline heuristic removed only false positives. Three real
  hardware faults were identified that the watchdog could not have found
  on its own: `airmon-1_scd30_fault`, `airquality1_particulate_fault`
  and `airquality2_particulate_fault`.
- `web.secret_key` was regenerated on first boot of the new code (the
  live config carried the committed placeholder). Existing browser
  sessions were invalidated once, as expected.

## 2026-10-21 — Initial public release

Initial commit. Flask + MQTT + watchdog + Telegram + dashboard in a
single Python process, packaged in Docker Compose. Schema v1
(`devices`, `alerts`, `audit_logs`).

## 2026-10-26 - Ignore list, persistent highlighting, working restart diagnostics

### Fixed
- **MQTT reconnect storm.** Both instances used the hardcoded client_id
  `facility_watchdog_server`. Mosquitto kicks the older session when a
  second client connects with the same id, so the two containers pushed
  each other off the broker continuously - 222 connects and 221
  disconnects in 30 minutes. Each instance now uses a unique id
  (`WATCHDOG_INSTANCE_ID`, set to `live` and `pilot` in the compose
  files). Result: 1 connect, 0 disconnects.
- **Snooze button missing from the table.** The compact one-line rows
  carried no snooze control; it existed only inside the expanded detail
  panel. The Snooze column now holds a dropdown (1h / 8h / 24h / 7d) on
  every row, and a snoozed device shows a Resume button.
- **Restart button reported success it could not deliver.** The API
  published with QoS 0 and never checked the result, so a command lost
  in a reconnect looked identical to a successful restart. It now
  publishes with QoS 1, waits for the broker acknowledgement, refuses
  to publish while the client is disconnected, and reports a device
  with no MQTT button as a failure with an explanation.

### Added
- Ignore list: `watchdog.ignored_devices` in the config, an
  **Ignore List** page at `/admin/ignored` linked from the top
  navigation, and audited `IGNORE_ADDED` / `IGNORE_REMOVED` events.
  An ignored device is hidden from the table and excluded from
  alerting; its readings are still recorded.
- Persistent status highlighting: a faulty device keeps an amber tint
  until it is snoozed, so a fresh failure looks different from a
  long-standing one and a handled fault looks calm.
- Button topics are now recorded from the device's own `button/*/state`
  topic, which is the evidence used to decide whether a device is
  restartable.
- `verify_features.py` and `verify_restart.sh` - end-to-end checks for
  the dashboard features and the restart path.

### Changed
- The wall board's alarm test button is now large, high-contrast and
  labelled **TEST ALARM**, so it can be found on a TV across a room.
- The Snooze column was widened (7% -> 10%) to fit the dropdown.

## 2026-10-26 - Wall board alarm controls, configuration help

### Added
- Five alarm tone styles: chime, warble, siren (default), klaxon and
  continuous. Each is a distinct tone pattern with its own waveform and
  gain, ordered by how intrusive it is. The site default is
  `watchdog.buzzer_style` in the config; an unknown value is rejected
  on save rather than silently falling back. The dropdown on the wall
  board auditions a style without changing the site default.
- **Configuration help** on the Admin Config page: every configuration
  key documented with its purpose, default, unit and the practical
  consequence of changing it. The YAML editor is unforgiving - a typo
  in a key name is silently ignored - so the reference sits next to it.

### Changed
- The alarm test button is now a toggle. Press once to start the tone
  and the screen flash; press again to stop. The button shows what it
  will do next and turns amber while the alarm runs. A test that
  cannot be stopped leaves the operator hunting for the TV remote.
- The wall board ribbon now always shows the alarm controls and the
  clock together at the right, with the clock bright and bold instead
  of dim grey. The buzzer and the time are the two things an operator
  must be able to see and reach at a glance.

### Fixed
- A duplicate `#clock` CSS rule later in the stylesheet was overriding
  the bold one, so the clock rendered dim grey. Removed.

## 2026-10-26 - Device restart now actually works

### Fixed
- **The restart button published to a topic no device listens on.** The
  command topic was hardcoded as
  `facility/<device>/env_sensor/button/restart/cmd`. ESPHome derives the
  real topic from the button's name, so it differs per device:
  `button/bme680-1_restart_device/command`,
  `button/maxim4_restart_device/command`,
  `button/scd30-2_restart/command`. Every restart was therefore
  delivered to nobody - and because the publish was acknowledged by the
  broker, it looked like success.

  The watchdog now discovers the real topic from each device's own MQTT
  discovery payload (`homeassistant/button/#`), which is where Home
  Assistant gets it from too. 31 devices are discovered automatically;
  no firmware change is needed.

  Verified end to end: the device's heartbeat counter resets on a
  restart (138 -> 2, and 54 -> 2 on repeat runs) and the LWT status
  cycles online -> offline -> online.

- The device row's Restart button is now disabled, with the reason in the
  tooltip, for any device that publishes no restart entity - instead of
  offering a button that does nothing.

### Added
- `GET /api/restart/topics` - shows the discovered command topic for
  every restartable device. The first thing to check when a restart
  button appears not to work.
- The devices API carries `can_restart` and `restart_topic` per device.
- `tools/verify_restart_e2e.py` - drives the real API and proves the
  device reboots (heartbeat reset plus the LWT status cycle).
- `tools/probe_restart.py` - discovers a device's real restart topic and
  tests it directly.

## 2026-10-26 - Heartbeat column, shared alarm controls, detail row fixes

### Fixed
- **Expanded detail rows detached from their device.** The sort selected
  every <tr> in the table, including the detail row, and re-appended
  them in sorted order. A detail row has no sort values, so it was
  appended to the bottom of the table and the device plus its expanded
  panel looked like two separate devices. The sort now selects only the
  device rows and re-attaches each detail row to its own device.
- **The filter could leave a hidden device's detail panel on screen, and
  counted detail rows as devices.** Both now operate on device rows only,
  and a detail row is always shown or hidden together with its device.
- **Phantom column at the right edge of the table.** The detail row
  spanned 12 columns while the header had 11, so the browser invented a
  column. The server-rendered "Loading..." placeholder had the same
  problem with 11. Both colspans are now derived: the client counts the
  real header at runtime, the server counts a shared header constant.
- The Dockerfile did not copy `alarm.js`, so the shared alarm module was
  missing from the image entirely.

### Added
- **Heartbeat column** (HB) back in the device table, between Last Seen
  and Snooze, and sortable. It shows the counter the watchdog itself uses
  to judge a stall.
- **Alarm controls on every page.** The TEST / MUTE / tone-style control
  now sits in the header ribbon of the dashboard as well as the wall
  board, so the alarm can be tested and muted from any tab. The engine
  moved into a new shared `alarm.js` - the tone list is defined once and
  both pages use it.
- **MUTE** silences the local alarm only. It deliberately does not stop
  the Telegram paging or the wall board's visual alarm: a muting mistake
  on a screen must never quiet the phone in someone's pocket.
- `pilot-ops.sh rebuild` - the pilot needs it for the same reason the
  live instance does.
- `tools/test_table_structure.js` - asserts the header and the row
  renderer produce the same columns in the same order, and that no
  hardcoded colspan remains.
- `tools/test_row_handling.js` - asserts that no code path treats a
  detail row as a device.

### Changed
- The front-end assets are baked into the image, so a JS change needs an
  explicit rebuild. Documented in the operations guide.
