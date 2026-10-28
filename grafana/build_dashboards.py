#!/usr/bin/env python3
"""Generate the PsygaBio Device Watchdog Grafana dashboards as JSON.

Dashboards are built from code rather than clicked together in the UI so they
are reviewable, diffable and reproducible — which is what a GMP change-control
story needs. Run build_dashboards.py to write the JSON, then push_dashboards.py
to upload it.

Data shape (discovered from the live bucket, 2026-10-27):
    bucket      GMP_sensor_data        org  psygabio
    measurement sensor_measurements
    tags        host, topic            field  value
    topic       facility/<device>/env_sensor/<kind>/<channel>/state
                — uniformly 6 segments across all 271 series, so splitting on
                "/" is safe: parts[1] = device, parts[4] = channel.

Telegraf does not tag per-device, so device/channel are parsed in Flux. At 271
series that costs nothing measurable (a parsed last() query returns in ~0.25s);
a Telegraf topic_parsing change would be an optimisation, not a prerequisite,
and would only apply to NEW data whereas this works on history too.

IMPORTANT Flux note: last() must come directly after range()+filter() so
InfluxDB can push it down. Putting map() first turns it into a full scan and
the query times out.
"""
import json
import os

DS = {"type": "influxdb", "uid": "aflneas0umnswd"}   # 'influxdb-1' (Flux)
BUCKET = "GMP_sensor_data"
OUT = os.path.dirname(os.path.abspath(__file__))

# Channels that are device telemetry, not environmental readings. Mirrors
# _DIAGNOSTIC_NAME_MARKERS in app.py so the two systems agree on what counts.
DIAG = ["uptime", "wifi_signal", "boot_count", "calibration", "mac_address",
        "ip_address", "ssid", "version", "last_calibrated", "compliance_note",
        "heartbeat_counter"]
DIAG_FILTER = " and ".join(
    f'not strings.containsStr(v: r.channel, substr: "{d}")' for d in DIAG)

PARSE = f'''
  |> map(fn: (r) => ({{ r with
       device: strings.split(v: r.topic, t: "/")[1],
       channel: strings.split(v: r.topic, t: "/")[4] }}))'''


def flux_freshness():
    """Every environmental channel with its last value and age in seconds."""
    return f'''import "strings"
from(bucket: "{BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "sensor_measurements" and r._field == "value")
  |> last(){PARSE}
  |> filter(fn: (r) => {DIAG_FILTER})
  |> map(fn: (r) => ({{ r with age_s: (int(v: now()) - int(v: r._time)) / 1000000000 }}))
  |> group()
  |> keep(columns: ["device", "channel", "_value", "age_s"])
  |> sort(columns: ["age_s"], desc: true)'''


def flux_device_age():
    """Per-device seconds since its most recent packet (device liveness)."""
    return f'''import "strings"
from(bucket: "{BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "sensor_measurements" and r._field == "value")
  |> last(){PARSE}
  |> group(columns: ["device"])
  |> max(column: "_time")
  |> map(fn: (r) => ({{ r with age_s: (int(v: now()) - int(v: r._time)) / 1000000000 }}))
  |> group()
  |> keep(columns: ["device", "age_s"])
  |> sort(columns: ["age_s"], desc: true)'''


def flux_metric_series(match, coarsen=8):
    """Time series for one metric family, one series per device+channel.

    The datasource caps a response at maxDataPoints * 10 points across ALL
    series combined. aggregateWindow(every: v.windowPeriod) yields roughly
    maxDataPoints points per series, so with 25-45 channels the panel blew
    straight through the ceiling and rendered an error instead of a graph.
    Coarsening the window by `coarsen` decouples per-series resolution from
    the cap: ~maxDataPoints/coarsen points each, which keeps a 45-series
    temperature panel comfortably inside it while staying adaptive to zoom.
    """
    return f'''import "strings"
from(bucket: "{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop){PARSE}
  |> filter(fn: (r) => r._measurement == "sensor_measurements" and r._field == "value")
  |> filter(fn: (r) => {match})
  |> filter(fn: (r) => {DIAG_FILTER})
  |> aggregateWindow(every: duration(v: int(v: v.windowPeriod) * {coarsen}),
                     fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{ r with _field: r.device + " / " + r.channel }}))
  |> keep(columns: ["_time", "_value", "_field"])'''


def flux_count(where, start="-30d"):
    return f'''import "strings"
from(bucket: "{BUCKET}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "sensor_measurements" and r._field == "value")
  |> last(){PARSE}
  |> filter(fn: (r) => {DIAG_FILTER})
  |> map(fn: (r) => ({{ r with age_s: (int(v: now()) - int(v: r._time)) / 1000000000 }}))
  |> filter(fn: (r) => {where})
  |> group()
  |> count(column: "device")'''


