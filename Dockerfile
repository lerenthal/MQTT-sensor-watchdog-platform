# Mushroom Facility Sensor Watchdog — Dockerfile
# Single-process container: Flask + APScheduler + MQTT subscriber + Telegram poller.

FROM python:3.12-slim

# System deps — curl is needed for the HEALTHCHECK; sqlite3 for offline db
# inspection if ops.sh needs to shell into the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Create a non-root user. Sensor MQTT publishes / DB writes don't need root,
# and dropping privileges is the cheapest insurance against container escape.
RUN groupadd --system --gid 1001 watchdog \
 && useradd --system --uid 1001 --gid watchdog \
    --home /app --shell /sbin/nologin watchdog

WORKDIR /app

# Install Python deps first so a code-only change rebuilds the layer cheaply.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application files (config and DB come in via bind mounts, not the image).
COPY app.py schema.sql README.md dashboard.js wallboard.js /app/

# Persistent / mountable paths. ops.sh bind-mounts ./data and ./config onto these.
RUN mkdir -p /app/data /app/config /app/backups \
 && chown -R watchdog:watchdog /app

USER watchdog

EXPOSE 5050

# Defaults can be overridden in docker-compose.yml. WATCHDOG_DB is in /app/data
# so the bind-mounted host directory captures it.
ENV WATCHDOG_DB=/app/data/sensor_watchdog.db \
    WATCHDOG_CONFIG=/app/config/config.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Curl on the same internal port; WARN if the Flask process is up but stuck
# (e.g. deadlocked) — Uptime check goes red instead of green.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5050/health || exit 1

# Single entrypoint — compose overrides the default command only via profiles,
# which we don't use. Keep it simple.
CMD ["python", "app.py"]
