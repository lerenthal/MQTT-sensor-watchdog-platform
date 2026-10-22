# Changelog

All notable changes to the MQTT Sensor Watchdog Platform, ordered newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows the date-stamped tag convention (`YYYY-MM-DD`); there
is no SemVer for this project.

## Unreleased

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
  `watchdog.sensor_flatline_min_change_pct` (default 0.1). Both
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

## 2026-10-21 — Initial public release

Initial commit. Flask + MQTT + watchdog + Telegram + dashboard in a
single Python process, packaged in Docker Compose. Schema v1
(`devices`, `alerts`, `audit_logs`).
