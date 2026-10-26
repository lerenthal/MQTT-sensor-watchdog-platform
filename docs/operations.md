# Operations

Once deployed, the watchdog is operated entirely through `ops.sh` and the
web dashboard. This doc covers the shell side.

## The operator entry point: `ops.sh`

A single script that wraps the full Docker Compose lifecycle. It detects
whether you have `docker compose` (V2 plugin) or `docker-compose` (V1) and
uses whichever is present.

```bash
./ops.sh <command> [args]
```

### Commands

| Command | Purpose |
|---|---|
| `start` | Build (if needed) and start the container in the background. |
| `stop` | Stop the container. Does NOT delete `./data`, `./config`, or `./backups`. |
| `restart` | Stop + start. Use when you changed `web.host`/`port`/`secret_key`, MQTT broker address, etc. |
| `logs` | Print the last 200 log lines and exit. Use `./ops.sh logs follow` to stream live. |
| `status` | Show container health, `/health` HTTP code, recent audit log entries. |
| `backup` | Snapshot the live SQLite DB to `./backups/`. Online-safe (uses SQLite `VACUUM INTO`). |
| `restore FILE` | Restore a backup. Snapshots current DB first. **Requires typing `YES` at a prompt.** |
| `shell` | Open `/bin/bash` *inside* the running container. Press `Ctrl+D` to exit. |
| `validate` | Parse `config.yaml`, `docker-compose.yml`, and `.env`. Reports structural issues. |
| `rebuild` | Force-rebuild the image from scratch (`--no-cache`). Use after dependency updates. |
| `update` | Git pull + rebuild + restart. Only works if this is a git clone. |
| `reset` | **Destructive.** Stop, wipe `./data` and `./config`, recreate empty dirs, start fresh. Requires typing `RESET` at a prompt. |
| `help` | Print the command list. |

### Common workflows

#### "Bring up, watch it run, take it down"

```bash
./ops.sh start              # build + run
xdg-open http://localhost:5050

./ops.sh status             # one-shot snapshot of state
./ops.sh logs follow        # Ctrl+C to stop tail

./ops.sh stop               # leave DB + config on disk; container removed
```

#### "Watch only the recent log lines without streaming"

```bash
./ops.sh logs
```

(Default is the last 200 lines. `follow` flag is for live streaming.)

#### "Snapshot the DB right now"

```bash
./ops.sh backup
# writes e.g. ./backups/sensor_watchdog.20261021T182500Z.db
```

The snapshot is a consistent copy — safe to do while the container is
running. To get it off-box:

```bash
scp ./backups/sensor_watchdog.20261021T182500Z.db admin@backup-host:
```

#### "Restore a backup"

```bash
./ops.sh restore ./backups/sensor_watchdog.20261021T182500Z.db
# confirmation prompt: type YES
```

The current DB is auto-snapshotted as `pre-restore.<timestamp>.db` before
overwriting. If you change your mind, the prior version is recoverable.

#### "See what the container is doing inside the box"

```bash
./ops.sh shell
```

Once inside:

```bash
$ ls                  # /app/{app.py, schema.sql, dashboard.js, data/, config/, backups/}
$ python3 -c "import sqlite3; c = sqlite3.connect('/app/data/sensor_watchdog.db'); print(c.execute('SELECT COUNT(*) FROM sensor_readings').fetchone()[0])"
$ ls -la /app/data    # see the .db-wal sidecars that mean WAL mode is on
$ cat /etc/hostname    # the container ID
$ exit                # back to host
```

#### "Run from a fresh state (DEVELOPMENT ONLY)"

```bash
./ops.sh reset    # type RESET at the prompt, wipes data + config + backups
./ops.sh start    # rebuilds, repopulates ./config/config.yaml from the bundled template
```

This is what you'd run on a sandbox VM. **Do not run `reset` against a
production host — it deletes the audit log.**

#### "Apply a config edit"

