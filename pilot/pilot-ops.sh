#!/usr/bin/env bash
# =============================================================================
# MQTT Sensor Watchdog — PILOT instance operator script
# =============================================================================
# A second deployment of the same image, used to evaluate the app on the
# facility big-screen TV before the production rollout.
#
#   ./pilot-ops.sh start | stop | restart | logs | status | backup | shell
#
# The pilot is fully independent of the live instance:
#   * its own port (5060)
#   * its own database (pilot/data/)
#   * its own config  (pilot/config/)
#   * Telegram OFF by default, so one fault does not page twice
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST_PORT=5060

log()  { printf '\033[1;34m[pilot]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[pilot] WARN\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[pilot] ERR\033[0m  %s\n' "$*" >&2; exit 1; }

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "neither 'docker compose' nor 'docker-compose' is available"
fi

mkdir -p data config backups

if [[ ! -f config/config.yaml ]]; then
    log "no pilot config found - copying the live one"
    cp ../config/config.yaml config/config.yaml 2>/dev/null \
        || cp ../config.yaml config/config.yaml
fi
if [[ ! -f .env ]]; then
    log "no pilot .env found - creating one (Telegram off)"
    cat > .env <<'EOF'
WATCHDOG_TG_ENABLED=false
TZ=Asia/Jerusalem
EOF
fi

cmd_start() {
    log "starting the pilot on port ${HOST_PORT}"
    "${COMPOSE[@]}" up -d --build
    log "wall display:  http://$(hostname -I | awk '{print $1}'):${HOST_PORT}/wallboard"
    log "dashboard:     http://$(hostname -I | awk '{print $1}'):${HOST_PORT}/"
    cmd_status
}

cmd_stop() {
    log "stopping the pilot (pilot/data and pilot/config are kept)"
    "${COMPOSE[@]}" down
}

cmd_restart() { cmd_stop || true; cmd_start; }

cmd_logs() {
    if [[ "${1:-}" == "follow" ]]; then
        "${COMPOSE[@]}" logs -f
    else
        "${COMPOSE[@]}" logs --tail=200
    fi
}

cmd_status() {
    echo
    log "container"
    "${COMPOSE[@]}" ps
    echo
    log "health endpoint"
    local code
    code="$(curl -sS -o /tmp/pilot_health.json -w '%{http_code}' --max-time 3 \
        "http://127.0.0.1:${HOST_PORT}/health" || echo 000)"
    echo "  GET /health -> HTTP ${code}  body: $(cat /tmp/pilot_health.json 2>/dev/null || echo n/a)"
    echo
    log "recent audit entries"
    if docker ps --filter name=sensor_watchdog_pilot --format '{{.Names}}' | grep -q .; then
        "${COMPOSE[@]}" exec -T watchdog-pilot python3 -c "
import sqlite3
c = sqlite3.connect('/app/data/sensor_watchdog.db')
rows = c.execute('SELECT timestamp, username, action, device_id FROM audit_logs ORDER BY id DESC LIMIT 8').fetchall()
for r in rows:
    print(f'  {r[0]}  {(r[1] or \"-\"):10s}  {r[2]:16s}  {r[3] or \"\"}')
if not rows:
    print('  (none yet)')
c.close()" 2>/dev/null || echo "  (could not query)"
    else
        echo "  (container not running)"
    fi
}

cmd_backup() {
    [[ -f data/sensor_watchdog.db ]] || die "no pilot database to back up"
    local ts target
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    target="backups/pilot.${ts}.db"
    log "snapshotting -> ${target}"
    "${COMPOSE[@]}" exec -T watchdog-pilot python3 -c "
import sqlite3
src = sqlite3.connect('/app/data/sensor_watchdog.db')
dst = sqlite3.connect('/app/backups/pilot.${ts}.db')
src.backup(dst)
dst.close(); src.close()" \
      && log "done: $(ls -lh "$target" | awk '{print $5}')" \
      || die "backup failed"
}

cmd_shell() {
    "${COMPOSE[@]}" exec watchdog-pilot /bin/bash
}

cmd="${1:-help}"
case "$cmd" in
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    restart)  cmd_restart ;;
    logs)     cmd_logs "${2:-}" ;;
    status)   cmd_status ;;
    backup)   cmd_backup ;;
    shell)    cmd_shell ;;
    help|--help|-h)
        grep -E '^#   \./pilot-ops\.sh' "$0" | sed 's/^#   //'
        ;;
    *) die "unknown command: $cmd (try: ./pilot-ops.sh help)" ;;
esac
