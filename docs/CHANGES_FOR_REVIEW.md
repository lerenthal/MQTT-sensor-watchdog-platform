# Change Summary — Review Fixes (2026-10-25)

**For:** Hermes (status update + change review)
**Baseline:** working tree was clean at `HEAD = 3438105`; these changes are **uncommitted**
in the working tree (nothing pushed, no branch cut — awaiting your review).
**Source of the work:** [docs/REVIEW_FINDINGS.md](REVIEW_FINDINGS.md) (19 findings, B1–B19).
All 19 are addressed; one additional bug (**B20**) was found and fixed during verification.

**Verification:** a new self-contained suite, [tests/test_review_fixes.py](../tests/test_review_fixes.py),
passes all 18 assertions. It uses a throwaway DB + a temp copy of `config.yaml`, so it never
touches live `data/` or `config/`.

```bash
# from the repo root (deps live in the repo .venv)
./.venv/bin/python tests/test_review_fixes.py      # -> ALL CHECKS PASSED
```

Files changed: `app.py`, `dashboard.js`, `wallboard.js`, `config.yaml`, `schema.sql`,
`CHANGELOG.md`, `docs/install.md`; added `docs/REVIEW_FINDINGS.md`, `docs/CHANGES_FOR_REVIEW.md`,
`tests/test_review_fixes.py`. **No changes to `config/`, `data/`, or `.env`.**

---

## 1. What changed, per finding

| ID | Class | Fix | Where | Verified |
|----|-------|-----|-------|----------|
| **B1** | missed-alarm | `SENSOR_SILENT` (a channel that stopped publishing) now always fires; only the value-flatline *guess* (`SUSPECTED_STALL`) stays behind the flag/firmware. Split `silent` vs `flat`; removed the `elif _fw: sensor_fault=[]` that nuked silent facts. | `watchdog_eval_loop()` | ✅ test |
| **B2** | missed-alarm | Systemic clusters now raise one persistent, Telegram-paged `SYSTEMIC` alert (synthetic id `__systemic__:<metric>`), resolve when cleared, and audit only on transition. Clusters are computed *before* the device loop so clustered devices aren't also paged individually. | `watchdog_eval_loop()` | ✅ test |
| **B3** | security | `html_lib.escape()` on all DB-sourced fields in `/audit` (also `/print`, admin history/config tables). Closes the unauthenticated stored-XSS via a failed-login username. | `audit_page()`, `print_report()`, admin views | ✅ test |
| **B4** | security | Refuse the committed/weak Flask `secret_key`: `_ensure_secret_key()` generates `secrets.token_hex(32)` on first boot and persists it; `WATCHDOG_SECRET_KEY` still overrides. | `_ensure_secret_key()`, `main()` | ✅ test |
| **B5** | missed-alarm | Broker-down page is now latched (fires once at ≥60 s down, re-nags every 10 min) instead of relying on a 15 s tick hitting a 5 s window. | `_watchdog_check_health()`, `mqtt_health` | code + reasoned |
| **B6** | missed-alarm | Firmware `Value Stall`/`Sensor Fault` ingested from `binary_sensor/<name>/state` (ON/OFF decoded to 1.0/0.0, stored as `diagnostic`); `_fw` verdict check made numeric. Diagnostics excluded from silent/flat/systemic/candidate queries. | `on_mqtt_message()`, `watchdog_eval_loop()` | ✅ test (⚠ see §5) |
| **B7** | false-reassurance | Wall board flash + buzzer now fire on a *new-fault* transition and auto-dismiss by timestamp, with periodic re-flash while unacked. No longer resets every 5 s poll; buzzer now actually stops. | `wallboard.js` | code (⚠ see §5) |
| **B8** | false-alarm | `OUT_OF_RANGE` dwell: a reading must stay out of range `out_of_range_hysteresis_seconds` (default 60) before it pages. Stops limit-boundary flapping. | `watchdog_eval_loop()` | ✅ test |
| **B9** | security | CSRF tokens on all state-changing requests; `snooze`/`unsnooze`/`config restore` moved GET→POST; cookie set `HttpOnly`+`SameSite=Lax`. | `_csrf_protect()`, routes, `dashboard.js` | ✅ test |
| **B10** | contention | In-loop `audit()` reuses the watchdog transaction's cursor instead of opening a second write connection. | `audit(..., cursor=)` | code |
| **B11** | false-alarm | "No data for N minutes" page latched by timestamp (first cross of 10 min, then ≤ every 30 min) — no more ~4× duplicates. | `_watchdog_check_health()` | code + reasoned |
| **B12** | correctness | Editing Telegram settings in `/admin/config` rebuilds the engine and the polling thread resumes without a restart; success message is now accurate. | `admin_config_post()`, `telegram_polling_thread()` | code |
| **B13** | wrong-signal | Wall board `systemic` flag now reflects a real same-metric cluster, not the `out_of_range` row count; `SYSTEMIC` treated as CRITICAL and sorted first. | `api_wallboard()` | code |
| **B14** | resource-leak | Telegram polling thread closes its DB connection on every path (`try/finally`). | `telegram_polling_thread()` | code |
| **B15** | security | `/print` alert rows escaped (device_id/type/message). | `print_report()` | code |
| **B16** | cosmetic | Dashboard per-sensor chips use the server's `flatline_limit` (sent in `/api/devices`) instead of a hardcoded 300 s. | `api_devices()`, `dashboard.js` | ✅ test |
| **B17** | footgun | Committed `config.yaml` ships `telegram.enabled: false`. | `config.yaml` | n/a |
| **B18** | code-smell | Removed the dead/unreachable block in `on_mqtt_message`. | `on_mqtt_message()` | compile |
| **B19** | docs | Fixed stale `_pct` key reference (now absolute `sensor_flatline_min_change`), schema/status enum comments, CHANGELOG; targeted `install.md` accuracy updates. | CHANGELOG, schema.sql, docs | n/a |
| **B20** | boot-failure *(new)* | **Found during testing:** a *fresh* DB crashed at boot because `_apply_migrations()` runs before `schema.sql` and touched `schema_meta`/`audit_logs`/`devices` before they existed. Added `schema_meta` creation + `_table_exists` guards. The live DB never hit this (its tables already exist). | `_apply_migrations()` | ✅ test |

