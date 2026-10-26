# =============================================================================
# Mushroom Facility Sensor Watchdog
# =============================================================================
# Independent MQTT subscriber + watchdog evaluator + Telegram nagging engine
# + Flask admin dashboard with login, theme toggle, config editing, audit
# trail, and printable status report.

import os
import re
import sys
import time
import csv
import io
import sqlite3
import secrets
import threading
import hashlib
import difflib
import html as html_lib
import logging
from datetime import datetime, timedelta
from functools import wraps

import requests
import yaml
from flask import (
    Flask, request, jsonify, redirect, url_for, session, abort, Response,
)
from apscheduler.schedulers.background import BackgroundScheduler
import paho.mqtt.client as mqtt

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sensor_watchdog")

# Process start time, used to tell a genuine systemic fault apart from the
# "the watchdog was down, so everything looks stale" artefact.
_PROCESS_START = datetime.now().timestamp()

# Channels this process has actually OBSERVED alive (a packet received since
# we started). A cluster of channels that were all stale before we came up is
# our own downtime, not a fleet-wide fault - we cannot claim to have detected
# something we never watched happen. Keyed by (device_id, sensor_name).
_OBSERVED_ALIVE = set()
_OBSERVED_LOCK = threading.Lock()

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
CONFIG_FILE = os.environ.get("WATCHDOG_CONFIG", "config.yaml")
if not os.path.exists(CONFIG_FILE):
    log.error(f"Configuration file {CONFIG_FILE} not found!")
    sys.exit(1)

with open(CONFIG_FILE, "r") as f:
    config = yaml.safe_load(f)

# Env-var overrides for the items you don't want in a checked-in config file.
# WATCHDOG_MQTT_PASSWORD, WATCHDOG_TG_TOKEN, WATCHDOG_TG_CHAT_ID, WATCHDOG_SECRET_KEY.
def _override(cfg, *keys, value):
    cur = cfg
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value
    return cfg

# Devices on the ignore list are installed but not currently in use. They are
# hidden from the dashboard and excluded from alerting entirely, so a
# decommissioned sensor cannot raise alarms.
def _persist_config():
    """Write the in-memory config back to the config file.

    Used by admin actions that change a key directly (ignore list) rather
    than through the YAML editor.
    """
    try:
        with open(CONFIG_FILE, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
        return True
    except OSError:
        log.exception("could not persist config to %s", CONFIG_FILE)
        return False


def _ignored_devices():
    raw = config.get("watchdog", {}).get("ignored_devices", []) or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(d).strip() for d in raw if str(d).strip()}


for env, path in [
    ("WATCHDOG_MQTT_BROKER",  ("mqtt", "broker")),
    ("WATCHDOG_MQTT_PORT",    ("mqtt", "port")),
    ("WATCHDOG_MQTT_USERNAME",("mqtt", "username")),
    ("WATCHDOG_MQTT_PASSWORD",("mqtt", "password")),
    ("WATCHDOG_TG_TOKEN",     ("telegram", "bot_token")),
    ("WATCHDOG_TG_CHAT_ID",   ("telegram", "chat_id")),
    ("WATCHDOG_TG_ENABLED",   ("telegram", "enabled")),
    ("WATCHDOG_SECRET_KEY",   ("web", "secret_key")),
    ("WATCHDOG_FACILITY_NAME",("facility", "name")),
]:
    if env in os.environ:
        val = os.environ[env]
        # Truthy/falsy for booleans, int for ports
        if env == "WATCHDOG_TG_ENABLED":
            val = val.lower() not in ("0", "false", "no", "")
        elif env == "WATCHDOG_MQTT_PORT":
            val = int(val)
        _override(config, *path, value=val)

DB_FILE = os.environ.get("WATCHDOG_DB", "sensor_watchdog.db")
SCHEMA_FILE = "schema.sql"

mqtt_health = {
    "connected": False,
    "last_connected_at": None,
    "last_disconnected_at": None,
    "reconnect_count": 0,
    "broker": config["mqtt"]["broker"],
    # Alert state, so pages fire on a state TRANSITION rather than on a tick
    # landing inside a narrow time window (review findings B5, B11).
    "down_alerted": False,       # broker-unreachable page already sent this outage
    "last_down_nag_at": None,    # last broker-down reminder
    "nodata_alerted_at": None,   # last "broker up but silent" page
}
mqtt_health_lock = threading.Lock()
_mqtt_client_holder = {"client": None}

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL = multiple readers + one writer without blocking each other.
    # Required because the MQTT thread writes from one connection while
    # the watchdog loop & web handlers each open their own.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # busy_timeout is a fallback in case WAL is somehow disabled.
    conn.execute("PRAGMA busy_timeout=15000")   # tolerate the watchdog loop's write burst
    return conn

def _column_exists(conn, table, column):
    return any(r[1] == column
               for r in conn.execute(f"PRAGMA table_info({table})").fetchall())

def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

def _apply_migrations(conn):
    # schema_meta must exist before the version stamps below. _apply_migrations
    # runs BEFORE schema.sql (so CREATE INDEX statements can see migrated
    # columns), which means on a brand-new DB schema.sql has not yet created
    # schema_meta — without this a fresh install crashes on first boot
    # (review finding B20). Idempotent on an existing DB.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    # v7 -> v8: per-device status history, so the dashboard can tell the
    # operator that a device JUST changed state (and flash the row) rather
    # than showing a static list of colours.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS device_status_history (
            device_id TEXT NOT NULL,
            status     TEXT NOT NULL,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            seen_at    INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_status_history_device
            ON device_status_history(device_id, changed_at DESC);
    """)
    # v6 -> v7: operator-acknowledged known-degraded sensors. These are
    # excluded from alerting so a documented, accepted fault does not drown
    # the dashboard in permanent noise.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS known_degraded (
            device_id   TEXT NOT NULL,
            sensor_name TEXT NOT NULL,
            reason      TEXT,
            suppressed_by TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (device_id, sensor_name)
        );
    """)
    # v5 -> v6: per-entity target ranges.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sensor_thresholds (
            device_id   TEXT NOT NULL,
            sensor_name TEXT NOT NULL,
            min_value   REAL,
            max_value   REAL NOT NULL,
            severity    TEXT NOT NULL DEFAULT 'WARNING',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (device_id, sensor_name)
        );
        CREATE TABLE IF NOT EXISTS out_of_range (
            device_id   TEXT NOT NULL,
            sensor_name TEXT NOT NULL,
            value       REAL,
            severity    TEXT,
            message     TEXT,
            entered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved    INTEGER DEFAULT 0,
            resolved_at TIMESTAMP,
            PRIMARY KEY (device_id, sensor_name)
        );
        CREATE INDEX IF NOT EXISTS idx_thresholds_device
            ON sensor_thresholds(device_id);
    """)
    # v4 -> v5: add sensor_readings.last_change_ts. Must run BEFORE any
    # CREATE INDEX that references it (schema.sql and the embedded script
    # below both do).
    if _table_exists(conn, "sensor_readings") and not _column_exists(conn, "sensor_readings", "last_change_ts"):
        conn.execute("ALTER TABLE sensor_readings ADD COLUMN last_change_ts TIMESTAMP")
        conn.execute("UPDATE sensor_readings SET last_change_ts = ts WHERE last_change_ts IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_flatline ON sensor_readings(last_change_ts)")
        log.info("Migration: added sensor_readings.last_change_ts (v4 -> v5)")
    # The _table_exists guards matter on a FRESH DB: _apply_migrations runs
    # before schema.sql, so these base tables may not exist yet. Without the
    # guard the ALTER raises "no such table" and the first boot crashes
    # (review finding B20). On a fresh DB the tables are created by schema.sql
    # (already carrying these columns) and the second _apply_migrations pass is
    # a no-op; on an upgrade the tables exist and the ALTER runs.
    if _table_exists(conn, "audit_logs") and not _column_exists(conn, "audit_logs", "username"):
        conn.execute("ALTER TABLE audit_logs ADD COLUMN username TEXT")
        log.info("Migration: added audit_logs.username")
    if _table_exists(conn, "devices") and not _column_exists(conn, "devices", "location"):
        conn.execute("ALTER TABLE devices ADD COLUMN location TEXT")
        conn.execute("ALTER TABLE devices ADD COLUMN role TEXT")
        log.info("Migration: added devices.location, devices.role")
    # New tables — embedded schema so the DB migrates from CREATE TABLE IF NOT
    # EXISTS statements; works for both fresh installs and upgrades.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS device_metadata (
            device_id TEXT PRIMARY KEY,
            location TEXT,
            role TEXT,
            source TEXT,                  -- 'csv:<file>' or 'manual'
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            uploaded_by TEXT
        );
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,                -- one per physical ESPHome node
            sensor_name TEXT NOT NULL,              -- e.g. 'maxim5_ground_temperature', informational
            sensor_type TEXT NOT NULL,              -- 'temperature','humidity','co2','light','pressure','heartbeat'
            value REAL NOT NULL,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_change_ts TIMESTAMP,             -- timestamp of last value change; NULL until second observation
            UNIQUE(device_id, sensor_name, sensor_type)
        );
        CREATE INDEX IF NOT EXISTS idx_readings_device_sensor
            ON sensor_readings(device_id, sensor_name, sensor_type);
        CREATE INDEX IF NOT EXISTS idx_readings_recent
            ON sensor_readings(ts DESC);
    """)
    # v3 -> v4: rename sensor_readings.sensor_id to sensor_name (the original
    # design assumed multi-device split; the real hardware uses one device per
    # physical node with informational sub-labels).
    if _column_exists(conn, "sensor_readings", "sensor_id") and not _column_exists(conn, "sensor_readings", "sensor_name"):
        conn.execute("ALTER TABLE sensor_readings RENAME COLUMN sensor_id TO sensor_name")
        log.info("Migration: renamed sensor_readings.sensor_id -> sensor_name")

    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version','8')"
    )

def init_db():
    if not os.path.exists(SCHEMA_FILE):
        log.error(f"Schema file {SCHEMA_FILE} not found!")
        sys.exit(1)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    # Enable WAL once at boot. Idempotent — persists in the DB file header.
    conn.execute("PRAGMA journal_mode=WAL")
    # Migrations run BEFORE schema.sql. Reason: schema.sql contains
    # CREATE INDEX statements that reference columns added by migrations
    # (e.g. sensor_readings.last_change_ts). On an existing DB the
    # CREATE TABLE IF NOT EXISTS statements are no-ops, so those indexes
    # would fail against a table that hasn't been ALTERed yet.
    _apply_migrations(conn)
    with open(SCHEMA_FILE, "r") as f:
        schema = f.read()
    conn.executescript(schema)
    # Run migrations a second time — cheap, idempotent, and it picks up
    # anything schema.sql just created that also needs an ALTER.
    _apply_migrations(conn)
    conn.commit()
    conn.close()
    log.info("Database initialized successfully.")

def audit(action, device_id=None, details="", username=None, cursor=None):
    if username is None:
        username = session.get("username", "anonymous")
    # In-transaction variant: when a caller is already inside an open write
    # transaction (the watchdog loop), it passes its own cursor so the audit
    # row is written on the SAME connection. Opening a second write connection
    # mid-transaction contends for SQLite's single write lock (review finding
    # B10 — the §7.3 "two writers" class). The caller owns the commit.
    if cursor is not None:
        try:
            cursor.execute(
                "INSERT INTO audit_logs (device_id, username, action, details) "
                "VALUES (?, ?, ?, ?)",
                (device_id, username, action, details),
            )
        except Exception:
            log.exception("audit (in-transaction) write failed for action=%s", action)
        return
    conn = None
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO audit_logs (device_id, username, action, details) "
            "VALUES (?, ?, ?, ?)",
            (device_id, username, action, details),
        )
        conn.commit()
    except Exception:
        # An audit write failing must not take down the operator action that
        # triggered it. Log loudly, move on.
        log.exception("audit write failed for action=%s", action)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

# -----------------------------------------------------------------------------
# Authentication
# -----------------------------------------------------------------------------
def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()

def _ensure_password_hashes():
    """Upgrade plaintext passwords to salted sha256 hashes on first boot."""
    users = config.get("auth", {}).get("users", {})
    if not users:
        return
    changed = False
    for _, u in users.items():
        if "password_hash" in u and "password" not in u:
            continue
        if "password" in u:
            salt = secrets.token_hex(8)
            u["password_hash"] = {
                "salt": salt,
                "hash": _hash_password(u["password"], salt),
            }
            del u["password"]
            changed = True
    if changed:
        with open(CONFIG_FILE, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
        log.info("Upgraded plaintext passwords to salted sha256 hashes in config.yaml")

def _ensure_secret_key():
    """Never serve with the committed placeholder Flask secret.

    A predictable/committed signing key lets anyone forge a session cookie
    (e.g. role=admin) and bypass the whole auth table (review finding B4). On
    first boot — when the key is empty, missing, or still a CHANGE_ME
    placeholder — generate a strong random key and persist it to the live
    config so it is stable across restarts (and env WATCHDOG_SECRET_KEY still
    overrides everything).
    """
    web = config.setdefault("web", {})
    key = str(web.get("secret_key") or "")
    weak = (not key) or ("change_me" in key.lower()) or (len(key) < 32)
    if not weak:
        return
    web["secret_key"] = secrets.token_hex(32)
    try:
        with open(CONFIG_FILE, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
        log.warning("Generated a new random web.secret_key (placeholder/weak key "
                    "replaced). Existing browser sessions are invalidated.")
    except OSError:
        # Could not persist (read-only config mount). Still use the strong
        # in-memory key for this run rather than the placeholder.
        log.warning("web.secret_key was weak; using a generated key for this "
                    "run but could not persist it to %s", CONFIG_FILE)

def _check_credentials(username, password):
    users = config.get("auth", {}).get("users", {})
    u = users.get(username)
    if not u or "password_hash" not in u:
        return False, None
    expected = u["password_hash"]["hash"]
    got = _hash_password(password, u["password_hash"]["salt"])
    if secrets.compare_digest(expected, got):
        return True, u.get("role", "operator")
    return False, None

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

# -----------------------------------------------------------------------------
# Telegram Engine
# -----------------------------------------------------------------------------
class TelegramEngine:
    def __init__(self, token, chat_id, enabled=True):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(enabled) and bool(token) and bool(chat_id)
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.session = requests.Session()

    def send_nag_alert(self, device_id, alert_type, message):
        if not self.enabled:
            log.info(f"[MOCK TELEGRAM] Nag Alert for {device_id}: {message}")
            return True
        text = (
            "🚨 <b>FACILITY SENSOR ALERT</b> 🚨\n\n"
            f"<b>Device:</b> <code>{device_id}</code>\n"
            f"<b>Type:</b> {alert_type}\n"
            f"<b>Details:</b> {message}\n"
            f"<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "⚠️ <i>This notification will repeat until acknowledged or snoozed.</i>"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "🔕 Ack (Silence)", "callback_data": f"ack:{device_id}"}],
                [
                    {"text": "💤 Snooze 1h",  "callback_data": f"snooze:1:{device_id}"},
                    {"text": "💤 Snooze 8h",  "callback_data": f"snooze:8:{device_id}"},
                    {"text": "💤 Snooze 24h", "callback_data": f"snooze:24:{device_id}"},
                ],
            ]
        }
        try:
            res = self.session.post(
                f"{self.api_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML", "reply_markup": reply_markup},
                timeout=10,
            )
            if res.status_code == 200:
                log.info(f"Telegram nag alert sent for {device_id}")
                return True
            log.error(f"Telegram API Error: {res.status_code} {res.text[:200]}")
            return False
        except Exception as e:
            log.error(f"Failed to send Telegram notification: {e}")
            return False

    def send_message(self, text, reply_markup=None):
        if not self.enabled:
            log.info(f"[MOCK TELEGRAM] {text}")
            return True
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            res = self.session.post(f"{self.api_url}/sendMessage", json=payload, timeout=10)
            return res.status_code == 200
        except Exception as e:
            log.error(f"Failed to send Telegram message: {e}")
            return False

    def answer_callback(self, callback_id, text):
        if not self.enabled:
            return
        try:
            self.session.post(
                f"{self.api_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text, "show_alert": True},
                timeout=5,
            )
        except Exception as e:
            log.error(f"Failed to answer callback query: {e}")

telegram_engine = TelegramEngine(
    token=config["telegram"]["bot_token"],
    chat_id=config["telegram"]["chat_id"],
    enabled=config["telegram"]["enabled"],
)

