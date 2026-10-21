# Security

The watchdog is a small, single-host application — but it does ingest
environment sensor data and emit alerts to operators, so a few security
practices are worth being deliberate about. This document covers what
threats the design mitigates against, where it's intentionally limited,
and what you need to do at deployment time.

## Threats considered

| Threat | Mitigation |
|---|---|
| Unauthenticated snooze / restart | Session login (Flask-Login-style flow). Web routes require `login_required`. |
| Snooze / restart by an operator without manager visibility | Audit log. Every action, including CSV upload, records the username. |
| Plaintext passwords in `config.yaml` | Auto-hashed to salted sha256 on first start. Plaintext is removed from disk. |
| Replay attacks on Telegram button clicks | Only the `callback_data` payload is parsed; actions are idempotent (setting `acknowledged=1` twice is a no-op). |
| SQL injection through any user-influenced string | SQLite parameterised queries throughout. The CSV upload parser is `csv.reader` (stdlib) and never builds SQL from row data. |
| Log injection in audit rows | All audit writes go through the `audit()` helper, which uses parameterised SQL. |
| Concurrent DB writers creating contention | SQLite WAL + `busy_timeout`. See [`docs/architecture.md`](architecture.md#sqlite-wal-mode). |
| Silent death on broker disconnect | `paho-mqtt`'s reconnect_delay_set + an explicit Telegram alarm after 60s of downtime. |

## What is not (yet) implemented

These are honest gaps. If any of them matter to your facility, treat them
as open work items.

1. **No TLS for the web server.** The Flask app speaks plain HTTP. Run
   the container behind a reverse proxy (nginx / Caddy / traefik) or a
   cloud LB to terminate TLS. The image has no nginx — we leave that to
   the operator's standard infrastructure.

2. **No CSRF token on form POSTs.** Flask sessions use the `SameSite`
   default (`Lax`) which blocks most cross-origin form submissions. If
   you serve over HTTPS and want stricter CSRF protection, consider
   flask-wtf in a future change.

3. **No role granularity.** There are exactly two roles: `admin` (full
   access) and `operator` (read + snooze + restart). Granular permissions
   per action, or temporary credentials, are not implemented.

4. **Telegram chat identity is per-message.** When a Telegram button is
   pressed, the audit row records the Telegram `@username` if present. A
   determined attacker who controls a Telegram account could press
   buttons from anywhere — but the audit trail makes this traceable.

5. **No rate limiting.** A script that hammers `/snooze/<id>/<hours>`
   will succeed indefinitely. The Flask app is single-threaded and
   sqlite-backed; sustained abuse would degrade the dashboard for real
   users. Add a reverse proxy with rate-limit middleware if this matters.

## Secrets

### What counts as a secret

- `WATCHDOG_MQTT_PASSWORD`
- `WATCHDOG_TG_TOKEN`
- `WATCHDOG_TG_CHAT_ID`
- `WATCHDOG_SECRET_KEY` (Flask session signing)

### Where secrets live

The recommended setup is:

- Real production deployments: `.env` (gitignored), injected via Docker
  Compose into the container as environment variables.
- Local development: `config/config.yaml` (gitignored when in `config/`,
  but `config.yaml` at the repo root is the *template* and is committed
  with `CHANGE_ME` placeholders).

Avoid committing `mqtt.password`, `telegram.bot_token`, `telegram.chat_id`,
`web.secret_key`, or any `.env` file to the repo. The bundled `config.yaml`
is sanitised at every commit. If you ever see real values in
`config.yaml`, that's a bug — open an issue.

### Generating the secret_key

A safe `WATCHDOG_SECRET_KEY`:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Rotate it to invalidate existing browser sessions (everyone will be
logged out). Useful when a former operator leaves.

## User accounts

Authentication uses a small fixed user table. There is currently no
self-service UI for adding or rotating users — admin must edit
`config.yaml` and restart.

### Default users shipped with the repo

| Username | Bundled password | Role |
|---|---|---|
| `yaniv` | `mushroom` | `admin` |
| `operator` | `operator` | `operator` |

The bundled `config.yaml` ships these for *first-time bootstrap only*.
On the first container start, `_ensure_password_hashes()` walks the user
table and rewrites each plaintext password as a salted sha256 hash:

```yaml
auth:
  users:
    yaniv:
      password_hash:
        salt: "abcd1234..."
        hash: "abcdef1234567890..."
      role: "admin"
```

From that point on, no recoverable password sits on disk. **You still need
to change the bundled passwords before going to production**, because
those values were committed and any attacker who has the repo already
knows them.

### Adding a user

1. Edit `config.yaml` and add a new entry under `auth.users:`:

   ```yaml
   auth:
     users:
       existing_user:
         password: "…"
         role: "operator"
   ```

   Roles:
   - `admin` — can reach `/admin/*` (config editor, CSV upload).
   - `operator` — read + snooze + restart; no `/admin/*` access.

2. `./ops.sh restart`.

The new plaintext password is hashed on the next start.

### Removing / disabling a user

Either:

- **Remove** the entry from `config.yaml`, `./ops.sh restart`. The user's
  existing session becomes invalid at the next request after rotation.
- **Set a long random password** they don't know. Existing session is
  invalidated on the same schedule.

### Session lifetime

`app.permanent_session_lifetime = timedelta(hours=12)`. After 12 hours
of inactivity, the user must sign in again. A restart also kills sessions
because `secret_key` is freshly loaded at boot (or stays the same if you
didn't rotate).

## Audit trail

All privileged actions are written to `audit_logs`:

| Action | When it fires |
|---|---|
| `LOGIN` | Successful login via `/login`. |
| `LOGIN_FAIL` | Failed login attempt. Includes the username that was tried. |
| `LOGOUT` | Click on the **Logout** button. |
| `SNOOZE` | Snooze via the dashboard. Includes duration. |
| `UNSNOOZE` | Unsnooze via the dashboard. |
| `ACK` | Ack button from Telegram. |
| `RESTART_SENT` | Restart button from the dashboard or Telegram. Includes the published topic. |
| `CONFIG_SAVE` | Edit in `/admin/config`. Includes the change note. |
| `CONFIG_ROLLBACK` | Restore action. Includes the restored version ID. |
| `CSV_UPLOAD` | CSV metadata upload. Includes row count and filename. |

The table is append-only. There is no UI to delete rows; to remove old
entries, run from the host:

```bash
./ops.sh shell
sqlite3 /app/data/sensor_watchdog.db \
  "DELETE FROM audit_logs WHERE timestamp < date('now', '-1 year');"
```

For audit-retention requirements, do this in a cron and pipe the output
to a backup.

## File permissions

By default the container runs as the host UID (via `HOST_UID` / `HOST_GID`
that you set when starting). That means:

- `./data/sensor_watchdog.db` is written by your user; readable by your
  user. Not readable by other unprivileged users on the host (if you
  keep `/opt/sensor-watchdog` mode `750`).
- `./config/config.yaml` is written by your user on first start.
- `./.env` is *yours*; the container reads it as your UID.

If you change permissions to make these world-readable, you've
effectively exposed any plaintext in them. Don't.

## Production checklist

Before going live:

- [ ] `mqtt.password`, `telegram.bot_token`, `telegram.chat_id`,
  `web.secret_key` are in `.env`, not `config.yaml`.
- [ ] `.env` is `chmod 600` and not committed.
- [ ] Default users' passwords are rotated from the bundled placeholders.
- [ ] `/health` endpoint is reachable from your monitoring system, OR
  the container healthcheck is integrated with your container platform.
- [ ] TLS terminates at a reverse proxy or cloud LB, not at Flask.
- [ ] Backups go to an off-box location (`scp` / `rsync` / object-store).
- [ ] Audit log retention is configured (or a downstream sink).
- [ ] A documented "operator offboarding" runbook: invalidate the
  operator's session (`WATCHDOG_SECRET_KEY=... ./ops.sh restart`),
  rotate their password.
