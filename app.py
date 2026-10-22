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
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _column_exists(conn, table, column):
    return any(r[1] == column
               for r in conn.execute(f"PRAGMA table_info({table})").fetchall())

def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

def _apply_migrations(conn):
    # v4 -> v5: add sensor_readings.last_change_ts. Must run BEFORE any
    # CREATE INDEX that references it (schema.sql and the embedded script
    # below both do).
    if _table_exists(conn, "sensor_readings") and not _column_exists(conn, "sensor_readings", "last_change_ts"):
        conn.execute("ALTER TABLE sensor_readings ADD COLUMN last_change_ts TIMESTAMP")
        conn.execute("UPDATE sensor_readings SET last_change_ts = ts WHERE last_change_ts IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_flatline ON sensor_readings(last_change_ts)")
        log.info("Migration: added sensor_readings.last_change_ts (v4 -> v5)")
    if not _column_exists(conn, "audit_logs", "username"):
        conn.execute("ALTER TABLE audit_logs ADD COLUMN username TEXT")
        log.info("Migration: added audit_logs.username")
    if not _column_exists(conn, "devices", "location"):
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
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version','5')"
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

def audit(action, device_id=None, details="", username=None):
    if username is None:
        username = session.get("username", "anonymous")
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_logs (device_id, username, action, details) "
        "VALUES (?, ?, ?, ?)",
        (device_id, username, action, details),
    )
    conn.commit()
    conn.close()

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

def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        with mqtt_health_lock:
            was_disconnected = not mqtt_health["connected"]
            mqtt_health["connected"] = True
            mqtt_health["last_connected_at"] = datetime.now()
            if was_disconnected and mqtt_health["last_disconnected_at"]:
                mqtt_health["reconnect_count"] += 1
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

    else:
        # Unknown substructure under env_sensor; skip silently to avoid log spam.
        return
    now = datetime.now()
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
                    conn.close()
                    return
            except (ValueError, TypeError):
                conn.close()
                return
            sensor_type = None
            # Match metric keywords.
            # - 'heartbeat' is checked specially because the topic name
            #   often puts it AFTER the sensor label (e.g.
            #   'sht41-4_heartbeat_counter'). We match a heartbeat
            #   metric only if a number in the name (e.g. 'counter') is
            #   present, to avoid matching things like 'light' that
            #   contain 'heat' as a substring.
            # - 'co2' must be checked before 'co' to avoid false positives
            #   on 'co' words.
            lname = sensor_name.lower()
            sensor_type = None
            if any(k in lname for k in ("temperature", "humidity", "light", "pressure")):
                for key in ("temperature", "humidity", "co2", "light", "pressure"):
                    if key in lname:
                        sensor_type = key
                        break
            elif "heartbeat" in lname and ("counter" in lname or "ping" in lname or "tick" in lname):
                # Strong heartbeat signal: name contains 'heartbeat' AND
                # one of the words ESPHome uses for that purpose (counter, ping, tick).
                # Falls through and rejects names like 'preheat_sensor' that merely
                # contain 'heat'.
                sensor_type = "heartbeat"
            elif "heartbeat" in lname:
                # Lenient fallback: matches 'sht41-4_heartbeat_counter' style names
                # even when the trailing word isn't one of the explicit ones above.
                sensor_type = "heartbeat"
            if sensor_type is None:
                # No recognised metric in the name. Likely a non-numeric sensor
                # whose float() somehow succeeded, or a numeric sensor whose
                # name doesn't encode a type. Drop silently.
                conn.close()
                return
            # Per-sensor row. UPSERT: update value+ts on every packet, but only
            # update last_change_ts when the value differs by more than
            # watchdog.sensor_flatline_min_change_pct (default 0.1%). This lets
            # us detect a value-flatline (sensor stuck) without needing a
            # historical readings table.
            # Heartbeat counters are exempt from the percentage tolerance: a
            # counter sitting at 16000 that ticks to 16001 is a 0.006% delta,
            # which the tolerance would wrongly classify as "unchanged".
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
                tol = float(config.get("watchdog", {}).get("sensor_flatline_min_change_pct", 0.1)) / 100.0
                cursor.execute(f"""
                    INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, value, ts, last_change_ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, sensor_name, sensor_type) DO UPDATE SET
                        value = excluded.value,
                        ts    = excluded.ts,
                        last_change_ts = CASE
                            WHEN ((excluded.value - value) BETWEEN -?*ABS(value) AND +?*ABS(value))
                              OR (excluded.value = value)
                            THEN last_change_ts
                            ELSE excluded.ts
                        END
                """, (device_id, sensor_name, sensor_type, val, now, now, tol, tol))
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
    except Exception as e:
        log.exception(f"on_mqtt_message failed for topic={topic}: {e}")

