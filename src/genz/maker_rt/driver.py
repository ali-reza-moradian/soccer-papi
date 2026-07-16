"""Quote driver — the event-driven glue over the pure engine.

For every universe market it evaluates BOTH rest directions on BOTH outcomes (rest on Poly hedge on
Kalshi, and vice-versa), arms/reprices/expires shadow quotes in the fill model, and on public trade
prints records shadow fills, marks the fee-inclusive hedge on the live book, and schedules
adverse-selection drift reads at +1/+5/+30s. It NEVER places an order (that is the live hedger, gated
elsewhere). Debounced calls come from the async loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import hedge as hedge_mod
from .fills import ShadowFillModel
from .quotes import compute_quote, needs_reprice


@dataclass
class Candidate:
    key: tuple
    sport: str
    game: str
    market_key: str
    rest_side: str
    direction: str            # "rest-poly" | "rest-kalshi"
    rest_ref: tuple
    rest_venue: str
    hedge_venue: str
    tick: float
    hedge_tick: float
    poly_rate: float
    hedge_lookup: dict        # how to fetch the hedge SideView from the store at any moment
    kickoff_ts: float


class QuoteDriver:
    def __init__(self, cfg: Any, state: Any, *, log: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.log = log
        self.fills = ShadowFillModel()
        self.universe: list = []
        self.prev: dict = {}                 # key -> last QuoteDecision (reprice detection)
        self.last_event: dict = {}           # key -> last emitted event kind (dedupe per-tick spam)
        self.drift_pending: list = []        # [{key, hedge_lookup, fill_ts, marks:{}}]
        self._cands: list = []

    # -- universe ----------------------------------------------------------
    def set_universe(self, universe: list) -> None:
        self.universe = universe
        self._cands = [c for qm in universe for c in self._candidates(qm)]

    def _candidates(self, qm: Any) -> list:
        out: list = []
        sides = list(qm.sides.items())
        if len(sides) != 2:
            return out
        for i in (0, 1):
            rest_side, rest_node = sides[i]
            hedge_node = sides[1 - i][1]
            # rest on POLY, hedge on KALSHI
            if rest_node.poly_token_id and hedge_node.kalshi_ticker:
                out.append(Candidate(
                    key=(qm.sport, qm.game, qm.market_key, rest_side, "rest-poly"),
                    sport=qm.sport, game=qm.game, market_key=qm.market_key, rest_side=rest_side,
                    direction="rest-poly", rest_ref=("polymarket", rest_node.poly_token_id, "BUY"),
                    rest_venue="polymarket", hedge_venue="kalshi", tick=0.01, hedge_tick=0.01,
                    poly_rate=self.cfg.poly_rate if hasattr(self.cfg, "poly_rate") else self.cfg.poly_fee_rate,
                    hedge_lookup={"venue": "kalshi", "ticker": hedge_node.kalshi_ticker,
                                  "side": hedge_node.kalshi_side},
                    kickoff_ts=qm.kickoff_ts))
            # rest on KALSHI, hedge on POLY (skip if this series charges a maker fee)
            series = str(rest_node.kalshi_ticker or "").split("-", 1)[0]
            maker_fee = series in getattr(self.cfg, "kalshi_maker_fee_series", ())
            if rest_node.kalshi_ticker and hedge_node.poly_token_id and not maker_fee:
                out.append(Candidate(
                    key=(qm.sport, qm.game, qm.market_key, rest_side, "rest-kalshi"),
                    sport=qm.sport, game=qm.game, market_key=qm.market_key, rest_side=rest_side,
                    direction="rest-kalshi", rest_ref=("kalshi", rest_node.kalshi_ticker, rest_node.kalshi_side),
                    rest_venue="kalshi", hedge_venue="polymarket", tick=0.01, hedge_tick=0.01,
                    poly_rate=float(hedge_node.poly_fee_rate or self.cfg.poly_fee_rate),
                    hedge_lookup={"venue": "polymarket", "token": hedge_node.poly_token_id},
                    kickoff_ts=qm.kickoff_ts))
        return out

    # -- lookups -----------------------------------------------------------
    def _rest_view(self, store: Any, c: Candidate):
        if c.rest_venue == "polymarket":
            return store.poly_view(c.rest_ref[1])
        return store.kalshi_view(c.rest_ref[1], c.rest_ref[2])

    def _hedge_view(self, store: Any, lookup: dict):
        if lookup.get("venue") == "polymarket":
            return store.poly_view(lookup.get("token"))
        return store.kalshi_view(lookup.get("ticker"), lookup.get("side"))

    # -- quote refresh -----------------------------------------------------
    def refresh_quotes(self, store: Any, now: Any, now_ts: float) -> None:
        """Re-evaluate every candidate against the current books; arm/reprice/expire + record events."""
        for c in self._cands:
            rest = self._rest_view(store, c)
            hedge = self._hedge_view(store, c.hedge_lookup)
            if rest is None or hedge is None:
                self._expire_if_open(c, now, "book_gone")
                self.last_event[c.key] = None
                continue
            tick = store.poly_tick(c.rest_ref[1]) if c.rest_venue == "polymarket" else c.tick
            hedge_tick = store.poly_tick(c.hedge_lookup.get("token"), 0.01) if c.hedge_venue == "polymarket" else 0.01
            dec = compute_quote(rest, hedge, hedge_venue=c.hedge_venue, tick=tick,
                                target_net=self.cfg.target_net, quote_usd=self.cfg.quote_usd,
                                poly_rate=c.poly_rate, hedge_tick=hedge_tick)
            prev = self.prev.get(c.key)
            self.prev[c.key] = dec
            if dec.would_be_behind:
                if c.key in self.fills.quotes:
                    self._expire_if_open(c, now, "now_behind")
                # DEDUPE: only record a 'behind' row on the TRANSITION into behind (or a >=1-tick move
                # while behind) — never once per 250ms tick, which would flood the CSV/summary.
                if self.last_event.get(c.key) != "behind" or needs_reprice(prev, dec, tick):
                    self.state.record(self._row("behind", c, now, dec, tick=tick), now)
                    self.last_event[c.key] = "behind"
                continue
            if not dec.viable:
                self._expire_if_open(c, now, dec.reason)
                self.last_event[c.key] = None
                continue
            was_open = c.key in self.fills.quotes
            if was_open and not needs_reprice(prev, dec, tick):
                continue                                  # unchanged within a tick
            queue_ahead = rest.bid_size_at(dec.quote_price)
            self.fills.arm(c.key, c.rest_ref, dec.quote_price, dec.size_shares, queue_ahead,
                           dec.at_best, now_ts, hedge_ctx={"lookup": c.hedge_lookup,
                           "hedge_venue": c.hedge_venue, "poly_rate": c.poly_rate})
            row = self._row("reprice" if was_open else "quote", c, now, dec, tick=tick,
                            queue_ahead=queue_ahead)
            self.state.record(row, now)
            self.last_event[c.key] = "quote"
            if self.log and not was_open:
                self.log.info("[MAKER_RT] QUOTE %s %s %s @ %.3f (floor %.3f, hedge_ask %.3f, net %.3f%%, "
                              "at_best=%s, q_ahead=%.0f)", c.game, c.market_key, c.direction,
                              dec.quote_price, dec.floor or 0, dec.hedge_best_ask or 0,
                              (dec.net_at_quote or 0) * 100, dec.at_best, queue_ahead)

    def _expire_if_open(self, c: Candidate, now: Any, reason: str) -> None:
        if c.key in self.fills.quotes:
            self.fills.disarm(c.key)
            self.state.record({"event": "expire", "mode": "shadow", "sport": c.sport, "game": c.game,
                               "market_key": c.market_key, "side": c.rest_side, "direction": c.direction,
                               "reason": reason}, now)

    # -- fills (shadow) ----------------------------------------------------
    def consume_prints(self, prints: list, store: Any, now: Any, now_ts: float) -> None:
        for rest_ref, price, volume in prints or []:
            for fe in self.fills.consume_print(rest_ref, price, volume, now_ts):
                self._on_shadow_fill(fe, store, now, now_ts)

    def _on_shadow_fill(self, fe: Any, store: Any, now: Any, now_ts: float) -> None:
        ctx = fe.hedge_ctx or {}
        hv = self._hedge_view(store, ctx.get("lookup", {}))
        mark = hedge_mod.mark_hedge(hv.ask_ladder, fe.size, ctx.get("hedge_venue", "kalshi"),
                                    ctx.get("poly_rate", self.cfg.poly_fee_rate)) if hv else None
        locked = hedge_mod.locked_net(fe.quote_price, mark["cost_per_share"]) if mark else None
        sport, game, mkey, side, direction = fe.key
        row = {"event": "fill", "mode": "shadow", "sport": sport, "game": game, "market_key": mkey,
               "side": side, "direction": direction, "quote_price": round(fe.quote_price, 4),
               "size": round(fe.size, 2), "at_best": fe.at_best, "trigger": fe.trigger,
               "quote_age_s": round(fe.quote_age_s, 2),
               "hedge_avg": round(mark["avg_price"], 4) if mark else "",
               "hedge_fee": round(mark["fee"], 4) if mark else "",
               "locked_net": round(locked * 100, 4) if locked is not None else None,
               "locked_pnl": round(locked * fe.size, 4) if locked is not None else None}
        self.state.record(row, now)
        if self.log:
            self.log.info("[MAKER_RT] SHADOW FILL %s %s %s @ %.3f (%s, age %.1fs) -> locked net %s%%",
                          game, mkey, direction, fe.quote_price, fe.trigger, fe.quote_age_s,
                          f"{locked*100:.2f}" if locked is not None else "n/a")
        self.drift_pending.append({"key": fe.key, "lookup": ctx.get("lookup", {}),
                                   "fill_ts": now_ts, "marks": {}, "sport": sport, "game": game,
                                   "market_key": mkey, "side": side, "direction": direction})

    def process_drift(self, store: Any, now: Any, now_ts: float) -> None:
        marks = tuple(self.cfg.drift_marks_s)
        still: list = []
        for d in self.drift_pending:
            age = now_ts - d["fill_ts"]
            hv = self._hedge_view(store, d["lookup"])
            mid = hedge_mod.hedge_mid(hv) if hv else None
            for m in marks:
                if m not in d["marks"] and age >= m:
                    d["marks"][m] = mid
            if age >= max(marks):
                self.state.record({"event": "drift", "mode": "shadow", "sport": d["sport"],
                                   "game": d["game"], "market_key": d["market_key"], "side": d["side"],
                                   "direction": d["direction"],
                                   "drift_1": d["marks"].get(marks[0]), "drift_5": d["marks"].get(marks[1]),
                                   "drift_30": d["marks"].get(marks[-1])}, now)
            else:
                still.append(d)
        self.drift_pending = still

    def expire_kickoff(self, now: Any, now_ts: float) -> None:
        cutoff = self.cfg.expire_before_kickoff_s
        for c in self._cands:
            if now_ts >= c.kickoff_ts - cutoff and c.key in self.fills.quotes:
                self._expire_if_open(c, now, "kickoff_window")

    def open_quote_count(self) -> int:
        return len(self.fills.quotes)

    def _row(self, event: str, c: Candidate, now: Any, dec: Any, *, tick: float = 0.01,
             queue_ahead: Optional[float] = None) -> dict:
        return {"event": event, "mode": "shadow", "sport": c.sport, "game": c.game,
                "market_key": c.market_key, "side": c.rest_side, "direction": c.direction,
                "rest_venue": c.rest_venue, "hedge_venue": c.hedge_venue,
                "quote_price": round(dec.quote_price, 4) if dec.quote_price is not None else "",
                "size": round(dec.size_shares, 2) if dec.size_shares is not None else "",
                "floor": round(dec.floor, 4) if dec.floor is not None else "",
                "at_best": dec.at_best, "hedge_ask": round(dec.hedge_best_ask, 4) if dec.hedge_best_ask else "",
                "net_at_quote": round(dec.net_at_quote * 100, 4) if dec.net_at_quote is not None else "",
                "queue_ahead": round(queue_ahead, 2) if queue_ahead is not None else "",
                "reason": dec.reason}
