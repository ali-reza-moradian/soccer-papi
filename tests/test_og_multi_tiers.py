"""Tier A (book-enriched winner/total families) + Tier B (book-only) assembly, with a synthetic MLB
tree + exchange ladders + the-odds-api event so the arb math, best-of-4 selection, exact-line gate,
settlement-chip inheritance, complementarity, and exchange-only walk are all exercised offline."""
from __future__ import annotations

from datetime import datetime, timezone

from src.arbitrage import exchange_fee_rate
from src.logsetup import get_logger
from src.og_multi import match, tiers
from src.og_multi.tiers import TierCfg

LOG = get_logger("test-ogm-tiers")
NOW = datetime(2026, 7, 19, 23, 0, 0, tzinfo=timezone.utc)
LAST = "2026-07-19T22:55:00Z"                    # 5 min before NOW -> fresh within the 30-min age gate
CFG = TierCfg(pinnacle_limit=5000, age_limit_min=30, poly_fee_rate=0.05,
              bankroll_cap=30000, min_total_stake=20)

# Exchange ask ladders keyed (venue, identifier, side). The away moneyline kalshi leg degrades past
# 200 shares so the walk-to-stake sizing has something to bite on.
LADDERS = {
    ("kalshi", "KML-LAD", "YES"): [(0.50, 200), (0.505, 100000)],   # LAD (away) best exchange -> ~2.0
    ("poly", "PML-LAD", "BUY"): [(0.53, 100000)],
    ("kalshi", "KML-NYY", "YES"): [(0.56, 100000)],                 # NYY (home) exchanges are worse
    ("poly", "PML-NYY", "BUY"): [(0.55, 100000)],
    ("kalshi", "KTOT85", "YES"): [(0.47, 100000)],                  # Over 8.5 best exchange
    ("kalshi", "KTOT85", "NO"): [(0.55, 100000)],
    ("poly", "PTOT85O", "BUY"): [(0.49, 100000)],
    ("poly", "PTOT85U", "BUY"): [(0.54, 100000)],
}


def _o(name, price, point=None):
    d = {"name": name, "price": price}
    if point is not None:
        d["point"] = point
    return d


def _event():
    def bk(key, markets):
        return {"key": key, "last_update": LAST,
                "markets": [{"key": k, "last_update": LAST, "outcomes": outs} for k, outs in markets]}
    pinn = bk("pinnacle", [
        ("h2h", [_o("New York Yankees", 1.95), _o("Los Angeles Dodgers", 1.95)]),
        ("totals", [_o("Over", 1.90, 8.5), _o("Under", 1.95, 8.5), _o("Over", 2.05, 10.5), _o("Under", 1.85, 10.5)]),
        ("spreads", [_o("New York Yankees", 1.90, -1.5), _o("Los Angeles Dodgers", 2.05, 1.5)]),
    ])
    onex = bk("onexbet", [
        ("h2h", [_o("New York Yankees", 2.30), _o("Los Angeles Dodgers", 1.88)]),
        ("totals", [_o("Over", 2.05, 8.5), _o("Under", 1.90, 8.5), _o("Over", 1.90, 10.5), _o("Under", 2.05, 10.5)]),
        ("spreads", [_o("New York Yankees", 2.10, -1.5), _o("Los Angeles Dodgers", 1.95, 1.5)]),
    ])
    return {"home_team": "New York Yankees", "away_team": "Los Angeles Dodgers",
            "commence_time": "2026-07-19T23:20:00Z", "bookmakers": [pinn, onex]}


def _node(mt, mk, side, label, line, kt, ks, pt, **extra):
    d = {"market_type": mt, "market_key": mk, "side": side, "outcome_label": label, "line": line,
         "kind": "2way", "confidence": "high", "kalshi_ticker": kt, "kalshi_side": ks, "poly_token_id": pt}
    d.update(extra)
    return d


def _game():
    return {"home": "New York Yankees", "away": "Los Angeles Dodgers", "date": "2026-07-19",
            "kickoff_utc": "2026-07-19T23:20:00Z", "nodes": [
                _node("ml2", "ml2", "away", "Los Angeles Dodgers", None, "KML-LAD", "YES", "PML-LAD"),
                _node("ml2", "ml2", "home", "New York Yankees", None, "KML-NYY", "YES", "PML-NYY"),
                _node("total_runs", "total_runs|8.5", "over", "Over 8.5", 8.5, "KTOT85", "YES", "PTOT85O",
                      settlement_risk="mlb_rain_rule"),
                _node("total_runs", "total_runs|8.5", "under", "Under 8.5", 8.5, "KTOT85", "NO", "PTOT85U"),
                _node("total_runs", "total_runs|9.5", "over", "Over 9.5", 9.5, "KTOT95", "YES", "PTOT95O"),
                _node("total_runs", "total_runs|9.5", "under", "Under 9.5", 9.5, "KTOT95", "NO", "PTOT95U"),
            ]}


def _rows(event=None, game=None):
    by = {"mlbG": (event or _event(), match.Matched("mlbG", game or _game(), "team_tokens", 1.0))}
    return tiers.build_rows("mlb", by, LADDERS, cfg=CFG, now=NOW, log=LOG)


