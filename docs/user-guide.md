# User Guide

A walkthrough for the operator / QC who logs into the dashboard and uses
it day-to-day. No programming background required.

## Logging in

Point a browser at `http://<host>:5050/`. The login form appears.

- **Username** — your assigned operator name (e.g. `yaniv`).
- **Password** — given by your admin.

Sessions last 12 hours of inactivity. To log out immediately, click
**Logout** in the top-right corner of any page.

If you forget your password, contact your admin. The admin resets it via
`config/config.yaml` (see [`docs/security.md`](security.md)).

## Dashboard (Devices tab)

The default landing page after login.

### Top bar

- **MQTT: OK / DOWN** badge — broker connection state. If you see **DOWN**,
  no data is being received; check the broker with your admin.
- **Auto-refresh: Ns** — the dashboard polls the API every N seconds.
- **Search box** — type to filter the devices table by device ID, location,
  role, status, or sensor values. There's a 200ms delay so typing stays
  smooth.
- **🖨 Print Report** — opens the printable status report.

### Devices table

| Column | What it shows |
|---|---|
| **Device** | The MQTT topic's second-level name (e.g. `sht41-1`). |
| **Location / Role** | The human-readable location and the role from `device_metadata`. Empty until the admin uploads a CSV. |
| **Status** | `ONLINE` (green), `DEGRADED` (purple), `STALLED` (orange), `SENSOR_SILENT` (cyan), `SUSPECTED` (yellow), `OFFLINE` (red). |
| **Last Seen** | When the most recent MQTT packet arrived. |
| **Heartbeat** | The last heartbeat-counter value the device published. |
| **Temp (°C)** | The latest temperature reading. |
| **Humidity (%)** | The latest humidity reading. |
| **CO2 (ppm)** | The latest CO₂ reading. |
| **Sensors** | Count of distinct sensor readings received. Click ▸ to expand a list. |
| **Snooze** | If active, shows *Snoozed until ...* and a snooze count. Empty means the device is actively monitored. |
| **Actions** | Snooze / Unsnooze dropdown, Restart button. |

#### Sort

Click any column header to sort by that column. Click again to reverse.
Sort state is **persisted in your browser** (localStorage) so reloading or
switching tabs preserves it.

#### Search

Type in the search box. The filter runs across every column value and the
full row text. Useful for finding one device in a list of 30+.

#### Expand "Sensors"

Click the **▸** next to a sensor count to expand a panel below the row.
You'll see one line per sensor reading:

```
sht41-1_temperature / temperature: 22.50
sht41-1_humidity   / humidity:    55.40
sht41-1_co2        / co2:       920.00
```

Click **▾** again to collapse. Multiple rows can be open simultaneously.

### Actions per device

#### Snooze

Click **Snooze ▾** then choose **1 Hour**, **8 Hours**, **24 Hours**, or
**7 Days**. The snooze suppresses new Telegram alerts for that period.

- Useful during planned maintenance.
- Snoozes the *device*, not the *fault type* — both STALLED and OFFLINE
  alerts for that device are silenced until the timer expires.
- Audit row: `SNOOZE / <duration>h` with your username and the device ID.

#### Unsnooze

When a device is currently snoozed, the **Snooze** button is replaced by
**Unsnooze**. Click it to resume monitoring immediately. Audit row:
`UNSNOOZE` with your username.

#### Restart

Click **Restart**. The watchdog publishes a `PRESS` message on
`facility/<device>/env_sensor/button/restart/cmd`. Your ESPHome device's
"restart" button should be listening to that topic. The dashboard expects
this within milliseconds; success means the device's MQTT traffic resumes
within ~10s of clicking.

Audit row: `RESTART_SENT / Published to <topic>` with your username.

#### Daily rhythm

A typical operator's day on the dashboard:

1. Log in.
2. Glance at the status column. Any orange or red badge is your cue.
3. For each fault:
   - Read the audit log (`Audit Log` tab) to see who already looked at it.
   - Click into the device's Sensors panel to see whether the readings are
     plausible.
   - If it's a planned condition (mid-cleaning, expected offline), Snooze.
   - If it's unexpected, click Restart. Wait 30s for new packets. If the
     device still shows OFFLINE, escalate to facilities with the device ID
     and the last time it was seen.

## Audit Log tab

