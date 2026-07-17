"""UFC market families beyond the fight winner — written ENTIRELY against the raw dumped texts
(tests/fixtures/raw/ufc_duusm_*.json), never assumptions:

  go_the_distance  Kalshi KXUFCDISTANCE 'goes the distance' <-> Poly 'Fight to Go the Distance?'.
                   PAIR (yes/no) only if the scorecard-draw + technical-decision facets AGREE; an NC/
                   cancel divergence is informational (dnc_50_50 note), NOT a refusal.
  method_by_kotko  Kalshi KXUFCMOV 'X by KO/TKO/DQ' <-> Poly 'X win by KO or TKO'. THE DQ TRAP: Kalshi's
                   bucket INCLUDES DQ, Poly's EXCLUDES it ('by disqualification ... resolve No') -> a DQ
                   win loses both legs -> REFUSE reason='bucket_mismatch_dq'. If a future Kalshi text is
                   DQ-free the parser sees it and the pair proceeds (yes/no) — the TEXT decides.
                   Submission/decision have no exact same-granularity twin (Poly submission is fight-
                   level, Poly has no decision) -> inventory, never forced.
  round_totals     Kalshi KXUFCROUNDS 'ends before round N' (round-START boundary) vs Poly 'O/U N.5'
                   (the 2:30 half-round mark) -> DIFFERENT cutoff conventions -> INVENTORY
                   reason='cutoff_convention_mismatch' (never forced).

Verified live 2026-07: KXUFCDISTANCE (goes the distance), KXUFCMOV (method of victory, DQ folded into
the KO/TKO bucket), KXUFCROUNDS ('ends before round N'). See tests/test_genz_ufc_families.py.
"""
from __future__ import annotations

import re
from typing import Optional

from .. import polymarket as pm
from .sports_base import FamilyResult, FamilySpec
from .tree_builder import _poly_market_fee


# --------------------------------------------------------------------------- #
# Facet parsers (pure — unit-tested against the committed fixtures)              #
# --------------------------------------------------------------------------- #
def _sentences(text: str) -> list[str]:
    # Break after a period (optionally closing a quote, as Poly writes it: 'resolve "No." Technical ...')
    # or a newline. Without the optional closing-quote the scorecard-draw and technical-draw clauses fuse
    # into one sentence and neither facet can be read.
    norm = " ".join(str(text or "").split())
    return [s for s in re.split(r'(?<=[.\n])"?\s+', norm) if s]


def _yesno(sentence: str) -> Optional[str]:
    """The resolution a single sentence names: 'yes' | 'no' | None (only when it names one, not both).
    Tolerant of the quoting Poly uses ('will resolve \"Yes.\"')."""
    low = sentence.lower()
    has_yes = bool(re.search(r'resolve[^"]{0,8}"?\s*yes\b', low))
    has_no = bool(re.search(r'resolve[^"]{0,8}"?\s*no\b', low))
    if has_yes and not has_no:
        return "yes"
    if has_no and not has_yes:
        return "no"
    return None


def parse_gtd_facets(text: str) -> dict:
    """The three settlement facets of a Go-the-Distance text, classified per SENTENCE (robust to the
    scorecard-draw and technical-draw sentences sitting side by side):
        scorecard_draw     'yes'|'no'|None — does a scorecard draw AFTER all rounds count as distance?
        technical_decision 'yes'|'no'|None — does a technical decision BEFORE completion count?
        nc                 'fifty_fifty'|'last_price'|None — NC/cancel handling
    'Goes the distance' is a defined term (the fight reaches the final bell): a plain Kalshi text with no
    overriding sentence yields the STANDARD facets (scorecard-draw=yes, technical-decision=no) — exactly
    what the Poly text states explicitly, so the two AGREE."""
    low = " ".join(str(text or "").lower().split())
    goes = ("goes the distance" in low or "goes the full" in low
            or "full scheduled number of rounds" in low)
    scorecard = technical = nc = None
    for s in _sentences(low):
        if scorecard is None and "scorecard" in s and "draw" in s:
            scorecard = _yesno(s)
        if technical is None and ("technical decision" in s or "technical draw" in s):
            technical = _yesno(s)
        if nc is None and any(k in s for k in ("no contest", "not scored", "cancel", "postpon")):
            if re.search(r'50-?50', s):
                nc = "fifty_fifty"
            elif re.search(r'(fair|last)[^.]{0,12}price', s):
                nc = "last_price"
    if scorecard is None and goes:
        scorecard = "yes"                                  # standard: a scorecard draw went the distance
    if technical is None and goes:
        technical = "no"                                   # standard: stopped before the bell != distance
    return {"scorecard_draw": scorecard, "technical_decision": technical, "nc": nc}


