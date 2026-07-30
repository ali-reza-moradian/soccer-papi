"""PRE-GAME live caps — the hard exposure limits enforced before/around every real order.

PURE and stateful (no I/O, no clients) so it is unit-tested directly; the continuous pre-game
executor (a separate, later enable) and the ``--smoke`` order both route their sizing + pre-place
decision through here. Every limit comes from the ``maker_rt.live`` config block:

  * ``quote_usd_max``       — the REST leg's notional cap ($). ``size_shares`` never sizes above it.
  * ``max_daily_stake_usd`` — the running SUM of ALL legs committed today (rest fills + hedges +
    unwinds). ``can_place`` does a PRE-PLACE projection: if committing this quote's projected pair
    stake would push the day's total past the cap, it REFUSES and HALTS quoting for the day (and the
    caller alerts Telegram). This bounds gross money at risk independently of realized pnl.
  * ``max_open_quotes``     — at most this many resting quotes at once.
  * ``max_fills_per_day``   — at most this many real fills/day.
  * ``max_daily_loss_usd``  — realized-loss auto-halt: once ``pnl_today <= -cap`` quoting halts.

Nothing here places an order; it only decides + accounts. Fail-CLOSED: any missing config value
falls back to the safe LiveConfig default.
"""
from __future__ import annotations

import math
from typing import Any, Optional


def direction_slot_ok(direction: str, open_by_direction: dict, enabled_directions: Any,
                      max_open: int, reserve: int) -> bool:
    """PER-DIRECTION SLOT RESERVATION. Of ``max_open`` total resting slots, guarantee ``reserve`` to EACH
    enabled direction; the remainder float. Returns True iff ``direction`` may claim ONE MORE slot WITHOUT
    eating into another enabled direction's still-unclaimed reserve.

    PURE (no I/O, no state) so it is unit-tested directly. ``open_by_direction`` maps direction -> current
    open-quote count. The GLOBAL ``max_open`` cap is enforced separately (LiveCaps.can_place); this only
    layers the fairness reservation on top.

      * ``reserve <= 0``            -> disabled -> always True (today's behavior; no reservation).
      * a single enabled direction  -> no 'other' to protect -> always True (single-direction unaffected).

    Example (max_open=2, reserve=1, dirs={rest-poly, rest-kalshi}): if rest-kalshi already holds 1 and
    rest-poly holds 0, rest-kalshi is REFUSED a 2nd (poly's 1 reserved slot is protected), while rest-poly
    is allowed its slot. Once both hold 1, the global max_open cap refuses either a 3rd."""
    if reserve <= 0:
        return True
    reserved_for_others = sum(max(0, reserve - int(open_by_direction.get(d, 0)))
                              for d in enabled_directions if d != direction)
    mine = int(open_by_direction.get(direction, 0))
    return mine < max_open - reserved_for_others


#: the SIZE-limiting constraints, in tie-break priority (caps before depth), plus the refusal marker.
BINDING_CONSTRAINTS = ("quote_usd_max", "pair_cap", "daily_stake", "hedge_depth", "book_depth")
_BINDING_ORDER = {n: i for i, n in enumerate(BINDING_CONSTRAINTS)}