# -----------------------------------------------------------------------------
# MQTT helpers
# -----------------------------------------------------------------------------
def _parse_ts(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None

# Metric keywords recognised in an ESPHome sensor_name. Order matters:
# longer / more specific patterns first, because "co2" must win over "co",
# and "ground_temperature" still resolves to temperature.
_SENSOR_METRIC_PATTERNS = (
    ("heartbeat",   ("heartbeat",)),
    ("co2",         ("co2", "co_2", "carbon_dioxide", "eco2", "equivalent_co2")),
    ("pm25",        ("pm2_5", "pm25")),
    ("pm10",        ("pm10", "pm_10")),
    ("pm1",         ("pm1", "pm_1")),
    ("pm4",         ("pm4", "pm_4")),
    ("particle",    ("particle", "particulate", "pm_")),
    ("tvoc",        ("tvoc", "voc")),
    ("no2",         ("no2", "nitrogen_dioxide")),
    ("o2",          ("oxygen",)),
    ("co",          ("carbon_monoxide",)),
    ("nh3",         ("nh3", "ammonia")),
    ("h2",          ("hydrogen",)),
    ("formaldehyde",("formaldehyde", "hcho")),
    ("illuminance", ("lux", "illuminance", "light_lux")),
    ("light",       ("light", "brightness")),
    ("pressure",    ("pressure", "barometric")),
    ("humidity",    ("humidity", "rh", "relative_humidity")),
    ("temperature", ("temperature", "temp")),
    ("wind",        ("wind",)),
    ("moisture",    ("moisture", "soil_moisture")),
    ("soil",        ("soil",)),
    ("power",       ("power", "watt")),
    ("energy",      ("energy", "kwh")),
    ("voltage",     ("voltage", "volt")),
    ("current",     ("current", "amp")),
    ("battery",     ("battery",)),
)


# Name fragments that mark a channel as a diagnostic or configuration value
# rather than an environmental reading. These legitimately hold constant (an
# uptime counter, a revision number, a calibration count) and must not be
# type-matched just because they contain a metric word.
_DIAGNOSTIC_NAME_MARKERS = (
    "uptime",
    "calibration_revision",
    "calibration_count",
    "calibration_lock",
    "last_calibrated",
    "calibration_status",
    "calibration_note",
    "compliance_note",
    "mac_address",
    "ip_address",
    "connected_ssid",
    "wifi_signal",
    "rssi",
    "boot_count",
    "firmware_version",
    "esp_home_version",
    "firmware",
    "version",
    "ssid",
    "address",
    "status",
)


# Diagnostics worth keeping because the device computes them and they answer
# a question the server cannot: 'Value Stall' (the firmware's own comparison
# of CO2/temperature against its previous reading, with a 30 minute
# threshold) and 'Sensor Fault' (no accepted reading within the stale
# window). Both are binary sensors published as text.
_USEFUL_DIAGNOSTICS = ("value_stall", "sensor_fault", "stall", "fault")


def _is_useful_diagnostic(sensor_name):
    lname = (sensor_name or "").lower()
    return any(k in lname for k in _USEFUL_DIAGNOSTICS)


def _classify_sensor(sensor_name):
    """Map an ESPHome sensor_name onto a metric type, or None if it is a
    diagnostic rather than a reading.

    The facility's sensors carry the metric in the name
    ('maxim5_co2', 'airmon-1_pm25', 'bme680-1_pressure'), so the name is the
    only signal available. Order is significant: 'co2' is matched before
    'co', and 'temperature' last so 'ground_temperature' still resolves to
    temperature.

    Diagnostic channels are rejected FIRST. Substring matching alone would
    type 'dual_temp_uptime' as temperature and
    'portable-sense1_co2_calibration_count' as co2 - both legitimately hold
    constant, so they would sit frozen forever and generate permanent false
    stall alerts.
    """
    lname = (sensor_name or "").lower()
    if not lname:
        return None

    for marker in _DIAGNOSTIC_NAME_MARKERS:
        if marker in lname:
            return None

    for metric, needles in _SENSOR_METRIC_PATTERNS:
        for needle in needles:
            if needle in lname:
                return metric
    return None


def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        with mqtt_health_lock:
            was_disconnected = not mqtt_health["connected"]
            mqtt_health["connected"] = True
            mqtt_health["last_connected_at"] = datetime.now()
            if was_disconnected and mqtt_health["last_disconnected_at"]:
                mqtt_health["reconnect_count"] += 1
            # Clear the broker-down alert latch so the next outage pages again.
            mqtt_health["down_alerted"] = False
            mqtt_health["last_down_nag_at"] = None
        client.subscribe(config["mqtt"]["topic_subscription"], qos=0)
        log.info(f"MQTT connected & subscribed to {config['mqtt']['topic_subscription']}")
        if was_disconnected:
            telegram_engine.send_message("✅ <b>MQTT broker reconnected.</b> Watchdog resumed.")
    else:
        log.error(f"MQTT connect failed, rc={rc}")

def on_mqtt_disconnect(client, userdata, rc, properties=None):
    with mqtt_health_lock:
        mqtt_health["connected"] = False
        mqtt_health["last_disconnected_at"] = datetime.now()
    log.warning(f"MQTT disconnected (rc={rc}); paho will auto-reconnect.")

def _retry_mqtt_write(device_id, sensor_name, sensor_type, val, now, tol):
    """Re-attempt a single sensor write on a fresh connection."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        if sensor_type == "heartbeat":
            cursor.execute("""
                INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, value, ts, last_change_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, sensor_name, sensor_type) DO UPDATE SET
                    value = excluded.value, ts = excluded.ts,
                    last_change_ts = CASE WHEN excluded.value = value
                                         THEN last_change_ts ELSE excluded.ts END
            """, (device_id, sensor_name, sensor_type, val, now, now))
        else:
            cursor.execute(f"""
                INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, value, ts, last_change_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, sensor_name, sensor_type) DO UPDATE SET
                    value = excluded.value, ts = excluded.ts,
                    last_change_ts = CASE
                        WHEN ABS(excluded.value - value) <= ? THEN last_change_ts
                        ELSE excluded.ts END
            """, (device_id, sensor_name, sensor_type, val, now, now, tol))
        if sensor_type == "temperature":
            cursor.execute("UPDATE devices SET last_temp=? WHERE device_id=?", (val, device_id))
        elif sensor_type == "humidity":
            cursor.execute("UPDATE devices SET last_hum=? WHERE device_id=?", (val, device_id))
        elif sensor_type == "co2":
            cursor.execute("UPDATE devices SET last_co2=? WHERE device_id=?", (val, device_id))
        elif sensor_type == "heartbeat":
            hb_val = int(val)
            prev = cursor.execute("SELECT last_heartbeat_value FROM devices WHERE device_id=?",
                                 (device_id,)).fetchone()
            if prev is None or prev["last_heartbeat_value"] != hb_val:
                cursor.execute("UPDATE devices SET last_heartbeat_time=?, last_heartbeat_value=? WHERE device_id=?",
                               (now, hb_val, device_id))
        conn.commit()
    finally:
        conn.close()


def on_mqtt_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload_str = msg.payload.decode("utf-8", errors="ignore").strip()
    except Exception:
        return
    parts = topic.split("/")
    # Observed topic shapes on this facility:
    #   facility/<device>/env_sensor/status
    #   facility/<device>/env_sensor/heartbeat_counter/state
    #   facility/<device>/env_sensor/sensor/<name>/<metric>/state
    if len(parts) < 3 or parts[0] != "facility":
        return
    device_id = parts[1]
    sub = parts[2:]   # everything after 'facility/<device>'
    if not sub or sub[0] != "env_sensor":
        return
    sub = sub[1:]     # drop 'env_sensor'

    # One physical ESPHome node == one device_id. The path then looks like:
    #   status                              -> health
    #   heartbeat_counter/state
    #   sensor/<name>/state                 -> reading; metric type is encoded in <name>
    #
    # Observed <name> conventions on this facility:
    #   maxim5_temperature, maxim5_humidity, maxim5_co2, maxim5_ground_temperature,
    #   scd30_1, sht41-1, ens160-1, ...   (some are pure IDs without an encoded type)
    sensor_name = ""
    metric_path = ""
    is_binary = False
    is_button = False
    is_button_state = False

    if sub and sub[0] == "status":
        metric_path = "status"

    elif sub and sub[0] == "heartbeat_counter":
        # raw layout: heartbeat_counter/state
        metric_path = "/".join(sub) if sub else ""

    elif sub and sub[0] == "sensor" and len(sub) >= 2:
        # Layout: sensor/<name>/state, sensor/<name>/config, sensor/<name>/<anything>
        # The trailing /state segment is the ESPHome telemetry convention; treat it
        # as a reading. Metric type is encoded in <name>.
        sensor_name = sub[1]
        # everything after <name> becomes the metric path so we can detect '/state'
        # vs '/config' vs deeper paths.
        sub = sub[2:]
        metric_path = "/".join(sub) if sub else ""

    elif sub and sub[0] == "button" and len(sub) >= 2:
        # ESPHome's MQTT button component publishes its state under
        # <prefix>/button/<name>/state and listens on
        # <prefix>/button/<name>/cmd. Only the STATE topic proves the
        # device has an MQTT button; the cmd topic merely proves something
        # was published there.
        sensor_name = sub[1]
        sub = sub[2:]
        metric_path = "/".join(sub) if sub else ""
        is_button = True
        is_button_state = metric_path.endswith("/state")

    elif sub and sub[0] == "binary_sensor" and len(sub) >= 2:
        # ESPHome publishes binary sensors (the firmware's own 'Value Stall' /
        # 'Sensor Fault' verdicts) to binary_sensor/<name>/state with an
        # ON/OFF text payload — a DIFFERENT topic shape and payload type than
        # numeric sensors. Previously this branch fell through to the final
        # 'return' and the firmware's authoritative stall verdict was silently
        # discarded (review finding B6). Route it like a sensor but flag it so
        # the reading handler decodes the text payload instead of float().
        # NOTE: confirm the exact slugified topic segment against one real
        # retained message before trusting it in production.
        sensor_name = sub[1]
        sub = sub[2:]
        metric_path = "/".join(sub) if sub else ""
        is_binary = True

    else:
        # Unknown substructure under env_sensor; skip silently to avoid log spam.
        return
    now = datetime.now()
    # Hoisted so the lock-retry handler always has it in scope.
    # Absolute tolerance: "the value has not moved by more than N units".
    # A percentage was wrong here - a CO2 sensor publishing whole ppm sits
    # on 449.0 for many minutes in a stable room, and 0.1% of 449 is
    # 0.45 ppm, below the device's own 1 ppm granularity, so the reading
    # could never be seen to change. Units, not percentages, is what
    # "frozen" means for a physical measurement.
    tol = float(config.get("watchdog", {}).get("sensor_flatline_min_change", 0.0))
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO devices (device_id, last_seen) VALUES (?, ?)",
            (device_id, now),
        )
        cursor.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (now, device_id))

        if metric_path == "status":
            value = payload_str.lower()
            if value == "offline":
                cursor.execute("UPDATE devices SET status = 'OFFLINE' WHERE device_id = ?", (device_id,))
            elif value == "online":
                cursor.execute("UPDATE devices SET status = 'ONLINE' WHERE device_id = ?", (device_id,))
        elif "heartbeat_counter" in metric_path and not metric_path.endswith("/config"):
            try:
                val = int(float(payload_str))
                row = cursor.execute(
                    "SELECT last_heartbeat_value FROM devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if row and (row["last_heartbeat_value"] is None or row["last_heartbeat_value"] != val):
                    cursor.execute(
                        "UPDATE devices SET last_heartbeat_time = ?, "
                        "last_heartbeat_value = ? WHERE device_id = ?",
                        (now, val, device_id),
                    )
            except (ValueError, TypeError):
                pass
        elif is_button:
            # Only the device's own button STATE topic is evidence that
            # it has an MQTT button component. A /cmd topic is just an
            # echo of a command somebody published.
            if not is_button_state:
                conn.close()
                return
            try:
                conn.execute("""
                    INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, value, ts, last_change_ts)
                    VALUES (?, ?, 'button', 1.0, ?, ?)
                    ON CONFLICT(device_id, sensor_name, sensor_type) DO UPDATE SET
                        value = excluded.value, ts = excluded.ts
                """, (device_id, sensor_name, now, now))
                conn.commit()
                conn.close()
                return
            except Exception:
                conn.close()
                return
        elif is_binary:
            # Firmware binary sensor ('Value Stall' / 'Sensor Fault'). Payload
            # is ON/OFF text, not a float, so decode it explicitly and store as
            # a 'diagnostic' reading (1.0 = active, 0.0 = clear). Only the
            # firmware's own fault verdicts are kept; every other binary sensor
            # is dropped as noise. Review finding B6.
            if not _is_useful_diagnostic(sensor_name):
                return
            v = payload_str.strip().lower()
            if v in ("on", "true", "1", "yes", "pressed"):
                val = 1.0
            elif v in ("off", "false", "0", "no", "released"):
                val = 0.0
            else:
                return
            sensor_type = "diagnostic"
            cursor.execute("""
                INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, value, ts, last_change_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, sensor_name, sensor_type) DO UPDATE SET
                    value = excluded.value,
                    ts    = excluded.ts,
                    last_change_ts = CASE
                        WHEN excluded.value = value THEN last_change_ts
                        ELSE excluded.ts
                    END
            """, (device_id, sensor_name, sensor_type, val, now, now))
            conn.commit()
            conn.close()
            return
        else:
            # Sensor reading. Match metric type against sensor_name (the ESPHome
            # convention here is to encode the type in the name, e.g.
            # 'maxim5_temperature', 'maxim5_co2'). Drop readings whose name
            # has no recognisable metric, or whose payload isn't numeric.
            # Examples we drop cleanly: 'maxim5_mac_address', 'maxim5_uptime',
            # 'maxim5_connected_ssid' — they're useful telemetry, not readings.
            try:
                val = float(payload_str)
                if val != val:  # NaN guard (NaN != NaN)
                    return
            except (ValueError, TypeError):
                return

            # Classify FIRST - the sentinel checks below need to know the
            # metric type, and sensor_type must be bound before it is used.
            sensor_type = _classify_sensor(sensor_name)
            if sensor_type is None:
                # Not an environmental metric. A useful diagnostic published as
                # a plain (numeric) sensor is kept under the 'diagnostic' type;
                # everything else (mac address, ip, ssid) is dropped as noise.
                # The firmware's ON/OFF stall/fault binary sensors are handled
                # in the is_binary branch above, not here.
                if _is_useful_diagnostic(sensor_name):
                    sensor_type = "diagnostic"
                else:
                    return

            # ESPHome publishes placeholder values for a sensor that has not
            # produced a real reading yet: 0 for an unpopulated channel, and
            # -273.15 for an uninitialised temperature. Storing those freezes
            # the channel on a value that is not a measurement, which then
            # reads as a permanent stall. Ignore them; the channel keeps its
            # last real value until a genuine one arrives.
            if sensor_type == "temperature" and val <= -100.0:
                return
            if sensor_type in ("pm1", "pm4", "pm10", "pm25", "particle") and val == 0.0:
                return
            if sensor_type == "battery" and val == 0.0:
                return
            # Record that we saw this channel alive. Used to distinguish a
            # genuine simultaneous failure (we watched it happen) from the
            # "watchdog was down, everything looks stale" artefact.
            with _OBSERVED_LOCK:
                _OBSERVED_ALIVE.add((device_id, sensor_name))

            # Per-sensor row. UPSERT: update value+ts on every packet, but only
            # advance last_change_ts when the value moved by more than the
            # absolute tolerance watchdog.sensor_flatline_min_change (default
            # 0.0 units, i.e. any movement counts). This lets us detect a
            # value-flatline (sensor stuck) without a historical readings table.
            # Heartbeat counters are exempt from the tolerance: they are exact
            # integer counters and any increment is a real tick.
            if sensor_type == "heartbeat":
                cursor.execute("""
                    INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, value, ts, last_change_ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, sensor_name, sensor_type) DO UPDATE SET
                        value = excluded.value,
                        ts    = excluded.ts,
                        last_change_ts = CASE
                            WHEN excluded.value = value THEN last_change_ts
                            ELSE excluded.ts
                        END
                """, (device_id, sensor_name, sensor_type, val, now, now))
            else:
                cursor.execute(f"""
                    INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, value, ts, last_change_ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, sensor_name, sensor_type) DO UPDATE SET
                        value = excluded.value,
                        ts    = excluded.ts,
                        last_change_ts = CASE
                            WHEN ABS(excluded.value - value) <= ? THEN last_change_ts
                            ELSE excluded.ts
                        END
                """, (device_id, sensor_name, sensor_type, val, now, now, tol))
            # Roll up to devices for fast dashboard reads (last value wins per device, any sensor).
            if sensor_type == "temperature":
                cursor.execute(
                    "UPDATE devices SET last_temp = ? WHERE device_id = ?", (val, device_id)
                )
            elif sensor_type == "humidity":
                cursor.execute(
                    "UPDATE devices SET last_hum = ? WHERE device_id = ?", (val, device_id)
                )
            elif sensor_type == "co2":
                cursor.execute(
                    "UPDATE devices SET last_co2 = ? WHERE device_id = ?", (val, device_id)
                )
            elif sensor_type == "heartbeat":
                # Roll the heartbeat up onto the device row. The STALLED rule
                # reads devices.last_heartbeat_time, so this MUST be updated
                # whenever a heartbeat packet arrives -- otherwise the device
                # is flagged STALLED forever regardless of what the sensor
                # is actually doing.
                hb_val = int(val)
                prev = cursor.execute(
                    "SELECT last_heartbeat_value FROM devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                # Only stamp the timestamp when the counter actually moved.
                # A device that keeps re-sending the same number is stalled.
                if prev is None or prev["last_heartbeat_value"] != hb_val:
                    cursor.execute(
                        "UPDATE devices SET last_heartbeat_time = ?, "
                        "last_heartbeat_value = ? WHERE device_id = ?",
                        (now, hb_val, device_id),
                    )
            # heartbeat/light/pressure intentionally not denormalised — dashboard only shows
            # the headline metrics; per-sensor view shows the rest.
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as e:
        # Contention with the watchdog's write transaction. The MQTT thread
        # must not drop the packet: the next publish of the same value
        # arrives within seconds anyway, but losing it would leave a stale
        # reading on the dashboard. Retry briefly, then give up quietly.
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            for attempt in range(3):
                time.sleep(0.4 * (attempt + 1))
                try:
                    _retry_mqtt_write(device_id, sensor_name, sensor_type,
                                      val, now, tol)
                    return
                except sqlite3.OperationalError:
                    continue
                except Exception:
                    break
            log.warning("on_mqtt_message: dropped %s after retries (%s)",
                        topic, e)
            return
        log.exception(f"on_mqtt_message failed for topic={topic}: {e}")
    except Exception as e:
        log.exception(f"on_mqtt_message failed for topic={topic}: {e}")
    finally:
        # Every exit path must release the connection. Leaked SQLite
        # connections hold read locks and starve the writer, which is how
        # 47 open descriptors on one file turns into a wedged database.
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

def start_mqtt():
    # A unique client_id per instance. Mosquitto enforces "one connection
    # per client_id" by kicking the OLDER session when a new one arrives
    # with the same id - so two watchdog instances sharing a hardcoded id
    # will push each other off the broker in a reconnect loop. That loop
    # silently breaks publishes (a restart command lands in a connection
    # that dies a second later).
    _suffix = os.environ.get("WATCHDOG_INSTANCE_ID", "").strip()
    if not _suffix:
        # Fall back to the web port, which differs between instances.
        _suffix = str(config.get("web", {}).get("port", "5050"))
    client = mqtt.Client(client_id=f"sensor-watchdog-{_suffix}")
    if config["mqtt"].get("username"):
        client.username_pw_set(config["mqtt"]["username"], config["mqtt"]["password"])
    client.reconnect_delay_set(
        min_delay=config["mqtt"]["reconnect_min_delay_seconds"],
        max_delay=config["mqtt"]["reconnect_max_delay_seconds"],
    )
    client.on_connect = on_mqtt_connect
    client.on_disconnect = on_mqtt_disconnect
    client.on_message = on_mqtt_message
    try:
        client.connect(
            config["mqtt"]["broker"], config["mqtt"]["port"],
            keepalive=config["mqtt"]["keepalive_seconds"],
        )
        client.loop_start()
        log.info(f"MQTT client started ({config['mqtt']['broker']}:{config['mqtt']['port']})")
    except Exception as e:
        log.error(f"MQTT initial connect failed: {e}; reconnect will continue in background.")
    return client

# -----------------------------------------------------------------------------
# Watchdog
# -----------------------------------------------------------------------------
def _raise_or_nag(cursor, device_id, fault_type, fault_msg, nag_interval_s, now):
    active = cursor.execute(
        "SELECT * FROM alerts WHERE device_id = ? AND alert_type = ? "
        "AND resolved = 0 ORDER BY id DESC LIMIT 1",
        (device_id, fault_type),
    ).fetchone()
    if active is None:
        cursor.execute(
            "INSERT INTO alerts (device_id, alert_type, message, last_nagged_at) "
            "VALUES (?, ?, ?, ?)",
            (device_id, fault_type, fault_msg, now),
        )
        telegram_engine.send_nag_alert(device_id, fault_type, fault_msg)
        return
    last_nagged = _parse_ts(active["last_nagged_at"])
    if (
        active["acknowledged"] == 0
        and (last_nagged is None or (now - last_nagged).total_seconds() > nag_interval_s)
    ):
        telegram_engine.send_nag_alert(device_id, fault_type, fault_msg)
        cursor.execute("UPDATE alerts SET last_nagged_at = ? WHERE id = ?", (now, active["id"]))

def _detect_systemic_faults(conn, now, flatline_limit):
    """Find faults that share a metric AND a time window across many devices.

    A single broker, ACL or network fault will stop one metric on every
    device at once. Reported individually that is N alarming alerts; reported
    as one cluster it is one actionable finding. We require BOTH:

      - the same sensor_type across many devices, and
      - their last-packet timestamps clustered inside a short window

    A genuine per-sensor hardware failure fails the second test: ten chips
    dying simultaneously across ten rooms is not a hardware pattern.

    RESTART ARTEFACT (important): if the watchdog itself was not running,
    no packets were recorded for the whole downtime, so EVERY channel ages
    past the window at the same moment and the detector sees a fleet-wide
    "systemic fault" that is really just the watchdog having been down.
    That is a false alarm and it pages people.

    The discriminator is whether we were WATCHING. A channel that last
    published before this process started was already stale when we came
    up - we were not observing it go stale, so we cannot claim to have
    detected a fault in it. Those channels are excluded from clustering.
    A channel that was alive when we started and has since gone quiet IS a
    genuine detection, however soon after the restart that happened.
    """
    # A channel that last published BEFORE this process started was already
    # stale when we came up - we were not watching when it went stale, so we
    # cannot claim to have detected a fault in it. Such channels are excluded
    # from clustering: otherwise every restart manufactures a fleet-wide
    # "systemic fault" that is really just our own downtime.
    #
    # Conversely, a channel that WAS alive when we started and has since
    # gone quiet is a genuine detection - we were watching it.
    _started_at = datetime.fromtimestamp(_PROCESS_START)

    # NOTE: this function is called with a plain connection (no row_factory),
    # so rows are tuples - index them positionally, never by name.
    rows = conn.execute("""
        SELECT r.device_id, r.sensor_type, r.ts, r.sensor_name
        FROM sensor_readings r
        WHERE r.sensor_type NOT IN ('heartbeat', 'diagnostic')
          AND r.ts IS NOT NULL
    """).fetchall()
    # Suppression applied in Python, for the same reason as in the device
    # loop: a correlated subquery on the write connection deadlocks it.
    _kd = set()
    for _r in conn.execute("SELECT device_id, sensor_name FROM known_degraded").fetchall():
        _kd.add((_r[0], _r[1]))
    rows = [r for r in rows if (r[0], r[3]) not in _kd]

    # Group silent sensors by metric.
    by_metric = {}
    _group_names = {}
    for r in rows:
        device_id, sensor_type, ts, sensor_name = r[0], r[1], r[2], r[3]
        last = _parse_ts(ts)
        if last is None:
            continue
        silent = (now - last).total_seconds()
        silent = (now - last).total_seconds()
        if silent > flatline_limit:
            by_metric.setdefault(sensor_type, []).append((device_id, silent, last))
            _group_names.setdefault(sensor_type, []).append((device_id, sensor_name))

    clusters = []
    for stype, items in by_metric.items():
        # A device can publish several channels that all classify as the same
        # metric (e.g. scd30_1 carries both 'scd30-1_co2' and 'scd30_co2').
        # Count devices, not channels, or one device inflates the cluster.
        per_device = {}
        for dev, silent, _last in items:
            prev = per_device.get(dev)
            if prev is None or silent > prev[0]:
                per_device[dev] = (silent, _last)
        items = [(d, s) for d, (s, _l) in per_device.items()]
        if len(items) < 3:
            continue
        # RESTART ARTEFACT GUARD. If NONE of the channels in this group was
        # observed alive by THIS process, the whole group went stale while
        # the watchdog was down. That is our own downtime, not a fleet-wide
        # sensor fault, and paging it would be a false alarm.
        # If at least one channel in the group was seen alive since we
        # started, the group was observed going stale - a genuine detection.
        # A channel counts as observed if we received a packet from it since
        # we started, OR if its silence began within one flatline window of
        # our start (i.e. we could plausibly have watched it go quiet).
        with _OBSERVED_LOCK:
            _observed = set(_OBSERVED_ALIVE)
        _watch_limit = _started_at + timedelta(seconds=flatline_limit)
        _any_observed = any(
            (dev, name) in _observed
            or per_device[dev][1] >= _watch_limit
            for dev, name in _group_names.get(stype, ())
            if dev in per_device
        )
        if not _any_observed:
            continue
        # Cluster by rounding the silence to the nearest 5 minutes; a shared
        # cause produces a tight cluster, independent failures spread out.
        buckets = {}
        for dev, silent in items:
            key = int(silent // 300)
            buckets.setdefault(key, []).append(dev)
        for key, devs in buckets.items():
            if len(devs) >= 3:
                clusters.append({
                    "sensor_type": stype,
                    "devices": sorted(set(devs)),
                    "count": len(set(devs)),
                    "window_start": key * 300,
                    "window_end": (key + 1) * 300,
                })

    # Keep only the largest cluster per metric — the dominant systemic fault.
    best = {}
    for cl in clusters:
        cur = best.get(cl["sensor_type"])
        if cur is None or cl["count"] > cur["count"]:
            best[cl["sensor_type"]] = cl
    return sorted(best.values(), key=lambda c: -c["count"])


def watchdog_eval_loop():
    """Detect faults in priority order: OFFLINE > STALLED > SUSPECTED_STALL > ONLINE.

    Status precedence at the device level:
      - OFFLINE    — no MQTT traffic past timeout, or LWT offline
      - STALLED    — heartbeat counter (if any) hasn't changed past timeout
      - DEGRADED   — at least one sensor value hasn't changed past sensor_flatline_seconds;
                     the rest of the device looks otherwise healthy
      - ONLINE     — all values flowing

    Per-sensor SUSPECTED_STALL alerts are raised separately and roll the
    device up to DEGRADED.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now()
        offline_limit  = config["watchdog"]["default_offline_timeout_seconds"]
        heartbeat_lim  = config["watchdog"]["default_heartbeat_timeout_seconds"]
        flatline_limit  = int(config["watchdog"].get("sensor_flatline_seconds", 300))
        # Server-side value-flatline detection. Off by default: it cannot
        # distinguish a frozen sensor from a stable room, and on this fleet
        # it generated only false positives. The firmware's own 'Value Stall'
        # entity is the authoritative signal; see docs/configuration.md.
        _flatline_enabled = bool(config["watchdog"].get("sensor_flatline_detection_enabled", False))
        # OUT_OF_RANGE dwell: a reading must stay out of range this long before
        # it pages, so a value oscillating across a limit does not flap the
        # alert (review finding B8). 0 restores the old immediate behaviour.
        oor_hysteresis = int(config["watchdog"].get("out_of_range_hysteresis_seconds", 60))
        nag_interval    = config["telegram"]["nagging_interval_seconds"]

        # Preload the suppressed-sensor names. Doing this as a correlated
        # NOT EXISTS subquery inside the per-device query opened a second
        # read cursor on the same connection while the device loop's write
        # transaction was open, which SQLite reports as "database is locked"
        # and aborts the loop's own writes. A plain in-memory set avoids it.
        _kd = get_db()
        _kd_map = {}
        for _r in _kd.execute("SELECT device_id, sensor_name FROM known_degraded"):
            _kd_map.setdefault(_r["device_id"], set()).add(_r["sensor_name"])
        _kd.close()

        # Detect systemic clusters UP FRONT (own connection), so a (device,
        # metric) pair caught in a cluster can be suppressed at the per-device
        # level before it raises its own SENSOR_SILENT alert and pages. The
        # cluster is reported as one alert after the loop (review finding B2).
        _sc = get_db()
        systemic = _detect_systemic_faults(_sc, now, flatline_limit)
        _sc.close()
        clustered_pairs = {
            (dev, c["sensor_type"]) for c in systemic for dev in c["devices"]
        }

        _ignored = _ignored_devices()
        for dev in cursor.execute("SELECT * FROM devices").fetchall():
            device_id = dev["device_id"]
            if device_id in _ignored:
                # Installed but not in use: no evaluation, no alerts. Clear any
                # stale alert so it does not linger on the wall board.
                cursor.execute(
                    "UPDATE alerts SET resolved = 1 WHERE device_id = ? AND resolved = 0",
                    (device_id,),
                )
                continue
            snooze_until = _parse_ts(dev["snooze_until"])
            if snooze_until and now < snooze_until:
                continue

            last_seen    = _parse_ts(dev["last_seen"])
            last_hb_time = _parse_ts(dev["last_heartbeat_time"])
            prev_status  = dev["status"]

            fault_type = None
            fault_msg  = ""

            # 1. OFFLINE check (highest precedence)
            is_offline_topic = prev_status == "OFFLINE"
            is_silent = last_seen and (now - last_seen).total_seconds() > offline_limit
            if is_offline_topic or is_silent:
                fault_type = "OFFLINE"
                silent_for = int((now - last_seen).total_seconds()) if last_seen else 0
                fault_msg = f"Device silent for {silent_for}s."

            # 2. STALLED (heartbeat) check — only if device isn't already offline
            elif last_hb_time and (now - last_hb_time).total_seconds() > heartbeat_lim:
                fault_type = "STALLED"
                fault_msg = f"Heartbeat counter frozen for {int((now - last_hb_time).total_seconds())}s."

            # 3. Per-sensor checks. Two distinct failure modes, deliberately
            #    separated so the operator knows which one they're chasing:
            #
            #    SENSOR_SILENT   — no packets at all for > flatline window.
            #                      Almost always a config/bus/power problem.
            #    SUSPECTED_STALL — packets ARE arriving but the value hasn't
            #                      changed past the window. Usually a dead chip
            #                      OR a genuinely stable environment.
            #
            #    Both roll the device up to DEGRADED.
            # Heartbeat is excluded from the per-sensor checks: it is a health
            # counter, not an environmental measurement, and the STALLED rule
            # above already covers it. Counting it here would double-report.
            sensor_fault = cursor.execute("""
                SELECT sensor_name, sensor_type, value, ts, last_change_ts,
                       (julianday(?) - julianday(ts)) * 86400.0        AS silent_s,
                       (julianday(?) - julianday(last_change_ts)) * 86400.0 AS flat_s
                FROM sensor_readings
                WHERE device_id = ?
                  AND sensor_type NOT IN ('heartbeat', 'diagnostic')
                  AND last_change_ts IS NOT NULL
                  AND (julianday(?) - julianday(last_change_ts)) * 86400.0 > ?
            """, (now.isoformat(sep=" "),
                  now.isoformat(sep=" "),
                  device_id,
                  now.isoformat(sep=" "),
                  flatline_limit)).fetchall()
            # Apply the suppression filter in Python (see the preload note).
            _suppressed = _kd_map.get(device_id, set())
            if _suppressed:
                sensor_fault = [r for r in sensor_fault
                                if r["sensor_name"] not in _suppressed]

            # The firmware publishes its own 'Value Stall' binary sensor,
            # computed on the device against its previous reading with a
            # 30 minute threshold. That verdict is authoritative: it knows
            # the sensor's history and its own sampling. Where the device
            # reports a stall, trust it; where it reports no stall, the
            # server-side flatline heuristic is only guessing, because a
            # stable room legitimately holds one value for hours.
            _fw = cursor.execute("""
                SELECT sensor_name, value FROM sensor_readings
                WHERE device_id = ? AND sensor_type = 'diagnostic'
            """, (device_id,)).fetchall()
            _fw_stalled = False
            _fw_fault = False
            _fw_detail = []      # names of the specific faulting sensors
            for _r in _fw:
                _n = (_r["sensor_name"] or "").lower()
                # Diagnostics are stored numerically (1.0 = active, 0.0 = clear)
                # by the binary_sensor ingest; tolerate a legacy text value too.
                try:
                    _on = float(_r["value"]) >= 0.5
                except (TypeError, ValueError):
                    _on = str(_r["value"]).strip().lower() in (
                        "on", "true", "1", "yes", "pressed")
                if not _on:
                    continue
                if "stall" in _n:
                    _fw_stalled = True
                    _fw_detail.append(_r["sensor_name"])
                elif "fault" in _n:
                    _fw_fault = True
                    _fw_detail.append(_r["sensor_name"])
            # Split the candidates into two fundamentally different things:
            #   SILENT = no packets at all for > window. This is a FACT, not a
            #            guess, and must always be reported (review finding B1 —
            #            previously it was nuked along with the flatline guess).
            #   FLAT   = packets ARE arriving but the value has not moved. This
            #            is a GUESS: a stable room legitimately holds one value.
            silent = [r for r in sensor_fault if (r["silent_s"] or 0) > flatline_limit]
            flat   = [r for r in sensor_fault if (r["silent_s"] or 0) <= flatline_limit]

            # Suppress SILENT channels that are part of a systemic cluster; the
            # cluster alert raised after the loop carries that information, and
            # paging each device individually is the storm this avoids (B2).
            silent = [r for r in silent
                      if (device_id, r["sensor_type"]) not in clustered_pairs]

            # The FLAT guess is only trusted when the device confirms it with
            # its own 'Value Stall'/'Sensor Fault' verdict, or when the operator
            # has explicitly enabled server-side flatline detection. Otherwise
            # drop it — but SILENT facts above are kept regardless.
            if _fw_stalled or _fw_fault:
                _names = ", ".join(_fw_detail) if _fw_detail else ""
                if _fw_stalled and _fw_fault:
                    _why = "firmware reports value stall and/or sensor fault"
                elif _fw_stalled:
                    _why = "firmware reports value stall"
                else:
                    _why = "firmware reports sensor fault"
                if _names:
                    _why += ": " + _names
                flat = [{
                    "sensor_name": "device",
                    "sensor_type": "diagnostic",
                    "value": 1.0,
                    "ts": now.isoformat(sep=" "),
                    "last_change_ts": now.isoformat(sep=" "),
                    "silent_s": 0.0,
                    "flat_s": 0.0,
                    "_fw": _why,
                }]
            elif not _flatline_enabled:
                flat = []

            sensor_fault = silent + flat

            if not fault_type and sensor_fault:
                # SENSOR_SILENT takes precedence over SUSPECTED_STALL per sensor.
                if silent:
                    worst = max(silent, key=lambda r: r["silent_s"])
                    fault_type = "SENSOR_SILENT"
                    fault_msg = (
                        f"Sensor {worst['sensor_name']} ({worst['sensor_type']}) "
                        f"stopped publishing {int(worst['silent_s'])}s ago."
                    )
                elif flat and flat[0].get("_fw"):
                    fault_type = "SUSPECTED_STALL"
                    fault_msg = f"Device {device_id}: {flat[0]['_fw']}."
                else:
                    worst = max(flat, key=lambda r: r["flat_s"])
                    fault_type = "SUSPECTED_STALL"
                    fault_msg = (
                        f"Sensor {worst['sensor_name']} ({worst['sensor_type']}) "
                        f"publishing but value unchanged for {int(worst['flat_s'])}s."
                    )

            # Update device.status
            if fault_type:
                if fault_type in ("SUSPECTED_STALL", "SENSOR_SILENT"):
                    new_status = "DEGRADED"
                else:
                    new_status = fault_type
            else:
                new_status = "ONLINE"

            if prev_status != new_status:
                cursor.execute(
                    "UPDATE devices SET status = ? WHERE device_id = ?",
                    (new_status, device_id),
                )
                # Feature 2: record the transition so the dashboard can flash
                # the row and show what it changed FROM. Without this the
                # operator sees a static colour and cannot tell a fresh
                # failure from a long-standing one.
                cursor.execute(
                    "INSERT INTO device_status_history(device_id, status, changed_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (device_id, new_status),
                )

            # Resolve alerts that no longer apply. This must be PER ALERT
            # TYPE, not "resolve everything when the device is healthy": a
            # device that is OFFLINE also has fault_type == "OFFLINE", so the
            # old blanket branch never ran for it and its SUSPECTED_STALL
            # alert stayed open forever.
            if fault_type is None:
                cursor.execute(
                    "UPDATE alerts SET resolved = 1 WHERE device_id = ? AND resolved = 0",
                    (device_id,),
                )
            else:
                # This device still has a fault of type `fault_type`. Resolve
                # the OTHER fault types it is no longer in.
                cursor.execute(
                    "UPDATE alerts SET resolved = 1 WHERE device_id = ? "
                    "AND resolved = 0 AND alert_type != ?",
                    (device_id, fault_type),
                )
            # Raise / re-nag alert
            if fault_type:
                _raise_or_nag(cursor, device_id, fault_type, fault_msg, nag_interval, now)

            # Commit per device. One transaction spanning the whole fleet
            # holds SQLite's single write lock for the entire pass, which
            # collides with the MQTT ingest thread and the web handlers on
            # every cycle. Per-device commits keep each write window short.
            conn.commit()

        # Release the write lock acquired by the device loop before doing the
        # heavier read-heavy passes below. WAL permits one writer; holding
        # this transaction across those scans blocks every MQTT ingest and
        # every web request, which is what produced the intermittent
        # "database is locked" on login.
        conn.commit()

        # Per-entity target ranges. Only evaluated on FRESH readings: a dead
        # channel must report SENSOR_SILENT, not also claim to be out of range
        # based on a stale value.
        # NOTE: use a SEPARATE connection. The device loop below iterates a
        # cursor on `conn` while writing; opening a second read cursor on the
        # same connection mid-iteration makes SQLite raise "database is
        # locked" and, because it happens inside the loop's transaction, it
        # blocks every other writer (MQTT thread, web handlers) until the
        # transaction unwinds.
        _rc = get_db()
        range_faults = _rc.execute("""
            SELECT t.device_id, t.sensor_name, t.min_value, t.max_value, t.severity,
                   r.value, r.ts
            FROM sensor_thresholds t
            JOIN sensor_readings r
              ON r.device_id = t.device_id AND r.sensor_name = t.sensor_name
            WHERE r.sensor_type != 'heartbeat'
              AND (julianday(?) - julianday(r.ts)) * 86400.0 <= ?
        """, (now.isoformat(sep=" "), max(flatline_limit, 120))).fetchall()
        # Resolve current breach state in the same read-only pass so the loop
        # below does no reads at all - only writes. entered_at drives the dwell.
        range_state = {
            (x["device_id"], x["sensor_name"]): (x["resolved"], x["entered_at"])
            for x in _rc.execute(
                "SELECT device_id, sensor_name, resolved, entered_at FROM out_of_range")
        }
        _rc.close()

        for rf in range_faults:
            dev, name, lo, hi, sev, val, ts = rf
            breach = (lo is not None and val < lo) or (val > hi)
            prev = range_state.get((dev, name))
            open_now = prev is not None and prev[0] == 0

            if breach and not open_now:
                # First cycle out of range: start the dwell clock. Do NOT page
                # yet — the value must persist out of range for oor_hysteresis
                # seconds before it alerts (review finding B8).
                side = ("below %.1f" % lo) if (lo is not None and val < lo) else ("above %.1f" % hi)
                msg = "%s is %.2f (%s, limit %.1f-%.1f)" % (
                    name, val, side, lo if lo is not None else -9999, hi)
                conn.execute("""
                    INSERT INTO out_of_range(device_id, sensor_name, value, severity, message)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, sensor_name) DO UPDATE SET
                        value=excluded.value, severity=excluded.severity,
                        message=excluded.message, resolved=0, resolved_at=NULL,
                        entered_at=CURRENT_TIMESTAMP
                """, (dev, name, val, sev, msg))
                if oor_hysteresis <= 0:
                    _raise_or_nag(cursor, dev, "OUT_OF_RANGE", msg, nag_interval, now)
                    log.warning("OUT_OF_RANGE %s: %s", dev, msg)
            elif breach and open_now:
                # Still out of range - refresh value/message. Page only once the
                # dwell has elapsed; _raise_or_nag dedups so re-calling is safe.
                entered = _parse_ts(prev[1])
                dwell_ok = entered is None or (now - entered).total_seconds() >= oor_hysteresis
                side = ("below %.1f" % lo) if (lo is not None and val < lo) else ("above %.1f" % hi)
                msg = "%s is %.2f (%s, limit %.1f-%.1f)" % (
                    name, val, side, lo if lo is not None else -9999, hi)
                conn.execute(
                    "UPDATE out_of_range SET value=?, message=? WHERE device_id=? AND sensor_name=?",
                    (val, msg, dev, name),
                )
                if dwell_ok:
                    _raise_or_nag(cursor, dev, "OUT_OF_RANGE", msg, nag_interval, now)
            elif not breach and open_now:
                conn.execute(
                    "UPDATE out_of_range SET resolved=1, resolved_at=CURRENT_TIMESTAMP "
                    "WHERE device_id=? AND sensor_name=?",
                    (dev, name),
                )
                conn.execute(
                    "UPDATE alerts SET resolved=1 WHERE device_id=? AND alert_type='OUT_OF_RANGE' "
                    "AND resolved=0 AND message LIKE ?",
                    (dev, "%" + name + "%"),
                )
                audit("OUT_OF_RANGE_CLEARED", device_id=dev,
                      details="%s back in range" % name, username="watchdog", cursor=cursor)
                log.info("OUT_OF_RANGE cleared: %s %s", dev, name)

        # Systemic faults: many devices losing the same metric in the same
        # window points at the broker, network or an ACL change rather than at
        # N independent sensors. The clusters were detected up front (so the
        # per-device loop already suppressed the clustered SENSOR_SILENT noise);
        # here we raise ONE persistent, nagging alert per cluster so it is
        # actually paged to Telegram and shown on every board, and resolve
        # clusters that have cleared (review finding B2). Previously this path
        # only logged + audited and paged nothing.
        SYSTEMIC_PREFIX = "__systemic__:"
        already_open = {
            r["device_id"] for r in cursor.execute(
                "SELECT device_id FROM alerts "
                "WHERE alert_type = 'SYSTEMIC' AND resolved = 0"
            ).fetchall()
        }
        current_ids = set()
        for c in systemic:
            syn = SYSTEMIC_PREFIX + c["sensor_type"]
            current_ids.add(syn)
            msg = "%s silent on %d devices (%s)" % (
                c["sensor_type"], c["count"], ", ".join(c["devices"]))
            # Also resolve any lingering per-device SENSOR_SILENT rows for the
            # clustered devices, in case one slipped through before the cluster
            # formed.
            for dev in c["devices"]:
                cursor.execute(
                    "UPDATE alerts SET resolved = 1 "
                    "WHERE device_id = ? AND alert_type = 'SENSOR_SILENT' "
                    "AND resolved = 0 AND message LIKE ?",
                    (dev, f"%{c['sensor_type']}%"),
                )
            _raise_or_nag(cursor, syn, "SYSTEMIC", msg, nag_interval, now)
            if syn not in already_open:
                # New cluster — audit once on the transition, not every cycle.
                log.warning("Systemic fault detected: %s", msg)
                audit("SYSTEMIC_FAULT", details=msg, username="watchdog", cursor=cursor)
        # Resolve clusters that are no longer present.
        for syn in already_open - current_ids:
            cursor.execute(
                "UPDATE alerts SET resolved = 1 WHERE device_id = ? "
                "AND alert_type = 'SYSTEMIC' AND resolved = 0",
                (syn,),
            )
            audit("SYSTEMIC_CLEARED", details=syn.split(":", 1)[-1],
                  username="watchdog", cursor=cursor)
            log.info("Systemic fault cleared: %s", syn)

        conn.commit()
        conn.close()
    except Exception:
        log.exception("watchdog_eval_loop crashed")

