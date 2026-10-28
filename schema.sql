-- SQLite Schema for Mushroom Facility Sensor Watchdog Platform
-- Idempotent: safe to run on every startup.
--
-- Migrations beyond CREATE TABLE IF NOT EXISTS are applied at boot via
-- _apply_migrations() in app.py because ALTER TABLE does not support
-- IF NOT EXISTS on all SQLite versions.

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    friendly_name TEXT,
    ip_address TEXT,
    fw_version TEXT,
    location TEXT,                            -- denormalised from device_metadata for fast dashboard reads
    role TEXT,                                -- e.g. 'producer','dup' (free text per CSV upload)
    status TEXT DEFAULT 'ONLINE',            -- 'ONLINE','OFFLINE','STALLED','DEGRADED' (set by watchdog_eval_loop)
    last_seen TIMESTAMP,
    last_heartbeat_time TIMESTAMP,
    last_heartbeat_value INTEGER,
    last_temp REAL,
    last_hum REAL,
    last_co2 REAL,
    snooze_until TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sensor_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,                  -- one per physical ESPHome node
    sensor_name TEXT NOT NULL,                -- e.g. 'maxim5_ground_temperature', informational
    sensor_type TEXT NOT NULL,                -- 'temperature','humidity','co2','light','pressure','heartbeat'
    value REAL NOT NULL,
    ts TIMESTAMP DEFAULT (datetime('now','localtime')),
    last_change_ts TIMESTAMP,                 -- timestamp of last value change; null until second observation
    publish_interval_s REAL,                  -- EWMA of observed seconds between packets (adaptive staleness)
    UNIQUE(device_id, sensor_name, sensor_type)
);

CREATE INDEX IF NOT EXISTS idx_readings_device_sensor
    ON sensor_readings(device_id, sensor_name, sensor_type);
CREATE INDEX IF NOT EXISTS idx_readings_recent
    ON sensor_readings(ts DESC);
CREATE INDEX IF NOT EXISTS idx_readings_flatline
    ON sensor_readings(last_change_ts);

CREATE TABLE IF NOT EXISTS device_metadata (
    device_id TEXT PRIMARY KEY,
    location TEXT,
    role TEXT,
    source TEXT,                              -- 'csv:<upload_id>' or 'manual'
    uploaded_at TIMESTAMP DEFAULT (datetime('now','localtime')),
    uploaded_by TEXT
);

CREATE TABLE IF NOT EXISTS csv_uploads (
    upload_id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_at TIMESTAMP DEFAULT (datetime('now','localtime')),
    saved_by TEXT NOT NULL,
    filename TEXT,
    row_count INTEGER,
    applied INTEGER DEFAULT 0,
    diff_summary TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,                 -- OFFLINE, STALLED, SENSOR_SILENT, SUSPECTED_STALL, OUT_OF_RANGE, SYSTEMIC
                                              -- NOTE: known_degraded, sensor_thresholds and out_of_range are
                                              -- created by _apply_migrations() in app.py, not here (schema v6/v7).
    message TEXT NOT NULL,
    triggered_at TIMESTAMP DEFAULT (datetime('now','localtime')),
    last_nagged_at TIMESTAMP NULL,
    acknowledged INTEGER DEFAULT 0,           -- 0 = No, 1 = Yes
    resolved INTEGER DEFAULT 0,               -- 0 = Active, 1 = Resolved
    FOREIGN KEY(device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT (datetime('now','localtime')),
    device_id TEXT,
    username TEXT,                            -- 'anonymous' for legacy rows or Telegram without a known user
    action TEXT NOT NULL,                     -- 'LOGIN','LOGOUT','LOGIN_FAIL','SNOOZE','UNSNOOZE','ACK','RESTART_SENT','CONFIG_SAVE','CONFIG_ROLLBACK'
    details TEXT
);

CREATE TABLE IF NOT EXISTS config_history (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_at TIMESTAMP DEFAULT (datetime('now','localtime')),
    saved_by TEXT NOT NULL,
    note TEXT,                                -- free-text reason from the editor
    content TEXT NOT NULL                     -- full YAML text snapshot
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Helpful indexes for the watchdog's hot paths
CREATE INDEX IF NOT EXISTS idx_alerts_active
    ON alerts(device_id, alert_type, resolved)
    WHERE resolved = 0;

CREATE INDEX IF NOT EXISTS idx_devices_status
    ON devices(status);

CREATE INDEX IF NOT EXISTS idx_audit_recent
    ON audit_logs(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_config_history_recent
    ON config_history(saved_at DESC);

-- Channels the device no longer publishes. The broker re-delivers the
-- last value of every published topic as a retained message on each
-- reconnect, so a retired channel has to be remembered, not merely
-- deleted from sensor_readings - otherwise it comes straight back.
CREATE TABLE IF NOT EXISTS retired_sensors (
    device_id   TEXT NOT NULL,
    sensor_name TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    retired_at  TIMESTAMP DEFAULT (datetime('now','localtime')),
    reason      TEXT,
    PRIMARY KEY (device_id, sensor_name, sensor_type)
);
