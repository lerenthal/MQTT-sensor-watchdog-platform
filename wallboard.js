// Wall-display alarm banner.
//
// Purpose: a big screen in the facility that anyone can see. It shows the
// most urgent active faults, large, with an audible cue. It is deliberately
// separate from the operator dashboard: this one has no controls, assumes no
// interaction, and is designed to be legible from across a room.
//
// Behaviour:
//   * New faults trigger a full-screen flash + a repeating tone.
//   * The banner auto-dismisses after a configurable window so a screen left
//     unattended does not scream forever, but the dismissal is recorded.
//   * Ack / Snooze from the dashboard or Telegram clears it, because those
//     are the actions that mean a human has seen it.
//   * If the screen has audio, it beeps. If not, the visual pulse alone
//     carries it - no error, no dead button.

const ALARM = {
    beepIntervalMs: 4000,
    autoDismissSeconds: 60,
    minFlashCount: 3,
    // While a fault is still unacknowledged, re-arm the flash/beep every few
    // minutes so a screen nobody is watching does not go quiet forever
    // (review finding B7/§5.6). Set to 0 to disable re-flashing.
    reflashSeconds: 300,
};

// Alarm run state. The previous implementation re-added the auto-dismiss class
// (and re-beeped) on every 5s poll, so the flash never showed and the buzzer
// never stopped. We now track which faults are already on screen and when the
// current alarm run started, and drive flash/beep off that — not off the poll.
let lastFaultKeys = new Set();
let alarmSince = 0;      // ms timestamp when the current (un-dismissed) run began

// Sound is synthesised with the Web Audio API: no asset to ship, works
// offline, and browsers only permit it after a user gesture on the page.
// Browsers block audio before any interaction, so we arm it on first click
// or keypress and remember that it is armed.
let audioCtx = null;
let audioArmed = false;
let beepTimer = null;

function armAudio() {
    if (audioArmed) return;
    try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        audioArmed = true;
        // A click anywhere counts as the gesture.
        document.removeEventListener('click', armAudio);
        document.removeEventListener('keydown', armAudio);
        updateAudioWarning();
    } catch (e) {
        audioArmed = false;
        updateAudioWarning();
    }
}
document.addEventListener('click', armAudio);
document.addEventListener('keydown', armAudio);

// The browser blocks audio until a real user gesture, so on a kiosk TV the
// alarm would be silent without this prompt. Show a persistent banner until
// the context is actually RUNNING (not merely created - a created context
// can still be suspended).
function updateAudioWarning() {
    const el = document.getElementById('audio-warn');
    if (!el) return;
    const running = audioCtx && audioCtx.state === 'running';
    if (audioCtx && audioCtx.state === 'suspended') {
        audioCtx.resume().then(updateAudioWarning).catch(() => {});
    }
    el.style.display = (audioArmed && running) ? 'none' : '';
}

// Check periodically: some browsers create the context suspended and only
// resume it on a later gesture.
setInterval(updateAudioWarning, 2000);
window.addEventListener('DOMContentLoaded', updateAudioWarning);

// ---------------------------------------------------------------------------
// Buzzer styles
//
// A facility alarm has to be noticed, but it also has to be tolerable - a
// tone that is too aggressive gets muted and then nobody hears the real
// one. Five styles, from a polite chime to a full klaxon, so the site can
// pick the level that matches its noise floor and shift.
//
//   chime    - two soft rising notes. Almost polite. Won't carry across a
//              noisy room.
//   warble   - a fast alternating warble. Noticeable, still not alarming.
//   siren    - the classic rising/falling two-tone siren. This is the
//              default: loud enough to carry, patterned enough to read as
//              "alarm" and not "equipment noise".
//   klaxon   - a low braying tone with a harsh harmonic. Hard to ignore.
//   continuous
//            - an unbroken tone, the most intrusive option. For a site
//              where a missed alarm is a GMP incident rather than an
//              inconvenience.
//
// Each style is a list of tones: {f: Hz, t: seconds, gain: 0..1, wave:
// oscillator type}. A tone with f = 0 is a rest.
// ---------------------------------------------------------------------------
const BUZZ_STYLES = {
    chime: {
        label: 'Chime (gentle)',
        gap: 0.5,
        tones: [
            { f: 660, t: 0.18, gain: 0.16, wave: 'sine' },
            { f: 990, t: 0.30, gain: 0.16, wave: 'sine' },
        ],
    },
    warble: {
        label: 'Warble (noticeable)',
        gap: 0.25,
        tones: [
            { f: 740, t: 0.10, gain: 0.22, wave: 'square' },
            { f: 560, t: 0.10, gain: 0.22, wave: 'square' },
            { f: 740, t: 0.10, gain: 0.22, wave: 'square' },
            { f: 560, t: 0.10, gain: 0.22, wave: 'square' },
        ],
    },
    siren: {
        label: 'Siren (default - recommended)',
        gap: 0.35,
        tones: [
            { f: 800, t: 0.22, gain: 0.26, wave: 'square' },
            { f: 1000, t: 0.22, gain: 0.26, wave: 'square' },
            { f: 1200, t: 0.22, gain: 0.26, wave: 'square' },
            { f: 1000, t: 0.22, gain: 0.26, wave: 'square' },
        ],
    },
    klaxon: {
        label: 'Klaxon (loud, harsh)',
        gap: 0.30,
        tones: [
            { f: 300, t: 0.55, gain: 0.34, wave: 'sawtooth' },
            { f: 452, t: 0.55, gain: 0.26, wave: 'sawtooth' },
        ],
    },
    continuous: {
        label: 'Continuous (most intrusive)',
        gap: 0.15,
        tones: [
            { f: 620, t: 1.60, gain: 0.30, wave: 'square' },
            { f: 933, t: 1.60, gain: 0.24, wave: 'sawtooth' },
        ],
    },
};

