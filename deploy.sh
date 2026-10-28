#!/usr/bin/env bash
# =============================================================================
# Deploy the PsygaBio Device Watchdog to a production host.
#
#   ./deploy.sh              deploy (build + start)
#   ./deploy.sh status       container and health
#   ./deploy.sh logs         follow the logs
#   ./deploy.sh restart      restart
#   ./deploy.sh rebuild      rebuild the image from scratch and restart
#   ./deploy.sh backup       snapshot the database
#   ./deploy.sh preflight    check the host is ready (run this first)
#   ./deploy.sh rollback     restore the previous image
#
# The stack is SEPARATE from the mosquitto/influxdb/telegraf/grafana
# stack: this script only ever touches the watchdog.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${HERE}/docker-compose.production.yml"
ENV_FILE="${HERE}/deploy/.env"
CONFIG_DIR="${HERE}/deploy/config"
DATA_DIR="${HERE}/deploy/data"
BACKUP_DIR="${HERE}/deploy/backups"

# --env-file points at deploy/.env: that is where the secrets live, and
# the project root's .env belongs to the development instance.
# Docker may need sudo. On the production host the operator is in the
# sudo group but NOT the docker group, so the plain command is not
# permitted. Detect it once and use sudo for the whole script.
if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
    DOCKER_SUDO=""
elif sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
    DOCKER_SUDO="(via sudo)"
else
    DOCKER=(docker)
    DOCKER_SUDO=""
fi

COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE" --project-directory "$HERE" \
         --env-file "$ENV_FILE")

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[1;32m  OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  XX\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# preflight - is this host ready?
# -----------------------------------------------------------------------------
cmd_preflight() {
    log "Preflight for the watchdog on $(hostname)"
    local problems=0

    printf '\n  Docker\n'
    if docker version >/dev/null 2>&1; then
        ok "docker is present and working"
    elif sudo -n docker version >/dev/null 2>&1; then
        ok "docker works via sudo ${DOCKER_SUDO}"
    else
        warn "docker is NOT working - the watchdog cannot run"
        warn "  the operator needs to be in the docker group, or have a"
        warn "  NOPASSWD sudo rule for docker"
        problems=$((problems+1))
    fi
    if docker compose version >/dev/null 2>&1; then
        ok "docker compose (v2) is available"
    else
        warn "docker compose v2 is not available"
        problems=$((problems+1))
    fi
    if [ "$(id -u)" -eq 0 ]; then
        ok "running as root - no sudo needed"
    elif docker info >/dev/null 2>&1; then
        ok "the user is in the docker group"
    elif sudo -n docker info >/dev/null 2>&1; then
        ok "not in the docker group, but sudo works for docker"
    else
        warn "not root, not in the docker group, and no passwordless sudo"
        problems=$((problems+1))
    fi

    printf '\n  The MQTT broker (the existing stack on this host)\n'
    local broker="10.10.17.11"
    if timeout 3 bash -c "exec 3<>/dev/tcp/${broker}/1883" 2>/dev/null; then
        ok "the broker at ${broker}:1883 is reachable"
    else
        warn "the broker at ${broker}:1883 is NOT reachable"
        problems=$((problems+1))
    fi

    printf '\n  The port the watchdog will use\n'
    local port="${WATCHDOG_PORT:-5050}"
    # The port may already be held BY THIS STACK - a redeploy replaces its
    # own container. Only a port held by something ELSE is a problem.
    local holder
    holder=$("${DOCKER[@]}" ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
             | grep -E "[:.]${port}->" | grep -v '^psygabio-watchdog ' | head -1)
    if [ -n "$holder" ]; then
        warn "port ${port} is already in use by another container:"
        warn "  ${holder}"
        problems=$((problems+1))
    else
        ok "port ${port} is free (or held by this stack)"
    fi

    printf '\n  The files this deploy needs\n'
    [ -f "$COMPOSE_FILE" ] && ok "docker-compose.production.yml" \
        || { warn "docker-compose.production.yml is missing"; problems=$((problems+1)); }
    [ -d "$CONFIG_DIR" ] && ok "deploy/config" \
        || { warn "deploy/config is missing - copy config/ into it"; problems=$((problems+1)); }
    [ -f "$ENV_FILE" ] && ok "deploy/.env" \
        || { warn "deploy/.env is missing - copy deploy/.env.example"; problems=$((problems+1)); }
    if [ -f "$ENV_FILE" ]; then
        local perms
        perms=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo 644)
        if [ "$perms" = "600" ]; then
            ok "deploy/.env is mode 600"
        else
            warn "deploy/.env is mode ${perms}, not 600 - it holds secrets"
        fi
    fi
    # The committed template must never be the live config: it carries
    # a password placeholder, so the watchdog would refuse every login.
    if [ -f "${CONFIG_DIR}/config.yaml" ]; then
        if grep -q 'hash: "!"' "${CONFIG_DIR}/config.yaml" 2>/dev/null; then
            warn "deploy/config/config.yaml still has the placeholder password"
            warn "  set a real password before deploying, or nobody can log in"
            problems=$((problems+1))
        else
            ok "deploy/config/config.yaml has a real password"
        fi
    fi

    printf '\n'
    if [ "$problems" -eq 0 ]; then
        ok "this host is ready"
        return 0
    fi
    warn "${problems} problem(s) to resolve before deploying"
    return 1
}

