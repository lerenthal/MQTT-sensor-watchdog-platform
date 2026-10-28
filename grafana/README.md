# Grafana dashboards

Version-controlled Grafana dashboards for the PsygaBio Device Watchdog, built
from code rather than clicked together in the UI so they are reviewable,
diffable and reproducible — which is what GMP change control needs.

```bash
./.venv/bin/python grafana/build_dashboards.py     # write the JSON
./.venv/bin/python grafana/push_dashboards.py      # upload to Grafana
```

`push_dashboards.py` reads the Grafana service-account token from a file
**outside the repo** and never prints it. Override the location with
`GRAFANA_TOKEN_FILE`; the file only has to contain the `glsa_…` token
somewhere.

## Dashboards

| Dashboard | UID | What it answers |
|---|---|---|
| Fleet Overview | `psygabio-overview` | Is anything wrong, and what — counts by freshness, device liveness, channel freshness, CO2/temp/humidity trends |
| Wall Board | `psygabio-wallboard` | Big-type kiosk view: reporting / stale / silent, plus the channels needing attention |
| Device Drilldown | `psygabio-device` | Per-device channel history — *"was it moving before it froze?"* |

The drilldown is the one the SQLite watchdog structurally cannot provide:
`sensor_readings` holds only the latest value per channel, so there is no
history to plot (§5.3 of the original review brief). InfluxDB has it.

## Data shape

Discovered from the live bucket on 2026-10-27:

```
bucket      GMP_sensor_data          org  psygabio
measurement sensor_measurements
tags        host, topic              field  value
topic       facility/<device>/env_sensor/<kind>/<channel>/state
```

Telegraf writes the whole MQTT topic as a single `topic` tag and does **not**
tag per device, so `device` and `channel` are parsed in Flux. All 271 series
are uniformly six segments, so `strings.split(v: r.topic, t: "/")` with
`[1]` = device and `[4]` = channel is safe.

A Telegraf `topic_parsing` change would make those real tags, but it is an
optimisation, not a prerequisite: at this cardinality a parsed `last()` query
returns in ~0.25 s, and the Flux approach also works on **historical** data,
which retagging would not.

## Three traps worth knowing

1. **`last()` must come directly after `range()` + `filter()`.** Put `map()`
   first and InfluxDB cannot push the aggregate down; the query degrades to a
   full scan over millions of points and times out. This cost a 7-day
   freshness query going from "times out" to 0.25 s.
2. **The datasource caps a response at `maxDataPoints × 10` points across all
   series combined.** `aggregateWindow(every: v.windowPeriod)` produces about
   `maxDataPoints` points *per series*, so any panel with more than ~10 series
   errors instead of drawing. The metric panels therefore coarsen the window
   (`v.windowPeriod × 8`) to decouple per-series resolution from the cap.
3. **Flux `count()` over an empty set returns no row, not `0`.** Without
   `noValue: "0"` the stat tiles read "No data" when the correct answer is
   zero — the one number that must never be ambiguous on an alarm board.

## Not covered here

- **Alert rules are not provisioned yet.** The mapping from the watchdog's
  rules to Grafana alerting (`for:` as the dwell, `absent_over_time` as
  SENSOR_SILENT, grouping as the systemic rollup, silences as snooze) is in
  `docs/REVIEW_2026-10-26.md`.
- **The GMP operator audit trail does not move to Grafana.** Grafana OSS has
  no attributable operator-action log; "who acknowledged this alarm and when"
  stays in the watchdog. That is the part worth keeping.
- **Firmware `Value Stall` / `Sensor Fault` are not in InfluxDB.** Only
  `sensor` and `number` topics reach Telegraf — no `binary_sensor` — so the
  firmware's own stall verdict is available to the watchdog but not to
  Grafana alerting.
- **Clean-up:** the Grafana datasource named `influxdb` (uid
  `dflnb33ywpurka`) is broken — empty URL, health `ERROR` — and is the
  **default**, so new panels silently point at a dead datasource. These
  dashboards pin `influxdb-1` (uid `aflneas0umnswd`) explicitly. Deleting the
  broken one is recommended.