# -----------------------------------------------------------------------------
# Telegram polling
# -----------------------------------------------------------------------------
def telegram_polling_thread():
    # Loop forever rather than returning when Telegram is disabled, so that
    # enabling it at runtime (via /admin/config, which rebuilds the engine)
    # resumes polling without a process restart (review finding B12).
    offset = 0
    while True:
        if not telegram_engine.enabled:
            time.sleep(10)
            continue
        timeout = config["telegram"].get("poll_timeout_seconds", 25)
        try:
            res = telegram_engine.session.get(
                f"{telegram_engine.api_url}/getUpdates?offset={offset}&timeout={timeout}",
                timeout=timeout + 5,
            ).json()
            if not res.get("ok"):
                time.sleep(5)
                continue
            for update in res.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" not in update:
                    continue
                cb = update["callback_query"]
                parts = (cb.get("data") or "").split(":")
                chat = cb.get("from", {}).get("username") or "telegram"
                cb_id = cb["id"]
                # try/finally so the connection is released on every path,
                # including exceptions — a leak here starves the writer, which
                # is the §7.3 "47 open descriptors" failure (review finding B14).
                conn = get_db()
                try:
                    cursor = conn.cursor()
                    if parts[0] == "ack" and len(parts) == 2:
                        dev_id = parts[1]
                        cursor.execute("UPDATE alerts SET acknowledged = 1 WHERE device_id = ? AND resolved = 0", (dev_id,))
                        cursor.execute(
                            "INSERT INTO audit_logs (device_id, username, action, details) "
                            "VALUES (?, ?, 'ACK', 'Acknowledged via Telegram')",
                            (dev_id, chat),
                        )
                        conn.commit()
                        telegram_engine.answer_callback(cb_id, f"Alert for {dev_id} acknowledged.")
                    elif parts[0] == "snooze" and len(parts) == 3:
                        try:
                            hours = int(parts[1]); dev_id = parts[2]
                        except ValueError:
                            telegram_engine.answer_callback(cb_id, "Invalid snooze payload.")
                            continue
                        snooze_until = datetime.now() + timedelta(hours=hours)
                        cursor.execute("UPDATE devices SET snooze_until = ? WHERE device_id = ?", (snooze_until, dev_id))
                        cursor.execute("UPDATE alerts SET resolved = 1 WHERE device_id = ? AND resolved = 0", (dev_id,))
                        cursor.execute(
                            "INSERT INTO audit_logs (device_id, username, action, details) "
                            "VALUES (?, ?, 'SNOOZE', ?)",
                            (dev_id, chat, f"Snoozed for {hours}h via Telegram"),
                        )
                        conn.commit()
                        telegram_engine.answer_callback(cb_id, f"{dev_id} snoozed for {hours} hours.")
                    else:
                        telegram_engine.answer_callback(cb_id, "Unknown action.")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception:
            log.exception("telegram polling loop crashed; sleeping 5s")
            time.sleep(5)

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = config["web"]["secret_key"]
app.permanent_session_lifetime = timedelta(hours=12)
# Harden the session cookie. Over plain HTTP (no TLS yet, §5.8) Secure can't be
# forced, but HttpOnly + SameSite=Lax blunt the common theft/CSRF vectors.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# -----------------------------------------------------------------------------
# CSRF protection (review finding B9)
# -----------------------------------------------------------------------------
# All state-changing requests go through POST and must carry a per-session
# token, supplied either as a hidden `_csrf` form field or an `X-CSRFToken`
# header (for fetch() calls). GET/HEAD/OPTIONS are never state-changing here.
def _csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["_csrf"] = tok
    return tok