---

## 2. Behavioral changes to be aware of before deploying

1. **More alerts once Telegram is on.** B1 + B2 restore real detections. A single sensor
   channel silent > `sensor_flatline_seconds` (900 s) now pages, and a ≥3-device metric
   cluster now pages. On the live fleet Telegram is currently off (`.env`
   `WATCHDOG_TG_ENABLED`), so nothing pages until it's enabled — but expect real volume when
   it is. The 2 genuine `STALLED` + 2 `OFFLINE` already-open alerts are unaffected.
2. **First boot regenerates the signing key → everyone re-logs in once.** The live
   `config/config.yaml` still uses the default `…CHANGE_ME` key (confirmed: it would be
   regenerated). That means the running instance is **currently forgeable** (B4), and the
   first restart on this code writes a fresh random key and invalidates existing browser
   sessions. This is a one-time, expected re-login. To avoid even that, set
   `WATCHDOG_SECRET_KEY` in `.env` before deploying.
3. **GET links for snooze/restore no longer work.** Any bookmark/script that hit
   `/snooze/...`, `/unsnooze/...`, or `/admin/config/restore/...` via GET will now 405/400.
   The dashboard UI uses POST forms with the CSRF token, so normal operation is unaffected.
4. **Wall board now stays quiet after auto-dismiss** (and re-flashes every 5 min while a
   fault is unacknowledged) instead of beeping on every poll. Needs a real-screen check (§5).

---

## 3. New / changed config keys (`config.yaml` → `watchdog:` / `telegram:` / `web:`)

