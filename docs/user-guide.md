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
| **Status** | `ONLINE` (green), `STALLED` (orange), `OFFLINE` (red). |
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
