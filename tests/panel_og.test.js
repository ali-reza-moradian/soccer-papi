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
const loc = { hash: '' };
const sandbox = {
  document: { getElementById: () => stubEl, querySelectorAll: () => [] },
  location: loc,
  history: { replaceState: (a, b, url) => { loc.hash = url; } },
  setInterval: () => 0,
  fetch: () => ({ then: () => ({ then: () => ({ catch: noop }) }) }),
  encodeURIComponent, decodeURIComponent,
  console, Date, JSON, Math, RegExp, String, Number, Array, parseFloat, parseInt, isNaN,
};
vm.createContext(sandbox);
vm.runInContext(js, sandbox);

const { ogShadowBooks, ogBetLine, schemaStale } = sandbox;
const strip = s => String(s).replace(/<[^>]+>/g, '');

// --- UI state round-trips through the URL hash (refresh restores tab + every sub-segment) ---
Object.assign(sandbox, { tab: 'og', ogSrc: 'current', ogView: 'funded', view: 'arbs', curGame: '26JUL15FRAESP', mFilter: 'corners', sport: 'mlb' });
sandbox.writeHash();
assert.ok(/tab=og/.test(loc.hash) && /og\.view=current/.test(loc.hash) && /og\.scope=funded/.test(loc.hash)
  && /genz\.filter=arbs/.test(loc.hash) && /genz\.game=26JUL15FRAESP/.test(loc.hash) && /genz\.mfilter=corners/.test(loc.hash)
  && /genz\.sport=mlb/.test(loc.hash),
  'writeHash emits all state incl. sport: ' + loc.hash);
Object.assign(sandbox, { tab: 'genz', ogSrc: 'history', ogView: 'all', view: 'all', curGame: null, mFilter: '', sport: 'soccer' });
loc.hash = '#tab=og&og.view=current&og.scope=funded&genz.filter=arbs&genz.game=G1&genz.mfilter=totals&genz.sport=mlb';
sandbox.readHash();
assert.strictEqual(sandbox.tab, 'og'); assert.strictEqual(sandbox.ogSrc, 'current');
assert.strictEqual(sandbox.ogView, 'funded'); assert.strictEqual(sandbox.view, 'arbs');
assert.strictEqual(sandbox.curGame, 'G1'); assert.strictEqual(sandbox.mFilter, 'totals');
assert.strictEqual(sandbox.sport, 'mlb', 'readHash restores genz.sport');
// a bad/empty hash leaves state untouched
loc.hash = ''; Object.assign(sandbox, { tab: 'og' }); sandbox.readHash();
assert.strictEqual(sandbox.tab, 'og', 'empty hash -> no change');
const legsJson = legs => JSON.stringify(legs);

// --- schema (OLD-CODE) banner: fires when data exists but its schema is missing or < expected ---
assert.strictEqual(schemaStale(null, null, 2), false, 'no data -> no banner');
assert.strictEqual(schemaStale({ schema: 2, games: {} }, null, 2), false, 'current schema -> no banner');
assert.strictEqual(schemaStale({ schema: 1, games: {} }, null, 2), true, 'old snapshot schema -> banner');
assert.strictEqual(schemaStale({ cycle_utc: 'x', games: {} }, null, 2), true, 'missing schema -> banner');
assert.strictEqual(schemaStale(null, { schema: 2 }, 2), false, 'heartbeat current -> no banner');
assert.strictEqual(schemaStale(null, { schema: 1 }, 2), true, 'heartbeat old schema -> banner');
assert.strictEqual(sandbox.EXPECTED_SCHEMA, 4, 'panel EXPECTED_SCHEMA matches engine SNAPSHOT_SCHEMA_VERSION');
assert.strictEqual(schemaStale({ schema: 3, games: {} }, null, 4), true, 'schema 3 is now OLD (expected 4)');
assert.strictEqual(schemaStale({ schema: 4, games: {} }, null, 4), false, 'schema 4 -> current');

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

