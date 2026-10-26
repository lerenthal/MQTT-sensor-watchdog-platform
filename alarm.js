// ============================================================================
// Shared alarm controls
//
// The alarm sound and the mute control belong to the OPERATOR, not to one
// page. An operator looking at the device table must be able to test that
// the facility can raise an audible alarm, and must be able to silence a
// nuisance alarm without leaving the page. The wall board needs the same
// controls. Rather than duplicate the Web Audio code, both pages load this
// module.
//
// Contents:
//   * BUZZ_STYLES - the five alarm tones (see the wallboard for the full
//     rationale behind each one).
//   * alarmAudio  - the Web Audio engine: arm, play a style, mute/unmute.
//   * AlarmUI     - the ribbon control: TEST / MUTE buttons plus a style
//     dropdown, wired to any container element.
//
// The wall board additionally drives a repeating alarm on a new fault. That
// stays in wallboard.js, but it uses the same alarmAudio engine so the
// style and mute state are shared.
// ============================================================================

// ---------------------------------------------------------------------------
// Buzzer styles
//
// A facility alarm has to be noticed, but it also has to be tolerable - a
// tone that is too aggressive gets muted and then nobody hears the real
// one. Five styles, from a polite chime to a full klaxon, so the site can
// pick the level that matches its noise floor and shift.
//
//   chime      - two soft rising notes. Almost polite. Won't carry across
//                 a noisy room.
//   warble     - a fast alternating warble. Noticeable, still not alarming.
//   siren      - the classic rising/falling two-tone siren. This is the
//                 default: loud enough to carry, patterned enough to read
//                 as "alarm" and not "equipment noise".
//   klaxon     - a low braying tone with a harsh harmonic. Hard to ignore.
//   continuous - an unbroken tone, the most intrusive option. For a site
//                 where a missed alarm is a GMP incident rather than an
//                 inconvenience.
//
// Each style is a list of tones: {f: Hz, t: seconds, gain: 0..1, wave:
// oscillator type}.
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

// ---------------------------------------------------------------------------
// The audio engine
//
// Sound is synthesised with the Web Audio API: no asset to ship, works
// offline, and browsers only permit it after a user gesture on the page.
// Browsers block audio before any interaction, so we arm it on first click
// or keypress and remember that it is armed.
//
// Mute is deliberately a LOCAL, session-level state. A muted alarm on the
// wall board must not silence the Telegram paging - those are separate
// channels to separate people, and a muting mistake on a screen should
// never quiet the phone in someone's pocket.
// ---------------------------------------------------------------------------
const alarmAudio = (function () {
    let ctx = null;
    let armed = false;
    let muted = false;
    let style = DEFAULT_BUZZ_STYLE;
    const listeners = [];

    function notify() {
        listeners.forEach(fn => { try { fn(state()); } catch (e) { /* listener error */ } });
    }

    function state() {
        return {
            armed: armed,
            muted: muted,
            style: style,
            running: !!(ctx && ctx.state === 'running'),
            supported: typeof (window.AudioContext || window.webkitAudioContext) === 'function',
        };
    }

    function arm() {
        if (armed) return state();
        try {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return state();
            ctx = new AC();
            armed = true;
            document.removeEventListener('click', arm);
            document.removeEventListener('keydown', arm);
            notify();
        } catch (e) {
            armed = false;
            notify();
        }
        return state();
    }

    function setStyle(name, persist) {
        if (!BUZZ_STYLES[name]) return state();
        style = name;
        if (persist) {
            try { localStorage.setItem('watchdog-buzzer-style', name); } catch (e) { /* private mode */ }
        }
        notify();
        return state();
    }

    function setMuted(v) {
        muted = !!v;
        try { localStorage.setItem('watchdog-alarm-muted', muted ? '1' : '0'); } catch (e) { /* private mode */ }
        notify();
        return state();
    }

    function toggleMute() {
        return setMuted(!muted);
    }

    // Play the alarm tone. Returns true if sound was actually scheduled.
    // `times` is how many bursts to play.
    function play(times) {
        if (!armed || !ctx) return false;   // no audio hardware: visual only
        if (muted) return false;            // operator muted it
        // A context can be created but left suspended by the browser's
        // autoplay policy. Resume it - this is the call that actually
        // unblocks sound once a real user gesture has happened.
        if (ctx.state === 'suspended') {
            ctx.resume().catch(() => {});
        }
        const st = BUZZ_STYLES[style] || BUZZ_STYLES[DEFAULT_BUZZ_STYLE];
        const cycle = st.tones.reduce((a, t) => a + t.t, 0) + st.gap;
        for (let i = 0; i < times; i++) {
            let t0 = ctx.currentTime + i * cycle;
            st.tones.forEach((tone) => {
                const start = t0;
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = tone.wave || 'square';
                osc.frequency.value = tone.f;
                gain.gain.setValueAtTime(0.0001, start);
                gain.gain.exponentialRampToValueAtTime(tone.gain, start + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, start + tone.t);
                osc.connect(gain).connect(ctx.destination);
                osc.start(start);
                osc.stop(start + tone.t + 0.02);
                t0 += tone.t;
            });
        }
        return true;
    }

    // Restore the persisted style and mute state.
    function restore() {
        try {
            const s = localStorage.getItem('watchdog-buzzer-style');
            if (s && BUZZ_STYLES[s]) style = s;
            muted = localStorage.getItem('watchdog-alarm-muted') === '1';
        } catch (e) { /* private mode */ }
        notify();
    }

    function onChange(fn) { listeners.push(fn); }

    return {
        arm: arm, play: play, state: state, onChange: onChange,
        setStyle: setStyle, setMuted: setMuted, toggleMute: toggleMute,
        restore: restore,
        get context() { return ctx; },
        get style() { return style; },
    };
})();

