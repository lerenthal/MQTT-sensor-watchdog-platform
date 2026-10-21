# Install

Bring a fresh host up to a working deployment of the MQTT Sensor Watchdog.

## 1. Host prerequisites

A Linux host (Debian / Ubuntu / RHEL) with:

- **Docker Engine 24+** with the Compose plugin (`docker compose`).
  Use [Docker's official install docs](https://docs.docker.com/engine/install/)
  for your distro. Don't use the distro's `docker.io` package — it's old.
  Verify with `docker info`.
- **Outbound network** to your MQTT broker.
- **Outbound network** to `api.telegram.org` (TCP 443) so Telegram
  nagging works.
- **`curl`** (only for one-time TLS fingerprint check below).
- **At least 1 GB free disk** for the SQLite database and log rotation.
- A **non-root user** with sudo if rootless Docker isn't enabled. The
  container runs as the host UID via `HOST_UID` / `HOST_GID`.

The watchdog itself runs **inside** the container; **nothing else** is
installed on the host.

### One-time: install Docker on Debian / Ubuntu

```bash
sudo apt-get update
sudo apt-get install ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                        docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker $USER
newgrp docker
docker info        # should print engine info without permission errors
```

## 2. Clone the repo

```bash
sudo mkdir -p /opt/sensor-watchdog
sudo chown -R $USER:$USER /opt/sensor-watchdog
cd /opt/sensor-watchdog

git clone https://github.com/lerenthal/MQTT-sensor-watchdog-platform.git .
```

## 3. Configure secrets (`.env`)

```bash
cp .env.example .env
$EDITOR .env
```

The env file controls secrets that should **never** live in version control:

| Variable | Purpose | Where to get it |
|---|---|---|
| `WATCHDOG_MQTT_PASSWORD` | MQTT broker password | Your broker admin |
| `WATCHDOG_TG_TOKEN` | Telegram bot token | `@BotFather` on Telegram |
| `WATCHDOG_TG_CHAT_ID` | Telegram target chat | Send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `WATCHDOG_SECRET_KEY` | Flask session signing | `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `TZ` | Container timezone | IANA name like `Asia/Jerusalem` |

Each env var **overrides** the matching `config.yaml` value at runtime. See
[`docs/configuration.md`](configuration.md) for full precedence.

You do not have to set all of them. If you only set the MQTT password and
leave the rest in `config.yaml`, that works too.

## 4. Configure behaviour (`config.yaml`)

```bash
mkdir -p config backups
cp config.yaml config/config.yaml
$EDITOR config/config.yaml
```

Replace the `CHANGE_ME` placeholders. See
[`docs/configuration.md`](configuration.md) for what each block does.

## 5. Bring it up

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) ./ops.sh start
```

This:

1. Validates the YAML, env file, and compose file.
2. Builds the Docker image (`docker compose build`) on first run.
3. Starts the container in the background (`docker compose up -d`).
4. Prints container + endpoint status.

If `./ops.sh status` reports `MQTT: DOWN`, that's expected on first start
while the MQTT client establishes — wait ~30 seconds for the broker
handshake, or check the broker credentials with `./ops.sh logs`.

## 6. Open the dashboard

```bash
xdg-open http://localhost:5050        # or just navigate there
```

First login uses the seeded credentials in `config.yaml`:

- `yaniv` / the password you set in `auth.users.yaniv.password`
- `operator` / the password you set in `auth.users.operator.password`

**Change both immediately** at `/admin/config` → set `auth.users.*.password`
to a real value, then submit. **The plaintext password is migrated to a
salted sha256 hash on the next container restart.** That's by design — the
file on disk no longer carries a recoverable password.

## 7. Verify the watchdog loop

```bash
./ops.sh logs --since 2m
```

You should see:

```
[INFO] MQTT client started (broker=…:1883)
[INFO] Migration: … (only on first run / schema upgrades)
[INFO] Database initialized successfully.
[INFO] Job "watchdog_eval_loop (trigger: interval[0:00:30], …)
[INFO] Job "_watchdog_check_health (trigger: interval[0:00:15], …)
[INFO] Starting Flask on 0.0.0.0:5050
```

## 8. Update later

```bash
cd /opt/sensor-watchdog
git pull
./ops.sh update    # rebuilds the image and restarts (DB + config preserved)
```

## Uninstall

```bash
./ops.sh stop      # container stops, DB + config kept on host
rm -rf data config backups .env
```

There is no system-wide install; everything lives in the project directory.

## Common first-run issues

| Symptom | Cause | Fix |
|---|---|---|
| `GET /health → curl: (56) Recv failure` immediately after start | Flask not bound yet (start_period is 20s) | Wait; check `./ops.sh logs` for startup errors |
| `MQTT: DOWN` in dashboard | Wrong broker address/credentials in `config.yaml` or `.env` | Edit `/admin/config`, click **Save & Apply**, then `./ops.sh restart` |
| Telegram button clicks do nothing | Bot token in `.env` is wrong or bot not added to the chat yet | Re-issue with `@BotFather`; check `chat_id` |
| Container restart loop with `database is locked` | Many writers (rare on real installs; more common on dev with high message rates) | Bump `/admin/config` `web.dashboard_refresh_seconds` higher; or open an issue |

For deeper topics see:

- [`docs/configuration.md`](configuration.md) — every key in `config.yaml`
- [`docs/operations.md`](operations.md) — every `./ops.sh` subcommand
- [`docs/security.md`](security.md) — auth, secrets, audit
