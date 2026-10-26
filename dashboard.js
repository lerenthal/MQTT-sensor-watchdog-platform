// Dashboard JS — kept in a separate file because the JSON-string backslash
// escapes in single-file Python string literals are too easy to get wrong.
// Loaded by app.py via {js_from_file} substitution at request time.

let ALL_DEVICES = [];
let SORT_KEY = localStorage.getItem('watchdog-sort-key') || null;
let SORT_DIR = localStorage.getItem('watchdog-sort-dir') || 'asc';
let SEARCH_DEBOUNCE = null;
// FIX 1: the table body is rebuilt wholesale on every poll, which wipes any
// open/closed state held in the DOM. Track expansion in a Set so panels
// survive the re-render. Persisted so a page reload keeps the operator's view.
let EXPANDED = new Set(JSON.parse(localStorage.getItem('watchdog-expanded') || '[]'));
// Reason panels are tracked separately from sensor panels so the operator can
// keep one open and the other closed. Default collapsed.
let REASON_OPEN = new Set(JSON.parse(localStorage.getItem('watchdog-reason-open') || '[]'));

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
}

// Server's flatline window (seconds). Updated from /api/devices on each poll so
// the per-sensor chips flag "silent/frozen" at the SAME threshold the watchdog
// uses, rather than a hardcoded value (review finding B16).
let FLATLINE_LIMIT = 300;

// Feature 3: rows are compact by default and expand on click. Expansion is
// per-device and survives the 10s poll, like the sensor panels.
let EXPANDED_ROWS = new Set(JSON.parse(localStorage.getItem('watchdog-expanded-rows') || '[]'));

// Feature 2: remember the status each device had on the previous poll, so a
// row that CHANGED state can be highlighted. A device that has been broken for
// an hour should look calm; a device that just broke should not.
let PREV_STATUS = new Map(JSON.parse(localStorage.getItem('watchdog-prev-status') || '[]'));

// A row flashes after its status changes and KEEPS flashing until the
// operator snoozes or otherwise acknowledges the device. A purely
// time-limited flash is the wrong signal for a facility: the fault may
// still be un-actioned hours later, and a wall display that has gone
// quiet reads as "handled" when it is not.
const FLASH_MS = 8000;      // how long the FLASH animation itself runs
const FLASH_PERSIST = true; // keep the highlight until snoozed

// State-changing requests must be POST and carry the CSRF token (finding B9).
// Render an inline POST form so these work without inline JS handlers.
function postForm(action, label, cls, confirmMsg) {
    const onsub = confirmMsg
        ? ' onsubmit="return confirm(\'' + confirmMsg + '\')"' : '';
    return '<form method="post" action="' + action + '" style="display:inline;margin:0"' + onsub + '>'
         + '<input type="hidden" name="_csrf" value="' + (window.CSRF_TOKEN || '') + '">'
         + '<button type="submit" class="' + cls + '">' + label + '</button></form>';
}
function postMenuItem(action, label) {
    return '<form method="post" action="' + action + '" style="margin:0">'
         + '<input type="hidden" name="_csrf" value="' + (window.CSRF_TOKEN || '') + '">'
         + '<button type="submit" class="dropdown-item" '
         + 'style="width:100%;text-align:left;background:none;border:none;'
         + 'cursor:pointer;font:inherit;color:inherit">' + label + '</button></form>';
}

// Compact human age: 45s / 12m / 3h / 2d. Operators read a duration far
// faster than a timestamp when the question is "is this current?".
function fmtAge(s) {
    if (s == null) return '?';
    if (s < 60) return Math.round(s) + 's';
    if (s < 3600) return Math.round(s / 60) + 'm';
    if (s < 86400) return (s / 3600).toFixed(s < 36000 ? 1 : 0) + 'h';
    return (s / 86400).toFixed(1) + 'd';
}

