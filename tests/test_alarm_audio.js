// Self-check for the alarm audio state machine in alarm/static/js/alarm.js.
// Extracts the real _getAudioContext..resetOutageAlarm method block straight
// out of the shipped file (so this fails if the guard logic or the pure-MP3
// policy ever regresses) and exercises it against a mock <audio> element
// with controllable play() timing.
//
// Policy under test: the configured MP3 is the ONLY sound this app ever
// plays. No synth/oscillator/TTS/default-sound fallback exists. If the MP3
// can't play (autoplay block, decode/network failure, or a synchronous
// throw), nothing else plays — only a console error + toast are reported.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const REAL_PATH = path.join(__dirname, '..', 'alarm', 'static', 'js', 'alarm.js');
const text = fs.readFileSync(REAL_PATH, 'utf8');

const start = text.indexOf('  _getAudioContext() {');
const end = text.indexOf('\n  resetOutageAlarm() {', start);
const endClose = text.indexOf('\n  }', end) + 4;
assert(start > 0 && end > start && endClose > end, 'markers not found — file structure changed');
const block = text.slice(start, endClose);

// Sanity: the race guards we rely on must still be present in the extracted block.
assert(block.includes('token !== this._alarmPlayToken'), 'token guard missing from source');
assert(block.includes('!this.isPlayingAlarm || this.isMuted'), 'isPlayingAlarm/isMuted guard missing from source');
assert(block.includes('_reportAlarmAudioFailure'), 'failure-reporting path missing from source');

// Pure-MP3 policy: none of this should exist anywhere in the shipped file —
// not just the extracted block — so a fallback sound added elsewhere in the
// class can't slip past this check either.
const forbidden = ['_startSynthBeep', '_stopSynthBeep', 'synthOsc', 'createOscillator', 'speechSynthesis', 'new Audio('];
for (const term of forbidden) {
  assert(!text.includes(term), `forbidden fallback-sound code reappeared: "${term}"`);
}
// Exactly one thing this app ever calls .play() on.
const playCallSites = (text.match(/\.play\(\)/g) || []).length;
assert.strictEqual(playCallSites, 1, `expected exactly one .play() call site, found ${playCallSites}`);

const src = `(class TestMonitor {\n${block}\n})`;
const TestMonitor = eval(src);

// toggleSound() touches a couple of icon elements purely for display; stub
// document so that read-only DOM styling doesn't need a real browser here.
global.document = { getElementById() { return null; } };
global.localStorage = { setItem() {}, getItem() { return null; } };
// No AudioContext at all in this sandbox — if any code path ever tried to
// synthesize a tone (createOscillator etc.) it would throw here, since
// nothing provides that API.
global.window = { AudioContext: undefined, webkitAudioContext: undefined };

function mockAudio() {
  return {
    loop: false, muted: true, volume: 0, currentTime: 0, paused: true,
    _resolvers: [],
    play() {
      this.paused = false;
      return new Promise((resolve, reject) => { this._resolvers.push({ resolve, reject }); });
    },
    pause() {
      this.paused = true;
      // Real HTMLMediaElement rejects any in-flight play() with AbortError
      // when pause() interrupts it.
      const pending = this._resolvers.splice(0);
      pending.forEach(r => r.reject(new Error('AbortError')));
    },
    settleLastPlay(ok) {
      const r = this._resolvers.pop();
      if (!r) return;
      ok ? r.resolve() : r.reject(new Error('NotSupportedError'));
    },
  };
}

function newMonitor() {
  const m = new TestMonitor();
  m.isMuted = false;
  m.alarmAudio = mockAudio();
  m.audioCtx = null;
  m._failureReports = 0;
  m.instancesPage = {
    _triggerEventToast(msg) { m._lastToast = msg; },
  };
  return m;
}

async function flush() { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); }

// Every playAlarm() schedules a real 60s auto-stop timer we don't care about
// in most sub-tests — stub it out so the process doesn't hang around.
global.setTimeout = () => 1;
global.clearTimeout = () => {};

