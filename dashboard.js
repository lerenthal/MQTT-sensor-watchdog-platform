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

function statusBadge(s) {
    if (s === 'ONLINE')  return '<span class="badge badge-online">ONLINE</span>';
    if (s === 'STALLED') return '<span class="badge badge-stalled">STALLED</span>';
    if (s === 'SUSPECTED_STALL') return '<span class="badge badge-suspect">SUSPECTED</span>';
    if (s === 'SENSOR_SILENT') return '<span class="badge badge-silent">SILENT</span>';
    if (s === 'DEGRADED') return '<span class="badge badge-degraded">DEGRADED</span>';
    return '<span class="badge badge-offline">OFFLINE</span>';
}

function sensorCell(d) {
    if (!d.sensor_count) {
        return '<span class="muted">no readings</span>';
    }
    const h = d.sensor_health || {healthy: 0, suspected: 0, silent: 0};
    const sorted = (d.sensors || []).slice().sort((a, b) => {
        if (a.sensor_name !== b.sensor_name) return a.sensor_name.localeCompare(b.sensor_name);
        return a.sensor_type.localeCompare(b.sensor_type);
    });
    const lines = sorted.map(s => {
        const v = (typeof s.value === 'number') ? s.value.toFixed(2) : '--';
        let flag = '';
        if (s.silent_seconds > 300) {
            flag = '<span class="badge badge-silent">silent '
                 + Math.round(s.silent_seconds / 60) + 'm</span>';
        } else if (s.flat_seconds > 300) {
            flag = '<span class="badge badge-suspect">frozen '
                 + Math.round(s.flat_seconds / 60) + 'm</span>';
        } else if (s.in_range === false) {
            const t = s.threshold || {};
            const lim = (t.min != null ? t.min : '') + ' - ' + t.max;
            flag = '<span class="badge badge-' + (t.severity === 'CRITICAL' ? 'offline' : 'stalled')
                 + '">out of range (' + lim + ')</span>';
        } else if (s.in_range === true) {
            flag = '<span class="muted" style="font-size:.75rem">in range</span>';
        }
        return '<div class="sensor-line"><span class="ts">'
             + escapeHtml(s.sensor_name) + '</span> &middot; '
             + escapeHtml(s.sensor_type) + ': <strong>' + escapeHtml(v) + '</strong> '
             + flag + '</div>';
    }).join('');
    const id = encodeURIComponent(d.device_id);
    const isOpen = EXPANDED.has(d.device_id);
    // Every sensor is named in the collapsed cell, grouped by metric. A count
    // alone hid the fact that one device can publish several channels of the
    // same type (maxim4 carries four temperature sensors).
    let tally = '<span class="muted">' + d.sensor_count + ' sensor'
        + (d.sensor_count !== 1 ? 's' : '') + '</span>';
    if (h.silent)    tally += ' <span class="badge badge-silent">' + h.silent + ' silent</span>';
    if (h.suspected) tally += ' <span class="badge badge-suspect">' + h.suspected + ' frozen</span>';
    const kd = (d.known_degraded || []).length;
    if (kd) tally += ' <span class="badge badge-muted">' + kd + ' suppressed</span>';
    return tally
        + ' <button class="btn btn-sm" onclick="toggleSensors(\''
        + escapeHtml(d.device_id) + '\', this)">'
        + (isOpen ? '&#9662;' : '&#9656;') + '</button>'
        + '<div id="sensors-' + id + '" '
        + 'style="display:' + (isOpen ? 'block' : 'none')
        + ';margin-top:.5rem;padding:.5rem;'
        + 'background:var(--bg);border-radius:6px;font-size:.85rem;">'
        + lines + '</div>';
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
        if (btn) btn.innerHTML = open ? '&#9662;' : '&#9656;';
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

window.collapseAll = function () {
    EXPANDED.clear();
    REASON_OPEN.clear();
    localStorage.setItem('watchdog-expanded', '[]');
    localStorage.setItem('watchdog-reason-open', '[]');
    refresh();
};

window.expandAll = function () {
    ALL_DEVICES.forEach(d => EXPANDED.add(d.device_id));
    localStorage.setItem('watchdog-expanded', JSON.stringify([...EXPANDED]));
    refresh();
};

function renderRow(d) {
    const status = statusBadge(d.status);
    const snooze = d.snooze_until
        ? '<span class="badge badge-warning">Snoozed until '
          + (d.snooze_until || '').slice(0, 16) + '</span>'
        : '<span class="muted">Active</span>';
    const id = encodeURIComponent(d.device_id);
    const actions = d.snooze_until
        ? '<a href="/unsnooze/' + id + '" class="btn btn-sm btn-success">Unsnooze</a>'
          + '<button class="btn btn-sm btn-danger" '
          + 'onclick="restart(\'' + escapeHtml(d.device_id) + '\')">Restart</button>'
        : '<div class="dropdown">'
          + '<button type="button" class="btn btn-sm btn-warning" '
          + 'onclick="this.parentElement.classList.toggle(\'open\')">Snooze &#9662;</button>'
          + '<div class="dropdown-menu">'
          + '<a class="dropdown-item" href="/snooze/' + id + '/1">1 Hour</a>'
          + '<a class="dropdown-item" href="/snooze/' + id + '/8">8 Hours</a>'
          + '<a class="dropdown-item" href="/snooze/' + id + '/24">24 Hours</a>'
          + '<a class="dropdown-item" href="/snooze/' + id + '/168">7 Days</a>'
          + '</div></div>'
          + '<button class="btn btn-sm btn-danger" '
          + 'onclick="restart(\'' + escapeHtml(d.device_id) + '\')">Restart</button>';

    // FIX 2: an explicit "why is this device not OK" column. Built from the
    // same signals the watchdog used, so it agrees with the status badge.
    // Collapsed by default: one truncated line plus a toggle that reveals the
    // full per-fault breakdown (type badge, message, trigger time).
    let reason;
    if (d.reason) {
        const isOpen = REASON_OPEN.has(d.device_id);
        const detail = (d.reason_detail || []).map(f =>
            '<div class="reason-line"><span class="badge badge-'
            + (f.alert_type === 'OFFLINE' ? 'offline'
              : f.alert_type === 'STALLED' ? 'stalled'
              : f.alert_type === 'SENSOR_SILENT' ? 'silent'
              : 'suspect')
            + '">' + escapeHtml(f.alert_type) + '</span> '
            + escapeHtml(f.message)
            + '<br><span class="ts">since '
            + (f.triggered_at || '').slice(0, 19) + '</span></div>'
        ).join('');
        const nf = (d.reason_detail || []).length;
        reason = '<span class="reason-summary" title="'
              + escapeHtml(d.reason) + '">' + escapeHtml(d.reason) + '</span>'
            + ' <button class="btn btn-sm" onclick="toggleReason(\''
            + escapeHtml(d.device_id) + '\', this)" title="'
            + (isOpen ? 'Hide' : 'Show') + ' fault detail">'
            + (isOpen ? '&#9662;' : '&#9656;')
            + (nf ? ' <span class="muted">' + nf + '</span>' : '') + '</button>'
            + '<div class="reason-panel" id="reason-' + encodeURIComponent(d.device_id) + '" '
            + 'style="display:' + (isOpen ? 'block' : 'none') + ';">'
            + (detail || '<span class="muted">' + escapeHtml(d.reason) + '</span>')
            + '</div>';
    } else {
        reason = '<span class="muted">—</span>';
    }

    return '<tr data-device="' + escapeHtml(d.device_id)
        + '" data-status="' + escapeHtml(d.status)
        + '" data-location="' + escapeHtml(d.location || '')
        + '" data-role="' + escapeHtml(d.role || '')
        + '" data-reason="' + escapeHtml(d.reason || '')
        + '" data-temp="' + (d.last_temp ?? '')
        + '" data-hum="' + (d.last_hum ?? '')
        + '" data-co2="' + (d.last_co2 ?? '')
        + '" data-lastseen="' + (d.last_seen || '')
        + '" data-heartbeat="' + (d.last_heartbeat_value ?? '')
        + '">'
        + '<td class="col-device"><strong>' + escapeHtml(d.device_id) + '</strong></td>'
        + '<td class="col-location">' + escapeHtml(d.location || '')
          + (d.role ? '<br><span class="muted" style="font-size:.75rem">'
                     + escapeHtml(d.role) + '</span>' : '') + '</td>'
        + '<td class="col-status">' + status + '</td>'
        + '<td class="col-reason">' + reason + '</td>'
        + '<td class="col-lastseen ts">' + (d.last_seen || 'Never').slice(5, 19) + '</td>'
        + '<td class="col-heartbeat num">' + (d.last_heartbeat_value ?? '--') + '</td>'
        + '<td class="col-temp num">' + (d.last_temp ?? '--') + '</td>'
        + '<td class="col-hum num">' + (d.last_hum ?? '--') + '</td>'
        + '<td class="col-co2 num">' + (d.last_co2 ?? '--') + '</td>'
        + '<td class="col-sensors">' + sensorSummary(d)
          + '<div style="margin-top:.3rem">' + sensorCell(d) + '</div></td>'
        + '<td class="col-snooze">' + snooze + '</td>'
        + '<td class="col-actions">' + actions + '</td>'
        + '</tr>';
}

function applyFilter() {
    const q = (document.getElementById('search').value || '').toLowerCase().trim();
    const rows = document.querySelectorAll('#devices-tbody tr');
    let visible = 0;
    rows.forEach(r => {
        if (!q) { r.style.display = ''; visible++; return; }
        const haystack = (r.dataset.device + ' ' + r.dataset.location + ' '
            + r.dataset.role + ' ' + r.dataset.status + ' ' + r.dataset.reason
            + ' ' + r.dataset.temp
            + ' ' + r.dataset.hum + ' ' + r.dataset.co2
            + ' ' + (r.textContent || '')).toLowerCase();
        r.style.display = haystack.includes(q) ? '' : 'none';
        if (r.style.display !== 'none') visible++;
    });
    document.getElementById('row-count').textContent =
        rows.length === 0 ? '' : (visible + ' of ' + rows.length);
}

function applySort() {
    if (!SORT_KEY) return;
    const tbody = document.getElementById('devices-tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
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
    rows.forEach(r => tbody.appendChild(r));
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

async function refresh() {
    try {
        const res = await fetch('/api/devices');
        if (res.status === 401) { window.location = '/login'; return; }
        const data = await res.json();
        ALL_DEVICES = data.devices || [];
        const tbody = document.getElementById('devices-tbody');
        if (!ALL_DEVICES.length) {
            tbody.innerHTML = '<tr><td colspan="12" class="muted">'
                + 'No devices yet &mdash; waiting for MQTT traffic&hellip;</td></tr>';
            document.getElementById('row-count').textContent = '';
            return;
        }
        tbody.innerHTML = ALL_DEVICES.map(renderRow).join('');
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

window.restart = async function (deviceId) {
    if (!confirm('Restart ' + deviceId + '?')) return;
    const r = await fetch('/api/restart/' + encodeURIComponent(deviceId),
        { method: 'POST' });
    const data = await r.json();
    if (!data.ok) alert('Restart failed: ' + (data.error || 'unknown'));
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
function sensorSummary(d) {
    const list = (d.sensors || [])
        .filter(s => s.sensor_type !== 'heartbeat')
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

    const parts = Object.keys(groups).sort().map(t => {
        const g = groups[t];
        const names = g.map(s => {
            const short = s.sensor_name
                .replace(new RegExp('^' + escapeRegExp(d.device_id) + '_?'), '')
                .replace(new RegExp('^' + escapeRegExp(d.device_id.replace(/_/g, '-')) + '_?'), '')
                || s.sensor_name;
            const v = (typeof s.value === 'number') ? s.value.toFixed(1) : '--';
            let cls = 'sv';
            if (s.known_degraded) cls += ' suppressed';
            else if (s.in_range === false) cls += ' bad';
            else if (s.silent_seconds > 300) cls += ' silent';
            else if (s.flat_seconds > 300) cls += ' flat';
            return '<span class="' + cls + '" title="' + escapeHtml(s.sensor_name) + '">'
                 + escapeHtml(short) + ' ' + escapeHtml(v) + '</span>';
        });
        return '<div class="svrow"><span class="svtype">' + escapeHtml(t) + '</span>'
             + names.join(' ') + '</div>';
    });
    return '<div class="svwrap">' + parts.join('') + '</div>';
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
