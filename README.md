# 🍄 MQTT Sensor Watchdog Platform

> Independent MQTT subscriber + watchdog evaluator + Telegram nagging engine +
> Flask admin dashboard for ESPHome-based environmental sensors in a facility
> running GMP-relevant production. Detects device **OFFLINE** and **STALLED**
> faults, maintains a full audit trail, and pages an operator via Telegram
> until acknowledged or snoozed.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

---

## What this does

You have N ESPHome (or compatible) sensor nodes publishing on MQTT to a Mosquitto
broker. This application sits between the broker and you:

1. **Subscribes** to your facility's MQTT topic tree and records every sensor
   reading into SQLite.
2. **Detects** two failure modes:
   - **OFFLINE** — the device stopped sending packets past a configured
     timeout (or reported `offline` via LWT).
   - **STALLED** — the device is still connected but its `heartbeat_counter`
     value stopped incrementing past a configured timeout.
3. **Re-nags** the operator via Telegram every *N* minutes until the alert is
   acknowledged or snoozed (1h / 8h / 24h from inline buttons).
4. **Logs** every action — login, snooze, restart, CSV upload, config change —
   to an append-only audit table with the operator username.
5. **Provides** a password-protected web dashboard with live sensor readings,
   drilldown per device, sortable / searchable columns, CSV-driven facility
   metadata (location, role), and printable status reports for compliance
   batches.

This is a **standalone** watchdog — it does not require, depend on, or
integrate with Home Assistant. It only needs an MQTT broker.

---

## Quick start

```bash
git clone https://github.com/lerenthal/MQTT-sensor-watchdog-platform
cd MQTT-sensor-watchdog-platform

# One command to bring it up (bin/build the image on first run)
HOST_UID=$(id -u) HOST_GID=$(id -g) ./ops.sh start
```

Then open **`http://localhost:5050`** and sign in with the seeded admin
(`/admin/config` → change passwords there or via the `auth:` block of
`config.yaml`).

To tear it back down without losing data:

```bash
./ops.sh stop    # keeps ./data and ./config/config.yaml
```

---

## Documentation

The doc set lives under [`docs/`](docs/). Read in this order:

> **[docs/REVIEW_BRIEF.md](docs/REVIEW_BRIEF.md)** — self-contained brief
> for an external code review (objectives, installation, codebase map,
> known issues, incident history, open questions).

| | Doc | Audience |
|---|---|---|
| 1 | [`docs/install.md`](docs/install.md) | Production engineer setting up a fresh host |
| 2 | [`docs/configuration.md`](docs/configuration.md) | Anyone editing `config.yaml` or `.env` |
| 3 | [`docs/operations.md`](docs/operations.md) | Operator who runs `./ops.sh start/stop/logs/...` |
| 4 | [`docs/user-guide.md`](docs/user-guide.md) | Operator/QC who logs in and uses the dashboard |
| 5 | [`docs/security.md`](docs/security.md) | Security/IT — auth, secrets, audit |
| 6 | [`docs/architecture.md`](docs/architecture.md) | Engineer who wants to understand the codebase |
| 7 | [`docs/development.md`](docs/development.md) | Contributor / local dev |

---

## Repository layout

```
.
├── app.py                  # Single-file Flask + MQTT + watchdog + Telegram
├── dashboard.js            # Client-side JS for the dashboard (sort/search/expand)
├── wallboard.js            # The wall display's own JS
├── alarm.js                # Shared alarm engine: tone styles, mute, test
├── schema.sql              # SQLite schema (idempotent)
├── config.yaml             # Bundled config template (committed; sanitised)
├── requirements.txt        # Python deps for non-Docker development
├── Dockerfile              # python:3.12-slim, non-root, healthcheck
├── docker-compose.yml      # The development instance
├── docker-compose.production.yml   # The production stack (separate)
├── ops.sh                  # Operator entry-point: start/stop/restart/logs/...
├── deploy.sh               # Production deploy: preflight/deploy/status/backup
├── grafana/                # Version-controlled Grafana dashboards
├── tests/                  # Test suites
├── tools/                  # Verification tools
├── deploy/                 # Production deploy inputs (.env.example, config)
├── .dockerignore           # Keep image small
├── .env.example            # Documented template; cp to .env for secrets
├── .gitignore
├── LICENSE                 # GPL v3
├── README.md               # this file
├── docs/                   # All documentation
└── data/                   # Created at runtime by ops.sh; bind-mounted in container
```

In production, only the following are persistent and operator-managed:

```
config/config.yaml         # live config (bind-mounted; gitignored)
.env                       # secrets (gitignored)
data/sensor_watchdog.db    # SQLite database (bind-mounted; gitignored)
backups/                   # ./ops.sh backup target (gitignored)
```

On the production host the same files live under `deploy/`, and the
database and backups are **named Docker volumes** so a `docker compose
down` cannot take the audit trail with it.

---

## Requirements

### Host
- Linux (Debian / Ubuntu / RHEL tested)
- Docker Engine 24+ and Compose v2 (or `docker-compose` v1)
- Outbound network to your MQTT broker
- Outbound network to `api.telegram.org` (for nag-alert delivery)

### Runtime (inside container)
- Python 3.12 (provided by the image)
- `paho-mqtt`, `APScheduler`, `Flask`, `requests`, `PyYAML` — installed from
  `requirements.txt` during image build
- `curl` for the in-container healthcheck
- A directory you control for `./data`, `./config`, `./backups`

---

## License

GPL-3.0. See [LICENSE](LICENSE).

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation.

## Verification tools

`tools/` holds end-to-end checks used to validate the app against the
live broker. They are runnable, not just documentation:

| Tool | What it proves |
|---|---|
| `tools/verify_features.py <url>` | The dashboard features: ignore list, snooze control, persistent highlighting, TEST ALARM button, honest restart reporting. Exits non-zero on failure. |
| `tests/test_false_alarm_fixes.py` | Retired channels are removed and not resurrected by the broker's retained replay. |
| `tests/test_production_readiness.py` | The WAL is checkpointed, the history is pruned and audited, a corrupt config says why, and no raw datetime is bound to SQL. |
| `tests/test_admin_routes.py` | Every admin route points at the right view; no stacked decorators. |
| `tests/test_snooze_semantics.py` | A snooze silences the wall board and an unsnooze restores it immediately. |
| `tools/verify_restart.sh <service> <device>` | The restart path: command published, broker acknowledgement, heartbeat reset. |
| `tools/verify_restart_probe.py <device>` | The restart path, run inside the container, watching for the LWT status cycle that proves a reboot. |
| `tools/find_restart_topic.py <device>` | Probes candidate restart topics to find the one a device actually listens on. |
| `tools/test_wallboard.js` | Headless test of the alarm toggle, mute and style selector - no browser needed. |
| `tools/test_table_structure.js` | Asserts the table header and the row renderer agree on the columns. |
| `tools/test_row_handling.js` | Asserts no code path treats an expanded detail row as a device. |
| `tools/test_expansion_controls.js` | Asserts the expand/collapse controls drive state something renders. |
| `tools/test_expansion_behaviour.js` | Runs the real dashboard.js against a stubbed DOM and proves the buttons change the output. |
| `tools/verify_restart_e2e.py` | Drives the real restart API and proves the device reboots. |
| `tools/probe_restart.py` | Discovers a device's real restart topic and tests it directly. |

```bash
.venv/bin/python tools/verify_features.py http://localhost:5050
bash tools/verify_restart.sh watchdog bme680-1
```

**Never run a restart test against a device in production** - a restart
drops its readings for a few seconds.
