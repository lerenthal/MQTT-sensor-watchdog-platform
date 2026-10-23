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
};

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
    } catch (e) {
        audioArmed = false;
    }
}
document.addEventListener('click', armAudio);
document.addEventListener('keydown', armAudio);

function beep(times) {
    if (!audioArmed || !audioCtx) return;   // no audio hardware: visual only
    for (let i = 0; i < times; i++) {
        const t0 = audioCtx.currentTime + i * 0.45;
        // Two-tone: a rising pair reads as "attention" rather than "error beep".
        [0, 0.12].forEach((offset, idx) => {
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = 'square';
            osc.frequency.value = idx === 0 ? 880 : 1175;
            const start = t0 + offset;
            gain.gain.setValueAtTime(0.0001, start);
            gain.gain.exponentialRampToValueAtTime(0.25, start + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.22);
            osc.connect(gain).connect(audioCtx.destination);
            osc.start(start);
            osc.stop(start + 0.25);
        });
    }
}

function stopBeep() {
    if (beepTimer) { clearInterval(beepTimer); beepTimer = null; }
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
        return;
    }

    const crit = faults.filter(f => f.severity === 'CRITICAL').length;
    stage.className = 'stage alarming';

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

    beep(ALARM.minFlashCount);
    if (!beepTimer) beepTimer = setInterval(() => beep(2), ALARM.beepIntervalMs);

    // Auto-dismiss: stop the noise, leave the list readable.
    if (ALARM.autoDismissSeconds > 0) {
        stage.classList.add('auto-dismissed');
        setTimeout(() => {
            stopBeep();
            stage.classList.remove('auto-dismissed');
        }, ALARM.autoDismissSeconds * 1000);
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

poll();
setInterval(poll, window.REFRESH_SECONDS * 1000);