# --- Tier A ----------------------------------------------------------------- #
def test_tier_a_best_of_four_and_exchange_only_walk():
    rows, _ = _rows()
    ml2 = [r for r in rows if r["family"] == "ml2"]
    assert len(ml2) == 1
    r = ml2[0]
    assert r["tier"] == "A" and r["rules_unverified"] is False and r["arb_sum_S"] < 1 and r["profit"] > 0
    assert {lg["book"] for lg in r["legs"]} == {"kalshi", "1xbet"}    # away->kalshi(2.0), home->1xbet(2.30)
    k = next(lg for lg in r["legs"] if lg["book"] == "kalshi")
    x = next(lg for lg in r["legs"] if lg["book"] == "1xbet")
    assert k["avg_fill_odds"] < k["top_odds"]                         # the exchange leg is WALKED
    assert x["avg_fill_odds"] == x["top_odds"]                        # the flat book leg never degrades


def test_tier_a_fee_math_matches_independent_recompute():
    rows, _ = _rows()
    r = next(x for x in rows if x["family"] == "ml2")
    # net fee = Σ exact per-share exchange taker fee over the chosen legs; only the kalshi leg counts.
    expected = exchange_fee_rate("kalshi", 0.5) + exchange_fee_rate("1xbet", 1 / 2.30)
    assert abs(r["fee_pct"] - expected * 100.0) < 1e-6 and abs(r["fee_pct"] - 1.75) < 0.01


def test_tier_a_settlement_chip_inherits_from_tree_node():
    rows, _ = _rows()
    tr = [r for r in rows if r["market"] == "Total Runs 8.5"]
    assert len(tr) == 1 and "RAIN RULE" in tr[0]["settlement"]


def test_tier_a_exact_line_only_offtree_line_has_no_book():
    rows, inv = _rows()
    assert not any(r["market"] == "Total Runs 9.5" for r in rows)     # toa posts no 9.5 totals
    assert inv["tier_a_no_book"] >= 1


def test_tier_a_stale_book_leg_dropped_kills_the_arb():
    ev = _event()
    for bm in ev["bookmakers"]:
        if bm["key"] == "onexbet":
            bm["last_update"] = "2026-07-19T21:00:00Z"                # 2h stale
            for mk in bm["markets"]:
                mk["last_update"] = "2026-07-19T21:00:00Z"
    rows, _ = _rows(event=ev)
    assert not any(r["family"] == "ml2" for r in rows)               # without fresh 1xbet@2.30 -> no arb


# --- Tier B ----------------------------------------------------------------- #
def test_tier_b_run_line_complementary_and_book_only():
    rows, _ = _rows()
    rl = [r for r in rows if r["family"] == "run_line"]
    assert len(rl) == 1 and rl[0]["tier"] == "B" and rl[0]["rules_unverified"] is True
    for r in (x for x in rows if x["tier"] == "B"):                  # NEVER an exchange leg on a Tier B row
        assert all(lg["book"] in ("pinnacle", "1xbet") for lg in r["legs"])


def test_tier_b_totals_only_off_tree_lines():
    rows, _ = _rows()
    tb = [r for r in rows if r["family"] == "total"]
    assert tb and all(r["market"] == "Total 10.5" for r in tb)       # 8.5 & 9.5 are tree lines -> excluded


def test_tier_b_rejects_asymmetric_spread():
    ev = _event()
    for bm in ev["bookmakers"]:                                       # break complementarity: LAD +2.0
        for mk in bm["markets"]:
            if mk["key"] == "spreads":
                for o in mk["outcomes"]:
                    if o["name"] == "Los Angeles Dodgers":
                        o["point"] = 2.0
    rows, _ = _rows(event=ev)
    assert not any(r["family"] == "run_line" for r in rows)          # -1.5 + 2.0 != 0 -> no run_line row


# --- Tier B routing for the other sports ------------------------------------ #
def _tennis_by_game():
    ev = {"home_team": "Andrey Rublev", "away_team": "Alexei Tabilo", "commence_time": "2026-07-18T13:00:00Z",
          "bookmakers": [
              {"key": "pinnacle", "last_update": LAST, "markets": [
                  {"key": "totals", "last_update": LAST, "outcomes": [_o("Over", 2.05, 22.5), _o("Under", 1.85, 22.5)]},
                  {"key": "spreads", "last_update": LAST, "outcomes": [_o("Andrey Rublev", 2.05, -3.5), _o("Alexei Tabilo", 1.90, 3.5)]}]},
              {"key": "onexbet", "last_update": LAST, "markets": [
                  {"key": "totals", "last_update": LAST, "outcomes": [_o("Over", 1.90, 22.5), _o("Under", 2.05, 22.5)]},
                  {"key": "spreads", "last_update": LAST, "outcomes": [_o("Andrey Rublev", 1.90, -3.5), _o("Alexei Tabilo", 2.05, 3.5)]}]}]}
    game = {"home": "Alexei Tabilo", "away": "Andrey Rublev", "date": "2026-07-18",
            "kickoff_utc": "2026-07-18T13:00:00Z", "nodes": []}
    return {"t1": (ev, match.Matched("t1", game, "surname_tokens", 1.0))}


def test_tennis_tier_b_game_total_and_spread():
    rows, _ = tiers.build_rows("tennis", _tennis_by_game(), {}, cfg=CFG, now=NOW, log=LOG)
    fams = {r["family"] for r in rows}
    assert "game_total" in fams and "game_spread" in fams
    assert all(r["tier"] == "B" and r["rules_unverified"] for r in rows)
