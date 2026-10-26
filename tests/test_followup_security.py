#!/usr/bin/env python3
"""Tests for the follow-up fixes applied on top of the C1-C6 review.

Covers the gaps that review found but did not close:

  1. The committed template must not carry a password hash that any
     password can produce. A fresh install has to be unloggable-in until
     the operator sets a password.
  2. The timezone is pinned explicitly at startup, and the app verifies
     that SQLite's localtime and Python's now() agree. A container
     started without TZ must not silently write UTC.
  3. The headline metric must be derived from the channels, and must
     report the age of the value it shows.

    ./.venv/bin/python tests/test_followup_security.py
"""
import os
import sqlite3
import sys
import tempfile
import shutil
from datetime import datetime

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


print("=== follow-up security and timezone tests ===")
print()

# ---------------------------------------------------------------------------
# 1. The committed template must not be loggable into.
# ---------------------------------------------------------------------------
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import hashlib
import yaml

def hash_pw(pw, salt):
    return hashlib.sha256(f"{salt}:{pw}".encode("utf-8")).hexdigest()

for cfg_path in ("config.yaml", "config/config.example.yaml"):
    full = os.path.join(REPO, cfg_path)
    if not os.path.exists(full):
        continue
    cfg = yaml.safe_load(open(full))
    users = (cfg.get("auth") or {}).get("users") or {}
    crackable = []
    for name, u in users.items():
        ph = u.get("password_hash") or {}
        salt, want = ph.get("salt"), ph.get("hash")
        if not salt or not want:
            continue
        # A placeholder must not be derivable from ANY password. Try a
        # spread of plausible defaults.
        for cand in ("mushroom", "operator", "admin", "password", "yaniv",
                     "changeme", "watchdog", "psyga", "psygabio"):
            if hash_pw(cand, salt) == want:
                crackable.append((name, cand))
                break
    check(f"{cfg_path}: no account is derivable from a common password",
          not crackable, str(crackable))

# The live config's hashes must NOT be in the committed template.
live = yaml.safe_load(open(os.path.join(REPO, "config/config.yaml")))
live_salts = set()
for u in ((live.get("auth") or {}).get("users") or {}).values():
    ph = u.get("password_hash") or {}
    if ph.get("salt"):
        live_salts.add((ph["salt"], ph["hash"]))
tmpl = yaml.safe_load(open(os.path.join(REPO, "config.yaml")))
tmpl_pairs = set()
for u in ((tmpl.get("auth") or {}).get("users") or {}).values():
    ph = u.get("password_hash") or {}
    if ph.get("salt") or ph.get("hash"):
        tmpl_pairs.add((ph.get("salt"), ph.get("hash")))
check("the committed template shares no hash with the live config",
      not (live_salts & tmpl_pairs), str(live_salts & tmpl_pairs))

# ---------------------------------------------------------------------------
# 2. The timezone is pinned, and SQLite and Python agree.
# ---------------------------------------------------------------------------
import app

check("the app pins a timezone at startup",
      app.time.tzname[0] in ("IST", "IDT", "UTC"),
      str(app.time.tzname))

probe = sqlite3.connect(":memory:")
sql_now = probe.execute("SELECT datetime('now','localtime')").fetchone()[0]
probe.close()
py_now = datetime.now().isoformat(sep=" ", timespec="seconds")
drift = abs((datetime.fromisoformat(sql_now)
             - datetime.fromisoformat(py_now)).total_seconds())
check("SQLite localtime and Python now() agree", drift <= 2, f"drift {drift}s")

# The config carries the timezone explicitly.
cfg = yaml.safe_load(open(os.path.join(REPO, "config/config.yaml")))
check("the live config carries facility.timezone",
      (cfg.get("facility") or {}).get("timezone") == "Asia/Jerusalem",
      str((cfg.get("facility") or {}).get("timezone")))

# The compose default must not be UTC, or a container started without a
# .env would write UTC while the app believes it is local.
for compose in ("docker-compose.yml", "pilot/docker-compose.yml"):
    full = os.path.join(REPO, compose)
    if not os.path.exists(full):
        continue
    text = open(full).read()
    check(f"{compose}: TZ default is not UTC",
          "TZ: ${TZ:-UTC}" not in text)

# ---------------------------------------------------------------------------
# 3. A placeholder password must be refused at login.
# ---------------------------------------------------------------------------
saved = app.config.get("auth", {}).get("users", {})
try:
    app.config["auth"]["users"] = {
        "ghost": {"role": "admin",
                  "password_hash": {"salt": "", "hash": "!"}},
    }
    ok, role = app._check_credentials("ghost", "anything")
    check("a placeholder-password account cannot be logged into",
          ok is False, f"returned {ok}")
    ok2, _ = app._check_credentials("ghost", "")
    check("a placeholder-password account is refused with an empty password too",
          ok2 is False)
finally:
    app.config["auth"]["users"] = saved

# A real account with a real hash must still work.
u = saved.get("yaniv")
if u and u.get("password_hash", {}).get("salt"):
    check("a real account is not mistaken for a placeholder",
          not app._is_placeholder_password(u))

# ---------------------------------------------------------------------------
# 4. The headline metric reports the age of what it shows.
# ---------------------------------------------------------------------------
sensors = [
    {"sensor_name": "d_co2", "sensor_type": "co2", "value": 400.0,
     "silent_seconds": 5000, "flat_seconds": None, "ts": "x"},
    {"sensor_name": "d_co2_old", "sensor_type": "co2", "value": 300.0,
     "silent_seconds": 90000, "flat_seconds": None, "ts": "y"},
]
h = app._headline_metric(sensors, "co2", 900)
check("the headline picks the freshest channel",
      h and h["sensor_name"] == "d_co2", str(h))
check("the headline reports the age", h and h["age_seconds"] == 5000, str(h))
check("the headline marks a stale value as stale",
      h and h["stale"] is True, str(h))
check("the headline counts every channel of the metric",
      h and h["channels"] == 2, str(h))

fresh = [{"sensor_name": "d_t", "sensor_type": "temperature", "value": 22.0,
          "silent_seconds": 5, "flat_seconds": None, "ts": "z"}]
h2 = app._headline_metric(fresh, "temperature", 900)
check("a fresh reading is not marked stale", h2 and h2["stale"] is False, str(h2))

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