// A headline reading (temperature / humidity / CO2) rendered HONESTLY.
//
// The old cell printed devices.last_co2 as a bare number. That column is
// overwritten by whichever channel published last and is never aged out, so a
// CO2 reading from two days ago (airmon-1 showed 438 ppm from 2 days prior)
// looked exactly like a live one, and a device whose CO2 had stopped while its
// temperature kept flowing still showed a plausible CO2 figure next to an
// ONLINE badge. That is the misleading-healthy-signal failure this whole
// system exists to prevent.
//
// So: always SHOW the value (the operator asked to still see CO2 on a
// CO2-stalled device) but never let a stale one masquerade as current — it is
// dimmed and carries its age. Multi-channel devices show the channel count.
function metricCell(h, unit, dp) {
    if (!h) return '<span class="muted">&mdash;</span>';
    const v = (typeof h.value === 'number') ? h.value.toFixed(dp) : '--';
    let cls = 'mval';
    if (h.known_degraded)        cls += ' mval-supp';
    else if (h.stale)            cls += ' mval-stale';
    else if (h.in_range === false) cls += ' mval-bad';

    const tip = h.sensor_name + ' = ' + v + unit
        + '\nlast packet ' + fmtAge(h.age_seconds) + ' ago (' + (h.ts || '?') + ')'
        + (h.channels > 1 ? '\n' + h.channels + ' channels of this metric; showing the freshest' : '')
        + (h.stale ? '\nSTALE: past this channel’s expected interval' : '')
        + (h.in_range === false ? '\nOUT OF RANGE' : '')
        + (h.known_degraded ? '\nalerting suppressed (known degraded)' : '');

    let out = '<span class="' + cls + '" title="' + escapeHtml(tip) + '">' + v + '</span>';
    if (h.stale) out += '<span class="mage" title="age of the last packet">'
                      + fmtAge(h.age_seconds) + '</span>';
    if (h.channels > 1) out += '<span class="mch" title="' + h.channels
                             + ' channels; freshest shown">&times;' + h.channels + '</span>';
    return out;
}

function statusBadge(s) {
    if (s === 'ONLINE')  return '<span class="badge badge-online">ONLINE</span>';
    if (s === 'STALLED') return '<span class="badge badge-stalled">STALLED</span>';
    if (s === 'SUSPECTED_STALL') return '<span class="badge badge-suspect">SUSPECTED</span>';
    if (s === 'SENSOR_SILENT') return '<span class="badge badge-silent">SILENT</span>';
    if (s === 'DEGRADED') return '<span class="badge badge-degraded">DEGRADED</span>';
    return '<span class="badge badge-offline">OFFLINE</span>';
}

window.toggleSensors = function (deviceId, btn) {
    if (EXPANDED.has(deviceId)) {
        EXPANDED.delete(deviceId);
    } else {
        EXPANDED.add(deviceId);
    }
    localStorage.setItem('watchdog-expanded', JSON.stringify([...EXPANDED]));
    // Flip the panel directly rather than waiting for the next poll.
    const el = document.getElementById('sensors-' + encodeURIComponent(deviceId));
    if (el) {
        const open = EXPANDED.has(deviceId);
        el.style.display = open ? 'block' : 'none';
        // The caret lives in the toggle button next to the summary line.
        const caret = el.parentElement
            ? el.parentElement.querySelector('.svtoggle') : null;
        if (caret) {
            caret.innerHTML = open ? '&#9662;' : '&#9656;';
            caret.title = (open ? 'Hide' : 'Show')
                        + ' every sensor on this device';
        }
        if (btn && btn !== caret) btn.innerHTML = open ? '&#9662;' : '&#9656;';
    }
};

window.toggleReason = function (deviceId, btn) {
    if (REASON_OPEN.has(deviceId)) {
        REASON_OPEN.delete(deviceId);
    } else {
        REASON_OPEN.add(deviceId);
    }
    localStorage.setItem('watchdog-reason-open', JSON.stringify([...REASON_OPEN]));
    const el = document.getElementById('reason-' + encodeURIComponent(deviceId));
    if (!el) return;
    const open = REASON_OPEN.has(deviceId);
    el.style.display = open ? 'block' : 'none';
    if (btn) {
        const nf = btn.querySelector('.muted');
        btn.innerHTML = (open ? '&#9662;' : '&#9656;')
                      + (nf ? ' <span class="muted">' + nf.textContent + '</span>' : '');
    }
};

window.collapseReasons = function () {
    REASON_OPEN.clear();
    localStorage.setItem('watchdog-reason-open', '[]');
    refresh();
};

// "Collapse all" closes everything: the per-device sensor panels, the
// fault/reason details, AND the expanded device rows.
//
// Both sets of state have to be cleared. Clearing only one of them is what
// made these buttons look broken: the device rows stayed expanded, so
// nothing appeared to happen.
//
// The re-render is done directly rather than through refresh(), because
// refresh() re-fetches from the server and the buttons must feel
// instantaneous. refresh() runs a moment later and re-applies the same
// state, so the two agree.
window.collapseAll = function () {
    EXPANDED.clear();
    REASON_OPEN.clear();
    EXPANDED_ROWS.clear();
    localStorage.setItem('watchdog-expanded', '[]');
    localStorage.setItem('watchdog-reason-open', '[]');
    localStorage.setItem('watchdog-expanded-rows', '[]');
    rerenderRows();
    refresh();
};

// "Expand sensors" opens every device's sensor panel without expanding
// the device rows. The device row carries the headline metrics already,
// so the operator can see the whole fleet's sensor detail at once.
window.expandAll = function () {
    ALL_DEVICES.forEach(d => EXPANDED.add(d.device_id));
    localStorage.setItem('watchdog-expanded', JSON.stringify([...EXPANDED]));
    rerenderRows();
    refresh();
};

