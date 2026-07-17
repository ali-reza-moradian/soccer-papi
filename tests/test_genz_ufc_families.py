"""UFC family-registry tests — selectors, settlement-facet parsers, and refusals driven ENTIRELY by the
committed raw fixtures (tests/fixtures/raw/ufc_duusm_*.json), never assumptions. Proves:

  * go_the_distance PAIRS (yes/no + dnc_50_50 note) because the scorecard-draw + technical-decision
    facets AGREE across venues, and REFUSES the moment a facet is made to diverge.
  * the DQ TRAP: Kalshi 'KO/TKO/DQ' vs Poly 'KO or TKO' (DQ excluded) -> REFUSE bucket_mismatch_dq,
    never flag-and-keep; a DQ-free Kalshi text (the TEXT decides) pairs instead.
  * round totals are UNPAIRABLE (Kalshi 'ends before round N' round-start vs Poly '2:30 mark') ->
    inventory reason='cutoff_convention_mismatch'.
  * the fight_winner path is byte-identical (its nodes are produced natively, untouched by the registry).
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone

from src.genz import families_ufc as F
from src.genz import sports_ufc as U
from src.genz.config import load_genz_config
from src.genz.sports_base import run_registry

RAW = os.path.join(os.path.dirname(__file__), "fixtures", "raw")


def _load(name):
    with open(os.path.join(RAW, name), encoding="utf-8") as fh:
        return json.load(fh)


_K = _load("ufc_duusm_kalshi.json")["series"]
_P = _load("ufc_duusm_poly.json")["event"]
_ESLUG = _P["slug"]
_EXTRA = _K["KXUFCDISTANCE"] + _K["KXUFCMOV"] + _K["KXUFCROUNDS"]
_POLY_REG = [m for m in _P["markets"] if str(m.get("slug")) != _ESLUG]
_CTX = {"fighter_a": "Dricus Du Plessis", "fighter_b": "Kamaru Usman",
        "surname": U.surname, "same_fighter": U._same_fighter}


def _registry(extra=None, poly=None):
    return run_registry(F.ufc_families(), extra if extra is not None else _EXTRA,
                        poly if poly is not None else _POLY_REG, ctx=_CTX, game_id="DUUSM")


def _k(series, frag):
    return next(m for m in _K[series] if frag in m["ticker"])


def _pq(question):
    return next(m for m in _P["markets"] if m.get("question") == question)


# --------------------------------------------------------------------------- #
# Facet parsers on the REAL texts                                               #
# --------------------------------------------------------------------------- #
def test_gtd_facets_agree_across_venues():
    k = _k("KXUFCDISTANCE", "DIST")
    p = _pq("Fight to Go the Distance?")
    kf = F.parse_gtd_facets(f"{k['rules_primary']} {k['rules_secondary']}")
    pf = F.parse_gtd_facets(p["description"])
    assert kf["scorecard_draw"] == pf["scorecard_draw"] == "yes"       # a scorecard draw went the distance
    assert kf["technical_decision"] == pf["technical_decision"] == "no"  # a technical decision did not
    assert pf["nc"] == "fifty_fifty" and kf["nc"] is None              # NC facet diverges -> INFORMATIONAL


def test_method_bucket_dq_trap_from_real_texts():
    k = _k("KXUFCMOV", "USMKOTKODQ")
    p = _pq("Will Kamaru Usman win by KO or TKO?")
    assert F.method_bucket(k["yes_sub_title"]) == {"type": "kotko", "dq": True}     # Kalshi FOLDS DQ in
    assert F.method_bucket(p["question"] + " " + p["description"]) == {"type": "kotko", "dq": False}  # Poly OUT


def test_round_cutoff_conventions_differ():
    k = _k("KXUFCROUNDS", "-5")
    p = _pq("O/U 2.5 Rounds")
    assert F.round_cutoff_convention(f"{k['title']} {k['rules_primary']}") == "round_start"
    assert F.round_cutoff_convention(f"{p['question']} {p['description']}") == "half_round"


# --------------------------------------------------------------------------- #
# go_the_distance — PAIR on agreement, REFUSE on a manufactured divergence        #
# --------------------------------------------------------------------------- #
def test_gtd_pairs_yes_no_with_dnc_note():
    nodes, unmatched, refusals = _registry()
    gtd = [n for n in nodes if n["market_type"] == "go_the_distance"]
    assert len(gtd) == 2 and {n["side"] for n in gtd} == {"yes", "no"}
    assert all(n["settlement_note"] == "dnc_50_50" for n in gtd)       # NC divergence -> informational
    assert all(n["kind"] == "2way" and n["poly_fee_rate"] == 0.05 for n in gtd)
    yes = next(n for n in gtd if n["side"] == "yes")
    assert yes["kalshi_side"] == "YES" and yes["poly_side"] == "Yes" and yes["poly_token_id"]
    assert not any(r["family"] == "go_the_distance" for r in refusals)


def test_gtd_refuses_when_a_facet_diverges():
    poly = copy.deepcopy(_POLY_REG)
    gtd = next(m for m in poly if "go-the-distance" in m["slug"])
    # Flip ONLY the technical-decision facet on the Poly side -> a real definition divergence.
    gtd["description"] = gtd["description"].replace(
        "Technical decisions or technical draws declared before all rounds are completed will resolve \"No.\"",
        "Technical decisions or technical draws declared before all rounds are completed will resolve \"Yes.\"")
    nodes, unmatched, refusals = _registry(poly=poly)
    assert not any(n["market_type"] == "go_the_distance" for n in nodes)   # NOT paired
    assert any(r["family"] == "go_the_distance" and r["reason"] == "gtd_facet_mismatch_technical_decision"
               for r in refusals)


# --------------------------------------------------------------------------- #
# method_by_kotko — the DQ trap REFUSES; a DQ-free text PAIRS (the TEXT decides)   #
# --------------------------------------------------------------------------- #
def test_method_kotko_refuses_on_dq_mismatch():
    nodes, unmatched, refusals = _registry()
    dq = [r for r in refusals if r["reason"] == "bucket_mismatch_dq"]
    assert len(dq) == 2                                                # both fighters' KO/TKO buckets
    assert {("KO/TKO/DQ" in r["kalshi"]) for r in dq} == {True}
    assert not any(n["market_type"] == "method_by_kotko" for n in nodes)   # never flag-and-keep


def test_method_kotko_pairs_when_kalshi_is_dq_free():
    extra = copy.deepcopy(_EXTRA)
    for m in extra:                                                    # a HYPOTHETICAL DQ-free Kalshi text
        if "KOTKODQ" in m["ticker"]:
            m["yes_sub_title"] = m["yes_sub_title"].replace("KO/TKO/DQ", "KO/TKO")
            m["title"] = m["title"].replace("KO/TKO/DQ", "KO or TKO")
    nodes, unmatched, refusals = _registry(extra=extra)
    kotko = [n for n in nodes if n["market_type"] == "method_by_kotko"]
    assert len(kotko) == 4 and {n["side"] for n in kotko} == {"yes", "no"}   # 2 fighters x yes/no
    assert not any(r["reason"] == "bucket_mismatch_dq" for r in refusals)


# --------------------------------------------------------------------------- #
# round_totals — unpairable, inventoried with the exact reason                    #
# --------------------------------------------------------------------------- #
def test_round_totals_inventoried_cutoff_mismatch():
    nodes, unmatched, refusals = _registry()
    assert not any(n["market_type"] == "round_totals" for n in nodes)
    rounds = [u for u in unmatched if u.get("family") == "round_totals"]
    assert rounds and all(u["reason"] == "cutoff_convention_mismatch" for u in rounds)
    assert {u["venue"] for u in rounds} == {"kalshi", "polymarket"}    # both sides listed


def test_inventory_covers_every_unpaired_market_no_no_family_noise():
    nodes, unmatched, refusals = _registry()
    # Every sibling Kalshi market + every non-winner Poly market is CLAIMED by a family (no 'no_family').
    assert not any(u.get("reason") == "no_family" for u in unmatched)
    reasons = {u["reason"] for u in unmatched}
    assert reasons == {"cutoff_convention_mismatch", "no_exact_twin"}


# --------------------------------------------------------------------------- #
# End-to-end build: registry flows into the tree; fight_winner stays native       #
# --------------------------------------------------------------------------- #
_EVENT = "KXUFCFIGHT-26JUL18DUUSM"
UFC_NOW = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)


class _KalshiWithSiblings:
    """Serves KXUFCFIGHT (winner) AND the sibling series from the committed fixture — so the registry
    runs end-to-end through build_tree, not just in isolation."""

    def iter_markets(self, *, series_ticker=None, status="open", **kw):
        if series_ticker == "KXUFCFIGHT":
            return list(_K["KXUFCFIGHT"])
        return list(_K.get(series_ticker, []))


class _PolyFull:
    def events_by_series(self, sport, *, closed=False, **kw):
        return [_P] if sport == "ufc" else []


def _build_full():
    from src.genz import tree_builder as tb
    return tb.build_tree(_KalshiWithSiblings(), _PolyFull(), load_genz_config(sport="ufc"),
                         now=UFC_NOW, spec=U.UFC_SPEC)


def test_end_to_end_build_adds_families_and_keeps_winner():
    g = _build_full()["games"][_EVENT]
    types = {n["market_type"] for n in g["nodes"]}
    assert "fight_winner" in types and "go_the_distance" in types      # winner + the new clean family
    assert "method_by_kotko" not in types and "round_totals" not in types  # both correctly unpaired
    winner = [n for n in g["nodes"] if n["market_type"] == "fight_winner"]
    assert {n["twin_key"] for n in winner} == {"fight_winner|usman", "fight_winner|du plessis"}
    assert g["coverage"]["refused_families"] == 2                       # both KO/TKO buckets refused
    assert any(r["reason"] == "bucket_mismatch_dq" for r in g["refusals"])
