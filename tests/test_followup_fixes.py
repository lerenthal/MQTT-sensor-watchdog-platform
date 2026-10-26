"""Regression tests for the 2026-10-26 review (C1-C6).

Self-contained: throwaway DB + temp copy of config.yaml, so the live data/ and
config/ are never touched.

Run:  ./.venv/bin/python tests/test_followup_fixes.py

Covers:
  C1  timestamps are local wall-clock, not SQLite's UTC CURRENT_TIMESTAMP
      (this one silently defeated the OUT_OF_RANGE dwell and skewed the GMP
      audit trail by the UTC offset)
  C2  headline metrics come from the channels, carry an age, and are still
      shown when stale
  C3  adaptive staleness: a slow channel is judged against its own cadence
  C4  OUT_OF_RANGE clear-side deadband stops boundary flapping
  C5  /api/devices does not leak SQLite connections per poll
"""
import os
import re
import sys
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

_tmp = tempfile.mkdtemp(prefix="wd_follow_")
_cfg = os.path.join(_tmp, "config.yaml")
shutil.copy(os.path.join(REPO, "config.yaml"), _cfg)
os.environ["WATCHDOG_CONFIG"] = _cfg
os.environ["WATCHDOG_DB"] = os.path.join(_tmp, "test.db")

import app  # noqa: E402

FAILS = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        FAILS.append(name)


def ts(delta_s=0):
    return (datetime.now() - timedelta(seconds=delta_s)).isoformat(sep=" ")