// "Expand all" - sensors AND the device detail rows.
window.expandEverything = function () {
    ALL_DEVICES.forEach(d => {
        EXPANDED.add(d.device_id);
        EXPANDED_ROWS.add(d.device_id);
    });
    localStorage.setItem('watchdog-expanded', JSON.stringify([...EXPANDED]));
    localStorage.setItem('watchdog-expanded-rows', JSON.stringify([...EXPANDED_ROWS]));
    rerenderRows();
    refresh();
};

// Re-render the table body from the current state, without a server
// round-trip. Used by the toolbar buttons so they respond immediately.
function rerenderRows() {
    const tbody = document.getElementById('devices-tbody');
    if (!tbody || !ALL_DEVICES.length) return;
    tbody.innerHTML = ALL_DEVICES.map(renderRow).join('');
    applyFilter();
    applySort();
}

// The snooze control. Present in the compact row (always visible) and again
// in the expanded detail. It is a dropdown of the configured presets plus an
// "Unsnooze" when the device is currently snoozed.
function snoozeControl(d, id) {
    if (d.snooze_until) {
        return '<div class="snooze-ctl">'
             + '<span class="badge badge-warning" title="Snoozed until '
             + escapeHtml(d.snooze_until.slice(0, 16)) + '">Snoozed</span>'
             + postForm('/unsnooze/' + id, 'Resume', 'btn btn-sm btn-success')
             + '</div>';
    }
    return '<div class="dropdown snooze-ctl">'
         + '<button type="button" class="btn btn-sm btn-warning" '
         + 'onclick="event.stopPropagation();this.parentElement.classList.toggle(\'open\')">'
         + 'Snooze &#9662;</button>'
         + '<div class="dropdown-menu">'
         + postMenuItem('/snooze/' + id + '/1', '1 Hour')
         + postMenuItem('/snooze/' + id + '/8', '8 Hours')
         + postMenuItem('/snooze/' + id + '/24', '24 Hours')
         + postMenuItem('/snooze/' + id + '/168', '7 Days')
         + '</div></div>';
}

// The Restart button.
//
// A device can only be restarted if its firmware publishes an MQTT restart
// entity. The command topic is derived by ESPHome from the button's name
// and differs per device, so the server discovers it from the MQTT
// discovery payloads and tells us which devices are restartable. Where a
// device is not restartable the button is disabled with the reason, rather
// than offered and silently doing nothing.
// The number of columns in the devices table, read from the real header.
//
// The detail row spans the full width with a colspan. That value used to
// be hardcoded, and when the header had 11 columns while the detail said
// 12 the browser created a phantom column - which showed up as a stray
// narrow column at the right edge of every expanded row. Reading it from
// the header means it can never drift out of step again.
function tableColumnCount() {
    const th = document.querySelector('table.devices thead tr th');
    if (!th) return 12;
    const row = th.closest('tr');
    return row ? row.children.length : 12;
}

function restartButton(d) {
    if (d.can_restart === false) {
        return '<button class="btn btn-sm btn-muted" disabled '
             + 'title="This device publishes no MQTT restart entity, so there '
             + 'is nothing to command. Its firmware would need a restart '
             + 'button with an mqtt: command_topic.">Restart</button>';
    }
    return '<button class="btn btn-sm btn-danger" onclick="event.stopPropagation();restart(\''
         + escapeHtml(d.device_id) + '\')">Restart</button>';
}

