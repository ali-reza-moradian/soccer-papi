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


class LiveCaps:
    """Per-day live exposure accounting + the pre-place decision. One instance per process/day."""

    def __init__(self, live_cfg: Any, *, telegram: Any = None, log: Any = None) -> None:
        self.quote_usd_max = float(getattr(live_cfg, "quote_usd_max", 5.0))
        self.max_open_quotes = int(getattr(live_cfg, "max_open_quotes", 2))
        self.max_fills_per_day = int(getattr(live_cfg, "max_fills_per_day", 10))
        self.max_daily_loss_usd = float(getattr(live_cfg, "max_daily_loss_usd", 25.0))
        self.max_daily_stake_usd = float(getattr(live_cfg, "max_daily_stake_usd", 100.0))
        self.telegram = telegram
        self.log = log
        # running day state
        self.stake_today = 0.0            # SUM of all legs actually committed today ($)
        self.fills_today = 0
        self.pnl_today = 0.0
        self.open_quotes = 0
        self.halted = False
        self.halt_reason: Optional[str] = None

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
        """Decide whether a NEW quote may rest, given the $ its pair would commit if it fills. Refuses
        (and HALTS for the day, alerting) when the projected stake would breach ``max_daily_stake_usd``.
        Also refuses on the open-quote cap, the fills-per-day cap, or an existing halt. Returns
        (ok, reason)."""
        if self.halted:
            return False, f"halted:{self.halt_reason}"
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")
            return False, "max_daily_loss_usd"
        if self.open_quotes >= self.max_open_quotes:
            return False, "max_open_quotes"
        if self.fills_today >= self.max_fills_per_day:
            return False, "max_fills_per_day"
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
