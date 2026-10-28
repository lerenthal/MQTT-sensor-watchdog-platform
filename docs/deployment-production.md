# Deploying the PsygaBio Device Watchdog to production

**Target:** `10.10.17.11` — the host that already runs the mosquitto,
influxdb, telegraf and grafana stack.

**The watchdog goes in its own Docker stack.** It shares nothing with the
broker stack except the host and the MQTT broker itself.

---

## Why a separate stack

The two stacks have different jobs and different failure modes:

| | Broker stack | Watchdog stack |
|---|---|---|
| Runs | mosquitto, influxdb, telegraf, grafana | watchdog |
| Holds | the record — MQTT history, time-series data, dashboards | the alarm |
| Changes | rarely, and carefully | often, and quickly |
| If it breaks | the record is lost | the alarm is silent |

The record must not be restarted to deploy a watchdog fix, and a watchdog
crash must not take the record down with it. Separate stacks make both
deployable independently.

---

## Before you start

You need on the production host:

- Docker Engine with the **compose v2** plugin
- SSH access
- The MQTT password (the same one the devices use)
- A Telegram bot token and chat id, if you want paging

Check the host is ready:

```bash
cd /opt/psygabio-watchdog
./deploy.sh preflight
```

The preflight checks Docker, the compose plugin, the broker's
reachability, that port 5050 is free, that the config and `.env` are in
place, and — importantly — that the config does **not** still carry the
placeholder password.

---

## Deploy

```bash
cd /opt/psygabio-watchdog

# 1. The config. The committed template carries a password PLACEHOLDER,
#    so it must be edited before it is used.
cp -r config deploy/config
nano deploy/config/config.yaml
```

In `deploy/config/config.yaml`:

```yaml
auth:
  users:
    yaniv:
      role: admin
      password: <the admin password>     # <- set this
```

A plaintext `password:` is upgraded to a salted SHA-256 hash on the
first start, and the plaintext is removed. The committed template cannot
be used as-is: it carries a placeholder that **no** password produces, so
every login would be refused. That is deliberate — it stops a working
credential from ever being in the repository — but it does mean the
password must be set on the server.

Also check:

```yaml
mqtt:
  broker: 10.10.17.11        # the broker on THIS host
  port: 1883
web:
  port: 5050
watchdog:
  # Every timestamp is local wall-clock. Leave this alone unless the
  # facility is not in Israel time.
  timezone: Asia/Jerusalem
```

```bash
# 2. The secrets.
cp deploy/.env.example deploy/.env
nano deploy/.env
chmod 600 deploy/.env          # required - it holds the MQTT password
```

At minimum set `WATCHDOG_MQTT_PASSWORD`. Set the Telegram variables to
enable paging.

```bash
# 3. Hash the passwords, so the config can stay READ-ONLY in the container.
#    (The app cannot rewrite a read-only config.)
./tools/set_password.py deploy/config/config.yaml yaniv '<the admin password>'
./tools/set_password.py deploy/config/config.yaml operator '<the operator password>'

# 4. Deploy.
./deploy.sh
```

The script builds the image, starts the container, waits for the health
check, and prints the status.

---

## After the deploy

```bash
./deploy.sh status
```

Expect:

```json
{"last_packet":"2026-10-28 14:20:31","last_packet_age_seconds":0,
 "mqtt_connected":true,"ok":true}
```

`ok: true` means a packet has actually been **received recently**, not
merely that the broker connection is up. A broker that is reachable but
no longer forwarding leaves the session "connected" while nothing
arrives — reporting that as healthy is exactly the false-confidence
failure this system exists to prevent.

Then open the dashboard:

```
http://10.10.17.11:5050/            the device table
http://10.10.17.11:5050/wallboard   the wall display
```

Log in with the admin account. **Check that the device list is
populated** — an empty table means the watchdog is not receiving, and
`./deploy.sh logs` will show why.

---

## The network — why the watchdog joins the broker's

The watchdog joins the broker stack's Docker network
(`gmp_logger_default`) and reaches the broker **by name**:

```yaml
networks:
  gmp_logger_default:
    external: true
    name: gmp_logger_default
```

This is **necessary**, not a convenience. The host's firewall (`ufw`) has
`deny (incoming)` as its default, and the broker's port 1883 is allowed
only from a subnet that is not on this host. A container on its own
bridge therefore **cannot** reach the host's LAN address — the connection
times out, exactly as it did on the first deploy attempt.

Joining the broker's network lets the container resolve `mosquitto` and
reach it directly, the same way `telegraf` does. The stack is still
separate: its own project, its own volumes, its own lifecycle, and
`./deploy.sh` only ever touches the watchdog.

If the broker stack's network is ever renamed, the watchdog's compose
file must be updated to match — it is declared `external`, so Docker will
not create it.

---

## The config is mounted READ-ONLY

```yaml
volumes:
  - ./deploy/config:/app/config:ro
```

A compromised process must not be able to rewrite the MQTT password or
the session key, so the config cannot be written at runtime.

The app normally upgrades a plaintext password to a salted hash by
rewriting the config. With a read-only mount that is impossible, so the
password is **pre-hashed before the deploy**:

```bash
./tools/set_password.py deploy/config/config.yaml yaniv <password>
```

The tool writes the hash and removes the plaintext. The app also no
longer fails to start if it cannot persist an upgrade — it logs a warning
and carries on.