def _csrf_input():
    return f'<input type="hidden" name="_csrf" value="{_csrf_token()}">'

@app.before_request
def _csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    # /login is the pre-auth credential POST; there is no session yet to bind a
    # token to, and it is not a cross-site state change on an existing session.
    if request.endpoint == "login":
        return
    expected = session.get("_csrf")
    sent = request.form.get("_csrf") or request.headers.get("X-CSRFToken", "")
    if not expected or not sent or not secrets.compare_digest(expected, sent):
        abort(400)

BASE_CSS = """
:root {
  --bg: #f5f7fb; --surface: #ffffff; --text: #1f2937; --muted: #6b7280;
  --border: #e5e7eb; --accent: #2563eb; --accent-hover: #1d4ed8;
  --success: #16a34a; --warning: #d97706; --danger: #dc2626;
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 4px 12px rgba(0,0,0,.04);
}
[data-theme="dark"] {
  --bg: #0f172a; --surface: #1e293b; --text: #e2e8f0; --muted: #94a3b8;
  --border: #334155; --accent: #60a5fa; --accent-hover: #93c5fd;
  --success: #22c55e; --warning: #f59e0b; --danger: #ef4444;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.3);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system,Segoe UI,system-ui,sans-serif; transition: background .2s, color .2s; }
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hover); }
.app-header {
  background: var(--surface); border-bottom: 1px solid var(--border);
  padding: .75rem 1.5rem; display: flex; align-items: center; gap: 1rem;
  position: sticky; top: 0; z-index: 100; box-shadow: var(--shadow);
}
.app-header h1 { font-size: 1.1rem; margin: 0; font-weight: 600; }
.nav-tabs { display: flex; gap: .25rem; margin-left: auto; }
.nav-tab {
  padding: .5rem .9rem; border-radius: 999px; font-weight: 500; font-size: .85rem;
  color: var(--muted); background: transparent; border: 1px solid transparent;
}
.nav-tab:hover { background: var(--bg); color: var(--text); }
.nav-tab.active { background: var(--accent); color: white; }
.user-chip { color: var(--muted); font-size: .85rem; }
.container { max-width: 1200px; margin: 1.5rem auto; padding: 0 1.5rem; }
/* The devices table needs the full viewport. On a 16:9 monitor the default
   1200px cap wasted roughly a third of the width, which is what forced
   horizontal scrolling. This variant claims the screen up to a sane ceiling
   so all twelve columns fit at 100% zoom on 1920x1080. */
.container-wide { max-width: 1920px; margin: 1rem auto; padding: 0 1rem; }
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 1.25rem; box-shadow: var(--shadow); margin-bottom: 1.5rem;
}
.card h3 { margin: 0 0 1rem; font-size: 1rem; font-weight: 600; }
/* Responsive devices table.
   The operator frequently runs the browser zoomed below 100% so more of the
   room fits. Layout rules, in priority order:
     1. table-layout: fixed + width:100%  -> columns compress to fit, never
        overflow horizontally.
     2. Per-column max-widths so a long value (a reason string) wraps instead
        of stretching its column.
     3. Below 1200px, drop the least-critical columns (Heartbeat, Last Seen,
        CO2) into the expanded sensor panel, where they are still available.
   table-layout:fixed is the load-bearing part: without it a browser picks
   column widths from content and the table overflows no matter what CSS
   says. */
.table.devices {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: .85rem;
}
.table.devices th, .table.devices td {
    text-align: left;
    padding: .5rem .5rem;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
    /* Break at word boundaries, never mid-word. A device id like
       'sht40-scd30-1_scd30_temperature' must wrap between words, not
       split 'temperature' across two lines. */
    overflow-wrap: normal;
    word-break: keep-all;
    hyphens: none;
}
/* Font size is user-adjustable via the toolbar slider. */
.table.devices { font-size: var(--table-font-size, .85rem); }
.table.devices th { font-size: calc(var(--table-font-size, .85rem) * .82); }
/* A word longer than its column still has to go somewhere - allow it to
   overflow its cell rather than break mid-word, and give the table room. */
.table.devices td { min-width: 0; }
.table.devices .nowrap { white-space: nowrap; }
#font-size-slider {
    width: 110px; accent-color: var(--accent); cursor: pointer;
}
.font-size-label {
    font-size: .75rem; color: var(--muted); white-space: nowrap;
    font-variant-numeric: tabular-nums;
}
.table.devices th {
    color: var(--muted); font-weight: 600; font-size: .7rem;
    text-transform: uppercase; letter-spacing: .03em;
}
.table.devices tbody tr:hover { background: var(--bg); }
/* Column sizing. Sums to ~100%. */
/* Sized against a 1920px viewport minus padding (~1870px usable):
     device 150  location 131  status 94   reason 411  lastseen 131
     hb 94       temp 84       hum 84        co2 94     sensors 224
     snooze 168  actions 206                                            */
/* Feature 3: compact one-line rows. The device row is a single line; the
   detail lives in a second <tr> that only exists while expanded. */
.row-compact td {
    padding: .35rem .5rem;
    vertical-align: middle;
    white-space: nowrap;
}
.row-compact { cursor: pointer; }
.row-compact:hover { background: var(--bg); }
.expand-caret { color: var(--muted); font-size: .8em; }

/* Feature 2: a device that JUST changed state flashes. The highlight is
   removed after FLASH_MS so a long-standing fault looks calm. */
@keyframes rowflash {
    0%, 100% { background: transparent; }
    50%      { background: rgba(250, 204, 21, .35); }
}
tr.row-changed { animation: rowflash 1s 4; }
tr.row-changed td { border-bottom-color: var(--warning); }
/* Persistent highlight for a fault that is still unhandled. Cleared
   automatically once the device is snoozed. */
tr.row-changed-persist td { background: rgba(250, 204, 21, .10); }
tr.row-changed-persist td:first-child { box-shadow: inset 3px 0 0 var(--warning); }
/* The snooze control must be usable directly in the compact row. */
.snooze-ctl { display: inline-flex; align-items: center; gap: .25rem; }
.snooze-ctl .dropdown-menu { right: 0; }
.table.devices td.col-actions { white-space: nowrap; }
/* Persistent tint by severity, so the colour still means something after
   the flash has ended. */
tr.row-offline  td:first-child { box-shadow: inset 3px 0 0 var(--danger); }
tr.row-degraded td:first-child { box-shadow: inset 3px 0 0 #6d28d9; }
tr.row-stalled  td:first-child { box-shadow: inset 3px 0 0 var(--warning); }
.changed-tag {
    display: inline-block; margin-left: .35rem; padding: 0 .35rem;
    border-radius: 4px; background: var(--warning); color: #1f2937;
    font-size: .7rem; font-weight: 700;
}

/* The expanded detail row. */
.detail-row td {
    background: var(--bg); padding: .7rem .8rem; white-space: normal;
    border-left: 3px solid var(--accent);
}
.detail-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: .5rem 1rem;
}
.detail-grid > div { display: flex; flex-direction: column; gap: .15rem; }
.dl { font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.dv { font-size: .85rem; }
.detail-sensors { margin-top: .6rem; padding-top: .5rem; border-top: 1px solid var(--border); }

.col-device    { width: 13%;  }
.col-location  { width: 9%;   }
.col-status    { width: 9%;   }
.col-temp      { width: 5%;   }
.col-hum       { width: 5%;   }
.col-co2       { width: 6%;   }
.col-reason    { width: 20%;  }
.col-sensors   { width: 13%;  }
.col-lastseen  { width: 8%;   }
.col-snooze    { width: 10%;  }
.col-actions   { width: 6%;   }
/* Values are tabular so digits line up column to column. */
.table.devices td.num { font-variant-numeric: tabular-nums; }
/* Narrow viewports: shed the columns an operator can recover from the
   sensor panel, so the remaining ones stay legible instead of all shrinking. */
@media (max-width: 1100px) {
    .table.devices { font-size: .78rem; }
    .table.devices th, .table.devices td { padding: .4rem .35rem; }
    .col-lastseen { display: none; }
    .col-device   { width: 17%; }
    .col-location { width: 12%; }
    .col-status   { width: 11%; }
    .col-reason   { width: 28%; }
    .col-sensors  { width: 14%; }
    .col-snooze   { width: 11%;  }
    .col-actions  { width: 6%;  }
}
@media (max-width: 860px) {
    .col-location, .col-co2 { display: none; }
}
.badge { display: inline-block; padding: .2rem .55rem; border-radius: 999px; font-size: .75rem; font-weight: 600; color: white; }
.badge.badge-online { background: var(--success); }
.badge.badge-stalled { background: var(--warning); }
.badge.badge-offline { background: var(--danger); }
.badge.badge-suspect   { background: #d97706; }
.badge.badge-degraded  { background: #6d28d9; }
.badge.badge-silent     { background: #0891b2; }
/* FIX 2: per-device fault explanation. Collapsed by default; the cell shows
   a one-line summary and expands to the full per-sensor breakdown. */
.reason-box { font-size: .8rem; line-height: 1.35; }
.reason-line { margin-bottom: .3rem; }
.systemic-banner {
    display: flex; align-items: flex-start; gap: .6rem;
    padding: .7rem .9rem; margin-bottom: .9rem;
    border-radius: 10px; border: 1px solid var(--danger);
    background: rgba(220,38,38,.08); color: var(--danger); font-size: .85rem;
}
.systemic-banner strong { color: var(--danger); }
.systemic-banner .muted { color: var(--muted); }
.reason-line:last-child { margin-bottom: 0; }
.reason-summary {
    display: inline-block; vertical-align: middle;
    max-width: calc(100% - 4.5rem);   /* leave room for the toggle button */
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.reason-panel {
    margin-top: .35rem; padding: .4rem; background: var(--bg);
    border-radius: 6px; min-width: 260px;
    max-height: 190px; overflow-y: auto;   /* long fault lists scroll in place */
}
/* FIX 1: keep the expanded sensor panel readable inside a table cell. */
.sensor-line { padding: .1rem 0; }
/* Compact per-sensor chips in the collapsed cell. */
.svwrap { font-size: .82em; line-height: 1.45; }
.svrow { margin-bottom: .15rem; }
.svtype {
    display: inline-block; min-width: 62px; color: var(--muted);
    font-size: .85em; text-transform: uppercase; letter-spacing: .03em;
}
.sv {
    display: inline-block; padding: 0 .35rem; margin: 0 .2rem .15rem 0;
    border-radius: 4px; background: var(--bg); border: 1px solid var(--border);
    white-space: nowrap; font-variant-numeric: tabular-nums;
}
.sv.silent     { border-color: #0891b2; color: #0891b2; }
.sv.flat       { border-color: #d97706; color: #d97706; }
.sv.bad        { border-color: var(--danger); color: var(--danger); font-weight: 600; }
.sv.suppressed { opacity: .5; text-decoration: line-through; }
.table.devices td.col-sensors > div {
    max-height: 190px; overflow-y: auto;
}

.badge.badge-warning   { background: var(--warning); color: white; }
.badge.badge-mqtt-ok { background: var(--success); }
.badge.badge-mqtt-down { background: var(--danger); }
.badge.badge-muted { background: var(--muted); }
.badge.badge-warning { background: var(--warning); color: white; }
.btn {
  display: inline-block; padding: .4rem .85rem; border-radius: 8px;
  font-size: .85rem; font-weight: 500; cursor: pointer; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); transition: all .15s;
}
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn-primary { background: var(--accent); color: white; border-color: var(--accent); }
.btn-primary:hover { background: var(--accent-hover); color: white; border-color: var(--accent-hover); }
.btn-danger { color: var(--danger); border-color: var(--danger); }
.btn-danger:hover { background: var(--danger); color: white; }
.btn-success { color: var(--success); border-color: var(--success); }
.btn-success:hover { background: var(--success); color: white; }
.btn-warning { color: var(--warning); border-color: var(--warning); }
.btn-warning:hover { background: var(--warning); color: white; }
.btn-sm { padding: .25rem .55rem; font-size: .8rem; }
.btn-group { display: inline-flex; gap: .25rem; }
#search { padding: .45rem .75rem; max-width: 340px; }
.sort-arrow { display: inline-block; width: 1em; color: var(--accent); font-weight: 700; }
th[data-sort] { user-select: none; cursor: pointer; }
.dropdown { position: relative; display: inline-block; }
.dropdown-menu {
  display: none; position: absolute; right: 0; top: 100%;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  min-width: 140px; box-shadow: var(--shadow); padding: .25rem 0; z-index: 10;
}
.dropdown.open .dropdown-menu { display: block; }
.dropdown-item { display: block; padding: .35rem .85rem; color: var(--text); }
.dropdown-item:hover { background: var(--bg); }
.ts { font-family: ui-monospace, SF Mono, Consolas, monospace; font-size: .82em; color: var(--muted); }
.muted { color: var(--muted); }
.toolbar { display: flex; gap: .5rem; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; }
input[type=text], input[type=password], textarea, select {
  width: 100%; padding: .55rem .75rem; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font: inherit;
}
textarea { font-family: ui-monospace, monospace; min-height: 380px; resize: vertical; }
input:focus, textarea:focus, select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.flash { padding: .75rem 1rem; border-radius: 8px; margin-bottom: 1rem; }
.flash.success { background: rgba(22,163,74,.1); color: var(--success); border: 1px solid var(--success); }
.flash.error { background: rgba(220,38,38,.1); color: var(--danger); border: 1px solid var(--danger); }
.diff { font-family: ui-monospace, monospace; font-size: .8rem; padding: 1rem; background: var(--bg); border-radius: 8px; border: 1px solid var(--border); overflow-x: auto; white-space: pre-wrap; }
.diff .add { background: rgba(22,163,74,.15); color: var(--success); }
.diff .del { background: rgba(220,38,38,.15); color: var(--danger); }
.login-wrap { max-width: 380px; margin: 5rem auto; padding: 2rem; background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); }
.login-wrap h2 { margin: 0 0 1.5rem; font-size: 1.25rem; }
.field { margin-bottom: 1rem; }
.field label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: .25rem; font-weight: 500; }
@media (max-width: 720px) {
  .container { padding: 0 .75rem; }
  .nav-tabs { flex-wrap: wrap; }
}
"""