const DEFAULT_BUZZ_STYLE = 'siren';

// The active style. Set from the server config, and overridable per
// session with setBuzzStyle() so an operator can audition a style without
// changing the site default.
let activeStyle = DEFAULT_BUZZ_STYLE;

function styleDef() {
    return BUZZ_STYLES[activeStyle] || BUZZ_STYLES[DEFAULT_BUZZ_STYLE];
}

// Play the alarm tone. Returns true if sound was actually scheduled.
// `times` is how many bursts to play.
function beep(times) {
    if (!audioArmed || !audioCtx) return false;   // no audio hardware: visual only
    // A context can be created but left suspended by the browser's autoplay
    // policy. Resume it - this is the call that actually unblocks sound once
    // a real user gesture has happened.
    if (audioCtx.state === 'suspended') {
        audioCtx.resume().catch(() => {});
    }
    const st = styleDef();
    const cycle = st.tones.reduce((a, t) => a + t.t, 0) + st.gap;
    for (let i = 0; i < times; i++) {
        let t0 = audioCtx.currentTime + i * cycle;
        st.tones.forEach((tone) => {
            const start = t0;
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = tone.wave || 'square';
            osc.frequency.value = tone.f;
            gain.gain.setValueAtTime(0.0001, start);
            gain.gain.exponentialRampToValueAtTime(tone.gain, start + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, start + tone.t);
            osc.connect(gain).connect(audioCtx.destination);
            osc.start(start);
            osc.stop(start + tone.t + 0.02);
            t0 += tone.t;
        });
    }
    return true;
}

// ---------------------------------------------------------------------------
// BEEP TEST - a toggle, not a one-shot.
//
// Press once: the test alarm starts and keeps going (tone + screen flash).
// Press again: it stops.
//
// A one-shot test that cannot be stopped is worse than useless - the
// operator ends up hunting for the TV remote to mute it. The button
// therefore shows what it will do next ("TEST ALARM" when idle, "STOP
// ALARM" while running) and always offers the stop action.
// ---------------------------------------------------------------------------
let testAlarmTimer = null;   // repeating timer for the sustained test tone
let testAlarmFlashTimer = null;
let testAlarmOn = false;

function setStatus(text, cls) {
    const status = document.getElementById('beep-status');
    if (!status) return;
    status.textContent = text;
    status.className = 'beep-status ' + (cls || '');
}

function updateBeepButton(on) {
    const btn = document.getElementById('beep-btn');
    if (!btn) return;
    btn.classList.toggle('on', !!on);
    btn.innerHTML = on
        ? '&#128266; STOP ALARM'
        : '&#128266; TEST ALARM';
}

function startTestAlarm() {
    testAlarmOn = true;
    updateBeepButton(true);

    // Play immediately, then keep repeating so the operator can confirm it
    // is sustained rather than a single blip.
    beep(1);
    testAlarmTimer = setInterval(() => beep(1), 1500);

    // Keep the screen flashing too, so the visual alarm is verifiable on a
    // display with no speakers.
    const stage = document.getElementById('stage');
    if (stage) {
        stage.classList.add('beep-test-flash');
        testAlarmFlashTimer = setInterval(() => {
            stage.classList.remove('beep-test-flash');
            // Force a reflow so the animation restarts.
            void stage.offsetWidth;
            stage.classList.add('beep-test-flash');
        }, 1200);
    }
}

