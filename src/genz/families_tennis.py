"""Tennis market families beyond the match winner — written against the raw dumped texts
(tests/fixtures/raw/tennis_rubtab_*.json):

  total_sets   Poly 'Total Sets O/U 2.5' (Over 2.5 = 3 completed sets) <-> a Kalshi total-sets market.
               THREE cases, decided by what Kalshi actually lists:
                 (a) a NATIVE Kalshi total-sets O/U -> pair (over/under) if the forfeit/retirement facet
                     AGREES; on divergence -> settlement_risk='forfeit_rule' (excluded, FORFEIT RULE chip).
                 (b) only EXACT set scores ('X wins 2-1' / '2-0') -> SYNTHESIZE (over 2.5 == a 2-1 by
                     either player). A retirement resolves Poly 50-50 but leaves NO official 2-1/2-0, so
                     the synthesized legs and Poly DIVERGE on the forfeit axis -> settlement_risk=
                     'forfeit_rule' (excluded, FORFEIT RULE chip). The divergence is EXPECTED.
                 (c) neither (today's live reality — Kalshi lists the winner only) -> Poly-only inventory.

Live-verified 2026-07: Kalshi KX(ATP|WTA)MATCH carries the WINNER only (no set series, no exact-score
markets), so total_sets is case (c) — Poly-only inventory — until Kalshi posts a set market. The
synthesis + forfeit-risk path (a/b) is unit-tested against a synthetic fixture in test_genz_tennis_families.
"""
from __future__ import annotations

import re
from typing import Optional

from .. import polymarket as pm
from .sports_base import FamilyResult, FamilySpec
from .tree_builder import _poly_market_fee


# --------------------------------------------------------------------------- #
# Selectors — scope the registry to the total-sets markets on each venue         #
# --------------------------------------------------------------------------- #
def _q(m: dict) -> str:
    return str(m.get("question") or m.get("groupItemTitle") or "")


def _pid(m: dict) -> str:
    return str(m.get("slug") or m.get("id") or "")


def _k_text(m: dict) -> str:
    return " ".join(str(m.get(k) or "") for k in ("title", "yes_sub_title", "rules_primary")).lower()


def is_registry_poly(m: dict) -> bool:
    """The Poly 'Total Sets O/U N.5' market (NOT set-games totals, match totals, or set winners)."""
    return "total sets" in _q(m).lower()


def _k_native_total_sets(m: dict) -> bool:
    t = _k_text(m)
    return "total sets" in t or "number of sets" in t or "total number of sets" in t


def _k_exact_set_score(m: dict) -> bool:
    """A Kalshi EXACT set-score market ('X wins 2-1', '2-0', 'straight sets', 'deciding set')."""
    t = _k_text(m)
    return bool(re.search(r'2\s*[-–]\s*[01]\b', t) or "straight set" in t or "deciding set" in t)


def is_registry_kalshi(m: dict) -> bool:
    return _k_native_total_sets(m) or _k_exact_set_score(m)


# --------------------------------------------------------------------------- #
# Facet parser — the forfeit/retirement axis                                     #
# --------------------------------------------------------------------------- #
def parse_total_sets_forfeit(text: str) -> Optional[str]:
    """How a total-sets text handles a started-but-INCOMPLETE match (retirement/walkover):
        'fifty_fifty'  -> resolves 50-50 (Poly: 'if the match begins but is not completed ... 50-50')
        'count_played' -> settles on the sets actually completed
        None           -> unstated."""
    low = " ".join(str(text or "").lower().split())
    if re.search(r'(not completed|does not finish|retire|walkover|withdraw)[^.]{0,60}50-?50', low) \
            or re.search(r'begins but is not completed[^.]{0,60}50-?50', low):
        return "fifty_fifty"
    if re.search(r'(not completed|retire|walkover)[^.]{0,60}(sets? (completed|played)|official)', low):
        return "count_played"
    return None


# --------------------------------------------------------------------------- #
# Poly O/U helpers                                                               #
# --------------------------------------------------------------------------- #
def _ou_tokens(m: dict) -> Optional[tuple[str, str]]:
    """(over_token, under_token) for a Poly O/U market, or None."""
    outs = [str(o).strip().lower() for o in pm._as_list(m.get("outcomes"))]
    toks = pm._as_list(m.get("clobTokenIds"))
    over = next((i for i, o in enumerate(outs) if o.startswith("over")), None)
    under = next((i for i, o in enumerate(outs) if o.startswith("under")), None)
    if over is None or under is None or len(toks) <= max(over, under):
        return None
    return str(toks[over]), str(toks[under])


def _set_score_side(m: dict) -> Optional[str]:
    """'over' (a 2-1 / deciding-set / three-set market -> 3 sets played) or 'under' (a 2-0 / straight-set
    market -> 2 sets) for a Kalshi exact-score market."""
    t = _k_text(m)
    if re.search(r'2\s*[-–]\s*1\b', t) or "deciding set" in t or "three set" in t:
        return "over"
    if re.search(r'2\s*[-–]\s*0\b', t) or "straight set" in t:
        return "under"
    return None