def _render_layout(title, body, current_tab=None, wide=False):
    tabs = [
        ("devices", "Devices", "index"),
        ("history", "Audit Log", "audit_page"),
        ("admin", "Admin Config", "admin_config"),
        ("ignored", "Ignore List", "admin_ignored"),
    ]
    if session.get("username"):
        tabs.append(("print", "Print Report", "print_report"))
        tabs.append(("wallboard", "Wall Board", "wallboard"))
        nav_html = "".join(
            f'<a class="nav-tab {"active" if current_tab==tid else ""}" '
            f'href="{url_for(ep)}">{label}</a>'
            for tid, label, ep in tabs
        )
        user_chip = (
            f'<span class="user-chip">Signed in as <strong>{session["username"]}</strong> '
            f'<span class="muted">({session.get("role","operator")})</span></span>'
            f'<button class="btn btn-sm" id="theme-toggle" title="Toggle theme">◐</button>'
            f'<a class="btn btn-sm" href="/logout">Logout</a>'
        )
    else:
        nav_html = ""
        user_chip = ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Sensor Watchdog</title>
<style>{BASE_CSS}</style>
</head>
<body data-theme="light">
<div class="app-header">
  <h1>🍄 Mushroom Facility Sensor Watchdog</h1>
  <nav class="nav-tabs">{nav_html}</nav>
  <div style="display:flex;gap:.5rem;align-items:center;">{user_chip}</div>
</div>
<div class="container{" container-wide" if wide else ""}">{body}</div>
<script>window.CSRF_TOKEN = "{_csrf_token()}";</script>
<script>
(function(){{
  var t = localStorage.getItem('watchdog-theme') || 'light';
  document.body.setAttribute('data-theme', t);
  var btn = document.getElementById('theme-toggle');
  if (btn) btn.addEventListener('click', function(){{
    var cur = document.body.getAttribute('data-theme');
    var nx = cur === 'dark' ? 'light' : 'dark';
    document.body.setAttribute('data-theme', nx);
    localStorage.setItem('watchdog-theme', nx);
  }});
  document.addEventListener('click', function(e){{
    document.querySelectorAll('.dropdown.open').forEach(function(d){{
      if (!d.contains(e.target)) d.classList.remove('open');
    }});
  }});
}})();
</script>
</body></html>
"""

# -----------------------------------------------------------------------------
# Auth routes
# -----------------------------------------------------------------------------
LOGIN_FORM = ('<div class="login-wrap"><h2>Sign in</h2>{flash}<form method="post">'
              '<div class="field"><label>Username</label>'
              '<input type="text" name="username" autofocus></div>'
              '<div class="field"><label>Password</label>'
              '<input type="password" name="password"></div>'
              '<button class="btn btn-primary" type="submit">Sign in</button>'
              '</form></div>')

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        ok, role = _check_credentials(username, password)
        if ok:
            session["username"] = username
            session["role"] = role
            audit("LOGIN", details=f"from {request.remote_addr}")
            return redirect(request.args.get("next") or url_for("index"))
        audit("LOGIN_FAIL", details=f"username={username!r}", username=username)
        return _render_layout(
            "Login",
            LOGIN_FORM.format(flash='<div class="flash error">Invalid credentials.</div>'),
        )
    return _render_layout("Login", LOGIN_FORM.format(flash=""))

@app.route("/logout")
def logout():
    if "username" in session:
        audit("LOGOUT", details="session ended")
        session.clear()
    return redirect(url_for("login"))

# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------
_ASSET_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_js(name):
    try:
        with open(os.path.join(_ASSET_DIR, name)) as fh:
            return fh.read()
    except OSError:
        log.warning("%s not found; that view will be non-interactive", name)
        return "console.error('%s missing');" % name

DASHBOARD_JS = _load_js("dashboard.js")
WALLBOARD_JS = _load_js("wallboard.js")


@app.route("/")
@login_required
def index():
    refresh = config['web']['dashboard_refresh_seconds']
    body = f"""<div class="toolbar">
  <span id="mqtt-badge" class="badge badge-mqtt-down">MQTT: ???</span>
  <span class="badge badge-muted">Auto-refresh: {refresh}s</span>
  <input id="search" placeholder="Search devices, location, role, status…"
         style="max-width:340px;margin-left:1rem"
         oninput="dashboardSearch()">
  <span id="row-count" class="muted"></span>
  <span class="font-size-label" id="font-size-label">A</span>
  <input id="font-size-slider" type="range" min="10" max="20" step="1"
         title="Table font size" oninput="setFontSize(this.value)">
  <button class="btn btn-sm" onclick="expandAll()" title="Expand every sensor panel">&#9662; Expand sensors</button>
  <button class="btn btn-sm" onclick="collapseAll()" title="Collapse all sensor panels and fault details">&#9656; Collapse all</button>
  <span style="margin-left:auto"><a class="btn btn-primary" href="{{{{ url_for('print_report') }}}}">🖨 Print Report</a></span>
</div>
<div class="card">
  <h3>Monitored Devices <span class="muted" style="font-weight:400;font-size:.85rem">— click a column header to sort</span></h3>
  <div id="systemic-banner"></div>
  <div style="overflow-x:auto">
    <table class="table devices">
      <thead><tr>
        <th class="col-device"    data-sort="device">Device <span class="sort-arrow"></span></th>
        <th class="col-location"  data-sort="location">Loc / Role <span class="sort-arrow"></span></th>
        <th class="col-status"    data-sort="status">Status <span class="sort-arrow"></span></th>
        <th class="col-temp"      data-sort="temp">°C <span class="sort-arrow"></span></th>
        <th class="col-hum"       data-sort="hum">RH% <span class="sort-arrow"></span></th>
        <th class="col-co2"       data-sort="co2">CO2 <span class="sort-arrow"></span></th>
        <th class="col-reason"    data-sort="reason">Reason <span class="sort-arrow"></span></th>
        <th class="col-sensors">Sensors</th>
        <th class="col-lastseen"  data-sort="lastseen">Last Seen <span class="sort-arrow"></span></th>
        <th class="col-snooze">Snooze</th>
        <th class="col-actions">Actions</th>
      </tr></thead>
      <tbody id="devices-tbody"><tr><td colspan="11" class="muted">Loading…</td></tr></tbody>
    </table>
  </div>
</div>
<script>window.REFRESH_SECONDS = {refresh}; window.CSRF_TOKEN = "{_csrf_token()}";</script>
<script>{DASHBOARD_JS}</script>
"""
    return _render_layout("Dashboard", body, current_tab="devices", wide=True)

@app.route("/api/devices")
@login_required
def api_devices():
    """Devices list + per-device sensor counts/values for the dashboard.

    Sensors are stored in `sensor_readings` (one row per device+sensor+type);
    we aggregate here so the JS poll is a single request, not N+1.
    """
    conn = get_db()
    # Feature 1: devices on the ignore list are not shown at all.
    _ignored = _ignored_devices()
    devices = [dict(r) for r in conn.execute(
        "SELECT * FROM devices ORDER BY device_id ASC"
    ).fetchall() if r["device_id"] not in _ignored]
    sensor_rows = conn.execute("""
        SELECT device_id, sensor_name, sensor_type, value, ts, last_change_ts
        FROM sensor_readings
    """).fetchall()
    now_s = datetime.now().isoformat(sep=" ")

    # Active fault alerts, so the dashboard can explain *why* a device is in
    # its current status rather than making the operator go dig for it.
    fault_rows = conn.execute("""
        SELECT device_id, alert_type, message, triggered_at
        FROM alerts
        WHERE resolved = 0
        ORDER BY triggered_at ASC
    """).fetchall()
    faults_by_device = {}
    for r in fault_rows:
        faults_by_device.setdefault(r["device_id"], []).append({
            "alert_type": r["alert_type"],
            "message": r["message"],
            "triggered_at": r["triggered_at"],
        })
    conn.close()

    sensors_by_device = {}
    for r in sensor_rows:
        silent_s = flat_s = None
        if r["ts"]:
            silent_s = int((datetime.now()
                            - _parse_ts(r["ts"])).total_seconds())
        if r["last_change_ts"]:
            flat_s = int((datetime.now()
                          - _parse_ts(r["last_change_ts"])).total_seconds())
        sensors_by_device.setdefault(r["device_id"], []).append({
            "sensor_name": r["sensor_name"],
            "sensor_type": r["sensor_type"],
            "value": r["value"],
            "ts": r["ts"],
            "last_change_ts": r["last_change_ts"],
            "silent_seconds": silent_s,
            "flat_seconds": flat_s,
        })

    flatline_limit = int(config.get("watchdog", {}).get("sensor_flatline_seconds", 300))

    # Per-entity target ranges and current in/out state, for dashboard colouring.
    thresholds = {}
    try:
        tc = get_db()
        for r in tc.execute(
            "SELECT device_id, sensor_name, min_value, max_value, severity "
            "FROM sensor_thresholds"
        ):
            thresholds.setdefault(r["device_id"], {})[r["sensor_name"]] = {
                "min": r["min_value"], "max": r["max_value"], "severity": r["severity"],
            }
        tc.close()
    except Exception:
        log.exception("threshold lookup failed")

    # Operator-acknowledged known-degraded sensors. Excluded from alerting,
    # but still shown so the operator can see the sensor exists and is
    # knowingly suppressed.
    hconn = get_db()
    try:
        kc = get_db()
        degraded = {}
        for r in kc.execute(
            "SELECT device_id, sensor_name, reason, suppressed_by, created_at "
            "FROM known_degraded"
        ):
            degraded.setdefault(r["device_id"], []).append({
                "sensor_name": r["sensor_name"],
                "reason": r["reason"],
                "suppressed_by": r["suppressed_by"],
                "created_at": r["created_at"],
            })
        kc.close()
    except Exception:
        log.exception("known_degraded lookup failed")
        degraded = {}

    for d in devices:
        thr = thresholds.get(d["device_id"], {})
        d["known_degraded"] = degraded.get(d["device_id"], [])
        s = sensors_by_device.get(d["device_id"], [])
        # Decorate each reading with its range verdict so the UI can colour it.
        for x in s:
            t = thr.get(x["sensor_name"])
            if t and x["sensor_type"] != "heartbeat":
                lo, hi = t["min"], t["max"]
                v = x["value"]
                x["threshold"] = t
                x["in_range"] = not ((lo is not None and v < lo) or v > hi)
            else:
                x["threshold"] = None
                x["in_range"] = None
        kd_names = {k["sensor_name"] for k in d.get("known_degraded", [])}
        for x in s:
            x["known_degraded"] = x["sensor_name"] in kd_names
        d["thresholds"] = thr
        d["sensors"] = s
        d["sensor_count"] = len({x["sensor_name"] for x in s})
        d["location"] = d.get("location") or ""
        d["role"] = d.get("role") or ""
        d["faults"] = faults_by_device.get(d["device_id"], [])

        # Per-sensor health, mirroring the watchdog's own classification so the
        # operator sees exactly what the watchdog saw.
        healthy, suspect, silent = [], [], []
        for x in s:
            if x["sensor_type"] in ("heartbeat", "diagnostic"):
                continue  # health counter / firmware verdict, not a reading
            if x["silent_seconds"] is not None and x["silent_seconds"] > flatline_limit:
                silent.append(x)
            elif x["flat_seconds"] is not None and x["flat_seconds"] > flatline_limit:
                suspect.append(x)
            else:
                healthy.append(x)
        d["sensor_health"] = {
            "healthy": len(healthy),
            "suspected": len(suspect),
            "silent": len(silent),
        }

        # Human-readable reason for the device's status, built from the same
        # signals the watchdog used. Empty string when healthy.
        reasons = []
        st = d["status"]
        if st == "OFFLINE":
            seen = _parse_ts(d.get("last_seen"))
            if seen:
                reasons.append("no MQTT traffic for %ds"
                               % int((datetime.now() - seen).total_seconds()))
            else:
                reasons.append("never seen on the broker")
        elif st == "STALLED":
            hb = _parse_ts(d.get("last_heartbeat_time"))
            if hb:
                reasons.append("heartbeat frozen for %ds"
                               % int((datetime.now() - hb).total_seconds()))
            else:
                reasons.append("heartbeat counter not received")
        elif st == "DEGRADED":
            for x in silent:
                reasons.append("%s stopped publishing %ds ago"
                               % (x["sensor_name"], x["silent_seconds"]))
            for x in suspect:
                reasons.append("%s value frozen for %ds"
                               % (x["sensor_name"], x["flat_seconds"]))
            if not reasons and d["faults"]:
                reasons.append(d["faults"][0]["message"])
        d["reason"] = "; ".join(reasons)
        d["reason_detail"] = d["faults"]
        # Feature 2: was this status reached recently, and what was it before?
        # The dashboard uses this to flash a changed row and to say
        # "was ONLINE, now OFFLINE".
        try:
            hist = hconn.execute(
                "SELECT status, changed_at FROM device_status_history "
                "WHERE device_id = ? ORDER BY changed_at DESC LIMIT 2",
                (d["device_id"],),
            ).fetchall()
            d["status_changed_at"] = hist[0]["changed_at"] if hist else None
            d["previous_status"] = hist[1]["status"] if len(hist) > 1 else None
        except Exception:
            d["status_changed_at"] = None
            d["previous_status"] = None

    # Systemic fault rollup for the dashboard banner. Needs its own
    # connection because the main one is closed by this point.
    systemic = []
    sysconn = None
    try:
        flatline_limit = int(config.get("watchdog", {}).get("sensor_flatline_seconds", 300))
        sysconn = get_db()
        systemic = _detect_systemic_faults(sysconn, datetime.now(), flatline_limit)
    except Exception:
        log.exception("systemic fault detection failed")
        systemic = []
    finally:
        if sysconn is not None:
            try:
                sysconn.close()
            except Exception:
                pass

    with mqtt_health_lock:
        health = dict(mqtt_health)
    # Expose the server's flatline window so the dashboard's per-sensor chips
    # flag "silent/frozen" at the same threshold the watchdog uses, instead of
    # a hardcoded 300s that disagreed with the configured value (finding B16).
    return jsonify({"devices": devices, "mqtt": health, "systemic": systemic,
                    "flatline_limit": flatline_limit})

@app.route("/api/devices/<device_id>/sensors")
@login_required
def api_device_sensors(device_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT sensor_name, sensor_type, value, ts
        FROM sensor_readings
        WHERE device_id = ?
        ORDER BY sensor_name, sensor_type
    """, (device_id,)).fetchall()
    conn.close()
    return jsonify({"device_id": device_id, "sensors": [dict(r) for r in rows]})

