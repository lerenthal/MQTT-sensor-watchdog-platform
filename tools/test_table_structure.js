#!/usr/bin/env node
// Verify the devices table's structure: the header columns and the cells
// the row renders must match EXACTLY.
//
// A mismatch is not cosmetic. The detail row spans the full table width
// with a colspan, and a colspan wider than the real column count makes
// the browser invent a phantom column - which is what showed up as a
// stray narrow strip at the right edge of every expanded row.
//
//   node tools/test_table_structure.js

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const dashboard = fs.readFileSync(path.join(ROOT, 'dashboard.js'), 'utf8');
const app = fs.readFileSync(path.join(ROOT, 'app.py'), 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`  PASS  ${name}`);
  } else {
    failures++;
    console.log(`  FAIL  ${name}${detail ? '  -> ' + detail : ''}`);
  }
}

console.log('=== devices table structure ===');
console.log();

// --- The header. It lives in a module-level constant
//     _DEVICES_TABLE_HEADER so the column count can be derived from it.
const QUOTE = '"'.repeat(3);
const constRe = new RegExp('_DEVICES_TABLE_HEADER = ' + QUOTE + '([\\s\\S]*?)' + QUOTE);
const constMatch = app.match(constRe);
if (!constMatch) {
  console.log('  FAIL  could not find _DEVICES_TABLE_HEADER in app.py');
  process.exit(1);
}
const headerHtml = constMatch[1];
const headerCols = [...headerHtml.matchAll(/<th[^>]*class="(col-[a-z0-9 ]+)"/g)]
  .map(m => m[1].split(' ')[0]);

// --- The row, from the JS. The class attribute may carry extra modifier
//     classes (num, ts), so take the first token.
const rowMatch = dashboard.match(/const main = '<tr class="' \+ rowClass[\s\S]*?return main \+ detail;/);
if (!rowMatch) {
  console.log('  FAIL  could not find renderRow in dashboard.js');
  process.exit(1);
}
const rowCols = [...rowMatch[0].matchAll(/<td class="(col-[a-z0-9 ]+)"/g)]
  .map(m => m[1].split(' ')[0]);

console.log(`  header: ${headerCols.length} columns`);
console.log(`  row   : ${rowCols.length} cells`);
console.log();

check('the table header comes from the shared constant',
  /<thead><tr>\s*\{_DEVICES_TABLE_HEADER\}\s*<\/tr><\/thead>/.test(app));
check('the header and the row have the same number of columns',
  headerCols.length === rowCols.length,
  `header ${headerCols.length} vs row ${rowCols.length}`);
check('the header and the row have the same columns in the same order',
  JSON.stringify(headerCols) === JSON.stringify(rowCols),
  `header ${headerCols.join(',')} vs row ${rowCols.join(',')}`);

// --- The heartbeat column the operator asked for.
check('the Heartbeat column is in the header',
  headerCols.includes('col-heartbeat'));
check('the Heartbeat column is in the row',
  rowCols.includes('col-heartbeat'));
check('the Heartbeat column is sortable',
  /<th[^>]*class="col-heartbeat"[^>]*data-sort="heartbeat"/.test(headerHtml));
check('the row carries the heartbeat value as a data attribute',
  /data-heartbeat="'\s*\+\s*\(d\.last_heartbeat_value/.test(dashboard));

// --- The colspan must be derived, never hardcoded. A hardcoded value is
//     what produced the phantom column.
check('no hardcoded colspan remains in dashboard.js',
  !/colspan="\d+"/.test(dashboard),
  (dashboard.match(/colspan="\d+"/g) || []).join(' '));
check('the detail row derives its colspan from the header',
  /tableColumnCount\(\)/.test(dashboard));
check('tableColumnCount reads the real header',
  /querySelector\('table\.devices thead tr th'\)/.test(dashboard));

// --- The server-rendered placeholder must span the same count.
check('the server placeholder row derives its colspan',
  /colspan="\{_devices_column_count\(\)\}" class="muted">Loading/.test(app));
check('the server derives the column count from the header constant',
  /_devices_column_count[\s\S]{0,400}_DEVICES_TABLE_HEADER/.test(app));

console.log();
console.log(failures === 0 ? '  all checks passed' : `  ${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