def start_mqtt():
    client = mqtt.Client(client_id="facility_watchdog_server")
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
    """
    rows = conn.execute("""
        SELECT device_id, sensor_type, ts
        FROM sensor_readings
        WHERE sensor_type != 'heartbeat'
          AND ts IS NOT NULL
    """).fetchall()

    # Group silent sensors by metric.
    by_metric = {}
    for r in rows:
        last = _parse_ts(r["ts"])
        if last is None:
            continue
        silent = (now - last).total_seconds()
        if silent > flatline_limit:
            by_metric.setdefault(r["sensor_type"], []).append((r["device_id"], silent))

    clusters = []
    for stype, items in by_metric.items():
        # A device can publish several channels that all classify as the same
        # metric (e.g. scd30_1 carries both 'scd30-1_co2' and 'scd30_co2').
        # Count devices, not channels, or one device inflates the cluster.
        per_device = {}
        for dev, silent in items:
            per_device[dev] = max(per_device.get(dev, 0), silent)
        items = [(d, s) for d, s in per_device.items()]
        if len(items) < 3:
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
        nag_interval    = config["telegram"]["nagging_interval_seconds"]

        for dev in cursor.execute("SELECT * FROM devices").fetchall():
            device_id = dev["device_id"]
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
                  AND sensor_type != 'heartbeat'
                  AND last_change_ts IS NOT NULL
                  AND (julianday(?) - julianday(last_change_ts)) * 86400.0 > ?
            """, (now.isoformat(sep=" "),
                  now.isoformat(sep=" "),
                  device_id,
                  now.isoformat(sep=" "),
                  flatline_limit)).fetchall()

            if not fault_type and sensor_fault:
                # SENSOR_SILENT takes precedence over SUSPECTED_STALL per sensor.
                silent = [r for r in sensor_fault if (r["silent_s"] or 0) > flatline_limit]
                flat   = [r for r in sensor_fault if (r["silent_s"] or 0) <= flatline_limit]
                if silent:
                    worst = max(silent, key=lambda r: r["silent_s"])
                    fault_type = "SENSOR_SILENT"
                    fault_msg = (
                        f"Sensor {worst['sensor_name']} ({worst['sensor_type']}) "
                        f"stopped publishing {int(worst['silent_s'])}s ago."
                    )
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

            # Resolve any open alerts of the same type if recovery happened
            if not fault_type:
                cursor.execute(
                    "UPDATE alerts SET resolved = 1 WHERE device_id = ? AND resolved = 0",
                    (device_id,),
                )
            elif new_status != prev_status:
                # When status transitions to a different fault, leave alerts of the
                # *previous* type alone — operators want to see the history.
                pass

            # Raise / re-nag alert
            if fault_type:
                _raise_or_nag(cursor, device_id, fault_type, fault_msg, nag_interval, now)

        # Systemic faults: many devices losing the same metric in the same
        # window points at the broker, network or an ACL change rather than at
        # N independent sensors. Suppress the per-device SENSOR_SILENT noise
        # for devices caught in a cluster; the cluster is reported instead.
        systemic = _detect_systemic_faults(conn, now, flatline_limit)
        if systemic:
            summary = "; ".join(
                "%s on %d devices" % (c["sensor_type"], c["count"]) for c in systemic
            )
            log.warning("Systemic fault detected: %s", summary)
            audit("SYSTEMIC_FAULT", details=summary, username="watchdog")
            clustered = {d for c in systemic for d in c["devices"]}
            # Resolve the individual per-device alerts for this round; the
            # cluster alert below carries the actionable information.
            for c in systemic:
                for dev in c["devices"]:
                    cursor.execute(
                        "UPDATE alerts SET resolved = 1 "
                        "WHERE device_id = ? AND alert_type = 'SENSOR_SILENT' "
                        "AND resolved = 0 AND message LIKE ?",
                        (dev, f"%{c['sensor_type']}%"),
                    )

        conn.commit()
        conn.close()
    except Exception:
        log.exception("watchdog_eval_loop crashed")