def method_bucket(text: str) -> Optional[dict]:
    """Classify a method-of-victory text into {type, dq} where type in {'kotko','submission','decision'}
    and dq=True when a DISQUALIFICATION resolves the SAME side. Kalshi folds DQ into its KO/TKO bucket
    (the identity literally reads 'KO/TKO/DQ'); Poly's 'KO or TKO' text EXCLUDES it ('by disqualification
    ... resolve No'). None when the text isn't a method market."""
    low = " ".join(str(text or "").lower().split())
    if "submission" in low:
        return {"type": "submission", "dq": bool(re.search(r'\bdq\b|/dq', low)) and "disqualification" not in low}
    if "decision" in low:
        return {"type": "decision", "dq": False}
    if "ko" in low or "tko" in low:
        dq = bool(re.search(r'ko/tko/dq|kotkodq|\bdq\b', low)) and "disqualification" not in low
        return {"type": "kotko", "dq": dq}
    return None


def round_cutoff_convention(text: str) -> Optional[str]:
    """'round_start' (Kalshi 'ends before round N' — the boundary is the START of round N) vs 'half_round'
    (Poly 'Over N.5' — the boundary is the 2:30 mid-round mark). None when not a rounds market."""
    low = " ".join(str(text or "").lower().split())
    if "ends before round" in low or "end before round" in low:
        return "round_start"
    if "2:30" in low or re.search(r'past the 2:?30', low) or ("o/u" in low and "round" in low):
        return "half_round"
    return None


# --------------------------------------------------------------------------- #
# Poly market helpers                                                           #
# --------------------------------------------------------------------------- #
def _poly_outs_toks(m: dict):
    return pm._as_list(m.get("outcomes")), pm._as_list(m.get("clobTokenIds"))


def _q(m: dict) -> str:
    return str(m.get("question") or m.get("groupItemTitle") or "")


def _pid(m: dict) -> str:
    return str(m.get("slug") or m.get("id") or "")


def _yes_no_nodes(km: dict, pm_: dict, family: str, twin_suffix: str, note: str = "",
                  kalshi_rule: Optional[dict] = None, poly_rule: Optional[dict] = None
                  ) -> Optional[list]:
    """Two paired nodes (yes/no) from a Kalshi yes/no market + a Poly Yes/No market — the shape shared by
    go_the_distance and (on a DQ MATCH) method_by_kotko. None if the Poly market isn't a clean Yes/No."""
    outs, toks = _poly_outs_toks(pm_)
    norm = [str(o).strip().lower() for o in outs]
    if "yes" not in norm or "no" not in norm or len(toks) < 2:
        return None
    fee = _poly_market_fee(pm_)
    texts = {"kalshi": f"{km.get('rules_primary','')} {km.get('rules_secondary','')}".strip()[:600],
             "poly": str(pm_.get("description") or "")[:600]}
    yes_tok, no_tok = toks[norm.index("yes")], toks[norm.index("no")]
    key = "|".join(x for x in (family, twin_suffix) if x)
    label = km.get("yes_sub_title") or km.get("title") or family
    nodes = []
    for side, ktoggle, tok in (("yes", "YES", yes_tok), ("no", "NO", no_tok)):
        node = {"twin_key": f"{key}|{side}", "market_type": family, "market_key": key, "side": side,
                "outcome_label": f"{label} — {side}", "line": None, "kind": "2way", "confidence": "high",
                "kalshi_ticker": km.get("ticker"), "kalshi_side": ktoggle,
                "poly_token_id": str(tok), "poly_side": "Yes" if side == "yes" else "No",
                "poly_fee_enabled": fee["enabled"], "poly_fee_rate": fee["rate"],
                "poly_fee_taker_only": fee["taker_only"],
                "settlement_texts": texts, "kalshi_rule": kalshi_rule or {}, "poly_rule": poly_rule or {}}
        if note:
            node["settlement_note"] = note
        nodes.append(node)
    return nodes


