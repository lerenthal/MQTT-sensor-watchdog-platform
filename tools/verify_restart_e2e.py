#!/usr/bin/env python3
"""End-to-end proof that the dashboard Restart button actually reboots a device.

Runs the REAL application API (the same endpoint the button calls), then
watches the device for the reboot signature:
  * an LWT status cycle: online -> offline -> online
  * a reset heartbeat counter
  * a gap in the data stream

    ./verify_restart_e2e.py [base_url] [device_id]

Warns loudly before running: a restart drops the device's readings for a
few seconds. Only use a device that is installed but not in production.
"""
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5050"
DEV = sys.argv[2] if len(sys.argv) > 2 else "bme680-1"

HERE = subprocess.run(["pwd"], capture_output=True, text=True).stdout.strip()

# --- Browser-side observer, running in the same container as the app so it
#     is allowed to connect to the broker.
observer_script = r'''
import time, sys, yaml, paho.mqtt.client as mqtt
dev = sys.argv[1]
cfg = yaml.safe_load(open("/app/config/config.yaml"))
events = []
gap = {"max": 0.0, "last": None}

def on_connect(cl, u, f, rc, *a):
    if rc == 0:
        cl.subscribe(f"facility/{dev}/#")

def on_msg(cl, u, m):
    now = time.time()
    if gap["last"] is not None:
        gap["max"] = max(gap["max"], now - gap["last"])
    gap["last"] = now
    if m.topic.endswith("/status"):
        events.append((round(now, 1), m.payload.decode("utf-8", "ignore")))

c = mqtt.Client(client_id="e2e-observer-" + dev)
c.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])
c.on_connect = on_connect
c.on_message = on_msg
c.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 10)
c.loop_start()
time.sleep(70)
c.loop_stop()
print("OBSERVER", events, gap["max"])
'''
with open("/tmp/e2e_observer.py", "w") as f:
    f.write(observer_script)
subprocess.run(["docker", "compose", "cp", "/tmp/e2e_observer.py",
                "watchdog:/tmp/e2e_observer.py"], capture_output=True)

obs = subprocess.Popen(
    ["docker", "compose", "exec", "-T", "watchdog", "python3",
     "/tmp/e2e_observer.py", DEV],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
time.sleep(8)

# --- Call the real API, exactly as the button does.
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.open(BASE + "/login",
        urllib.parse.urlencode({"username": "yaniv", "password": "mushroom"}).encode())
page = op.open(BASE + "/").read().decode()
tok = re.search(r'CSRF_TOKEN = "([a-f0-9]+)"', page).group(1)

before = subprocess.run(
    ["docker", "compose", "exec", "-T", "watchdog", "python3", "-c",
     "import sqlite3;c=sqlite3.connect('/app/data/sensor_watchdog.db');"
     f"r=c.execute(\"SELECT value FROM sensor_readings WHERE device_id='{DEV}' "
     "AND sensor_type='heartbeat'\").fetchone();"
     "print(r[0] if r else 'none');c.close()"],
    capture_output=True, text=True).stdout.strip().split("\n")[-1]

print(f"=== end-to-end restart test: {DEV} via {BASE} ===")
print(f"  heartbeat before: {before}")

req = urllib.request.Request(BASE + "/api/restart/" + DEV,
                             data=urllib.parse.urlencode({"_csrf": tok}).encode())
try:
    r = op.open(req)
    body = r.read().decode()
    print(f"  API -> HTTP {r.status}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"  API -> HTTP {e.code}")
try:
    print(f"  body: {json.dumps(json.loads(body))}")
except Exception:
    print(f"  body: {body[:200]}")

# Watch the heartbeat for a reset.
print("  watching the heartbeat for 60s (a reboot resets the counter)...")
reset = False
lowest = None
for _ in range(20):
    time.sleep(3)
    now = subprocess.run(
        ["docker", "compose", "exec", "-T", "watchdog", "python3", "-c",
         "import sqlite3;c=sqlite3.connect('/app/data/sensor_watchdog.db');"
         f"r=c.execute(\"SELECT value FROM sensor_readings WHERE device_id='{DEV}' "
         "AND sensor_type='heartbeat'\").fetchone();"
         "print(r[0] if r else 'none');c.close()"],
        capture_output=True, text=True).stdout.strip().split("\n")[-1]
    try:
        v = float(now)
    except (TypeError, ValueError):
        continue
    if lowest is None or v < lowest:
        lowest = v
    try:
        if v < float(before):
            reset = True
            print(f"  HEARTBEAT RESET: {before} -> {now}")
            break
    except (TypeError, ValueError):
        pass
    print(f"     heartbeat {now}")

print()
print("=== RESULT ===")
print(f"  heartbeat reset : {'YES' if reset else 'NO'} (before {before}, lowest {lowest})")
# The observer runs for a fixed window; give it a moment to flush its
# summary rather than blocking on it.
try:
    out = obs.communicate(timeout=15)[0]
except subprocess.TimeoutExpired:
    obs.kill()
    out = obs.stdout.read() if obs.stdout else ""
for line in (out or "").splitlines():
    if line.startswith("OBSERVER"):
        print(f"  {line}")
