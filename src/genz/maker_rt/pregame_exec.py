"""PRE-GAME CONTINUOUS LIVE executor — rest-POLY only (the pilot's live surface).

Owns the real Polymarket resting orders for armed pre-game rest-poly candidates end-to-end:

  * place / reprice / cancel — driven by the QuoteDriver's quote lifecycle. A REPRICE is
    cancel -> CONFIRM cancelled -> place (so we never exceed ``max_open_quotes`` mid-transition), and
    the never-crossable rule is RE-CHECKED against the live book immediately before every POST.
  * fill -> hedge — a fill is detected from the Poly USER socket (order size_matched delta) AND, as a
    reliable backup, a throttled REST poll of each open order (a fill we can't see is a fill we can't
    hedge). On a fill we RE-VERIFY the hedge on the live Kalshi book; if the walked locked-net is below
    the decline floor we DECLINE + market-unwind the poly fill, else we lift the Kalshi IOC hedge sized
    to the matched amount. ONE in-flight hedge at a time (shared guard).
  * caps — sizing capped by ``quote_usd_max``; a PRE-PLACE projected-pair-stake check against
    ``max_daily_stake_usd`` (breach -> refuse + HALT for the day + Telegram); ``max_open_quotes`` /
    ``max_fills_per_day`` / ``max_daily_loss_usd`` all enforced (see :class:`LiveCaps`).
  * user-feed-down safety — if the Poly USER socket drops, placement HALTS and every open quote is
    cancelled (fills we can't observe can't be hedged).

Every live action -> events CSV (mode 'live') + Telegram. IN-PLAY is untouched (its own gate/executor).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from . import hedge as hedge_mod
from .live import assert_live_allowed

PREGAME_HEDGE_DECLINE_FLOOR = -0.010     # re-verify at fill: walked locked-net below this -> decline+unwind


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
    matched_seen: float = 0.0


class PregameLiveExecutor:
    def __init__(self, cfg: Any, gate: Any, order_client: Any, hedger: Any, caps: Any, poly: Any, *,
                 in_flight: Any = None, telegram: Any = None, state: Any = None, log: Any = None) -> None:
        self.cfg = cfg
        self.gate = gate
        self.order_client = order_client        # PolyOrderClient (rest/cancel)
        self.hedger = hedger                     # LiveHedger (kalshi IOC + poly unwind)
        self.caps = caps                         # LiveCaps
        self.poly = poly                         # PolyExec (REST get_order / neg_risk)
        self.in_flight = in_flight
        self.telegram = telegram
        self.state = state
        self.log = log
        self.open_orders: dict = {}              # candidate key -> _LiveOrder
        self.feed_ok: bool = False               # Poly USER socket health (starts DOWN until it connects)
        self._neg_cache: dict = {}               # token -> neg_risk (fetched once)

    # -- gate / eligibility --------------------------------------------------
    def armed(self) -> bool:
        """CHEAP per-loop arm check: enabled + arm-file present. The EXPENSIVE startup self-check
        (readable balances/approvals) already passed — this executor is only constructed when the
        pre-game gate armed at startup — so ongoing arming just needs the config flag + the on-disk arm
        file. Removing the arm file mid-run disarms placement immediately (no network calls)."""
        lc = getattr(self.cfg, "live", None)
        if not getattr(lc, "enabled", False):
            return False
        arm = getattr(lc, "arm_file", "")
        return bool(arm and os.path.exists(arm))

    def eligible(self, c: Any, phase: str) -> bool:
        """Live only for: armed pre-game gate, PRE phase, REST-POLY direction, user feed UP, not halted."""
        return bool(self.armed() and phase == "pre" and c.direction == "rest-poly"
                    and self.feed_ok and not self.caps.halted)

    # -- placement -----------------------------------------------------------
    def place_or_reprice(self, c: Any, dec: Any, rest: Any, store: Any, now: Any, now_ts: float) -> None:
        """Place (or reprice) the real GTC rest for candidate ``c`` at ``dec.quote_price``. Re-checks the
        never-crossable rule on the LIVE book, enforces caps, and reprices via cancel->confirm->place."""
        assert_live_allowed("pre", self.armed())     # HARD (cheap) re-lock immediately before any order
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
        size = self.caps.size_shares(price)
        existing = self.open_orders.get(c.key)
        if existing is not None:
            if abs(existing.price - price) < tick - 1e-9:
                return                                                 # unchanged within a tick -> keep resting
            if not self._cancel(c.key, now, "reprice"):                # cancel not confirmed -> do NOT dup-place
                return
        hedge_ask = dec.hedge_best_ask if dec.hedge_best_ask is not None else price
        projected = self.caps.projected_pair_stake(price, size, hedge_ask, size)
        ok, reason = self.caps.can_place(projected)
        if not ok:
            self._alert(f"[MAKER_RT][LIVE] REFUSE {c.game} {c.direction} @ {price:.4f}: {reason} "
                        f"(projected pair ${projected:.2f}, stake_today ${self.caps.stake_today:.2f})")
            self._record(c, "expire", now, price=price, size=size, hedge_ask=hedge_ask, reason=reason)
            return
        neg = self._neg_for(token, store)
        try:
            res = self.order_client.rest(token, price, size, tick_size=tick, neg_risk=neg)
        except Exception as exc:  # noqa: BLE001
            self._alert(f"[MAKER_RT][LIVE] PLACE FAILED {c.game} {c.direction} @ {price:.4f}: {exc}")
            return
        oid = res.get("order_id")
        if not oid:
            self._alert(f"[MAKER_RT][LIVE] place returned no order_id ({c.game}): {res}")
            return
        lo = _LiveOrder(key=c.key, order_id=oid, token=token, price=price, size=float(size),
                        side=c.rest_side, direction=c.direction, sport=c.sport, game=c.game,
                        market_key=c.market_key, hedge_lookup=dict(c.hedge_lookup),
                        poly_rate=c.poly_rate, placed_ts=now_ts)
        self.open_orders[c.key] = lo
        self.caps.on_open()
        kind = "reprice" if existing is not None else "quote"
        self._record(c, kind, now, price=price, size=size, hedge_ask=hedge_ask, order_id=oid)
        self._alert(f"[MAKER_RT][LIVE] {kind.upper()} {c.game} {c.market_key} {c.direction} @ {price:.4f} "
                    f"x{int(size)} (~${price*size:.2f}) id {oid}")
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
        self._record_lo(lo, "expire", now, reason=reason)
        self._alert(f"[MAKER_RT][LIVE] CANCEL {lo.game} {lo.direction} ({reason}) id {lo.order_id}")
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
        lo.matched_seen = float(total_matched)
        fill_price = float(avg_price) if avg_price not in (None, "") else lo.price
        acquired = self.in_flight.acquire(("pre", key)) if self.in_flight is not None else True
        try:
            self._hedge_fill(lo, delta, fill_price, store, now, now_ts)
        finally:
            if acquired and self.in_flight is not None:
                self.in_flight.release(("pre", key))
        if total_matched >= lo.size - 1e-9:                # fully filled -> no remainder resting
            self.open_orders.pop(key, None)
            self.caps.on_close()
        elif self.caps.halted:                             # partial + a cap tripped -> pull the remainder
            self._cancel(key, now, "halt_after_partial")

    def _hedge_fill(self, lo: _LiveOrder, matched: float, fill_price: float, store: Any,
                    now: Any, now_ts: float) -> None:
        self.caps.commit_stake(matched * fill_price)              # rest leg committed
        hl = lo.hedge_lookup
        hedge_venue = hl.get("venue", "kalshi")
        hv = store.kalshi_view(hl.get("ticker"), hl.get("side")) if hedge_venue == "kalshi" \
            else store.poly_view(hl.get("token"))
        re_mark = hedge_mod.mark_hedge(hv.ask_ladder, matched, hedge_venue, lo.poly_rate) if hv else None
        locked = hedge_mod.locked_net(fill_price, re_mark["cost_per_share"]) if re_mark else None
        self._alert(f"[MAKER_RT][LIVE] FILL {lo.game} {lo.direction} {matched:.0f}@{fill_price:.4f} "
                    f"(id {lo.order_id}) — verifying hedge (locked~{'' if locked is None else f'{locked*100:.2f}%'}).")
        if locked is None or locked < PREGAME_HEDGE_DECLINE_FLOOR:
            cost = self._unwind(lo, matched, fill_price)
            self.caps.on_fill(-(cost or 0.0))
            self._record_lo(lo, "hedge_declined", now, price=fill_price, size=matched,
                            locked_net=locked, unwind_cost=cost)
            self._alert(f"[MAKER_RT][LIVE] HEDGE DECLINED {lo.game} (locked "
                        f"{'n/a' if locked is None else f'{locked*100:.2f}%'} < floor) -> unwound; cost ${cost or 0:.2f}")
            return
        res = self.hedger.hedge({"token_id": lo.token, "side": "BUY", "price": fill_price, "size": matched},
                                {"ticker": hl.get("ticker"), "side": hl.get("side", "yes"),
                                 "best_ask": getattr(hv, "best_ask", None)})
        status = getattr(res, "status", "error")
        if status == "locked":
            self.caps.commit_stake(float(getattr(res, "hedged_shares", 0.0))
                                   * float(getattr(res, "hedge_avg_price", 0.0) or 0.0))
            pnl = float(getattr(res, "locked_pnl", 0.0) or 0.0)
            self.caps.on_fill(pnl)
            self._record_lo(lo, "hedge_locked", now, price=fill_price, size=matched,
                            locked_net=locked, locked_pnl=pnl,
                            hedge_avg=getattr(res, "hedge_avg_price", None))
            self._alert(f"[MAKER_RT][LIVE] HEDGE LOCKED {lo.game} {matched:.0f} -> pnl ${pnl:.2f}")
        else:                                              # unwound / partial_unwound / error
            uc = getattr(res, "unwind_cost", None)
            if uc is not None:
                self.caps.commit_stake(abs(float(uc)))
            self.caps.on_fill(-(float(uc) if uc is not None else 0.0))
            self._record_lo(lo, "hedge_" + status, now, price=fill_price, size=matched,
                            locked_net=locked, unwind_cost=uc)
            self._alert(f"[MAKER_RT][LIVE] HEDGE {status.upper()} {lo.game} (unwind cost "
                        f"${uc if uc is not None else 0:.2f})")

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

    def snapshot(self) -> dict:
        """Live state for the panel: open quotes, today's committed stake vs cap, fills, pnl, halt."""
        return {"open_quotes": len(self.open_orders), "stake_today": round(self.caps.stake_today, 2),
                "stake_cap": self.caps.max_daily_stake_usd, "fills_today": self.caps.fills_today,
                "pnl_today": round(self.caps.pnl_today, 4), "halted": self.caps.halted,
                "feed_ok": self.feed_ok}

    # -- CSV + telegram ------------------------------------------------------
    def _record(self, c: Any, event: str, now: Any, *, price: float = None, size: float = None,
                hedge_ask: float = None, order_id: str = None, reason: str = "") -> None:
        if self.state is None:
            return
        self.state.record({"event": event, "mode": "live", "sport": c.sport, "phase": "pre",
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
        row = {"event": event, "mode": "live", "sport": lo.sport, "phase": "pre", "game": lo.game,
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

    def _alert(self, text: str) -> None:
        if self.log:
            self.log.warning(text)
        if self.telegram:
            try:
                self.telegram(text)
            except Exception as exc:  # noqa: BLE001 — a telegram failure never blocks execution
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] telegram send failed: %s", exc)