function stopTestAlarm() {
    testAlarmOn = false;
    updateBeepButton(false);
    if (testAlarmTimer) { clearInterval(testAlarmTimer); testAlarmTimer = null; }
    if (testAlarmFlashTimer) { clearInterval(testAlarmFlashTimer); testAlarmFlashTimer = null; }
    const stage = document.getElementById('stage');
    if (stage) stage.classList.remove('beep-test-flash');
    setStatus('Alarm stopped.', '');
}

// BEEP TEST. Plays the same tone the alarm uses, so an operator can confirm
// the screen can actually make a sound before they rely on it. Also reports
// WHY it might be silent, because "no sound" is the single most likely
// reason a wall display is not doing its job.
window.beepTest = function () {
    // Tapping the button again stops whatever is running. Checked first so
    // the stop action always works, even if audio failed to arm.
    if (testAlarmOn) {
        stopTestAlarm();
        return;
    }

    // This click IS the user gesture the browser needs, so arm first.
    armAudio();

    if (!audioCtx) {
        setStatus('No Web Audio support in this browser - the alarm will be VISUAL ONLY.',
                  'bad');
        // Still flash the screen so the operator sees something happen.
        startTestAlarm();
        return;
    }

    if (audioCtx.state === 'suspended') {
        audioCtx.resume().then(() => {
            const ok = beep(1);
            setStatus(ok
                ? 'Playing - if you hear nothing, check the TV/system volume. Press again to stop.'
                : 'Could not schedule audio.',
                ok ? 'ok' : 'bad');
            startTestAlarm();
        }).catch((err) => {
            setStatus('Audio blocked by the browser (' + (err && err.name || 'error')
                      + '). Click the page once, then try again.', 'bad');
            startTestAlarm();
        });
        return;
    }

    const ok = beep(1);
    setStatus(ok
        ? 'Playing - if you hear nothing, check the TV/system volume. Press again to stop.'
        : 'Could not schedule audio.',
        ok ? 'ok' : 'bad');
    startTestAlarm();
    updateAudioWarning();
};

// Briefly flash the whole screen, so the operator can also verify the
// VISUAL alarm works even when the TV has no speakers.
function flashTest() {
    const stage = document.getElementById('stage');
    if (!stage) return;
    stage.classList.add('beep-test-flash');
    setTimeout(() => stage.classList.remove('beep-test-flash'), 1500);
}

function stopBeep() {
    if (beepTimer) { clearInterval(beepTimer); beepTimer = null; }
}

// ---------------------------------------------------------------------------
// Style selection
//
// The site default is set in the config (watchdog.buzzer_style). An
// operator can audition a different style on this screen - handy when
// deciding which one to make the site default - and the choice sticks in
// this browser's local storage. It does NOT change the site default.
// ---------------------------------------------------------------------------
const STYLE_KEY = 'watchdog-buzzer-style';

function applyStyle(name, persist) {
    if (!BUZZ_STYLES[name]) return;
    activeStyle = name;
    if (persist) {
        try { localStorage.setItem(STYLE_KEY, name); } catch (e) { /* private mode */ }
    }
    const sel = document.getElementById('buzzer-style');
    if (sel) sel.value = name;
    const lbl = document.getElementById('buzzer-style-label');
    if (lbl) lbl.textContent = BUZZ_STYLES[name].label;
}

window.setBuzzStyle = function (name) {
    applyStyle(name, true);
    const st = BUZZ_STYLES[name];
    setStatus('Style: ' + (st ? st.label : name), 'ok');
    // Audition it immediately so the operator hears what they selected.
    armAudio();
    beep(1);
};

