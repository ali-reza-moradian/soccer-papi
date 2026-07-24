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


#: the constraint names the binding-constraint diagnostic can emit (the panel/digest keys).
BINDING_CONSTRAINTS = ("quote_usd_max", "pair_cap", "hedge_depth", "book_depth", "venue_minimum")


def binding_constraint(price: float, *, quote_usd_max: float, max_pair_stake_usd: float,
                       hedge_ask: Optional[float], hedge_depth: Optional[float],
                       book_depth: Optional[float], min_floor: int) -> tuple[str, float]:
    """WHAT actually limits the rest-leg SIZE, so we can tell whether raising caps would grow fills or
    whether depth (or the pilot minimum) is the ceiling.

    The maker rests the pilot MINIMUM (``min_floor`` shares) by design — ``LiveCaps.size_shares`` never
    scales up to the notional cap. So compute the largest whole-share count each resource would allow
    and take the smallest:

      * ``quote_usd_max`` — the rest-leg notional cap:            floor(quote_usd_max / price)
      * ``pair_cap``      — the whole-pair stake cap:             floor(max_pair_stake_usd /(price+hedge))
      * ``hedge_depth``   — resting hedge shares available:       floor(hedge_depth)
      * ``book_depth``    — rest-book liquidity that could fill:  floor(book_depth)

    If EVERY resource would allow >= ``min_floor``, the size is set by the pilot minimum -> return
    ``('venue_minimum', min_floor)`` (raising caps/finding more depth would NOT grow the fill). Otherwise
    return the single smallest resource and its share ceiling (raising THAT one would help). PURE."""
    px = max(float(price), 1e-9)
    allow: dict = {"quote_usd_max": int(float(quote_usd_max) / px + 1e-9)}
    if hedge_ask:
        denom = px + float(hedge_ask)
        if denom > 1e-9:
            allow["pair_cap"] = int(float(max_pair_stake_usd) / denom + 1e-9)
    if hedge_depth is not None:
        allow["hedge_depth"] = int(float(hedge_depth) + 1e-9)
    if book_depth is not None:
        allow["book_depth"] = int(float(book_depth) + 1e-9)
    # Smallest allowance wins; ties break by the BINDING_CONSTRAINTS order (caps before depth).
    order = {n: i for i, n in enumerate(BINDING_CONSTRAINTS)}
    name, ceiling = min(allow.items(), key=lambda kv: (kv[1], order.get(kv[0], 99)))
    if ceiling >= int(min_floor):
        return "venue_minimum", float(min_floor)
    return name, float(ceiling)


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

    def roll(self, day: str) -> None:
        """Reset the per-DAY counters (stake/fills/pnl + halt) at a new UTC day. ``open_quotes`` is NOT
        reset — a resting order survives midnight. A daily-cap halt clears with the new day."""
        if day == self._day:
            return
        self._day = day
        self.stake_today = 0.0
        self.fills_today = 0
        self.pnl_today = 0.0
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

    def on_fill(self, pnl: float = 0.0) -> None:
        self.fills_today += 1
        self.pnl_today += float(pnl)
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    def on_loss(self, usd: float) -> None:
        """Record a realized loss (positive ``usd`` = $ lost), e.g. an unwind cost."""
        self.pnl_today -= float(usd)
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    # -- halt ----------------------------------------------------------------
    def _halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        msg = (f"[MAKER_RT][LIVE] AUTO-HALT ({reason}): stake_today=${self.stake_today:.2f} "
               f"pnl_today=${self.pnl_today:.2f} -- pre-game quoting stopped for the day.")
        if self.log:
            self.log.warning(msg)
        if self.telegram:
            try:
                self.telegram(msg)
            except Exception as exc:  # noqa: BLE001 — a telegram failure never blocks the halt
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] telegram halt-alert failed: %s", exc)