def plan_size(price: float, hedge_ask: Optional[float], *, quote_usd_max: float,
              max_pair_stake_usd: float, daily_stake_headroom: float, hedge_depth: Optional[float],
              book_depth: Optional[float], venue_minimum: int) -> dict:
    """The LARGEST whole-share rest-leg size that fits EVERY constraint — the venue minimum is a FLOOR,
    NEVER a ceiling. (The old ``min(floor_min, cap)`` clamped every quote DOWN to 5 shares, so a $2.80
    fill rested against a $20 cap.) Take the smallest of the resource allowances:

      * ``quote_usd_max`` — rest-leg notional cap:            quote_usd_max / price
      * ``pair_cap``      — whole-pair stake cap:             max_pair_stake_usd / (price + hedge_ask)
      * ``daily_stake``   — remaining daily budget:           daily_stake_headroom / (price + hedge_ask)
      * ``hedge_depth``   — resting hedge shares at the ask:  hedge_depth
      * ``book_depth``    — rest-book liquidity:              book_depth

    ``pair_cap``/``daily_stake`` divide by the PAIR notional (both legs count toward those caps), so the
    resulting size can never breach them. floor() to whole shares. If that largest size is BELOW the
    venue minimum, the quote is REFUSED (``binding='below_venue_minimum'``) — we never clamp DOWN to the
    minimum. Returns {size, binding, refused, limiter}. PURE."""
    px = max(float(price), 1e-9)
    ha = max(float(hedge_ask or 0.0), 0.0)
    pair_px = px + ha if (px + ha) > 1e-9 else px
    inf = float("inf")
    allow = {
        "quote_usd_max": float(quote_usd_max) / px,
        "pair_cap": float(max_pair_stake_usd) / pair_px,
        "daily_stake": max(0.0, float(daily_stake_headroom)) / pair_px,
        "hedge_depth": float(hedge_depth) if hedge_depth is not None else inf,
        "book_depth": float(book_depth) if book_depth is not None else inf,
    }
    limiter = min(allow, key=lambda k: (allow[k], _BINDING_ORDER.get(k, 99)))
    size = int(math.floor(min(allow.values()) + 1e-9))
    vmin = int(max(1, venue_minimum))
    if size < vmin:
        # The market/caps can't even support the venue minimum -> REFUSE the quote (never clamp down).
        return {"size": 0, "binding": "below_venue_minimum", "refused": True, "limiter": limiter,
                "max_fit": size, "venue_minimum": vmin}
    return {"size": size, "binding": limiter, "refused": False, "limiter": limiter,
            "max_fit": size, "venue_minimum": vmin}