@app.route("/snooze/<device_id>/<int:hours>", methods=["POST"])
@login_required
def set_snooze(device_id, hours):
    snooze_until = datetime.now() + timedelta(hours=hours)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE devices SET snooze_until = ? WHERE device_id = ?", (snooze_until, device_id))
    cursor.execute("UPDATE alerts SET resolved = 1 WHERE device_id = ? AND resolved = 0", (device_id,))
    conn.commit(); conn.close()
    audit("SNOOZE", device_id=device_id, details=f"Web snooze for {hours}h")
    return redirect(url_for("index"))

@app.route("/unsnooze/<device_id>", methods=["POST"])
@login_required
def unsnooze(device_id):
    conn = get_db()
    conn.execute("UPDATE devices SET snooze_until = NULL WHERE device_id = ?", (device_id,))
    conn.commit(); conn.close()
    audit("UNSNOOZE", device_id=device_id, details="Unsnoozed via web")
    return redirect(url_for("index"))


@app.route("/device/<device_id>/reset_stale", methods=["POST"])
@login_required
def reset_device_stale(device_id):
    """Operator-driven reset for a device whose STALLED alert is a false
    positive -- e.g. the firmware stops publishing the heartbeat counter but
    measurements are still flowing. Resolves the device's open alerts and
    rebases last_change_ts so SUSPECTED_STALL doesn't immediately re-fire.
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE alerts SET resolved = 1 WHERE device_id = ? AND resolved = 0",
        (device_id,),
    )
    cursor.execute("""
        UPDATE sensor_readings
        SET last_change_ts = CURRENT_TIMESTAMP
        WHERE device_id = ?
    """, (device_id,))
    cursor.execute(
        "UPDATE devices SET status = 'ONLINE', snooze_until = NULL WHERE device_id = ?",
        (device_id,),
    )
    conn.commit(); conn.close()
    audit("RESET_STALE", device_id=device_id,
          details="Operator reset; alerts resolved, last_change_ts rebased.")
    return redirect(url_for("index"))


@app.route("/api/restart/<device_id>", methods=["POST"])
@login_required
def api_restart(device_id):
    client = _mqtt_client_holder["client"]
    if client is None:
        return jsonify({"ok": False, "error": "MQTT client not initialized"}), 503
    topic = f"facility/{device_id}/env_sensor/button/restart/cmd"
    conn = None
    try:
        conn = get_db()
        if not client.is_connected():
            # paho would queue this and it would go out on the next connect,
            # or vanish. Say so plainly instead of reporting a success the
            # operator cannot rely on.
            return jsonify({
                "ok": False,
                "error": "MQTT client is not connected - the command was NOT sent",
            }), 503
        info = client.publish(topic, "PRESS", qos=1, retain=False)
        # qos=1 so the broker acknowledges; a queued-but-unsent message is
        # then visible rather than silently lost.
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            return jsonify({
                "ok": False,
                "error": "MQTT publish was not acknowledged by the broker",
            }), 502

        # A device whose firmware has no MQTT button component will not act on
        # the command, and the publish will look like a success. The
        # observable proxy is whether the device has EVER published a topic
        # under its own button/ subtree - ESPHome's button component
        # publishes its state there. If it never has, this firmware has no
        # MQTT button, so say so instead of implying the device rebooted.
        _btn = conn.execute(
            "SELECT COUNT(*) FROM sensor_readings WHERE device_id = ? "
            "AND sensor_type = 'button'",
            (device_id,),
        ).fetchone()[0] if conn else 0
        # Only rows recorded from a genuine button STATE topic count. The
        # recorder ignores /cmd topics, so this is the device's own evidence
        # that it has an MQTT button component.
        has_mqtt_button = _btn > 0

        if has_mqtt_button:
            audit("RESTART_SENT", device_id=device_id,
                  details=f"Published to {topic} (qos=1, acked)")
            log.info(f"Restart command published for {device_id} on {topic}")
            return jsonify({"ok": True, "topic": topic,
                            "note": "Command delivered to the broker. The device "
                                    "will reboot if its firmware has an MQTT "
                                    "button on this topic."})

        # No evidence of an MQTT button on this device. Report it honestly:
        # the command reached the broker, but nothing is listening, so the
        # device will not reboot. The operator needs to know that before
        # they walk to the device expecting it to come back.
        audit("RESTART_COMMAND_UNACKNOWLEDGED", device_id=device_id,
              details=f"Published to {topic} (acked) but device has no MQTT "
                      f"button topic - firmware change required")
        log.warning(
            "Restart command for %s was published to %s but the device has "
            "never published a button/ topic, so it has no MQTT button "
            "component and will not reboot. Firmware change required.",
            device_id, topic)
        return jsonify({
            "ok": False,
            "published": True,
            "topic": topic,
            "error": "Command reached the broker, but this device has no MQTT "
                     "button component, so it will NOT reboot. Its firmware "
                     "needs a button: - platform: mqtt with "
                     "command_topic: button/restart/cmd.",
        }), 502
    except Exception as e:
        log.exception("Restart publish failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

# -----------------------------------------------------------------------------
# Audit log
# -----------------------------------------------------------------------------
@app.route("/audit")
@login_required
def audit_page():
    conn = get_db()
    rows = conn.execute(
        "SELECT timestamp, device_id, username, action, details "
        "FROM audit_logs ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    # Escape every DB-sourced field: audit rows carry attacker-controllable
    # text (an unauthenticated failed-login username is recorded verbatim, and
    # device/sensor names come straight off the broker) — review finding B3.
    rows_html = "".join(
        f'<tr><td class="ts">{(r["timestamp"] or "")[:19]}</td>'
        f'<td>{html_lib.escape(r["username"] or "")}</td>'
        f'<td>{html_lib.escape(r["action"] or "")}</td>'
        f'<td>{html_lib.escape(r["device_id"] or "")}</td>'
        f'<td class="muted">{html_lib.escape(r["details"] or "")}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="5" class="muted">No audit entries yet.</td></tr>'
    body = f"""<div class="card">
  <h3>Audit Log <span class="muted" style="font-weight:400;font-size:.85rem">— last 200 entries</span></h3>
  <div style="overflow-x:auto">
    <table class="table">
      <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Device</th><th>Details</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>"""
    return _render_layout("Audit Log", body, current_tab="history")

def _parse_sensor_csv(text):
    """Parse and validate a sensor-metadata CSV upload.

    Supported header (case-insensitive, any column order):
        device_id, location, role

    Return: (rows, errors) where errors is a list of human-readable strings.
    """
    errors = []
    rows = []
    if not text.strip():
        return rows, ["CSV is empty"]
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return rows, ["CSV has no rows"]
    header_norm = [h.strip().lower() for h in header]
    if "device_id" not in header_norm:
        return rows, ["CSV must include a 'device_id' column"]
    di  = header_norm.index("device_id")
    li  = header_norm.index("location")      if "location"      in header_norm else None
    ri  = header_norm.index("role")          if "role"          in header_norm else None
    si  = header_norm.index("sensor_name")   if "sensor_name"   in header_norm else None
    mini= header_norm.index("min_value")     if "min_value"     in header_norm else None
    maxi= header_norm.index("max_value")     if "max_value"     in header_norm else None
    sevi= header_norm.index("severity")      if "severity"      in header_norm else None
    if si is not None and maxi is None:
        return [], ["CSV has a 'sensor_name' column but no 'max_value' - a range needs an upper limit"]

    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(not (c or "").strip() for c in raw):
            continue
        # Pad short rows so indexed access doesn't IndexError.
        while len(raw) < len(header):
            raw.append("")
        device_id = (raw[di] or "").strip()
        if not device_id:
            errors.append(f"line {line_no}: missing device_id")
            continue
        location = (raw[li].strip() if li is not None and li < len(raw) else "")
        role     = (raw[ri].strip() if ri is not None and ri < len(raw) else "")

        # Optional per-entity threshold columns. Rows without sensor_name are
        # metadata-only, which keeps the original two-column format working.
        def col(idx):
            return (raw[idx].strip() if idx is not None and idx < len(raw) else "")
        sname = col(si)
        minv  = col(mini)
        maxv  = col(maxi)
        sev   = col(sevi).upper() or "WARNING"

        # Severity is only meaningful on a row that actually defines a range.
        # A metadata-only row (no sensor_name) leaves it blank and must not be
        # rejected for it.
        defines_range = bool(sname) and bool(maxv)
        if defines_range and sev not in ("WARNING", "CRITICAL"):
            errors.append("line %d: severity must be WARNING or CRITICAL" % line_no)
            continue

        # Parse numerics defensively - a malformed number is a rejected row,
        # not a 500 from deep inside float().
        min_val = max_val = None
        bad = False
        for label, txt in (("min_value", minv), ("max_value", maxv)):
            if not txt:
                continue
            try:
                v = float(txt)
            except ValueError:
                errors.append("line %d: %s must be a number (got %r)"
                              % (line_no, label, txt))
                bad = True
                break
            if label == "min_value":
                min_val = v
            else:
                max_val = v
        if bad:
            continue
        if defines_range and max_val is not None and min_val is not None \
                and min_val >= max_val:
            errors.append("line %d: min_value must be below max_value" % line_no)
            continue

        rows.append({
            "device_id": device_id,
            "location":  location,
            "role":      role,
            "sensor_name": sname,
            "min_value": min_val,
            "max_value": max_val,
            "severity":  sev,
        })
    return rows, errors


@app.route("/admin/sensors/csv", methods=["POST"])
@login_required
def admin_sensors_csv():
    if session.get("role") != "admin":
        abort(403)
    csv_text = request.form.get("csv_text", "")
    filename = (request.form.get("filename") or "pasted.csv").strip()
    note = (request.form.get("note") or "").strip()
    rows, errors = _parse_sensor_csv(csv_text)
    if errors or not rows:
        return _render_layout(
            "Admin — CSV upload failed",
            f'<div class="card"><div class="flash error">CSV rejected: {html_lib.escape("; ".join(errors) or "no rows")}</div>'
            f'<a class="btn" href="/admin/config">← Back</a></div>',
            current_tab="admin",
        )
    # Save to csv_uploads for the audit trail before applying.
    diff_summary = f"rows={len(rows)}; first_device={rows[0]['device_id'] if rows else ''}"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO csv_uploads (saved_by, filename, row_count, applied, diff_summary) "
        "VALUES (?, ?, ?, 0, ?)",
        (session.get("username","?"), filename, len(rows), diff_summary),
    )
    upload_id = cursor.lastrowid
    source = f"csv:{upload_id}"
    for r in rows:
        cursor.execute("""
            INSERT INTO device_metadata(device_id, location, role, source, uploaded_at, uploaded_by)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                location    = excluded.location,
                role        = excluded.role,
                source      = excluded.source,
                uploaded_at = CURRENT_TIMESTAMP,
                uploaded_by = excluded.uploaded_by
        """, (r["device_id"], r["location"], r["role"], source, session.get("username","?")))
        cursor.execute("""
            INSERT INTO devices(device_id, last_seen, location, role)
            VALUES (?, NULL, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                location = excluded.location,
                role     = excluded.role
        """, (r["device_id"], r["location"], r["role"]))

        # A row carrying sensor_name + max_value also defines a target range.
        if r.get("sensor_name") and r.get("max_value") is not None:
            cursor.execute("""
                INSERT INTO sensor_thresholds(device_id, sensor_name, min_value, max_value, severity)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id, sensor_name) DO UPDATE SET
                    min_value = excluded.min_value,
                    max_value = excluded.max_value,
                    severity  = excluded.severity
            """, (r["device_id"], r["sensor_name"], r["min_value"], r["max_value"], r["severity"]))
    cursor.execute("UPDATE csv_uploads SET applied = 1 WHERE upload_id = ?", (upload_id,))
    conn.commit(); conn.close()
    audit("CSV_UPLOAD",
          details=f"upload_id={upload_id}; rows={len(rows)}; file={filename!r}; note={note!r}")
    return redirect(url_for("admin_config", saved=1))


@app.route("/admin/ignored", methods=["GET", "POST"])
@login_required
def admin_ignored():
    """Manage the ignore list: devices that are installed but not in use.

    An ignored device is hidden from the dashboard AND excluded from
    alerting, so a decommissioned or spare sensor cannot raise alarms.
    """
    if session.get("role") != "admin":
        abort(403)

    if request.method == "POST":
        action = request.form.get("action", "")
        device_id = (request.form.get("device_id") or "").strip()
        who = session.get("username", "?")

        if action == "remove" and device_id:
            _ignored = _ignored_devices()
            _ignored.discard(device_id)
            config.setdefault("watchdog", {})["ignored_devices"] = sorted(_ignored)
            _persist_config()
            audit("IGNORE_REMOVED", device_id=device_id,
                  details="Device returned to monitoring", username=who)
            return redirect(url_for("admin_ignored"))

        if action == "add" and device_id:
            _ignored = _ignored_devices()
            _ignored.add(device_id)
            config.setdefault("watchdog", {})["ignored_devices"] = sorted(_ignored)
            _persist_config()
            audit("IGNORE_ADDED", device_id=device_id,
                  details="Device hidden from dashboard and alerting", username=who)
            return redirect(url_for("admin_ignored"))

        return redirect(url_for("admin_ignored"))

    ignored = _ignored_devices()
    conn = get_db()
    all_devices = [r[0] for r in conn.execute(
        "SELECT DISTINCT device_id FROM devices ORDER BY device_id")]
    known = [d for d in all_devices if d not in ignored]
    conn.close()

    ignored_rows = "".join(
        f'<tr><td><strong>{html_lib.escape(d)}</strong></td>'
        f'<td class="muted">hidden from dashboard, excluded from alerting</td>'
        f'<td><form method="post" action="/admin/ignored" style="display:inline">'
        f'{_csrf_input()}'
        f'<input type="hidden" name="action" value="remove">'
        f'<input type="hidden" name="device_id" value="{html_lib.escape(d)}">'
        f'<button class="btn btn-sm btn-success" type="submit">Resume monitoring</button>'
        f'</form></td></tr>'
        for d in sorted(ignored)
    ) or '<tr><td colspan="3" class="muted">No devices ignored.</td></tr>'

    options = "".join(
        f'<option value="{html_lib.escape(d)}">{html_lib.escape(d)}</option>'
        for d in known
    ) or '<option value="">— no monitored devices —</option>'

    body = f"""<div class="card">
  <h3>Ignore list</h3>
  <p class="muted" style="margin-top:0">
    Devices that are physically installed but currently not in use. An ignored
    device is <strong>hidden from the dashboard</strong> and
    <strong>excluded from alerting entirely</strong>, so it cannot raise
    alarms. Its readings are still recorded in the database.
    <br><span class="muted">Also editable at <code>/admin/config</code> under
    <code>watchdog.ignored_devices</code>.</span>
  </p>
  <form method="post" action="/admin/ignored">
    {_csrf_input()}
    <input type="hidden" name="action" value="add">
    <div class="field">
      <label>Device</label>
      <select name="device_id">{options}</select>
    </div>
    <button class="btn btn-primary" type="submit">Ignore this device</button>
  </form>
</div>
<div class="card">
  <h3>Currently ignored</h3>
  <div style="overflow-x:auto">
    <table class="table table-sm">
      <thead><tr><th>Device</th><th>Effect</th><th></th></tr></thead>
      <tbody>{ignored_rows}</tbody>
    </table>
  </div>
  <a class="btn" href="/admin/config">← Back to admin config</a>