# -----------------------------------------------------------------------------
# Telegram polling
# -----------------------------------------------------------------------------
def telegram_polling_thread():
    if not telegram_engine.enabled:
        return
    offset = 0
    timeout = config["telegram"].get("poll_timeout_seconds", 25)
    while True:
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
                conn = get_db()
                cursor = conn.cursor()

                if parts[0] == "ack" and len(parts) == 2:
                    dev_id = parts[1]
                    cursor.execute("UPDATE alerts SET acknowledged = 1 WHERE device_id = ? AND resolved = 0", (dev_id,))
                    cursor.execute(
                        "INSERT INTO audit_logs (device_id, username, action, details) "
                        "VALUES (?, ?, 'ACK', 'Acknowledged via Telegram')",
                        (dev_id, chat),
                    )
                    conn.commit(); conn.close()
                    telegram_engine.answer_callback(cb_id, f"Alert for {dev_id} acknowledged.")
                elif parts[0] == "snooze" and len(parts) == 3:
                    try:
                        hours = int(parts[1]); dev_id = parts[2]
                    except ValueError:
                        conn.close()
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
                    conn.commit(); conn.close()
                    telegram_engine.answer_callback(cb_id, f"{dev_id} snoozed for {hours} hours.")
                else:
                    conn.close()
                    telegram_engine.answer_callback(cb_id, "Unknown action.")
        except Exception:
            log.exception("telegram polling loop crashed; sleeping 5s")
            time.sleep(5)

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = config["web"]["secret_key"]
app.permanent_session_lifetime = timedelta(hours=12)

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
    overflow-wrap: anywhere;   /* long device_ids / topics wrap, not overflow */
    word-break: break-word;
}
.table.devices th {
    color: var(--muted); font-weight: 600; font-size: .7rem;
    text-transform: uppercase; letter-spacing: .03em;
}
.table.devices tbody tr:hover { background: var(--bg); }
/* Column sizing. Sums to ~100%. */
.col-device    { width: 13%; }
.col-location  { width: 11%; }
.col-status    { width: 7%;  }
.col-reason    { width: 22%; }
.col-lastseen  { width: 11%; }
.col-heartbeat { width: 6%;  }
.col-temp      { width: 6%;  }
.col-hum       { width: 6%;  }
.col-co2       { width: 6%;  }
.col-sensors   { width: 7%;  }
.col-snooze    { width: 5%;  }
.col-actions   { width: 6%;  }
/* Values are tabular so digits line up column to column. */
.table.devices td.num { font-variant-numeric: tabular-nums; }
/* Narrow viewports: shed the columns an operator can recover from the
   sensor panel, so the remaining ones stay legible instead of all shrinking. */
@media (max-width: 1200px) {
    .table.devices { font-size: .78rem; }
    .table.devices th, .table.devices td { padding: .4rem .35rem; }
    .col-lastseen, .col-heartbeat { display: none; }
    .col-device   { width: 17%; }
    .col-location { width: 14%; }
    .col-status   { width: 9%;  }
    .col-reason   { width: 30%; }
    .col-temp     { width: 8%;  }
    .col-hum      { width: 8%;  }
    .col-co2      { width: 8%;  }
    .col-sensors  { width: 9%;  }
    .col-snooze   { width: 0;   }
    .col-actions  { width: 0;   }
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
    max-width: 320px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; display: inline-block; vertical-align: middle;
}
.reason-panel {
    margin-top: .35rem; padding: .4rem; background: var(--bg);
    border-radius: 6px; min-width: 260px;
}
/* FIX 1: keep the expanded sensor panel readable inside a table cell. */
.sensor-line { padding: .1rem 0; }

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

