#!/usr/bin/env python3
"""Tests for the snooze semantics and the target-range editor.

The snooze contract, in one place:

  * A snoozed device's faults do NOT appear on the wall board.
  * A snooze SUPPRESSES the fault - it does not just hide it. The range
    pass must not re-raise an alert the operator just silenced, or the
    fault comes back to the wall board within 30 seconds.
  * An unsnooze brings a live fault back IMMEDIATELY, not on the next
    30-second watchdog pass.

    ./.venv/bin/python tests/test_snooze_semantics.py
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

print("=== snooze semantics ===")
print()

# ---------------------------------------------------------------------------
# 1. The wallboard respects a snooze.
# ---------------------------------------------------------------------------
i = src.find("def api_wallboard")
j = src.find("\n@app.route", i + 10)
wall = src[i:j if j > 0 else i + 4000]
check("the wallboard query filters on snooze_until",
      "snooze_until" in wall)
check("the wallboard joins the devices table to read the snooze",
      "LEFT JOIN devices" in wall)
check("the wallboard passes the comparison time as a parameter",
      re.search(r'WHERE a\.resolved = 0.*snooze_until <= \?', wall, re.S) is not None)

# ---------------------------------------------------------------------------
# 2. A snooze suppresses the fault, it does not merely hide it.
# ---------------------------------------------------------------------------
i = src.find("def set_snooze")
j = src.find("\n@app.route", i + 10)
snooze = src[i:j if j > 0 else i + 2000]
check("a snooze resolves the device's open alerts",
      "UPDATE alerts SET resolved = 1" in snooze)
check("a snooze clears the out_of_range dwell record",
      "DELETE FROM out_of_range" in snooze,
      "otherwise the range pass re-raises the silenced fault")
check("the snooze is audited", "SNOOZE" in snooze)

# ---------------------------------------------------------------------------
# 3. An unsnooze brings a live fault back immediately.
# ---------------------------------------------------------------------------
i = src.find("def unsnooze")
j = src.find("\n@app.route", i + 10)
unsnooze = src[i:j if j > 0 else i + 2000]
check("an unsnooze clears the deadline",
      "snooze_until = NULL" in unsnooze)
check("an unsnooze re-evaluates the device immediately",
      "_reevaluate_device" in unsnooze)
check("the unsnooze is audited", "UNSNOOZE" in unsnooze)

# The re-evaluation helper itself.
i = src.find("def _reevaluate_device")
check("the re-evaluation helper exists", i > 0)
if i > 0:
    j = src.find("\n@app.route", i + 10)
    fn = src[i:j if j > 0 else i + 2000]
    check("it re-opens the device's alert of its current fault type",
          "resolved = 0" in fn and "alert_type" in fn)
    check("it only acts on a real fault type",
          "_DEVICE_ALERT_TYPES" in fn)
    check("it is called from the unsnooze handler",
          "_reevaluate_device(device_id)" in unsnooze)

# ---------------------------------------------------------------------------
# 4. The target-range editor.
# ---------------------------------------------------------------------------
print()
print("=== the target range editor ===")
check("/admin/ranges is registered",
      '@app.route("/admin/ranges"' in src)
check("it is admin-only",
      re.search(r'def admin_ranges\(\):.*?role"\) != "admin"', src, re.S) is not None)
check("it is reachable from the ignore list page",
      'href="/admin/ranges"' in src)
check("a saved range is audited", "RANGE_SAVED" in src)
check("a removed range is audited", "RANGE_REMOVED" in src)
check("a range change clears the stale alert for that channel",
      "OUT_OF_RANGE" in src and "message LIKE" in src)
check("an invalid range (min above max) is rejected",
      "is above the maximum" in src)
check("a blank range removes the threshold",
      "Both blank is a delete" in src)
# The alert table has no sensor_name column - the match must be on the
# message, or the UPDATE raises "no such column" and the page 500s.
check("the alert update matches on the message, not a sensor_name column",
      "message LIKE ?" in src
      and "sensor_name = ? AND alert_type = 'OUT_OF_RANGE'" not in src)

# The range editor's channel picker must come from the real channels.
check("the channel picker is built from the channels that exist",
      "_sensor_channels" in src)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