// ================= GENZ ALL-GAMES overview (default view) =================
// (1) default-to-ALL: an absent or stale game resolves to ALL; a valid id is kept; ALL round-trips.
assert.strictEqual(sandbox.resolveGame(null, { G1: {} }), 'ALL', 'absent game -> ALL');
assert.strictEqual(sandbox.resolveGame('GONE', { G1: {} }), 'ALL', 'stale game (not in snapshot) -> ALL');
assert.strictEqual(sandbox.resolveGame('G1', { G1: {} }), 'G1', 'valid game kept');
assert.strictEqual(sandbox.resolveGame('ALL', { G1: {} }), 'ALL', 'ALL kept');
assert.strictEqual(sandbox.resolveGame('G1', {}), 'ALL', 'empty snapshot -> ALL even for a named id');
// hash without genz.game leaves curGame untouched (tabs() resolves the default to ALL at render time)
loc.hash = '#tab=genz&genz.sport=mlb'; sandbox.curGame = 'ZZZ'; sandbox.readHash();
assert.strictEqual(sandbox.curGame, 'ZZZ', 'readHash: no genz.game -> curGame untouched');
// ALL round-trips through the hash
sandbox.curGame = 'ALL'; sandbox.writeHash();
assert.ok(/genz\.game=ALL/.test(loc.hash), 'writeHash emits genz.game=ALL: ' + loc.hash);
loc.hash = '#tab=genz&genz.game=ALL'; sandbox.curGame = 'x'; sandbox.readHash();
assert.strictEqual(sandbox.curGame, 'ALL', 'readHash restores genz.game=ALL');

// (2) global sort across EVERY game: Net % desc (nulls last), tie-break SUM ascending.
const slate = { games: {
  G1: { teams: 'Mets vs Phillies', kickoff_utc: '2026-07-16T23:10:00Z', markets: [
    { market_type: 'ml2', implied_cost: 1.001, net_roi_pct: null },
    { market_type: 'total_runs', line: 8.5, implied_cost: 0.95, net_roi_pct: 2.5 } ] },
  G2: { teams: 'Rays vs Yankees', kickoff_utc: '2026-07-16T20:05:00Z', started: true, markets: [
    { market_type: 'ml2', implied_cost: 0.99, net_roi_pct: 1.0 },
    { market_type: 'total_runs', line: 9.5, implied_cost: 0.90, net_roi_pct: null } ] } } };
const rows = sandbox.collectAllRows(slate, 'all', '');
const nets = []; for (let i = 0; i < rows.length; i++) nets.push(rows[i].m.net_roi_pct);
assert.deepStrictEqual(nets, [2.5, 1.0, null, null], 'Net % desc, nulls last: ' + nets);
assert.strictEqual(rows[2].m.implied_cost, 0.90, 'null tie-break by SUM asc -> 0.90 first');
assert.strictEqual(rows[3].m.implied_cost, 1.001, 'null tie-break by SUM asc -> 1.001 last');
// every game (incl. a started one) contributes rows; each carries game id/teams/kickoff for GAME+TIME
assert.strictEqual(rows.length, 4, 'ALL view = every market from every game (started included)');
assert.ok(rows[0].id && rows[0].teams && rows[0].kickoff, 'ALL row carries id/teams/kickoff');
// the All-markets/Arbs-only toggle + market-type filter apply across the whole slate
assert.strictEqual(sandbox.collectAllRows(slate, 'arbs', '').length, 3, 'arbs-only (<1) across the slate = 3');
const mlOnly = sandbox.collectAllRows(slate, 'all', 'ml2');
assert.ok(mlOnly.length === 2 && mlOnly.every(function (r) { return r.m.market_type === 'ml2'; }), 'mfilter across slate');
assert.strictEqual(sandbox.collectAllRows(null, 'all', '').length, 0, 'no snapshot -> no rows');

// (3) clicking a GAME cell switches to that game's chip view (sets curGame + writes the hash).
Object.assign(sandbox, { SNAP: slate, tab: 'genz', view: 'all', mFilter: '', curGame: 'ALL' });
sandbox.SNAPS.mlb = slate;
sandbox.selectGame('G2');
assert.strictEqual(sandbox.curGame, 'G2', 'GAME-cell click sets curGame to that game');
assert.ok(/genz\.game=G2/.test(loc.hash), 'GAME-cell click writes genz.game=G2: ' + loc.hash);