function renderRow(d) {
    const status = statusBadge(d.status);
    const id = encodeURIComponent(d.device_id);
    const isOpen = EXPANDED_ROWS.has(d.device_id);

    // Headline readings come from the server's per-channel rollup, which knows
    // each value's age — not from the never-aged devices.last_* columns.
    const hl = d.headline || {};
    const hTemp = hl.temperature, hHum = hl.humidity, hCo2 = hl.co2;
    const anyStale = [hTemp, hHum, hCo2].some(h => h && h.stale);
    const anyOor   = [hTemp, hHum, hCo2].some(h => h && h.in_range === false);

    // --- Feature 2: did this device's status change since the last poll?
    const prev = PREV_STATUS.get(d.device_id);
    // A snoozed device is considered handled: it must not flash, however
    // recently its status changed.
    const snoozed = !!d.snooze_until;
    const changed = prev !== undefined && prev !== d.status && !snoozed;
    const changedFrom = changed ? prev : null;

    const restartBtn = '<button class="btn btn-sm btn-danger" '
        + 'onclick="restart(\'' + escapeHtml(d.device_id) + '\')">Restart</button>';
    const resetStaleBtn = (d.status === 'DEGRADED')
        ? postForm('/device/' + id + '/reset_stale', 'Reset', 'btn btn-sm btn-warning',
                   'Clear the stall alert for ' + d.device_id + '?')
        : '';
    const actions = d.snooze_until
        ? postForm('/unsnooze/' + id, 'Unsnooze', 'btn btn-sm btn-success')
          + restartBtn + resetStaleBtn
        : '<div class="dropdown">'
          + '<button type="button" class="btn btn-sm btn-warning" '
          + 'onclick="this.parentElement.classList.toggle(\'open\')">Snooze &#9662;</button>'
          + '<div class="dropdown-menu">'
          + postMenuItem('/snooze/' + id + '/1', '1 Hour')
          + postMenuItem('/snooze/' + id + '/8', '8 Hours')
          + postMenuItem('/snooze/' + id + '/24', '24 Hours')
          + postMenuItem('/snooze/' + id + '/168', '7 Days')
          + '</div></div>'
          + restartBtn + resetStaleBtn;

    // --- Compact one-line summary (always visible).
    const loc = d.location || '';
    const role = d.role ? ' (' + d.role + ')' : '';
    const sensors = sensorSummary(d);
    const reasonShort = d.reason
        ? '<span class="reason-summary" title="' + escapeHtml(d.reason) + '">'
          + escapeHtml(d.reason) + '</span>'
        : '<span class="muted">&mdash;</span>';

    const changedTag = changed
        ? '<span class="changed-tag" title="Status changed from '
          + escapeHtml(changedFrom || '?') + '">'
          + escapeHtml(changedFrom || '?') + ' &rarr; ' + escapeHtml(d.status)
          + '</span>'
        : '';

    // --- Detail (shown only when the row is expanded).
    const detail = isOpen ? (
        '<tr class="detail-row" data-detail-for="' + escapeHtml(d.device_id) + '">'
      + '<td colspan="' + tableColumnCount() + '">'
      + '<div class="detail-grid">'
      + '<div><span class="dl">Location / Role</span><span class="dv">'
        + escapeHtml(loc || '\u2014') + escapeHtml(role) + '</span></div>'
      + '<div><span class="dl">Status</span><span class="dv">' + status + '</span></div>'
      + '<div><span class="dl">Last seen</span><span class="dv ts">'
        + (d.last_seen || 'Never') + '</span></div>'
      + '<div><span class="dl">Heartbeat</span><span class="dv num">'
        + (d.last_heartbeat_value ?? '\u2014') + '</span></div>'
      + '<div><span class="dl">Reason</span><span class="dv">'
        + (d.reason_detail || []).map(f =>
            '<div><span class="badge badge-' + faultBadgeClass(f.alert_type) + '">'
            + escapeHtml(f.alert_type) + '</span> ' + escapeHtml(f.message)
            + ' <span class="ts">since ' + escapeHtml((f.triggered_at || '').slice(0, 19))
            + '</span></div>').join('')
        + '</span></div>'
      + '<div><span class="dl">Snooze</span><span class="dv">'
        + snoozeControl(d, id) + '</span></div>'
      + '<div><span class="dl">Actions</span><span class="dv">' + actions + '</span></div>'
      + '</div>'
      + '<div class="detail-sensors"><span class="dl">All sensors</span>'
      + sensors + '</div>'
      + '</td></tr>'
    ) : '';

    // A device that is faulty and NOT snoozed keeps a persistent highlight.
    // This is what "flash until snoozed" means in practice: the flash
    // animation is finite, the colour is not, and it clears when the
    // operator snoozes.
    const faulty = d.status && d.status !== 'ONLINE' && !snoozed;
    const rowClass = 'row-compact'
        + (changed ? ' row-changed' : '')
        + (faulty ? ' row-changed-persist' : '')
        + (d.status === 'OFFLINE' ? ' row-offline' : '')
        + (d.status === 'DEGRADED' ? ' row-degraded' : '')
        + (d.status === 'STALLED' ? ' row-stalled' : '');

    const main = '<tr class="' + rowClass + '" data-device="' + escapeHtml(d.device_id)
        + '" data-status="' + escapeHtml(d.status)
        + '" data-location="' + escapeHtml(d.location || '')
        + '" data-role="' + escapeHtml(d.role || '')
        + '" data-reason="' + escapeHtml(d.reason || '')
        + '" data-temp="' + (hTemp ? hTemp.value : '')
        + '" data-hum="' + (hHum ? hHum.value : '')
        + '" data-co2="' + (hCo2 ? hCo2.value : '')
        + '" data-lastseen="' + (d.last_seen || '')
        + '" data-heartbeat="' + (d.last_heartbeat_value ?? '')
        // Flags the summary chips filter on.
        + '" data-stale="' + (anyStale ? '1' : '')
        + '" data-oor="' + (anyOor ? '1' : '')
        + '" data-snoozed="' + (snoozed ? '1' : '')
        + '" onclick="toggleRow(\'' + escapeHtml(d.device_id) + '\')">'
        + '<td class="col-device"><span class="expand-caret">'
          + (isOpen ? '&#9662;' : '&#9656;') + '</span> <strong>'
          + escapeHtml(d.device_id) + '</strong></td>'
        + '<td class="col-location">' + escapeHtml(loc)
          + (d.role ? '<br><span class="muted" style="font-size:.75rem">'
                     + escapeHtml(d.role) + '</span>' : '') + '</td>'
        + '<td class="col-status">' + status + changedTag + '</td>'
        + '<td class="col-temp num">' + metricCell(hTemp, '°C', 1) + '</td>'
        + '<td class="col-hum num">' + metricCell(hHum, '%', 1) + '</td>'
        + '<td class="col-co2 num">' + metricCell(hCo2, ' ppm', 0) + '</td>'
        + '<td class="col-reason">' + reasonShort + '</td>'
        + '<td class="col-sensors">' + sensors + '</td>'
        + '<td class="col-lastseen ts">'
          + (d.last_seen || 'Never').slice(5, 19) + '</td>'
        + '<td class="col-heartbeat num">' + (d.last_heartbeat_value ?? '--') + '</td>'
        + '<td class="col-snooze">' + snoozeControl(d, id) + '</td>'
        + '<td class="col-actions">'
          + restartButton(d)
          + '</td>'
        + '</tr>';

    return main + detail;
}

