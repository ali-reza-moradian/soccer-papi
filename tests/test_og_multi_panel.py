"""Panel render test (node): the OG tab's multi-sport machinery renders a per-sport og_current feed
identically to soccer, plus the tier / settlement / BOOK-RULES-UNVERIFIED chips and the toa
capabilities footer. Skipped when node is unavailable."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

PANEL = os.path.join(os.path.dirname(__file__), "..", "data", "genz", "papi_panel.html")

# DOM/runtime stubs so the panel's inline JS (definitions + event wiring) loads headless; then we seed
# a synthetic MLB feed and drive the OG render functions directly.
_HARNESS = r"""
const __els = {};
function __el(){ return { innerHTML:'', textContent:'', value:'', className:'', onclick:null,
  oninput:null, onchange:null, style:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}},
  querySelectorAll(){return [];}, querySelector(){return null;}, getAttribute(){return null;},
  setAttribute(){}, appendChild(){}, addEventListener(){} }; }
function DOC(id){ return __els[id] || (__els[id] = __el()); }
var document = { getElementById: DOC, querySelectorAll(){return [];}, querySelector(){return null;},
  addEventListener(){}, createElement(){return __el();}, body: __el() };
var __P = { then(){ return __P; }, catch(){ return __P; } };
function fetch(){ return __P; }
function setInterval(){} function setTimeout(){}
var history = { replaceState(){} }; var location = { hash:'' }; var window = {};
__PANEL_JS__
// ---- drive the OG render with a synthetic MLB feed ----
OGCS.mlb = {
  cycle_utc: new Date(Date.now()).toISOString(), sport:'mlb', scan_interval_s:300,
  toa_capabilities:{ sport_keys:['baseball_mlb'], markets_served:['h2h','totals'], credits:3, daily_total:3 },
  arbs:[
    { match:'Los Angeles Dodgers vs New York Yankees', market:'Total Runs 8.5', tier:'A', family:'total_runs',
      settlement:['RAIN RULE'], rules_unverified:false, roi_pct:2.0, arb_sum_S:0.98, net_roi_pct:1.5,
      fee_pct:1.75, actionable:true, shadow_books:[], fee_trap:false, below_floor:false,
      total_stake:30000, t_max_honest:30000, profit:1500,
      legs:[ {outcome:'Over 8.5', book:'kalshi', top_odds:2.13, avg_fill_odds:2.10, stake:15000, payout:30000},
             {outcome:'Under 8.5', book:'pinnacle', top_odds:1.95, avg_fill_odds:1.95, stake:15000, payout:30000} ] },
    { match:'Los Angeles Dodgers vs New York Yankees', market:'Run Line 1.5', tier:'B', family:'run_line',
      settlement:[], rules_unverified:true, roi_pct:1.0, arb_sum_S:0.99, net_roi_pct:1.0, fee_pct:0.0,
      actionable:true, shadow_books:[], fee_trap:false, below_floor:false,
      total_stake:5000, t_max_honest:5000, profit:50,
      legs:[ {outcome:'NYY -1.5', book:'1xbet', top_odds:2.10, avg_fill_odds:2.10, stake:2500, payout:5000},
             {outcome:'LAD +1.5', book:'pinnacle', top_odds:2.05, avg_fill_odds:2.05, stake:2500, payout:5000} ] }
  ]
};
tab = 'og'; ogSrc = 'current'; ogView = 'funded'; ogSport = 'mlb';
ogTable();
ogSportChips();
console.log('BODY<<' + DOC('ogbody').innerHTML + '>>');
console.log('CAP<<' + DOC('ogcap').textContent + '>>');
console.log('CHIPS<<' + DOC('ogsportchips').innerHTML + '>>');
"""


def _panel_js() -> str:
    with open(PANEL, encoding="utf-8") as fh:
        html = fh.read()
    js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    # Keep the definitions + event wiring; drop the load-time bootstrap (readHash/applyState/poll/render).
    cut = js.index("readHash();applyState();")
    return js[:cut]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_og_panel_renders_per_sport_with_tier_and_settlement_chips(tmp_path):
    harness = _HARNESS.replace("__PANEL_JS__", _panel_js())
    script = tmp_path / "panel_harness.js"
    script.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    body = re.search(r"BODY<<(.*?)>>", out.stdout, re.S).group(1)
    cap = re.search(r"CAP<<(.*?)>>", out.stdout, re.S).group(1)
    chips = re.search(r"CHIPS<<(.*?)>>", out.stdout, re.S).group(1)

    # Rows render with the walked-fill legs (soccer path reused) + the additive tier/settlement chips.
    assert "Total Runs 8.5" in body and "Run Line 1.5" in body
    assert "RAIN RULE" in body                                  # Tier A settlement chip inherited
    assert "BOOK RULES UNVERIFIED" in body                      # Tier B rules_unverified chip
    assert ">A</span>" in body and ">B</span>" in body          # tier chips
    assert "(fills 2.10)" in body                               # walked avg-fill on the kalshi leg
    # Per-sport capabilities footer.
    assert "the-odds-api (MLB) serves: h2h, totals" in cap
    assert "confirm on both books before betting" in cap
    # The MLB OG chip goes live (active) and shows its arb count.
    assert 'data-ogsp="mlb"' in chips and "schip active" in chips
