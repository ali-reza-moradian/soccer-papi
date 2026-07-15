"""Honest walk-to-stake sizing for OG arbs — the same principle as the GenZ fill curves.

The scanner's `max_profit = roi_decimal x T_max` uses the TOP-of-book price of every leg and assumes it
holds for the whole stake. On a thin exchange leg that is a lie: buying $1,142 of France at a 2.65 top
actually fills at ~2.567 avg, so the payout is < the total staked and the "arb" is a loss.

This module re-sizes each arb HONESTLY: walk each EXCHANGE leg (Polymarket, Kalshi) through its real ask
book level-by-level; fixed-odds books (Pinnacle, 1xbet, …) stay at their flat price, capped by their
limit. We grow an equal-payout target `R` (each leg secures R payout) and stop at the MARGINAL boundary
where securing one more unit of payout costs >= $1 across the legs (walked S >= 1) — beyond there is no
edge. Per leg we report the top odds, the AVG-FILL odds at the recommended stake, the stake and payout;
profit = min(leg payout) - total stake.

Commission note: the arb's existence (S < 1) is already gated on commission-adjusted odds upstream; this
layer models book DEPTH (the fill you'd actually get), so it works in raw book prices and does not
re-apply Kalshi's small taker fee per share. Pure — no network; ladders are fetched by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import bookmath

_INF = float("inf")


@dataclass
class SizedLeg:
    outcome: str
    book: str
    top_odds: float                  # decimal odds at the top of book (what the scanner displayed)
    avg_fill_odds: float             # decimal odds actually realized at `stake` (<= top on a walked leg)
    stake: float                     # dollars staked on this leg
    payout: float                    # dollars returned if this leg's outcome wins


@dataclass
class HonestSize:
    total_stake: float               # recommended TOTAL stake = min(bankroll cap, honest boundary)
    t_max_honest: float              # the honest boundary total stake (before the bankroll cap)
    profit: float                    # min(leg payout) - total_stake (the GUARANTEED walked profit)
    legs: list[SizedLeg]


def _prep(legs: list[dict[str, Any]]):
    """Split legs into walked EXCHANGE books and flat, limit-capped FIXED books. Returns (ex, fx) or
    None if an exchange leg advertised a book but it has no usable depth (can't size honestly)."""
    ex: list[dict[str, Any]] = []
    fx: list[dict[str, Any]] = []
    for lg in legs:
        ladder = lg.get("ladder")
        if ladder:
            asks = bookmath.valid_asks(ladder)          # ascending, validated (price in (0,1), size>0)
            if not asks:
                return None
            cum, c = [], 0.0
            for p, s in asks:
                c += s
                cum.append((p, c))                      # (price, cumulative shares through this level)
            ex.append({"leg": lg, "asks": asks, "cum": cum, "depth": c, "fee_book": lg.get("fee_book")})
        else:
            d = float(lg["top_odds"])
            if d <= 1.0:
                return None
            lim = lg.get("limit")
            maxR = (float(lim) * d) if (lim and float(lim) > 0) else _INF   # max payout a fixed leg supplies
            fx.append({"leg": lg, "odds": d, "maxR": maxR})
    return ex, fx


def _exch_fee_rate(fee_book: Optional[str], price: float, poly_rate: float) -> float:
    """Per-share taker fee for a walked exchange leg: Kalshi 0.07·p·(1−p); Polymarket poly_rate·min(p,1−p)."""
    if fee_book == "kalshi":
        return 0.07 * price * (1.0 - price)
    if fee_book == "polymarket":
        return poly_rate * min(price, 1.0 - price)
    return 0.0


def _walk_fee(e, R: float, poly_rate: float) -> float:
    """The exact taker fee ($) for buying R shares on one exchange leg — fee_rate(level price)·shares
    summed across the consumed ladder levels."""
    fee, rem = 0.0, R
    for p, s in e["asks"]:
        take = min(rem, s)
        if take <= 0:
            break
        fee += _exch_fee_rate(e["fee_book"], p, poly_rate) * take
        rem -= take
    return fee


def _total_cost(ex, fx, R: float) -> float:
    return (sum(bookmath.walk_book(e["asks"], R).cost for e in ex)
            + sum(R / f["odds"] for f in fx))


def _size_at(ex, fx, R: float, poly_rate: float):
    """Per-leg fills at equal-payout target R. Returns (sized, total_stake, min_payout, fee_total)."""
    sized: list[SizedLeg] = []
    total = fee_total = 0.0
    min_pay = _INF
    for e in ex:
        w = bookmath.walk_book(e["asks"], R)
        cost, pay = w.cost, w.filled                    # each share pays $1 -> payout == shares filled
        total += cost
        fee_total += _walk_fee(e, R, poly_rate)
        min_pay = min(min_pay, pay)
        sized.append(SizedLeg(e["leg"]["outcome"], e["leg"]["book"], float(e["leg"]["top_odds"]),
                              (pay / cost if cost > 0 else 0.0), cost, pay))
    for f in fx:
        d = f["odds"]
        stake = R / d
        total += stake
        min_pay = min(min_pay, R)
        sized.append(SizedLeg(f["leg"]["outcome"], f["leg"]["book"], d, d, stake, R))
    return sized, total, min_pay, fee_total


def _solve_R_for_total(ex, fx, target: float, hi: float) -> float:
    """The R whose total stake == target (total cost is monotone in R). Bisection on [0, hi]."""
    lo = 0.0
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if _total_cost(ex, fx, mid) < target:
            lo = mid
        else:
            hi = mid
    return lo


def honest_size(legs: list[dict[str, Any]], *, bankroll_cap: float = 0.0,
                poly_fee_rate: float = 0.05) -> Optional[HonestSize]:
    """Honestly size one arb, NET of the exact exchange taker fees. ``legs`` = per-leg dicts with
    ``outcome``, ``book``, ``top_odds`` and EITHER ``ladder`` (ascending (price, size) ask book ->
    walked EXCHANGE leg; set ``fee_book`` = 'kalshi'/'polymarket' for its exact per-share fee) OR
    ``limit`` (flat FIXED leg; ``top_odds`` should already be commission-adjusted). Returns a HonestSize,
    or None when not even the first unit of payout is profitable at the margin NET of fees."""
    prep = _prep(legs)
    if prep is None:
        return None
    ex, fx = prep
    fixed_S = sum(1.0 / f["odds"] for f in fx)          # constant marginal S contribution of fixed legs

    def exch_marginal(e, R: float) -> Optional[float]:  # marginal book price at cumulative R shares
        for p, end in e["cum"]:
            if R < end - 1e-9:
                return p
        return None                                     # walked past the leg's depth

    caps = [e["depth"] for e in ex] + [f["maxR"] for f in fx]
    cap_R = min(caps) if caps else 0.0                  # bounded by the thinnest depth / tightest limit
    if cap_R <= 0 or cap_R == _INF:
        return None
    breakpoints = sorted({end for e in ex for (_, end) in e["cum"]}
                         | {f["maxR"] for f in fx if f["maxR"] != _INF})
    R_star, prev = 0.0, 0.0
    for bp in breakpoints:
        bp = min(bp, cap_R)
        if bp <= prev + 1e-9:
            continue
        mid = 0.5 * (prev + bp)
        margs = []
        for e in ex:                                    # marginal price PLUS the leg's per-share fee
            p = exch_marginal(e, mid)
            if p is None:                               # an exchange leg exhausted mid-segment
                margs = None
                break
            margs.append(p + _exch_fee_rate(e["fee_book"], p, poly_fee_rate))
        if margs is None:
            break
        if fixed_S + sum(margs) < 1.0:                  # marginal S still < 1 NET of fees -> segment pays
            R_star, prev = bp, bp
        else:
            break                                       # marginal boundary reached
        if bp >= cap_R - 1e-9:
            break
    if R_star <= 0:
        return None

    _, boundary_total, _, _ = _size_at(ex, fx, R_star, poly_fee_rate)
    R_rec, recommended_total = R_star, boundary_total
    if bankroll_cap and 0.0 < bankroll_cap < boundary_total:
        R_rec = _solve_R_for_total(ex, fx, bankroll_cap, R_star)
        recommended_total = bankroll_cap
    sized, total, min_pay, fee_total = _size_at(ex, fx, R_rec, poly_fee_rate)
    return HonestSize(total_stake=round(total, 2), t_max_honest=round(boundary_total, 2),
                      profit=round(min_pay - total - fee_total, 2),   # NET of exchange taker fees
                      legs=[SizedLeg(l.outcome, l.book, round(l.top_odds, 4), round(l.avg_fill_odds, 4),
                                     round(l.stake, 2), round(l.payout, 2)) for l in sized])