A chronological list of every action, newest first. Columns:

- **Time** — server-side timestamp.
- **User** — who did it (or `anonymous` for Telegram button actions whose
  chat isn't tied to an operator account).
- **Action** — `LOGIN`, `LOGOUT`, `LOGIN_FAIL`, `SNOOZE`, `UNSNOOZE`, `ACK`,
  `RESTART_SENT`, `CONFIG_SAVE`, `CONFIG_ROLLBACK`, `CSV_UPLOAD`.
- **Device** — the device ID, if applicable.
- **Details** — free-text notes the action itself recorded.

200 rows at a time. For forensic-level access, run from the host shell:

```bash
./ops.sh shell
sqlite3 /app/data/sensor_watchdog.db \
  "SELECT timestamp, username, action, device_id, details \
   FROM audit_logs ORDER BY id DESC LIMIT 1000;"
```

The audit log is **append-only**. There is no UI to delete rows.

## Admin Config tab

This tab is only visible to users with the `admin` role. It exposes:

### Configuration editor

The full `config.yaml` content in a textarea. Press **Preview Diff** to
see what will change before you commit it. Press **Save & Apply** to write
to disk and reload the running configuration.

A history table below tracks every save. **Download** to get a `.yaml`
copy of any past version. **Restore** to make that past version live
again — the current version is auto-snapshotted before being overwritten.

Live reload works for: timeouts, snooze presets, auth users, telegram
credentials, MQTT password. **Restart** is required for: broker address,
broker port, web host/port, web secret key. The button reads
*"host/port changes need a full restart"* when relevant.

### Sensor metadata (CSV) upload

Upload a CSV mapping each `device_id` to its location and role. The card
text contains a sample CSV. Format reminders:

```csv
device_id,location,role
sht41-1,Spawn Incubator A,producer
```

- Header row required. Extra columns are ignored.
- An upload that lacks a `device_id` column is rejected before any row is
  written.
- Quote fields containing commas: `"Fruiting, North"`.

Existing rows are **upserted**, not duplicated. Uploads are append-only in
the audit log; the data is overwritten in place.

A complete history of uploads is at `/admin/sensors/history`.

## Print Report tab

A printable view of the current state. Click **🖨 Print Report** from the
dashboard or the **Print Report** tab.

- Facility name and location in the header.
- "Generated by" line: the username and role you signed in with.
- "Generated at": server timestamp at the moment the page rendered.
- Devices table includes location and per-sensor readings.
- Active alerts table.
- Two signature lines at the bottom (operator + reviewer).

The on-screen "🖨 Print this report" button invokes your browser's
print-to-PDF or paper flow. The `@media print` CSS removes nav and chrome
so the printed page is paper-friendly.

## Telegram bot interaction

If Telegram is enabled (`telegram.enabled: true` in config, and the bot
token is correct), an alert to a faulted device is a Telegram message like:

```
🚨 FACILITY SENSOR ALERT 🚨

Device: sht41-7
Type: STALLED
Details: Heartbeat counter frozen for 187s.
Time: 2026-10-21 14:32:11

⚠️ This notification will repeat until acknowledged or snoozed.
```

Below the message are four buttons:

- **🔕 Ack (Silence)** — silence the alert (don't snooze; you'll be
  notified again when the device goes back into fault).
- **💤 Snooze 1h / 8h / 24h** — set a snooze timer.

You may also receive **MQTT broker unreachable** notifications when the
broker has been down for more than a minute; those re-send every 10
minutes until reconnect.

## Things you should never need to do (and shouldn't try)

- Edit files inside the container. The container isephemeral state; the
  bind-mounted host directory is what matters.
- Restart the host. The watchdog is set to `restart: unless-stopped`.
  Host reboots don't kill it.
- Modify the database directly. Use the web UI or CSV uploads.
- Share your password. Each operator has their own account; the audit log
  relies on per-user attribution.

## Common questions

**Q: Why does my dashboard show STALLED for a device whose heartbeat exists in Home Assistant / InfluxDB?**
A: This was a real bug, fixed in the current build. Devices publish their
heartbeat under a name like `sht41-4_heartbeat_counter`, and the watchdog's
metric filter used to discard anything that wasn't temperature/humidity/
CO₂/light/pressure — so the heartbeat was never recorded, and the device
was permanently marked `STALLED`. Confirm the fix took effect by opening
the device's **Sensors** drilldown: you should see a
`…_heartbeat_counter` line with a rising value. If it's missing, check
`./ops.sh logs` for `on_mqtt_message failed`.

**Q: What's the difference between STALLED, SUSPECTED and SILENT?**
A: Three different faults, each pointing somewhere different:

- **STALLED** — the device publishes a heartbeat counter, but the counter's
  *value* has stopped changing. The device is on the network but its
  health loop isn't ticking.
- **SILENT** (`SENSOR_SILENT`) — one specific sensor channel has stopped
  publishing entirely, while the other channels on that device keep
  working. Almost always a wiring, power, or per-sensor config problem.
  The alert names the exact sensor.
- **SUSPECTED** (`SUSPECTED_STALL`) — that channel is still publishing,
  but the number hasn't moved. Either a dead sensor chip, or a room that
  has genuinely stabilised (a sealed CO₂ chamber will sit at one ppm
  value for hours — this is a *suspected* stall precisely because we
  can't tell those apart from the outside).

**Q: I get SUSPECTED_STALL alerts on a room that's just steady. What now?**
A: Raise `watchdog.sensor_flatline_seconds` at `/admin/config` (300 s is
the default; try 900 or 1800 for rooms that plateau), or clear the
current one with `POST /device/<id>/reset_stale` until the value moves
again.

**Q: What does DEGRADED mean?**
A: The device is still publishing, but at least one of its sensor channels
failed a per-sensor check. Expand the **Sensors** column to see which one.
The device isn't down — one part of it is unhappy.

**Q: Can I restart a device from the dashboard?**
A: Yes — the red **Restart** button in the Actions column publishes `PRESS`
to `facility/<device>/env_sensor/button/restart/cmd`. This only works if
the ESPHome node has an MQTT button component listening on that topic; if
it isn't configured, the publish goes nowhere and you'd need physical
access instead.

**Q: Why does the dashboard say `MQTT: DOWN`?**
A: The watchdog cannot reach the MQTT broker from this host. Talk to your
admin — they may need to adjust the broker address or open a firewall port.

**Q: Why is a device showing STALLED when it just sent a reading?**
A: The watchdog evaluates heartbeat **value changes**, not packet
presence. If the device is publishing temperature correctly but its
`heartbeat_counter` value hasn't incremented for 3 minutes, the heartbeat
sensor might be the fault. Verify by checking the device in ESPHome's
web UI.

**Q: How do I add a new operator?**
A: Ask your admin. They will edit `config/config.yaml`'s `auth.users:`
block, restart the container, and tell you your temporary password to
change on first login.

**Q: Why does clicking Snooze not stop Telegram alerts?**
A: Either Telegram is not configured, or the snooze duration is shorter
than the cooldown you expect. Wait until the timer expires (or click
Unsnooze) and re-evaluate.

## Ignore list (devices not currently in use)

Some devices are physically installed but not in service - a spare, a
bench test unit, a sensor in a room that is closed for the season. These
would otherwise appear in the table and raise alarms.

**Admin Config > Ignore List** (or `/admin/ignored`) lists them. Pick a
device and press **Ignore this device**. It disappears from the dashboard
and is excluded from alerting entirely - no Telegram message, no wall
board entry, no status evaluation. Its readings are still recorded, so
resuming monitoring restores its full history.

**Resume monitoring** puts it straight back. Every change is audited as
`IGNORE_ADDED` / `IGNORE_REMOVED`.

The same list can be edited as YAML at **Admin Config**:

```yaml
watchdog:
  ignored_devices:
    - pt100-3
    - spare-sensor-2
```

Saving the YAML editor hot-reloads the list; no restart is needed.

## Status highlighting

A row that changes status flashes for a few seconds and shows a
`was X -> now Y` tag. After the flash it keeps a persistent amber
tint for as long as the fault is unhandled. **The highlight clears when
you snooze the device** - that is the signal that someone has taken
ownership of it. A device that has been broken all shift therefore looks
different from one that just failed, and a snoozed device looks calm.

## Snoozing

The **Snooze** column of the table carries a dropdown (1 hour, 8 hours,
24 hours, 7 days) on every row, so a device can be silenced without
expanding the row. A snoozed device shows a **Snoozed** badge with a
**Resume** button instead.