# -----------------------------------------------------------------------------
# deploy
# -----------------------------------------------------------------------------
cmd_deploy() {
    cmd_preflight || die "preflight failed - fix the problems above first"

    # A redeploy replaces this stack's own container, so stop it first
    # rather than leaving it to fight the new one for the port.
    "${COMPOSE[@]}" down 2>/dev/null || true

    log "Building the watchdog image"
    "${COMPOSE[@]}" build

    log "Starting the watchdog"
    "${COMPOSE[@]}" up -d

    log "Waiting for the health check"
    local i
    for i in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:5050/health >/dev/null 2>&1; then
            ok "the watchdog is healthy"
            cmd_status
            return 0
        fi
        sleep 2
    done
    warn "the watchdog did not become healthy in 60s - check the logs:"
    "${COMPOSE[@]}" logs --tail=40
    return 1
}

# -----------------------------------------------------------------------------
# status
# -----------------------------------------------------------------------------
cmd_status() {
    log "Containers"
    "${COMPOSE[@]}" ps
    log "Health"
    local h
    h=$(curl -fsS http://127.0.0.1:5050/health 2>/dev/null || echo "unreachable")
    echo "  $h"
    log "Recent errors"
    "${COMPOSE[@]}" logs --tail=200 2>/dev/null \
        | grep -iE "traceback|critical|error" | tail -10 \
        || echo "  (none)"
}

# -----------------------------------------------------------------------------
# logs / restart / rebuild
# -----------------------------------------------------------------------------
cmd_logs()    { "${COMPOSE[@]}" logs -f "${@:-}"; }
cmd_restart() { log "Restarting"; "${COMPOSE[@]}" restart; sleep 5; cmd_status; }
cmd_rebuild() {
    log "Rebuilding the image from scratch"
    "${COMPOSE[@]}" build --no-cache
    "${COMPOSE[@]}" up -d
    sleep 10
    cmd_status
}

# -----------------------------------------------------------------------------
# backup - the audit trail is the record; back it up
# -----------------------------------------------------------------------------
cmd_backup() {
    # The backup is written INSIDE the container, into the backups volume.
    # The path inside the container is /app/backups - the host's
    # deploy/backups path does not exist there, because the backups are a
    # named Docker volume.
    local stamp
    stamp=$(date +%Y%m%d-%H%M%S)
    log "Backing up the database"
    # A consistent copy: VACUUM INTO takes a transactionally consistent
    # snapshot of a live database without stopping the service.
    "${COMPOSE[@]}" exec -T watchdog python3 -c "
import sqlite3
c = sqlite3.connect('/app/data/sensor_watchdog.db')
c.execute(\"VACUUM INTO '/app/backups/sensor_watchdog-${stamp}.db'\")
c.close()
print('  snapshot written')
" || die "the backup failed"
    # Prune anything older than 30 days, inside the volume.
    "${COMPOSE[@]}" exec -T watchdog sh -c \
        "find /app/backups -name 'sensor_watchdog-*.db' -mtime +30 -delete" || true
    "${COMPOSE[@]}" exec -T watchdog sh -c \
        "ls -la /app/backups/ | tail -3"
    ok "the backup is in the backups volume"
}

# -----------------------------------------------------------------------------
# rollback
# -----------------------------------------------------------------------------
cmd_rollback() {
    log "Available images"
    "${DOCKER[@]}" images psygabio-watchdog --format '  {{.Tag}}  {{.ID}}  {{.CreatedSince}}'
    warn "To roll back, tag the image you want and re-deploy:"
    warn "  docker tag psygabio-watchdog:<tag> psygabio-watchdog:latest"
    warn "  ./deploy.sh restart"
}

case "${1:-deploy}" in
    deploy)    cmd_deploy ;;
    preflight) cmd_preflight ;;
    status)    cmd_status ;;
    logs)      shift; cmd_logs "$@" ;;
    restart)   cmd_restart ;;
    rebuild)   cmd_rebuild ;;
    backup)    cmd_backup ;;
    rollback)  cmd_rollback ;;
    help|--help|-h)
        # The usage block only. awk stops at the first line that is not
        # a comment, so the script body is never printed.
        awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
        ;;
    *) die "unknown command: $1 (try ./deploy.sh help)" ;;
esac
