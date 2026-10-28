#!/usr/bin/env python3
"""Tests for the print report: what it contains, and how it can be shaped.

The report is a GMP record, so what it contains matters:

  * IGNORED DEVICES ARE EXCLUDED BY DEFAULT. An ignored device is one
    the operator has taken out of monitoring; putting it in the report
    would contradict that. The count of what was left out is still
    shown, so the report is honest about it.

  * The report can be FILTERED (status, location, search) and SORTED
    (device, location, status, last seen). The filters live in the URL,
    so a specific report can be linked to or reproduced.

  * The filter controls are hidden on paper, so the printed document
    contains the report and not the controls.

    ./.venv/bin/python tests/test_print_report.py
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

# Isolate the print_report function.
i = src.find("def print_report(")
j = src.find("\n@app.route", i + 10)
fn = src[i:j if j > 0 else i + 30000]

print("=== the print report ===")
print()

# --- 1. The ignored devices are excluded.
check("the report excludes the ignored devices",
      "ignored = _ignored_devices()" in fn
      and re.search(r'devices = \[d for d in all_devices '
                    r'if d\["device_id"\] not in ignored\]', fn) is not None)
check("the report says how many were excluded",
      "Ignored devices excluded" in fn)
check("the alerts for ignored devices are excluded too",
      re.search(r'alerts = \[a for a in alerts '
                r'if a\["device_id"\] not in ignored\]', fn) is not None)
check("the ignored devices can be included on request",
      "ignored=1" in fn or 'f_include_ignored' in fn)

# --- 2. The filters.
check("the report takes a status filter",
      'request.args.get("status")' in fn)
check("the report takes a location filter",
      'request.args.get("location")' in fn)
check("the report takes a search filter",
      'request.args.get("q")' in fn)
check("the report takes a sort order",
      'request.args.get("sort")' in fn)
check("the filters are in the URL, so a report can be linked to",
      'method="get" action="/print"' in fn)
check("the sort offers location",
      '"location": _loc_key' in fn)
check("the sort offers status, worst first",
      '"status": _status_key' in fn)
check("the sort offers the device id",
      '"device": lambda d' in fn)
check("the sort offers last-seen",
      '"lastseen": lambda d' in fn)

# --- 3. The controls are hidden on paper.
check("the filter bar is inside a no-print wrapper",
      re.search(r'class="no-print"[^>]*>\{filter_controls\}', fn) is not None)
check("the print stylesheet hides .no-print",
      "@media print { .no-print { display: none; } }" in src)

# --- 4. The note is honest.
check("the note says what was excluded",
      "report-note" in fn and "_excluded_ignored" in fn and "_excluded_by_filter" in fn)
check("the excluded counts cannot go negative",
      "_excluded = len(all_devices) - len(devices)" in fn
      and "_excluded_by_filter = _excluded - _excluded_ignored" in fn)

# --- 5. The location sort puts unlabelled devices last.
check("devices with no location sort last",
      'return ((1, "") if not _loc else (0, _loc)' in fn)

# --- 6. The report header counts what is IN the report.
check("the header counts the devices in the report",
      "Devices in this report" in fn)
check("the header no longer claims all devices are monitored",
      "Devices monitored" not in fn)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