```bash
$EDITOR config/config.yaml
./ops.sh restart
```

Most settings reload *without* a restart, but if the broker address, port,
or Flask `secret_key` changed, a full restart is required.

## Container lifecycle

| Aspect | How it works |
|---|---|
| Restart policy | `unless-stopped` — survives host reboots unless you explicitly stop it. |
| Health endpoint | `GET /health` inside the container. Returns `{"ok": true|false}` based on MQTT connection state. Container is "healthy" when the endpoint returns 200. |
| Healthcheck interval | Every 30s. Start period 20s (Flask takes ~10s to import everything on a fresh DB). |
| Logging | JSON-file driver, max 10 MB per file, rotated 3 times. Inspect via `./ops.sh logs`. |
| Container name | `sensor_watchdog` (one container, no replicas — this is stateful). |
| Network | Default bridge. The container can reach your MQTT broker via its routable address. |

## Log analysis

Common patterns and what they mean:

| Pattern | Meaning |
|---|---|
| `MQTT client started (broker=…:port)` | Container has connected to the broker. |
| `Migration: added devices.location, devices.role` | DB upgrade path ran. Only fires once per upgrade. |
| `Migration: renamed sensor_readings.sensor_id -> sensor_name` | DB upgrade to v4. Only fires once. |
| `MQTT connected & subscribed to facility/+/env_sensor/#` | Reconnect succeeded after a disconnect. |
| `MQTT broker unreachable. Down for Ns.` | Wired to Telegram if you have one configured. |
| `on_mqtt_message failed for topic=…` | A specific malformed message. Followed by a Python traceback. Inspect the topic & payload. |
| `watchdog_eval_loop crashed` | The 30-second cycle hit an exception. Caught — the next cycle runs. |
| `database is locked` | WAL contention — fixed by default. If you see it again, file a bug. |
| `Failed to send Telegram notification` | Bot token, chat ID, or network. Check `telegram.enabled` and `chat_id`. |

For a long-tail grep over historical messages:

```bash
docker compose logs > /tmp/watchdog.log
grep -E "ERROR|crashed|MQTT" /tmp/watchdog.log | less
```

## Disk + log growth

Logs rotate at 10 MB × 4 = 40 MB max. The SQLite database typically grows
~5 MB per 1000 sensors per day at 1 Hz reporting; with the default
`UNIQUE(device_id, sensor_name, sensor_type)` UNIQUE index, the table stores
one row per (device, sensor, type) — not one per message. So even a noisy
broker only adds roughly `devices × sensors_per_device × types × ~50 bytes`.

Backups older than a year can be deleted manually:

```bash
find backups -mtime +365 -delete
```

The DB itself is not auto-pruned. If you need long-term retention beyond
"in this file", set up a periodic `./ops.sh backup && scp …` cron.

## Restart policy

The watchdog is set to `restart: unless-stopped` in `docker-compose.yml`.
When you run `./ops.sh stop`, Compose removes the container so it will not
relaunch. To re-enable auto-relaunch:

```bash
docker compose up -d        # after ./ops.sh start fails, this restores it
# or:
./ops.sh start
```

## Failure modes the operator should know

| Symptom | Likely cause | First action |
|---|---|---|
| Container won't start | config.yaml malformed | `./ops.sh logs` for the YAML error; fix and `./ops.sh restart` |
| `/health` returns 200 but `MQTT: DOWN` persistently | Broker unreachable from this network | Test broker reachability: `docker compose exec watchdog nc -zv 10.10.17.11 1883` |
| Telegram buttons don't respond | Bot token wrong / chat_id wrong | `curl https://api.telegram.org/bot<TOKEN>/getMe` (replace `<TOKEN>` with the real value) |
| Audit log missing rows | `auth.users` deleted without reload | Restart — the in-process audit helper reads session username, not a stale cached value |
| A device stuck in STALLED after MQTT says it's fine | Heartbeat sensor is dead, even though it sends other readings. Real hardware fault. | Real fault — call facilities. |
| Dashboard shows rows but every `last_temp` is NULL | ESPHome publishes a topic shape the parser doesn't understand. | Subscribe manually to verify; open an issue with a topic sample. |

