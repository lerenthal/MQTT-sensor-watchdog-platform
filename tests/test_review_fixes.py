"""Regression tests for the 2026-10-25 code-review fixes.

Self-contained: runs against a throwaway SQLite DB and a temp copy of
config.yaml, so it never touches the live data/ or config/ directories.

Run:  python3 tests/test_review_fixes.py        (expects the app's deps importable;
      the repo .venv has them: ./.venv/bin/python tests/test_review_fixes.py)

Covers: B1 (per-device SENSOR_SILENT), B2 (systemic cluster paging + clear),
B6 (firmware Value Stall ingestion), B8 (OUT_OF_RANGE dwell), B4 (secret-key
generation), B20 (fresh-DB boot), CSRF (B9) and XSS escaping (B3), and the
sensor classifier.
"""
import os
import sys
import shutil
import tempfile
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

_tmp = tempfile.mkdtemp(prefix="wd_test_")
_cfg = os.path.join(_tmp, "config.yaml")
shutil.copy(os.path.join(REPO, "config.yaml"), _cfg)
os.environ["WATCHDOG_CONFIG"] = _cfg
os.environ["WATCHDOG_DB"] = os.path.join(_tmp, "test.db")

import app  # noqa: E402
import yaml  # noqa: E402

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
              "sensor_thresholds", "audit_logs"):
        c.execute(f"DELETE FROM {t}")
    c.commit(); c.close()


def add_device(did, hb_s=0):
    c = app.get_db()
    c.execute("INSERT INTO devices(device_id, status, last_seen, "
              "last_heartbeat_time, last_heartbeat_value) VALUES(?,?,?,?,?)",
              (did, "ONLINE", ts(0), ts(hb_s), 100))
    c.commit(); c.close()


def add_reading(did, name, typ, val, age_s):
    c = app.get_db()
    c.execute("INSERT INTO sensor_readings(device_id, sensor_name, sensor_type, "
              "value, ts, last_change_ts) VALUES(?,?,?,?,?,?)",
              (did, name, typ, val, ts(age_s), ts(age_s)))
    c.commit(); c.close()


def open_alerts():
    c = app.get_db()
    rows = [dict(r) for r in c.execute(
        "SELECT device_id, alert_type FROM alerts WHERE resolved=0")]
    c.close()
    return rows


class Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