function faultBadgeClass(t) {
    if (t === 'SYSTEMIC') return 'offline';
    if (t === 'OUT_OF_RANGE') return 'stalled';
    if (t === 'OFFLINE') return 'offline';
    if (t === 'STALLED') return 'stalled';
    if (t === 'SENSOR_SILENT') return 'silent';
    return 'suspect';
}

// Feature 3: expand/collapse a device row. The detail is a separate <tr>, so
// a re-render is the simplest way to add or remove it.
window.toggleRow = function (deviceId) {
    if (EXPANDED_ROWS.has(deviceId)) {
        EXPANDED_ROWS.delete(deviceId);
    } else {
        EXPANDED_ROWS.add(deviceId);
    }
    localStorage.setItem('watchdog-expanded-rows', JSON.stringify([...EXPANDED_ROWS]));
    refresh();
};


function applyFilter() {
    const q = (document.getElementById('search').value || '').toLowerCase().trim();
    const tbody = document.getElementById('devices-tbody');
    // Only the DEVICE rows are counted and filtered. A detail row is not a
    // device: it has no data-device, so it must never be counted in the
    // total, and it must be hidden or shown together with the device it
    // belongs to - otherwise a filtered-out device leaves its expanded
    // panel stranded in the table.
    const rows = Array.from(tbody.querySelectorAll('tr:not(.detail-row)'));
    let visible = 0;
    rows.forEach(r => {
        let show = true;
        if (q) {
            const haystack = (r.dataset.device + ' ' + r.dataset.location + ' '
                + r.dataset.role + ' ' + r.dataset.status + ' ' + r.dataset.reason
                + ' ' + r.dataset.temp
                + ' ' + r.dataset.hum + ' ' + r.dataset.co2
                + ' ' + (r.textContent || '')).toLowerCase();
            show = haystack.includes(q);
        }
        // A summary chip can filter on a condition that is not text, e.g.
        // "has a stale reading". Those use the row's data flags.
        if (show && ACTIVE_FLAG === '__stale__')   show = r.dataset.stale === '1';
        if (show && ACTIVE_FLAG === '__oor__')     show = r.dataset.oor === '1';
        if (show && ACTIVE_FLAG === '__snoozed__') show = r.dataset.snoozed === '1';
        r.style.display = show ? '' : 'none';
        if (show) visible++;
        // Keep the detail row in step with its device.
        const detail = tbody.querySelector('tr.detail-row[data-detail-for="'
            + r.dataset.device + '"]');
        if (detail) detail.style.display = show ? '' : 'none';
    });
    document.getElementById('row-count').textContent =
        rows.length === 0 ? '' : (visible + ' of ' + rows.length);
}

