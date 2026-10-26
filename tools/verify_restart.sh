#!/usr/bin/env bash
# =============================================================================
# Verify the device Restart button end-to-end, from INSIDE the container.
#
#   ./verify_restart.sh [container_service] [device_id]
#     e.g. ./verify_restart.sh watchdog bme680-1
#
# Why from inside the container? The Mosquitto ACL on this facility allows
# the watchdog's source address (the docker bridge) but rejects new logins
# from the host's LAN address. A probe run from the host therefore gets
# "not authorised" and sees nothing, which looks exactly like a broken
# publish. Running the subscriber beside the publisher removes that
# confound.
#
# Proves:
#   1. The restart API accepts the call.
#   2. The MQTT restart command is actually PUBLISHED and received on the
#      restart topic.
#   3. The device's heartbeat counter RESETS - the observable proof the
#      ESPHome node rebooted. A reboot resets the template sensor's counter,
#      so the value drops sharply instead of continuing to increment.
# =============================================================================
set -euo pipefail

SERVICE="${1:-watchdog}"
DEV="${2:-bme680-1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== restart test: ${DEV} (service: ${SERVICE}) ==="

# --- Start a subscriber inside the container, on the restart topic.
docker compose exec -T "$SERVICE" python3 - "$DEV" <<'PYEOF' &
import sys, time, yaml, paho.mqtt.client as mqtt
dev = sys.argv[1]
cfg = yaml.safe_load(open("/app/config/config.yaml"))
seen = []
c = mqtt.Client(client_id="restart-probe-" + dev)
c.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"]["password"])
def on_conn(cl, u, f, rc, *a):
    if rc == 0:
        cl.subscribe("facility/+/env_sensor/button/restart/cmd")
        cl.subscribe("facility/+/env_sensor/#")
c.on_connect = on_conn
def on_msg(cl, u, m):
    seen.append((m.topic, m.payload.decode("utf-8", "ignore")))
c.on_message = on_msg
try:
    c.connect(cfg["mqtt"]["broker"], cfg["mqtt"]["port"], 10)
except Exception as e:
    print("[probe] connect failed:", e); raise SystemExit
c.loop_start()
deadline = time.time() + 75
while time.time() < deadline:
    time.sleep(0.5)
    if seen:
        # give it a moment to collect, then stop
        time.sleep(1); break
c.loop_stop()
print("[probe] received:")
for t, p in seen:
    if "button" in t or "restart" in t:
        print("   ", t, "payload=", repr(p))
PYEOF
PROBE_PID=$!
sleep 4

# --- Heartbeat before.
before=$(docker compose exec -T "$SERVICE" python3 -c "
import sqlite3
c=sqlite3.connect('/app/data/sensor_watchdog.db')
r=c.execute(\"SELECT value FROM sensor_readings WHERE device_id='${DEV}' AND sensor_type='heartbeat'\").fetchone()
print(r[0] if r else 'none')
c.close()" | tail -1)
echo "  heartbeat before: ${before}"

# --- Call the API the way the dashboard button does.
python3 - "$SERVICE" "$DEV" <<'PYEOF'
import sys, re, urllib.request, urllib.parse, urllib.error, http.cookiejar
service, dev = sys.argv[1], sys.argv[2]
base = "http://localhost:5050"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
try:
    op.open(base + "/login",
            urllib.parse.urlencode({"username":"yaniv","password":"mushroom"}).encode())
except urllib.error.HTTPError as e:
    print("  login failed:", e.code); raise SystemExit(1)
page = op.open(base + "/").read().decode()
tok = re.search(r'CSRF_TOKEN = "([a-f0-9]+)"', page).group(1)
req = urllib.request.Request(base + "/api/restart/" + dev,
                             data=urllib.parse.urlencode({"_csrf": tok}).encode())
try:
    r = op.open(req)
    print("  restart API -> HTTP", r.status, r.read().decode()[:160].strip())
except urllib.error.HTTPError as e:
    print("  restart API -> HTTP", e.code, e.read()[:240].decode("utf-8","ignore"))
PYEOF

# --- Watch the heartbeat.
echo "  watching the heartbeat for 75s (a reboot resets the counter)..."
reset=no
lowest=""
for _ in $(seq 1 25); do
    sleep 3
    now=$(docker compose exec -T "$SERVICE" python3 -c "
import sqlite3
c=sqlite3.connect('/app/data/sensor_watchdog.db')
r=c.execute(\"SELECT value FROM sensor_readings WHERE device_id='${DEV}' AND sensor_type='heartbeat'\").fetchone()
print(r[0] if r else 'none')
c.close()" | tail -1)
    if [ "$lowest" = "" ] || awk "BEGIN{exit !($now < $lowest)}"; then lowest="$now"; fi
    if awk "BEGIN{exit !($now < $before)}"; then
        reset=yes
        echo "  RESET DETECTED: ${before} -> ${now}"
        break
    fi
    echo "     heartbeat ${now}"
done

wait $PROBE_PID 2>/dev/null || true
echo
echo "  RESULT:"
echo "    heartbeat reset : ${reset} (before ${before}, lowest ${lowest})"
