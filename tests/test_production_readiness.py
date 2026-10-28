#!/usr/bin/env python3
"""Tests for the production-readiness review.

Four defects found in the review before deployment:

  1. The WAL was never checkpointed. The MQTT ingest connection is
     deliberately long-lived, and SQLite only checkpoints when the LAST
     connection closes - so the WAL grew to four times the size of the
     database and stayed there.

  2. alerts, device_status_history and audit_logs grew without bound -
     about 75 alert rows an hour on a flapping fleet. Unbounded history
     is not just a disk problem: it makes the audit trail unreadable and
     slows every query that scans it.

  3. A corrupt config file at startup produced a bare traceback and a
     crash loop with no explanation - the worst possible failure mode on
     a production server.

  4. A snoozed device's fault could still be re-raised by the range
     pass. (Covered in test_snooze_semantics.py, but the retention
     pruning must not resurrect it either.)

    ./.venv/bin/python tests/test_production_readiness.py
"""
import os
import re
import sys

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

print("=== production readiness ===")
print()

# ---------------------------------------------------------------------------
# 1. The WAL must be checkpointed.
# ---------------------------------------------------------------------------
print("-- the write-ahead log --")
check("there is a WAL checkpoint function",
      "def _checkpoint_wal(" in src)
check("it uses a truncating checkpoint",
      "wal_checkpoint(TRUNCATE)" in src)
check("it is scheduled",
      re.search(r'scheduler\.add_job\(\s*\n?\s*_checkpoint_wal', src) is not None)
check("it runs on a timer, not every evaluation cycle",
      re.search(r'_checkpoint_wal, "interval",\s*\n\s*(?:seconds|minutes)=\d+', src) is not None)
# The auto-checkpoint threshold must not be RAISED above SQLite's default:
# the long-lived ingest connection holds a read snapshot that stops the
# auto-checkpoint from resetting the WAL, so a bigger threshold just means
# a bigger WAL that never shrinks.
m = re.search(r'PRAGMA wal_autocheckpoint=(\d+)', src)
check("the WAL auto-checkpoint is not above the default", m and int(m.group(1)) <= 1000,
      f"wal_autocheckpoint={m.group(1) if m else '?'}")

# ---------------------------------------------------------------------------
# 2. The history tables must be pruned.
# ---------------------------------------------------------------------------
print()
print("-- history retention --")
check("there is a history pruning function",
      "def _prune_history(" in src)
for tbl in ("alerts", "device_status_history", "audit_logs"):
    check(f"{tbl} is pruned",
          f"DELETE FROM {tbl}" in src)
check("the pruning is configurable",
      "alert_retention_days" in src
      and "status_history_retention_days" in src
      and "audit_retention_days" in src)
check("a retention of 0 disables the pruning",
      re.search(r"if alert_days > 0:", src) is not None)
check("the pruning is AUDITED - a silently truncated audit log is not one",
      "HISTORY_PRUNED" in src)
check("the pruner runs on its own connection",
      re.search(r'def _prune_history_job\(\).*?get_db\(\)', src, re.S) is not None)
check("the pruner is scheduled, not in the 30-second loop",
      re.search(r'_prune_history_job, "interval",\s*\n\s*hours=\d+', src) is not None)
# Open alerts must never be pruned - they are live faults.
check("only RESOLVED alerts are pruned",
      "DELETE FROM alerts WHERE resolved = 1" in src)

# ---------------------------------------------------------------------------
# 3. A corrupt config must say why.
# ---------------------------------------------------------------------------
print()
print("-- startup robustness --")
check("the config load is wrapped in a try",
      re.search(r'try:\s*\n\s*with open\(CONFIG_FILE, "r"\)', src) is not None)
check("a config read failure logs the reason",
      "Cannot read the configuration file" in src)
check("a config read failure explains the fix",
      "Restore the last good version" in src)
check("a non-mapping config is rejected",
      "the top level of the config must be a mapping" in src)

