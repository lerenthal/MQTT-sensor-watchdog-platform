#!/usr/bin/env node
// Regression test: a detail row must never be treated as a device.
//
// The expanded detail row is a separate <tr> in the same tbody. Two bugs
// came from forgetting that:
//
//   1. The sort selected every <tr>, so a detail row was sorted
//      independently of its device and got appended to the bottom of the
//      table - the device and its expanded panel then looked like two
//      separate entries.
//   2. The filter counted every <tr>, so "18 of 21" included detail rows,
//      and a detail row could stay visible while its device was hidden.
//
//   node tools/test_row_handling.js

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const js = fs.readFileSync(path.join(ROOT, 'dashboard.js'), 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`  PASS  ${name}`);
  } else {
    failures++;
    console.log(`  FAIL  ${name}${detail ? '  -> ' + detail : ''}`);
  }
}

console.log('=== device row / detail row handling ===');
console.log();

// --- Every row selection must exclude the detail rows.
//
// The selector regex must handle a nested :not(...) - a naive [^)]* stops
// at the first closing parenthesis and would miss the selector entirely,
// which would make this check pass vacuously.
const selectors = [...js.matchAll(/querySelectorAll\(((?:'(?:[^'\\]|\\.)*')|(?:"(?:[^"\\]|\\.)*"))\)/g)]
  .map(m => m[1]);

const rowSelectors = selectors.filter(s => /\btr\b/.test(s));
console.log(`  row selectors found: ${rowSelectors.length}`);
for (const s of rowSelectors) console.log(`     ${s}`);
console.log();

check('row selectors were actually found', rowSelectors.length > 0,
  'the check would be vacuous if none matched');
// A selector is safe if it either excludes .detail-row explicitly, or
// matches on a class that only a device row can carry (a detail row is
// rendered with class="detail-row" and never gains a status class).
const unfiltered = rowSelectors.filter(s =>
  !s.includes('.detail-row')
  && !s.includes(':not(')
  && !s.includes('row-changed')
  && !s.includes('row-compact'));
check('every row selector either excludes detail rows or matches only device rows',
  unfiltered.length === 0,
  unfiltered.join(' | '));

// --- The sort must re-attach each detail row to its device.
check('the sort selects only device rows',
  /const rows = Array\.from\(tbody\.querySelectorAll\('tr:not\(\.detail-row\)'\)\)/.test(js));
check('the sort re-appends each detail row after its device',
  /tr\.detail-row\[data-detail-for="/.test(js)
  && /if \(detail\) tbody\.appendChild\(detail\);/.test(js));

// --- The filter must keep the detail row in step with its device.
check('the filter counts only device rows',
  /const rows = Array\.from\(tbody\.querySelectorAll\('tr:not\(\.detail-row\)'\)\);/.test(js));
check('the filter hides the detail row with its device',
  /if \(detail\) detail\.style\.display = show \? '' : 'none';/.test(js));

// --- The row count must reflect devices, not rows.
check('the row count uses the device row count',
  /rows\.length === 0 \? '' : \(visible \+ ' of ' \+ rows\.length\)/.test(js));

// --- The detail row must carry the link back to its device.
check('the detail row identifies its device',
  /data-detail-for="'\s*\+\s*escapeHtml\(d\.device_id\)/.test(js));
check('the device row identifies its device',
  /data-device="'\s*\+\s*escapeHtml\(d\.device_id\)/.test(js));

console.log();
console.log(failures === 0 ? '  all checks passed' : `  ${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
