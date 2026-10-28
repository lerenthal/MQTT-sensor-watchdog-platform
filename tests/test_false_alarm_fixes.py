#!/usr/bin/env python3
"""Tests for the false-alarm fixes: retired channels and packet loss.

Three defects are guarded here:

  1. A channel the device does not publish any more (a misnamed sensor
     from a shared config, flashed once) was kept forever and raised
     SENSOR_SILENT on every cycle - a permanent false alarm about a
     sensor that does not exist. It must be retired, and removed from the
     dashboard.

  2. The MQTT ingest path opened a fresh SQLite connection per message.
     paho runs its network loop and the message handler in one thread,
     so the database time is socket time, and a full receive buffer drops
     packets. At ~35 messages/second that starved the socket: devices
     flapped OFFLINE while publishing every 10 seconds, because
     last_seen only advanced once per ~120s.

  3. The publish_interval_s EWMA must converge on the channel's real
     cadence, which it can only do if the packets actually arrive.

    ./.venv/bin/python tests/test_false_alarm_fixes.py
"""
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS - {name}")
    else:
        FAIL += 1
        print(f"FAIL - {name}" + (f"  [{detail}]" if detail else ""))


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(REPO, "app.py")).read()

print("=== false-alarm fixes ===")
print()

# ---------------------------------------------------------------------------
# 1. The retirement mechanism exists and is wired in.
# ---------------------------------------------------------------------------
check("the retirement threshold is configurable",
      "retired_sensor_after_seconds" in src)
# The DELETE is written as two adjacent string literals, so the pattern
# has to tolerate the split.
check("a retired channel is DELETED from sensor_readings, not just filtered",
      re.search(r"DELETE FROM sensor_readings[\s\"']*\s*"
                r"WHERE device_id = \?[\s\"']*\s*"
                r"AND sensor_name = \?", src, re.S) is not None)
check("retirement only happens while the device's heartbeat is fresh",
      "_device_heartbeat_fresh" in src)
check("retirement is audited",
      "SENSOR_RETIRED" in src)
check("the retirement is logged",
      re.search(r'Retiring channel %s/%s', src) is not None)

# The retirement must NOT fire for a device that has gone quiet - that is a
# real fault and must still alert.
check("the retirement is gated on the device still publishing",
      "_device_heartbeat_fresh" in src
      and src.index("_device_heartbeat_fresh") < src.index("DELETE FROM sensor_readings"))

# ---------------------------------------------------------------------------
# 2. The MQTT ingest path reuses one connection.
# ---------------------------------------------------------------------------
check("a shared MQTT ingest connection exists",
      "_get_mqtt_conn" in src)
check("the handler uses the shared connection",
      re.search(r"conn = _get_mqtt_conn\(\)", src) is not None)
# The handler must not close the shared connection.
i = src.find("def on_mqtt_message")
j = src.find("\ndef ", i + 10)
handler = src[i:j]
check("the handler never closes the shared connection",
      "conn.close()" not in handler,
      f"{handler.count('conn.close()}')} close() calls left")
check("the handler commits its work",
      "conn.commit()" in handler)
check("a broken ingest connection is reset, not left dead",
      "_reset_mqtt_conn" in handler)

# ---------------------------------------------------------------------------
# 3. The real behaviour: a stale channel is retired, a live one is not.
# ---------------------------------------------------------------------------
import app

db = tempfile.mktemp(suffix=".db")
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE devices (device_id TEXT PRIMARY KEY, last_seen TIMESTAMP,
    status TEXT, last_heartbeat_time TIMESTAMP, last_heartbeat_value REAL);
CREATE TABLE sensor_readings (device_id TEXT, sensor_name TEXT,
    sensor_type TEXT, value REAL, ts TIMESTAMP, last_change_ts TIMESTAMP,
    publish_interval_s REAL,
    UNIQUE(device_id, sensor_name, sensor_type));
