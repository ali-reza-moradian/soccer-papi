"""CONTINUOUS LIVE executor — rest-POLY only, BOTH phases (pre-game + in-play).

Owns the real Polymarket resting orders for armed rest-poly candidates end-to-end. ONE instance
governs PRE-GAME and IN-PLAY with a SHARED budget (one :class:`LiveCaps`) and ONE global in-flight
hedge guard, so pre+inplay exposure is a single pool.

  * place / reprice / cancel — driven by the QuoteDriver's quote lifecycle. A REPRICE is
    cancel -> CONFIRM cancelled -> place (never exceeds ``max_open_quotes`` mid-transition); the
    never-crossable rule is RE-CHECKED against the live book immediately before every POST. IN-PLAY
    placement is additionally gated by the driver's anti-phantom rails + a cool-off (``cooloff_ok``):
    the node must be unfrozen AND both books fresh for >= ``freeze_cooloff_s`` before any in-play POST,
    and any freeze/stale on a node with a live resting order CANCELS it (not just disarms shadow).
  * fill -> hedge (identical both phases) — a fill is detected off the Poly USER socket (size_matched
    delta) AND a throttled REST backup. The hedge is RE-VERIFIED on the live Kalshi book; below the
    -1% decline floor we DECLINE + market-unwind, else lift the Kalshi IOC hedge sized to the match.
    ONE in-flight hedge GLOBALLY across pre+inplay.
  * caps — SHARED ``LiveCaps`` (quote_usd_max sizing; pre-place projected-pair-stake vs
    max_daily_stake_usd -> refuse+HALT+Telegram; max_open_quotes / max_fills_per_day / max_daily_loss).
  * in-play FIRST-FILL soft circuit — after the day's FIRST in-play fill, pause in-play placement for
    ``first_fill_pause_s`` and Telegram the full fill->hedge->locked_net chain (pre-game unaffected);
    resume automatically. Any in-play fill with locked_net <= ``halt_locked_net`` (-2%) HALTS in-play
    for the day (pre-game continues).
  * user-feed-down safety — the Poly USER socket dropping HALTS placement + cancels open quotes.

Every live action -> events CSV (mode 'live') + Telegram.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from . import hedge as hedge_mod
from .live import assert_live_allowed

HEDGE_DECLINE_FLOOR = -0.010     # re-verify at fill: walked locked-net below this -> decline+unwind (both phases)


@dataclass
class _LiveOrder:
    key: tuple
    order_id: str
    token: str
    price: float
    size: float
    side: str
    direction: str
    sport: str
    game: str
    market_key: str
    hedge_lookup: dict
    poly_rate: float
    placed_ts: float
    phase: str = "pre"
    best_bid: Optional[float] = None
    matched_seen: float = 0.0


class PregameLiveExecutor:
    """Unified live executor for pre-game AND in-play rest-poly (the ``pregame_exec`` on the driver)."""

    def __init__(self, cfg: Any, gate: Any, order_client: Any, hedger: Any, caps: Any, poly: Any, *,
                 in_flight: Any = None, telegram: Any = None, state: Any = None, log: Any = None) -> None:
        self.cfg = cfg
        self.gate = gate
        self.order_client = order_client        # PolyOrderClient (rest/cancel)
        self.hedger = hedger                     # LiveHedger (kalshi IOC + poly unwind)
        self.caps = caps                         # LiveCaps (SHARED across phases)
        self.poly = poly                         # PolyExec (REST get_order / neg_risk)
        self.in_flight = in_flight               # ONE global in-flight guard (pre+inplay)
        self.telegram = telegram
        self.state = state
        self.log = log
        self.open_orders: dict = {}              # candidate key -> _LiveOrder
        self.feed_ok: bool = False               # Poly USER socket health (starts DOWN until it connects)
        self._neg_cache: dict = {}               # token -> neg_risk (fetched once)
        # IN-PLAY first-fill circuit (per UTC day; replaces the calendar guard).
        li = getattr(cfg, "live_inplay", None)
        self.inplay_first_fill_pause_s = float(getattr(li, "first_fill_pause_s", 120.0))
        self.inplay_halt_locked_net = float(getattr(li, "halt_locked_net", -0.020))
        self.inplay_cooloff_s = float(getattr(li, "freeze_cooloff_s", 10.0))
        self.inplay_fills_today = 0
        self.inplay_pause_until = 0.0
        self.inplay_halted = False
        self._day = ""
        # REPRICE HYSTERESIS + connection-freshness knobs.
        self.reprice_min_ticks = int(getattr(cfg, "reprice_min_ticks", 2))
        self.min_rest_s = float(getattr(cfg, "min_rest_s", 20.0))
        ip = getattr(cfg, "inplay", None)
        self.conn_fresh_s = float(getattr(ip, "conn_fresh_s", 30.0))
        self.node_quiet_max_s = float(getattr(ip, "node_quiet_max_s", 180.0))
        # TELEGRAM digest (routine quote/reprice/cancel collapse; fills/hedges/pause/halt/errors instant).
        self.digest_min = float(getattr(cfg, "telegram_digest_min", 15.0))
        self._digest = {"quotes": 0, "cancels": {}, "fills": 0}
        self._digest_since = 0.0
        # LIFETIME metrics (live quotes only): closed-quote lifetimes + an at-best time sampler.
        self._lifetimes: list = []
        self._atbest_hits = 0
        self._atbest_samples = 0

    # -- daily roll ----------------------------------------------------------
    def roll_day(self, now: Any) -> None:
        """Reset the per-day in-play circuit AND the shared caps' daily counters at UTC midnight."""
        day = now.strftime("%Y%m%d")
        if day == self._day:
            return
        self._day = day
        self.inplay_fills_today = 0
        self.inplay_pause_until = 0.0
        self.inplay_halted = False
        self._lifetimes = []
        self._atbest_hits = 0
        self._atbest_samples = 0
        if hasattr(self.caps, "roll"):
            self.caps.roll(day)

    # -- gate / eligibility --------------------------------------------------
    @staticmethod
    def _armed(block: Any) -> bool:
        """CHEAP arm check for a gate block: enabled + on-disk arm file. The expensive startup self-check
        already passed (this executor is only built when a gate armed at startup)."""
        if not getattr(block, "enabled", False):
            return False
        arm = getattr(block, "arm_file", "")
        return bool(arm and os.path.exists(arm))

    def pre_armed(self) -> bool:
        return self._armed(getattr(self.cfg, "live", None))

    def inplay_armed(self) -> bool:
        return self._armed(getattr(self.cfg, "live_inplay", None))

    def eligible(self, c: Any, phase: str, now_ts: float = 0.0) -> bool:
        """Live-eligible iff rest-poly, user feed UP, caps not halted, AND the phase's own gate is armed.
        In-play additionally requires: not in-play-halted AND not inside the first-fill pause. (The
        driver enforces the freeze/stale/persistence/cool-off rails BEFORE this.)"""
        if c.direction != "rest-poly" or not self.feed_ok or self.caps.halted:
            return False
        if phase == "pre":
            return self.pre_armed()
        if phase == "inplay":
            return bool(self.inplay_armed() and not self.inplay_halted and now_ts >= self.inplay_pause_until)
        return False

    def cooloff_ok(self, store: Any, c: Any, freeze_until_ts: float, now_ts: float) -> bool:
        """IN-PLAY cool-off: node NOT frozen now, thawed >= ``freeze_cooloff_s`` ago, AND both books
        CONNECTION-fresh (a quiet book on a healthy socket is fresh). Stricter than the shadow rail."""
        if now_ts < float(freeze_until_ts or 0.0):
            return False
        if freeze_until_ts and (now_ts - float(freeze_until_ts)) < self.inplay_cooloff_s:
            return False
        return bool(store.node_fresh(c.rest_id, now_ts, self.conn_fresh_s, self.node_quiet_max_s)
                    and store.node_fresh(c.hedge_id, now_ts, self.conn_fresh_s, self.node_quiet_max_s))

    # -- placement -----------------------------------------------------------
    def place_or_reprice(self, c: Any, dec: Any, rest: Any, store: Any, now: Any, now_ts: float,
                         phase: str = "pre") -> None:
        """Place (or reprice) the real GTC rest for ``c`` at ``dec.quote_price`` in ``phase``. Re-checks
        never-crossable on the LIVE book, enforces the shared caps, reprices via cancel->confirm->place."""
        armed = self.pre_armed() if phase == "pre" else self.inplay_armed()
        assert_live_allowed(phase, armed)             # HARD (cheap) re-lock immediately before any order
        token = c.rest_ref[1]
        price = float(dec.quote_price)
        tick = store.poly_tick(token, 0.01) or 0.01
        live_rest = store.poly_view(token) or rest
        best_ask = getattr(live_rest, "best_ask", None)
        if best_ask is not None and price > best_ask - tick + 1e-9:    # would CROSS now -> never place
            if c.key in self.open_orders:
                self._cancel(c.key, now, "would_cross_at_post")
            return
        if price < tick - 1e-9:
            if c.key in self.open_orders:
                self._cancel(c.key, now, "below_tick")
            return
        existing = self.open_orders.get(c.key)
        if existing is not None:
            cur = existing.price
            floor = getattr(dec, "floor", None)
            crosses = best_ask is not None and cur > best_ask - tick + 1e-9
            below_floor = floor is not None and cur > floor + 1e-9      # resting ABOVE floor -> nets < target
            if crosses or below_floor:
                # MANDATORY reprice: the resting price is now UNECONOMIC (crosses the live book / under
                # target). Cancel + replace immediately.
                if not self._cancel(c.key, now, "reprice_cross" if crosses else "reprice_floor"):
                    return
            else:
                if abs(cur - price) < tick - 1e-9:
                    return                                             # same price -> keep resting
                # VOLUNTARY upward reprice ONLY: >= reprice_min_ticks better (or no-longer-at-best) AND the
                # order has rested >= min_rest_s. Otherwise KEEP RESTING to preserve queue position.
                rested = (now_ts - existing.placed_ts) >= self.min_rest_s
                higher = price >= cur + self.reprice_min_ticks * tick - 1e-9
                rest_bid = getattr(live_rest, "best_bid", None)
                no_longer_best = rest_bid is not None and rest_bid > cur + 1e-9
                if not (rested and (higher or no_longer_best)):
                    return
                if not self._cancel(c.key, now, "reprice"):            # cancel not confirmed -> do NOT dup-place
                    return
        size = self.caps.size_shares(price)
        hedge_ask = dec.hedge_best_ask if dec.hedge_best_ask is not None else price
        projected = self.caps.projected_pair_stake(price, size, hedge_ask, size)
        ok, reason = self.caps.can_place(projected)
        if not ok:
            self._routine("refuse", f"[MAKER_RT][LIVE] REFUSE {c.game} {c.direction} [{phase}] @ "
                          f"{price:.4f}: {reason} (projected pair ${projected:.2f}, "
                          f"stake_today ${self.caps.stake_today:.2f})")
            self._record(c, "expire", now, phase, price=price, size=size, hedge_ask=hedge_ask, reason=reason)
            return
        neg = self._neg_for(token, store)
        try:
            res = self.order_client.rest(token, price, size, tick_size=tick, neg_risk=neg)
        except Exception as exc:  # noqa: BLE001
            self._alert(f"[MAKER_RT][LIVE] PLACE FAILED {c.game} {c.direction} [{phase}] @ {price:.4f}: {exc}")
            return
        oid = res.get("order_id")
        if not oid:
            self._alert(f"[MAKER_RT][LIVE] place returned no order_id ({c.game}): {res}")
            return
        lo = _LiveOrder(key=c.key, order_id=oid, token=token, price=price, size=float(size),
                        side=c.rest_side, direction=c.direction, sport=c.sport, game=c.game,
                        market_key=c.market_key, hedge_lookup=dict(c.hedge_lookup), poly_rate=c.poly_rate,
                        placed_ts=now_ts, phase=phase, best_bid=getattr(rest, "best_bid", None))
        self.open_orders[c.key] = lo
        self.caps.on_open()
        kind = "reprice" if existing is not None else "quote"
        self._record(c, kind, now, phase, price=price, size=size, hedge_ask=hedge_ask, order_id=oid)
        self._routine("quote", f"[MAKER_RT][LIVE] {kind.upper()} {c.game} {c.market_key} {c.direction} "
                      f"[{phase}] @ {price:.4f} x{int(size)} (~${price*size:.2f}) id {oid}")
        matched = float(res.get("shares") or 0)          # a maker shouldn't fill on POST, but the book can move
        if matched > 0:
            self._on_fill_detected(c.key, matched, float(res.get("avg_price") or price), store, now, now_ts)

    # -- cancels -------------------------------------------------------------
    def cancel(self, c: Any, now: Any, reason: str) -> bool:
        return self._cancel(c.key, now, reason)

    def cancel_key(self, key: tuple, now: Any, reason: str) -> bool:
        return self._cancel(key, now, reason)

    def _cancel(self, key: tuple, now: Any, reason: str) -> bool:
        """Cancel the tracked order and CONFIRM cancellation. Returns True only when confirmed cancelled
        (so a reprice never double-places). An unconfirmed cancel leaves it tracked for a later sweep."""
        lo = self.open_orders.get(key)
        if lo is None:
            return True
        resp = None
        try:
            resp = self.order_client.cancel(lo.order_id)
        except Exception as exc:  # noqa: BLE001
            self._alert(f"[MAKER_RT][LIVE] cancel raised for {lo.order_id}: {exc}")
        if not self._cancel_confirmed(lo.order_id, resp):
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] cancel NOT confirmed %s (%s) — keeping tracked.",
                                 lo.order_id, reason)
            return False
        self.open_orders.pop(key, None)
        self.caps.on_close()
        self._record_lifetime(lo, now)
        self._record_lo(lo, "expire", now, reason=reason)
        self._routine("cancel", f"[MAKER_RT][LIVE] CANCEL {lo.game} {lo.direction} [{lo.phase}] "
                      f"({reason}) id {lo.order_id}", reason=reason)
        return True

    def _cancel_confirmed(self, oid: str, resp: Any) -> bool:
        if isinstance(resp, dict):
            canceled = resp.get("canceled") or resp.get("cancelled") or []
            if oid in canceled:
                return True
        try:
            o = self.poly.get_order(oid)
        except Exception:  # noqa: BLE001 — a gone/404 order is no longer active
            return True
        if not isinstance(o, dict) or not o:
            return True
        return str(o.get("status") or "").upper() in ("CANCELED", "CANCELLED")

    def cancel_all(self, reason: str, now: Any = None) -> int:
        """Cancel EVERY open order (shutdown / halt / feed-down). Best-effort venue cancel-all + per-key
        confirm. Returns how many were open."""
        n = len(self.open_orders)
        try:
            self.poly.cancel_all()
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] cancel_all() failed: %s", exc)
        from .state import utcnow
        for key in list(self.open_orders):
            self._cancel(key, now or utcnow(), reason)
        return n

    def cancel_inplay_open(self, now: Any, reason: str = "inplay_halt") -> int:
        """Cancel only the IN-PLAY open orders (pre-game orders keep resting)."""
        keys = [k for k, lo in self.open_orders.items() if lo.phase == "inplay"]
        for k in keys:
            self._cancel(k, now, reason)
        return len(keys)

    def enforce_arm_state(self, now: Any) -> None:
        """Each loop: if a phase's gate was disarmed (arm file removed / enabled off), cancel that phase's
        open orders immediately."""
        pre_ok, ip_ok = self.pre_armed(), self.inplay_armed()
        for k, lo in list(self.open_orders.items()):
            if (lo.phase == "pre" and not pre_ok) or (lo.phase == "inplay" and not ip_ok):
                self._cancel(k, now, "disarmed")

    # -- feed health ---------------------------------------------------------
    def set_feed_ok(self, ok: bool, now: Any = None) -> None:
        """User-socket health. On a DOWN transition (was up, now down): HALT placement + cancel every open
        quote (a fill we cannot observe cannot be hedged). On an UP transition: re-enable placement."""
        was = self.feed_ok
        self.feed_ok = bool(ok)
        if was and not ok:
            self._alert("[MAKER_RT][LIVE] Poly USER socket DOWN -- halting placement + cancelling open quotes.")
            self.cancel_all("poly_user_down", now)
        elif ok and not was and self.log:
            self.log.info("[MAKER_RT][LIVE] Poly USER socket UP -- placement enabled.")

    # -- fill detection + hedge ----------------------------------------------
    def on_order_update(self, event: dict, store: Any, now: Any, now_ts: float) -> None:
        """Poly USER-socket order update -> detect our fills by size_matched delta (matched to our oid)."""
        oid = event.get("order_id")
        key = self._key_for_oid(oid)
        if key is None:
            return
        matched = event.get("size_matched")
        if matched is None:
            return
        self._on_fill_detected(key, float(matched), event.get("price"), store, now, now_ts)

    def poll_open_orders(self, store: Any, now: Any, now_ts: float) -> None:
        """REST backup fill detector (throttled by the caller): read each open order's size_matched and
        hedge any delta the socket missed. Also drops orders cancelled out from under us."""
        for key, lo in list(self.open_orders.items()):
            try:
                o = self.poly.get_order(lo.order_id)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(o, dict) or not o:
                continue
            matched = o.get("size_matched")
            status = str(o.get("status") or "").upper()
            if matched is not None and float(matched) > lo.matched_seen + 1e-9:
                self._on_fill_detected(key, float(matched), o.get("price"), store, now, now_ts)
            elif status in ("CANCELED", "CANCELLED") and lo.matched_seen <= 1e-9:
                self.open_orders.pop(key, None)      # cancelled out-of-band -> stop tracking
                self.caps.on_close()

    def _on_fill_detected(self, key: tuple, total_matched: float, avg_price: Any, store: Any,
                          now: Any, now_ts: float) -> None:
        lo = self.open_orders.get(key)
        if lo is None:
            return
        delta = float(total_matched) - lo.matched_seen
        if delta <= 1e-9:
            return
        # ONE in-flight hedge GLOBALLY (pre+inplay). If busy, DEFER without advancing matched_seen so the
        # next detection (socket or REST poll) retries this delta once the guard frees.
        if self.in_flight is not None and not self.in_flight.acquire(("live", key)):
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] hedge in-flight busy — deferring fill of %s", lo.game)
            return
        try:
            lo.matched_seen = float(total_matched)
            fill_price = float(avg_price) if avg_price not in (None, "") else lo.price
            result = self._hedge_fill(lo, delta, fill_price, store, now, now_ts)
        finally:
            if self.in_flight is not None:
                self.in_flight.release(("live", key))
        self._digest["fills"] += 1                          # count for the digest (the hedge is instant-alerted)
        if lo.phase == "inplay":
            self._apply_inplay_circuit(lo, result, now, now_ts)
        if total_matched >= lo.size - 1e-9:                # fully filled -> no remainder resting
            self._record_lifetime(lo, now)
            self.open_orders.pop(key, None)
            self.caps.on_close()
        elif self.caps.halted or (lo.phase == "inplay" and self.inplay_halted):
            self._cancel(key, now, "halt_after_partial")   # partial + a cap/circuit tripped -> pull remainder

    def _hedge_fill(self, lo: _LiveOrder, matched: float, fill_price: float, store: Any,
                    now: Any, now_ts: float) -> dict:
        """Hedge one fill (re-verify -> decline+unwind or IOC lift). Returns a result dict for the CSV +
        the in-play circuit (outcome, locked_net, pnl, hedge order id, chain string)."""
        self.caps.commit_stake(matched * fill_price)              # rest leg committed
        hl = lo.hedge_lookup
        hedge_venue = hl.get("venue", "kalshi")
        hv = store.kalshi_view(hl.get("ticker"), hl.get("side")) if hedge_venue == "kalshi" \
            else store.poly_view(hl.get("token"))
        re_mark = hedge_mod.mark_hedge(hv.ask_ladder, matched, hedge_venue, lo.poly_rate) if hv else None
        locked = hedge_mod.locked_net(fill_price, re_mark["cost_per_share"]) if re_mark else None
        self._alert(f"[MAKER_RT][LIVE] FILL {lo.game} {lo.direction} [{lo.phase}] {matched:.0f}@{fill_price:.4f} "
                    f"(id {lo.order_id}) -- verifying hedge (locked~{'n/a' if locked is None else f'{locked*100:.2f}%'}).")
        if locked is None or locked < HEDGE_DECLINE_FLOOR:
            cost = self._unwind(lo, matched, fill_price)
            self.caps.on_fill(-(cost or 0.0))
            self._record_lo(lo, "hedge_declined", now, price=fill_price, size=matched,
                            locked_net=locked, unwind_cost=cost)
            self._alert(f"[MAKER_RT][LIVE] HEDGE DECLINED {lo.game} [{lo.phase}] (locked "
                        f"{'n/a' if locked is None else f'{locked*100:.2f}%'} < floor) -> unwound; cost ${cost or 0:.2f}")
            return {"outcome": "hedge_declined", "locked_net": locked, "pnl": -(cost or 0.0),
                    "hedge_order_id": None,
                    "chain": self._chain(lo, matched, fill_price, "declined+unwound", locked, -(cost or 0.0), None)}
        res = self.hedger.hedge({"token_id": lo.token, "side": "BUY", "price": fill_price, "size": matched},
                                {"ticker": hl.get("ticker"), "side": hl.get("side", "yes"),
                                 "best_ask": getattr(hv, "best_ask", None)})
        status = getattr(res, "status", "error")
        hedge_oid = ((getattr(res, "detail", None) or {}).get("kalshi") or {}).get("order_id") \
            if isinstance(getattr(res, "detail", None), dict) else None
        if status == "locked":
            self.caps.commit_stake(float(getattr(res, "hedged_shares", 0.0))
                                   * float(getattr(res, "hedge_avg_price", 0.0) or 0.0))
            pnl = float(getattr(res, "locked_pnl", 0.0) or 0.0)
            self.caps.on_fill(pnl)
            self._record_lo(lo, "hedge_locked", now, price=fill_price, size=matched, locked_net=locked,
                            locked_pnl=pnl, hedge_avg=getattr(res, "hedge_avg_price", None))
            self._alert(f"[MAKER_RT][LIVE] HEDGE LOCKED {lo.game} [{lo.phase}] {matched:.0f} -> pnl ${pnl:.2f} "
                        f"(hedge id {hedge_oid})")
            return {"outcome": "hedge_locked", "locked_net": locked, "pnl": pnl, "hedge_order_id": hedge_oid,
                    "chain": self._chain(lo, matched, fill_price, "locked", locked, pnl, hedge_oid)}
        uc = getattr(res, "unwind_cost", None)              # unwound / partial_unwound / error
        if uc is not None:
            self.caps.commit_stake(abs(float(uc)))
        self.caps.on_fill(-(float(uc) if uc is not None else 0.0))
        self._record_lo(lo, "hedge_" + status, now, price=fill_price, size=matched,
                        locked_net=locked, unwind_cost=uc)
        self._alert(f"[MAKER_RT][LIVE] HEDGE {status.upper()} {lo.game} [{lo.phase}] (unwind cost "
                    f"${uc if uc is not None else 0:.2f})")
        return {"outcome": "hedge_" + status, "locked_net": locked, "pnl": -(float(uc) if uc is not None else 0.0),
                "hedge_order_id": hedge_oid,
                "chain": self._chain(lo, matched, fill_price, status, locked, -(float(uc) if uc else 0.0), hedge_oid)}

    def _apply_inplay_circuit(self, lo: _LiveOrder, result: dict, now: Any, now_ts: float) -> None:
        """After an IN-PLAY fill: (a) if locked_net <= halt threshold -> HALT in-play for the day + cancel
        in-play opens; (b) on the day's FIRST in-play fill -> pause in-play placement + Telegram the chain.
        Pre-game is never affected."""
        self.inplay_fills_today += 1
        locked = (result or {}).get("locked_net")
        if locked is not None and locked <= self.inplay_halt_locked_net and not self.inplay_halted:
            self.inplay_halted = True
            self._alert(f"[MAKER_RT][INPLAY] DAY-HALT: in-play fill locked_net {locked*100:.2f}% <= "
                        f"{self.inplay_halt_locked_net*100:.1f}% -- in-play placement stopped for the day "
                        f"(pre-game continues).")
            self.cancel_inplay_open(now, "inplay_day_halt")
        if self.inplay_fills_today == 1:
            self.inplay_pause_until = now_ts + self.inplay_first_fill_pause_s
            self._alert(f"[MAKER_RT][INPLAY] FIRST in-play fill of the day -- pausing in-play placement "
                        f"{self.inplay_first_fill_pause_s:.0f}s (pre-game unaffected). CHAIN: {(result or {}).get('chain')}")

    def _chain(self, lo: _LiveOrder, matched: float, fill_price: float, outcome: str,
               locked: Optional[float], pnl: float, hedge_oid: Any) -> str:
        return (f"{lo.game} {lo.market_key} [{lo.phase}] fill {matched:.0f}@{fill_price:.4f} rest_id={lo.order_id} "
                f"-> {outcome} hedge_id={hedge_oid} locked_net={'n/a' if locked is None else f'{locked*100:.2f}%'} "
                f"pnl=${pnl:.2f}")

    def _unwind(self, lo: _LiveOrder, shares: float, fill_price: float) -> Optional[float]:
        """Market-sell the naked poly fill; return the realized loss ($, positive = lost)."""
        poly = getattr(self.hedger, "poly", None) or self.poly
        unwind = None
        try:
            unwind = poly.place_market_sell(lo.token, shares)
        except Exception as exc:  # noqa: BLE001
            self._alert(f"[MAKER_RT][LIVE] unwind sell FAILED {lo.token[:10]}...: {exc}")
            return None
        sell_px = (unwind or {}).get("avg_price") if isinstance(unwind, dict) else None
        if sell_px is None:
            return None
        cost = round((float(fill_price) - float(sell_px)) * float(shares), 4)
        self.caps.commit_stake(float(sell_px) * float(shares))
        return cost

    # -- helpers -------------------------------------------------------------
    def _neg_for(self, token: str, store: Any) -> Optional[bool]:
        if token in self._neg_cache:
            return self._neg_cache[token]
        neg = None
        try:
            _tick, neg = self.poly._tick_and_negrisk(token)
        except Exception:  # noqa: BLE001 — leave neg None; PolyExec re-fetches at place time
            neg = None
        self._neg_cache[token] = neg
        return neg

    def _key_for_oid(self, oid: Any) -> Optional[tuple]:
        for key, lo in self.open_orders.items():
            if lo.order_id == oid:
                return key
        return None

    def open_count(self) -> int:
        return len(self.open_orders)

    def snapshot(self, now_ts: float = 0.0) -> dict:
        """Live state for the panel: per-phase open counts, shared stake/fills/pnl, in-play circuit, and a
        LIVE QUOTES detail list (market, phase, our price, best bid, size, age, order id)."""
        pre_open = sum(1 for lo in self.open_orders.values() if lo.phase == "pre")
        ip_open = sum(1 for lo in self.open_orders.values() if lo.phase == "inplay")
        quotes = [{"game": lo.game, "market_key": lo.market_key, "phase": lo.phase,
                   "price": round(lo.price, 4), "best_bid": lo.best_bid, "size": int(lo.size),
                   "age_s": round(now_ts - lo.placed_ts, 1) if now_ts else None,
                   "order_id": (lo.order_id[:10] + "…") if lo.order_id else None}
                  for lo in self.open_orders.values()]
        lt = sorted(self._lifetimes)
        n = len(lt)
        med = None if n == 0 else round(lt[n // 2] if n % 2 else (lt[n // 2 - 1] + lt[n // 2]) / 2.0, 1)
        atbest = round(self._atbest_hits / self._atbest_samples, 4) if self._atbest_samples else None
        return {"open_quotes": len(self.open_orders), "pre_open": pre_open, "inplay_open": ip_open,
                "stake_today": round(self.caps.stake_today, 2), "stake_cap": self.caps.max_daily_stake_usd,
                "fills_today": self.caps.fills_today, "pnl_today": round(self.caps.pnl_today, 4),
                "halted": self.caps.halted, "feed_ok": self.feed_ok,
                "inplay_fills": self.inplay_fills_today,
                "inplay_paused": bool(now_ts and now_ts < self.inplay_pause_until),
                "inplay_halted": self.inplay_halted,
                "median_quote_age_s": med, "time_at_best_share": atbest, "quotes": quotes}

    # -- CSV + telegram ------------------------------------------------------
    def _record(self, c: Any, event: str, now: Any, phase: str, *, price: float = None, size: float = None,
                hedge_ask: float = None, order_id: str = None, reason: str = "") -> None:
        if self.state is None:
            return
        self.state.record({"event": event, "mode": "live", "sport": c.sport, "phase": phase,
                           "game": c.game, "market_key": c.market_key, "side": c.rest_side,
                           "direction": c.direction, "rest_venue": c.rest_venue,
                           "hedge_venue": c.hedge_venue,
                           "quote_price": round(price, 4) if price is not None else "",
                           "size": round(size, 2) if size is not None else "",
                           "hedge_ask": round(hedge_ask, 4) if hedge_ask is not None else "",
                           "reason": reason or (order_id or "")}, now)

    def _record_lo(self, lo: _LiveOrder, event: str, now: Any, *, price: float = None, size: float = None,
                   locked_net: float = None, locked_pnl: float = None, unwind_cost: float = None,
                   hedge_avg: float = None, reason: str = "") -> None:
        if self.state is None:
            return
        row = {"event": event, "mode": "live", "sport": lo.sport, "phase": lo.phase, "game": lo.game,
               "market_key": lo.market_key, "side": lo.side, "direction": lo.direction,
               "quote_price": round(price if price is not None else lo.price, 4),
               "size": round(size if size is not None else lo.size, 2),
               "reason": reason or lo.order_id}
        if locked_net is not None:
            row["locked_net"] = round(float(locked_net) * 100, 4)
        if hedge_avg is not None:
            row["hedge_avg"] = round(float(hedge_avg), 4)
        for k, v in (("locked_pnl", locked_pnl), ("unwind_cost", unwind_cost)):
            if v is not None:
                row[k] = v
        self.state.record(row, now)

    # -- alerting: INSTANT (fill/hedge/unwind/pause/halt/feed/error) vs ROUTINE (quote/reprice/cancel) --
    def _send_telegram(self, text: str) -> None:
        if not self.telegram:
            return
        try:
            self.telegram(text)
        except Exception as exc:  # noqa: BLE001 — a telegram failure never blocks execution
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] telegram send failed: %s", exc)

    def _instant(self, text: str) -> None:
        """A material event -> WARNING log + Telegram immediately."""
        if self.log:
            self.log.warning(text)
        self._send_telegram(text)

    _alert = _instant                                # back-compat: existing instant call sites

    @staticmethod
    def _digest_bucket(reason: Any) -> str:
        r = str(reason or "")
        if "reprice" in r or r in ("would_cross_at_post", "below_tick"):
            return "reprice"
        if "stale" in r:
            return "stale"
        if "thin" in r:
            return "thin"
        return "expire"

    def _routine(self, kind: str, text: str, reason: Any = None) -> None:
        """A routine event (quote/reprice/cancel/refuse): INFO log, then either count into the digest or
        (when telegram_digest_min == 0) Telegram immediately, like the old per-event behavior."""
        if self.log:
            self.log.info(text)
        if self.digest_min <= 0:
            self._send_telegram(text)
            return
        if kind == "quote":
            self._digest["quotes"] += 1
        elif kind == "cancel":
            b = self._digest_bucket(reason)
            self._digest["cancels"][b] = self._digest["cancels"].get(b, 0) + 1

    def maybe_flush_digest(self, now_ts: float) -> None:
        """Every ``telegram_digest_min`` minutes send ONE digest line for the routine activity."""
        if self.digest_min <= 0:
            return
        if self._digest_since == 0.0:
            self._digest_since = now_ts
            return
        if now_ts - self._digest_since < self.digest_min * 60.0:
            return
        d = self._digest
        total = sum(d["cancels"].values())
        if d["quotes"] or total or d["fills"]:
            reasons = ", ".join(f"{v} {k}" for k, v in sorted(d["cancels"].items(), key=lambda x: -x[1]))
            line = (f"[MAKER_RT][DIGEST {int(self.digest_min)}m] {d['quotes']} quotes, {total} cancels"
                    f"{' [' + reasons + ']' if reasons else ''}, {d['fills']} fills, open {len(self.open_orders)}")
            self._send_telegram(line)
            if self.log:
                self.log.warning(line)
        self._digest = {"quotes": 0, "cancels": {}, "fills": 0}
        self._digest_since = now_ts

    def sample_metrics(self, store: Any, now_ts: float) -> None:
        """Each loop: sample whether each LIVE quote is currently AT BEST (our price >= live best bid)."""
        for lo in self.open_orders.values():
            v = store.poly_view(lo.token)
            bb = getattr(v, "best_bid", None) if v is not None else None
            self._atbest_samples += 1
            if bb is None or lo.price >= bb - 1e-9:
                self._atbest_hits += 1

    def _record_lifetime(self, lo: _LiveOrder, now: Any) -> None:
        """A live quote closed (cancel or full fill): record how long it rested (the churn metric)."""
        try:
            age = now.timestamp() - float(lo.placed_ts)
        except Exception:  # noqa: BLE001
            return
        if age >= 0:
            self._lifetimes.append(age)
            if len(self._lifetimes) > 5000:
                self._lifetimes = self._lifetimes[-5000:]
