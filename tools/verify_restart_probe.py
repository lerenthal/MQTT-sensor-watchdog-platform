#!/usr/bin/env python3
"""Definitive restart test: runs INSIDE the watchdog container.

Three questions, one run:

  1. Does the published command actually appear on the broker?
     (a subscriber inside the container watches the restart topic)
  2. Does the device reboot? A reboot produces an MQTT LWT cycle:
     the retained status topic goes "offline" then back to "online".
     We watch that topic - it is the only ground truth.
  3. Is the device's data interrupted? We watch every topic under the
     device and record the longest gap between consecutive messages.

Run:  docker compose exec -T watchdog python3 verify_restart_probe.py <device>
"""
import sys
import time

import yaml
import paho.mqtt.client as mqtt

DEV = sys.argv[1] if len(sys.argv) > 1 else "bme680-1"
cfg = yaml.safe_load(open("/app/config/config.yaml"))

BASE = f"facility/{DEV}/env_sensor"
RESTART_TOPIC = f"{BASE}/button/restart/cmd"

status_seen = []
messages = []
last_msg_at = None
max_gap = 0.0

def on_msg(c, u, m):
    global last_msg_at, max_gap
    now = time.time()
    if last_msg_at is not None:
        max_gap = max(max_gap, now - last_msg_at)
    last_msg_at = now
    messages.append((m.topic, m.payload[:40].decode("utf-8", "ignore"), now))
    if m.topic.endswith("/status"):
        status_seen.append((round(now, 1), m.payload.decode("utf-8", "ignore")))

c = mqtt.Client(client_id="restart-probe-" + DEV)
c.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])

def on_connect(cl, u, f, rc, *a):
    print(f"  probe connected rc={rc}")
    if rc == 0:
        cl.subscribe(f"{BASE}/#")
        cl.subscribe("facility/+/env_sensor/button/restart/cmd")

c.on_connect = on_connect
c.on_message = on_msg

print(f"=== restart probe: {DEV} ===")
print(f"  subscribing to {BASE}/#")
c.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 10)
c.loop_start()
time.sleep(8)

# ---- Publish the restart command the way the watchdog does.
print(f"  publishing PRESS -> {RESTART_TOPIC}")
info = c.publish(RESTART_TOPIC, "PRESS", qos=1, retain=False)
info.wait_for_publish(timeout=5)
print(f"  publish acked: {info.is_published()}")

# ---- Watch for a reboot signature.
print("  watching for 90s: LWT status transitions and data gaps ...")
t0 = time.time()
last_status = None
while time.time() - t0 < 90:
    time.sleep(2)
    if status_seen:
        last = status_seen[-1]
        if last[1] != last_status:
            print(f"    STATUS {last[1]!r} at t+{last[0]-time.time():.1f}s")
            last_status = last[1]

c.loop_stop()

print()
print("  RESULT")
print(f"    command seen on the broker : "
      f"{'YES' if any('button' in m[0] for m in messages) else 'NO'}")
print(f"    LWT status transitions    : {status_seen if status_seen else 'NONE (no reboot)'}")
print(f"    longest data gap          : {max_gap:.1f}s")
print(f"    messages observed         : {len(messages)}")
if messages:
    print("    topics seen (sample):")
    for t in sorted({m[0] for m in messages})[:14]:
        print("      ", t)