function applySort() {
    if (!SORT_KEY) return;
    const tbody = document.getElementById('devices-tbody');
    // Only the DEVICE rows are sortable. A detail row is not a device and
    // has no sort values, so including it here detached it from its
    // device and left the expanded panel stranded at the bottom of the
    // table - the row and its detail then looked like two separate
    // devices.
    const rows = Array.from(tbody.querySelectorAll('tr:not(.detail-row)'));
    const key = SORT_KEY;
    const dir = SORT_DIR === 'asc' ? 1 : -1;
    rows.sort((a, b) => {
        const av = a.dataset[key] ?? '';
        const bv = b.dataset[key] ?? '';
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn) && av !== '' && bv !== '') {
            return (an - bn) * dir;
        }
        if (av === '' && bv !== '') return 1;
        if (bv === '' && av !== '') return -1;
        return String(av).localeCompare(String(bv)) * dir;
    });
    // Re-append each device row, and immediately after it the detail row
    // that belongs to it, so the expanded panel always stays attached to
    // its device.
    rows.forEach(r => {
        tbody.appendChild(r);
        const detail = tbody.querySelector('tr.detail-row[data-detail-for="'
            + r.dataset.device + '"]');
        if (detail) tbody.appendChild(detail);
    });
    document.querySelectorAll('th[data-sort]').forEach(th => {
        const arrow = th.dataset.sort === key
            ? (SORT_DIR === 'asc' ? ' \u2191' : ' \u2193') : '';
        const span = th.querySelector('.sort-arrow');
        if (span) span.textContent = arrow;
    });
}

function handleSortClick(th) {
    const key = th.dataset.sort;
    if (SORT_KEY === key) {
        SORT_DIR = SORT_DIR === 'asc' ? 'desc' : 'asc';
    } else {
        SORT_KEY = key;
        SORT_DIR = 'asc';
    }
    localStorage.setItem('watchdog-sort-key', SORT_KEY);
    localStorage.setItem('watchdog-sort-dir', SORT_DIR);
    applySort();
}

// Systemic fault banner: N devices losing the same metric in the same
// window is a broker / network / ACL event, not N sensor failures. Say so
// explicitly so an operator doesn't go chasing twenty CO2 sensors.
function renderSystemic(list) {
    const el = document.getElementById('systemic-banner');
    if (!el) return;
    if (!list.length) { el.innerHTML = ''; return; }
    el.innerHTML = list.map(c =>
        '<div class="systemic-banner"><span>&#9888;</span><div>'
        + '<strong>Systemic fault: ' + escapeHtml(c.sensor_type) + ' silent on '
        + c.count + ' devices</strong><br>'
        + '<span class="muted">All stopped between '
        + Math.round(c.window_start / 60) + ' and ' + Math.round(c.window_end / 60)
        + ' minutes ago. Simultaneous loss of one metric across many devices '
        + 'points at the broker, network or an ACL change &mdash; not at the '
        + 'sensors themselves.</span><br>'
        + '<span class="muted">Affected: ' + escapeHtml(c.devices.join(', '))
        + '</span></div></div>'
    ).join('');
}

// Fleet summary strip. On a 31-device table the operator's first question is
// "is anything wrong, and how much", which previously required reading every
// row. Each chip is also a one-click filter into the table.
function renderSummary(devices) {
    const el = document.getElementById('fleet-summary');
    if (!el) return;
    const by = {ONLINE: 0, OFFLINE: 0, STALLED: 0, DEGRADED: 0};
    let staleMetrics = 0, outOfRange = 0, snoozed = 0;
    devices.forEach(d => {
        by[d.status] = (by[d.status] || 0) + 1;
        if (d.snooze_until) snoozed++;
        const hl = d.headline || {};
        ['temperature', 'humidity', 'co2'].forEach(m => {
            const h = hl[m];
            if (!h) return;
            if (h.stale) staleMetrics++;
            if (h.in_range === false) outOfRange++;
        });
    });
    const chip = (label, n, cls, filter, tip) =>
        '<button class="sumchip ' + cls + (n ? '' : ' sumchip-zero') + '"'
        + ' title="' + escapeHtml(tip) + '"'
        + ' onclick="summaryFilter(\'' + filter + '\')">'
        + '<span class="sumn">' + n + '</span> ' + label + '</button>';

    el.innerHTML =
        chip('online',    by.ONLINE   || 0, 'sum-ok',    'ONLINE',   'Devices reporting normally')
      + chip('offline',   by.OFFLINE  || 0, 'sum-crit',  'OFFLINE',  'No MQTT traffic at all')
      + chip('stalled',   by.STALLED  || 0, 'sum-warn',  'STALLED',  'Heartbeat counter frozen')
      + chip('degraded',  by.DEGRADED || 0, 'sum-degr',  'DEGRADED', 'Device up, at least one channel bad')
      + chip('stale readings', staleMetrics, 'sum-stale', '__stale__',
             'Headline readings older than their channel’s expected interval')
      + chip('out of range', outOfRange, 'sum-oor', '__oor__',
             'Fresh readings outside their configured target range')
      + (snoozed ? chip('snoozed', snoozed, 'sum-muted', '__snoozed__',
             'Alerting silenced by an operator') : '')
      + '<button class="sumchip sumchip-clear" onclick="summaryFilter(\'\')"'
      + ' title="Clear the filter">show all</button>';
}