## Cron job suggestion

Add a weekly backup cron (root or your user):

```cron
0 4 * * 1 cd /opt/sensor-watchdog && ./ops.sh backup \
  && find backups -mtime +30 -delete
```

This runs every Monday at 04:00, takes a fresh backup, and prunes anything
older than 30 days.

## Production checklist

Before going live on a facility:

- [ ] `mqtt.password`, `telegram.bot_token`, `telegram.chat_id`,
  `web.secret_key` are all in `.env`, **not** in `config.yaml`.
- [ ] `.env` is not committed to git (`git status` doesn't list it).
- [ ] Both `auth.users.*.password` entries are changed from the bundled
  placeholders, and `config/config.yaml` has been rewritten by the
  container's password-hash migration (no plaintext remaining).
- [ ] `web.secret_key` is a 32+ byte random value, not `CHANGE_ME`.
- [ ] `./ops.sh status` shows healthy for at least 5 minutes after start.
- [ ] One device sends MQTT traffic, and the device appears in the
  dashboard within 10s of connecting.
- [ ] Force a fault: stop a sensor node, see OFFLINE in the dashboard
  within `default_offline_timeout_seconds` + 30s.
- [ ] Trigger the Telegram button — confirm the call-back updates the
  alert state.
- [ ] `./ops.sh backup && ./ops.sh restore ./backups/<that file>` works
  end-to-end before you rely on it.

## Restarting a device from the dashboard

The **Restart** button in a device row publishes `PRESS` to
`facility/<device_id>/env_sensor/button/restart/cmd` with QoS 1 and waits
for the broker to acknowledge it.

**The device will only reboot if its firmware has an MQTT button that
listens on that topic.** A device whose ESPHome configuration declares
`button: - platform: restart` WITHOUT an `mqtt:` block has a *local-only*
button: it appears in the Home Assistant device page and can be pressed
there, but it does not subscribe to MQTT and will ignore the command.

The watchdog detects this and says so rather than reporting a false
success:

| Situation | HTTP | Body |
|---|---|---|
| Device has an MQTT button | 200 | `{"ok": true, "topic": ...}` |
| Command reached the broker, device has no MQTT button | 502 | `{"ok": false, "published": true, "error": "...will NOT reboot..."}` |
| Broker unreachable / not acknowledged | 502 | `{"ok": false, "error": "MQTT publish was not acknowledged..."}` |

Both non-success cases are audited. The second is audited as
`RESTART_COMMAND_UNACKNOWLEDGED` so the gap is visible in the audit log
rather than only in a browser alert.

### To make a device restartable

Add an MQTT button to the device's ESPHome configuration:

```yaml
mqtt:
  id: mqtt_client
  broker: 10.10.17.11
  ...
button:
  - platform: mqtt
    id: restart_button
    name: "Restart"
    command_topic: button/restart/cmd
    payload_press: "PRESS"
```

The full topic the watchdog publishes to is
`facility/<device_id>/env_sensor/button/restart/cmd`, so with the
default ESPHome `mqtt:` prefix the `command_topic` above resolves to the
right place. Verify with `mosquitto_sub -t 'facility/+/env_sensor/button/#'`
- the device must publish a `.../button/<name>/state` topic, which is
also the evidence the watchdog uses to decide the device is
restartable.

### Verifying a restart

`./verify_restart.sh <service> <device_id>` publishes the command and
watches the heartbeat counter. A reboot resets the counter, so the value
drops sharply rather than continuing to increment.

**Run the probe from inside the container.** The facility broker ACL
allows the watchdog's source address (the Docker bridge) but rejects new
logins from the host LAN address; a probe run from the host gets "not
authorised" and sees nothing, which looks identical to a broken publish.