# ---------------------------------------------------------------------------
# 4. The config keys exist in every deployment.
# ---------------------------------------------------------------------------
print()
print("-- the config keys are in every deployment --")
import yaml
for cfg in ("config.yaml", "config/config.yaml", "pilot/config/config.yaml"):
    full = os.path.join(REPO, cfg)
    if not os.path.exists(full):
        continue
    w = (yaml.safe_load(open(full)).get("watchdog") or {})
    check(f"{cfg}: alert_retention_days", w.get("alert_retention_days") == 90)
    check(f"{cfg}: status_history_retention_days",
          w.get("status_history_retention_days") == 30)
    check(f"{cfg}: audit_retention_days", w.get("audit_retention_days") == 365)

# ---------------------------------------------------------------------------
# 5. The retention keys are documented in the config help.
# ---------------------------------------------------------------------------
i = src.find("CONFIG_HELP = [")
help_block = src[i:i + 8000]
for key in ("watchdog.alert_retention_days",
            "watchdog.status_history_retention_days",
            "watchdog.audit_retention_days"):
    check(f"the config help documents {key}", key in help_block)

# ---------------------------------------------------------------------------
# 6. No SQL may bind a raw datetime.
#
# SQLite's default datetime adapter is deprecated in Python 3.12 and is
# slated for removal. It also formats differently from the explicit ISO
# strings used everywhere else, so the same column can end up holding two
# formats - and the mixed formats then compare wrongly.
# ---------------------------------------------------------------------------
print()
print("-- no raw datetime bound to SQL --")
check("there is one timestamp formatter",
      "def _ts(" in src)
# Every parameter tuple that binds a timestamp must use _ts().
raw = []
# Every SQL parameter tuple that binds a timestamp. The pattern looks for
# a tuple followed by a cursor/conn call - which is what a parameter list
# looks like - and skips function definitions, whose signature also ends
# in a comma-separated tuple.
for m in re.finditer(r"\(([^()]*\bnow\b[^()]*)\)\s*,\s*(?:cursor|conn)\.", src):
    tup = m.group(1)
    if re.search(r"(?<!_ts\()(?<![\w.])now(?=[,)])", tup):
        ln = src[:m.start()].count("\n") + 1
        line = src.splitlines()[ln - 1]
        if line.strip().startswith("def "):
            continue
        raw.append(ln)
# The multi-line form: a parameter list that ends on a line of its own.
for m in re.finditer(r"^\s*val,\s*now,\s*tol\)", src, re.M):
    ln = src[:m.start()].count("\n") + 1
    raw.append(ln)
check("no SQL parameter binds a bare datetime", not raw, str(raw))

# ---------------------------------------------------------------------------
# 7. A read-only config must not break anything.
#
#    In production the config is mounted READ-ONLY, so a compromised
#    process cannot rewrite the MQTT password or the session key. Three
#    things must therefore work anyway: the app must start, the ignore
#    list must be savable, and the failure must be a note rather than a
#    traceback filling the log.
# ---------------------------------------------------------------------------
print()
print("-- a read-only config --")
check("the ignore list lives in the DATABASE, not the config file",
      "CREATE TABLE IF NOT EXISTS ignored_devices" in src
      and "FROM ignored_devices" in src)
check("the ignore list is seeded from the config on first run",
      re.search(r'_seed\s*=\s*config\.get\("watchdog"', src) is not None)
check("saving the ignore list does not depend on writing the config",
      "_set_ignored_devices(" in src
      and re.search(r'if action == "add"[\s\S]{0,400}?'
                   r'_set_ignored_devices\(_ignored\)[\s\S]{0,120}?'
                   r'_persist_config\(\)', src) is None,
      "the add path still calls _persist_config directly")
check("a config write failure is a warning, not a traceback",
      re.search(r'except OSError as e:\s*\n\s*# A read-only config is EXPECTED', src)
      is not None)
check("the password upgrade survives a read-only config",
      "The config may be mounted READ-ONLY" in src
      and re.search(r'except OSError as e:\s*\n\s*log\.warning\(\s*"Could not persist the password upgrade', src)
      is not None)
check("there is a tool to pre-hash a password",
      os.path.exists(os.path.join(REPO, "tools/set_password.py")))

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
