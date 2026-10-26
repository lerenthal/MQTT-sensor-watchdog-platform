#!/usr/bin/env python3
"""Test a device's REAL restart command topic, discovered from MQTT discovery.

The watchdog used to publish to a hardcoded
    <prefix>/button/restart/cmd
but ESPHome's MQTT discovery shows each device's actual command topic is
    <prefix>/button/<entity_name>/command

This script reads the real topic from the retained discovery config, then
publishes to it and watches for the reboot (LWT status cycle + a reset
heartbeat counter).

    docker compose cp tools/probe_restart.py watchdog:/tmp/pr.py
    docker compose exec -T watchdog python3 /tmp/pr.py bme680-1
"""
import json
import sys
import time

import yaml
import paho.mqtt.client as mqtt

DEV = sys.argv[1] if len(sys.argv) > 1 else "bme680-1"
cfg = yaml.safe_load(open("/app/config/config.yaml"))

discovered = {}


def on_connect(cl, u, f, rc, *a):
    if rc == 0:
        cl.subscribe("homeassistant/button/#")


def on_msg(cl, u, m):
    try:
        d = json.loads(m.payload)
    except Exception:
        return
    if d.get("dev_cla") == "restart":
        discovered[d.get("name", "")] = d.get("cmd_t", "")


c = mqtt.Client(client_id="probe-restart")
c.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])
c.on_connect = on_connect
c.on_message = on_msg
c.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 10)
c.loop_start()
time.sleep(8)
c.loop_stop()

# The device's own restart button.
target = None
for name, cmd in discovered.items():
    if "restart" in name.lower() and "safe mode" not in name.lower():
        if f"/{DEV}/" in cmd or cmd.startswith(f"facility/{DEV}/"):
            target = (name, cmd)
            break

print(f"=== restart probe: {DEV} ===")
if not target:
    print("  no restart button found in discovery for this device")
    raise SystemExit(1)

name, cmd = target
print(f"  button   : {name}")
print(f"  cmd topic: {cmd}")
print()

# Watch the device's own status + heartbeat while we fire the command.
status_seen = []
gap = {"max": 0.0, "last": None}


def on_connect2(cl, u, f, rc, *a):
    if rc == 0:
        cl.subscribe(f"facility/{DEV}/#")


def on_msg2(cl, u, m):
    now = time.time()
    if gap["last"] is not None:
        gap["max"] = max(gap["max"], now - gap["last"])
    gap["last"] = now
    if m.topic.endswith("/status"):
        status_seen.append((round(now, 1), m.payload.decode("utf-8", "ignore")))


c2 = mqtt.Client(client_id="probe-watch")
c2.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])
c2.on_connect = on_connect2
c2.on_message = on_msg2
c2.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 10)
c2.loop_start()
time.sleep(6)

print(f"  publishing PRESS -> {cmd}")
info = c2.publish(cmd, "PRESS", qos=1, retain=False)
info.wait_for_publish(timeout=5)
print(f"  publish acked: {info.is_published()}")
print()
print("  watching 75s for a reboot (LWT offline -> online) ...")
t0 = time.time()
last_status = None
while time.time() - t0 < 75:
    time.sleep(2)
    if status_seen:
        ts, val = status_seen[-1]
        if val != last_status:
            print(f"    STATUS {val!r} at t+{ts - time.time():.1f}s")
            last_status = val

c2.loop_stop()

offline_cycle = any(v == "offline" for _, v in status_seen)
print()
print("  RESULT")
print(f"    status transitions : {status_seen if status_seen else 'none'}")
print(f"    reboot detected    : {'YES' if offline_cycle else 'NO'}")
print(f"    longest data gap   : {gap['max']:.1f}s")
