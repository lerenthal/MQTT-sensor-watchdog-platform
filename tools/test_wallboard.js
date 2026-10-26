// Headless test of the wallboard alarm toggle and style selector.
//
// The toggle logic is pure DOM + timer behaviour, so it can be exercised
// without a browser by stubbing the few DOM APIs it touches. This catches
// the class of bug where the markup is right but the state machine is not
// (e.g. the button says STOP but the timer keeps running).
//
//   node tools/test_wallboard.js
//
// Requires: node (no npm packages).

const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'wallboard.js');
const code = fs.readFileSync(SRC, 'utf8');

// ---------------------------------------------------------------------------
// Minimal DOM stub
// ---------------------------------------------------------------------------
function makeEl(id) {
  return {
    id,
    textContent: '',
    className: '',
    innerHTML: '',
    value: '',
    style: {},
    _classes: new Set(),
    classList: {
      add: (c) => this && null,
    },
  };
}

// The code under test uses: document.getElementById, classList (add/remove/
// contains/toggle), setTimeout/setInterval, localStorage, AudioContext,
// fetch, window.
const timers = { intervals: {}, timeouts: {} };
let nextTimerId = 1;

const elements = {};
function el(id) {
  if (!elements[id]) {
    const e = {
      id,
      textContent: '',
      className: '',
      innerHTML: '',
      value: '',
      style: {},
      _classes: new Set(),
    };
    e.classList = {
      add: (c) => e._classes.add(c),
      remove: (c) => e._classes.delete(c),
      contains: (c) => e._classes.has(c),
      toggle: (c) => (e._classes.has(c) ? e._classes.delete(c) : e._classes.add(c)),
    };
    // A real DOM keeps textContent and innerHTML in sync. The button label
    // is set via innerHTML, so mirror that for the text assertions.
    let _html = '';
    Object.defineProperty(e, 'innerHTML', {
      get: () => _html,
      set: (v) => {
        _html = String(v);
        e.textContent = _html.replace(/<[^>]*>/g, '').trim();
      },
    });
    elements[id] = e;
  }
  return elements[id];
}

// Pre-create the elements the wallboard expects.
['beep-btn', 'beep-status', 'stage', 'clock', 'mqtt-state', 'audio-warn',
 'buzzer-style'].forEach(el);
// The server renders the button with this label; the stub must start in
// the same state or the "starts idle" assertion proves nothing.
el('beep-btn').innerHTML = '&#128266; TEST ALARM';

const store = {};
global.document = {
  getElementById: (id) => (id in elements ? elements[id] : (el(id), elements[id])),
  querySelector: () => null,
  addEventListener: () => {},
  removeEventListener: () => {},
  title: '',
};
global.window = {
  addEventListener: () => {},
  location: {},
  REFRESH_SECONDS: 5,
  SITE_BUZZ_STYLE: 'siren',
};
// The code reads the AudioContext constructor off `window`.
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
global.setInterval = (fn, ms) => { const id = nextTimerId++; timers.intervals[id] = { fn, ms }; return id; };
global.clearInterval = (id) => { delete timers.intervals[id]; };
global.setTimeout = (fn, ms) => { const id = nextTimerId++; timers.timeouts[id] = { fn, ms }; return id; };
global.clearTimeout = (id) => { delete timers.timeouts[id]; };
global.fetch = async () => ({ status: 200, json: async () => ({ faults: [], mqtt_ok: true, total: 0 }) });
global.escapeHtml = (s) => String(s ?? '');

// A fake AudioContext that records what was scheduled. The real API chain
// is osc -> gain -> destination, and both createOscillator() and
// createGain() must return objects with a connect() method.
const scheduled = [];
global.AudioContext = class {
  constructor() {
    this.state = 'running';
    this.currentTime = 0;
    this.destination = { connect: () => {} };
  }
  resume() { this.state = 'running'; return Promise.resolve(); }
  createOscillator() {
    const osc = {
      type: 'square',
      frequency: { value: 0 },
      connect: (node) => node,
      start: (t) => scheduled.push({ wave: osc.type, freq: osc.frequency.value, at: t }),
      stop: () => {},
    };
    return osc;
  }
  createGain() {
    return {
      gain: {
        setValueAtTime: () => {},
        exponentialRampToValueAtTime: () => {},
      },
      connect: (node) => node,
    };
  }
};

// The code under test reads the constructor from `window`.
global.window.AudioContext = global.AudioContext;