// Clicking a summary chip filters the table through the existing search box,
// so filter state stays in exactly one place.
window.summaryFilter = function (token) {
    const box = document.getElementById('search');
    if (!box) return;
    if (token === '__stale__' || token === '__oor__' || token === '__snoozed__') {
        ACTIVE_FLAG = token;
        box.value = '';
    } else {
        ACTIVE_FLAG = null;
        box.value = token;
    }
    applyFilter();
};
let ACTIVE_FLAG = null;

async function refresh() {
    try {
        const res = await fetch('/api/devices');
        if (res.status === 401) { window.location = '/login'; return; }
        const data = await res.json();
        ALL_DEVICES = data.devices || [];
        if (typeof data.flatline_limit === 'number') FLATLINE_LIMIT = data.flatline_limit;
        const tbody = document.getElementById('devices-tbody');
        if (!ALL_DEVICES.length) {
            tbody.innerHTML = '<tr><td colspan="' + tableColumnCount() + '" class="muted">'
                + 'No devices yet &mdash; waiting for MQTT traffic&hellip;</td></tr>';
            document.getElementById('row-count').textContent = '';
            return;
        }
        // Feature 2: remember each device's status so the NEXT render can
        // tell that it changed. Do this BEFORE rendering, otherwise every
        // device would look "changed" on every poll.
        const nowStatus = new Map();
        ALL_DEVICES.forEach(d => nowStatus.set(d.device_id, d.status));
        PREV_STATUS = nowStatus;
        localStorage.setItem('watchdog-prev-status', JSON.stringify([...PREV_STATUS]));

        tbody.innerHTML = ALL_DEVICES.map(renderRow).join('');
        renderSummary(ALL_DEVICES);
        applyFilter();
        applySort();

        const mb = document.getElementById('mqtt-badge');
        if (data.mqtt.connected) {
            mb.className = 'badge badge-mqtt-ok'; mb.textContent = 'MQTT: OK';
        } else {
            mb.className = 'badge badge-mqtt-down'; mb.textContent = 'MQTT: DOWN';
        }
        renderSystemic(data.systemic || []);
    } catch (e) { console.error(e); }
}

// Feature 2: the row FLASHES for FLASH_MS, then settles to a persistent
// highlight while the fault is still unhandled. The highlight is removed
// once the operator snoozes the device - renderRow re-derives the class
// on every poll from (status changed, not snoozed), so it disappears by
// itself when the device is snoozed.
//
// The CSS animation itself is finite (so it does not strobe forever); the
// persistent colour comes from the row-changed-persist class.
setTimeout(() => {
    document.querySelectorAll('tr.row-changed').forEach(tr => {
        tr.classList.remove('row-changed');
        tr.classList.add('row-changed-persist');
    });
}, FLASH_MS);

window.restart = async function (deviceId) {
    if (!confirm('Restart ' + deviceId + '?')) return;
    const r = await fetch('/api/restart/' + encodeURIComponent(deviceId),
        { method: 'POST', headers: { 'X-CSRFToken': window.CSRF_TOKEN || '' } });
    let data = {};
    try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
    if (r.status === 401) { window.location = '/login'; return; }
    if (!data.ok) {
        // 404 means the device publishes no restart entity at all - there
        // is nothing to command, and the firmware would need one. Other
        // failures are transport problems (not connected, not acked).
        if (r.status === 404) {
            alert('Cannot restart ' + deviceId + '.\n\n'
                + (data.error || 'No restart entity is published for it.'));
        } else {
            alert('Restart failed: ' + (data.error || 'unknown'));
        }
        return;
    }
    refresh();
};

// Table font size. Stored per-browser so each operator can set their own
// reading comfort on a shared wall screen or laptop.
window.setFontSize = function (px) {
    const v = px + 'px';
    document.documentElement.style.setProperty('--table-font-size', v);
    localStorage.setItem('watchdog-font-size', px);
    const lbl = document.getElementById('font-size-label');
    if (lbl) lbl.textContent = px;
};

