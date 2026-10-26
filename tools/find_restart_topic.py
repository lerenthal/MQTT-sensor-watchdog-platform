#!/usr/bin/env python3
"""Find the topic an ESPHome restart button actually listens on.

The watchdog publishes to a hardcoded
    <prefix>/button/restart/cmd
but a device whose firmware has no MQTT button (or uses a different name)
will never act on it. This probes candidate topics and watches the
device's OWN boot_count / uptime, which are the ground truth for a
reboot.

Usage (inside the container):
    python3 find_restart_topic.py <device_id> [prefix]
"""
import sys
import time

import yaml
import paho.mqtt.client as mqtt

DEV = sys.argv[1] if len(sys.argv) > 1 else "bme680-1"
PREFIX = sys.argv[2] if len(sys.argv) > 2 else f"facility/{DEV}/env_sensor"

cfg = yaml.safe_load(open("/app/config/config.yaml"))

state = {}
boot_events = []


def on_msg(c, u, m):
    t = m.topic
    p = m.payload.decode("utf-8", "ignore")
    if t.endswith("_boot_count/state"):
        state["boot_count"] = p
        boot_events.append((round(time.time(), 1), "boot_count", p))
    elif t.endswith("_uptime/state"):
        state["uptime"] = p
    elif t.endswith("/status"):
        state["status"] = p
        boot_events.append((round(time.time(), 1), "status", p))


c = mqtt.Client(client_id="find-restart-topic")
c.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])


def on_connect(cl, u, f, rc, *a):
    if rc == 0:
        cl.subscribe(f"{PREFIX}/#")
        cl.subscribe("facility/+/env_sensor/#")


c.on_connect = on_connect
c.on_message = on_msg
c.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 10)
c.loop_start()
time.sleep(8)

print(f"=== finding the restart topic for {DEV} ===")
print(f"  prefix: {PREFIX}")
print(f"  baseline: boot_count={state.get('boot_count')} uptime={state.get('uptime')}")
base_boot = state.get("boot_count")

# Candidates: (topic, payload). Ordered by likelihood.
CANDIDATES = [
    (f"{PREFIX}/button/restart/cmd",                    "PRESS"),
    (f"{PREFIX}/button/{DEV}_restart/cmd",              "PRESS"),
    (f"{PREFIX}/button/{DEV.replace('-', '_')}_restart/cmd", "PRESS"),
    (f"{PREFIX}/button/{DEV}_Restart/cmd",              "PRESS"),
    (f"{PREFIX}/button/restart/cmd",                    "ON"),
    (f"{PREFIX}/button/{DEV}_restart/cmd",              "ON"),
    (f"{PREFIX}/restart/cmd",                           "PRESS"),
    (f"{PREFIX}/button/reset/cmd",                      "PRESS"),
]

for topic, payload in CANDIDATES:
    before_boot = state.get("boot_count")
    before_uptime = state.get("uptime")
    n_before = len(boot_events)
    print(f"\n  trying: {topic}  payload={payload!r}")
    info = c.publish(topic, payload, qos=1, retain=False)
    info.wait_for_publish(timeout=5)
    time.sleep(18)
    after_boot = state.get("boot_count")
    after_uptime = state.get("uptime")
    new_events = boot_events[n_before:]
    rebooted = (before_boot is not None and after_boot is not None
                and after_boot != before_boot)
    went_offline = any(e[1] == "status" and e[2] == "offline" for e in new_events)
    print(f"    boot_count {before_boot} -> {after_boot}   uptime {before_uptime} -> {after_uptime}")
    if new_events:
        print(f"    events: {[(e[1], e[2]) for e in new_events]}")
    if rebooted or went_offline:
        print(f"\n  *** WORKING TOPIC: {topic}  payload={payload!r} ***")
        if rebooted:
            print(f"      boot_count {before_boot} -> {after_boot}")
        if went_offline:
            print("      device went offline (LWT) = it rebooted")
        break
else:
    print("\n  no candidate rebooted the device.")

c.loop_stop()
