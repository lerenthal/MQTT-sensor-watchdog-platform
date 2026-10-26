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
// The repeating alarm timer for an unacknowledged fault. The audio engine
// itself lives in alarm.js, shared with the operator dashboard, so the tone
// and the mute state are the same on both screens.
let beepTimer = null;

// ---------------------------------------------------------------------------
// Style selection
//
// The site default is set in the config (watchdog.buzzer_style). The
// operator can audition a different style on this screen - handy when
// deciding which one to make the site default - and the choice sticks in
// this browser's local storage. It does NOT change the site default.
//
// The engine itself lives in alarm.js, shared with the operator dashboard.
// ---------------------------------------------------------------------------
// The wall board writes status text into the shared alarm control.
function setStatus(text, cls) {
    const s = document.getElementById('alarm-status');
    if (!s) return;
    s.textContent = text;
    s.className = 'alarm-status ' + (cls || '');
}

window.setBuzzStyle = function (name) {
    alarmAudio.setStyle(name, true);
    const st = BUZZ_STYLES[name];
    setStatus('Style: ' + (st ? st.label : name), 'ok');
    alarmAudio.arm();
    alarmAudio.play(1);
};

// The wall board mounts the shared alarm control. The ribbon markup is
// rendered by the server; AlarmUI fills it with the TEST / MUTE buttons and
// the style dropdown.
document.addEventListener('DOMContentLoaded', function () {
    AlarmUI.mount('alarm-ribbon');
});

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
        document.title = (window.APP_NAME || 'Device Watchdog') + ' - ALL CLEAR';
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
                   + ' - ' + (window.APP_NAME || 'Device Watchdog');

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
        if (isNew) alarmAudio.play(ALARM.minFlashCount);        // one-shot on a new fault only
        if (!beepTimer) beepTimer = setInterval(() => alarmAudio.play(2), ALARM.beepIntervalMs);
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
        alarmAudio.play(3);
        if (!beepTimer) beepTimer = setInterval(() => alarmAudio.play(2), ALARM.beepIntervalMs);
    }
}

poll();
setInterval(poll, window.REFRESH_SECONDS * 1000);