# --------------------------------------------------------------------------- #
# Family: go_the_distance (2-way yes/no)                                         #
# --------------------------------------------------------------------------- #
def _k_is_gtd(m: dict) -> bool:
    return "distance" in f"{m.get('title','')} {m.get('yes_sub_title','')}".lower()


def _p_is_gtd(m: dict) -> bool:
    q = _q(m).lower()
    return "go the distance" in q or "goes the distance" in q


def _build_gtd(k_claim: list, p_claim: list, ctx: dict) -> FamilyResult:
    res = FamilyResult()
    km = k_claim[0] if k_claim else None
    pm_ = next((m for m in p_claim if len(_poly_outs_toks(m)[1]) >= 2), None)
    if km is None or pm_ is None:                          # one venue only -> inventory both sides
        for m in k_claim:
            res.claimed_k.add(str(m.get("ticker")))
            res.inventory.append({"venue": "kalshi", "family": "go_the_distance",
                                  "title": m.get("title"), "reason": "one_venue_only"})
        for m in p_claim:
            res.claimed_p.add(_pid(m))
            res.inventory.append({"venue": "polymarket", "family": "go_the_distance",
                                  "title": _q(m), "reason": "one_venue_only"})
        return res
    res.claimed_k.add(str(km.get("ticker")))
    res.claimed_p.add(_pid(pm_))
    kf = parse_gtd_facets(f"{km.get('rules_primary','')} {km.get('rules_secondary','')}")
    pf = parse_gtd_facets(str(pm_.get("description") or ""))
    for axis in ("scorecard_draw", "technical_decision"):   # a real definition divergence -> refuse
        if kf[axis] is not None and pf[axis] is not None and kf[axis] != pf[axis]:
            res.refusals.append({"family": "go_the_distance", "reason": f"gtd_facet_mismatch_{axis}",
                                 "kalshi": km.get("yes_sub_title"), "poly": _q(pm_)})
            return res
    note = "dnc_50_50" if kf["nc"] != pf["nc"] else ""      # NC divergence is informational, not a refusal
    nodes = _yes_no_nodes(km, pm_, "go_the_distance", "", note=note, kalshi_rule=kf, poly_rule=pf)
    if nodes is None:
        res.inventory.append({"venue": "polymarket", "family": "go_the_distance", "title": _q(pm_),
                              "reason": "poly_not_yes_no"})
    else:
        res.nodes.extend(nodes)
    return res


# --------------------------------------------------------------------------- #
# Family: method_by_kotko (the DQ trap) + method inventory                        #
# --------------------------------------------------------------------------- #
def _k_is_method(m: dict) -> bool:
    return "KXUFCMOV" in str(m.get("ticker") or "")


def _p_is_method(m: dict) -> bool:
    q = _q(m).lower()
    return ("by ko" in q or "by tko" in q or "win by ko or tko" in q or "won by ko or tko" in q
            or "by submission" in q or "by decision" in q)