| Key | Default | Meaning |
|-----|---------|---------|
| `watchdog.out_of_range_hysteresis_seconds` | `60` | Dwell before `OUT_OF_RANGE` pages (B8). `0` = old immediate behaviour. |
| `telegram.enabled` | `false` *(was `true`)* | Off until a real token/chat_id is set (B17). |
| `web.secret_key` | `CHANGE_ME_generated_on_first_boot` | Auto-generated+persisted on first boot if left as placeholder/weak (B4). |

All are hot-reloadable via `/admin/config` except `web.secret_key` (applied at boot).
Existing keys unchanged. The stale `sensor_flatline_min_change_pct` never existed in code;
the real key is the absolute `sensor_flatline_min_change` (default `0.0`).

---

## 4. Verification performed

`tests/test_review_fixes.py` (18 assertions, all passing) covers:

- **Detection state machine:** B1 single-channel `SENSOR_SILENT`; B2 cluster raise,
  per-device suppression, and clear-on-recovery; B6 firmware-stall → `SUSPECTED_STALL`.
- **Ingestion:** B6 `binary_sensor` ON → stored `diagnostic` = 1.0; classifier ordering
  (`co2` before `co`, `ground_temperature` → temperature, diagnostics → `None`).
- **OUT_OF_RANGE:** B8 dwell gating (no page on first breach; pages once dwell elapses).
- **Security:** B3 `/audit` escapes an injected failed-login username; B9 CSRF rejects a
  token-less POST (400) and accepts a valid one (302), restore is POST-only (405 on GET);
  B4 placeholder key regenerates to a 64-hex value and persists.
- **Boot:** B20 a brand-new DB initialises to schema v7 with all 11 tables, and a hashed
  admin login verifies.

Also: `python3 -m py_compile app.py` clean; `node --check` clean on both JS files.

---

## 5. Still needs live / hardware verification (cannot be done from code review)

- **B6 — firmware topic shape.** The ingest assumes `facility/<dev>/env_sensor/binary_sensor/<slug>/state`
  with an `ON`/`OFF` payload. This matches ESPHome's defaults and is marked with a `NOTE:` in
  code, but should be confirmed against **one real retained message** from an SCD30 before
  trusting it in production. If the real topic differs, only the routing prefix in
  `on_mqtt_message()` needs adjusting.
- **B7 / D4 — wall board on the real 1080p TV.** The flash/beep logic is corrected and unit
  of behaviour is sound, but it must be seen/heard on the actual kiosk. The AudioContext
  gesture limitation (§5.5) remains a browser constraint: a kiosk with no interaction stays
  visual-only until something clicks/keys the page once.
- **D5 — Telegram round-trip.** B2/B5/B11/B12 are fixed in code and the mock path is
  exercised, but the live `ack:`/`snooze:` callback round-trip still needs a real
  `bot_token`/`chat_id`.

---

## 6. How to deploy / review the changes

```bash
cd /media/yaniv/1tb_WDC/hermes_agent_projects/sensor_gaurd_rails/sensor_watchdog
git diff                       # review the working-tree changes
./.venv/bin/python tests/test_review_fixes.py   # re-run the suite

# when satisfied, rebuild the container (DB + config preserved):
HOST_UID=$(id -u) HOST_GID=$(id -g) ./ops.sh rebuild
./ops.sh status
```

Nothing is committed yet. Suggested commit grouping if you want separate commits:
(1) detection fixes B1/B2/B6/B20, (2) security B3/B4/B9/B15, (3) alerting B5/B8/B11/B12/B13,
(4) wall board B7, (5) cleanup/docs B10/B14/B16/B17/B18/B19 + tests. Or one commit — your call.

**Rollback:** `git checkout -- <files>` (changes are working-tree only). The only persistent
side effect of *running* the new code is the one-time `secret_key` regeneration in
`config/config.yaml` (§2.2) — back that file up first if you want to preserve the current key.