</div>"""
    return _render_layout("Ignore list", body, current_tab="admin")


@app.route("/admin/degraded", methods=["GET", "POST"])
@login_required
def admin_degraded():
    """Manage operator-acknowledged known-degraded sensors.

    A sensor listed here is excluded from SENSOR_SILENT / SUSPECTED_STALL
    alerting and from systemic-fault clustering, so a documented, accepted
    fault does not permanently occupy the dashboard. The sensor still shows
    in the sensor panel, marked as suppressed.
    """
    if session.get("role") != "admin":
        abort(403)

    if request.method == "POST":
        action = request.form.get("action", "")
        device_id = (request.form.get("device_id") or "").strip()
        sensor_name = (request.form.get("sensor_name") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        who = session.get("username", "?")

        if action == "remove":
            conn = get_db()
            conn.execute(
                "DELETE FROM known_degraded WHERE device_id=? AND sensor_name=?",
                (device_id, sensor_name),
            )
            conn.commit(); conn.close()
            audit("KNOWN_DEGRADED_REMOVED", device_id=device_id,
                  details=f"{sensor_name}: {reason}", username=who)
            return redirect(url_for("admin_degraded"))

        # The picker submits "device|sensor" in one field.
        if "|" in sensor_name and not device_id:
            device_id, sensor_name = [p.strip() for p in sensor_name.split("|", 1)]
        if not device_id or not sensor_name:
            return redirect(url_for("admin_degraded"))

        conn = get_db()
        conn.execute("""
            INSERT INTO known_degraded(device_id, sensor_name, reason, suppressed_by, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id, sensor_name) DO UPDATE SET
                reason = excluded.reason,
                suppressed_by = excluded.suppressed_by,
                created_at = CURRENT_TIMESTAMP
        """, (device_id, sensor_name, reason, who))
        conn.commit(); conn.close()
        audit("KNOWN_DEGRADED_ADDED", device_id=device_id,
              details=f"{sensor_name}: {reason}", username=who)
        return redirect(url_for("admin_degraded"))

    conn = get_db()
    rows = conn.execute("""
        SELECT device_id, sensor_name, reason, suppressed_by, created_at
        FROM known_degraded ORDER BY device_id, sensor_name
    """).fetchall()
    conn.close()
    rows_html = "".join(
        f'<tr><td>{html_lib.escape(r["device_id"])}</td>'
        f'<td>{html_lib.escape(r["sensor_name"])}</td>'
        f'<td class="muted">{html_lib.escape(r["reason"] or "")}</td>'
        f'<td>{html_lib.escape(r["suppressed_by"] or "")}</td>'
        f'<td class="ts">{(r["created_at"] or "")[:19]}</td>'
        f'<td><form method="post" action="/admin/degraded" style="display:inline">'
        f'{_csrf_input()}'
        f'<input type="hidden" name="action" value="remove">'
        f'<input type="hidden" name="device_id" value="{html_lib.escape(r["device_id"])}">'
        f'<input type="hidden" name="sensor_name" value="{html_lib.escape(r["sensor_name"])}">'
        f'<button class="btn btn-sm btn-danger" type="submit">Remove</button>'
        f'</form></td></tr>'
        for r in rows
    ) or '<tr><td colspan="6" class="muted">None — every sensor is actively monitored.</td></tr>'

    # Candidate sensors: currently flagged as silent or stalled.
    cand = conn = get_db()
    candidates = cand.execute("""
        SELECT r.device_id, r.sensor_name, r.sensor_type,
               (julianday('now') - julianday(r.last_change_ts)) * 86400.0 AS flat_s
        FROM sensor_readings r
        WHERE r.sensor_type NOT IN ('heartbeat', 'diagnostic')
          AND r.last_change_ts IS NOT NULL
          AND (julianday('now') - julianday(r.last_change_ts)) * 86400.0 > ?
          AND NOT EXISTS (
              SELECT 1 FROM known_degraded k
              WHERE k.device_id=r.device_id AND k.sensor_name=r.sensor_name)
        ORDER BY flat_s DESC LIMIT 40
    """, (int(config.get("watchdog", {}).get("sensor_flatline_seconds", 300)),)).fetchall()
    cand.close()
    cand_html = "".join(
        f'<option value="{html_lib.escape(c["device_id"])}|{html_lib.escape(c["sensor_name"])}">'
        f'{html_lib.escape(c["device_id"])} / {html_lib.escape(c["sensor_name"])} '
        f'({html_lib.escape(c["sensor_type"])}, {int(c["flat_s"]//60)}m)</option>'
        for c in candidates
    ) or '<option value="">— no currently-degraded sensors —</option>'

    body = f"""<div class="card">
  <h3>Known-degraded sensors</h3>
  <p class="muted" style="margin-top:0">
    Suppress alerting for a sensor you have investigated and accepted.
    The sensor keeps reporting and stays visible in the dashboard, marked as
    suppressed, but it no longer raises SENSOR_SILENT / SUSPECTED_STALL and
    is excluded from systemic-fault clustering.
    <strong>Use this for a diagnosed fault, not for a fault you have not
    looked at.</strong>
  </p>
  <form method="post" action="/admin/degraded">
    {_csrf_input()}
    <input type="hidden" name="action" value="add">
    <div class="field">
      <label>Sensor (currently flagged)</label>
      <select name="sensor_name">{cand_html}</select>
    </div>
    <div class="field">
      <label>Reason (recorded in the audit trail)</label>
      <input type="text" name="reason" placeholder="e.g. SCD30 on I2C bus down; replacement scheduled">
    </div>
    <button class="btn btn-primary" type="submit">Suppress alerting</button>
  </form>
</div>
<div class="card">
  <h3>Suppressed sensors</h3>
  <div style="overflow-x:auto">
    <table class="table table-sm">
      <thead><tr><th>Device</th><th>Sensor</th><th>Reason</th><th>By</th><th>Since</th><th></th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>"""
    return _render_layout("Known-degraded sensors", body, current_tab="admin")


@app.route("/admin/sensors/history")
@login_required
def admin_sensors_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT upload_id, saved_at, saved_by, filename, row_count, diff_summary "
        "FROM csv_uploads ORDER BY upload_id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    body = f"""<div class="card">
  <h3>CSV upload history <span class="muted" style="font-weight:400;font-size:.85rem">— last 20</span></h3>
  <div style="overflow-x:auto">
    <table class="table">
      <thead><tr><th>ID</th><th>Saved at</th><th>By</th><th>File</th><th>Rows</th><th>Summary</th></tr></thead>
      <tbody>{''.join(f'<tr><td>{r["upload_id"]}</td><td class="ts">{(r["saved_at"] or "")[:19]}</td><td>{html_lib.escape(r["saved_by"] or "")}</td><td>{html_lib.escape(r["filename"] or "")}</td><td>{r["row_count"]}</td><td class="muted">{html_lib.escape(r["diff_summary"] or "")}</td></tr>' for r in rows) or '<tr><td colspan="6" class="muted">No uploads yet.</td></tr>'}</tbody>
    </table>
  </div>
  <a class="btn" href="/admin/config">← Back to admin</a>
</div>"""
    return _render_layout("CSV upload history", body, current_tab="admin")


# -----------------------------------------------------------------------------
# Admin config editor
# -----------------------------------------------------------------------------
def _read_config_text():
    with open(CONFIG_FILE, "r") as f:
        return f.read()

def _save_config_history(content, saved_by, note):
    conn = get_db()
    conn.execute(
        "INSERT INTO config_history (saved_by, note, content) VALUES (?, ?, ?)",
        (saved_by, note, content),
    )
    cap = int(config.get("auth", {}).get("config_history_max_versions", 50))
    over = conn.execute("SELECT COUNT(*) FROM config_history").fetchone()[0] - cap
    if over > 0:
        ids = [
            r[0] for r in conn.execute(
                "SELECT version_id FROM config_history ORDER BY saved_at ASC LIMIT ?",
                (over,),
            ).fetchall()
        ]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM config_history WHERE version_id IN ({placeholders})", ids
            )
    conn.commit(); conn.close()

def _config_diff_html(old_text, new_text):
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=False),
        new_text.splitlines(keepends=False),
        fromfile="current", tofile="proposed", lineterm="",
    )
    out = []
    for line in diff:
        cls = "add" if line.startswith("+") and not line.startswith("+++") \
              else "del" if line.startswith("-") and not line.startswith("---") \
              else ""
        safe = html_lib.escape(line)
        out.append(f'<span class="{cls}">{safe}</span>')
    return "<br>".join(out) if out else '<span class="muted">(no textual changes)</span>'

@app.route("/admin/config", methods=["GET"])
@login_required
def admin_config():
    if session.get("role") != "admin":
        return _render_layout(
            "Admin",
            '<div class="card"><div class="flash error">Admin role required.</div></div>',
            current_tab="admin",
        )
    content = _read_config_text()
    conn = get_db()
    history = conn.execute(
        "SELECT version_id, saved_at, saved_by, note FROM config_history "
        "ORDER BY version_id DESC LIMIT 25"
    ).fetchall()
    conn.close()
    history_rows = "".join(
        f'<tr><td class="ts">{(r["saved_at"] or "")[:19]}</td>'
        f'<td>{html_lib.escape(r["saved_by"] or "")}</td>'
        f'<td>{html_lib.escape(r["note"] or "")}</td>'
        f'<td><a class="btn btn-sm" href="/admin/config/download/{r["version_id"]}">Download</a> '
        f'<form method="post" action="/admin/config/restore/{r["version_id"]}" style="display:inline" '
        f'onsubmit="return confirm(\'Restore this version as the new live config?\')">'
        f'{_csrf_input()}'
        f'<button class="btn btn-sm btn-warning" type="submit">Restore</button>'
        f'</form></td></tr>'
        for r in history
    ) or '<tr><td colspan="4" class="muted">No prior versions yet — first save seeds one.</td></tr>'

    flash_html = ('<div class="flash success">Configuration saved.</div>' if request.args.get('saved') == '1' else '') + ('<div class="flash success">CSV applied. Reload dashboard to see new location/role labels.</div>' if request.args.get('csv') == 'ok' else '')

    body = f"""{flash_html}
<div class="card">
  <h3>Edit configuration</h3>
  <p class="muted" style="margin-top:0">
    Saving snapshots the current config to history before overwriting it.
    YAML is validated before save; invalid YAML is rejected. Host / port
    changes need a full restart; everything else reloads live.
  </p>
  <form method="post" action="/admin/config">
    {_csrf_input()}
    <textarea name="content">{html_lib.escape(content)}</textarea>
    <div class="field" style="margin-top:1rem">
      <label>Change note (optional — saved to audit trail)</label>
      <input type="text" name="note" placeholder="e.g. bumped heartbeat_timeout to 240s">
    </div>
    <div class="btn-group">
      <button class="btn btn-primary" type="submit" name="action" value="apply">Save &amp; Apply</button>
      <button class="btn" type="submit" name="action" value="preview">Preview Diff</button>
    </div>
  </form>
</div>
<div class="card">
  <h3>Sensor metadata (CSV upload)</h3>
  <p class="muted" style="margin-top:0">
    Upload a CSV to assign a <strong>location</strong> and optional
    <strong>role</strong> to each device, and to define
    <strong>target ranges</strong> per entity.
    Header row required. Only the named columns are read; extras are ignored.
  </p>
  <details style="margin-bottom:1rem">
    <summary class="muted">CSV format &amp; example</summary>
    <pre class="diff" style="margin-top:.5rem">device_id,location,role,sensor_name,min_value,max_value,severity
sht41-1,Spawn Inc A,producer,,,,
sht41-1,,,sht41-1_temperature,18,24,CRITICAL
sht41-1,,,sht41-1_humidity,70,90,WARNING
sht41-1,,,sht41-1_co2,,1200,WARNING
sht41-2,"Fruiting, North",dup,,,
sht41-3,,,sht41-3_temperature,24,30,WARNING</pre>
    <p class="muted" style="margin:.6rem 0 0">
      Leave <span class="ts">sensor_name / min_value / max_value / severity</span>
      empty for a metadata-only row. Fill them in to set a target range for one
      entity: a fresh reading outside the range raises
      <strong>OUT_OF_RANGE</strong> and the row is flagged in the dashboard.
      <span class="ts">severity</span> is <strong>WARNING</strong> or
      <strong>CRITICAL</strong>; CRITICAL sorts first on the wall board.
      A blank <span class="ts">min_value</span> means no lower bound.
      Ranges are only evaluated against fresh readings, so a dead sensor reports
      SENSOR_SILENT rather than a misleading out-of-range.</p>
  </details>
  <p class="muted" style="margin-top:0">
    Need to hide a device that is installed but not in use?
    <a href="/admin/ignored">Manage the ignore list</a>.
  </p>
  <form method="post" action="/admin/sensors/csv">
    {_csrf_input()}
    <div class="field">
      <label>CSV (paste the contents here, or upload a file)</label>
      <input type="file" accept=".csv,text/csv" id="csv-file"
             onchange="if(this.files[0]) this.files[0].text().then(t=>document.getElementById('csv_text').value=t)"
             style="margin-bottom:.5rem">
      <textarea id="csv_text" name="csv_text" placeholder="device_id,location,role&#10;sht41-1,Spawn Incubator A,producer"></textarea>
    </div>
    <input type="hidden" name="filename" value="manual.csv">
    <div class="field">
      <label>Note (optional &mdash; saved to audit trail)</label>
      <input type="text" name="note" placeholder="e.g. mapped 30 sensors to rooms">
    </div>
    <button class="btn btn-primary" type="submit">Apply CSV</button>
    <a class="btn" href="/admin/sensors/history">Upload history</a>
  </form>
</div>
<div class="card">
  <h3>Configuration history</h3>
  <div style="overflow-x:auto">
    <table class="table">
      <thead><tr><th>Saved at</th><th>By</th><th>Note</th><th></th></tr></thead>
      <tbody>{history_rows}</tbody>
    </table>
  </div>
</div>"""
    return _render_layout("Admin Configuration", body, current_tab="admin")

@app.route("/admin/config", methods=["POST"])
@login_required
def admin_config_post():
    if session.get("role") != "admin":
        abort(403)
    action = request.form.get("action", "preview")
    new_content = request.form.get("content", "")
    note = (request.form.get("note") or "").strip()

    try:
        parsed = yaml.safe_load(new_content)
        if not isinstance(parsed, dict):
            raise ValueError("Top-level YAML must be a mapping.")
    except Exception as e:
        return _render_layout(
            "Admin",
            f'<div class="card"><div class="flash error">YAML parse error: {e}</div>'
            f'<a class="btn" href="/admin/config">← Back to editor</a></div>',
            current_tab="admin",
        )

    old_content = _read_config_text()
    if action == "preview":
        diff_html = _config_diff_html(old_content, new_content)
        body = f"""<div class="card">
  <h3>Preview diff</h3>
  <p class="muted">Review the proposed changes, then save or discard.</p>
  <div class="diff">{diff_html}</div>
  <form method="post" action="/admin/config" style="margin-top:1rem">
    {_csrf_input()}
    <input type="hidden" name="content" value="{html_lib.escape(new_content, quote=True)}">
    <input type="hidden" name="note" value="{html_lib.escape(note, quote=True)}">
    <div class="btn-group">
      <button class="btn btn-primary" type="submit" name="action" value="apply">Confirm &amp; Save</button>
      <a class="btn" href="/admin/config">Cancel</a>
    </div>
  </form>
</div>"""
        return _render_layout("Preview config diff", body, current_tab="admin")

    _save_config_history(old_content, session.get("username", "?"), note or "(no note)")
    with open(CONFIG_FILE, "w") as f:
        f.write(new_content)
    audit("CONFIG_SAVE", details=f"note={note!r}; bytes={len(old_content)}->{len(new_content)}")

    new_cfg = yaml.safe_load(new_content)
    for key in ("telegram", "watchdog", "facility", "snooze_presets", "auth"):
        if key in new_cfg:
            config[key] = new_cfg[key]
    # ignored_devices lives under watchdog:, which is hot-reloaded above, so
    # the ignore list takes effect on the next watchdog tick without a
    # restart. Normalise it to a sorted list of strings either way.
    _ign = config.get("watchdog", {}).get("ignored_devices", []) or []
    if isinstance(_ign, str):
        _ign = [_ign]
    config.setdefault("watchdog", {})["ignored_devices"] = sorted(
        {str(d).strip() for d in _ign if str(d).strip()})
    if "web" in new_cfg and "dashboard_refresh_seconds" in new_cfg["web"]:
        config["web"]["dashboard_refresh_seconds"] = new_cfg["web"]["dashboard_refresh_seconds"]

    # Rebuild the Telegram engine so token/chat_id/enabled changes take effect
    # live. The module-level engine was constructed once at import, so without
    # this a UI edit silently did nothing until restart (review finding B12).
    # The polling thread picks up the new global on its next iteration.
    global telegram_engine
    telegram_engine = TelegramEngine(
        token=config["telegram"]["bot_token"],
        chat_id=config["telegram"]["chat_id"],
        enabled=config["telegram"]["enabled"],
    )

    body = """<div class="card">
  <div class="flash success">Configuration saved. Telegram and watchdog settings
  are now live; MQTT broker/credentials and web host/port still require a restart
  (<code>./ops.sh restart</code>).</div>
  <a class="btn btn-primary" href="/admin/config">← Back to editor</a>