function initStyle() {
    let name = DEFAULT_BUZZ_STYLE;
    // Server-provided site default wins on a fresh browser.
    if (window.SITE_BUZZ_STYLE && BUZZ_STYLES[window.SITE_BUZZ_STYLE]) {
        name = window.SITE_BUZZ_STYLE;
    }
    // A local override (an audition) takes precedence over the site default.
    try {
        const saved = localStorage.getItem(STYLE_KEY);
        if (saved && BUZZ_STYLES[saved]) name = saved;
    } catch (e) { /* private mode */ }
    applyStyle(name, false);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function badge(sev, cls) {
    return '<span class="sev sev-' + (cls || sev.toLowerCase()) + '">'
         + sev + '</span>';
}

function render(faults, mqttOk, meta) {
    const stage = document.getElementById('stage');
    const clock = document.getElementById('clock');
    clock.textContent = new Date().toLocaleTimeString();

    document.getElementById('mqtt-state').textContent = mqttOk ? 'BROKER OK' : 'BROKER DOWN';
    document.getElementById('mqtt-state').className = mqttOk ? 'mqtt ok' : 'mqtt down';

    if (!faults.length) {
        stage.className = 'stage all-clear';
        stage.innerHTML =
            '<div class="clear-mark">&#10003;</div>'
          + '<div class="clear-text">ALL SENSORS NOMINAL</div>'
          + '<div class="clear-sub">'
          + meta.total + ' devices monitored &middot; last packet '
          + escapeHtml(meta.lastPacket || 'never') + '</div>';
        stopBeep();
        document.title = 'Sensor Watchdog - ALL CLEAR';
        // Reset alarm run state so the next fault flashes/beeps as "new".
        lastFaultKeys = new Set();
        alarmSince = 0;
        return;
    }

    const crit = faults.filter(f => f.severity === 'CRITICAL').length;

    // Decide whether this poll introduces a NEW fault (one not on screen last
    // poll). A new fault (re-)arms the flash and the beep.
    const keys = new Set(faults.map(f => f.device_id + '|' + f.alert_type));
    let isNew = false;
    keys.forEach(k => { if (!lastFaultKeys.has(k)) isNew = true; });
    lastFaultKeys = keys;

    const now = Date.now();
    if (isNew || alarmSince === 0) {
        alarmSince = now;
    } else if (ALARM.reflashSeconds > 0
               && (now - alarmSince) >= ALARM.reflashSeconds * 1000) {
        // Still unacknowledged after the re-flash interval: arm again.
        alarmSince = now;
    }

    document.title = '!! ' + faults.length + ' FAULT' + (faults.length === 1 ? '' : 'S')
                   + ' - Sensor Watchdog';

    const rows = faults.slice(0, 12).map(f =>
        '<tr><td>' + badge(f.severity, f.severity === 'CRITICAL' ? 'crit' : 'warn')
      + '</td><td class="dev">' + escapeHtml(f.device_id) + '</td>'
      + '<td class="typ">' + escapeHtml(f.alert_type) + '</td>'
      + '<td class="msg">' + escapeHtml(f.message) + '</td>'
      + '<td class="since">' + escapeHtml((f.triggered_at || '').slice(5, 19)) + '</td></tr>'
    ).join('');

    stage.innerHTML =
        '<div class="alarm-head">'
      + '<div class="alarm-title">'
      + (crit ? '<span class="crit-n">' + crit + ' CRITICAL</span> ' : '')
      + '<span class="tot-n">' + faults.length + ' ACTIVE FAULT'
      + (faults.length === 1 ? '' : 'S') + '</span></div>'
      + '<div class="alarm-sub">' + meta.total + ' devices monitored'
      + (meta.systemic ? ' &middot; SYSTEMIC FAULT DETECTED' : '') + '</div></div>'
      + '<table class="faults"><thead><tr><th>Sev</th><th>Device</th>'
      + '<th>Type</th><th>Detail</th><th>Since</th></tr></thead><tbody>'
      + rows + '</tbody></table>'
      + (faults.length > 12
          ? '<div class="more">+ ' + (faults.length - 12) + ' more</div>' : '');

    // Within the auto-dismiss window → flash + beep; after it → go static and
    // silent until a new fault (or the re-flash interval) re-arms. Derived from
    // alarmSince, so a steady 5s poll no longer resets it every tick (B7).
    const withinWindow = ALARM.autoDismissSeconds <= 0
        || (now - alarmSince) < ALARM.autoDismissSeconds * 1000;

    if (withinWindow) {
        stage.className = 'stage alarming';          // flashing (not auto-dismissed)
        if (isNew) beep(ALARM.minFlashCount);        // one-shot on a new fault only
        if (!beepTimer) beepTimer = setInterval(() => beep(2), ALARM.beepIntervalMs);
    } else {
        stage.className = 'stage alarming auto-dismissed';  // static, readable
        stopBeep();
    }
}

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
}

// ---------------------------------------------------------------------------
// Poll
// ---------------------------------------------------------------------------
async function poll() {
    try {
        const res = await fetch('/api/wallboard');
        if (res.status === 401) { window.location = '/login'; return; }
        const d = await res.json();
        render(d.faults || [], d.mqtt_ok, d);
    } catch (e) {
        document.getElementById('stage').className = 'stage alarming';
        document.getElementById('stage').innerHTML =
            '<div class="alarm-head"><div class="alarm-title">'
          + '<span class="crit-n">WATCHDOG UNREACHABLE</span></div>'
          + '<div class="alarm-sub">Cannot reach the monitoring service</div></div>';
        document.title = '!! WATCHDOG UNREACHABLE';
        beep(3);
        if (!beepTimer) beepTimer = setInterval(() => beep(2), ALARM.beepIntervalMs);
    }
}

initStyle();
poll();
setInterval(poll, window.REFRESH_SECONDS * 1000);