// ================= MAKER RT v3 strip (schema 2: by_sport / by_phase / achievable) =================
const mstrip = strip(sandbox.makerStripHtml({
  mode: 'shadow', sockets: { poly_market: true, kalshi: true, poly_user: false },
  quotes: 12, fills: 3, fill_rate: 0.25, at_best_share: 0.5, median_net_at_fill: 1.2,
  drift_median_1: 0.1, drift_median_5: 0.2, drift_median_30: -0.3, behind_best: 4,
  by_phase: { pre: { quotes: 10, fills: 3, fill_rate: 0.3, behind_best: 2 },
              inplay: { quotes: 2, fills: 0, fill_rate: 0, behind_best: 2 } },
  by_sport: {
    mlb: { quotes: 12, fills: 3, fill_rate: 0.25, at_best_share: 0.5, median_net_at_fill: 1.2,
           achievable: { n: 2820, p50: -0.008, share_ge_25bp: 0.03, share_ge_100bp: 0.001 } },
    tennis: { quotes: 0, fills: 0, fill_rate: 0, at_best_share: 0, median_net_at_fill: null,
              achievable: { n: 0 } } } }));
assert.ok(/MAKER RT/.test(mstrip) && /shadow/i.test(mstrip), 'strip shows label + mode: ' + mstrip);
assert.ok(/quotes 12/.test(mstrip) && /fills 3/.test(mstrip) && /fill-rate 25%/.test(mstrip), 'metrics: ' + mstrip);
assert.ok(/median net 1\.20%/.test(mstrip) && /at-best 50%/.test(mstrip), 'net/at-best: ' + mstrip);
assert.ok(/behind 4/.test(mstrip), 'behind-best count: ' + mstrip);
// PRE | LIVE phase split
assert.ok(/PRE q 10/.test(mstrip) && /LIVE q 2/.test(mstrip), 'PRE|LIVE split: ' + mstrip);
// per-sport rows + achievable ladder ("achv p50 -0.8% ... 0.25%: 3%")
assert.ok(/MLB q 12/.test(mstrip), 'per-sport MLB row: ' + mstrip);
assert.ok(/achv p50 -0\.8%/.test(mstrip) && /0\.25%: 3%/.test(mstrip), 'achievable ladder line: ' + mstrip);
// a zero-quote sport is greyed (class), not omitted
assert.ok(/mrtsub grey/.test(sandbox.makerStripHtml({ mode: 'shadow', sockets: {},
  by_sport: { tennis: { quotes: 0, achievable: { n: 0 } } } })), 'zero-quote sport greyed');
// LIVE mode badge flips
assert.ok(/live/i.test(sandbox.makerStripHtml({ mode: 'live', sockets: {} })), 'live badge');

// --- in-play (schema 4): a row flagged inplay gets a red LIVE badge; collectAllRows carries phase ---
const inplayRow = { market_type: 'total_goals', line: '2.5', side_a: 'over', venue_a: 'kalshi',
  price_a: 0.45, side_b: 'under', venue_b: 'polymarket', price_b: 0.5, implied_cost: 0.95, roi_pct: 5,
  net_roi_pct: 2, fee_rate_pct: 1, phase: 'inplay', inplay: true, exec_status: 'inplay_collect' };
const rc = sandbox.marketCells(inplayRow);
assert.ok(/livetag[^>]*>LIVE/.test(rc.cells), 'in-play row renders a LIVE badge: ' + rc.cells);
const preRow = Object.assign({}, inplayRow, { phase: 'pre', inplay: false });
assert.ok(!/livetag[^>]*>LIVE/.test(sandbox.marketCells(preRow).cells), 'pre-game row has no LIVE badge');
// collectAllRows carries the game phase for the TIME-column live tag
const ipSlate = { games: { LIVEG: { teams: 'A vs B', kickoff_utc: '2026-07-16T20:00:00Z', started: true,
  phase: 'inplay', markets: [inplayRow] } } };
const ipRows = sandbox.collectAllRows(ipSlate, 'all', '');
assert.strictEqual(ipRows.length, 1, 'in-play market collected');
assert.strictEqual(ipRows[0].phase, 'inplay', 'row carries game phase for the live tag');

console.log('panel_og.test.js: all assertions passed');