// ---------------------------------------------------------------------------
// The ribbon control
//
// Renders into a container element:
//
//   <div id="alarm-ribbon"></div>
//
// and gives the operator:
//   TEST   - play the alarm tone so they can confirm the facility can be
//            heard. Toggles: press to start, press again to stop.
//   MUTE   - silence the local alarm. Deliberately does NOT stop the
//            Telegram paging or the visual alarm.
//   style  - a dropdown to audition the five tones.
// ---------------------------------------------------------------------------
const AlarmUI = (function () {
    let container = null;
    let testTimer = null;
    let flashTimer = null;
    let testRunning = false;

    function el(id) { return document.getElementById(id); }

    function render() {
        if (!container) return;
        const s = alarmAudio.state();
        const style = BUZZ_STYLES[s.style] || BUZZ_STYLES[DEFAULT_BUZZ_STYLE];
        const options = Object.keys(BUZZ_STYLES).map(k =>
            '<option value="' + k + '"' + (k === s.style ? ' selected' : '') + '>'
            + BUZZ_STYLES[k].label + '</option>').join('');

        container.innerHTML =
            '<span class="alarm-ctl-label">Alarm</span>'
          + '<button id="alarm-test-btn" class="alarm-btn'
          + (testRunning ? ' on' : '') + '" type="button"'
          + ' title="Play the alarm tone to confirm the facility can be heard. '
          + 'Press again to stop.">'
          + (testRunning ? '&#128266; STOP TEST' : '&#128266; TEST') + '</button>'
          + '<button id="alarm-mute-btn" class="alarm-btn'
          + (s.muted ? ' muted' : '') + '" type="button"'
          + ' title="Silence the alarm on this screen. Telegram paging is '
          + 'not affected.">'
          + (s.muted ? '&#128263; MUTED' : '&#128263; MUTE') + '</button>'
          + '<select id="alarm-style-select" class="alarm-style" '
          + 'title="Alarm tone style">'
          + options + '</select>'
          + '<span id="alarm-status" class="alarm-status">'
          + (s.muted ? 'Muted'
                     : (s.running ? style.label.split(' (')[0] : 'Click to enable sound'))
          + '</span>';

        el('alarm-test-btn').addEventListener('click', toggleTest);
        el('alarm-mute-btn').addEventListener('click', toggleMute);
        el('alarm-style-select').addEventListener('change', function (e) {
            alarmAudio.setStyle(e.target.value, true);
            // Audition it so the operator hears what they selected.
            alarmAudio.arm();
            alarmAudio.play(1);
        });
    }

    function setStatus(text, cls) {
        const s = el('alarm-status');
        if (!s) return;
        s.textContent = text;
        s.className = 'alarm-status ' + (cls || '');
    }

    // ---- The test is a toggle: press to start, press again to stop.
    function toggleTest() {
        if (testRunning) {
            stopTest();
            return;
        }
        alarmAudio.arm();
        const s = alarmAudio.state();
        if (!s.supported) {
            setStatus('No audio support - visual only', 'bad');
            startFlash();
            return;
        }
        if (s.muted) {
            setStatus('Muted - unmute to hear the test', 'bad');
            return;
        }
        alarmAudio.play(1);
        testTimer = setInterval(() => alarmAudio.play(1), 1500);
        testRunning = true;
        startFlash();
        render();
        setStatus('Playing - press STOP to stop', 'ok');
    }

    function stopTest() {
        testRunning = false;
        if (testTimer) { clearInterval(testTimer); testTimer = null; }
        stopFlash();
        render();
        setStatus('Test stopped', '');
    }

    // The visual alarm, so the test is verifiable on a screen with no
    // speakers.
    function startFlash() {
        const stage = el('stage');
        if (!stage) return;
        stage.classList.add('beep-test-flash');
        flashTimer = setInterval(() => {
            stage.classList.remove('beep-test-flash');
            void stage.offsetWidth;      // force a reflow so it restarts
            stage.classList.add('beep-test-flash');
        }, 1200);
    }

    function stopFlash() {
        if (flashTimer) { clearInterval(flashTimer); flashTimer = null; }
        const stage = el('stage');
        if (stage) stage.classList.remove('beep-test-flash');
    }

    function toggleMute() {
        const s = alarmAudio.toggleMute();
        render();
        setStatus(s.muted
            ? 'Muted on this screen - Telegram paging still active'
            : 'Alarm unmuted', s.muted ? 'bad' : 'ok');
    }

    function mount(selector) {
        container = typeof selector === 'string' ? el(selector) : selector;
        if (!container) return;
        alarmAudio.restore();
        // The site default from the config wins over a stored audition
        // only when nothing was stored; a stored choice is the operator's
        // deliberate override.
        if (window.SITE_BUZZ_STYLE && BUZZ_STYLES[window.SITE_BUZZ_STYLE]) {
            let stored = null;
            try { stored = localStorage.getItem('watchdog-buzzer-style'); } catch (e) { /* private mode */ }
            if (!stored) alarmAudio.setStyle(window.SITE_BUZZ_STYLE, false);
        }
        alarmAudio.onChange(function () { render(); });
        render();
        // Any click arms the audio context, since the browser demands a
        // user gesture before sound is allowed.
        document.addEventListener('click', function once() {
            alarmAudio.arm();
            document.removeEventListener('click', once);
        });
    }

    return {
        mount: mount,
        render: render,
        stopTest: stopTest,
        isTesting: function () { return testRunning; },
    };
})();