def reset():
    c = app.get_db()
    for t in ("devices", "sensor_readings", "alerts", "out_of_range",
              "sensor_thresholds", "audit_logs", "device_status_history"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit(); c.close()


def add_device(did, hb_s=0):
    c = app.get_db()
    c.execute("INSERT INTO devices(device_id, status, last_seen, "
              "last_heartbeat_time, last_heartbeat_value) VALUES(?,?,?,?,?)",
              (did, "ONLINE", ts(0), ts(hb_s), 100))
    c.commit(); c.close()


def add_reading(did, name, typ, val, age_s, interval=None):
    c = app.get_db()
    c.execute("INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, "
              "value, ts, last_change_ts, publish_interval_s) VALUES(?,?,?,?,?,?,?)",
              (did, name, typ, val, ts(age_s), ts(age_s), interval))
    c.commit(); c.close()


def open_alerts():
    c = app.get_db()
    rows = [dict(r) for r in c.execute(
        "SELECT device_id, alert_type FROM alerts WHERE resolved=0")]
    c.close()
    return rows


def main():
    app.init_db()

    # ---- C1: local wall-clock timestamps --------------------------------
    # Force the legacy UTC column defaults, exactly as a pre-existing
    # production database still has them, and prove the explicit writes win.
    reset()
    c = app.get_db(); cur = c.cursor()
    now = datetime.now()
    app._raise_or_nag(cur, "tz", "OFFLINE", "probe", 180, now)
    app.audit("TZ_PROBE", device_id="tz", details="p", username="t", cursor=cur)
    c.commit()
    a = c.execute("SELECT triggered_at FROM alerts WHERE device_id='tz'").fetchone()
    g = c.execute("SELECT timestamp FROM audit_logs WHERE action='TZ_PROBE'").fetchone()
    c.close()
    skew_a = abs((now - app._parse_ts(str(a["triggered_at"]))).total_seconds())
    skew_g = abs((now - app._parse_ts(str(g["timestamp"]))).total_seconds())
    check("C1 alerts.triggered_at is local wall-clock", skew_a < 5)
    check("C1 audit_logs.timestamp is local wall-clock", skew_g < 5)

    # ---- C2: honest headline metrics -------------------------------------
    sensors = [
        {"sensor_type": "co2", "sensor_name": "d_co2_old", "value": 999.0,
         "ts": ts(5000), "silent_seconds": 5000, "flat_seconds": 5000,
         "in_range": None, "threshold": None, "known_degraded": False},
        {"sensor_type": "co2", "sensor_name": "d_co2_new", "value": 450.0,
         "ts": ts(30), "silent_seconds": 30, "flat_seconds": 30,
         "in_range": None, "threshold": None, "known_degraded": False},
    ]
    h = app._headline_metric(sensors, "co2", 900)
    check("C2 headline picks the FRESHEST channel",
          h and h["value"] == 450.0 and h["sensor_name"] == "d_co2_new")
    check("C2 headline counts all channels of the metric", h and h["channels"] == 2)
    check("C2 fresh reading is not marked stale", h and h["stale"] is False)

    stale_only = [dict(sensors[0])]
    h2 = app._headline_metric(stale_only, "co2", 900)
    # The operator asked to still SEE CO2 on a CO2-stalled device: the value
    # must be returned, just flagged.
    check("C2 stale CO2 is still returned, flagged stale",
          h2 and h2["value"] == 999.0 and h2["stale"] is True
          and h2["age_seconds"] >= 5000)
    check("C2 device with no such metric returns None",
          app._headline_metric(sensors, "humidity", 900) is None)

    # ---- C3: adaptive staleness ------------------------------------------
    # A channel that genuinely publishes every ~900s must NOT be called silent
    # at 1000s; the old fixed window did exactly that and made it flap.
    reset()
    add_device("slow")
    add_reading("slow", "slow_co2", "co2", 500.0, 1000, interval=900.0)
    app.watchdog_eval_loop()
    check("C3 slow channel within its own cadence is not SENSOR_SILENT",
          not any(x["device_id"] == "slow" and x["alert_type"] == "SENSOR_SILENT"
                  for x in open_alerts()))

    # Same channel, now silent for far longer than 3x its cadence -> silent.
    reset()
    add_device("slow2")
    add_reading("slow2", "slow2_co2", "co2", 500.0, 4000, interval=900.0)
    app.watchdog_eval_loop()
    check("C3 slow channel past 3x its cadence IS SENSOR_SILENT",
          any(x["device_id"] == "slow2" and x["alert_type"] == "SENSOR_SILENT"
              for x in open_alerts()))

    # A fast channel is still caught promptly by the floor.
    reset()
    add_device("fast")
    add_reading("fast", "fast_co2", "co2", 500.0, 1000, interval=30.0)
    app.watchdog_eval_loop()
    check("C3 fast channel past the floor IS SENSOR_SILENT",
          any(x["device_id"] == "fast" and x["alert_type"] == "SENSOR_SILENT"
              for x in open_alerts()))

    # ---- C4: OUT_OF_RANGE clear-side deadband ----------------------------
    # Band 18..24, margin 2% of 6 = 0.12, so anything above 23.88 must NOT
    # clear the breach.
    reset()
    add_device("oor")
    add_reading("oor", "oor_t", "temperature", 30.0, 0)
    c = app.get_db()
    c.execute("INSERT INTO sensor_thresholds(device_id, sensor_name, min_value, "
              "max_value, severity) VALUES('oor','oor_t',18.0,24.0,'CRITICAL')")
    c.commit(); c.close()
    app.watchdog_eval_loop()                       # opens the breach (dwell starts)
    c = app.get_db()
    c.execute("UPDATE out_of_range SET entered_at=?", (ts(300),))
    c.commit(); c.close()
    app.watchdog_eval_loop()                       # dwell elapsed -> raised
    check("C4 breach raised after dwell",
          any(x["device_id"] == "oor" and x["alert_type"] == "OUT_OF_RANGE"
              for x in open_alerts()))

    # Value drops just inside the limit but still within the deadband.
    c = app.get_db()
    c.execute("UPDATE sensor_readings SET value=23.95, ts=? WHERE device_id='oor'", (ts(0),))
    c.commit(); c.close()
    app.watchdog_eval_loop()
    check("C4 value inside limits but within deadband does NOT clear",
          any(x["device_id"] == "oor" and x["alert_type"] == "OUT_OF_RANGE"
              for x in open_alerts()))

    # Value returns clearly inside -> clears.
    c = app.get_db()
    c.execute("UPDATE sensor_readings SET value=22.0, ts=? WHERE device_id='oor'", (ts(0),))
    c.commit(); c.close()
    app.watchdog_eval_loop()
    check("C4 value clear of the deadband DOES clear",
          not any(x["device_id"] == "oor" and x["alert_type"] == "OUT_OF_RANGE"
                  for x in open_alerts()))

    # ---- C6: an OUT_OF_RANGE alert must SURVIVE across cycles ------------
    # The device loop used to blanket-resolve every alert for a device it
    # considered otherwise healthy, including OUT_OF_RANGE, which the range
    # pass then re-created. _raise_or_nag therefore saw "no active alert"
    # every cycle and sent a brand-new page each time: 120 pages an hour from
    # one sensor, with nagging_interval_seconds bypassed completely.
    reset()
    add_device("rp")
    add_reading("rp", "rp_t", "temperature", 30.0, 0)
    c = app.get_db()
    c.execute("INSERT INTO sensor_thresholds(device_id, sensor_name, min_value, "
              "max_value, severity) VALUES('rp','rp_t',18.0,24.0,'CRITICAL')")
    c.commit(); c.close()
    app.watchdog_eval_loop()
    c = app.get_db(); c.execute("UPDATE out_of_range SET entered_at=?", (ts(300),))
    c.commit(); c.close()

    pages = []
    real_send = app.telegram_engine.send_nag_alert
    app.telegram_engine.send_nag_alert = lambda d, t, m: pages.append((d, t))
    try:
        for _ in range(5):
            app.watchdog_eval_loop()
    finally:
        app.telegram_engine.send_nag_alert = real_send

    c = app.get_db()
    rows = c.execute("SELECT id, resolved FROM alerts "
                     "WHERE device_id='rp' AND alert_type='OUT_OF_RANGE'").fetchall()
    c.close()
    # Exactly one alert row, still open, and exactly one page (the initial
    # raise) — not one per cycle.
    check("C6 OUT_OF_RANGE stays ONE durable alert across cycles",
          len(rows) == 1 and rows[0]["resolved"] == 0)
    check(f"C6 no re-page per cycle (pages={len(pages)} over 5 cycles)",
          len(pages) <= 1)

    # The device loop must still own and resolve its OWN alert types.
    reset()
    add_device("own", hb_s=100000)          # heartbeat frozen -> STALLED
    app.watchdog_eval_loop()
    check("C6 device loop still raises its own types (STALLED)",
          any(x["device_id"] == "own" and x["alert_type"] == "STALLED"
              for x in open_alerts()))
    c = app.get_db()
    c.execute("UPDATE devices SET last_heartbeat_time=? WHERE device_id='own'", (ts(0),))
    c.commit(); c.close()
    app.watchdog_eval_loop()
    check("C6 device loop still resolves its own types when healthy",
          not any(x["device_id"] == "own" and x["alert_type"] == "STALLED"
                  for x in open_alerts()))

    # ---- C5: /api/devices must not leak connections ----------------------
    app.app.secret_key = "x" * 40
    app.app.testing = True
    cl = app.app.test_client()
    with cl.session_transaction() as s:
        s["username"] = "yaniv"; s["role"] = "admin"; s["_csrf"] = "tok"
    import gc
    cl.get("/api/devices")
    gc.collect()
    before = len([o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)])
    for _ in range(12):
        cl.get("/api/devices")
    gc.collect()
    after = len([o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)])
    check(f"C5 no connection growth over 12 polls (before={before} after={after})",
          after <= before + 1)

    j = cl.get("/api/devices").get_json()
    check("C5 /api/devices still serves headline metrics",
          all("headline" in d for d in j["devices"]) if j["devices"] else True)

    print()
    if FAILS:
        print("FAILURES:", FAILS)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