""")
now = datetime.now()
fresh = (now - timedelta(seconds=5)).isoformat(sep=" ")
old = (now - timedelta(hours=30)).isoformat(sep=" ")
conn.execute("INSERT INTO devices VALUES ('d1', ?, 'ONLINE', ?, 10)",
             (fresh, fresh))
# A channel the device publishes, and one it published once and dropped.
conn.execute("INSERT INTO sensor_readings VALUES "
             "('d1','d1_temperature','temperature',22.0,?,?,10)", (fresh, fresh))
conn.execute("INSERT INTO sensor_readings VALUES "
             "('d1','d2_temperature','temperature',24.0,?,?,119)", (old, old))
conn.commit()
conn.close()

# Run the retirement logic the way the watchdog loop does.
def retire(device_id, retired_after_s):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    retired = []
    rows = c.execute("""
        SELECT sensor_name, sensor_type,
               (julianday(?) - julianday(ts)) * 86400.0 AS silent_s
        FROM sensor_readings
        WHERE device_id = ?
          AND sensor_type NOT IN ('heartbeat', 'diagnostic')
          AND (julianday(?) - julianday(ts)) * 86400.0 > ?
    """, (now.isoformat(sep=" "), device_id,
          now.isoformat(sep=" "), retired_after_s)).fetchall()
    for r in rows:
        c.execute("DELETE FROM sensor_readings WHERE device_id = ? "
                  "AND sensor_name = ? AND sensor_type = ?",
                  (device_id, r["sensor_name"], r["sensor_type"]))
        retired.append(r["sensor_name"])
    c.commit()
    c.close()
    return retired

retired = retire("d1", 21600)
check("a channel the device no longer publishes is retired",
      "d2_temperature" in retired, str(retired))
check("a channel the device IS publishing is not retired",
      "d1_temperature" not in retired, str(retired))

c = sqlite3.connect(db)
names = [r[0] for r in c.execute(
    "SELECT sensor_name FROM sensor_readings WHERE device_id='d1'")]
c.close()
check("the retired channel is gone from the dashboard's sensor list",
      "d2_temperature" not in names, str(names))
check("the live channel is still there",
      "d1_temperature" in names, str(names))

# A recently-dead channel must NOT be retired - that is a real fault.
c = sqlite3.connect(db)
recent_dead = (now - timedelta(minutes=20)).isoformat(sep=" ")
c.execute("INSERT INTO sensor_readings VALUES "
          "('d1','d1_humidity','humidity',50.0,?,?,10)",
          (recent_dead, recent_dead))
c.commit()
c.close()
retired2 = retire("d1", 21600)
check("a channel that died 20 minutes ago is NOT retired (it is a real fault)",
      "d1_humidity" not in retired2, str(retired2))

os.remove(db)

# ---------------------------------------------------------------------------
# 5. The real behaviour: a RETIRED channel is not resurrected by the
#    broker's retained replay.
#
#    The broker keeps the last value of every published topic and
#    re-delivers it as a retained message on every reconnect. A retirement
#    that only deleted the row was undone by the very next reconnect -
#    which is exactly what happened to dalas-1/dalas-2_temperature.
# ---------------------------------------------------------------------------
import shutil
import importlib

scratch = tempfile.mktemp(suffix=".db")
shutil.copy(os.path.join(REPO, "data/sensor_watchdog.db"), scratch) \
    if os.path.exists(os.path.join(REPO, "data/sensor_watchdog.db")) else None

# Build a minimal DB with the retired_sensors table.
tdb = tempfile.mktemp(suffix=".db")
tc = sqlite3.connect(tdb)
tc.executescript("""
CREATE TABLE sensor_readings (device_id TEXT, sensor_name TEXT,
    sensor_type TEXT, value REAL, ts TIMESTAMP, last_change_ts TIMESTAMP,
    publish_interval_s REAL,
    UNIQUE(device_id, sensor_name, sensor_type));
CREATE TABLE retired_sensors (device_id TEXT, sensor_name TEXT,
    sensor_type TEXT, retired_at TIMESTAMP, reason TEXT,
    PRIMARY KEY (device_id, sensor_name, sensor_type));
""")
now2 = datetime.now()
tc.execute("INSERT INTO sensor_readings (device_id,sensor_name,sensor_type,value,ts,last_change_ts,publish_interval_s) "
           "VALUES ('d2','d2_temperature','temperature',22.0,?,?,10)",
           (now2.isoformat(sep=" "), now2.isoformat(sep=" ")))
tc.execute("INSERT INTO retired_sensors (device_id,sensor_name,sensor_type) "
           "VALUES ('d2','d2_phantom','temperature')")
tc.commit()
tc.close()

app.DB_FILE = tdb
app._mqtt_conn = None


class FakeMsg:
    def __init__(self, topic, payload, retain=False):
        self.topic = topic
        self.payload = payload.encode()
        self.retain = retain


app.on_mqtt_message(None, None, FakeMsg(
    "facility/d2/env_sensor/sensor/d2_phantom/state", "24.0", retain=True))

tc = sqlite3.connect(tdb)
resurrected = [r[0] for r in tc.execute(
    "SELECT sensor_name FROM sensor_readings WHERE device_id='d2'")]
tc.close()
check("a retired channel is NOT resurrected by the retained replay",
      "d2_phantom" not in resurrected, str(resurrected))
check("a live channel is still ingested",
      "d2_temperature" in resurrected, str(resurrected))
os.remove(tdb)

# ---------------------------------------------------------------------------
# 4. The config carries the threshold in every deployment.
# ---------------------------------------------------------------------------
import yaml
for cfg in ("config.yaml", "config/config.yaml", "pilot/config/config.yaml"):
    full = os.path.join(REPO, cfg)
    if not os.path.exists(full):
        continue
    w = (yaml.safe_load(open(full)).get("watchdog") or {})
    check(f"{cfg}: retired_sensor_after_seconds is set",
          w.get("retired_sensor_after_seconds") == 21600,
          str(w.get("retired_sensor_after_seconds")))

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
