"""Tennis total_sets family tests. The Poly 'Total Sets O/U 2.5' comes from the committed live fixture
(tests/fixtures/raw/tennis_rubtab_poly.json); Kalshi lists the WINNER only there, so today's live case is
Poly-only inventory. The synthesis + FORFEIT-RULE risk path (a Kalshi that lists exact set scores) is
proven against a SYNTHETIC Kalshi fixture, since Kalshi posts no set market yet."""
from __future__ import annotations

import json
import os

from src.genz import families_tennis as F
from src.genz import sports_tennis as T
from src.genz.config import load_genz_config
from src.genz.sports_base import run_registry

RAW = os.path.join(os.path.dirname(__file__), "fixtures", "raw")
_P = json.load(open(os.path.join(RAW, "tennis_rubtab_poly.json"), encoding="utf-8"))["event"]
_TS = next(m for m in _P["markets"] if "Total Sets" in m.get("question", ""))
_CTX = {"player_a": "Andrey Rublev", "player_b": "Alejandro Tabilo", "surname": T.surname}


def _reg(kalshi):
    return run_registry(F.tennis_families(), kalshi, [_TS], ctx=_CTX, game_id="RUBTAB")


def _kmkt(ticker, title):
    return {"ticker": ticker, "title": title, "yes_sub_title": title,
            "rules_primary": f"If {title}, the market resolves to Yes."}


# --------------------------------------------------------------------------- #
# Selectors + facet parser on the real Poly text                                #
# --------------------------------------------------------------------------- #
def test_poly_total_sets_selected_and_forfeit_is_5050():
    assert F.is_registry_poly(_TS)
    assert not F.is_registry_poly(next(m for m in _P["markets"] if "Match O/U" in m.get("question", "")))
    assert F.parse_total_sets_forfeit(_TS["description"]) == "fifty_fifty"   # 'not completed ... 50-50'


# --------------------------------------------------------------------------- #
# Case (c): today's live reality — Kalshi has no set market -> Poly-only inventory #
# --------------------------------------------------------------------------- #
def test_today_kalshi_has_no_sets_so_poly_only_inventory():
    nodes, unmatched, refusals = _reg([])
    assert nodes == [] and refusals == []
    assert len(unmatched) == 1 and unmatched[0]["family"] == "total_sets"
    assert unmatched[0]["reason"] == "one_venue_only" and unmatched[0]["venue"] == "polymarket"


# --------------------------------------------------------------------------- #
# Case (b): Kalshi lists EXACT set scores -> synthesize + FORFEIT-RULE risk        #
# --------------------------------------------------------------------------- #
def test_exact_scores_synthesize_over_under_flagged_forfeit_rule():
    kalshi = [_kmkt("KXATPSET-RUB21", "Rublev wins 2-1"), _kmkt("KXATPSET-TAB21", "Tabilo wins 2-1"),
              _kmkt("KXATPSET-RUB20", "Rublev wins 2-0"), _kmkt("KXATPSET-TAB20", "Tabilo wins 2-0")]
    nodes, unmatched, refusals = _reg(kalshi)
    assert {n["side"] for n in nodes} == {"over", "under"}
    assert all(n["settlement_risk"] == "forfeit_rule" for n in nodes)        # EXCLUDED, FORFEIT RULE chip
    assert all(n["synthesis"] == "exact_set_scores" for n in nodes)
    over = next(n for n in nodes if n["side"] == "over")
    assert set(over["synthesis_tickers"]) == {"KXATPSET-RUB21", "KXATPSET-TAB21"}   # 2-1 either player
    assert over["poly_side"] == "Over" and over["poly_token_id"]


# --------------------------------------------------------------------------- #
# Case (a): a NATIVE Kalshi total-sets O/U with a DIVERGENT forfeit rule -> risk   #
# --------------------------------------------------------------------------- #
def test_native_total_sets_with_divergent_forfeit_is_excluded():
    native = _kmkt("KXATPTOTALSETS-1", "Total sets over 2.5")
    native["rules_primary"] = ("Resolves Yes if the total number of sets is 3. If a player retires the "
                               "market settles on the sets completed at that point.")   # count_played != 50-50
    nodes, unmatched, refusals = _reg([native])
    assert {n["side"] for n in nodes} == {"over", "under"}
    assert all(n["settlement_risk"] == "forfeit_rule" for n in nodes)       # forfeit facet diverges -> risk


# --------------------------------------------------------------------------- #
# End-to-end: the winner path is unchanged; total_sets flows in as Poly inventory  #
# --------------------------------------------------------------------------- #
def test_registry_never_touches_winner_or_other_markets():
    # is_registry_poly must claim ONLY total-sets — not set winners, games O/U, handicap, match totals.
    claimed = [m.get("question") for m in _P["markets"] if F.is_registry_poly(m)]
    assert claimed == ["Andrey Rublev vs. Alejandro Tabilo: Total Sets O/U 2.5"]