// ---- #3: collapsed sensor cell lists every sensor by name --------------------
// The count alone hid the fact that a device can publish several sensors of
// the same type (maxim4 has four temperature channels). The collapsed cell
// now names them, so nothing is invisible until you expand.
// The Sensors cell.
//
// ONE LINE by default. The operator asked for exactly this: a compact row
// per device, with the full sensor data behind a click rather than
// spilling sideways and making the table scroll.
//
// The collapsed line shows, per metric type, the FRESHEST channel's value
// plus a count badge when a metric has more than one channel (maxim4 has
// four temperature sensors). Clicking the cell opens a panel with EVERY
// channel, its value and its health.
//
// The expanded state lives in EXPANDED, which is persisted - the same set
// the toolbar's "Expand sensors" / "Collapse all" buttons drive.
function sensorSummary(d) {
    const list = (d.sensors || [])
        .filter(s => s.sensor_type !== 'heartbeat' && s.sensor_type !== 'diagnostic')
        .slice()
        .sort((a, b) => {
            if (a.sensor_type !== b.sensor_type) return a.sensor_type.localeCompare(b.sensor_type);
            return a.sensor_name.localeCompare(b.sensor_name);
        });
    if (!list.length) return '<span class="muted">no readings</span>';

    const groups = {};
    list.forEach(s => {
        (groups[s.sensor_type] = groups[s.sensor_type] || []).push(s);
    });

    const shortName = (s) => s.sensor_name
        .replace(new RegExp('^' + escapeRegExp(d.device_id) + '_?'), '')
        .replace(new RegExp('^' + escapeRegExp(d.device_id.replace(/_/g, '-')) + '_?'), '')
        || s.sensor_name;

    const healthCls = (s) => {
        if (s.known_degraded) return ' suppressed';
        if (s.in_range === false) return ' bad';
        if (s.silent_seconds > FLATLINE_LIMIT) return ' silent';
        if (s.flat_seconds > FLATLINE_LIMIT) return ' flat';
        return '';
    };

    // --- The full detail, rendered once and shown/hidden.
    const detail = Object.keys(groups).sort().map(t => {
        const names = groups[t].map(s => {
            const v = (typeof s.value === 'number') ? s.value.toFixed(2) : '--';
            let extra = '';
            if (s.silent_seconds > FLATLINE_LIMIT) {
                extra = ' <span class="badge badge-silent">silent '
                      + Math.round(s.silent_seconds / 60) + 'm</span>';
            } else if (s.flat_seconds > FLATLINE_LIMIT) {
                extra = ' <span class="badge badge-suspect">frozen '
                      + Math.round(s.flat_seconds / 60) + 'm</span>';
            } else if (s.in_range === false) {
                const th = s.threshold || {};
                const lim = (th.min != null ? th.min : '') + ' - ' + th.max;
                extra = ' <span class="badge badge-'
                      + (th.severity === 'CRITICAL' ? 'offline' : 'stalled')
                      + '">out of range (' + lim + ')</span>';
            } else if (s.known_degraded) {
                extra = ' <span class="badge badge-muted">suppressed</span>';
            }
            return '<div class="sensor-line"><span class="ts">'
                 + escapeHtml(s.sensor_name) + '</span> &middot; '
                 + escapeHtml(s.sensor_type) + ': <strong>'
                 + escapeHtml(v) + '</strong>' + extra + '</div>';
        }).join('');
        return '<div class="svrow"><span class="svtype">' + escapeHtml(t) + '</span>'
             + names + '</div>';
    }).join('');

    // --- The collapsed line: freshest channel per metric, + a count.
    const line = Object.keys(groups).sort().map(t => {
        const g = groups[t].slice().sort((a, b) =>
            (a.silent_seconds ?? 1e9) - (b.silent_seconds ?? 1e9));
        const best = g[0];
        const v = (typeof best.value === 'number') ? best.value.toFixed(1) : '--';
        const extra = g.length > 1
            ? ' <span class="svcount" title="' + g.length + ' channels of '
              + escapeHtml(t) + '">\u00d7' + g.length + '</span>'
            : '';
        return '<span class="sv svhead' + healthCls(best) + '" title="'
             + escapeHtml(best.sensor_name) + '">'
             + escapeHtml(shortName(best)) + ' ' + escapeHtml(v) + '</span>' + extra;
    }).join('');

    const id = encodeURIComponent(d.device_id);
    const isOpen = EXPANDED.has(d.device_id);
    return '<div class="svwrap">'
         + '<div class="svline">'
         + line
         + '<button class="btn btn-xs svtoggle" onclick="toggleSensors(\''
         + escapeHtml(d.device_id) + '\')" title="'
         + (isOpen ? 'Hide' : 'Show') + ' every sensor on this device">'
         + (isOpen ? '&#9662;' : '&#9656;') + '</button>'
         + '</div>'
         + '<div id="sensors-' + id + '" class="svpanel" style="display:'
         + (isOpen ? 'block' : 'none') + '">' + detail + '</div>'
         + '</div>';
}

function escapeRegExp(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

window.dashboardSearch = function () {
    if (SEARCH_DEBOUNCE) clearTimeout(SEARCH_DEBOUNCE);
    SEARCH_DEBOUNCE = setTimeout(applyFilter, 200);
};

window.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('th[data-sort]').forEach(th => {
        th.style.cursor = 'pointer';
        th.addEventListener('click', () => handleSortClick(th));
    });
    if (SORT_KEY) applySort();

    // Restore the operator's chosen table font size.
    const saved = localStorage.getItem('watchdog-font-size');
    const slider = document.getElementById('font-size-slider');
    if (slider) {
        const px = saved || 13;
        slider.value = px;
        setFontSize(px);
    }
});

refresh();
setInterval(refresh, window.REFRESH_SECONDS * 1000);