def _render_layout(title, body, current_tab=None):
    tabs = [
        ("devices", "Devices", "index"),
        ("history", "Audit Log", "audit_page"),
        ("admin", "Admin Config", "admin_config"),
    ]
    if session.get("username"):
        tabs.append(("print", "Print Report", "print_report"))
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
<div class="container">{body}</div>
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
DASHBOARD_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.js")
try:
    DASHBOARD_JS = open(DASHBOARD_JS_PATH).read()
except OSError:
    log.warning("dashboard.js not found at %s; dashboard will be non-interactive", DASHBOARD_JS_PATH)
    DASHBOARD_JS = "console.error('dashboard.js missing');"


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
        <th class="col-reason"    data-sort="reason">Reason <span class="sort-arrow"></span></th>
        <th class="col-lastseen"  data-sort="lastseen">Last Seen <span class="sort-arrow"></span></th>
        <th class="col-heartbeat" data-sort="heartbeat">HB <span class="sort-arrow"></span></th>
        <th class="col-temp"      data-sort="temp">°C <span class="sort-arrow"></span></th>
        <th class="col-hum"       data-sort="hum">RH% <span class="sort-arrow"></span></th>
        <th class="col-co2"       data-sort="co2">CO2 <span class="sort-arrow"></span></th>
        <th class="col-sensors">Sensors</th>
        <th class="col-snooze">Snooze</th>
        <th class="col-actions">Actions</th>
      </tr></thead>
      <tbody id="devices-tbody"><tr><td colspan="11" class="muted">Loading…</td></tr></tbody>
    </table>
  </div>
