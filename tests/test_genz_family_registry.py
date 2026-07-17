"""Cross-cutting family-registry tests: the run_registry primitive (residual 'no_family'), the MLB audit
(Kalshi lists GAME+TOTAL only -> already at max overlap, no forced additions), the snapshot propagating
refused-family counts, and maker_rt automatically ADMITTING a clean new family (go_the_distance) while
EXCLUDING a settlement-risk one (total_sets forfeit_rule) with NO rail changes."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from src.genz.sports_base import FamilyResult, FamilySpec, run_registry

RAW = os.path.join(os.path.dirname(__file__), "fixtures", "raw")


# --------------------------------------------------------------------------- #
# run_registry primitive — claimed markets vs the 'no_family' residual            #
# --------------------------------------------------------------------------- #
def test_registry_residual_is_no_family():
    def build(k, p, ctx):
        r = FamilyResult()
        for m in k:
            r.claimed_k.add(str(m.get("ticker")))
        for m in p:
            r.claimed_p.add(str(m.get("slug")))
        r.nodes.append({"twin_key": "fam|x", "market_type": "fam"})
        return r
    fam = FamilySpec("fam", "2way", lambda m: m.get("ticker") == "K1", lambda m: m.get("slug") == "P1", build)
    kalshi = [{"ticker": "K1", "title": "claimed"}, {"ticker": "K2", "title": "orphan-k"}]
    poly = [{"slug": "P1", "question": "claimed"}, {"slug": "P2", "question": "orphan-p"}]
    nodes, unmatched, refusals = run_registry([fam], kalshi, poly)
    assert len(nodes) == 1 and refusals == []
    nofam = [u for u in unmatched if u.get("reason") == "no_family"]
    assert {u["identifier"] for u in nofam} == {"K2", "P2"}       # only the unclaimed markets
    assert {u["venue"] for u in nofam} == {"kalshi", "polymarket"}


def test_registry_selector_exception_is_swallowed():
    boom = FamilySpec("boom", "2way", lambda m: 1 / 0, lambda m: False, lambda k, p, c: FamilyResult())
    nodes, unmatched, refusals = run_registry([boom], [{"ticker": "K1"}], [])
    assert nodes == [] and refusals == []                        # a crashing selector never aborts the build
    assert unmatched and unmatched[0]["reason"] == "no_family"


# --------------------------------------------------------------------------- #
# MLB audit — Kalshi lists GAME + TOTAL only; Poly NRFI has no Kalshi twin        #
# --------------------------------------------------------------------------- #
def test_mlb_kalshi_is_game_and_total_only_max_overlap():
    k = json.load(open(os.path.join(RAW, "mlb_ladnyy_kalshi.json"), encoding="utf-8"))
    # The MLB adapter already pairs BOTH families Kalshi exposes: moneyline (game) + total runs (total).
    assert set(k.keys()) >= {"game_markets", "total_markets"}
    assert all("KXMLBGAME" in m["ticker"] for m in k["game_markets"])
    assert all("KXMLBTOTAL" in m["ticker"] for m in k.get("total_markets", []))   # empty for this game
    p = json.load(open(os.path.join(RAW, "mlb_ladnyy_poly.json"), encoding="utf-8"))["event"]
    ptypes = {m.get("sportsMarketType") for m in p["markets"]}
    assert "moneyline" in ptypes                                  # paired to KXMLBGAME
    assert "nrfi" in ptypes                                       # Poly-only: Kalshi lists NO first-inning market
    # -> MLB is already at maximum cross-venue overlap; no forced additions.


# --------------------------------------------------------------------------- #
# Snapshot carries the per-game refused-family count (panel tooltip source)        #
# --------------------------------------------------------------------------- #
def test_snapshot_propagates_refused_families():
    from src.genz.engine import build_snapshot
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": "2026-07-18T21:00:00Z",
                             "coverage": {"refused_families": 2},
                             "refusals": [{"family": "method_by_kotko", "reason": "bucket_mismatch_dq"}],
                             "nodes": []}}}
    snap = build_snapshot(tree, markets=[], priced={}, rows_by_key={}, now=now, sport="ufc")
    g = snap["games"]["G1"]
    assert g["refused_families"] == 2
    assert g["refusals"][0]["reason"] == "bucket_mismatch_dq"


def test_snapshot_refused_families_defaults_zero_for_soccer():
    from src.genz.engine import build_snapshot
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": "2026-07-18T21:00:00Z", "nodes": []}}}
    g = build_snapshot(tree, markets=[], priced={}, rows_by_key={}, now=now)["games"]["G1"]
    assert g["refused_families"] == 0 and g["refusals"] == []     # soccer/mlb have no registry -> 0


# --------------------------------------------------------------------------- #
# maker_rt: a clean new family flows into the universe; a risk one never does      #
# --------------------------------------------------------------------------- #
def _node(mtype, mkey, side, tok, kt, kside, risk=None, note=None):
    n = {"market_type": mtype, "market_key": mkey, "side": side, "line": None, "kind": "2way",
         "poly_token_id": tok, "poly_side": side.title(), "poly_fee_rate": 0.05,
         "kalshi_ticker": kt, "kalshi_side": kside}
    if risk:
        n["settlement_risk"] = risk
    if note:
        n["settlement_note"] = note
    return n


def test_maker_rt_admits_clean_family_excludes_risk_family():
    from src.genz.maker_rt.universe import build_universe, poly_tokens
    future = "2027-01-01T00:00:00Z"
    tree = {"games": {
        # go_the_distance: clean 2-way MECE with an INFORMATIONAL dnc note -> ADMITTED (still tradable).
        "GTD": {"away": "A", "home": "B", "kickoff_utc": future, "nodes": [
            _node("go_the_distance", "go_the_distance", "yes", "gd_y", "KXDIST", "YES", note="dnc_50_50"),
            _node("go_the_distance", "go_the_distance", "no", "gd_n", "KXDIST", "NO", note="dnc_50_50")]},
        # total_sets synthesis: settlement_risk='forfeit_rule' -> EXCLUDED everywhere, never quoted.
        "TS": {"away": "C", "home": "D", "kickoff_utc": future, "nodes": [
            _node("total_sets", "total_sets", "over", "ts_o", "KXSET", "YES", risk="forfeit_rule"),
            _node("total_sets", "total_sets", "under", "ts_u", "KXSET", "NO", risk="forfeit_rule")]}}}
    uni = build_universe({"ufc": tree}, now_ts=0.0, max_games=20, expire_before_kickoff_s=120)
    games = {qm.game for qm in uni}
    assert "GTD" in games and "TS" not in games                  # clean admitted, forfeit-risk excluded
    assert set(poly_tokens(uni)) == {"gd_y", "gd_n"}
