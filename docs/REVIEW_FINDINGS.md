# Code & Functional Review — MQTT Sensor Watchdog

**Reviewed:** working tree at `HEAD = 3438105` (one commit past the brief's `68efef7`;
tree clean, `app.py` identical to the reviewed commit).
**Scope:** `app.py` (3007 lines), `dashboard.js`, `wallboard.js`, `schema.sql`,
`config.yaml`, `.env.example`, `Dockerfile`, `docker-compose.yml`, `ops.sh`, docs.
**Method:** full read of every file + targeted verification (syntax compile, grep of
every detection code path, git tracking of secrets).

The headline is uncomfortable but important:

> **Two of the six documented detection rules do not fire by default, and the one
> incident the system is proudest of catching (§7.2, a single metric going silent
> fleet-wide) would today produce no phone alert and no persistent alert — only a
> dashboard banner and a log line.**

This is not a regression you introduced carelessly; it is the *combination* of two
defensible individual decisions (disable the flatline heuristic; roll systemic faults
up instead of paging per-device) that together leave a hole. Details in §B1/B2.

---

## A. Executive summary — ranked

| # | Finding | Class | Where |
|---|---|---|---|
| B1 | Per-device `SENSOR_SILENT` **and** `SUSPECTED_STALL` never fire with the shipped config | missed-alarm | `app.py:1060-1086` |
| B2 | Systemic cluster is never paged and never persisted — only logged/audited | missed-alarm | `app.py:1238-1254` |
| B3 | Unauthenticated **stored XSS**: login username → `/audit` (runs in admin's browser) | security | `app.py:1650`, `app.py:2006-2013` |
| B4 | Committed default `secret_key` ⇒ forgeable admin session if deployed un-overridden | security | `config.yaml:54` |
| B5 | 60-second "broker unreachable" Telegram page can be silently skipped | missed-alarm | `app.py:2937` |
| B6 | Firmware `Value Stall` entity can never be ingested (dead diagnostic path) | missed-alarm | `app.py:471-476, 584-622, 671, 1046-1078` |
| B7 | Wall-board flash **and** buzzer auto-dismiss are broken; buzzer never stops | false-reassurance / D4 | `wallboard.js:101,128-138` |
| B8 | `OUT_OF_RANGE` has no hysteresis → oscillating value re-pages on every crossing | false-alarm | `app.py:1187-1226` |
| B9 | State-changing **GET** routes (config restore, snooze/unsnooze) → CSRF / prefetch | security | `app.py:1927,1939,2565` |
| B10 | `audit()` opens a 2nd write connection inside the loop's open write transaction | resource / contention | `app.py:1224,1243` |
| B11 | "No data for N minutes" Telegram alert fires ~4× per occurrence | false-alarm | `app.py:2921` |
| B12 | Telegram settings edited via `/admin/config` silently don't take effect | correctness | `app.py:384,2528-2536` |
| B13 | Wall-board "SYSTEMIC FAULT DETECTED" is driven by `out_of_range` count, mislabeled | wrong-signal | `app.py:2820,2840` |
| B14 | Telegram polling thread leaks a DB connection on its error path | resource-leak | `app.py:1286,1319` |
| B15 | `/print` alert rows render device_id/message unescaped (authed XSS) | security | `app.py:2672-2676` |
| B16 | Dashboard per-sensor chips hardcode 300 s; server uses configurable 900 s | cosmetic | `dashboard.js:44,47,379-380` |
| B17 | Committed `config.yaml` ships `telegram.enabled: true` with placeholder creds | footgun | `config.yaml:20` |
| B18 | Dead/unreachable code block (botched copy-paste) in `on_mqtt_message` | code smell | `app.py:704-719` |
| B19 | Doc drift: CHANGELOG/comment/schema describe keys & statuses that no longer exist | docs | several |

Secrets check (brief §5.12): **clean.** `config/config.yaml` and `.env` are gitignored
and **not tracked** (`git ls-files` shows neither). The real broker password lives only
in the untracked runtime `config/config.yaml`. Nothing sensitive is staged.

---

## B. Bugs — detail, reproduction, fix

### B1 — Per-device silent/stall detection is disabled by default *(missed-alarm, high)*
`app.py:1060-1086`, confirmed at `app.py:1079`.

`watchdog_eval_loop()` builds `sensor_fault` from readings whose value is stale, then:

```python
if _fw_stalled or _fw_fault:   # firmware diagnostic rows → never exist (see B6)
    sensor_fault = [...]
elif _fw:                      # any diagnostic row present → never true (see B6)
    sensor_fault = []
elif not _flatline_enabled:    # sensor_flatline_detection_enabled defaults to false
    sensor_fault = []          # ← everything collapses here, every cycle
```

`SENSOR_SILENT` / `SUSPECTED_STALL` are only ever assigned inside
`if not fault_type and sensor_fault:` (lines 1088-1108). Because `sensor_fault` is
forced empty, **neither ever fires for an individual device.** The brief treats these
as different: §5.1 only disables the *flatline* (`SUSPECTED_STALL`) heuristic, while
§4.3 and the dashboard still promise that "a dead channel reports `SENSOR_SILENT`."
It does not. A channel that **stops publishing entirely** (the config/bus/power failure
`SENSOR_SILENT` was built for) is invisible unless ≥3 devices lose the *same* metric in
the same window (systemic path, B2).

**Repro:** unplug one SCD30's CO₂ feed while temp/humidity keep flowing. Device stays
`ONLINE`, heartbeat ticks, no alert, no page — forever.

**Fix:** decouple the two rules. `SENSOR_SILENT` ("no packets at all, `silent_s >
limit`") is a *fact*, not a guess — it should fire regardless of `_flatline_enabled`.
Keep only the value-flatline branch (`SUSPECTED_STALL` where packets arrive but value is
frozen) behind the flag:

```python
silent = [r for r in sensor_fault if (r["silent_s"] or 0) > flatline_limit]
flat   = [r for r in sensor_fault if (r["silent_s"] or 0) <= flatline_limit]
if not _flatline_enabled and not _fw:
    flat = []          # suppress the guess, keep the fact
sensor_fault = silent + flat
```

### B2 — A systemic cluster is detected but never escalated *(missed-alarm, high)*
`app.py:1238-1254`.

When `_detect_systemic_faults()` returns clusters, the loop does exactly three things:
`log.warning`, `audit("SYSTEMIC_FAULT", …)`, and `UPDATE alerts SET resolved = 1` on the
per-device rows. It **never** calls `_raise_or_nag`, never inserts a cluster alert row,
never sends Telegram. So a systemic event yields:

- a dashboard banner (computed live in `/api/devices`, only if someone is looking), and
- an audit row re-written **every 30 s** for the whole outage (feeds §5.10 unbounded growth).

The §7.2 incident — CO₂ silent on 17 devices while temp/humidity flow — is precisely this
path: devices are not `OFFLINE` (recent packets), per-device `SENSOR_SILENT` is off (B1),
and the `_watchdog_check_health` "no data" page never fires because `MAX(ts)` across all
readings is still fresh. **Net: the incident the brief celebrates as caught would not
reach a single phone today.** That defeats D5, the system's reason to exist (§1.1).

**Fix:** raise one persistent, nagging alert per cluster and page it, e.g.
`_raise_or_nag(cursor, f"__systemic__:{sensor_type}", "SYSTEMIC", summary, nag_interval, now)`
with a synthetic device id, and only audit on state *transition* (new cluster / cleared),
not every cycle.

### B3 — Unauthenticated stored XSS via login → `/audit` *(security, high)*
`app.py:1650` writes `details=f"username={username!r}"` from the raw, **unauthenticated**
login form. `/audit` (`app.py:2006-2013`) renders `username` and `details` with an
f-string and **no escaping** (`html_lib` is imported and used elsewhere, just not here):

```python
f'<td>{r["username"] or ""}</td>'
f'<td class="muted">{r["details"] or ""}</td>'
```

`!r` is Python `repr`, which does not HTML-escape `<` `>`.

**Repro (no auth needed):**
```bash
curl -s -X POST http://host:5050/login \
  --data-urlencode 'username=<img src=x onerror=alert(document.cookie)>' \
  --data-urlencode 'password=x'
```
Next time an admin opens **Audit Log**, the payload executes in their session. On a GMP
system the audit trail is exactly where a tamperer wants code to run.

**Fix:** `html_lib.escape()` every DB-sourced field in `/audit` (and as defense-in-depth,
in `/print` B15 and the admin history/config-history tables where `saved_by`/`note` are
printed raw at `app.py:2328,2398-2399`). Device ids and sensor names are equally
attacker-controllable by anyone who can publish to the broker.

### B4 — Committed default `secret_key` enables admin session forgery *(security, high in prod)*
`config.yaml:54` → `secret_key: "mushroom_facility_secure_key_CHANGE_ME"`. The install
path (brief §8.2, `.env.example` all-commented) never forces an override, so a deploy that
edits broker/auth but not `web.secret_key` signs Flask session cookies with a
**publicly-known** key. An attacker forges `{"username":"yaniv","role":"admin"}`, signs it
with the repo's key, and is admin — no password, bypassing the whole auth table.

**Fix:** refuse to boot when `secret_key` is empty / contains `CHANGE_ME`, **or**
auto-generate `secrets.token_hex(32)` on first boot and persist it (same mechanism as
`_ensure_password_hashes`). Mark the cookie `Secure`/`HttpOnly`/`SameSite=Lax` once TLS
lands (§5.8).

### B5 — The 60-second broker-down page can be skipped *(missed-alarm, medium)*
`app.py:2937`: `if 60 <= downtime < 65 or int(downtime) % 600 == 0:`. The health job runs
every **15 s** (`app.py:2996`). The trigger window is **5 s** wide and unaligned to the
tick clock, so if ticks land at `downtime=58` then `73`, the window `[60,65)` is jumped —
the first "broker unreachable" page is **never sent** (~⅔ of outages by arc length). The
`% 600 == 0` fallback needs `int(downtime)` to hit an exact multiple at a tick (≈1 s
window) — also unreliable. This is the §7.1 alarm; it should be the most dependable one.

**Fix:** stop encoding "already paged" as a time coincidence. Keep an
`alerted_down` flag in `mqtt_health`, set it when `downtime >= 60` and unsent, clear it on
reconnect; re-nag on a real interval (`now - last_nag > 600`). Same pattern fixes B11.

### B6 — Firmware `Value Stall` can never reach the watchdog *(missed-alarm; answers §6 Q2)*
Two independent blocks, either sufficient:

1. **Topic routing** (`app.py:584-622`): ESPHome publishes binary sensors to
   `…/env_sensor/binary_sensor/<name>/state`. The parser only accepts `sub[0] in
   {status, heartbeat_counter, sensor}`; `binary_sensor` hits the final `else: return`.
2. **Payload parse** (`app.py:671`): even if it were routed as `sensor/…`, the branch does
   `val = float(payload_str)` first and `return`s on `ValueError`. `"ON"`/`"OFF"` fail
   `float()`, so `_is_useful_diagnostic()` (line 688) is never consulted.

Therefore `_USEFUL_DIAGNOSTICS`, `_is_useful_diagnostic`, the `sensor_type="diagnostic"`
store, and the entire `_fw_stalled`/`_fw_fault` block (`app.py:1046-1078`) are **dead
code**. The firmware's authoritative verdict is silently discarded. This is also *why* B1
can't be rescued by firmware data.

**Answer to §6 Q2 — it is a Python-side topic/parse gap, not a firmware change.** Fix in
`on_mqtt_message`: accept `sub[0] == "binary_sensor"`, and for the stall/fault entities map
text payloads (`on/off/true/false`) to a stored diagnostic value *before* the numeric
parse. Note the entity name has a space ("SCD30-2 Value Stall") but ESPHome slugifies the
topic segment to `scd30-2_value_stall`, so `_is_useful_diagnostic` ("stall"/"fault")
already matches on the topic form — only the routing/parse order blocks it. The entity name
space is a red herring for the *topic*; it only matters if you ever match on the friendly
name. **Verify against one real retained message** before trusting this.

### B7 — Wall-board flash & buzzer never auto-dismiss (and barely flash) *(false-reassurance; D4)*
`wallboard.js`. CSS flashes only `.stage.alarming:not(.auto-dismissed)` (`app.py:2772`).
In `render()` (line 101) the class is reset to `stage alarming` (flashing) and then, line
133, `auto-dismissed` is **added immediately** — synchronously, before any paint — so the
background flash is never visible. The 60 s `setTimeout` (line 134) *removes* it, i.e. it
would *start* flashing at 60 s, but `poll()` re-renders every 5 s and re-adds the class, so
net: effectively static.

Worse for audio: `beep(ALARM.minFlashCount)` runs on **every** 5 s poll while faults exist
(line 128), independent of `beepTimer`. `stopBeep()` only clears the interval timer, so the
per-poll beep keeps firing — **the buzzer never auto-dismisses; it beeps every 5 s forever.**
That inverts §5.6's stated behavior. On a kiosk TV, either it's annoyingly perpetual (if
audio got armed) or silent (if not — the known gesture problem, §5.5).

**Fix:** drive flash/beep off a *new-fault* transition (diff the fault set against the last
poll), not off every render; implement auto-dismiss with a timestamp
(`if Date.now() - firstSeen < autoDismissMs`) checked inside `render`, and move the
one-shot `beep()` into the same transition guard. Re-flash periodically while unacked
(§5.6) by resetting `firstSeen` on a new fault only.

### B8 — `OUT_OF_RANGE` flaps without hysteresis *(false-alarm; answers §6 Q9)*
`app.py:1187-1226`. Each cycle: `breach and not was_open` → insert + `_raise_or_nag`
(Telegram); `not breach and was_open` → resolve. A value oscillating across the limit
(sensor noise at a setpoint boundary) therefore raises→resolves→raises, **paging on every
upward crossing** and spamming `OUT_OF_RANGE_CLEARED` audits. For a GMP system whose
credibility depends on not crying wolf (§9), this is the classic flapping alarm.

**Fix:** add dwell/hysteresis: require `breach` to persist ≥ N seconds (e.g. store
`entered_at`, only `_raise_or_nag` once `now - entered_at > hysteresis_s`), and add a
deadband on clear (value must return inside `limit ± margin`). Make both configurable.

### B9 — State-changing GET routes *(security, medium)*
`/snooze/<id>/<h>` (`:1927`), `/unsnooze/<id>` (`:1939`) and especially
`/admin/config/restore/<version_id>` (`:2565`) mutate state on **GET**. A GET is
prefetchable and triggerable cross-site (`<img src=".../admin/config/restore/3">`). The
restore link's `onclick=confirm()` is client-side only and does nothing for a crafted
request. Combined with cookie auth and no CSRF (B3's cousin), an admin who loads a hostile
page can have config silently rolled back.

**Fix:** convert these to POST forms and add a CSRF token (Flask-WTF or a signed
double-submit token). `/api/restart` is already correctly POST.

### B10 — `audit()` contends with the loop's open write transaction *(resource/contention; answers §6 Q3)*
`app.py:1224` (`OUT_OF_RANGE_CLEARED`) and `:1243` (`SYSTEMIC_FAULT`) call `audit()`, which
opens **its own** connection and `commit()`s — while the loop's `conn` holds an *uncommitted*
write transaction (writes at `:1196-1223`, committed only at `:1256`). SQLite allows one
writer; the audit connection blocks on the loop's write lock up to `busy_timeout` (15 s) or
drops the row (`audit()` swallows the exception at `:249`). This re-introduces exactly the
§7.3 class ("two writers, one process"). It hasn't bitten because `OUT_OF_RANGE` is rare
(one threshold configured) and Telegram is off, but it is latent.

**Fix:** pass the live `cursor` into `audit()` for in-loop calls (write on the same
transaction), or buffer in-loop audit rows and flush them after the final `commit()`.

**(§6 Q4 answer:** the three extra read connections (`_kd`, `_rc`, `_sc`) are **sound and
intentional** — they are the correct fix for the §7.3 deadlock (a second cursor on the write
connection). They're opened and closed sequentially, cost ~3 PRAGMAs each, negligible at a
30 s cadence. The only genuine connection problem in the loop is `audit()` above.)

### B11 — "No data for N minutes" pages ~4× each time *(false-alarm, low)*
`app.py:2921`: `if mins % 30 == 0 or (600 < age < 660):`. With a 15 s job, `mins` stays
constant for a whole minute, so `mins % 30 == 0` is true on ~4 consecutive ticks → 4
Telegram messages, contradicting the "at most once every 30 minutes" comment. Same
root cause as B5. **Fix:** the `alerted`-flag pattern from B5.

### B12 — Telegram config edits via the UI silently no-op *(correctness, medium)*
`telegram_engine` is constructed once at import (`app.py:384`). `/admin/config` apply
hot-reloads the `config` dict (`:2528-2533`) but never rebuilds `telegram_engine`, yet the
success flash says *"everything else reloads live"* (`:2536`). An operator who pastes a real
`bot_token`/`chat_id` or flips `enabled` through the editor sees "saved and applied" and
believes alerting is on — it is not until a restart. Given D5 is the riskiest surface, this
is a trap on the very screen used to arm it.

**Fix:** after writing config, re-instantiate `telegram_engine` (and restart the polling
thread) when `telegram.*` changed; or change the flash to say Telegram/MQTT need a restart.

### B13 — Wall-board "systemic" flag is the wrong metric *(wrong-signal, low-medium)*
`app.py:2820`: `sysrows = SELECT COUNT(*) FROM out_of_range WHERE resolved=0`; returned as
`"systemic": sysrows > 0` (`:2840`) and rendered as "SYSTEMIC FAULT DETECTED"
(`wallboard.js:121`). That lights up for any out-of-range breach, **not** for a
`_detect_systemic_faults` cluster. The dashboard uses the real detector; the wall board
conflates two concepts. **Fix:** compute real clusters for the wall feed (same helper), or
rename the flag to `out_of_range`.

### B14 — Telegram polling thread leaks a connection on error *(resource-leak, low)*
`app.py:1286` opens `conn` inside the `for update` loop; if anything between there and the
branch `close()` raises, the outer `except` (`:1319`) logs and sleeps without closing. Over a
flapping broker this leaks connections (the §7.3 "47 fds" shape). Low today (Telegram off by
default). **Fix:** wrap the per-update body in `try/finally: conn.close()`.

### B15 — `/print` renders alerts unescaped *(security, medium — authed)*
`app.py:2672-2676`: `a["device_id"]`, `a["alert_type"]`, `a["message"]` are interpolated
without `html_lib.escape` (the device *rows* above them are escaped — inconsistent).
`message` embeds `sensor_name`, which is attacker-controllable via MQTT. **Fix:** escape
these three fields.

### B16 — Dashboard chip thresholds hardcoded *(cosmetic, low)*
`dashboard.js:44,47,379-380` flag "silent/frozen" at a literal `> 300`, while the server's
`sensor_health` counts use configurable `sensor_flatline_seconds` (900 default,
`app.py:1850-1852,1897`). With the shipped config the per-sensor chips disagree with the
status badge for 300–900 s. **Fix:** pass `flatline_limit` into the JSON and use it in JS.

### B17 — Template ships Telegram "enabled" with placeholders *(footgun, low)*
`config.yaml:20` `enabled: true` plus placeholder token/chat. `TelegramEngine.__init__`
computes `enabled = bool(enabled) and bool(token) and bool(chat_id)` — placeholders are
truthy, so a fresh deploy that forgets `.env` runs the poller against a bogus token
(endless failed `getUpdates`) and attempts real sends. The live `.env` here sets
`WATCHDOG_TG_ENABLED` so this instance is fine, but the committed default is wrong.
**Fix:** ship `enabled: false`.

### B18 — Dead/unreachable block in `on_mqtt_message` *(code smell, low)*
`app.py:704-719`: after `if sensor_type == "battery" and val == 0.0: return`, lines
705-713 (a duplicated comment block + an `if _is_useful_diagnostic…/else: return`) sit
*inside* that `if`, after the `return` — unreachable. Lines 714-719 (`if sensor_type is
None:` + a stray `conn.close()`) are also unreachable because `sensor_type` is guaranteed
non-`None` by line 680. Harmless (verified: file compiles) but it's a clear sign of a
botched edit and should be deleted so the real control flow is readable.

### B19 — Documentation drift *(docs)*
- CHANGELOG (`CHANGELOG.md:22-24`) and the inline comment (`app.py:720-727`) describe
  `sensor_flatline_min_change_pct` default `0.1`/"0.1%"; the code/config use the **absolute**
  `sensor_flatline_min_change` default `0.0` (`app.py:631`, `config.yaml:41`). The percentage
  key no longer exists.
- `schema.sql:15,66` comment `status` as only `ONLINE/STALLED/OFFLINE` and `alert_type` as
  `OFFLINE/STALLED`; code adds `DEGRADED/SENSOR_SILENT/SUSPECTED_STALL/OUT_OF_RANGE`.
- `schema.sql` is not the whole schema — `known_degraded`, `sensor_thresholds`,
  `out_of_range` exist only via `_apply_migrations()`. `docs/architecture.md:91` says "three
  tables plus two" then lists eight, and its Dockerfile `COPY` list omits `wallboard.js`.
- `docs/install.md` still documents the pre-container workflow (§5.7 — confirmed).

---

## C. Answers to the brief's §6 questions

1. **Server-side "stable room vs frozen sensor"?** Yes, partially, and it's worth doing
   because B1+B6 leave you with *no* flatline coverage at all. Single best signal:
   **per-channel rolling variance / distinct-value count over a window**, not last-change
   age. A stable room still shows sensor quantization dither (±0.1 °C, ±1 ppm) and
   occasional 1-LSB moves; a dead chip emits a *bit-identical* value every cycle. Track a
   small ring of recent values per channel (or `COUNT(DISTINCT value)` over the last K
   readings if you add the history table in §5.3) and alarm when distinct-count collapses to
   1 across many publish intervals **and** the publish cadence is still healthy. Cross-sensor
   correlation (temp flat while co-located humidity moves) is a good secondary. This keeps
   the "fact" (`SENSOR_SILENT`) always-on and makes the "guess" (`SUSPECTED_STALL`)
   defensible — see B1.
2. **`Value Stall` not captured — Python or firmware?** **Python side** (B6). It's a
   topic-routing + parse-order gap: `binary_sensor/…` is dropped before classification, and
   the numeric parse rejects `ON/OFF` before `_is_useful_diagnostic` runs. The entity-name
   space doesn't matter for the topic (ESPHome slugifies it). No firmware change required,
   but confirm against one real retained message.
3. **Connection leaks / two cursors on an open write txn?** The historical leaks are fixed;
   `on_mqtt_message` is now `try/finally`-clean. Two live issues remain: **B10** (`audit()`
   second write connection during the loop's open transaction) and **B14** (telegram thread
   leak on error). The web handler at `app.py:2258` (`cand = conn = get_db()` with no
   `try/finally`) leaks if that query raises — minor.
4. **Three read connections per cycle — thrashing?** No. See B10's parenthetical: it's the
   correct, cheap fix for the §7.3 deadlock. Leave it.
5. **Classification ordering on every path, incl. diagnostics?** Ordering is correct where
   it runs (`_classify_sensor` rejects diagnostics first, matches `co2` before `co`,
   `temperature` last — verified). **But** the diagnostic *capture* path is unreachable
   (B6/B18): the `sensor_type = "diagnostic"` assignment after the placeholder guards sits
   after a `return` and behind a `float()` that already rejected the text payload. So the
   "ordering" you're worried about on the diagnostic branch never executes. Fix B6/B18 and
   re-verify.
6. **Split the 3007-line file?** Worth it, and it need not break single-file deploy — ship a
   small package copied whole into the image. Natural seams, low-risk order: `db.py`
   (`get_db`, migrations, `audit`), `mqtt_ingest.py` (`on_mqtt_message`, classify tables),
   `detect.py` (`watchdog_eval_loop`, `_detect_systemic_faults`, `_raise_or_nag`),
   `telegram.py`, `web/` (routes + the big `BASE_CSS`/HTML strings, which are ~700 lines of
   non-logic noise today). Pull the inline CSS/HTML into template/static files first — that
   alone removes ~⅓ of the file and makes the Python reviewable. **Effort: M, value: high
   for reviewability**, but do it *after* B1/B2 — don't refactor around a known hole.
7. **`alerts … resolved=0` index?** You already have the partial index
   `idx_alerts_active … WHERE resolved=0` (`schema.sql:98-100`). At 31 devices / single-digit
   open alerts it's a non-issue; the partial index already covers the dashboard poll. No
   materialized view needed at this scale. Revisit only if the fleet grows 10×.
8. **Does Telegram long-poll starve Flask?** No. `getUpdates` blocks in `requests` (releases
   the GIL during I/O), and Flask's dev server is threaded. The real coupling is SQLite
   writes (B10), not CPU. The genuine risk is the opposite: `app.run()` is the **Werkzeug dev
   server** — not suitable for unattended production (no graceful worker recycling, single
   process). See §E.
9. **`OUT_OF_RANGE` hysteresis?** Yes — currently absent and it flaps (**B8**). Add dwell +
   deadband.

---

## D. What §5 misses (gaps not in the brief's own list)

- **The §4.3 rule table is wrong about what's live.** `SENSOR_SILENT` is documented as an
  active rule but is disabled with the rest of the flatline logic (B1). §5.1 only owns up to
  `SUSPECTED_STALL`. This is the single most important undocumented gap.
- **Systemic faults don't page or persist (B2).** §5 never states that the systemic rollup
  has no escalation path — readers assume §7.2 is covered going forward. It isn't.
- **The Docker `HEALTHCHECK` is the data-flow check, not a liveness check.** `/health`
  returns 503 during a broker outage (by design, §7.1). Used as the container health probe
  (`Dockerfile:44`, `docker-compose.yml:42`) this marks the container **unhealthy exactly
  when it's doing its job**. `restart: unless-stopped` won't restart on unhealthy (only on
  exit), so it's safe today — but any add-on autoheal/orchestrator would restart the watchdog
  mid-outage, wiping in-memory `mqtt_health` and re-running the cold-start rebase. Split
  liveness (`/health?mode=live`) from readiness.
- **Secret-key forgery (B4)** is a bigger security item than §5.12's "is anything staged"
  (which is clean). §5.12 asks the smaller question.
- **No CSRF anywhere** and **state-changing GETs (B9)** — not mentioned.
- **Audit trail is append-only but not tamper-evident.** For GMP, consider a hash chain
  (each row stores `H(prev_hash || row)`) so deletion/edit is detectable; today a DB-file
  writer can rewrite history silently. Pairs with §5.10.
- **No input bound on MQTT-created device ids / sensor names.** A broker publisher can create
  unbounded `devices`/`sensor_readings` rows (`INSERT OR IGNORE INTO devices` on any topic)
  and inject the XSS payloads of B3/B15. Consider an allowlist or a cap.

---

## E. Production-readiness gap list (D1–D8)

| Req | State | Gap to close before production |
|---|---|---|
| D1 container | ✅ | — |
| D2 port 5050 | ✅ | — |
| D3 LAN reachable | ⚠ | Still unverified from a second host (brief's own note). Trivial to test. |
| D4 wall display | ❌ | **B7** — flash/beep auto-dismiss broken, buzzer perpetual, audio-arming (§5.5) unsolved. Must be seen on the real TV at 1080p before sign-off. |
| D5 Telegram | ❌ | Never exercised live (§5.4) **and** B2 (systemic not paged), B5 (broker-down page skippable), B11 (4× spam), B12 (UI edits no-op). Fix B2/B5/B12, then round-trip `ack:`/`snooze:` callbacks once with a real token. |
| D6 unattended/reboot | ⚠ | Replace the Werkzeug dev server with a WSGI server (`waitress`/`gunicorn`); split the healthcheck (above); confirm `restart: unless-stopped` + WAL across a real reboot; add watchdog-self-failure alerting (APScheduler job death, thread death) and disk-space monitoring (brief §6 Q10). |
| D7 config/DB persist | ✅ | Ownership model (§6 Q11): host `./data`/`./config` must be owned by `HOST_UID:HOST_GID`; the app *writes* `config.yaml` (hashing, save, restore) so the config bind mount must be writable, not read-only. Document this. |
| D8 install docs | ❌ | `docs/install.md` rewrite for the container/`ops.sh` flow (§5.7). |

**Other must-dos for months-unattended:** schedule `ops.sh backup` (cron/systemd timer —
exists but never runs, §6 Q10); add `audit_logs` retention/export (§5.10); log the config
pruning (§5.11); set `SESSION_COOKIE_*` flags behind the TLS reverse proxy (§5.8);
`docker compose` `env_file` may error if `.env` is absent on older Compose — `ops.sh` works
around it by `touch`ing one, but document the minimum Compose version.

**§6 Q12 (restart + bind-mounted SQLite across reboots/upgrades):** safe *in principle* —
WAL + a single process + a bind mount survive restarts. The caveats are (a) an unclean kill
leaves `-wal`/`-shm` that SQLite recovers on next open (fine), (b) `ops.sh backup`'s
`VACUUM INTO` from the host reads the live WAL consistently (fine), and (c) never run a
second writer (e.g. host `sqlite3` write, or a 2nd container) against the same file — that's
the §5.9 boundary. So: safe, with "single writer" as the hard invariant.

---

## F. Quick wins (high value / low effort), in order

1. `html_lib.escape` the three raw-render sites (B3, B15, admin tables) — ~1 h, kills the
   unauth XSS.
2. Boot-refuse / auto-generate `secret_key` (B4) — ~1 h.
3. Decouple `SENSOR_SILENT` from the flatline flag (B1) — ~2 h, restores a whole detection rule.
4. Page + persist systemic clusters (B2) — ~2–3 h, closes the §7.2 hole.
5. Replace the time-window alert guards with an `alerted` flag (B5, B11) — ~2 h.
6. Ship `telegram.enabled: false`; add dwell to `OUT_OF_RANGE` (B17, B8) — ~2 h.
7. Fix the wall board (B7) and verify on the real screen (D4) — ~half a day incl. hardware.
8. Delete the dead block (B18) and fix the doc drift (B19) — ~1 h.

Then: the test suite (§5.13). Highest-leverage targets, in priority: `_classify_sensor`
(table-driven, pure — trivial to test and high-value), the detection state machine in
`watchdog_eval_loop` (feed a fake DB, assert fault_type transitions — this is where B1 would
have been caught), and the MQTT topic parser. These three are pure-ish and cover the bugs
that actually bit you.
