"""Arbitrage math: best-odds selection (clone-aware), stake sizing with limits, ROI, T_max.

All math is in DECIMAL odds. For a MECE market with outcomes i = 1..n:

    p_i = 1 / o_i_eff            (o_i_eff = effective odds after exchange commission)
    S   = sum(p_i)
    arb exists  iff  S < 1
    ROI         = (1/S) - 1
    stake_i     = T * p_i / S            (equalises payouts: every leg returns T/S)
    T_max       = min_i ( L_i * o_i_eff * S )     (binding leg uses its full limit)
    max_profit  = T_max * ((1/S) - 1)

Commission on net winnings: o_eff = 1 + (o - 1) * (1 - c).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class Candidate:
    """One book's price for one outcome, after universe/staleness filtering."""

    outcome_id: int
    outcome_name: str
    book: str
    clone_group: str
    decimal_odds: float
    american_odds: Optional[str] = None
    limit: Optional[float] = None        # known max stake / liquidity; None = unknown
    changed_at: Optional[str] = None
    main_line: bool = False
    is_exchange: bool = False
    commission: float = 0.0

    @property
    def eff_odds(self) -> float:
        """Effective decimal odds after commission on net winnings."""
        return 1.0 + (self.decimal_odds - 1.0) * (1.0 - self.commission)


@dataclass
class Leg:
    outcome_id: int
    outcome_name: str
    book: str
    decimal_odds: float
    eff_odds: float
    american_odds: Optional[str]
    limit: Optional[float]
    changed_at: Optional[str]
    is_exchange: bool
    commission: float
    stake: float = 0.0          # stake at T_max (filled by compute_arb)


@dataclass
class ArbResult:
    legs: list[Leg]
    arb_sum_S: float
    roi_decimal: float
    t_max: float
    max_profit: float
    binding_book: str
    min_leg_limit: float
    involves_exchange: bool
    low_confidence: bool

    @property
    def is_arb(self) -> bool:
        return self.arb_sum_S < 1.0

    @property
    def roi_pct(self) -> float:
        return self.roi_decimal * 100.0


# --------------------------------------------------------------------------- #
# Leg selection (clone-aware)                                                   #
# --------------------------------------------------------------------------- #
def _better_candidate(a: Candidate, b: Candidate) -> bool:
    """True if `a` should be preferred over `b` as a clone-group representative."""
    if a.eff_odds != b.eff_odds:
        return a.eff_odds > b.eff_odds
    la, lb = (a.limit or 0.0), (b.limit or 0.0)
    if la != lb:
        return la > lb
    return False


def _reps_per_clone_group(cands: list[Candidate]) -> list[Candidate]:
    """Collapse candidates so each clone group contributes at most one (its best)."""
    best: dict[str, Candidate] = {}
    for c in cands:
        cur = best.get(c.clone_group)
        if cur is None or _better_candidate(c, cur):
            best[c.clone_group] = c
    return list(best.values())


def select_legs(outcomes: dict[int, list[Candidate]]) -> Optional[list[Candidate]]:
    """Pick the best-priced Candidate for each outcome (clone-aware), MINIMISING S.

    The SAME account may back more than one leg of an arb: e.g. place the Home win on
    Polymarket and BOTH the Draw and the Away win on 1xBet. Each outcome is therefore
    chosen independently — best effective odds, then highest limit — with no requirement
    that legs come from distinct books. (Clones of one book are still collapsed per
    outcome so a single outcome is never priced from the same liquidity pool twice.)

    Because the legs are independent, picking the best price for every outcome yields the
    global minimum S directly. Returns the chosen candidates (even if S >= 1 — the caller
    decides), or None if any outcome has no eligible candidate.
    """
    if not outcomes:
        return None

    chosen: list[Candidate] = []
    for cands in outcomes.values():
        reps = _reps_per_clone_group(cands)
        if not reps:
            return None
        best = reps[0]
        for c in reps[1:]:
            if _better_candidate(c, best):
                best = c
        chosen.append(best)
    return chosen


# --------------------------------------------------------------------------- #
# Arb computation                                                              #
# --------------------------------------------------------------------------- #
def compute_arb(candidates: list[Candidate], unknown_limit_fallback: float = 100.0,
                low_confidence_limit_floor: float = 10.0) -> ArbResult:
    """Compute the full arbitrage result for one chosen leg per outcome.

    ``low_confidence_limit_floor`` marks the arb low_confidence (but never discards it) when any
    leg's known limit is below this dollar amount — common on thin Polymarket/Kalshi books where
    a tiny or unknown limit means the human, not the bot, should judge whether to take it.
    """
    if not candidates:
        raise ValueError("compute_arb requires at least one candidate")

    S = sum(1.0 / c.eff_odds for c in candidates)
    roi = (1.0 / S) - 1.0

    known = [c for c in candidates if c.limit and c.limit > 0]
    # Null/non-positive limit, or a known-but-tiny limit (< floor), all => low confidence.
    low_confidence = any((c.limit is None or c.limit < low_confidence_limit_floor) for c in candidates)

    if known:
        # T_max is the tightest L_i * o_i_eff * S across legs with a known limit.
        binding = min(known, key=lambda c: c.limit * c.eff_odds * S)
        t_max = binding.limit * binding.eff_odds * S
        binding_book = binding.book
        min_leg_limit = min(c.limit for c in known)
    else:
        t_max = float(unknown_limit_fallback)
        binding_book = candidates[0].book
        min_leg_limit = 0.0
        low_confidence = True

    legs: list[Leg] = []
    for c in candidates:
        stake = t_max * (1.0 / c.eff_odds) / S
        legs.append(
            Leg(
                outcome_id=c.outcome_id,
                outcome_name=c.outcome_name,
                book=c.book,
                decimal_odds=c.decimal_odds,
                eff_odds=c.eff_odds,
                american_odds=c.american_odds,
                limit=c.limit,
                changed_at=c.changed_at,
                is_exchange=c.is_exchange,
                commission=c.commission,
                stake=round(stake, 2),
            )
        )

    return ArbResult(
        legs=legs,
        arb_sum_S=S,
        roi_decimal=roi,
        t_max=t_max,
        max_profit=t_max * roi,
        binding_book=binding_book,
        min_leg_limit=min_leg_limit,
        involves_exchange=any(c.is_exchange for c in candidates),
        low_confidence=low_confidence,
    )


def make_signature(fixture_id: str, market_id: int, line: Optional[float], legs: Iterable[Leg]) -> str:
    """Stable hash of fixture + market + line + sorted (book, rounded odds) pairs."""
    pairs = sorted((leg.book, round(leg.decimal_odds, 2)) for leg in legs)
    line_str = "" if line is None else f"{line:g}"
    raw = f"{fixture_id}|{market_id}|{line_str}|" + "|".join(f"{b}@{o}" for b, o in pairs)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