class LiveCaps:
    """Per-day live exposure accounting + the pre-place decision. One instance per process/day."""

    def __init__(self, live_cfg: Any, *, telegram: Any = None, log: Any = None) -> None:
        self.quote_usd_max = float(getattr(live_cfg, "quote_usd_max", 5.0))
        self.max_open_quotes = int(getattr(live_cfg, "max_open_quotes", 2))
        self.max_fills_per_day = int(getattr(live_cfg, "max_fills_per_day", 10))
        self.max_daily_loss_usd = float(getattr(live_cfg, "max_daily_loss_usd", 25.0))
        self.max_daily_stake_usd = float(getattr(live_cfg, "max_daily_stake_usd", 100.0))
        # PER-PAIR cap: ``quote_usd_max`` bounds the REST leg only, but a cheap rest leg's HEDGE can dwarf
        # it (TBTOR: rest leg $1.02, hedge $16.21 — 16x). This bounds the WHOLE pair (rest + worst-case
        # hedge) for ONE bet, so a single low-priced quote can't commit an outsized hedge. A breach
        # REFUSES that one quote (it's a sizing limit, not a daily breach — no day-halt).
        self.max_pair_stake_usd = float(getattr(live_cfg, "max_pair_stake_usd", 25.0))
        self.telegram = telegram
        self.log = log
        # running day state
        self.stake_today = 0.0            # SUM of all legs actually committed today ($)
        self.fills_today = 0
        self.pnl_today = 0.0
        self.open_quotes = 0
        self.halted = False
        self.halt_reason: Optional[str] = None
        self._day = ""                   # UTC day of the running counters (rolled by roll())

    #: Halts that a new UTC day must NOT clear. A DAILY-cap halt is a budget that genuinely resets at
    #: midnight; a SAFETY halt is a latched statement that something is unresolved, and unresolved
    #: things do not become resolved because the clock rolled over. This happened twice in production
    #: (TBTOR 2026-07-23, ZAYRZE 2026-07-25): a "MANUAL CHECK REQUIRED" freeze silently expired at
    #: 00:00Z and the bot resumed quoting over a naked, unverified position.
    STICKY_HALTS = ("orphan_position", "booking_quarantine")

    def roll(self, day: str) -> None:
        """Reset the per-DAY counters (stake/fills/pnl + daily-cap halt) at a new UTC day.
        ``open_quotes`` is NOT reset — a resting order survives midnight. A daily-cap halt clears with
        the new day; a STICKY_HALTS safety latch does NOT."""
        if day == self._day:
            return
        self._day = day
        self.stake_today = 0.0
        self.fills_today = 0
        self.pnl_today = 0.0
        if self.halt_reason in self.STICKY_HALTS:
            return                        # keep halted + keep the reason: only a human clears these
        self.halted = False
        self.halt_reason = None

    # -- sizing --------------------------------------------------------------
    def size_shares(self, price: float, *, min_shares: int = 5) -> int:
        """Whole-share size for a REST leg at ``price``: at least ``min_shares`` AND the venue's ~$1
        minimum, but never a notional above ``quote_usd_max``. If even ``min_shares`` would exceed the
        cap the cap wins (returns the largest whole-share count that fits, >= 1)."""
        px = max(float(price), 1e-9)
        floor_min = max(int(min_shares), math.ceil(1.0 / px))     # venue min (>= $1) and >= min_shares
        max_by_cap = int(self.quote_usd_max / px + 1e-9)          # largest whole-share count <= cap $
        if max_by_cap < 1:
            return 1                                              # price alone exceeds the cap -> 1 share
        return max(1, min(floor_min, max_by_cap))

    @staticmethod
    def projected_pair_stake(rest_price: float, rest_shares: float,
                             hedge_price: float, hedge_shares: float) -> float:
        """The $ BOTH legs of a hedged pair would commit if the rest leg fully fills and is hedged."""
        return float(rest_price) * float(rest_shares) + float(hedge_price) * float(hedge_shares)

    # -- pre-place decision --------------------------------------------------
    def can_place(self, projected_pair_stake: float) -> tuple[bool, str]:
        """Decide whether a NEW quote may rest, given the $ its pair (rest + worst-case hedge) would
        commit if it fills. Refuses (and HALTS for the day, alerting) when the projected stake would
        breach ``max_daily_stake_usd``; refuses this ONE quote (no day-halt) when the pair alone breaches
        ``max_pair_stake_usd``. Also refuses on the open-quote cap, the fills-per-day cap, or an existing
        halt. Returns (ok, reason)."""
        if self.halted:
            return False, f"halted:{self.halt_reason}"
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")
            return False, "max_daily_loss_usd"
        if self.open_quotes >= self.max_open_quotes:
            return False, "max_open_quotes"
        if self.fills_today >= self.max_fills_per_day:
            return False, "max_fills_per_day"
        # PER-PAIR sizing cap FIRST (a single oversized pair is refused, not day-halted).
        if float(projected_pair_stake) > self.max_pair_stake_usd + 1e-9:
            return False, "max_pair_stake_usd"
        if self.stake_today + float(projected_pair_stake) > self.max_daily_stake_usd + 1e-9:
            self._halt("max_daily_stake_usd")
            return False, "max_daily_stake_usd"
        return True, "ok"

    # -- accounting (called on real venue events) ----------------------------
    def commit_stake(self, usd: float) -> None:
        """Add REAL committed notional from a leg (a rest fill, a hedge lift, or an unwind sell)."""
        self.stake_today += float(usd)

    def on_open(self) -> None:
        self.open_quotes += 1

    def on_close(self) -> None:
        self.open_quotes = max(0, self.open_quotes - 1)

    #: The plausible-pnl bound is derived from what a hedged pair CAN produce, not from its stake: a
    #: pair's pnl is ``locked_net x shares``, ``locked_net`` is capped by the sanity ceiling, and the
    #: stake is capped by ``max_pair_stake_usd`` — so |pnl| <= ceiling x pair_cap, times headroom for
    #: fees, rounding and an over-fill. Bounding by STAKE alone is far too loose to be useful: 2x the
    #: $350 pair cap is $700, and the Fortaleza fiction was $320.
    FILL_PNL_SANITY_EDGE = 0.05      # mirrors max_plausible_edge_pct (the ceiling that gates quoting)
    FILL_PNL_SANITY_MULT = 3.0       # headroom
    FILL_PNL_SANITY_FLOOR = 5.0      # never bound tighter than this, whatever the pair cap is

    def fill_pnl_bound(self, locked: bool = True) -> float:
        """The largest |pnl| a single fill can plausibly book.

        TWO regimes, because the two outcomes are bounded by different things:
          * ``locked=True``  — a HEDGED pair. Its pnl is ``locked_net x shares`` and ``locked_net`` is
            capped by the sanity ceiling, so the bound is ceiling x pair-cap. This is the tight one,
            and the one the Fortaleza fiction had to pass.
          * ``locked=False`` — a realized UNWIND / flatten / orphan mark. That is a position outcome,
            not an edge, so it is bounded by the money committed rather than by the ceiling. Applying
            the tight bound here would refuse honest losses (a naked leg can lose its whole stake)."""
        if not locked:
            return max(self.FILL_PNL_SANITY_FLOOR, 2.0 * self.max_pair_stake_usd)
        return max(self.FILL_PNL_SANITY_FLOOR,
                   self.FILL_PNL_SANITY_MULT * self.FILL_PNL_SANITY_EDGE * self.max_pair_stake_usd)

    def implausible_fill_pnl(self, pnl: float, locked: bool = True) -> bool:
        """Is this single-fill pnl outside anything the strategy could have produced?

        ``on_fill`` is the ONLY feeder of the daily-loss rail, and it accepted any number at all. On
        2026-07-29 a +$320.05 booking on a $14.12 rest leg sailed straight through — minutes after the
        SAME subsystem rejected a 12.6% quote-time edge as a probable bug — and re-based ``pnl_today``
        so far positive that the -$50 rail would have needed a REAL -$380 to trip. A rail fed by an
        unbounded number is not a rail."""
        return abs(float(pnl)) > self.fill_pnl_bound(locked) + 1e-9

    def on_fill(self, pnl: float = 0.0, *, locked: bool = True) -> None:
        """Count a fill and move the daily pnl. An IMPLAUSIBLE pnl is booked as ZERO and halts for
        review rather than re-basing the loss rail in either direction (a phantom gain disables it; a
        phantom loss false-halts the day). ``locked`` selects which bound applies — see
        ``fill_pnl_bound``."""
        self.fills_today += 1
        if self.implausible_fill_pnl(pnl, locked):
            self._halt("implausible_fill_pnl")
            return
        self.pnl_today += float(pnl)
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    def on_loss(self, usd: float) -> None:
        """Record a realized loss (positive ``usd`` = $ lost), e.g. an unwind cost."""
        self.pnl_today -= float(usd)
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    def adjust_pnl(self, usd: float) -> None:
        """Restate today's pnl WITHOUT counting a fill (signed: + improves the day).

        For a correction to an ALREADY-counted trade — a provisional worst-case mark rebooked at what the
        venue actually paid. Routing that through ``on_fill`` would spend one of the day's scarce
        max_fills_per_day slots on a bookkeeping entry: the CSKA restatement pushed fills_today 7 -> 8
        without a single new order being placed. The daily-loss rail is still re-checked, because a
        correction can move the day in either direction."""
        self.pnl_today += float(usd)
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    # -- halt ----------------------------------------------------------------
    def _halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        # LOG the technical reason; send a PLAIN, self-explanatory STOPPED alert to Telegram.
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] AUTO-HALT (%s): stake_today=$%.2f pnl_today=$%.2f — "
                             "quoting stopped for the day.", reason, self.stake_today, self.pnl_today)
        human = {
            "max_daily_loss_usd": (f"I hit today's loss limit (down ${abs(self.pnl_today):.2f}) — I've "
                                   f"stopped placing new bets for the day."),
            "max_daily_stake_usd": (f"I've used today's full budget (${self.stake_today:.2f} of "
                                    f"${self.max_daily_stake_usd:.0f}) — no new bets today."),
        }.get(reason, "I've stopped placing new bets for the day (a daily safety limit was reached).")
        if self.telegram:
            try:
                self.telegram(f"🔴 STOPPED · {human} Anything already placed stays fully hedged; "
                              f"I resume automatically tomorrow.")
            except Exception as exc:  # noqa: BLE001 — a telegram failure never blocks the halt
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] telegram halt-alert failed: %s", exc)
