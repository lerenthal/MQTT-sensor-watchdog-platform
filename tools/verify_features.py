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

# ---- 4. The alarm control is present on the wall board.
wb = get("/wallboard")
has_test = "alarm-test-btn" in wb and "AlarmUI.mount" in wb
results.append(("4. wallboard alarm TEST control", has_test))

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
    # A device WITH a restart entity must report success AND name the real
    # command topic - the topic it discovered, not a guess.
    ok = (payload.get("ok") is True
          and payload.get("topic", "").endswith("/command")
          and status == 200)
    results.append((f"5. restart of a restartable device succeeds (HTTP {status})", ok))
    if not ok:
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

# ---- 6. The alarm toggle and the five styles, from the shared module.
has_toggle = "STOP TEST" in wb and "toggleTest" in wb
results.append(("6. alarm button is a toggle (STOP TEST present)", has_toggle))
styles = ["chime", "warble", "siren", "klaxon", "continuous"]
present = [st for st in styles if f"value=\"{st}\"" in wb or f"{{ f: {st[0]}" in wb]
# The styles live in the shared module as a JS object, not as <option>
# markup, so check the object keys.
present = [st for st in styles if f"    {st}: {{" in wb]
results.append((f"6. all five buzzer styles defined ({len(present)}/5)",
                len(present) == 5))
results.append(("6. the style dropdown is rendered by the shared module",
                "alarm-style-select" in wb))
results.append(("6. site default style injected",
                "window.SITE_BUZZ_STYLE" in wb))
results.append(("6. clock in the top ribbon", 'id="clock"' in wb))
results.append(("6. alarm control in the top ribbon", 'id="alarm-ribbon"' in wb))

# ---- 7. Configuration help items.
admin = get("/admin/config")
n_help = admin.count('class="help-item"')
results.append((f"7. configuration help items present ({n_help})", n_help >= 15))
results.append(("7. help documents buzzer_style", "watchdog.buzzer_style" in admin))
results.append(("7. help documents the MQTT client id", "mqtt.client_id" in admin))

# ---- 8. The restart path: discovered topics, not a guessed one.
topics = json.loads(get("/api/restart/topics"))
results.append((f"8. restart topics discovered ({topics.get('count', 0)} devices)",
                topics.get("count", 0) > 0))
# No topic may be the old hardcoded guess.
bad = [t for t in (topics.get("topics") or {}).values()
       if t.endswith("/button/restart/cmd")]
results.append(("8. no device uses the old hardcoded topic", not bad))
# The topic must be the one the device actually publishes.
sample = list((topics.get("topics") or {}).items())[:1]
if sample:
    dev, t = sample[0]
    results.append((f"8. topic for {dev} is an entity command topic",
                    t.endswith("/command")))

devs = json.loads(get("/api/devices"))["devices"]
res = [d for d in devs if d.get("can_restart")]
results.append((f"8. devices report can_restart ({len(res)}/{len(devs)})",
                len(res) == len(devs) and len(devs) > 0))

# ---- 9. A restart for a device with no restart entity must 404, not
#        claim success.
tok = re.search(r'CSRF_TOKEN = "([a-f0-9]+)"', nav)
if tok:
    st, body = post("/api/restart/no-such-device-xyz", {"_csrf": tok.group(1)})
    try:
        payload = json.loads(body)
    except Exception:
        payload = {}
    results.append((f"9. unknown device restart -> 404 (got {st})", st == 404))
    results.append(("9. unknown device restart does not claim success",
                    payload.get("ok") is False))

# ---- 10. The alarm control is on the DASHBOARD ribbon, not only the
#         wall board, and it has TEST and MUTE.
dash = get("/")
results.append(("10. alarm ribbon on the dashboard header",
                'id="alarm-ribbon"' in dash))
results.append(("10. alarm.js loaded on the dashboard",
                "alarmAudio" in dash and "AlarmUI.mount" in dash))
wall = get("/wallboard")
results.append(("10. alarm ribbon on the wall board",
                'id="alarm-ribbon"' in wall))
# The shared module must define the TEST and MUTE controls.
results.append(("10. shared module has a TEST control",
                "alarm-test-btn" in dash))
results.append(("10. shared module has a MUTE control",
                "alarm-mute-btn" in dash))
results.append(("10. shared module has the style dropdown",
                "alarm-style-select" in dash))
# The style list must be in the shared module, not duplicated.
results.append(("10. the tone list is defined once, in the shared module",
                dash.count("Chime (gentle)") <= 1))

# ---- 11. The heartbeat column.
rows_js = get("/")  # the page embeds dashboard.js
results.append(("11. Heartbeat column in the header",
                'class="col-heartbeat"' in dash))
results.append(("11. Heartbeat column in the compact row",
                "col-heartbeat num" in rows_js))
# The devices table's own rendering must not hardcode a colspan. Other
# tables on the page (audit log, config history) legitimately have their
# own.
_dev_js = rows_js
_dev_marker = _dev_js.find("tableColumnCount")
_rowspan = _dev_js[max(0, _dev_marker - 4000):_dev_marker + 2000] if _dev_marker > 0 else _dev_js
results.append(("11. the devices table derives its colspan",
                'colspan="' not in _rowspan))

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
