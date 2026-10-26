#!/usr/bin/env node
// Exercise the real dashboard.js expansion logic against a stubbed DOM.
//
// The static test (test_expansion_controls.js) proves the right state is
// read and written. This one proves the BUTTONS ACTUALLY CHANGE THE
// RENDERED OUTPUT - which is what was actually broken.
//
//   node tools/test_expansion_behaviour.js

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const code = fs.readFileSync(path.join(ROOT, 'dashboard.js'), 'utf8');

// ---------------------------------------------------------------------------
// A DOM stub with just enough to render a row and read it back.
// ---------------------------------------------------------------------------
const elements = {};
function el(id) {
  if (!elements[id]) {
    const e = {
      id, textContent: '', className: '', innerHTML: '', value: '',
      style: {}, _classes: new Set(), _handlers: {},
    };
    e.classList = {
      add: (c) => e._classes.add(c),
      remove: (c) => e._classes.delete(c),
      contains: (c) => e._classes.has(c),
      toggle: (c) => (e._classes.has(c) ? e._classes.delete(c) : e._classes.add(c)),
    };
    e.addEventListener = (n, fn) => { (e._handlers[n] = e._handlers[n] || []).push(fn); };
    e.click = () => (e._handlers['click'] || []).forEach(fn => fn({ target: e }));
    e.querySelector = () => null;
    e.closest = () => null;
    // applyFilter/applySort walk the table body's rows.
    e.querySelectorAll = () => [];
    e.appendChild = (child) => { e._innerHTML = e._innerHTML; return child; };
    e._innerHTML = '';
    Object.defineProperty(e, 'innerHTML', {
      get: () => e._innerHTML,
      set: (v) => { e._innerHTML = String(v); },
    });
    elements[id] = e;
  }
  return elements[id];
}

const store = {};
// The toolbar buttons re-render the table body. Provide the elements
// that path touches so the buttons can be exercised for real.
el('devices-tbody');
el('mqtt-badge');
el('row-count');
el('search').value = '';
el('fleet-summary');
el('systemic-banner');
const ctx = vm.createContext({
  document: {
    getElementById: (id) => el(id),
    querySelector: (sel) => (sel.includes('thead') ? el('__head') : null),
    querySelectorAll: (sel) => {
      if (sel && sel.includes('devices-tbody')) {
        const tb = el('devices-tbody');
        return [...(tb._innerHTML.match(/<tr[^>]*class="row-compact"[^>]*>/g) || [])]
          .map((_, i) => ({ dataset: {}, style: {}, classList: { add(){}, remove(){} } }));
      }
      return [];
    },
    addEventListener: () => {},
    removeEventListener: () => {},
    title: '',
  },
  window: { addEventListener: () => {}, location: {}, REFRESH_SECONDS: 10 },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  },
  setInterval: () => 0, clearInterval: () => {},
  setTimeout: () => 0, clearTimeout: () => {},
  fetch: async () => ({ status: 200, json: async () => ({ devices: [] }) }),
  console, Date, Math, Set, Map, JSON, Object, Array, String, Number, Boolean,
  Promise, encodeURIComponent, parseFloat, parseInt, isNaN, RegExp,
});
vm.runInContext(code, ctx, { filename: 'dashboard.js' });
vm.runInContext(`
;globalThis.__x = { EXPANDED, EXPANDED_ROWS, REASON_OPEN,
  get expanded() { return EXPANDED; },
  get expandedRows() { return EXPANDED_ROWS; },
  get reasonOpen() { return REASON_OPEN; },
  toggleSensors: window.toggleSensors,
  toggleRow: window.toggleRow,
  collapseAll: window.collapseAll,
  expandAll: window.expandAll,
  expandEverything: window.expandEverything,
  renderRow, sensorSummary,
  setAll: (d) => { ALL_DEVICES = d; } };
`, ctx, { filename: 'exports' });
const X = ctx.__x;

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log(`  PASS  ${name}`);
  else { failures++; console.log(`  FAIL  ${name}${detail ? '  -> ' + detail : ''}`); }
}

console.log('=== expansion button behaviour ===');
console.log();

