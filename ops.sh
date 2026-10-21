#!/usr/bin/env bash
# =============================================================================
# Mushroom Facility Sensor Watchdog — operator entry point
# =============================================================================
# One command for everything: start, stop, restart, logs, status, backup,
# restore, rebuild, validate, shell-in.
#
# Requires either `docker compose` (V2 plugin, recommended) or legacy
# `docker-compose` (V1, deprecated). Detected automatically.
#
# Usage:
#   ./ops.sh start          # build (if needed) + start detached
#   ./ops.sh stop           # stop, keep DB & config on host
#   ./ops.sh restart        # stop + start
#   ./ops.sh logs           # last 200 lines, exits (use 'follow' for live tail)
#   ./ops.sh status         # container health, mqtt state, recent alerts
#   ./ops.sh backup         # snapshot the DB to ./backups/
#   ./ops.sh restore FILE   # restore a DB from ./backups/FILE (interactive)
#   ./ops.sh shell          # /bin/bash inside the running container
#   ./ops.sh validate       # sanity-check config.yaml + .env
#   ./ops.sh rebuild        # stop + force-rebuild image + start
#   ./ops.sh update         # git pull, rebuild, restart
#   ./ops.sh reset          # DESTRUCTIVE: stop, wipe DB & config, recreate
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- compose command detection ----------------------------------------------
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
    echo "WARNING: using legacy docker-compose (V1). Install the docker compose V2 plugin." >&2
else
    echo "ERROR: neither 'docker compose' nor 'docker-compose' is available." >&2
    echo "Install Docker Engine + the compose plugin." >&2
    exit 1
fi

