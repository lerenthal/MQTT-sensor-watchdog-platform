# Documentation

Per-topic documentation for the MQTT Sensor Watchdog Platform.

> **[REVIEW_BRIEF.md](REVIEW_BRIEF.md)** — a self-contained brief for an
> external code review: objectives, current installation, full codebase map,
> known issues, incident history, and specific questions to answer. Start here
> if you have been asked to review this project.

| Doc | Audience | When to read |
|---|---|---|
| [`install.md`](install.md) | Production engineer | First-time deployment on a fresh host. |
| [`configuration.md`](configuration.md) | Anyone editing the config | Reference for every key in `config.yaml` and every `WATCHDOG_*` env var. |
| [`operations.md`](operations.md) | Operator | The `./ops.sh` command surface, container lifecycle, log triage, backups. |
| [`user-guide.md`](user-guide.md) | Operator / QC | Dashboard navigation, snooze/restart workflow, the print report. |
| [`security.md`](security.md) | Security / IT | Threat model, secrets, audit trail, user accounts. |
| [`architecture.md`](architecture.md) | Engineer | Code-level tour: file layout, threading, WAL, detection rules. |
| [`development.md`](development.md) | Contributor | Local dev setup, coding style, extending the system. |

For the project itself, see the [top-level README](../README.md).
