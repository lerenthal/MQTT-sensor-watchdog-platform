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
const ALARM_SRC = path.join(__dirname, '..', 'alarm.js');
const code = fs.readFileSync(SRC, 'utf8');
const alarmCode = fs.readFileSync(ALARM_SRC, 'utf8');

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
    // addEventListener: the control wires its buttons this way. The
    // handlers are recorded so a test can invoke them with a click().
    e._handlers = {};
    e.addEventListener = (name, fn) => {
        (e._handlers[name] = e._handlers[name] || []).push(fn);
    };
    e.click = () => {
        (e._handlers['click'] || []).forEach(fn => fn({ target: e }));
    };
    e.querySelector = () => null;
    e.closest = () => null;
    e.change = (v) => {
        e.value = v;
        (e._handlers['change'] || []).forEach(fn => fn({ target: e }));
    };
    // A real DOM keeps textContent and innerHTML in sync. The button label
    // is set via innerHTML, so mirror that for the text assertions.
    let _html = '';
    Object.defineProperty(e, 'innerHTML', {
      get: () => _html,
      set: (v) => {
        _html = String(v);
        e.textContent = _html.replace(/<[^>]*>/g, '').trim();
        // A real DOM builds child nodes from the markup. The control looks
        // its buttons up with getElementById, so the stub must create them
        // or the test would be asserting against empty placeholders.
        const ids = _html.matchAll(/id="([a-z0-9-]+)"/g);
        for (const m of ids) {
          const child = el(m[1]);
          // Give the child the tag's text content, so a button's label is
          // readable.
          const tagMatch = _html.match(
            new RegExp('<[^>]*id="' + m[1] + '"[^>]*>([\\s\\S]*?)<', 'i'));
          if (tagMatch) {
            child.textContent = tagMatch[1].replace(/<[^>]*>/g, '').trim();
          }
          // A real DOM reflects the class attribute into classList.
          const clsMatch = _html.match(
            new RegExp('<[^>]*id="' + m[1] + '"[^>]*class="([^"]*)"', 'i'));
          if (clsMatch) {
            child._classes = new Set(clsMatch[1].split(/\s+/).filter(Boolean));
          }
          // A re-rendered node is a NEW node with fresh listeners. The
          // stub reuses the object, so drop the old handlers to match.
          child._handlers = {};
        }
        // The container's own class attribute too.
        const selfCls = _html.match(/^<[^>]*class="([^"]*)"/);
        if (selfCls) e._classes = new Set(selfCls[1].split(/\s+/).filter(Boolean));
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
;globalThis.__exports = Object.assign({ setBuzzStyle: window.setBuzzStyle },
  globalThis.__alarm);
`;
vm.runInContext(alarmCode + `
;globalThis.__alarm = { alarmAudio, AlarmUI, BUZZ_STYLES };`,
    ctx, { filename: 'alarm.js' });
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

// --- 2. The shared engine: arm, style, mute.
const audio = X.alarmAudio;
check('alarmAudio exists', !!audio);
check('the engine starts unarmed', audio.state().armed === false);
audio.arm();
check('arm() arms the context', audio.state().armed === true);
check('arm() creates a running context', audio.state().running === true);
console.log();

// --- 3. The style switcher.
X.setBuzzStyle('klaxon');
check('setBuzzStyle changes the active style', audio.style === 'klaxon',
  'style=' + audio.style);
check('setBuzzStyle persists to localStorage',
  localStorage.getItem('watchdog-buzzer-style') === 'klaxon');
console.log();

// --- 4. An unknown style must be rejected, not applied.
const before = audio.style;
X.setBuzzStyle('not-a-style');
check('an unknown style is rejected', audio.style === before,
  'style=' + audio.style);
console.log();

// --- 5. The alarm path uses the selected style.
scheduled.length = 0;
audio.play(1);
check('play schedules oscillators', scheduled.length > 0,
  'scheduled=' + scheduled.length);
const waves = new Set(scheduled.map(s => s.wave));
const freqs = new Set(scheduled.map(s => s.freq));
check('play uses the active style waveform',
  waves.has(X.BUZZ_STYLES.klaxon.tones[0].wave),
  'waves=' + [...waves].join(','));
check('play uses the active style frequencies',
  freqs.has(X.BUZZ_STYLES.klaxon.tones[0].f) || freqs.has(X.BUZZ_STYLES.klaxon.tones[1].f),
  'freqs=' + [...freqs].join(','));
console.log();

// --- 6. Mute: silences the local alarm but is a local, reversible state.
scheduled.length = 0;
audio.setMuted(true);
check('mute sets the muted flag', audio.state().muted === true);
const mutedCount = scheduled.length;
audio.play(1);
check('a muted alarm schedules no sound', scheduled.length === mutedCount,
  `scheduled ${mutedCount} -> ${scheduled.length}`);
audio.setMuted(false);
scheduled.length = 0;
audio.play(1);
check('unmute restores the sound', scheduled.length > 0);
check('mute state persists', localStorage.getItem('watchdog-alarm-muted') === '0');
console.log();

// --- 7. The ribbon control: TEST toggles, MUTE toggles, style dropdown.
el('alarm-ribbon').innerHTML = '';   // AlarmUI renders into this
X.AlarmUI.mount('alarm-ribbon');
// A re-render replaces the button markup, so re-read the elements from
// the container after every render. A real DOM does the same: the old
// node is replaced by a new one with fresh listeners.
function ribbonEls() {
  return {
    btn: el('alarm-test-btn'),
    muteBtn: el('alarm-mute-btn'),
    styleSel: el('alarm-style-select'),
  };
}
let { btn, muteBtn, styleSel } = ribbonEls();
check('the ribbon renders a TEST button', !!btn);
check('the ribbon renders a MUTE button', !!muteBtn);
check('the ribbon renders a style dropdown', !!styleSel);
const optCount = (el('alarm-ribbon').innerHTML.match(/<option/g) || []).length;
check('the style dropdown lists all five styles', optCount === 5,
  'options=' + optCount);

if (btn) {
    check('the TEST button starts idle',
      btn.textContent.includes('TEST') && !btn.textContent.includes('STOP'),
      btn.textContent);
    btn.click();
    ({ btn, muteBtn, styleSel } = ribbonEls());
    check('after one press the button offers STOP',
      btn.textContent.includes('STOP'), btn.textContent);
    check('after one press the button has the "on" class',
      btn.classList.contains('on'));
    check('a repeating timer is running while the test is on',
      Object.keys(timers.intervals).length > 0);
    const whileOn = Object.keys(timers.intervals).length;
    btn.click();
    ({ btn, muteBtn, styleSel } = ribbonEls());
    check('after the second press the button is idle again',
      !btn.classList.contains('on') && btn.textContent.includes('TEST'),
      btn.textContent);
    check('the repeating timer is cleared when stopped',
      Object.keys(timers.intervals).length < whileOn,
      `${Object.keys(timers.intervals).length} vs ${whileOn}`);
}

if (muteBtn) {
    muteBtn.click();
    ({ btn, muteBtn, styleSel } = ribbonEls());
    check('MUTE silences the alarm', audio.state().muted === true);
    check('the MUTE button shows the muted state',
      muteBtn.textContent.includes('MUTED'), muteBtn.textContent);
    muteBtn.click();
    ({ btn, muteBtn, styleSel } = ribbonEls());
    check('MUTE again unmutes', audio.state().muted === false);
    check('the MUTE button shows the unmuted state',
      muteBtn.textContent.includes('MUTE') && !muteBtn.textContent.includes('MUTED'),
      muteBtn.textContent);
}
console.log();

// --- 8. Stopping the test clears the screen flash.
const stage = el('stage');
stage.classList.add('beep-test-flash');
X.AlarmUI.mount('alarm-ribbon');
if (el('alarm-test-btn')) el('alarm-test-btn').click();
if (el('alarm-test-btn')) el('alarm-test-btn').click();
check('stopping clears the screen flash',
  !stage.classList.contains('beep-test-flash'));

console.log();
console.log(failures === 0 ? '  all checks passed' : `  ${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