// ---------------------------------------------------------------------------
// Load the code under test
// ---------------------------------------------------------------------------
// Run the file in a vm context whose globals ARE the stubs above, so the
// `const BUZZ_STYLES` declaration is visible to the assertions. A plain
// eval() would scope the const to the eval call.
const vm = require('vm');
const ctx = vm.createContext({
  document: global.document,
  window: global.window,
  localStorage: global.localStorage,
  setInterval: global.setInterval,
  clearInterval: global.clearInterval,
  setTimeout: global.setTimeout,
  clearTimeout: global.clearTimeout,
  fetch: global.fetch,
  AudioContext: global.AudioContext,
  escapeHtml: global.escapeHtml,
  console,
  Date,
  Math,
  Set,
  Promise,
  JSON,
  Object,
  Array,
  String,
  Number,
  Boolean,
});
// `const` at the top level of a vm script does not become a property of the
// context object, so append an export line that publishes the bindings the
// assertions need.
const EXPORTS = `
;globalThis.__exports = { BUZZ_STYLES, initStyle, beep,
  armAudio, setBuzzStyle: window.setBuzzStyle, beepTest: window.beepTest,
  get activeStyle() { return activeStyle; } };
`;
vm.runInContext(code + EXPORTS, ctx, { filename: 'wallboard.js' });
const X = ctx.__exports;
// The exported getter gives access to the script's live `activeStyle`.
const activeStyleRef = { get activeStyle() { return X.activeStyle; } };

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------
let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`  PASS  ${name}`);
  } else {
    failures++;
    console.log(`  FAIL  ${name}${detail ? '  -> ' + detail : ''}`);
  }
}

console.log('=== wallboard alarm toggle + style selector ===');
console.log();

// --- 1. Five styles are defined and each has a label and tones.
const expectedStyles = ['chime', 'warble', 'siren', 'klaxon', 'continuous'];
check('five buzzer styles defined',
  expectedStyles.every(s => X.BUZZ_STYLES[s]),
  'missing: ' + expectedStyles.filter(s => !X.BUZZ_STYLES[s]).join(', '));
check('every style has a label',
  expectedStyles.every(s => X.BUZZ_STYLES[s].label && X.BUZZ_STYLES[s].label.length > 3));
check('every style has tones',
  expectedStyles.every(s => Array.isArray(X.BUZZ_STYLES[s].tones) && X.BUZZ_STYLES[s].tones.length > 0));
check('styles are ordered by intensity (chime -> continuous)',
  X.BUZZ_STYLES.chime.tones[0].gain < X.BUZZ_STYLES.continuous.tones[0].gain,
  `chime ${X.BUZZ_STYLES.chime.tones[0].gain} vs continuous ${X.BUZZ_STYLES.continuous.tones[0].gain}`);
console.log();

// --- 2. The toggle: press starts, press again stops.
X.initStyle();
check('initStyle applied the site default (siren)',
  activeStyleRef.activeStyle === 'siren', 'activeStyle=' + activeStyleRef.activeStyle);

const btn = el('beep-btn');
check('button starts in the idle state', !btn.classList.contains('on'));
check('button label starts as TEST ALARM',
  btn.textContent.includes('TEST ALARM'), btn.textContent);

// Press once.
X.beepTest();
check('after one press the button is in the "on" state', btn.classList.contains('on'));
check('after one press the label offers STOP',
  btn.textContent.includes('STOP'), btn.textContent);
check('a repeating timer is running while the alarm is on',
  Object.keys(timers.intervals).length > 0);
const timersWhileOn = Object.keys(timers.intervals).length;

// Press again.
X.beepTest();
check('after the second press the button leaves the "on" state',
  !btn.classList.contains('on'));
check('after the second press the label is TEST ALARM again',
  btn.textContent.includes('TEST ALARM') && !btn.textContent.includes('STOP'),
  btn.textContent);
check('the repeating timer is cleared when stopped',
  Object.keys(timers.intervals).length < timersWhileOn,
  `${Object.keys(timers.intervals).length} vs ${timersWhileOn}`);
console.log();

// --- 3. The style switcher.
X.setBuzzStyle('klaxon');
check('setBuzzStyle changes the active style', activeStyleRef.activeStyle === 'klaxon',
  'activeStyle=' + activeStyleRef.activeStyle);
check('setBuzzStyle updates the dropdown', el('buzzer-style').value === 'klaxon',
  el('buzzer-style').value);
check('setBuzzStyle persists to localStorage',
  localStorage.getItem('watchdog-buzzer-style') === 'klaxon');
check('setBuzzStyle reports the new label',
  el('beep-status').textContent.includes('Klaxon'), el('beep-status').textContent);

// An unknown style must be rejected, not applied.
const before = activeStyleRef.activeStyle;
X.setBuzzStyle('not-a-style');
check('an unknown style is rejected', activeStyleRef.activeStyle === before,
  'activeStyle=' + activeStyleRef.activeStyle);
console.log();

// --- 4. The alarm path uses the selected style.
scheduled.length = 0;
X.armAudio();
X.beep(1);
check('beep schedules oscillators', scheduled.length > 0,
  'scheduled=' + scheduled.length);
const waves = new Set(scheduled.map(s => s.wave));
const freqs = new Set(scheduled.map(s => s.freq));
check('beep uses the active style waveform',
  waves.has(X.BUZZ_STYLES.klaxon.tones[0].wave),
  'waves=' + [...waves].join(','));
check('beep uses the active style frequencies',
  freqs.has(X.BUZZ_STYLES.klaxon.tones[0].f) || freqs.has(X.BUZZ_STYLES.klaxon.tones[1].f),
  'freqs=' + [...freqs].join(','));
console.log();

// --- 5. Stopping the test alarm also clears the flash.
const stage = el('stage');
stage.classList.add('beep-test-flash');
X.beepTest();      // start
X.beepTest();      // stop
check('stopping clears the screen flash',
  !stage.classList.contains('beep-test-flash'));

console.log();
console.log(failures === 0 ? '  all checks passed' : `  ${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