**The session key must be strong and set in the config.** If it is
missing or weak the app generates a new one, but it cannot save it to a
read-only config, so the key would change on every restart and
invalidate every session. The deploy sets a 64-character key.

---

## The backups are a named volume

The database and the backups are named Docker volumes, so a
`docker compose down` cannot take the audit trail with it.

`./deploy.sh backup` writes **inside the container**, to `/app/backups` in
the backups volume. The host path `deploy/backups` does not exist — the
volume is mounted at `/app/backups` in the container, and the two paths
are not the same thing.

To copy a backup off the host:

```bash
sudo -n docker cp psygabio-watchdog:/app/backups/sensor_watchdog-<stamp>.db .
```

**Backups on the same disk are not backups.** Copy them somewhere else.

---

## The operator is not in the docker group

On this host the operator is in the `sudo` group but not the `docker`
group, so `docker` needs `sudo`. `deploy.sh` detects this and uses
`sudo -n docker` automatically. For that to work the operator needs a
passwordless sudo rule for docker:

```
yaniv ALL=(ALL) NOPASSWD: /usr/bin/docker, /usr/bin/docker compose
```


---

## Operating it

```bash
./deploy.sh status     # containers, health, recent errors
./deploy.sh logs       # follow the logs
./deploy.sh restart    # restart
./deploy.sh rebuild    # rebuild the image from scratch and restart
./deploy.sh backup     # snapshot the database
./deploy.sh preflight  # re-check the host
```

**A front-end change needs a rebuild, not a restart.** `app.py`,
`dashboard.js`, `wallboard.js` and `alarm.js` are baked into the image.
`restart` reuses the cached build layer; `rebuild` is the safe one after
a code change.

### Backups

The database holds the audit trail, so back it up:

```bash
./deploy.sh backup
```

This writes a transactionally consistent snapshot (via `VACUUM INTO`,
without stopping the service) to `deploy/backups/` and prunes anything
older than 30 days.

**Backups on the same disk are not backups.** Copy them off the host:

```bash
rsync -av deploy/backups/ backup-server:/backups/psygabio-watchdog/
```

---

## The MQTT client id

`WATCHDOG_INSTANCE_ID` in `deploy/.env` **must be unique on the host**.

Mosquitto allows one connection per client id and kicks the older
session when a second client connects with the same id. Two instances
sharing an id push each other off the broker in a reconnect loop — which
looks exactly like a flaky network and is very hard to diagnose.

The default is `psygabio-prod`. If you ever run a second watchdog on
the host (a pilot, say), give it a different one.

---

## The broker ACL

The watchdog authenticates as the MQTT user in the config. The broker's
ACL is by **username**, not by source address, so the watchdog connects
fine from the production host whether it runs in a container or directly.

If you change the MQTT username or password, the watchdog stops
receiving and the device table goes empty. The health endpoint will show
`mqtt_connected: false` — that is the first thing to check.

---

## The Grafana dashboards

The Grafana dashboards are **already deployed** to the Grafana instance
on `10.10.17.11:3000` and read from InfluxDB, not from the watchdog's
SQLite database. They are unaffected by this deployment.

They are version-controlled in `grafana/`, so they can be rebuilt and
re-pushed:

```bash
./.venv/bin/python grafana/build_dashboards.py     # regenerate the JSON
./.venv/bin/python grafana/push_dashboards.py      # upload to Grafana
```

The push script reads the service-account token from a file **outside
the repository** and never prints it:

```bash
GRAFANA_TOKEN_FILE=/path/to/grafana_info.txt ./.venv/bin/python grafana/push_dashboards.py
```

**Why the dashboards read InfluxDB and not SQLite:** `sensor_readings`
holds only the **latest** value per channel, so there is no history to
plot. "Was it moving before it froze?" — the question that matters when
a sensor stops — needs the time series, and InfluxDB has it.

---

## Rolling back

```bash
./deploy.sh rollback      # lists the available images
docker tag psygabio-watchdog:<previous> psygabio-watchdog:latest
./deploy.sh restart
```

The database is in a named volume, so it survives a redeploy. If the
config was changed, restore the previous version from the Admin Config
history page (it keeps 50 versions with timestamps and notes).

---

## What to check in the first hour

1. `./deploy.sh status` — healthy, and `last_packet_age_seconds` small
2. The device table is populated and the statuses look right
3. The wall board shows the fleet, and the TEST ALARM button beeps
4. Trigger a test alarm from Telegram (ack one of the buttons) and
   confirm the dashboard reflects it
5. `./deploy.sh backup` and copy the backup off the host
6. **Set up the browser on the TV to kiosk mode** pointing at
   `http://10.10.17.11:5050/wallboard`, so it survives a reboot

---

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| The container will not start | The config is invalid or missing a password | `./deploy.sh logs` — the app says exactly what is wrong |
| Healthy, but the device table is empty | The MQTT credentials are wrong, or the client id is duplicated | `mqtt_connected` in `/health`; check `WATCHDOG_INSTANCE_ID` is unique |
| Devices show OFFLINE but are publishing | The broker ACL changed, or the device id changed | Compare the device id with the topic in the broker |
| A front-end change did not appear | The image was not rebuilt | `./deploy.sh rebuild` |
| The wall board is blank | The browser lost the session | Log in again; the kiosk browser should store the session |
| Everything stopped at once | The broker stack was restarted | `./deploy.sh status`; the watchdog reconnects by itself within a minute |
