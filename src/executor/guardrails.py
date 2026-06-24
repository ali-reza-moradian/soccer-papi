"""Guardrails (Phase 4): every cap enforced BEFORE each live placement.

A single :meth:`Guardrails.pre_trade_check` runs all the config gates and returns a decision. A
blocked decision -> log + skip. A ``halt`` decision (daily-loss / consecutive-error breach) -> the
caller trips the STOP file so no further cycle runs. Dedupe is by the structural arb fingerprint.
Only LIVE fills feed the daily spend/loss counters (read from the ledger).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from . import config as exec_config
from .fees_sizing import depth_at_or_below


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    halt: bool = False            # True -> caller should trip the STOP file


class Guardrails:
    def __init__(self, cfg: exec_config.ExecConfig, *, ledger: Any = None,
                 stop_path: str | None = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.stop_path = stop_path or exec_config.STOP_FILE
        self._last_fire_ts: float = 0.0
        self._fired: dict[str, float] = {}     # fingerprint -> last fire monotonic-ish ts
        self._consec_errors: int = 0

    # -- error / success bookkeeping ----------------------------------------
    def record_error(self) -> GuardDecision:
        """Count a consecutive adapter error; halt when the cap is hit."""
        self._consec_errors += 1
        if self._consec_errors >= self.cfg.max_consecutive_errors:
            return GuardDecision(False, f"max_consecutive_errors={self.cfg.max_consecutive_errors} reached", halt=True)
        return GuardDecision(True, f"error {self._consec_errors}/{self.cfg.max_consecutive_errors}")

    def record_success(self) -> None:
        self._consec_errors = 0

    def mark_fired(self, fingerprint: str, *, now: Optional[float] = None) -> None:
        """Record that a placement was fired for ``fingerprint`` (drives dedupe + cooldown)."""
        t = now if now is not None else time.time()
        self._last_fire_ts = t
        if fingerprint:
            self._fired[fingerprint] = t

    # -- the gate ------------------------------------------------------------
    def pre_trade_check(self, narb: Any, edge: Any, kalshi_ladder: list[tuple[float, float]],
                        poly_ladder: list[tuple[float, float]], size: float, *,
                        now: Optional[float] = None) -> GuardDecision:
        """2-leg convenience entry (kept for the existing call sites/tests). Delegates to the N-leg
        check with [(kalshi leg, its ladder), (poly leg, its ladder)]."""
        leg_ladders = [(narb.kalshi, kalshi_ladder), (narb.poly, poly_ladder)]
        return self.pre_trade_check_n(narb, edge, leg_ladders, size, now=now)

    def pre_trade_check_n(self, narb: Any, edge: Any,
                          leg_ladders: list[tuple[Any, list[tuple[float, float]]]], size: float, *,
                          now: Optional[float] = None) -> GuardDecision:
        """Run ALL guardrails in priority order over N legs. First failure wins.

        ``leg_ladders`` is one (VenueLeg, live ask ladder) per leg. min-liquidity must hold on EVERY
        leg; the per-trade cap and net-edge are over the total of all N legs (in ``edge``)."""
        t = now if now is not None else time.time()
        cfg = self.cfg

        # 0) hard kill-switch.
        if exec_config.stop_file_present(self.stop_path):
            return GuardDecision(False, "STOP file present", halt=True)

        # 1) consecutive-error breach (set by record_error between cycles).
        if self._consec_errors >= cfg.max_consecutive_errors:
            return GuardDecision(False, f"consecutive errors >= {cfg.max_consecutive_errors}", halt=True)

        # 2) dedupe — same structural arb within the window.
        fp = getattr(narb, "fingerprint", "") or ""
        if fp and fp in self._fired and (t - self._fired[fp]) < cfg.dedupe_minutes * 60.0:
            return GuardDecision(False, f"dedupe: fingerprint fired within {cfg.dedupe_minutes}m")

        # 3) cooldown between any two placements.
        if self._last_fire_ts and (t - self._last_fire_ts) < cfg.cooldown_seconds:
            return GuardDecision(False, f"cooldown: < {cfg.cooldown_seconds}s since last placement")

        # 4) per-trade notional cap = total capital across ALL N legs.
        if edge.total_cost > cfg.max_per_trade_usd + 1e-9:
            return GuardDecision(False,
                                 f"per-trade cap: ${edge.total_cost:.2f} > ${cfg.max_per_trade_usd:.2f}")

        # 5) daily caps (LIVE only, from the ledger).
        if self.ledger is not None:
            c = self.ledger.today_live_counters()
            if c["trades"] >= cfg.max_trades_per_day:
                return GuardDecision(False, f"max_trades_per_day={cfg.max_trades_per_day} reached")
            if c["spend_usd"] + edge.total_cost > cfg.max_daily_spend_usd + 1e-9:
                return GuardDecision(False,
                                     f"daily spend cap: ${c['spend_usd']:.2f}+${edge.total_cost:.2f} "
                                     f"> ${cfg.max_daily_spend_usd:.2f}")
            if c["loss_usd"] >= cfg.max_daily_loss_usd:
                return GuardDecision(False,
                                     f"daily loss cap hit: ${c['loss_usd']:.2f} >= ${cfg.max_daily_loss_usd:.2f}",
                                     halt=True)

        # 6) min book liquidity: EVERY leg's book shows >= mult x size at its target (quote) price.
        need = cfg.min_book_liquidity_mult * size
        for leg, ladder in leg_ladders:
            if leg is None:
                continue
            depth = depth_at_or_below(ladder, leg.detected_price)
            if depth < need - 1e-9:
                short = "poly" if leg.venue == "polymarket" else leg.venue
                return GuardDecision(
                    False,
                    f"{short} liquidity {depth:.1f} < {cfg.min_book_liquidity_mult}x size ({need:.1f})")

        # 7) modeled net edge must clear the floor after fees + slippage (all N legs).
        if edge.net_edge_pct < cfg.min_net_edge_pct_after_costs - 1e-9:
            return GuardDecision(False,
                                 f"net edge {edge.net_edge_pct:.2f}% < floor {cfg.min_net_edge_pct_after_costs:.2f}%")

        return GuardDecision(True, "ok")
