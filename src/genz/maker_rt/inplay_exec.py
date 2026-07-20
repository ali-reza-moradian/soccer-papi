"""IN-PLAY LIVE EXECUTOR — built + unit-tested, LOCKED behind its OWN gate.

An in-play live action requires the IN-PLAY gate armed (``maker_rt.live_inplay.enabled`` AND the
``data/ops/ARM_MAKER_INPLAY`` file AND the startup self-check) — fully independent of the pre-game
gate. In this build that gate never opens (``armed()`` is False), so nothing here places an order; the
rules below are exercised only by the unit tests with fakes, and the driver's every call into this
module is a no-op while locked.

Rails STRICTER than the shadow in-play collector:
  * cool-off — a live quote arms only once the node has been unfrozen AND both books fresh for
    >= ``freeze_cooloff_s`` (``cooloff_ok``);
  * hedge RE-VERIFY at the fill — BEFORE firing, re-walk the live hedge book; if the walked locked-net
    would fall below ``hedge_decline_floor`` (-1.0%), DECLINE the hedge and immediately market-unwind
    the poly fill ('hedge_declined') rather than leg into a bad hedge;
  * ONE in-flight fill globally, SHARED with the pre-game path via an injected :class:`InFlightGuard`;
  * caps — ``quote_usd_max`` (sizing), ``max_open_quotes``, ``max_fills_per_day``;
  * first-negative-day AUTO-HALT — once ``pnl_today <= -max_daily_loss_usd``, stop quoting for the day.
Every live in-play action is written to the events CSV (event ``inplay_live``) AND alerted to Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import hedge as hedge_mod
from .live import assert_live_allowed


class InFlightGuard:
    """A single global one-in-flight token, SHARED between the pre-game and in-play live paths so at most
    ONE fill is ever being hedged at a time across BOTH. Not thread-safe by design — the maker runs one
    asyncio loop; acquisition is a plain check-and-set between awaits."""

    def __init__(self) -> None:
        self._holder: Any = None

    def acquire(self, who: Any) -> bool:
        if self._holder is not None:
            return False
        self._holder = who
        return True

    def release(self, who: Any = None) -> None:
        self._holder = None

    @property
    def busy(self) -> bool:
        return self._holder is not None


@dataclass
class InplayExecResult:
    action: str                          # 'hedged'|'hedge_declined'|'unwound'|'partial_unwound'|'error'|
    detail: dict = field(default_factory=dict)   # 'refused_inflight'|'refused_cap'|'refused_halt'


class InplayLiveExecutor:
    """Owns a real in-play maker fill end-to-end once the in-play gate is armed. All order clients live
    on the injected :class:`~src.genz.maker_rt.hedge.LiveHedger`; this class never builds one, so an
    unarmed process can never place an order."""

    def __init__(self, cfg: Any, gate: Any, hedger: Any, *, in_flight: Optional[InFlightGuard] = None,
                 telegram: Any = None, state: Any = None, log: Any = None) -> None:
        self.cfg = cfg
        self.li = getattr(cfg, "live_inplay", None)
        self.gate = gate
        self.hedger = hedger
        self.in_flight = in_flight or InFlightGuard()
        self.telegram = telegram
        self.state = state
        self.log = log
        self.fills_today = 0
        self.pnl_today = 0.0
        self.halted = False

    # -- gate --------------------------------------------------------------
    def armed(self) -> bool:
        """True ONLY when the in-play gate is armed (enabled + ARM_MAKER_INPLAY + self-check). Any error
        -> False (fail-closed). This is what every driver hook checks, so a locked build is a total
        no-op."""
        try:
            return bool(self.gate.evaluate_inplay().armed)
        except Exception:  # noqa: BLE001 — a gate probe error is a refusal, never a crash
            return False

    # -- cool-off rail -----------------------------------------------------
    def cooloff_ok(self, store: Any, c: Any, freeze_until_ts: float, now_ts: float) -> bool:
        """True when the node is NOT frozen now, it has been >= ``freeze_cooloff_s`` since the last freeze
        thawed, AND both books are fresh. Stricter than the shadow rail (which quotes the instant a book
        is fresh again) — a live quote waits out the cool-off after any shock."""
        cool = float(getattr(self.li, "freeze_cooloff_s", 10.0))
        fresh_s = float(getattr(getattr(self.cfg, "inplay", None), "fresh_s", 10.0))
        if now_ts < float(freeze_until_ts or 0.0):                        # currently frozen
            return False
        if freeze_until_ts and (now_ts - float(freeze_until_ts)) < cool:  # thawed < cool-off ago
            return False
        return bool(store.is_fresh(c.rest_id, now_ts, fresh_s)
                    and store.is_fresh(c.hedge_id, now_ts, fresh_s))

    # -- fill handling -----------------------------------------------------
    def on_fill(self, fe: Any, ctx: dict, store: Any, now: Any, now_ts: float, *,
                hedge_view: Any = None, mark: Any = None) -> InplayExecResult:
        """Handle a real in-play maker fill: re-verify the hedge on the LIVE book, then fire it or
        decline+unwind. Enforces the daily auto-halt, the fill cap, and one-in-flight (shared). Records
        to CSV + Telegram. Never raises (a failure is logged + returned)."""
        phase = ctx.get("phase", "inplay")
        # HARD LOCK re-checked right before any order (belt-and-suspenders over the driver's armed()).
        assert_live_allowed(phase, self.gate.may_place(phase))

        if self.halted or self.pnl_today <= -float(self.li.max_daily_loss_usd):
            return self._refuse("refused_halt", fe, ctx, now, {"pnl_today": round(self.pnl_today, 4)})
        if self.fills_today >= int(self.li.max_fills_per_day):
            return self._refuse("refused_cap", fe, ctx, now, {"fills_today": self.fills_today})
        if not self.in_flight.acquire(("inplay", fe.key)):
            return self._refuse("refused_inflight", fe, ctx, now, {})
        try:
            hv = hedge_view
            hedge_venue = ctx.get("hedge_venue", "kalshi")
            poly_rate = ctx.get("poly_rate", getattr(self.cfg, "poly_fee_rate", 0.05))
            # RE-VERIFY: re-walk the live hedge book for our size, fee-inclusive.
            re_mark = hedge_mod.mark_hedge(hv.ask_ladder, fe.size, hedge_venue, poly_rate) if hv else None
            locked = hedge_mod.locked_net(fe.quote_price, re_mark["cost_per_share"]) if re_mark else None
            floor = float(getattr(self.li, "hedge_decline_floor", -0.010))
            if locked is None or locked < floor:
                return self._decline_and_unwind(fe, ctx, now, locked, floor)
            res = self.hedger.hedge(self._fill_dict(fe), self._hedge_spec(ctx.get("lookup", {}), hv, hedge_venue))
            return self._record_hedge(fe, ctx, now, res, locked)
        finally:
            self.in_flight.release(("inplay", fe.key))

    # -- decline / hedge outcomes -----------------------------------------
    def _decline_and_unwind(self, fe: Any, ctx: dict, now: Any, locked: Optional[float],
                            floor: float) -> InplayExecResult:
        """The walked hedge is too dear (< floor) -> do NOT leg into it. Immediately market-unwind the
        poly maker fill to flatten the naked leg, and alert 'hedge_declined'."""
        token = self._rest_token(fe)
        unwind = None
        poly = getattr(self.hedger, "poly", None)
        if poly is not None and token:
            try:
                unwind = poly.place_market_sell(token, fe.size)
            except Exception as exc:  # noqa: BLE001
                unwind = {"status": "error", "error": str(exc)}
        # loss on the unwind (positive = $ lost): (fill_price - sell_price) * size.
        cost = None
        if isinstance(unwind, dict) and unwind.get("avg_price") is not None:
            cost = round((float(fe.quote_price) - float(unwind["avg_price"])) * float(fe.size), 4)
            self.pnl_today -= cost
        detail = {"locked_if_hedged": locked, "floor": floor, "unwind": unwind, "unwind_cost": cost}
        self._emit("hedge_declined", fe, ctx, now, detail)
        self._maybe_halt(now)
        return InplayExecResult("hedge_declined", detail)

    def _record_hedge(self, fe: Any, ctx: dict, now: Any, res: Any, locked_est: Optional[float]) -> InplayExecResult:
        status = getattr(res, "status", "error")
        if status == "locked":
            self.fills_today += 1
            self.pnl_today += float(getattr(res, "locked_pnl", 0.0) or 0.0)
        else:                                              # unwound / partial_unwound / error
            uc = getattr(res, "unwind_cost", None)
            if uc is not None:
                self.pnl_today -= float(uc)
        detail = {"status": status, "hedged_shares": getattr(res, "hedged_shares", 0.0),
                  "hedge_avg": getattr(res, "hedge_avg_price", None),
                  "locked_pnl": getattr(res, "locked_pnl", None),
                  "unwind_cost": getattr(res, "unwind_cost", None), "locked_net_est": locked_est}
        self._emit("hedge_" + status, fe, ctx, now, detail)
        self._maybe_halt(now)
        return InplayExecResult("hedged" if status == "locked" else status, detail)

    def _maybe_halt(self, now: Any) -> None:
        if not self.halted and self.pnl_today <= -float(self.li.max_daily_loss_usd):
            self.halted = True
            self._alert("[MAKER_RT][INPLAY] AUTO-HALT: pnl_today $%.2f <= -$%s — in-play quoting stopped "
                        "for the day." % (self.pnl_today, self.li.max_daily_loss_usd))

    # -- helpers -----------------------------------------------------------
    def _refuse(self, action: str, fe: Any, ctx: dict, now: Any, detail: dict) -> InplayExecResult:
        self._emit(action, fe, ctx, now, detail)
        return InplayExecResult(action, detail)

    @staticmethod
    def _rest_token(fe: Any) -> Optional[str]:
        ref = getattr(fe, "rest_ref", None) or ()
        return ref[1] if len(ref) > 1 else None

    def _fill_dict(self, fe: Any) -> dict:
        return {"token_id": self._rest_token(fe), "side": "BUY", "price": fe.quote_price, "size": fe.size}

    @staticmethod
    def _hedge_spec(lookup: dict, hv: Any, hedge_venue: str) -> dict:
        return {"ticker": lookup.get("ticker"), "side": lookup.get("side", "yes"),
                "token_id": lookup.get("token"), "venue": hedge_venue,
                "best_ask": getattr(hv, "best_ask", None)}

    def _emit(self, action: str, fe: Any, ctx: dict, now: Any, detail: dict) -> None:
        """Write the live in-play action to the events CSV AND alert Telegram (spec: every live in-play
        action -> CSV + Telegram)."""
        key = getattr(fe, "key", ())
        sport, game, mkey, side, direction = (tuple(key) + (None,) * 5)[:5]
        row = {"event": "inplay_live", "mode": "live", "sport": sport,
               "phase": ctx.get("phase", "inplay"), "game": game, "market_key": mkey, "side": side,
               "direction": direction, "quote_price": round(float(fe.quote_price), 4),
               "size": round(float(fe.size), 2), "reason": action}
        for k in ("locked_pnl", "unwind_cost"):
            if detail.get(k) is not None:
                row[k] = detail[k]
        if detail.get("locked_net_est") is not None:
            row["locked_net"] = round(float(detail["locked_net_est"]) * 100, 4)
        if self.state is not None:
            self.state.record(row, now)
        self._alert("[MAKER_RT][INPLAY] %s %s %s %s @ %.4f x%.0f :: %s"
                    % (action, game, mkey, direction, float(fe.quote_price), float(fe.size), detail))

    def _alert(self, text: str) -> None:
        if self.log:
            self.log.warning(text)
        if self.telegram:
            try:
                self.telegram(text)
            except Exception as exc:  # noqa: BLE001 — a telegram failure never blocks execution
                if self.log:
                    self.log.warning("[MAKER_RT][INPLAY] telegram send failed: %s", exc)