def target(flux, ref="A"):
    return {"refId": ref, "datasource": DS, "query": flux}


def stat(title, flux, x, y, w, h, unit="none", steps=None, pid=0):
    return {
        "type": "stat", "title": title, "datasource": DS, "id": pid,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(flux)],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "textMode": "auto", "colorMode": "background",
                    "graphMode": "none", "justifyMode": "auto"},
        # Flux count() over an empty set returns NO ROW, not 0, so without
        # noValue the tile reads "No data" when the honest answer is zero —
        # the one number an operator most wants to be unambiguous.
        "fieldConfig": {"defaults": {
            "unit": unit, "mappings": [], "noValue": "0",
            "thresholds": {"mode": "absolute",
                           "steps": steps or [{"color": "green", "value": None}]}},
            "overrides": []},
    }


def table(title, flux, x, y, w, h, pid=0):
    return {
        "type": "table", "title": title, "datasource": DS, "id": pid,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(flux)],
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""},
                    "sortBy": [{"displayName": "age_s", "desc": True}]},
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                    "filterable": True, "inspect": False},
                         "mappings": [],
                         "thresholds": {"mode": "absolute",
                                        "steps": [{"color": "text", "value": None}]}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "age_s"},
                 "properties": [
                     {"id": "displayName", "value": "Age"},
                     {"id": "unit", "value": "s"},
                     {"id": "custom.cellOptions",
                      "value": {"type": "color-background", "mode": "gradient"}},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": "green", "value": None},
                         {"color": "#EAB839", "value": 900},
                         {"color": "orange", "value": 3600},
                         {"color": "red", "value": 86400}]}},
                     {"id": "custom.width", "value": 120}]},
                {"matcher": {"id": "byName", "options": "_value"},
                 "properties": [{"id": "displayName", "value": "Last value"},
                                {"id": "decimals", "value": 2},
                                {"id": "custom.width", "value": 130}]},
                {"matcher": {"id": "byName", "options": "device"},
                 "properties": [{"id": "displayName", "value": "Device"},
                                {"id": "custom.width", "value": 170}]},
                {"matcher": {"id": "byName", "options": "channel"},
                 "properties": [{"id": "displayName", "value": "Channel"}]},
            ]},
    }


def timeseries(title, flux, x, y, w, h, unit, pid=0):
    return {
        "type": "timeseries", "title": title, "datasource": DS, "id": pid,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        # One series PER CHANNEL across ~31 devices, so the default ~1000
        # points per series blows past the datasource's 10k ceiling and the
        # panel errors instead of drawing. aggregateWindow follows
        # v.windowPeriod, which Grafana derives from this.
        "maxDataPoints": 1000,
        "targets": [target(flux)],
        "options": {"legend": {"displayMode": "table", "placement": "right",
                               "showLegend": True, "calcs": ["lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit, "custom": {
                "drawStyle": "line", "lineWidth": 1, "fillOpacity": 4,
                "showPoints": "never", "spanNulls": False,
                "axisPlacement": "auto", "pointSize": 4},
            "mappings": [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]}},
            "overrides": []},
    }


# Age thresholds shared by the stat tiles: green < 15m, amber < 1h, red beyond.
AGE_STEPS = [{"color": "green", "value": None},
             {"color": "#EAB839", "value": 900},
             {"color": "red", "value": 3600}]