# --- helpers -----------------------------------------------------------------
log()  { printf '\033[1;34m[%(%H:%M:%S)T] %s\033[0m\n' -1 "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERR\033[0m  %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

need docker

ensure_dirs() {
    [[ -d data ]]    || mkdir -p data
    [[ -d config ]]  || mkdir -p config
    [[ -d backups ]] || mkdir -p backups
}

ensure_dirs

# First-boot: drop a config.yaml in ./config/ if none exists, so the
# container doesn't crash looking for /app/config/config.yaml.
if [[ ! -f config/config.yaml ]]; then
    log "no config/config.yaml found — bootstrapping a copy"
    cp config.yaml config/config.yaml
fi

# --- subcommands -------------------------------------------------------------
cmd_start() {
    ensure_dirs
    log "building (if needed) and starting"
    "${COMPOSE[@]}" up -d --build
    cmd_status
}

cmd_stop() {
    log "stopping (keeps DB & config on host)"
    "${COMPOSE[@]}" down
}

cmd_restart() {
    cmd_stop || true
    cmd_start
}

cmd_rebuild() {
    log "rebuilding image from scratch"
    "${COMPOSE[@]}" build --no-cache
    "${COMPOSE[@]}" up -d
    cmd_status
}

cmd_logs() {
    # docker compose logs -f is a streaming follow. To avoid blocking the
    # terminal forever, default to "last 200 lines and exit"; pass "follow"
    # as an argument for the live tail behaviour.
    if [[ "${1:-}" == "follow" ]]; then
        "${COMPOSE[@]}" logs -f
    else
        "${COMPOSE[@]}" logs --tail=200
    fi
}

cmd_shell() {
    if ! "${COMPOSE[@]}" ps --services --filter "status=running" 2>/dev/null | grep -q watchdog; then
        die "watchdog is not running. Start it first: ./ops.sh start"
    fi
    "${COMPOSE[@]}" exec watchdog /bin/bash
}

cmd_status() {
    echo
    log "container status"
    "${COMPOSE[@]}" ps
    echo
    log "image / image status"
    docker ps -a --filter "name=sensor_watchdog" \
        --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" || true
    echo
    log "app health endpoint"
    local code
    code="$(curl -sS -o /tmp/health.json -w '%{http_code}' \
        --max-time 3 "http://127.0.0.1:5050/health" || echo "000")"
    echo "  GET /health -> HTTP ${code}  body: $(cat /tmp/health.json 2>/dev/null || echo 'n/a')"
    echo
    log "recent audit entries"
    # Read the DB from inside the container so we don't fight a host-side
    # file lock when the watchdog has WAL mode enabled.
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q watchdog; then
        docker compose exec -T watchdog python3 -c "
import sqlite3
c = sqlite3.connect('/app/data/sensor_watchdog.db')
rows = c.execute(
    'SELECT timestamp, username, action, device_id, substr(details,1,40) '
    'FROM audit_logs ORDER BY id DESC LIMIT 8').fetchall()
if not rows:
    print('  (none yet)')
else:
    for r in rows:
        print(f'  {r[0]}  {(r[1] or \"-\"):8s}  {r[2]:14s}  {(r[3] or \"-\"):14s}  {r[4] or \"\"}')
c.close()
" 2>&1 | sed 's/^/  /' || echo "  (unable to query DB from inside container)"
    else
        echo "  (container not running — start it first)"
    fi
}

cmd_validate() {
    log "validating config.yaml"
    # Prefer the project venv's python3 if present (it has yaml installed).
    local py
    if [[ -x .venv/bin/python ]]; then
        py=".venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        py="python3"
    else
        warn "no python available; skipping parse check"
        py=""
    fi
    if [[ -n "$py" ]]; then
        "$py" -c "
import yaml, sys
cfg = yaml.safe_load(open('config/config.yaml'))
required_top = ('mqtt','telegram','watchdog','web')
for k in required_top:
    assert k in cfg, f'missing top-level key: {k}'
for k in ('broker','port','topic_subscription'):
    assert k in cfg['mqtt'], f'missing mqtt.{k}'
for k in ('enabled','bot_token','chat_id'):
    assert k in cfg['telegram'], f'missing telegram.{k}'
for k in ('check_interval_seconds','default_heartbeat_timeout_seconds','default_offline_timeout_seconds'):
    assert k in cfg['watchdog'], f'missing watchdog.{k}'
users = cfg.get('auth', {}).get('users', {})
if users:
    # If any user still has plaintext password, it's fine — _ensure_password_hashes
    # upgrades it on first start. The point is to confirm config structure.
    print('  auth users       :', list(users.keys()))
print('  YAML parsed OK')
print(f\"  mqtt.broker        = {cfg['mqtt']['broker']}\")
print(f\"  mqtt.port          = {cfg['mqtt']['port']}\")
print(f\"  telegram.enabled   = {cfg['telegram']['enabled']}\")
print(f\"  web.port           = {cfg['web']['port']}\")
print(f\"  watchdog check     = {cfg['watchdog']['check_interval_seconds']}s\")
" || die "config validation failed"
    fi
    log "validating .env (if present)"
    if [[ -f .env ]]; then
        # Strip comments / blank lines, count Watches
        local n
        n=$(grep -cv '^\s*\(#\|$\)' .env)
        log ".env present, ${n} active line(s)"
    else
        log ".env not present (optional)"
    fi
    log "validating docker-compose.yml"
    # `docker compose config` returns nonzero if .env is referenced but missing.
    # Create an empty .env on demand so the validate step stays idempotent;
    # we don't overwrite anything if a real .env exists.
    if [[ ! -f .env ]]; then
        touch .env
    fi
    "${COMPOSE[@]}" config -q > /dev/null || die "compose file invalid"
    log "compose file valid"
}

cmd_backup() {
    [[ -f data/sensor_watchdog.db ]] || die "no DB at data/sensor_watchdog.db — nothing to back up"
    local stamp db ts target
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    db="sensor_watchdog.${ts}.db"
    target="backups/${db}"
    log "snapshotting DB -> ${target}"
    # Online-safe copy: VACUUM INTO is a SQLite feature that writes a consistent
    # snapshot even while writers are active. Requires sqlite3 3.27+ (we have it).
    sqlite3 data/sensor_watchdog.db ".timeout 5000" \
        "VACUUM INTO '${target}.tmp';" \
        && mv "${target}.tmp" "$target"
    log "done: $(ls -lh "$target" | awk '{print $5, $9}')"
    log "to restore: ./ops.sh restore backups/${db}"
}

cmd_restore() {
    local file="${1:-}"
    [[ -n "$file" ]] || die "usage: ./ops.sh restore <file>"
    [[ -f "$file" ]] || die "no such backup: $file"
    warn "this will REPLACE data/sensor_watchdog.db after stopping the container"
    warn "and will snapshot the current DB first"
    read -rp "Type YES to continue: " ans
    [[ "$ans" == "YES" ]] || die "aborted"
    cmd_stop || true
    if [[ -f data/sensor_watchdog.db ]]; then
        local snap="backups/pre-restore.$(date -u +%Y%m%dT%H%M%SZ).db"
        cp -v data/sensor_watchdog.db "$snap" || true
    fi
    cp -v "$file" data/sensor_watchdog.db
    cmd_start
}

cmd_update() {
    log "git pull + rebuild + restart"
    [[ -d .git ]] || die "not a git repo; nothing to update from"
    git pull --ff-only
    cmd_rebuild
}

cmd_reset() {
    warn "this will STOP the container, DELETE ./data and ./config,"
    warn "then recreate empty dirs. ALL DEVICES, AUDIT LOGS AND CONFIG ARE LOST."
    read -rp "Type RESET to continue: " ans
    [[ "$ans" == "RESET" ]] || die "aborted"
    "${COMPOSE[@]}" down -v || true
    rm -rf data config backups
    ensure_dirs
    cp config.yaml config/config.yaml
    cmd_start
}

# --- dispatch ----------------------------------------------------------------
cmd="${1:-help}"
shift || true

case "$cmd" in
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    restart)  cmd_restart ;;
    logs)     cmd_logs ;;
    status)   cmd_status ;;
    backup)   cmd_backup ;;
    restore)  cmd_restore "$@" ;;
    shell)    cmd_shell ;;
    validate) cmd_validate ;;
    rebuild)  cmd_rebuild ;;
    update)   cmd_update ;;
    reset)    cmd_reset ;;
    help|--help|-h)
        grep -E '^#   \./ops\.sh' "$0" | sed 's/^#   //'
        ;;
    *)
        die "unknown command: $cmd (try: ./ops.sh help)"
        ;;
esac
