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
from .caps import direction_slot_ok
from .live import assert_live_allowed

HEDGE_DECLINE_FLOOR = -0.010     # re-verify at fill: walked locked-net below this -> decline+unwind (both phases)
# A full-slot refusal (max_open_quotes / reserve_per_direction) recurs EVERY debounce loop while the caps
# are full — the driver retries each viable candidate ~4x/s. Log a given node+direction's slot refusal at
# most once per this window; the suppressed count is surfaced in the Telegram digest line instead.
REFUSE_LOG_EVERY_S = 300.0
_SLOT_REFUSE_REASONS = ("max_open_quotes", "reserve_per_direction")


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
    rest_venue: str = "polymarket"     # "polymarket" | "kalshi" — dispatches place/cancel/fill/unwind/verify
    kalshi_side: str = ""              # YES|NO when rest_venue == "kalshi" (rest_ref[2])


class PregameLiveExecutor:
    """Unified live executor for pre-game AND in-play rest-poly (the ``pregame_exec`` on the driver)."""

    def __init__(self, cfg: Any, gate: Any, order_client: Any, hedger: Any, caps: Any, poly: Any, *,
                 in_flight: Any = None, telegram: Any = None, state: Any = None, log: Any = None,
                 kalshi_order_client: Any = None, kalshi: Any = None) -> None:
        self.cfg = cfg
        self.gate = gate
        self.order_client = order_client        # PolyOrderClient (rest_poly rest/cancel)
        self.kalshi_order_client = kalshi_order_client   # KalshiOrderClient (rest_kalshi rest/cancel/status)
        self.hedger = hedger                     # LiveHedger (venue-aware: kalshi IOC | poly FAK)
        self.caps = caps                         # LiveCaps (SHARED across directions + phases)
        self.poly = poly                         # PolyExec (REST get_order / neg_risk / conditional_balance)
        self.kalshi = kalshi                     # KalshiExec (REST order status / positions / unwind sell)
        self.in_flight = in_flight               # ONE global in-flight guard (all directions + phases)
        self.telegram = telegram
        self.state = state
        self.log = log
        self.open_orders: dict = {}              # candidate key -> _LiveOrder
        self.feed_ok: bool = False               # Poly USER socket health (starts DOWN until it connects)
        self.kalshi_feed_ok: bool = False        # Kalshi WS health (rest_kalshi fill signal; DOWN until up)
        # LIVE-eligible rest directions (normalised, e.g. {"rest-poly","rest-kalshi"}). rest_kalshi stays
        # OFF until its SMOKE-KALSHI passes and config maker_rt.directions adds it.
        self.directions = {str(d).replace("_", "-") for d in getattr(cfg, "directions", ("rest_poly",))}
        # PER-DIRECTION SLOT RESERVATION: guarantee each enabled direction this many of max_open_quotes so
        # one direction (e.g. rest-kalshi) can't monopolize every slot. 0 = off / single-direction no-op.
        self.reserve_per_direction = int(getattr(cfg, "reserve_per_direction", 0))
        self._refuse_log_at: dict = {}           # (candidate key, reason) -> last log ts (throttles slot-refusal spam)
        self._neg_cache: dict = {}               # token -> neg_risk (fetched once)
        self._traded_tickers: set = set()        # kalshi tickers we've rested on -> reconcile for flatness
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
        self._digest = {"quotes": 0, "cancels": {}, "fills": 0, "refuse_suppressed": 0}
        self._digest_since = 0.0
        # LIFETIME metrics (live quotes only): closed-quote lifetimes + an at-best time sampler.
        self._lifetimes: list = []
        self._atbest_hits = 0
        self._atbest_samples = 0
        # POSITION INTEGRITY: an ORPHAN (unwind not confirmed flat / reconciliation mismatch) latches a
        # full halt until manual clearance. auto_flatten (config) decides whether reconciliation re-sells.
        self.orphan: Optional[dict] = None
        self._traded_tokens: set = set()             # tokens we've placed on -> reconcile these for flatness
        self.auto_flatten = bool(getattr(getattr(cfg, "live", None), "auto_flatten", False))
        # Persist the watch-set NEXT TO the arm file (data/ops in prod; the test tmp dir in tests) so a
        # token left non-flat by a CRASHED prior run is re-checked by the startup reconcile. The in-memory
        # set alone dies with the process, and a blanket list_positions() sweep is unusable on this funder
        # wallet (hundreds of unrelated positions -> every one a false orphan; see reconcile_positions).
        _arm = getattr(getattr(cfg, "live", None), "arm_file", "") or ""
        self._traded_path = (os.path.join(os.path.dirname(_arm), "maker_rt_traded_tokens.json")
                             if _arm else "")
        self._load_traded_tokens()

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
        """Live-eligible iff the direction is enabled (config maker_rt.directions), its FILL feed is UP,
        caps not halted, AND the phase's own gate is armed. rest-poly needs the Poly USER socket; rest-kalshi
        needs the Kalshi WS (its fill channel). In-play additionally requires: not in-play-halted AND not
        inside the first-fill pause. (The driver enforces the freeze/stale/persistence/cool-off rails first.)"""
        if c.direction not in self.directions or self.caps.halted:
            return False
        feed_ok = self.feed_ok if c.direction == "rest-poly" else self.kalshi_feed_ok
        if not feed_ok:
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

    # -- venue dispatch (rest_poly = Polymarket | rest_kalshi = Kalshi) ------
    def _rest_tick(self, c: Any, store: Any) -> float:
        return 0.01 if c.rest_venue == "kalshi" else (store.poly_tick(c.rest_ref[1], 0.01) or 0.01)

    def _rest_view_c(self, c: Any, store: Any):
        """Live rest-book SideView for a CANDIDATE (venue-dispatched)."""
        if c.rest_venue == "kalshi":
            return store.kalshi_view(c.rest_ref[1], c.rest_ref[2])
        return store.poly_view(c.rest_ref[1])

    def _rest_view_lo(self, lo: _LiveOrder, store: Any):
        """Live rest-book SideView for an OPEN ORDER (venue-dispatched)."""
        if lo.rest_venue == "kalshi":
            return store.kalshi_view(lo.token, lo.kalshi_side)
        return store.poly_view(lo.token)

    def _do_rest(self, c: Any, price: float, size: float, tick: float, store: Any) -> dict:
        """Post the resting maker order on the candidate's REST venue. Returns the normalized result."""
        if c.rest_venue == "kalshi":
            return self.kalshi_order_client.rest(c.rest_ref[1], c.rest_ref[2], price, size)
        neg = self._neg_for(c.rest_ref[1], store)
        return self.order_client.rest(c.rest_ref[1], price, size, tick_size=tick, neg_risk=neg)

    def _venue_get_order(self, lo: _LiveOrder) -> Any:
        """Venue-dispatched single-order status read (for cancel-confirm + REST fill poll)."""
        if lo.rest_venue == "kalshi":
            return self.kalshi_order_client.order_status(lo.order_id)
        return self.poly.get_order(lo.order_id)

    @staticmethod
    def _order_matched(lo: _LiveOrder, o: dict) -> Any:
        """Matched quantity from a venue order dict (Poly: size_matched; Kalshi: fill_count or count-remaining)."""
        if lo.rest_venue == "kalshi":
            for k in ("fill_count", "taker_fill_count", "filled_count", "count_filled"):
                if o.get(k) is not None:
                    return float(o[k])
            cnt, rem = o.get("count"), o.get("remaining_count")
            if cnt is not None and rem is not None:
                return float(cnt) - float(rem)
            return None
        return o.get("size_matched")

    def _venue_cancel(self, lo: _LiveOrder) -> Any:
        if lo.rest_venue == "kalshi":
            return self.kalshi_order_client.cancel(lo.order_id)
        return self.order_client.cancel(lo.order_id)

    def _track_rested(self, lo: _LiveOrder) -> None:
        """Add the rested instrument to the maker-scoped reconcile watch-set (persisted for restarts)."""
        if lo.rest_venue == "kalshi":
            if lo.token not in self._traded_tickers:
                self._traded_tickers.add(lo.token)
                self._persist_traded_tokens()
        elif lo.token not in self._traded_tokens:
            self._traded_tokens.add(lo.token)
            self._persist_traded_tokens()

    # -- placement -----------------------------------------------------------
    def place_or_reprice(self, c: Any, dec: Any, rest: Any, store: Any, now: Any, now_ts: float,
                         phase: str = "pre") -> None:
        """Place (or reprice) the real GTC rest for ``c`` at ``dec.quote_price`` in ``phase``. Re-checks
        never-crossable on the LIVE book, enforces the shared caps, reprices via cancel->confirm->place."""
        armed = self.pre_armed() if phase == "pre" else self.inplay_armed()
        assert_live_allowed(phase, armed)             # HARD (cheap) re-lock immediately before any order
        token = c.rest_ref[1]
        price = float(dec.quote_price)
        tick = self._rest_tick(c, store)
        live_rest = self._rest_view_c(c, store) or rest
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
        if ok and not self._reservation_ok(c.direction):     # per-direction slot fairness (global cap passed)
            ok, reason = False, "reserve_per_direction"
        if not ok:
            self._refuse(c, phase, price, reason, projected, now_ts)
            self._record(c, "expire", now, phase, price=price, size=size, hedge_ask=hedge_ask, reason=reason)
            return
        try:
            res = self._do_rest(c, price, size, tick, store)
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
                        placed_ts=now_ts, phase=phase, best_bid=getattr(rest, "best_bid", None),
                        rest_venue=c.rest_venue,
                        kalshi_side=(c.rest_ref[2] if c.rest_venue == "kalshi" else ""))
        self.open_orders[c.key] = lo
        self._track_rested(lo)                         # reconcile this instrument for flatness (persisted)
        self.caps.on_open()
        kind = "reprice" if existing is not None else "quote"
        self._record(c, kind, now, phase, price=price, size=size, hedge_ask=hedge_ask, order_id=oid)
        self._routine("quote", f"[MAKER_RT][LIVE] {kind.upper()} {c.game} {c.market_key} {c.direction} "
                      f"[{phase}] @ {price:.4f} x{int(size)} (~${price*size:.2f}) id {oid}")
        matched = float(res.get("shares") or 0)          # a maker shouldn't fill on POST, but the book can move
        if matched > 0:
            self._on_fill_detected(c.key, matched, float(res.get("avg_price") or price), store, now, now_ts)

    def _reservation_ok(self, direction: str) -> bool:
        """True iff ``direction`` may claim one more of max_open_quotes without eating another enabled
        direction's guaranteed reserve (per-direction slot fairness). Counts the CURRENT opens per
        direction from the live order book. Off (or single direction) -> always True."""
        open_by_direction: dict = {}
        for lo in self.open_orders.values():
            open_by_direction[lo.direction] = open_by_direction.get(lo.direction, 0) + 1
        return direction_slot_ok(direction, open_by_direction, self.directions,
                                 self.caps.max_open_quotes, self.reserve_per_direction)

    def _refuse(self, c: Any, phase: str, price: float, reason: str, projected: float,
                now_ts: float) -> None:
        """Log a placement refusal. A full-slot refusal (max_open_quotes / reserve_per_direction) recurs
        every loop while the caps are full, so it is throttled to once per REFUSE_LOG_EVERY_S per
        node+direction+reason; suppressed hits are counted for the digest line instead of spamming."""
        text = (f"[MAKER_RT][LIVE] REFUSE {c.game} {c.direction} [{phase}] @ {price:.4f}: {reason} "
                f"(projected pair ${projected:.2f}, stake_today ${self.caps.stake_today:.2f})")
        if reason in _SLOT_REFUSE_REASONS:
            k = (c.key, reason)
            if now_ts - self._refuse_log_at.get(k, -1e18) < REFUSE_LOG_EVERY_S:
                self._digest["refuse_suppressed"] = self._digest.get("refuse_suppressed", 0) + 1
                return
            self._refuse_log_at[k] = now_ts
        self._routine("refuse", text)

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
            resp = self._venue_cancel(lo)
        except Exception as exc:  # noqa: BLE001
            self._alert(f"[MAKER_RT][LIVE] cancel raised for {lo.order_id}: {exc}")
        if not self._cancel_confirmed(lo, resp):
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

    def _cancel_confirmed(self, lo: _LiveOrder, resp: Any) -> bool:
        if isinstance(resp, dict):
            canceled = resp.get("canceled") or resp.get("cancelled") or []
            if lo.order_id in canceled:
                return True
        try:
            o = self._venue_get_order(lo)             # venue-dispatched status read
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
            self.poly.cancel_all()                    # Poly: every resting order on the account is ours
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] cancel_all() failed: %s", exc)
        if self.kalshi_order_client is not None:      # Kalshi: cancel our TRACKED resting orders (maker-scoped)
            try:
                self.kalshi_order_client.cancel_all()
            except Exception as exc:  # noqa: BLE001
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] kalshi cancel_all() failed: %s", exc)
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

    def on_kalshi_fill(self, event: dict, store: Any, now: Any, now_ts: float) -> None:
        """Kalshi WS 'fill' message -> detect our rest-kalshi fills by order_id. Kalshi fills arrive as
        DELTAS (contracts filled in this print), so accumulate onto matched_seen. A maker fill executes at
        our resting price (lo.price)."""
        key = self._key_for_oid(event.get("order_id"))
        if key is None:
            return
        lo = self.open_orders.get(key)
        if lo is None or lo.rest_venue != "kalshi":
            return
        count = event.get("count")
        if count in (None, ""):
            return
        total = lo.matched_seen + float(count)               # DELTA -> cumulative high-water mark
        self._on_fill_detected(key, total, lo.price, store, now, now_ts)

    def set_kalshi_feed_ok(self, ok: bool, now: Any = None) -> None:
        """Kalshi WS health (rest-kalshi FILL signal). On a DOWN transition: halt rest-kalshi placement +
        cancel open KALSHI quotes (a fill we cannot observe cannot be hedged). Pre/poly quotes unaffected."""
        was = self.kalshi_feed_ok
        self.kalshi_feed_ok = bool(ok)
        if was and not ok:
            from .state import utcnow
            self._alert("[MAKER_RT][LIVE] Kalshi WS DOWN -- cancelling open rest-kalshi quotes (fills unobservable).")
            for k, lo in list(self.open_orders.items()):
                if lo.rest_venue == "kalshi":
                    self._cancel(k, now or utcnow(), "kalshi_feed_down")
        elif ok and not was and self.log:
            self.log.info("[MAKER_RT][LIVE] Kalshi WS UP -- rest-kalshi placement enabled.")

    def poll_open_orders(self, store: Any, now: Any, now_ts: float) -> None:
        """REST backup fill detector (throttled by the caller): read each open order's size_matched and
        hedge any delta the socket missed. Also drops orders cancelled out from under us."""
        for key, lo in list(self.open_orders.items()):
            try:
                o = self._venue_get_order(lo)        # venue-dispatched (kalshi = get_orders lookup)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(o, dict) or not o:
                continue
            matched = self._order_matched(lo, o)
            status = str(o.get("status") or "").upper()
            price = o.get("price") if lo.rest_venue == "polymarket" else lo.price   # kalshi maker fills at our rest
            if matched is not None and float(matched) > lo.matched_seen + 1e-9:
                self._on_fill_detected(key, float(matched), price, store, now, now_ts)
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
        """Hedge one fill: re-verify -> DECLINE+unwind, or lift the Kalshi IOC (a miss/partial unwinds the
        UNHEDGED remainder). EVERY unwind goes through _verified_unwind (REST-confirm flat or SCREAM+HALT).
        Returns the result dict (outcome, locked_net, pnl, hedge order id, chain)."""
        self.caps.commit_stake(matched * fill_price)              # rest leg committed
        hl = lo.hedge_lookup
        hedge_venue = hl.get("venue", "kalshi")
        hv = store.kalshi_view(hl.get("ticker"), hl.get("side")) if hedge_venue == "kalshi" \
            else store.poly_view(hl.get("token"))
        re_mark = hedge_mod.mark_hedge(hv.ask_ladder, matched, hedge_venue, lo.poly_rate) if hv else None
        locked = hedge_mod.locked_net(fill_price, re_mark["cost_per_share"]) if re_mark else None
        self._record_fill(lo, matched, fill_price, now)          # ledger chain head: fill -> hedge_* -> unwind
        self._instant(f"[MAKER_RT][LIVE] FILL {lo.game} {lo.direction} [{lo.phase}] {matched:.0f}@"
                      f"{fill_price:.4f} (id {lo.order_id}) -- verifying hedge "
                      f"(locked~{'n/a' if locked is None else f'{locked*100:.2f}%'}).")
        # DECLINE: the walked hedge is too dear -> do NOT leg in; unwind the WHOLE fill (verified).
        if locked is None or locked < HEDGE_DECLINE_FLOOR:
            return self._unwind_and_record(lo, matched, fill_price, locked, "hedge_declined", now)
        # FIRE the hedge on the COMPLEMENT venue (rest_poly -> Kalshi IOC; rest_kalshi -> Poly FAK).
        if hedge_venue == "polymarket":
            res = self.hedger.hedge_poly({"price": fill_price, "size": matched},
                                         {"token": hl.get("token"), "best_ask": getattr(hv, "best_ask", None)})
        else:
            res = self.hedger.hedge({"token_id": lo.token, "side": "BUY", "price": fill_price, "size": matched},
                                    {"ticker": hl.get("ticker"), "side": hl.get("side", "yes"),
                                     "best_ask": getattr(hv, "best_ask", None)})
        status = getattr(res, "status", "error")
        hedged = float(getattr(res, "hedged_shares", 0.0) or 0.0)
        hedge_avg = getattr(res, "hedge_avg_price", None)
        _detail = getattr(res, "detail", None) or {}
        hedge_oid = ((_detail.get("kalshi") or _detail.get("poly") or {}) or {}).get("order_id") \
            if isinstance(_detail, dict) else None
        if status == "locked":
            self.caps.commit_stake(hedged * float(hedge_avg or 0.0))
            pnl = float(getattr(res, "locked_pnl", 0.0) or 0.0)
            self.caps.on_fill(pnl)
            self._record_lo(lo, "hedge_locked", now, price=fill_price, size=matched, locked_net=locked,
                            locked_pnl=pnl, hedge_avg=hedge_avg, hedge_order_id=hedge_oid)
            self._instant(f"[MAKER_RT][LIVE] HEDGE LOCKED {lo.game} [{lo.phase}] {matched:.0f} -> "
                          f"pnl ${pnl:.2f} (hedge id {hedge_oid})")
            return {"outcome": "hedge_locked", "locked_net": locked, "pnl": pnl, "hedge_order_id": hedge_oid,
                    "chain": self._chain(lo, matched, fill_price, "locked", locked, pnl, hedge_oid)}
        # MISS / PARTIAL / ERROR -> unwind the UNHEDGED remainder (verified). A partial hedge locks its part.
        if hedged > 0:
            self.caps.commit_stake(hedged * float(hedge_avg or 0.0))
        remainder = max(0.0, matched - hedged)
        return self._unwind_and_record(lo, remainder, fill_price, locked, "hedge_unwound", now,
                                       hedge_oid=hedge_oid)

    def _unwind_and_record(self, lo: _LiveOrder, shares: float, fill_price: float, locked: Optional[float],
                           ok_outcome: str, now: Any, hedge_oid: Any = None) -> dict:
        """Unwind ``shares`` of the naked fill and VERIFY the position is flat. On success record
        ``ok_outcome`` (hedge_declined | hedge_unwound). On failure record 'unwind_FAILED' + SCREAM + HALT
        all live quoting -- NO 'unwound' row is ever written without a venue-confirmed flat position."""
        if shares <= 1e-9:                                    # nothing naked (fully hedged) -> flat by construction
            self._record_lo(lo, ok_outcome, now, price=fill_price, size=0, locked_net=locked, unwind_cost=0.0)
            return {"outcome": ok_outcome, "locked_net": locked, "pnl": 0.0, "hedge_order_id": hedge_oid,
                    "chain": self._chain(lo, 0, fill_price, ok_outcome, locked, 0.0, hedge_oid)}
        u = self._verified_unwind(lo, shares, fill_price)
        if u["ok"]:
            cost = u["cost"] or 0.0
            self.caps.on_fill(-cost)
            if u["sold"] > 0 and u["sell_px"] is not None:
                self.caps.commit_stake(float(u["sell_px"]) * float(u["sold"]))
            self._record_lo(lo, ok_outcome, now, price=fill_price, size=shares, locked_net=locked,
                            unwind_cost=cost)
            self._instant(f"[MAKER_RT][LIVE] {ok_outcome.upper()} {lo.game} [{lo.phase}] -- VERIFIED FLAT; "
                          f"sold {u['sold']:.0f}@{u['sell_px']} cost ${cost:.2f} (hedge id {hedge_oid})")
            return {"outcome": ok_outcome, "locked_net": locked, "pnl": -cost, "hedge_order_id": hedge_oid,
                    "chain": self._chain(lo, shares, fill_price, ok_outcome, locked, -cost, hedge_oid)}
        # VERIFY-OR-SCREAM: the position is NOT confirmed flat -> ORPHAN. Book the worst-case loss.
        rem = u["remaining"]
        est_loss = round(float(fill_price) * float(rem if rem is not None else shares), 4)
        self.caps.on_fill(-est_loss)
        self._record_lo(lo, "unwind_FAILED", now, price=fill_price, size=shares, locked_net=locked,
                        unwind_cost=est_loss)
        self._orphan(lo, rem, u.get("sell_res"), now)
        return {"outcome": "unwind_FAILED", "locked_net": locked, "pnl": -est_loss,
                "hedge_order_id": hedge_oid,
                "chain": self._chain(lo, shares, fill_price, "unwind_FAILED", locked, -est_loss, hedge_oid)}

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

    def _verified_unwind(self, lo: _LiveOrder, shares: float, fill_price: float) -> dict:
        """VENUE-DISPATCHED verified unwind: market-sell the naked fill AND REST-verify the position is flat.
        Returns {ok, sold, sell_px, cost, remaining, sell_res}. ok is True ONLY when the position read
        confirms flat. A read failure is treated as NOT flat (fail-closed). Identical doctrine both venues."""
        if lo.rest_venue == "kalshi":
            return self._verified_unwind_kalshi(lo, shares, fill_price)
        return self._verified_unwind_poly(lo, shares, fill_price)

    def _verified_unwind_poly(self, lo: _LiveOrder, shares: float, fill_price: float) -> dict:
        poly = getattr(self.hedger, "poly", None) or self.poly
        sell_res = None
        try:
            sell_res = poly.place_market_sell(lo.token, shares)
        except Exception as exc:  # noqa: BLE001
            sell_res = {"status": "error", "error": str(exc)}
        sold = float((sell_res or {}).get("shares") or 0.0) if isinstance(sell_res, dict) else 0.0
        sell_px = (sell_res or {}).get("avg_price") if isinstance(sell_res, dict) else None
        remaining = None
        try:                                                     # SOURCE OF TRUTH (not the sell response);
            # settlement is NOT instant -- poll (forcing a re-sync) so a stale PRE-sell balance can't
            # falsely read non-flat and scream unwind_FAILED. Stops as soon as it reads <= 0.5.
            if hasattr(poly, "settle_conditional_balance"):
                remaining = poly.settle_conditional_balance(lo.token, lambda b: b <= 0.5)
            else:                                                # test doubles: plain read
                remaining = poly.conditional_balance(lo.token)
        except Exception as exc:  # noqa: BLE001 - read failed -> UNKNOWN -> fail closed
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] position read failed for %s: %s", lo.token[:12], exc)
            remaining = None
        flat = remaining is not None and remaining <= 0.5
        cost = round((float(fill_price) - float(sell_px)) * sold, 4) if (sell_px is not None and sold > 0) else None
        return {"ok": bool(flat), "sold": sold, "sell_px": sell_px, "cost": cost,
                "remaining": remaining, "sell_res": sell_res}

    def _verified_unwind_kalshi(self, lo: _LiveOrder, shares: float, fill_price: float) -> dict:
        """IOC-sell the naked Kalshi position, then REST-verify FLAT via the portfolio positions endpoint."""
        n = int(round(float(shares)))
        sell_res = None
        try:
            sell_res = self.kalshi.place_market_sell(lo.token, lo.kalshi_side, n)
        except Exception as exc:  # noqa: BLE001
            sell_res = {"status": "error", "error": str(exc)}
        sold = float((sell_res or {}).get("fill_count") or 0.0) if isinstance(sell_res, dict) else 0.0
        sell_px = (sell_res or {}).get("avg_price") if isinstance(sell_res, dict) else None
        remaining = None
        try:
            remaining = self._settle_kalshi_flat(lo.token)      # SOURCE OF TRUTH — portfolio positions
        except Exception as exc:  # noqa: BLE001 - read failed -> UNKNOWN -> fail closed
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] kalshi position read failed for %s: %s", lo.token, exc)
            remaining = None
        flat = remaining is not None and abs(remaining) <= 0.5
        cost = round((float(fill_price) - float(sell_px)) * sold, 4) if (sell_px is not None and sold > 0) else None
        return {"ok": bool(flat), "sold": sold, "sell_px": sell_px, "cost": cost,
                "remaining": remaining, "sell_res": sell_res}

    def _kalshi_position(self, ticker: str) -> float:
        """Net |contracts| held on ``ticker`` from the Kalshi portfolio positions endpoint (0 if flat/absent)."""
        resp = self.kalshi.get_positions()
        rows = resp.get("market_positions") if isinstance(resp, dict) else (resp or [])
        if not rows and isinstance(resp, dict):
            rows = resp.get("positions") or resp.get("data") or []
        for p in rows or []:
            if not isinstance(p, dict):
                continue
            if p.get("ticker") == ticker or p.get("market_ticker") == ticker:
                for k in ("position", "net_position", "count"):
                    if p.get(k) is not None:
                        return abs(float(p[k]))
                return 0.0
        return 0.0

    def _settle_kalshi_flat(self, ticker: str, *, tries: int = 4, poll_s: float = 0.5) -> Optional[float]:
        """Poll the Kalshi position until it reads flat (<= 0.5) or ``tries`` exhausted — mirrors the Poly
        settle poll so a brief post-sell lag can't falsely scream unwind_FAILED. Returns the last read."""
        import time as _time
        last: Optional[float] = None
        for i in range(max(1, tries)):
            try:
                last = self._kalshi_position(ticker)
            except Exception:  # noqa: BLE001
                last = None
            if last is not None and abs(last) <= 0.5:
                return last
            if i < tries - 1:
                _time.sleep(poll_s)
        return last

    def _orphan(self, lo: _LiveOrder, remaining: Any, sell_res: Any, now: Any) -> None:
        self._orphan_detected(lo.game, lo.token, lo.phase, remaining, f"unwind_FAILED sell_res={sell_res}", now)

    def _orphan_detected(self, game: str, token: str, phase: str, remaining: Any, detail: str,
                         now: Any) -> None:
        """A naked ORPHAN (unwind not confirmed flat, or a reconciliation mismatch). HALT ALL live quoting,
        cancel opens, SCREAM (CRITICAL Telegram). Latched until manually cleared. The red panel banner +
        the 5-min reconciliation loop surface it — this is what makes the class self-detecting."""
        if self.orphan is not None:                          # already latched -> don't re-scream every loop
            return
        try:
            detected = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            detected = ""
        self.orphan = {"game": game, "token": token, "remaining": remaining, "phase": phase,
                       "detected": detected, "detail": detail}
        self.caps.halted = True
        self.caps.halt_reason = "orphan_position"
        self._instant(f"[MAKER_RT][CRITICAL] ORPHAN POSITION {game} [{phase}] token {str(token)[:12]}... "
                      f"remaining={remaining} ({detail}). HALTING all live quoting + cancelling opens. "
                      f"MANUAL CHECK REQUIRED.")
        self.cancel_all("orphan_halt", now)

    def _load_traded_tokens(self) -> None:
        """Reload the persisted watch-set at startup (best-effort; a corrupt/locked file never blocks
        startup). This is what lets the startup reconcile catch a position stranded by a crashed run."""
        import json
        if not self._traded_path or not os.path.exists(self._traded_path):
            return
        try:
            with open(self._traded_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):                       # legacy bare list = poly tokens only
                self._traded_tokens.update(str(t) for t in data if t)
            elif isinstance(data, dict):
                self._traded_tokens.update(str(t) for t in (data.get("tokens") or []) if t)
                self._traded_tickers.update(str(t) for t in (data.get("tickers") or []) if t)
        except Exception:  # noqa: BLE001
            pass

    def _persist_traded_tokens(self) -> None:
        """Atomically write the watch-set (poly tokens + kalshi tickers). Best-effort: persistence must
        NEVER crash live trading."""
        import json
        if not self._traded_path:
            return
        try:
            tmp = self._traded_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"tokens": sorted(self._traded_tokens),
                           "tickers": sorted(self._traded_tickers)}, fh)
            os.replace(tmp, self._traded_path)
        except Exception:  # noqa: BLE001
            pass

    def reconcile_positions(self, now: Any) -> Optional[dict]:
        """Read ACTUAL positions and diff against the bot's belief (flat -- a resting order is NOT a
        position; every fill is hedged or unwound-to-flat). Any non-zero holding on a token WE TRADED
        this run is an ORPHAN. Runs at startup + every 5 min while armed. If ``auto_flatten`` it also
        market-sells the orphan to flat (default False -> halt + scream only). Returns the orphan or None.

        Scoped to ``self._traded_tokens`` ON PURPOSE: this funder wallet holds hundreds of unrelated
        positions (other bots/markets -- Bitcoin up/down, other sports, etc.). A blanket list_positions()
        sweep would flag EVERY one of those as an orphan and instantly halt live quoting on a false
        positive. Only the tokens this maker actually placed on can be a maker orphan."""
        if self.orphan is not None:
            return self.orphan
        open_toks = {lo.token for lo in self.open_orders.values()}
        suspects: dict = {}
        flat_toks: list = []
        for tok in list(self._traded_tokens):                # POLY: reliable per-token read (CLOB), maker-scoped
            try:
                bal = self.poly.conditional_balance(tok)
            except Exception:  # noqa: BLE001 - unknown -> keep watching (don't prune on a read failure)
                continue
            if bal is not None and bal > 0.5:
                suspects[tok] = bal
            elif bal is not None and tok not in open_toks:   # confirmed flat + not actively quoting -> drop
                flat_toks.append(tok)
        if flat_toks:                                        # bound the watch-set to open + non-flat tokens
            self._traded_tokens.difference_update(flat_toks)
            self._persist_traded_tokens()
        flat_tks: list = []
        for tk in list(self._traded_tickers):                # KALSHI: maker-scoped portfolio position read
            try:
                pos = self._kalshi_position(tk)
            except Exception:  # noqa: BLE001
                continue
            if pos is not None and abs(pos) > 0.5:
                suspects[tk] = pos
            elif pos is not None and tk not in open_toks:
                flat_tks.append(tk)
        if flat_tks:
            self._traded_tickers.difference_update(flat_tks)
            self._persist_traded_tokens()
        if not suspects:
            return None
        tok, bal = next(iter(suspects.items()))
        if self.auto_flatten and tok in self._traded_tokens:  # optional flatten (POLY only; kalshi needs a side)
            try:
                self.poly.place_market_sell(tok, bal)
                bal2 = self.poly.conditional_balance(tok)
                if bal2 is not None and bal2 <= 0.5:
                    self._instant(f"[MAKER_RT][CRITICAL] reconciliation auto-flattened orphan {str(tok)[:12]}... "
                                  f"({bal} sh); position now flat. HALTING for manual review anyway.")
            except Exception as exc:  # noqa: BLE001
                self._instant(f"[MAKER_RT][CRITICAL] reconciliation auto-flatten FAILED {str(tok)[:12]}...: {exc}")
        self._orphan_detected("reconciliation", tok, "?", bal,
                              f"{len(suspects)} orphan instrument(s); auto_flatten={self.auto_flatten}", now)
        return self.orphan

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
                "inplay_halted": self.inplay_halted, "orphan": self.orphan,
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
                   hedge_avg: float = None, hedge_order_id: Any = None, reason: str = "") -> None:
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
        if hedge_order_id is not None:
            row["hedge_order_id"] = hedge_order_id
        # REALIZED pnl ($): a locked hedge's pnl, else the NEGATIVE of the unwind/orphan cost.
        realized = locked_pnl if locked_pnl is not None else (
            -float(unwind_cost) if unwind_cost is not None else None)
        if realized is not None:
            row["realized_pnl_usd"] = round(float(realized), 4)
        for k, v in (("locked_pnl", locked_pnl), ("unwind_cost", unwind_cost)):
            if v is not None:
                row[k] = v
        self.state.record(row, now)

    def _record_fill(self, lo: _LiveOrder, matched: float, fill_price: float, now: Any) -> None:
        """The head of the ledger chain: a live 'fill' row (fill -> hedge_* -> unwind|unwind_FAILED)."""
        if self.state is None:
            return
        self.state.record({"event": "fill", "mode": "live", "sport": lo.sport, "phase": lo.phase,
                           "game": lo.game, "market_key": lo.market_key, "side": lo.side,
                           "direction": lo.direction, "quote_price": round(float(fill_price), 4),
                           "size": round(float(matched), 2), "reason": lo.order_id}, now)

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
        suppressed = d.get("refuse_suppressed", 0)
        if d["quotes"] or total or d["fills"] or suppressed:
            reasons = ", ".join(f"{v} {k}" for k, v in sorted(d["cancels"].items(), key=lambda x: -x[1]))
            line = (f"[MAKER_RT][DIGEST {int(self.digest_min)}m] {d['quotes']} quotes, {total} cancels"
                    f"{' [' + reasons + ']' if reasons else ''}, {d['fills']} fills"
                    f"{f', {suppressed} slot-refuses suppressed' if suppressed else ''}, "
                    f"open {len(self.open_orders)}")
            self._send_telegram(line)
            if self.log:
                self.log.warning(line)
        self._digest = {"quotes": 0, "cancels": {}, "fills": 0, "refuse_suppressed": 0}
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