def dash_overview():
    """Operations overview: is anything wrong, and what."""
    p = []
    i = 1
    p.append(stat("Channels reporting (< 15 min)",
                  flux_count("r.age_s <= 900"), 0, 0, 6, 4, pid=i)); i += 1
    p.append(stat("Channels stale (> 15 min)",
                  flux_count("r.age_s > 900 and r.age_s <= 3600"), 6, 0, 6, 4,
                  steps=[{"color": "green", "value": None}, {"color": "#EAB839", "value": 1}],
                  pid=i)); i += 1
    p.append(stat("Channels silent (> 1 h)",
                  flux_count("r.age_s > 3600"), 12, 0, 6, 4,
                  steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
                  pid=i)); i += 1
    p.append(stat("Channels dead (> 24 h)",
                  flux_count("r.age_s > 86400"), 18, 0, 6, 4,
                  steps=[{"color": "green", "value": None}, {"color": "dark-red", "value": 1}],
                  pid=i)); i += 1
    p.append(table("Device liveness — seconds since the device's last packet",
                   flux_device_age(), 0, 4, 10, 14, pid=i)); i += 1
    p.append(table("Channel freshness — every environmental channel, stalest first",
                   flux_freshness(), 10, 4, 14, 14, pid=i)); i += 1
    p.append(timeseries("CO2 (ppm)", flux_metric_series(
        'strings.containsStr(v: r.channel, substr: "co2") or '
        'strings.containsStr(v: r.channel, substr: "carbon_dioxide") or '
        'strings.containsStr(v: r.channel, substr: "eco2")'),
        0, 18, 24, 9, "ppm", pid=i)); i += 1
    # "temperature" already covers every temp channel on this fleet; adding a
    # bare "temp" matcher only duplicated series and doubled the point count.
    p.append(timeseries("Temperature (°C)", flux_metric_series(
        'strings.containsStr(v: r.channel, substr: "temperature")'),
        0, 27, 12, 9, "celsius", pid=i)); i += 1
    p.append(timeseries("Humidity (%)", flux_metric_series(
        'strings.containsStr(v: r.channel, substr: "humidity")'),
        12, 27, 12, 9, "humidity", pid=i)); i += 1
    return {
        "uid": "psygabio-overview",
        "title": "PsygaBio — Fleet Overview",
        "tags": ["psygabio", "watchdog"],
        "timezone": "Asia/Jerusalem",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "editable": True,
        "panels": p,
    }


def dash_wallboard():
    """Wall board: readable across a room, kiosk mode."""
    p = []
    i = 1
    p.append(stat("REPORTING", flux_count("r.age_s <= 900"), 0, 0, 8, 7, pid=i)); i += 1
    p.append(stat("STALE > 15 MIN", flux_count("r.age_s > 900 and r.age_s <= 3600"),
                  8, 0, 8, 7,
                  steps=[{"color": "green", "value": None}, {"color": "#EAB839", "value": 1}],
                  pid=i)); i += 1
    p.append(stat("SILENT > 1 HOUR", flux_count("r.age_s > 3600"), 16, 0, 8, 7,
                  steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
                  pid=i)); i += 1
    t = table("Channels needing attention", f'''{flux_freshness()}
  |> filter(fn: (r) => r.age_s > 900)
  |> limit(n: 14)''', 0, 7, 24, 15, pid=i)
    p.append(t)
    return {
        "uid": "psygabio-wallboard",
        "title": "PsygaBio — Wall Board",
        "tags": ["psygabio", "watchdog", "wallboard"],
        "timezone": "Asia/Jerusalem",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "editable": True,
        "panels": p,
    }


def dash_device():
    """Per-device drilldown — answers 'was it moving before it froze?'."""
    series = f'''import "strings"
from(bucket: "{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop){PARSE}
  |> filter(fn: (r) => r._measurement == "sensor_measurements" and r._field == "value")
  |> filter(fn: (r) => r.device == "${{device}}")
  |> filter(fn: (r) => {DIAG_FILTER})
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{ r with _field: r.channel }}))
  |> keep(columns: ["_time", "_value", "_field"])'''
    diag = f'''import "strings"
from(bucket: "{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop){PARSE}
  |> filter(fn: (r) => r._measurement == "sensor_measurements" and r._field == "value")
  |> filter(fn: (r) => r.device == "${{device}}")
  |> filter(fn: (r) => strings.containsStr(v: r.channel, substr: "heartbeat_counter"))
  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)
  |> map(fn: (r) => ({{ r with _field: r.channel }}))
  |> keep(columns: ["_time", "_value", "_field"])'''
    p = [
        timeseries("All environmental channels", series, 0, 0, 24, 11, "none", pid=1),
        timeseries("Heartbeat counter (should climb; flat = STALLED)",
                   diag, 0, 11, 24, 8, "none", pid=2),
    ]
    return {
        "uid": "psygabio-device",
        "title": "PsygaBio — Device Drilldown",
        "tags": ["psygabio", "watchdog"],
        "timezone": "Asia/Jerusalem",
        "schemaVersion": 39,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "editable": True,
        "templating": {"list": [{
            "name": "device", "label": "Device", "type": "query",
            "datasource": DS, "refresh": 1, "includeAll": False, "multi": False,
            "query": f'''import "strings"
from(bucket: "{BUCKET}")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "sensor_measurements")
  |> last(){PARSE}
  |> keep(columns: ["device"])
  |> group()
  |> distinct(column: "device")''',
        }]},
        "panels": p,
    }


if __name__ == "__main__":
    for name, d in (("overview", dash_overview()),
                    ("wallboard", dash_wallboard()),
                    ("device", dash_device())):
        path = os.path.join(OUT, f"dashboard-{name}.json")
        with open(path, "w") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
        print(f"  wrote {path}  ({len(d['panels'])} panels)")