def main():
    # B20: a fresh DB must boot (migrations run before schema.sql).
    app.init_db()
    check("B20 fresh DB initialises", True)

    # classifier
    check("classify co2 before co", app._classify_sensor("maxim5_co2") == "co2")
    check("classify ground_temperature",
          app._classify_sensor("maxim5_ground_temperature") == "temperature")
    check("classify diagnostic uptime -> None",
          app._classify_sensor("dual_temp_uptime") is None)

    # B1
    reset()
    add_device("d_single")
    add_reading("d_single", "d_single_temperature", "temperature", 20.0, 1000)
    app.watchdog_eval_loop()
    check("B1 single silent channel -> SENSOR_SILENT",
          any(a["device_id"] == "d_single" and a["alert_type"] == "SENSOR_SILENT"
              for a in open_alerts()))

    # B2 raise + suppression
    # Simulates a watchdog that HAD been running and watched these channels
    # go quiet: they are marked as observed-alive so the restart-artefact
    # guard (channels already stale when the process started) does not
    # suppress a genuine simultaneous failure.
    reset()
    for d in ("c1", "c2", "c3"):
        add_device(d)
        add_reading(d, d + "_co2", "co2", 450.0, 1000)
        app._OBSERVED_ALIVE.add((d, d + "_co2"))
    app.watchdog_eval_loop()
    al = open_alerts()
    check("B2 cluster -> one SYSTEMIC alert",
          any(a["alert_type"] == "SYSTEMIC" and a["device_id"] == "__systemic__:co2"
              for a in al))
    check("B2 clustered devices not paged individually",
          not any(a["alert_type"] == "SENSOR_SILENT" and a["device_id"] in ("c1", "c2", "c3")
                  for a in al))

    # B2 clear
    c = app.get_db(); c.execute("UPDATE sensor_readings SET ts=?, last_change_ts=?",
                                (ts(0), ts(0))); c.commit(); c.close()
    app.watchdog_eval_loop()
    check("B2 SYSTEMIC resolves when metric returns",
          not any(a["alert_type"] == "SYSTEMIC" for a in open_alerts()))

    # B6
    reset()
    add_device("d_fw")
    add_reading("d_fw", "d_fw_temperature", "temperature", 21.0, 0)
    app.on_mqtt_message(None, None,
        Msg("facility/d_fw/env_sensor/binary_sensor/d_fw_value_stall/state", "ON"))
    c = app.get_db()
    diag = c.execute("SELECT value FROM sensor_readings WHERE device_id='d_fw' "
                     "AND sensor_type='diagnostic'").fetchone()
    c.close()
    check("B6 binary_sensor stored as diagnostic=1.0",
          diag is not None and float(diag["value"]) == 1.0)
    app.watchdog_eval_loop()
    check("B6 firmware stall -> SUSPECTED_STALL",
          any(a["device_id"] == "d_fw" and a["alert_type"] == "SUSPECTED_STALL"
              for a in open_alerts()))

    # B8 dwell
    reset()
    add_device("d_oor")
    add_reading("d_oor", "d_oor_temperature", "temperature", 30.0, 0)
    c = app.get_db()
    c.execute("INSERT INTO sensor_thresholds(device_id, sensor_name, min_value, "
              "max_value, severity) VALUES(?,?,?,?,?)",
              ("d_oor", "d_oor_temperature", 18.0, 24.0, "CRITICAL"))
    c.commit(); c.close()
    app.watchdog_eval_loop()
    check("B8 first breach starts dwell, no page yet",
          not any(a["alert_type"] == "OUT_OF_RANGE" for a in open_alerts()))
    c = app.get_db(); c.execute("UPDATE out_of_range SET entered_at=?", (ts(120),))
    c.commit(); c.close()
    app.watchdog_eval_loop()
    check("B8 breach past dwell -> OUT_OF_RANGE raised",
          any(a["device_id"] == "d_oor" and a["alert_type"] == "OUT_OF_RANGE"
              for a in open_alerts()))

    # B4 secret-key generation (temp config, safe to overwrite)
    app.config["web"]["secret_key"] = "CHANGE_ME_placeholder"
    app._ensure_secret_key()
    import re
    gen = app.config["web"]["secret_key"]
    check("B4 placeholder key regenerated (64-hex)",
          bool(re.fullmatch(r"[0-9a-f]{64}", gen))
          and yaml.safe_load(open(_cfg))["web"]["secret_key"] == gen)

    # CSRF (B9) + XSS escaping (B3) via the test client
    app.app.secret_key = "x" * 40
    app.app.testing = True
    cl = app.app.test_client()
    cl.post("/login", data={"username": "<img src=x onerror=alert(1)>", "password": "no"})
    with cl.session_transaction() as s:
        s["username"] = "yaniv"; s["role"] = "admin"; s["_csrf"] = "tok"
    body = cl.get("/audit").get_data(as_text=True)
    check("B3 /audit escapes injected username",
          "<img src=x onerror" not in body and "&lt;img" in body)
    check("B9 POST without CSRF token -> 400",
          cl.post("/snooze/dev1/1").status_code == 400)
    check("B9 POST with CSRF token -> 302",
          cl.post("/snooze/dev1/1", data={"_csrf": "tok"}).status_code == 302)
    check("B9 config restore is POST-only (GET 405)",
          cl.get("/admin/config/restore/1").status_code == 405)
    # Restart artefact: channels that were ALREADY stale before this process
    # started were not watched going quiet, so they must not be clustered as a
    # fleet-wide fault. This is the failure mode that produced a false
    # "systemic fault on 30 devices" on every container restart.
    reset()
    for d in ("r1", "r2", "r3", "r4"):
        add_device(d)
        add_reading(d, d + "_co2", "co2", 450.0, 100000)   # very old
    app._OBSERVED_ALIVE.clear()                          # nothing seen alive
    app.watchdog_eval_loop()
    check("restart artefact: pre-existing stale channels are not clustered",
          not any(a["alert_type"] == "SYSTEMIC" for a in open_alerts()))

    # ...but once a channel IS observed alive, a genuine cluster is reported.
    reset()
    for d in ("g1", "g2", "g3"):
        add_device(d)
        add_reading(d, d + "_co2", "co2", 450.0, 1000)
        app._OBSERVED_ALIVE.add((d, d + "_co2"))
    app.watchdog_eval_loop()
    check("observed channels still cluster",
          any(a["alert_type"] == "SYSTEMIC" for a in open_alerts()))
    app._OBSERVED_ALIVE.clear()

    check("B16 /api/devices exposes flatline_limit",
          "flatline_limit" in cl.get("/api/devices").get_json())

    print()
    if FAILS:
        print("FAILURES:", FAILS)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