def _build_method(k_claim: list, p_claim: list, ctx: dict) -> FamilyResult:
    same_fighter = ctx.get("same_fighter") or (lambda a, b: False)
    res = FamilyResult()
    for km in k_claim:
        res.claimed_k.add(str(km.get("ticker")))
        kb = method_bucket(km.get("yes_sub_title") or km.get("title"))
        if not kb or kb["type"] != "kotko":                # submission / decision / draw-nc: no exact twin
            res.inventory.append({"venue": "kalshi", "family": "method", "title": km.get("yes_sub_title"),
                                  "reason": "no_exact_twin"})
            continue
        fighter_sn = _method_fighter(km.get("yes_sub_title") or "", ctx)
        twin = next((m for m in p_claim if _pid(m) not in res.claimed_p
                     and (method_bucket(_q(m)) or {}).get("type") == "kotko"
                     and _poly_names_fighter(m, fighter_sn)), None)
        if twin is None:
            res.inventory.append({"venue": "kalshi", "family": "method_by_kotko",
                                  "title": km.get("yes_sub_title"), "reason": "one_venue_only"})
            continue
        res.claimed_p.add(_pid(twin))
        pb = method_bucket(f"{_q(twin)} {twin.get('description','')}")
        if kb["dq"] != (pb or {}).get("dq"):               # THE DQ TRAP -> refuse (never flag-and-keep)
            res.refusals.append({"family": "method_by_kotko", "reason": "bucket_mismatch_dq",
                                 "kalshi": km.get("yes_sub_title"), "poly": _q(twin)})
            continue
        nodes = _yes_no_nodes(km, twin, "method_by_kotko", fighter_sn.replace(" ", "_"),
                              kalshi_rule=kb, poly_rule=pb)   # texts AGREE (DQ-free both sides) -> pair
        if nodes:
            res.nodes.extend(nodes)
    for m in p_claim:                                       # Poly method markets with no exact twin
        if _pid(m) not in res.claimed_p:
            res.claimed_p.add(_pid(m))
            res.inventory.append({"venue": "polymarket", "family": "method", "title": _q(m),
                                  "reason": "no_exact_twin"})
    return res


def _method_fighter(yes_sub: str, ctx: dict) -> str:
    surname = ctx.get("surname") or (lambda s: s)
    same = ctx.get("same_fighter") or (lambda a, b: False)
    for fn in (ctx.get("fighter_a"), ctx.get("fighter_b")):
        if fn and same(yes_sub, fn):
            return surname(fn)
    return surname(re.split(r"\bby\b", yes_sub, flags=re.I)[0].strip())


def _poly_names_fighter(m: dict, fighter_surname: str) -> bool:
    return bool(fighter_surname) and fighter_surname.split()[-1] in _q(m).lower()


# --------------------------------------------------------------------------- #
# Family: round_totals (cutoff-convention mismatch -> inventory, never a node)    #
# --------------------------------------------------------------------------- #
def _k_is_rounds(m: dict) -> bool:
    return "KXUFCROUNDS" in str(m.get("ticker") or "") or "end before round" in str(m.get("title", "")).lower()


def _p_is_rounds(m: dict) -> bool:
    q = _q(m).lower()
    return "rounds" in q and ("o/u" in q or "over" in q or ".5" in q)


def _build_rounds(k_claim: list, p_claim: list, ctx: dict) -> FamilyResult:
    res = FamilyResult()
    kconv = {round_cutoff_convention(f"{m.get('title','')} {m.get('rules_primary','')}") for m in k_claim}
    pconv = {round_cutoff_convention(f"{_q(m)} {m.get('description','')}") for m in p_claim}
    for m in k_claim:
        res.claimed_k.add(str(m.get("ticker")))
    for m in p_claim:
        res.claimed_p.add(_pid(m))
    # Kalshi round-START boundary vs Poly half-round (2:30) mark -> incompatible; never force a node.
    reason = ("cutoff_convention_mismatch" if ("round_start" in kconv and "half_round" in pconv)
              else "one_venue_only")
    for m in k_claim:
        res.inventory.append({"venue": "kalshi", "family": "round_totals", "title": m.get("title"),
                              "reason": reason})
    for m in p_claim:
        res.inventory.append({"venue": "polymarket", "family": "round_totals", "title": _q(m),
                              "reason": reason})
    return res


# --------------------------------------------------------------------------- #
# The UFC family registry (the fight_winner family stays native in sports_ufc)    #
# --------------------------------------------------------------------------- #
def ufc_families() -> list:
    return [
        FamilySpec("go_the_distance", "2way", _k_is_gtd, _p_is_gtd, _build_gtd,
                   settlement_axes=("scorecard_draw", "technical_decision", "nc")),
        FamilySpec("method_by_kotko", "2way", _k_is_method, _p_is_method, _build_method,
                   settlement_axes=("bucket_definition",)),
        FamilySpec("round_totals", "2way", _k_is_rounds, _p_is_rounds, _build_rounds,
                   settlement_axes=("cutoff_convention",)),
    ]
