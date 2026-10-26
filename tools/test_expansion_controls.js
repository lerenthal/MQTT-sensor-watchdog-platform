#!/usr/bin/env node
// Tests for the dashboard's expansion controls and the compact row.
//
// Three real bugs are guarded here:
//
//  1. The "Expand sensors" / "Collapse all" buttons were dead. They
//     operated on an expansion Set that no rendering path read, because
//     the function that used it (sensorCell) had been superseded and
//     nothing called it any more. A button that changes state nothing
//     renders looks exactly like a button that does not work.
//
//  2. The Print Report link was written as {{{{ url_for(...) }}}} inside
//     an f-string, so it rendered as the literal text
//     "{{ url_for('print_report') }}" and pointed at nothing.
//
//  3. The Sensors cell must be ONE line by default, with the full
//     per-channel list behind a toggle - not every channel inline, which
//     is what made the table wide and forced a sideways scroll.
//
//   node tools/test_expansion_controls.js

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const js = fs.readFileSync(path.join(ROOT, 'dashboard.js'), 'utf8');
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

console.log('=== expansion controls and the compact row ===');
console.log();

// --- 1. Every expansion Set must be read by something that renders.
const sets = [
  { name: 'EXPANDED',     key: 'watchdog-expanded' },
  { name: 'EXPANDED_ROWS', key: 'watchdog-expanded-rows' },
  { name: 'REASON_OPEN',  key: 'watchdog-reason-open' },
];
for (const s of sets) {
  const uses = [...js.matchAll(new RegExp(`(?<![A-Z_])${s.name}(?![A-Z_])`, 'g'))];
  const reads = uses.filter(m => {
    const after = js.slice(m.index + s.name.length, m.index + s.name.length + 12);
    return /\.(has|get|size)\b/.test(after);
  });
  check(`${s.name} is READ somewhere (not just written)`,
        reads.length > 0, `${uses.length} uses, ${reads.length} reads`);
  check(`${s.name} is persisted under its own key`,
        js.includes(`'${s.key}'`));
}

// --- 2. The toolbar buttons must touch EVERY expansion set, or "collapse
//        all" leaves something expanded and the button looks broken.
const collapseAll = js.slice(js.indexOf('window.collapseAll'),
                             js.indexOf('window.collapseAll') + 900);
for (const s of sets) {
  check(`collapseAll() clears ${s.name}`,
        new RegExp(`${s.name}\\.clear\\(\\)`).test(collapseAll),
        collapseAll.slice(0, 160).replace(/\n/g, ' '));
}

// --- 3. The buttons must exist in the toolbar, and each must call a
//        function that is defined.
const buttons = [...app.matchAll(/onclick="(\w+)\(\)"/g)].map(m => m[1]);
check('the toolbar has expand, collapse and print controls',
      buttons.includes('expandAll') && buttons.includes('collapseAll'),
      buttons.join(', '));
for (const b of buttons) {
  check(`${b}() is defined in dashboard.js`,
        js.includes(`window.${b} = function`) || js.includes(`function ${b}(`));
}

// --- 4. The Print Report link must be a real URL, not a literal
//        {{ url_for(...) }} left behind by f-string brace escaping.
const printLink = app.match(/href="([^"]*)"[^>]*>\s*🖨 Print Report/);
check('the Print Report link exists in the template', !!printLink);
if (printLink) {
  // A single-brace {url_for(...)} is the correct form inside an
  // f-string. A DOUBLE-brace {{ url_for(...) }} would be the escaped,
  // never-evaluated form - the bug.
  check('the Print Report link is not an unrendered Jinja expression',
        !printLink[1].includes('{{'), printLink[1]);
  check('the Print Report link uses a single-brace f-string expression',
        printLink[1].includes("url_for('print_report')"),
        printLink[1]);
}

// --- 5. The Sensors cell must be one line with a panel behind it.
const ssStart = js.indexOf('function sensorSummary');
const ssEnd = js.indexOf('\nfunction ', ssStart + 10);
const ss = js.slice(ssStart, ssEnd > 0 ? ssEnd : ssStart + 6000);
check('sensorSummary renders a collapsed line', /svline/.test(ss));
check('sensorSummary renders an expandable panel', /svpanel/.test(ss));
check('the panel is hidden by default',
      /isOpen \? 'block' : 'none'/.test(ss));
check('sensorSummary has a toggle button', /toggleSensors/.test(ss));
check('the collapsed line shows a channel count when a metric has several',
      /svcount/.test(ss));
check('the full channel list is only in the panel, not the line',
      /detail/.test(ss));

// The dead function must be gone - if it comes back, the buttons will
// silently target a Set that nothing renders.
check('the superseded sensorCell() is not left in the file',
      !js.includes('function sensorCell('));

// --- 6. The table must fit without a horizontal scroll at desktop width.
const widthBlock = app.slice(app.indexOf('.col-device    {'),
                             app.indexOf('.col-device    {') + 600);
const widths = [...widthBlock.matchAll(/\.col-(\w+)\s*\{\s*width:\s*([\d.]+)%/g)];
const total = widths.reduce((a, m) => a + parseFloat(m[2]), 0);
check('the column widths sum to 100% or less',
      total <= 100.5, `total ${total}%`);
check('the sensors column has room for the summary line',
      widths.some(m => m[1] === 'sensors' && parseFloat(m[2]) >= 12),
      widthBlock.replace(/\n/g, ' ').slice(0, 120));

console.log();
console.log(failures === 0 ? '  all checks passed' : `  ${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