(async () => {
  // 1. Normal play: MP3 succeeds, nothing else happens.
  {
    const m = newMonitor();
    m.playAlarm();
    m.alarmAudio.settleLastPlay(true);
    await flush();
    assert.strictEqual(m._lastToast, undefined, 'no failure toast when MP3 plays fine');
    assert.strictEqual(m.alarmAudio.paused, false);
  }

  // 2. Repeated playAlarm() calls for the same outage must not double-play.
  {
    const m = newMonitor();
    m.playAlarm();
    m.playAlarm();
    m.playAlarm();
    assert.strictEqual(m.alarmAudio._resolvers.length, 1, 'only one play() attempt for one outage');
  }

  // 3. Rapid acknowledge before play() settles: must not report/act after stop.
  {
    const m = newMonitor();
    m.playAlarm();
    m.stopAlarm(); // ack path: pause() while play() is still pending -> rejects
    await flush();
    assert.strictEqual(m._lastToast, undefined, 'no failure toast for an alarm already stopped (ack race)');
    assert.strictEqual(m.alarmAudio.paused, true, 'audio must stay paused after ack');
  }

  // 4. Mute while play() pending: must not report/act after mute.
  {
    const m = newMonitor();
    m.playAlarm();
    m.toggleSound(false); // mute -> stopAlarm() -> pause() -> rejects pending play()
    await flush();
    assert.strictEqual(m._lastToast, undefined, 'no failure toast after mute');
  }

  // 5. Genuine MP3 failure while still the current attempt: reported, and
  //    absolutely nothing else plays — no sound is the only allowed outcome.
  {
    const m = newMonitor();
    m.playAlarm();
    m.alarmAudio.settleLastPlay(false); // decode/network failure, nothing else interfered
    await flush();
    assert.ok(m._lastToast && /no sound/i.test(m._lastToast), 'operator must be told MP3 failed and nothing else plays');
    assert.strictEqual(typeof m._startSynthBeep, 'undefined', 'no beep method exists at all');
  }

  // 6. Stale-attempt race: stop -> play -> stop -> play, first attempt's
  //    rejection must not report/act over the second (current) attempt.
  {
    const m = newMonitor();
    m.playAlarm();                // attempt #1, play() pending
    m.resetOutageAlarm();         // pause() -> rejects attempt #1's promise (async)
    m.playAlarm();                // attempt #2 starts fresh, new pending play()
    await flush();                // let attempt #1's rejection catch-handler run
    assert.strictEqual(m._lastToast, undefined, 'stale attempt #1 rejection must not report over attempt #2');
    m.alarmAudio.settleLastPlay(true); // attempt #2 succeeds normally
    await flush();
    assert.strictEqual(m._lastToast, undefined, 'attempt #2 played the real MP3 cleanly, still nothing reported');
  }

  // 7. Stale-attempt race where the OLD attempt's failure must not survive a
  //    NEWER attempt that also fails — exactly one report, not duplicated.
  {
    const m = newMonitor();
    m.playAlarm();
    m.resetOutageAlarm();
    m.playAlarm();
    await flush(); // attempt #1 rejection settles, guarded, no report
    m.alarmAudio.settleLastPlay(false); // attempt #2 genuinely fails
    await flush();
    assert.ok(m._lastToast && /no sound/i.test(m._lastToast), 'exactly the one attempt that actually failed gets reported');
  }

  // 8. play() throws synchronously (older engines do this instead of
  //    rejecting) — must not escape playAlarm() uncaught, must be reported,
  //    and must not play anything else.
  {
    const m = newMonitor();
    m.alarmAudio.play = () => { throw new Error('InvalidStateError'); };
    assert.doesNotThrow(() => m.playAlarm(), 'a synchronous play() throw must not escape playAlarm()');
    assert.ok(m._lastToast && /no sound/i.test(m._lastToast), 'synchronous throw must still be reported, no sound played');
  }

  // 9. Stale resolved promise: if this attempt is no longer the current one
  //    by the time its play() promise fulfills (token bumped from under
  //    it), the success branch must not log a misleading "playing cleanly".
  {
    const m = newMonitor();
    const realLog = console.log;
    const logs = [];
    console.log = (msg) => logs.push(msg);
    try {
      m.playAlarm();                     // attempt #1, play() pending, token captured internally
      m._alarmPlayToken++;               // simulate a newer attempt having since superseded it
      m.alarmAudio.settleLastPlay(true); // attempt #1's own promise fulfills late
      await flush();
      assert.ok(!logs.some(l => /playing cleanly/.test(l)), 'stale attempt must not log success');
    } finally {
      console.log = realLog;
    }
  }

  // 10. No <audio> element at all: still no alternative sound, just reported.
  {
    const m = newMonitor();
    m.alarmAudio = null;
    m.playAlarm();
    assert.ok(m._lastToast && /no sound/i.test(m._lastToast), 'missing element must be reported, not silently substituted');
  }

  console.log('ALL ALARM AUDIO CHECKS PASSED (pure-MP3, no fallback sound)');
})().catch(e => { console.error('FAILED:', e); process.exit(1); });