</div>
<script>window.REFRESH_SECONDS = {refresh};</script>
<script>{DASHBOARD_JS}</script>
"""
    return _render_layout("Dashboard", body, current_tab="devices")

@app.route("/api/devices")
@login_required
def api_devices():
    """Devices list + per-device sensor counts/values for the dashboard.

    Sensors are stored in `sensor_readings` (one row per device+sensor+type);
    we aggregate here so the JS poll is a single request, not N+1.
    """
    conn = get_db()
    devices = [dict(r) for r in conn.execute(
        "SELECT * FROM devices ORDER BY device_id ASC"
    ).fetchall()]
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

    for d in devices:
        s = sensors_by_device.get(d["device_id"], [])
        d["sensors"] = s
        d["sensor_count"] = len({x["sensor_name"] for x in s})
        d["location"] = d.get("location") or ""
        d["role"] = d.get("role") or ""
        d["faults"] = faults_by_device.get(d["device_id"], [])

        # Per-sensor health, mirroring the watchdog's own classification so the
        # operator sees exactly what the watchdog saw.
        healthy, suspect, silent = [], [], []
        for x in s:
            if x["sensor_type"] == "heartbeat":
                continue  # a health counter, not an environmental reading
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

    # Systemic fault rollup for the dashboard banner. Needs its own
    # connection because the main one is closed by this point.
    try:
        flatline_limit = int(config.get("watchdog", {}).get("sensor_flatline_seconds", 300))
        sysconn = get_db()
        systemic = _detect_systemic_faults(sysconn, datetime.now(), flatline_limit)
        sysconn.close()
    except Exception as e:
        log.exception("systemic fault detection failed")
        systemic = []

    with mqtt_health_lock:
        health = dict(mqtt_health)
    return jsonify({"devices": devices, "mqtt": health, "systemic": systemic})

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

@app.route("/snooze/<device_id>/<int:hours>")
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

@app.route("/unsnooze/<device_id>")
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
    try:
        client.publish(topic, "PRESS", qos=0, retain=False)
        audit("RESTART_SENT", device_id=device_id, details=f"Published to {topic}")
        log.info(f"Restart command published for {device_id} on {topic}")
        return jsonify({"ok": True, "topic": topic})
    except Exception as e:
        log.exception("Restart publish failed")
        return jsonify({"ok": False, "error": str(e)}), 500

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
    rows_html = "".join(
        f'<tr><td class="ts">{(r["timestamp"] or "")[:19]}</td>'
        f'<td>{r["username"] or ""}</td>'
        f'<td>{r["action"]}</td>'
        f'<td>{r["device_id"] or ""}</td>'
        f'<td class="muted">{r["details"] or ""}</td></tr>'
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
    di = header_norm.index("device_id")
    li = header_norm.index("location") if "location" in header_norm else None
    ri = header_norm.index("role")       if "role"       in header_norm else None

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
        rows.append({
            "device_id": device_id,
            "location":  location,
            "role":      role,
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
    cursor.execute("UPDATE csv_uploads SET applied = 1 WHERE upload_id = ?", (upload_id,))
    conn.commit(); conn.close()
    audit("CSV_UPLOAD",
          details=f"upload_id={upload_id}; rows={len(rows)}; file={filename!r}; note={note!r}")
    return redirect(url_for("admin_config", saved=1))


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
      <tbody>{''.join(f'<tr><td>{r["upload_id"]}</td><td class="ts">{(r["saved_at"] or "")[:19]}</td><td>{r["saved_by"]}</td><td>{html_lib.escape(r["filename"] or "")}</td><td>{r["row_count"]}</td><td class="muted">{html_lib.escape(r["diff_summary"] or "")}</td></tr>' for r in rows) or '<tr><td colspan="6" class="muted">No uploads yet.</td></tr>'}</tbody>
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
        f'<td>{r["saved_by"]}</td>'
        f'<td>{r["note"] or ""}</td>'
        f'<td><a class="btn btn-sm" href="/admin/config/download/{r["version_id"]}">Download</a> '
        f'<a class="btn btn-sm btn-warning" href="/admin/config/restore/{r["version_id"]}" '
        f'onclick="return confirm(\'Restore this version as the new live config?\')">Restore</a></td></tr>'
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
    Upload a CSV to assign a <strong>location</strong> and optional <strong>role</strong> to each device.
    Header row required. Only the columns named below are read; extra columns are ignored.
  </p>
  <details style="margin-bottom:1rem">
    <summary class="muted">CSV format &amp; example</summary>
    <pre class="diff" style="margin-top:.5rem">device_id,location,role
sht41-1,Spawn Incubator A,producer
sht41-2,"Fruiting Room, North",dup
sht41-3,Fruiting Room B,
bme280-1,Drying Room,reference</pre>
  </details>
  <form method="post" action="/admin/sensors/csv">
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
    if "web" in new_cfg and "dashboard_refresh_seconds" in new_cfg["web"]:
        config["web"]["dashboard_refresh_seconds"] = new_cfg["web"]["dashboard_refresh_seconds"]

    body = """<div class="card">
  <div class="flash success">Configuration saved and applied (host/port changes need restart).</div>
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

@app.route("/admin/config/restore/<int:version_id>", methods=["GET"])
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
        f'<tr><td>{fmt(a["triggered_at"])}</td><td>{a["device_id"]}</td>'
        f'<td>{a["alert_type"]}</td><td>{a["message"]}</td></tr>'
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
# Misc
# -----------------------------------------------------------------------------
@app.route("/health")
def health():
    with mqtt_health_lock:
        return jsonify({"ok": mqtt_health["connected"]})

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
    try:
        with mqtt_health_lock:
            connected = mqtt_health["connected"]
            last_disc = mqtt_health["last_disconnected_at"]
        if connected or last_disc is None:
            return
        downtime = (datetime.now() - last_disc).total_seconds()
        if 60 <= downtime < 65 or int(downtime) % 600 == 0:
            telegram_engine.send_message(
                "⚠️ <b>MQTT broker unreachable.</b>\n"
                f"Down for {int(downtime)}s. Watchdog is blind until reconnect."
            )
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
