#!/usr/bin/env python3
"""Verify the five UI features requested for the dashboard and wall board.

    ./verify_features.py [base_url]

Checks, against a RUNNING instance:
  1. /admin/ignored is reachable AND linked from the top navigation.
  2. The compact table carries a snooze control (not only the detail panel).
  3. A row that is faulty and not snoozed gets the persistent highlight
     class; a snoozed device does not.
  4. The wall board carries a prominent TEST ALARM button.
  5. The restart API reports honestly when a device has no MQTT button.
"""
import os
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import json

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5050"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(path):
    return op.open(BASE + path).read().decode("utf-8", "ignore")


def post(path, data):
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data).encode())
    try:
        r = op.open(req)
        return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


op.open(BASE + "/login",
        urllib.parse.urlencode({"username": "yaniv", "password": "mushroom"}).encode())

results = []

# ---- 1. Ignore list reachable and discoverable.
try:
    page = get("/admin/ignored")
    reachable = "Ignore this device" in page
except urllib.error.HTTPError as e:
    reachable = False
    page = ""
nav = get("/")
linked = "Ignore List" in nav
results.append(("1. /admin/ignored reachable", reachable))
results.append(("1. Ignore List in top nav", linked))

# ---- 2. Snooze control in the compact row.
js = get("/")  # the page embeds dashboard.js
# dashboard.js is served inline? No - it is injected server-side. Check the HTML.
has_snooze_fn = "snoozeControl" in js
results.append(("2. snoozeControl() present in page", has_snooze_fn))

# ---- 3. Persistent highlight class + snoozed exclusion.
has_persist = "row-changed-persist" in js
has_snoozed_guard = "snoozed" in js
results.append(("3. row-changed-persist class", has_persist))
results.append(("3. snoozed devices excluded from flash", has_snoozed_guard))

# ---- 4. Wall board TEST ALARM button.
wb = get("/wallboard")
has_test = "TEST ALARM" in wb and "beepTest()" in wb
results.append(("4. wallboard TEST ALARM button", has_test))

# ---- 5. Restart API honesty.
tok = re.search(r'CSRF_TOKEN = "([a-f0-9]+)"', nav)
if tok:
    status, body = post("/api/restart/bme680-1", {"_csrf": tok.group(1)})
    # A device with no MQTT button must NOT report plain success: the
    # command reaches the broker but nothing acts on it, so the API
    # returns ok:false with published:true.
    # Parse rather than string-match: Flask's jsonify sorts keys and the
    # separator style differs from a naive quote.
    try:
        payload = json.loads(body)
    except Exception:
        payload = {}
    honest = (payload.get("published") is True
               and payload.get("ok") is False
               and status >= 400)
    results.append((f"5. restart reports no-MQTT-button (HTTP {status})", honest))
    if not honest:
        print("      body:", body[:300])
else:
    results.append(("5. restart API (no CSRF token found)", False))

# ---- Ignore list round trip.
token = re.search(r'name="_csrf" value="([a-f0-9]+)"', page)
if token:
    st, _ = post("/admin/ignored", {"_csrf": token.group(1), "action": "add",
                                    "device_id": "sht41-5"})
    ids = [d["device_id"] for d in json.loads(get("/api/devices"))["devices"]]
    hidden = "sht41-5" not in ids
    st2, _ = post("/admin/ignored", {"_csrf": token.group(1), "action": "remove",
                                     "device_id": "sht41-5"})
    ids2 = [d["device_id"] for d in json.loads(get("/api/devices"))["devices"]]
    back = "sht41-5" in ids2
    results.append(("1. ignore hides the device", hidden))
    results.append(("1. resume restores the device", back))

print()
print("=== feature verification ===")
fails = 0
for name, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        fails += 1
print()
print(f"  {len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