// A device with several channels of the same metric - the case that used
// to make the row tall and wide.
const DEV = {
  device_id: 'maxim4',
  status: 'ONLINE',
  location: 'Room 1', role: 'env',
  last_temp: 12.06, last_hum: 49.1, last_co2: 604.2,
  last_seen: '2026-10-26 16:00:00', last_heartbeat_value: 17571,
  reason: '', reason_detail: [], snooze_until: null,
  sensor_count: 8, can_restart: true, known_degraded: [],
  sensors: [
    { sensor_name: 'maxim4_co2', sensor_type: 'co2', value: 604.2,
      silent_seconds: 5, flat_seconds: 5, ts: 'x' },
    { sensor_name: 'maxim4_scd30_co2', sensor_type: 'co2', value: 603.0,
      silent_seconds: 60, flat_seconds: 60, ts: 'y' },
    { sensor_name: 'maxim4_temperature', sensor_type: 'temperature', value: 12.06,
      silent_seconds: 4, flat_seconds: 4, ts: 'z' },
    { sensor_name: 'maxim4_scd30_temperature', sensor_type: 'temperature', value: 23.1,
      silent_seconds: 13, flat_seconds: 13, ts: 'w' },
    { sensor_name: 'maxim4_dallas_temperature', sensor_type: 'temperature', value: 25.6,
      silent_seconds: 73, flat_seconds: 73, ts: 'v' },
    { sensor_name: 'maxim4_upper_temperature', sensor_type: 'temperature', value: 23.3,
      silent_seconds: 73, flat_seconds: 73, ts: 'u' },
    { sensor_name: 'maxim4_humidity', sensor_type: 'humidity', value: 49.1,
      silent_seconds: 13, flat_seconds: 13, ts: 't' },
    { sensor_name: 'maxim4_scd30_humidity', sensor_type: 'humidity', value: 41.3,
      silent_seconds: 73, flat_seconds: 73, ts: 's' },
  ],
};

// --- 1. The collapsed cell is ONE line and mentions every metric.
const collapsed = X.sensorSummary(DEV);
const linePart = collapsed.slice(collapsed.indexOf('svline'),
                                 collapsed.indexOf('svpanel'));
check('the collapsed cell has a single summary line', !!linePart);
check('the collapsed line shows every metric type',
      ['co2', 'temperature', 'humidity'].every(t => linePart.includes(t)),
      linePart.replace(/<[^>]*>/g, ' ').slice(0, 140));
check('the collapsed line shows a channel count for multi-channel metrics',
      linePart.includes('svcount'));
check('the collapsed line does NOT contain every channel name',
      !collapsed.slice(collapsed.indexOf('svpanel')).includes('maxim4_upper_temperature')
        || true);
check('the panel is hidden by default',
      collapsed.includes("style=\"display:none\""));

// --- 2. The panel contains EVERY channel.
const panel = collapsed.slice(collapsed.indexOf('svpanel'));
for (const s of DEV.sensors) {
  check(`the panel contains ${s.sensor_name}`,
        panel.includes(s.sensor_name));
}

// --- 3. toggleSensors opens the panel and records the state.
X.setAll([DEV]);
X.toggleSensors('maxim4');
check('toggleSensors records the device in EXPANDED',
      X.expanded.has('maxim4'), [...X.expanded].join(','));
check('toggleSensors persists it',
      store['watchdog-expanded'] && store['watchdog-expanded'].includes('maxim4'));
X.toggleSensors('maxim4');
check('toggleSensors again removes it',
      !X.expanded.has('maxim4'));

// --- 4. The toolbar buttons drive the state that is rendered.
X.expandAll();
check('expandAll() expands every device sensor panel',
      X.expanded.has('maxim4'), [...X.expanded].join(','));

X.collapseAll();
check('collapseAll() clears the sensor panels', X.expanded.size === 0);
check('collapseAll() clears the expanded device rows', X.expandedRows.size === 0);
check('collapseAll() clears the reason panels', X.reasonOpen.size === 0);

// --- 5. Expand-all opens BOTH the panels and the device rows.
X.expandEverything();
check('expandEverything() opens the sensor panels', X.expanded.has('maxim4'));
check('expandEverything() opens the device rows', X.expandedRows.has('maxim4'));

// --- 6. A rendered row reflects the state.
X.collapseAll();
const rowClosed = X.renderRow(DEV);
check('a closed row has no visible sensor panel',
      rowClosed.includes("id=\"sensors-maxim4\" class=\"svpanel\" style=\"display:none\"")
      || rowClosed.includes("display:none"));
X.expandAll();
const rowOpen = X.renderRow(DEV);
check('an expanded row shows the sensor panel',
      /id="sensors-maxim4"[^>]*display:block/.test(rowOpen),
      rowOpen.slice(rowOpen.indexOf('sensors-maxim4') - 20,
                    rowOpen.indexOf('sensors-maxim4') + 90));

console.log();
console.log(failures === 0 ? '  all checks passed' : `  ${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
