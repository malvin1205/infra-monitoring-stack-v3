// Self-check for the per-endpoint Default Job storage helpers in
// alarm/static/js/alarm.js. Extracts the real _loadJobDefaults /
// getDefaultJob / setDefaultJob block straight out of the shipped file and
// exercises it against a localStorage shim.
//
// Policy under test: Default Job is keyed by Prometheus endpoint URL.
// Switching endpoints must surface the NEW endpoint's own default (or null),
// never the previous one. A legacy bare-string value is invalid JSON and
// must degrade to "no defaults" without throwing.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const REAL_PATH = path.join(__dirname, '..', 'alarm', 'static', 'js', 'alarm.js');
const text = fs.readFileSync(REAL_PATH, 'utf8').replace(/\r\n/g, '\n');

const start = text.indexOf('function _loadJobDefaults() {');
const end = text.indexOf('\n}\n', text.indexOf('function setDefaultJob(')) + 2;
assert(start > 0 && end > start, 'markers not found — helper block structure changed');
const block = text.slice(start, end);
assert(block.includes('JSON.stringify(map)'), 'setDefaultJob no longer serialises a map');
assert(block.includes("job !== 'all'"), "setDefaultJob no longer treats 'all' as clear");

const KEY = 'infrawatch.defaultJob';
let store = {};
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const JOB_DEFAULT_LS_KEY = KEY;
// eval in strict mode keeps declarations local — re-export onto global.
eval(block + '\nglobal.getDefaultJob = getDefaultJob; global.setDefaultJob = setDefaultJob;');

const A = 'http://192.168.9.16:9090';
const B = 'http://192.168.40.133:9090';

// Fresh store: nothing set.
assert.strictEqual(getDefaultJob(A), null);
assert.strictEqual(getDefaultJob(null), null, 'null endpoint must be safe');

// Independent per-endpoint defaults.
setDefaultJob(A, 'blackbox-ping-internal');
setDefaultJob(B, 'node');
assert.strictEqual(getDefaultJob(A), 'blackbox-ping-internal');
assert.strictEqual(getDefaultJob(B), 'node');

// Setting one endpoint must not disturb the other.
setDefaultJob(A, 'cadvisor');
assert.strictEqual(getDefaultJob(A), 'cadvisor');
assert.strictEqual(getDefaultJob(B), 'node');

// Clearing: null or 'all' removes just that endpoint's entry.
setDefaultJob(A, null);
assert.strictEqual(getDefaultJob(A), null);
assert.strictEqual(getDefaultJob(B), 'node');
setDefaultJob(B, 'all');
assert.strictEqual(getDefaultJob(B), null);

// Legacy bare-string value (pre-per-endpoint) must not throw, reads as empty.
store[KEY] = 'blackbox-ping-internal';
assert.strictEqual(getDefaultJob(A), null, 'legacy string must degrade to no default');
setDefaultJob(A, 'node'); // overwrites the junk with a valid map
assert.strictEqual(getDefaultJob(A), 'node');
assert.strictEqual(store[KEY][0], '{', 'store must now hold a JSON object');

console.log('ok — per-endpoint Default Job storage');
