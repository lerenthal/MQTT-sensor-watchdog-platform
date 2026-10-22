// Dashboard JS — kept in a separate file because the JSON-string backslash
// escapes in single-file Python string literals are too easy to get wrong.
// Loaded by app.py via {js_from_file} substitution at request time.

let ALL_DEVICES = [];
let SORT_KEY = localStorage.getItem('watchdog-sort-key') || null;
let SORT_DIR = localStorage.getItem('watchdog-sort-dir') || 'asc';
let SEARCH_DEBOUNCE = null;

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
    const sorted = (d.sensors || []).slice().sort((a, b) => {
        if (a.sensor_name !== b.sensor_name) return a.sensor_name.localeCompare(b.sensor_name);
        return a.sensor_type.localeCompare(b.sensor_type);
    });
    const lines = sorted.map(s => {
        const v = (typeof s.value === 'number') ? s.value.toFixed(2) : '--';
        return '<div><span class="ts">' + escapeHtml(s.sensor_name) + '</span> &middot; '
             + escapeHtml(s.sensor_type) + ': <strong>' + escapeHtml(v) + '</strong></div>';
    }).join('');
    const id = encodeURIComponent(d.device_id);
    return '<span class="muted">' + d.sensor_count + ' sensor'
        + (d.sensor_count !== 1 ? 's' : '') + '</span>'
        + ' <button class="btn btn-sm" onclick="toggleSensors(\''
        + escapeHtml(d.device_id) + '\', this)">&#9656;</button>'
        + '<div id="sensors-' + id + '" '
        + 'style="display:none;margin-top:.5rem;padding:.5rem;'
        + 'background:var(--bg);border-radius:6px;font-size:.85rem;">'
        + lines + '</div>';
}

window.toggleSensors = function (deviceId, btn) {
    const id = 'sensors-' + encodeURIComponent(deviceId);
    const el = document.getElementById(id);
    if (!el) return;
    const open = el.style.display !== 'none';
    el.style.display = open ? 'none' : 'block';
    btn.innerHTML = open ? '&#9656;' : '&#9662;';
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

    return '<tr data-device="' + escapeHtml(d.device_id)
        + '" data-status="' + escapeHtml(d.status)
        + '" data-location="' + escapeHtml(d.location || '')
        + '" data-role="' + escapeHtml(d.role || '')
        + '" data-temp="' + (d.last_temp ?? '')
        + '" data-hum="' + (d.last_hum ?? '')
        + '" data-co2="' + (d.last_co2 ?? '')
        + '" data-lastseen="' + (d.last_seen || '')
        + '" data-heartbeat="' + (d.last_heartbeat_value ?? '')
        + '">'
        + '<td><strong>' + escapeHtml(d.device_id) + '</strong></td>'
        + '<td>' + escapeHtml(d.location || '')
          + '<br><span class="muted" style="font-size:.75rem">'
          + escapeHtml(d.role || '') + '</span></td>'
        + '<td>' + status + '</td>'
        + '<td class="ts">' + (d.last_seen || 'Never') + '</td>'
        + '<td>' + (d.last_heartbeat_value ?? 'N/A') + '</td>'
        + '<td>' + (d.last_temp ?? '--') + '</td>'
        + '<td>' + (d.last_hum ?? '--') + '</td>'
        + '<td>' + (d.last_co2 ?? '--') + '</td>'
        + '<td>' + sensorCell(d) + '</td>'
        + '<td>' + snooze + '</td>'
        + '<td>' + actions + '</td>'
        + '</tr>';
}

function applyFilter() {
    const q = (document.getElementById('search').value || '').toLowerCase().trim();
    const rows = document.querySelectorAll('#devices-tbody tr');
    let visible = 0;
    rows.forEach(r => {
        if (!q) { r.style.display = ''; visible++; return; }
        const haystack = (r.dataset.device + ' ' + r.dataset.location + ' '
            + r.dataset.role + ' ' + r.dataset.status + ' ' + r.dataset.temp
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

async function refresh() {
    try {
        const res = await fetch('/api/devices');
        if (res.status === 401) { window.location = '/login'; return; }
        const data = await res.json();
        ALL_DEVICES = data.devices || [];
        const tbody = document.getElementById('devices-tbody');
        if (!ALL_DEVICES.length) {
            tbody.innerHTML = '<tr><td colspan="11" class="muted">'
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
});

refresh();
setInterval(refresh, window.REFRESH_SECONDS * 1000);
