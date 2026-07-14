// Node test for the OG panel's PURE funded/shadow classifier + per-leg BET-line sizing (no DOM).
// The panel is a single HTML file; this extracts its <script>, evaluates it in a stubbed context,
// and asserts the pure functions. Run:  node tests/panel_og.test.js   (exit 0 = pass).
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync(path.join(__dirname, '..', 'data', 'genz', 'papi_panel.html'), 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('no <script> found in papi_panel.html'); process.exit(1); }
// Drop the auto-run tail (needs a live DOM); keep every function + top-level var.
const js = m[1].replace(/poll\(\);\s*setInterval\(poll,\s*15000\);/, '').replace(/renderAll\(\);\s*$/, '');

const noop = () => {};
const stubEl = { textContent: '', innerHTML: '', value: '', querySelectorAll: () => [], classList: { toggle: noop }, onclick: null, oninput: null, onchange: null };
const sandbox = {
  document: { getElementById: () => stubEl, querySelectorAll: () => [] },
  setInterval: () => 0,
  fetch: () => ({ then: () => ({ then: () => ({ catch: noop }) }) }),
  console, Date, JSON, Math, RegExp, String, Number, Array, parseFloat, parseInt, isNaN,
};
vm.createContext(sandbox);
vm.runInContext(js, sandbox);

const { ogShadowBooks, ogBetLine } = sandbox;
const strip = s => String(s).replace(/<[^>]+>/g, '');
const legsJson = legs => JSON.stringify(legs);

// --- funded vs shadow (computed from parsed legs_json books, NOT the actionable flag) ---
const funded = { legs_json: legsJson([
  { outcome: 'Over', book: 'pinnacle', decimal_odds: 2.1, stake: 1500 },
  { outcome: 'Under', book: '1xbet', decimal_odds: 2.05, stake: 1536 }]),
  bookmakers: 'pinnacle, 1xbet', total_stake_max: 3036, roi_decimal: 0.037 };
const shadow = { legs_json: legsJson([
  { outcome: 'England', book: 'polymarket', decimal_odds: 2.78, stake: 1137 },
  { outcome: 'Draw', book: 'williamhill', decimal_odds: 3.16, stake: 1000 },
  { outcome: 'Argentina', book: 'polymarket', decimal_odds: 3.2, stake: 987 }]),
  bookmakers: 'polymarket, williamhill, polymarket', total_stake_max: 3124, roi_decimal: 0.0112 };

const J = x => JSON.stringify(x);   // compare by value (sandbox arrays are a different realm)
assert.strictEqual(J(ogShadowBooks(funded)), '[]', 'all-fundable legs -> no shadow books');
assert.strictEqual(J(ogShadowBooks(shadow)), '["williamhill"]', 'one non-fundable leg -> SHADOW list');

// legs_json unparseable -> fall back to the bookmakers string
const badJson = { legs_json: '{oops', bookmakers: 'kalshi, bcgame' };
assert.strictEqual(J(ogShadowBooks(badJson)), '["bcgame"]', 'fallback to bookmakers string');

// --- per-leg BET line scales to the bankroll box; whole dollars; below-$1 guard ---
sandbox.BANK = 3036;                                    // >= T_max -> scale 1
let line = strip(ogBetLine(funded));
assert.ok(/\$1500 Over @ pinnacle/.test(line) && /\$1536 Under @ 1xbet/.test(line), 'full-size split: ' + line);
assert.ok(/profit \$112 on \$3036/.test(line), 'profit = roi_decimal * T_max at scale 1: ' + line);

sandbox.BANK = 303.6;                                   // scale 0.1
line = strip(ogBetLine(funded));
assert.ok(/\$150 Over @ pinnacle/.test(line) && /\$154 Under @ 1xbet/.test(line), 'scaled split: ' + line);
assert.ok(/profit \$11 on \$304/.test(line), 'scaled profit: ' + line);

sandbox.BANK = 1;                                       // a leg stake*scale < $1
line = strip(ogBetLine(funded));
assert.ok(/below \$1 legs — raise bankroll/.test(line), 'below-$1 guard: ' + line);

console.log('panel_og.test.js: all assertions passed');