</div>"""
    return _render_layout("Config saved", body, current_tab="admin")

@app.route("/admin/config/download/<int:version_id>")
@login_required
def admin_config_download(version_id):
    if session.get("role") != "admin":
        abort(403)
    conn = get_db()
    row = conn.execute(
        "SELECT content, saved_at, saved_by FROM config_history WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    conn.close()
    if row is None:
        abort(404)
    return Response(
        row["content"],
        mimetype="application/x-yaml",
        headers={
            "Content-Disposition": f'attachment; filename="config.v{version_id:04d}.{row["saved_at"][:10]}.yaml"',
            "X-Config-Version": str(version_id),
            "X-Saved-By": row["saved_by"] or "",
            "X-Saved-At": row["saved_at"] or "",
        },
    )

@app.route("/admin/config/restore/<int:version_id>", methods=["POST"])
@login_required
def admin_config_restore(version_id):
    if session.get("role") != "admin":
        abort(403)
    conn = get_db()
    row = conn.execute(
        "SELECT content FROM config_history WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    conn.close()
    if row is None:
        abort(404)
    current = _read_config_text()
    _save_config_history(
        current, session.get("username", "?"),
        f"auto-snapshot before restoring v{version_id}",
    )
    with open(CONFIG_FILE, "w") as f:
        f.write(row["content"])
    audit("CONFIG_ROLLBACK", details=f"Restored version_id={version_id}")
    return redirect(url_for("admin_config", saved=1))

# -----------------------------------------------------------------------------
# Print report
# -----------------------------------------------------------------------------
PRINT_CSS = """
@page { size: A4; margin: 18mm 14mm; }
body { font: 11pt/1.45 -apple-system, "Segoe UI", system-ui, sans-serif; color: #111; margin: 0; }
.report-header { border-bottom: 2px solid #111; padding-bottom: 1rem; margin-bottom: 1.5rem; }
.report-header h1 { margin: 0 0 .25rem; font-size: 1.4rem; }
.report-meta { font-size: .85rem; color: #555; }
.report-meta dt { font-weight: 600; display: inline-block; min-width: 130px; }
.report-meta dd { display: inline; margin: 0 1rem 0 0; }
table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: .85rem; }
th, td { border: 1px solid #ccc; padding: .4rem .55rem; text-align: left; }
th { background: #f3f4f6; }
.status-ONLINE { color: #16a34a; font-weight: 600; }
.status-STALLED { color: #d97706; font-weight: 600; }
.status-OFFLINE { color: #dc2626; font-weight: 600; }
.signature-block { margin-top: 2.5rem; }
.signature-line {
  display: inline-block; width: 45%; margin-right: 4%;
  border-top: 1px solid #333; padding-top: .5rem; font-size: .85rem; color: #555;
}
@media print { .no-print { display: none; } }
"""

@app.route("/print")
@login_required
def print_report():
    facility = config.get("facility", {})
    conn = get_db()
    devices = [dict(r) for r in conn.execute(
        "SELECT * FROM devices ORDER BY device_id ASC"
    ).fetchall()]
    alerts = conn.execute(
        "SELECT device_id, alert_type, message, triggered_at FROM alerts "
        "WHERE resolved = 0 ORDER BY triggered_at DESC LIMIT 50"
    ).fetchall()
    conn.close()

    def fmt(v): return "—" if not v else v[:19]
    conn2 = get_db()
    sensor_rows = conn2.execute("""
        SELECT device_id, sensor_name, sensor_type, value, ts
        FROM sensor_readings
    """).fetchall()
    conn2.close()
    sensors_by_device = {}
    for r in sensor_rows:
        sensors_by_device.setdefault(r["device_id"], []).append({
            "sensor_name": r["sensor_name"],
            "sensor_type": r["sensor_type"],
            "value": r["value"],
            "ts": r["ts"],
        })

    def sensor_cell_html(d):
        items = sensors_by_device.get(d["device_id"], [])
        if not items:
            return '<span style="color:#888">—</span>'
        lines = []
        for s in sorted(items, key=lambda x: (x["sensor_name"], x["sensor_type"])):
            vstr = f"{s['value']:.2f}" if isinstance(s["value"], (int, float)) else "—"
            lines.append(
                f'<div>{html_lib.escape(s["sensor_name"])} / {html_lib.escape(s["sensor_type"])}: '
                f'<strong>{vstr}</strong></div>'
            )
        return "".join(lines)

    device_rows = "".join(
        f'<tr>'
        f'<td><strong>{html_lib.escape(d["device_id"])}</strong><br>'
        f'<span style="color:#555;font-size:.85em">{html_lib.escape(d.get("location") or "")}'
        f'{(" &middot; " + html_lib.escape(d["role"])) if d.get("role") else ""}</span></td>'
        f'<td class="status-{d["status"]}">{d["status"]}</td>'
        f'<td>{fmt(d["last_seen"])}</td>'
        f'<td>{d["last_heartbeat_value"] if d["last_heartbeat_value"] is not None else "—"}</td>'
        f'<td>{d["last_temp"] if d["last_temp"] is not None else "—"}</td>'
        f'<td>{d["last_hum"]  if d["last_hum"]  is not None else "—"}</td>'
        f'<td>{d["last_co2"]  if d["last_co2"]  is not None else "—"}</td>'
        f'<td>{sensor_cell_html(d)}</td>'
        f'<td>{fmt(d["snooze_until"]) if d["snooze_until"] else "—"}</td>'
        f'</tr>'
        for d in devices
    ) or '<tr><td colspan="9" style="text-align:center;color:#888">No devices</td></tr>'
    alert_rows = "".join(
        f'<tr><td>{fmt(a["triggered_at"])}</td><td>{html_lib.escape(a["device_id"] or "")}</td>'
        f'<td>{html_lib.escape(a["alert_type"] or "")}</td>'
        f'<td>{html_lib.escape(a["message"] or "")}</td></tr>'
        for a in alerts
    ) or '<tr><td colspan="4" style="text-align:center;color:#888">No active alerts</td></tr>'

    with mqtt_health_lock:
        mqtt_status = "Connected" if mqtt_health["connected"] else "DISCONNECTED"
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>{html_lib.escape(facility.get('report_title','Status Report'))}</title>
<style>{PRINT_CSS}</style>
</head>
<body>
<div class="report-header">
  <h1>{html_lib.escape(facility.get('name','Facility'))} — {html_lib.escape(facility.get('report_title','Status Report'))}</h1>
  <dl class="report-meta">
    <dt>Location:</dt><dd>{html_lib.escape(facility.get('location',''))}</dd>
    <dt>Generated by:</dt><dd>{html_lib.escape(session.get('username','?'))} ({html_lib.escape(session.get('role','operator'))})</dd>
    <dt>Generated at:</dt><dd>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</dd>
    <dt>MQTT broker:</dt><dd>{config['mqtt']['broker']}:{config['mqtt']['port']} ({mqtt_status})</dd>
    <dt>Devices monitored:</dt><dd>{len(devices)}</dd>
    <dt>Active alerts:</dt><dd>{len(alerts)}</dd>
  </dl>
</div>
<h2 style="font-size:1.05rem;margin:1.5rem 0 .5rem">Device Status</h2>
<table>
  <thead><tr><th>Device / Location</th><th>Status</th><th>Last Seen</th><th>Heartbeat</th>
    <th>Temp °C</th><th>Humidity %</th><th>CO2 ppm</th><th>Sensors</th><th>Snoozed Until</th></tr></thead>
  <tbody>{device_rows}</tbody>
</table>
<h2 style="font-size:1.05rem;margin:1.5rem 0 .5rem">Active Alerts</h2>
<table>
  <thead><tr><th>Triggered</th><th>Device</th><th>Type</th><th>Message</th></tr></thead>
  <tbody>{alert_rows}</tbody>
</table>
<div class="signature-block">
  <div class="signature-line">Operator signature &amp; date</div>
  <div class="signature-line">Reviewer signature &amp; date</div>
</div>
<div class="no-print" style="margin-top:2rem;text-align:center">
  <button class="btn btn-primary" onclick="window.print()">🖨 Print this report</button>
  <a class="btn" href="/">← Back to dashboard</a>
</div>
</body></html>"""

# -----------------------------------------------------------------------------
# Wall-display alarm board
# -----------------------------------------------------------------------------
WALL_CSS = """
* { box-sizing: border-box; margin: 0; }
body {
    background: #0b0f14; color: #e6edf3; height: 100vh; overflow: hidden;
    font-family: -apple-system, 'Segoe UI', system-ui, sans-serif;
    display: flex; flex-direction: column;
}
.bar {
    display: flex; align-items: center; gap: 1.5rem;
    padding: .9rem 2rem; background: #111820; border-bottom: 2px solid #1f2a37;
    flex-shrink: 0;
}
.bar h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: .02em; }
.mqtt { margin-left: auto; font-size: 1rem; font-weight: 700; padding: .3rem .9rem;
        border-radius: 999px; }
/* Beep test control. The operator must be able to find it on a TV across
   a room, so it is deliberately large and high-contrast - it is the one
   control that proves the wall display can raise an audible alarm. */
.beep-controls { display: flex; align-items: center; gap: .6rem; }
/* The browser blocks audio until a real user gesture. Until then the alarm
   would be silent, which is the one failure mode a wall display must not
   have. This banner says so explicitly instead of leaving the operator
   wondering why the buzzer never sounds. */
.audio-warn {
    background: #4a3a14; color: #fbbf24; padding: .3rem .8rem;
    border-radius: 999px; font-size: .85rem; font-weight: 600;
    animation: pulse 1.6s infinite;
}
.beep-btn {
    background: #1f2937; color: #fbbf24; border: 2px solid #fbbf24;
    border-radius: 8px; padding: .45rem 1.1rem; font-size: 1.05rem;
    font-weight: 800; letter-spacing: .04em;
    cursor: pointer; font-family: inherit;
}
.beep-btn:hover { background: #fbbf24; color: #1f2937; }
.beep-status { font-size: .8rem; color: #7d8590; max-width: 34ch; }
.beep-status.ok  { color: #4ade80; }
.beep-status.bad { color: #f87171; }
/* Brief full-screen flash used by the beep test, so the VISUAL alarm can be
   verified on a screen with no speakers. */
@keyframes beepflash {
    0%,100% { background: #0b0f14; }
    50%     { background: #7f1d1d; }
}
.stage.beep-test-flash { animation: beepflash .5s 3; }
.mqtt.ok   { background: #10371f; color: #4ade80; }
.mqtt.down { background: #4a1414; color: #f87171; animation: pulse 1s infinite; }
#clock { font-size: 1.5rem; font-weight: 300; color: #7d8590;
         font-variant-numeric: tabular-nums; }
.stage { flex: 1; padding: 2rem; overflow-y: auto; }
.clear-mark { font-size: 12rem; color: #16a34a; text-align: center; line-height: 1;
              margin-top: 4vh; }
.clear-text { font-size: 3.5rem; font-weight: 800; text-align: center;
              color: #4ade80; letter-spacing: .04em; margin-top: 1rem; }
.clear-sub { font-size: 1.3rem; text-align: center; color: #7d8590; margin-top: 1rem; }
.alarm-head { display: flex; align-items: baseline; gap: 1.5rem;
              margin-bottom: 1.5rem; flex-wrap: wrap; }
.alarm-title { font-size: 3.2rem; font-weight: 800; }
.crit-n { color: #f87171; }
.tot-n  { color: #fbbf24; }
.alarm-sub { font-size: 1.3rem; color: #7d8590; }
table.faults { width: 100%; border-collapse: collapse; font-size: 1.15rem; }
table.faults th {
    text-align: left; padding: .6rem .8rem; color: #7d8590;
    font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
    border-bottom: 2px solid #1f2a37;
}
table.faults td { padding: .8rem; border-bottom: 1px solid #1a222c; }
.sev { display: inline-block; padding: .2rem .6rem; border-radius: 6px;
       font-size: .75rem; font-weight: 700; }
.sev-crit { background: #4a1414; color: #f87171; }
.sev-warn { background: #4a3a14; color: #fbbf24; }
.dev  { font-weight: 700; font-family: ui-monospace, monospace; }
.typ  { color: #93a1b0; }
.msg  { color: #e6edf3; }
.since { color: #7d8590; font-family: ui-monospace, monospace; white-space: nowrap; }
.more { margin-top: 1rem; color: #7d8590; font-size: 1.1rem; }
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .45 } }
/* Auto-dismiss stops the flashing but keeps the list readable. */
.stage.alarming.auto-dismissed { background: #0b0f14; }
.stage.alarming:not(.auto-dismissed) { animation: flashbg 1.2s infinite; }
@keyframes flashbg {
    0%,100% { background: #2a0f0f; }
    50%     { background: #0b0f14; }
}
"""

@app.route("/wallboard")
@login_required
def wallboard():
    body = f"""<div class="bar">
  <h1>&#127812; Sensor Watchdog &mdash; Facility Board</h1>
  <span id="audio-warn" class="audio-warn" style="display:none">
    &#128266; Click anywhere to enable the alarm sound
  </span>
  <span id="mqtt-state" class="mqtt down">BROKER ...</span>
  <span id="clock"></span>
  <span class="beep-controls">
    <button class="beep-btn" onclick="beepTest()" title="Play the alarm tone and flash the screen">&#128266; TEST ALARM</button>
    <span id="beep-status" class="beep-status"></span>
  </span>
</div>
<div id="stage" class="stage"></div>
<script>window.REFRESH_SECONDS = 5; window.CSRF_TOKEN = "{_csrf_token()}";</script>
<script>{WALLBOARD_JS}</script>"""
    html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sensor Watchdog - Facility Board</title>
<style>{WALL_CSS}</style></head>
<body>{body}</body></html>"""
    return html_doc


@app.route("/api/wallboard")
@login_required
def api_wallboard():
    """Compact fault feed for the wall display."""
    conn = get_db()
    rows = conn.execute("""
        SELECT device_id, alert_type, message, triggered_at
        FROM alerts
        WHERE resolved = 0
        ORDER BY
            CASE alert_type
                WHEN 'SYSTEMIC'    THEN 0
                WHEN 'OUT_OF_RANGE' THEN 1
                WHEN 'OFFLINE'     THEN 2
                WHEN 'STALLED'     THEN 3
                WHEN 'SENSOR_SILENT' THEN 4
                ELSE 5
            END,
            triggered_at ASC
    """).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    last = conn.execute("SELECT MAX(ts) FROM sensor_readings").fetchone()[0]
    # A true systemic flag: an actual same-metric/same-window cluster, NOT the
    # count of out-of-range breaches the old code mislabelled as systemic
    # (review finding B13).
    flatline_limit = int(config.get("watchdog", {}).get("sensor_flatline_seconds", 300))
    systemic = _detect_systemic_faults(conn, datetime.now(), flatline_limit)
    conn.close()

    faults = [{
        "device_id": r["device_id"],
        "alert_type": r["alert_type"],
        "message": r["message"],
        "triggered_at": r["triggered_at"],
        "severity": "CRITICAL" if r["alert_type"] in (
            "SYSTEMIC", "OUT_OF_RANGE", "OFFLINE", "STALLED") else "WARNING",
    } for r in rows]

    with mqtt_health_lock:
        mqtt_ok = mqtt_health["connected"]

    return jsonify({
        "faults": faults,
        "mqtt_ok": mqtt_ok,
        "total": total,
        "lastPacket": (last[:19] if last else None),
        "systemic": len(systemic) > 0,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# -----------------------------------------------------------------------------
# Misc
# -----------------------------------------------------------------------------
@app.route("/health")
def health():
    """Liveness, not merely session state.

    A broker that is reachable but no longer forwarding leaves the MQTT
    session 'connected' while nothing arrives. Reporting that as healthy is
    exactly the false-confidence failure this watchdog exists to prevent, so
    we additionally check that packets have actually been received recently.
    """
    with mqtt_health_lock:
        connected = mqtt_health["connected"]
        last_disc = mqtt_health["last_disconnected_at"]

    payload = {
        "ok": False,
        "mqtt_connected": connected,
        "last_packet": None,
        "last_packet_age_seconds": None,
    }

    try:
        conn = get_db()
        row = conn.execute("SELECT MAX(ts) FROM sensor_readings").fetchone()
        conn.close()
        if row and row[0]:
            last = _parse_ts(row[0])
            if last:
                age = (datetime.now() - last).total_seconds()
                payload["last_packet"] = row[0]
                payload["last_packet_age_seconds"] = int(age)
                # 10 minutes with no packet from a live broker is an outage,
                # regardless of what the MQTT session believes.
                payload["ok"] = connected and age < 600
    except Exception:
        log.exception("health check failed")

    status = 200 if payload["ok"] else 503
    return jsonify(payload), status

@app.errorhandler(403)
def _403(_):
    return _render_layout("Forbidden", '<div class="card"><div class="flash error">403 — forbidden.</div></div>'), 403

@app.errorhandler(404)
def _404(_):
    return _render_layout("Not found", '<div class="card"><div class="flash error">404 — page not found.</div></div>'), 404

# -----------------------------------------------------------------------------
# Background jobs
# -----------------------------------------------------------------------------
def _watchdog_check_health():
    """Two independent failure modes worth telling the operator about:
      1. MQTT session lost (broker unreachable / refused).
      2. Session alive but no packets arriving (broker up, forwarding
         stopped). (2) is the one that hides longest, because the client
         still believes it is connected.
    """
    try:
        now = datetime.now()
        with mqtt_health_lock:
            connected = mqtt_health["connected"]
            last_disc = mqtt_health["last_disconnected_at"]
            down_alerted = mqtt_health["down_alerted"]
            last_down_nag = mqtt_health["last_down_nag_at"]
            nodata_alerted_at = mqtt_health["nodata_alerted_at"]

        # (2) connected but silent. Page once when silence first crosses 10
        # minutes, then at most every 30 minutes — tracked by timestamp, not by
        # a tick coinciding with a narrow window (review finding B11).
        if connected:
            try:
                conn = get_db()
                row = conn.execute("SELECT MAX(ts) FROM sensor_readings").fetchone()
                conn.close()
                age = None
                if row and row[0]:
                    last = _parse_ts(row[0])
                    age = (now - last).total_seconds() if last else None
                if age is not None and age > 600:
                    if (nodata_alerted_at is None
                            or (now - nodata_alerted_at).total_seconds() >= 1800):
                        telegram_engine.send_message(
                            "⚠️ <b>No sensor data for %d minutes.</b>\n"
                            "MQTT session is still connected, so the broker "
                            "is reachable but not forwarding. This is a "
                            "broker, ACL or network fault &mdash; not a "
                            "sensor fault." % int(age // 60)
                        )
                        with mqtt_health_lock:
                            mqtt_health["nodata_alerted_at"] = now
                else:
                    # Data flowing again — clear the latch.
                    if nodata_alerted_at is not None:
                        with mqtt_health_lock:
                            mqtt_health["nodata_alerted_at"] = None
            except Exception:
                log.exception("no-data check failed")
            return

        # (1) session lost. Page once when downtime first reaches 60s (a latch,
        # so a 15s health tick can no longer step over a 5s trigger window —
        # review finding B5), then re-nag every 10 minutes until reconnect.
        if last_disc is None:
            return
        downtime = (now - last_disc).total_seconds()
        if downtime >= 60 and not down_alerted:
            telegram_engine.send_message(
                "⚠️ <b>MQTT broker unreachable.</b>\n"
                f"Down for {int(downtime)}s. Watchdog is blind until reconnect."
            )
            with mqtt_health_lock:
                mqtt_health["down_alerted"] = True
                mqtt_health["last_down_nag_at"] = now
        elif down_alerted and (last_down_nag is None
                               or (now - last_down_nag).total_seconds() >= 600):
            telegram_engine.send_message(
                "⚠️ <b>MQTT broker still unreachable.</b>\n"
                f"Down for {int(downtime)}s. Watchdog is blind until reconnect."
            )
            with mqtt_health_lock:
                mqtt_health["last_down_nag_at"] = now
    except Exception:
        log.exception("_watchdog_check_health crashed")

def _rebase_stale_readings_on_boot():
    """On first boot to schema v5 (or after a long downtime), rebase
    last_change_ts so existing rows do not immediately look "stalled".

    Without this, every sensor_readings row whose last_change_ts is older
    than sensor_flatline_seconds will fire a SUSPECTED_STALL on the first
    watchdog tick -- a noisy false-start.

    We only rebase to "now", then mark the corresponding alert as
    already-handled by clearing any open SUSPECTED_STALL alerts whose
    triggered_at falls in the rebase window.
    """
    flatline_limit = int(config["watchdog"].get("sensor_flatline_seconds", 300))
    conn = get_db()
    cursor = conn.cursor()
    rebase_count = cursor.execute("""
        UPDATE sensor_readings
        SET last_change_ts = CURRENT_TIMESTAMP
        WHERE last_change_ts IS NULL
           OR (julianday('now') - julianday(last_change_ts)) * 86400.0 > ?
    """, (flatline_limit * 4,)).rowcount  # 4x the limit: only rebase genuinely stale.
    if rebase_count:
        # Mark the device as ONLINE on cold start so we don't show DEGRADED for
        # 5 minutes after every restart.
        cursor.execute("""
            UPDATE devices SET status = 'ONLINE'
            WHERE status IS NOT 'OFFLINE'
        """)
        cursor.execute(
            "UPDATE alerts SET resolved = 1 WHERE alert_type IN ('SUSPECTED_STALL','SENSOR_SILENT') AND resolved = 0"
        )
        log.info("Cold-start rebase: set last_change_ts = now() for %d stale rows; "
                 "resolved pre-existing SUSPECTED_STALL alerts.", rebase_count)
    conn.commit()
    conn.close()


def main():
    init_db()
    _ensure_password_hashes()
    _ensure_secret_key()
    # Apply the (possibly freshly generated) signing key before serving.
    app.secret_key = config["web"]["secret_key"]
    _rebase_stale_readings_on_boot()
    client = start_mqtt()
    _mqtt_client_holder["client"] = client
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        watchdog_eval_loop, "interval",
        seconds=config["watchdog"]["check_interval_seconds"],
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _watchdog_check_health, "interval",
        seconds=15, max_instances=1, coalesce=True,
    )
    scheduler.start()
    threading.Thread(target=telegram_polling_thread, daemon=True).start()
    log.info(f"Starting Flask on {config['web']['host']}:{config['web']['port']}")
    app.run(
        host=config["web"]["host"], port=config["web"]["port"],
        debug=False, use_reloader=False,
    )

if __name__ == "__main__":
    main()