# --------------------------------------------------------------------------- #
# Family: total_sets                                                             #
# --------------------------------------------------------------------------- #
def _build_total_sets(k_claim: list, p_claim: list, ctx: dict) -> FamilyResult:
    res = FamilyResult()
    p_ts = next((m for m in p_claim if _ou_tokens(m)), None)
    if p_ts is None:                                       # kalshi-only (no Poly O/U) -> inventory
        for m in k_claim:
            res.claimed_k.add(str(m.get("ticker")))
            res.inventory.append({"venue": "kalshi", "family": "total_sets", "title": m.get("title"),
                                  "reason": "one_venue_only"})
        return res
    res.claimed_p.add(_pid(p_ts))
    native = [m for m in k_claim if _k_native_total_sets(m)]
    exact = [m for m in k_claim if _k_exact_set_score(m) and not _k_native_total_sets(m)]
    if native:
        return _pair_native(native[0], p_ts, k_claim, res)
    if exact:
        return _synthesize(exact, p_ts, res)
    # Case (c): today's reality — Kalshi lists no set market at all -> Poly-only inventory.
    res.inventory.append({"venue": "polymarket", "family": "total_sets", "title": _q(p_ts),
                          "reason": "one_venue_only"})
    return res


def _pair_native(km: dict, p_ts: dict, k_claim: list, res: FamilyResult) -> FamilyResult:
    for m in k_claim:
        res.claimed_k.add(str(m.get("ticker")))
    ou = _ou_tokens(p_ts)
    kf = parse_total_sets_forfeit(f"{km.get('rules_primary','')} {km.get('rules_secondary','')}")
    pf = parse_total_sets_forfeit(str(p_ts.get("description") or ""))
    risk = "forfeit_rule" if (kf != pf or kf is None or pf is None) else ""   # divergent/unknown -> excluded
    over_tok, under_tok = ou
    for side, ktoggle, tok in (("over", "YES", over_tok), ("under", "NO", under_tok)):
        res.nodes.append(_ts_node(km.get("ticker"), ktoggle, tok, side, p_ts, risk,
                                  synthesis=None, kf=kf, pf=pf))
    return res


def _synthesize(exact: list, p_ts: dict, res: FamilyResult) -> FamilyResult:
    """Case (b): synthesize Over 2.5 == (2-1 by either player) from exact-score markets. A retirement
    yields no official 2-1/2-0 while Poly resolves 50-50 -> the forfeit axis DIVERGES by construction ->
    settlement_risk='forfeit_rule' (excluded, FORFEIT RULE chip). EXPECTED — never a tradable node."""
    for m in exact:
        res.claimed_k.add(str(m.get("ticker")))
    ou = _ou_tokens(p_ts)
    over_k = [m for m in exact if _set_score_side(m) == "over"]
    under_k = [m for m in exact if _set_score_side(m) == "under"]
    over_tok, under_tok = ou
    for side, ktoggle, tok, legs in (("over", "YES", over_tok, over_k), ("under", "NO", under_tok, under_k)):
        rep = legs[0] if legs else exact[0]
        res.nodes.append(_ts_node(rep.get("ticker"), ktoggle, tok, side, p_ts, "forfeit_rule",
                                  synthesis=[str(m.get("ticker")) for m in legs] or None))
    return res


def _ts_node(kticker, kside, ptok, side, p_ts, risk, *, synthesis=None, kf=None, pf=None) -> dict:
    fee = _poly_market_fee(p_ts)
    node = {"twin_key": f"total_sets|{side}", "market_type": "total_sets", "market_key": "total_sets",
            "side": side, "outcome_label": f"Total sets {side} 2.5", "line": 2.5, "kind": "2way",
            "confidence": "high", "kalshi_ticker": kticker, "kalshi_side": kside,
            "poly_token_id": str(ptok), "poly_side": "Over" if side == "over" else "Under",
            "poly_fee_enabled": fee["enabled"], "poly_fee_rate": fee["rate"],
            "poly_fee_taker_only": fee["taker_only"],
            "settlement_texts": {"poly": str(p_ts.get("description") or "")[:600]},
            "kalshi_rule": {"forfeit": kf}, "poly_rule": {"forfeit": pf}}
    if synthesis:
        node["synthesis_tickers"] = synthesis                # the exact-score legs this Over/Under sums
        node["synthesis"] = "exact_set_scores"
    if risk:
        node["settlement_risk"] = risk                       # 'forfeit_rule' -> excluded everywhere
    return node


def tennis_families() -> list:
    return [FamilySpec("total_sets", "2way", is_registry_kalshi, is_registry_poly, _build_total_sets,
                       settlement_axes=("forfeit_rule",))]
