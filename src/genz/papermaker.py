"""PAPER MAKER (dry-run) — measure the MAKER-side edge with SYNTHETIC quotes.

This NEVER places an order and NEVER touches the executor or any order API. It rests IMAGINARY maker
bids and asks whether the market would have filled them, then marks the hedge it WOULD have taken — to
measure whether a passive (maker-fee-free) leg + an aggressive hedge leg clears a target net edge.

For each priced 2-way node it evaluates BOTH maker directions:
    rest-on-POLY   (maker fee 0) + hedge-take KALSHI (fee 0.07*p*(1-p))
    rest-on-KALSHI (maker fee 0) + hedge-take POLY   (fee poly_rate*min(p,1-p), the node's rate)

Economics (per share; each share pays $1 if its outcome wins, the two outcomes are complementary):
    net = 1 - rest_price - hedge_ask - hedge_fee(hedge_ask)
The FLOOR is the highest tick-aligned rest price that still nets >= target_net:
    floor = floor_to_tick( 1 - hedge_ask - hedge_fee(hedge_ask) - target_net )

FILL RULE (conservative — we cannot know queue position): a quote counts FILLED only when the rest
venue's best ASK later drops STRICTLY BELOW quote_price (the market traded through our level). 20s
sampling misses fast wicks, so the measured fill rate is a LOWER BOUND. Everything is labeled PAPER.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .. import bookmath
from . import config as gz_config

DEFAULT_TARGET_NET_PCT = 1.0
DEFAULT_REF_SHARES = 100.0
DEFAULT_TICK = 0.01
DEFAULT_POLY_FEE_RATE = 0.05
DRIFT_CYCLES = (1, 5)                 # measure rest-side mid this many cycles after a fill

CSV_COLUMNS = [
    "ts", "day", "event", "game", "market_key", "side", "direction",
    "rest_venue", "hedge_venue", "quote_price", "floor", "at_best",
    "hedge_ask", "rest_ask", "combo_net_pct",
    "hedge_price_quote", "hedge_price_fill", "hedge_slippage_c",
    "drift_mid_1", "drift_mid_5", "reason",
]


# --------------------------------------------------------------------------- #
# Pure economics (unit-tested against the confirmed fee formulas)                #
# --------------------------------------------------------------------------- #
def hedge_fee_rate(venue: str, price: float, poly_rate: float = DEFAULT_POLY_FEE_RATE) -> float:
    """Per-share TAKER fee for the aggressive hedge leg: Kalshi 0.07*p*(1-p); Polymarket
    poly_rate*min(p,1-p). Any other venue -> 0."""
    p = min(max(float(price), 0.0), 1.0)
    if venue == "kalshi":
        return 0.07 * p * (1.0 - p)
    if venue == "polymarket":
        return poly_rate * min(p, 1.0 - p)
    return 0.0


def _floor_to_tick(x: float, tick: float) -> float:
    tick = tick or DEFAULT_TICK
    import math
    return round(math.floor(x / tick + 1e-9) * tick, 6)


def _round_to_tick(x: float, tick: float) -> float:
    tick = tick or DEFAULT_TICK
    return round(round(x / tick) * tick, 6)


def floor_price(hedge_ask: float, hedge_venue: str, *, poly_rate: float = DEFAULT_POLY_FEE_RATE,
                target_net_pct: float = DEFAULT_TARGET_NET_PCT, tick: float = DEFAULT_TICK) -> float:
    """The highest TICK-ALIGNED rest price at which resting + hedging still nets >= target_net_pct."""
    raw = 1.0 - hedge_ask - hedge_fee_rate(hedge_venue, hedge_ask, poly_rate) - target_net_pct / 100.0
    return _floor_to_tick(raw, tick)


def combo_net(rest_price: float, hedge_ask: float, hedge_venue: str,
              poly_rate: float = DEFAULT_POLY_FEE_RATE) -> float:
    """Net edge (fraction) of the maker combo at a given rest price and hedge ask (NET of the hedge
    taker fee): 1 - rest_price - hedge_ask - hedge_fee."""
    return 1.0 - rest_price - hedge_ask - hedge_fee_rate(hedge_venue, hedge_ask, poly_rate)


def quote_from_floor(floor: float, best_bid: Optional[float], tick: float = DEFAULT_TICK) -> Optional[float]:
    """The synthetic quote price: JOIN/IMPROVE the current best bid (best_bid + one tick) but NEVER
    above the economics floor. None if the floor is below the minimum tick (no viable quote)."""
    if floor < 0.01:
        return None
    if best_bid is None:
        q = floor
    else:
        q = min(floor, _round_to_tick(best_bid + tick, tick))
    return q if q >= 0.01 - 1e-9 else None


def is_filled(quote_price: float, observed_ask: Optional[float]) -> bool:
    """Conservative fill: TRUE only when the rest venue's best ask has traded STRICTLY BELOW our quote
    (ask == quote is NOT a fill — we can't know our queue position)."""
    return observed_ask is not None and observed_ask < quote_price - 1e-9


def mark_hedge(ladder: list, ref_shares: float, hedge_venue: str,
               poly_rate: float = DEFAULT_POLY_FEE_RATE) -> Optional[dict[str, float]]:
    """Mark the hedge WE WOULD have taken off ``ladder`` for ``ref_shares``: walk the book (VWAP) and
    add that venue's taker fee. Returns {avg_price, shares, fee, cost, cost_per_share} or None if empty."""
    w = bookmath.walk_book(bookmath.valid_asks(ladder), ref_shares)
    if w.filled <= 0:
        return None
    fee = hedge_fee_rate(hedge_venue, w.avg_price, poly_rate) * w.filled
    return {"avg_price": w.avg_price, "shares": w.filled, "fee": fee,
            "cost": w.cost + fee, "cost_per_share": (w.cost + fee) / w.filled}


# --------------------------------------------------------------------------- #
# Per-quote state + the cycle-driven state machine                               #
# --------------------------------------------------------------------------- #
@dataclass
class Quote:
    key: tuple                        # (game, market_key, side, direction)
    game: str
    market_key: str
    side: str
    direction: str                    # "rest-poly" | "rest-kalshi"
    rest_venue: str
    hedge_venue: str
    quote_price: float
    floor: float
    at_best: bool
    hedge_ask_at_quote: float
    tick: float
    hedge_poly_rate: float            # poly rate to apply if hedging on Polymarket (0 if fees off)
    rest_poly_token: Optional[str] = None
    rest_kalshi_ticker: Optional[str] = None
    rest_kalshi_side: Optional[str] = None


@dataclass
class PaperMaker:
    """Cycle-driven synthetic maker. observe() is called once per genz cycle with the pre-game markets,
    the priced ask ladders, and the book source; it emits quote/fill/expire events and refreshes the
    daily CSV + summary. All state is in-memory (reset at day rollover); nothing is ever placed."""
    target_net_pct: float = DEFAULT_TARGET_NET_PCT
    ref_shares: float = DEFAULT_REF_SHARES
    poly_rate: float = DEFAULT_POLY_FEE_RATE
    quotes: dict[tuple, Quote] = field(default_factory=dict)
    drift_pending: list[dict] = field(default_factory=list)   # fills awaiting +1/+5 cycle mid readings
    day: str = ""
    cycle_no: int = 0
    # daily aggregates
    n_quotes: int = 0
    n_fills: int = 0
    n_expired_unfilled: int = 0
    at_best_hits: int = 0
    fill_nets: list[float] = field(default_factory=list)      # net_pct at each fill
    hedge_slippages_c: list[float] = field(default_factory=list)

    def _reset_day(self, day: str) -> None:
        self.day = day
        self.quotes.clear()
        self.drift_pending.clear()
        self.cycle_no = 0
        self.n_quotes = self.n_fills = self.n_expired_unfilled = self.at_best_hits = 0
        self.fill_nets = []
        self.hedge_slippages_c = []

    def observe(self, markets, priced: dict, md: Any, now: datetime, log: Any = None,
                *, csv_path: Optional[str] = None, summary_path: Optional[str] = None) -> None:
        """One paper-maker cycle. ``markets`` are the PRE-GAME 2-way markets (started games already gated
        out upstream); their quotes are expired here. Drop-safe: any error is logged, never raised."""
        try:
            self._observe(markets, priced, md, now, log, csv_path, summary_path)
        except Exception as exc:  # noqa: BLE001 - a measurement tool must never break the cycle
            if log:
                log.warning("[PAPER] paper-maker cycle error (%s) — skipped.", exc)

    def _observe(self, markets, priced, md, now, log, csv_path, summary_path) -> None:
        day = now.strftime("%Y%m%d")
        if day != self.day:
            self._reset_day(day)
        self.cycle_no += 1
        events: list[dict] = []
        alive: set[tuple] = set()

        for m in markets or []:
            sides = list(getattr(m, "sides", {}).items())
            if len(sides) != 2:
                continue
            (sa, na), (sb, nb) = sides
            for rest_side, rnode, cnode in ((sa, na, nb), (sb, nb, na)):
                for direction in ("rest-poly", "rest-kalshi"):
                    evt = self._one(m, rest_side, rnode, cnode, direction, priced, md, now, log)
                    if evt is not None:
                        key = evt["_key"]
                        alive.add(key)
                        if evt.get("event"):
                            events.append(evt)

        # Expire quotes not re-observed this cycle (their game started, or the node vanished).
        for key in list(self.quotes):
            if key not in alive:
                q = self.quotes.pop(key)
                self.n_expired_unfilled += 1
                events.append(self._row("expire", now, q, reason="kickoff / node gone"))

        events.extend(self._process_drift(priced, md, now))

        if events and csv_path:
            self._append_csv(events, csv_path)
        if summary_path:
            self._write_summary(summary_path, now)
        if log:
            log.info("[PAPER] cycle %d: %d live quote(s), %d fill(s) today, fill-rate %.0f%% (LOWER BOUND).",
                     self.cycle_no, len(self.quotes), self.n_fills,
                     100.0 * self.n_fills / self.n_quotes if self.n_quotes else 0.0)

    def _one(self, m, rest_side, rnode, cnode, direction, priced, md, now, log) -> Optional[dict]:
        key = (m.game, m.market_key, rest_side, direction)
        rest_venue = "polymarket" if direction == "rest-poly" else "kalshi"
        hedge_venue = "kalshi" if direction == "rest-poly" else "polymarket"
        rest_pv = _pv(priced, rest_venue, rnode)                # rest side, our own outcome
        hedge_pv = _pv(priced, hedge_venue, cnode)              # hedge = the COMPLEMENT outcome
        if rest_pv is None or hedge_pv is None or not hedge_pv.ladder or hedge_pv.best_ask is None:
            return {"_key": key}                                # keep alive (still pre-game) but no quote
        hedge_ask = hedge_pv.ladder[0][0]
        # tick: the rest venue's price grid (Polymarket carries tick_size on the node; Kalshi is $0.01).
        tick = float(rnode.get("tick_size") or DEFAULT_TICK) if rest_venue == "polymarket" else DEFAULT_TICK
        if hedge_venue == "polymarket":                        # poly hedge fee uses the complement node's rate
            prate = float(cnode.get("poly_fee_rate") or self.poly_rate) if cnode.get("poly_fee_enabled") else 0.0
        else:
            prate = self.poly_rate                             # unused by the kalshi hedge fee
        floor = floor_price(hedge_ask, hedge_venue, poly_rate=prate, target_net_pct=self.target_net_pct, tick=tick)
        rest_ask = rest_pv.best_ask

        # FILL check for an existing quote (rest ask traded strictly through it).
        existing = self.quotes.get(key)
        if existing is not None and is_filled(existing.quote_price, rest_ask):
            self.quotes.pop(key)
            return self._on_fill(existing, hedge_pv, hedge_ask, now, log)

        # No viable quote: floor below tick, or floor crosses the rest venue's own ask.
        if floor < 0.01 or (rest_ask is not None and floor >= rest_ask - 1e-9):
            if existing is not None:                            # a previously-live quote is now unviable
                self.quotes.pop(key)
                self.n_expired_unfilled += 1
                return self._row("expire", now, existing, reason="floor gone / crosses ask")
            return {"_key": key}

        best_bid = _bid_for(rest_pv, md, rest_venue, rnode)
        qp = quote_from_floor(floor, best_bid, tick)
        if qp is None:
            return {"_key": key}
        at_best = best_bid is not None and qp >= best_bid - 1e-9

        if existing is not None and abs(existing.floor - floor) < tick - 1e-9 and existing.quote_price == qp:
            return {"_key": key}                                # unchanged within a tick -> no re-quote

        q = Quote(key=key, game=m.game, market_key=m.market_key, side=rest_side, direction=direction,
                  rest_venue=rest_venue, hedge_venue=hedge_venue, quote_price=qp, floor=floor,
                  at_best=at_best, hedge_ask_at_quote=hedge_ask, tick=tick, hedge_poly_rate=prate,
                  rest_poly_token=rnode.get("poly_token_id"), rest_kalshi_ticker=rnode.get("kalshi_ticker"),
                  rest_kalshi_side=rnode.get("kalshi_side"))
        self.quotes[key] = q
        self.n_quotes += 1
        if at_best:
            self.at_best_hits += 1
        ev = self._row("requote" if existing is not None else "quote", now, q)
        ev["rest_ask"] = _r(rest_ask)
        return ev

    def _on_fill(self, q: Quote, hedge_pv, hedge_ask_now, now, log) -> dict:
        self.n_fills += 1
        marked = mark_hedge(hedge_pv.ladder, self.ref_shares, q.hedge_venue, q.hedge_poly_rate)
        hedge_cost_ps = marked["cost_per_share"] if marked else hedge_ask_now
        net_pct = (1.0 - q.quote_price - hedge_cost_ps) * 100.0   # hedge cost already includes the taker fee
        self.fill_nets.append(net_pct)
        slippage_c = (hedge_ask_now - q.hedge_ask_at_quote) * 100.0
        self.hedge_slippages_c.append(slippage_c)
        # schedule adverse-selection drift readings on the rest side
        self.drift_pending.append({"q": q, "fill_cycle": self.cycle_no, "mids": {}})
        ev = self._row("fill", now, q)
        ev.update(combo_net_pct=_r(net_pct), hedge_ask=_r(hedge_ask_now),
                  hedge_price_quote=_r(q.hedge_ask_at_quote), hedge_price_fill=_r(hedge_ask_now),
                  hedge_slippage_c=_r(slippage_c))
        if log:
            log.info("[PAPER] FILL %s %s %s rest@%.3f -> net %.2f%% (hedge slip %.2fc).",
                     q.game, q.market_key, q.direction, q.quote_price, net_pct, slippage_c)
        return ev

    def _process_drift(self, priced, md, now) -> list[dict]:
        """Record rest-side mid at +1 and +5 cycles after each fill (adverse-selection signal)."""
        done: list[dict] = []
        still: list[dict] = []
        for d in self.drift_pending:
            age = self.cycle_no - d["fill_cycle"]
            q = d["q"]
            for c in DRIFT_CYCLES:
                if age == c:
                    d["mids"][c] = _rest_mid(priced, md, q)
            if age >= max(DRIFT_CYCLES):
                ev = self._row("drift", now, q)
                ev.update(drift_mid_1=_r(d["mids"].get(DRIFT_CYCLES[0])),
                          drift_mid_5=_r(d["mids"].get(DRIFT_CYCLES[1])))
                done.append(ev)
            else:
                still.append(d)
        self.drift_pending = still
        return done

    def _row(self, event: str, now: datetime, q: Quote, reason: str = "") -> dict:
        row = {c: "" for c in CSV_COLUMNS}
        row.update(_key=q.key, event=event, ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"), day=self.day,
                   game=q.game, market_key=q.market_key, side=q.side, direction=q.direction,
                   rest_venue=q.rest_venue, hedge_venue=q.hedge_venue, quote_price=_r(q.quote_price),
                   floor=_r(q.floor), at_best=q.at_best, hedge_ask=_r(q.hedge_ask_at_quote), reason=reason)
        return row

    def _append_csv(self, events: list[dict], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            for e in events:
                w.writerow({k: e.get(k, "") for k in CSV_COLUMNS})

    def summary(self, now: datetime) -> dict:
        nets = sorted(self.fill_nets)
        def pctl(p):
            if not nets:
                return None
            i = min(len(nets) - 1, max(0, int(round(p * (len(nets) - 1)))))
            return round(nets[i], 4)
        return {
            "day": self.day, "updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "quotes": self.n_quotes, "fills": self.n_fills,
            "fill_rate": round(self.n_fills / self.n_quotes, 4) if self.n_quotes else 0.0,
            "at_best_share": round(self.at_best_hits / self.n_quotes, 4) if self.n_quotes else 0.0,
            "median_net_at_fill": pctl(0.5), "p10_net": pctl(0.1),
            "worst_net": round(nets[0], 4) if nets else None,
            "mean_hedge_slippage_c": round(sum(self.hedge_slippages_c) / len(self.hedge_slippages_c), 4)
                                     if self.hedge_slippages_c else None,
            "expired_unfilled": self.n_expired_unfilled,
            "paper": True,   # this is ALWAYS a dry-run measurement — never a real order
        }

    def _write_summary(self, path: str, now: datetime) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.summary(now), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # -- state persistence (the fresh-process genz loop loses in-memory state each cycle) ------------
    def to_dict(self) -> dict:
        return {"day": self.day, "cycle_no": self.cycle_no, "n_quotes": self.n_quotes,
                "n_fills": self.n_fills, "n_expired_unfilled": self.n_expired_unfilled,
                "at_best_hits": self.at_best_hits, "fill_nets": self.fill_nets,
                "hedge_slippages_c": self.hedge_slippages_c,
                "quotes": [[list(k), asdict(v)] for k, v in self.quotes.items()],
                "drift_pending": [{"q": asdict(d["q"]), "fill_cycle": d["fill_cycle"],
                                   "mids": {str(k): v for k, v in d["mids"].items()}}
                                  for d in self.drift_pending]}

    @classmethod
    def load(cls, path: str, *, target_net_pct: float, ref_shares: float, poly_rate: float) -> "PaperMaker":
        pm = cls(target_net_pct=target_net_pct, ref_shares=ref_shares, poly_rate=poly_rate)
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (FileNotFoundError, ValueError, OSError):
            return pm
        pm.day = str(d.get("day") or "")
        pm.cycle_no = int(d.get("cycle_no", 0))
        pm.n_quotes = int(d.get("n_quotes", 0))
        pm.n_fills = int(d.get("n_fills", 0))
        pm.n_expired_unfilled = int(d.get("n_expired_unfilled", 0))
        pm.at_best_hits = int(d.get("at_best_hits", 0))
        pm.fill_nets = [float(x) for x in (d.get("fill_nets") or [])]
        pm.hedge_slippages_c = [float(x) for x in (d.get("hedge_slippages_c") or [])]
        pm.quotes = {tuple(k): _quote_from_dict(v) for k, v in (d.get("quotes") or [])}
        pm.drift_pending = [{"q": _quote_from_dict(x["q"]), "fill_cycle": int(x["fill_cycle"]),
                             "mids": {int(kk): vv for kk, vv in (x.get("mids") or {}).items()}}
                            for x in (d.get("drift_pending") or [])]
        return pm

    def save_state(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Helpers                                                                        #
# --------------------------------------------------------------------------- #
def _quote_from_dict(d: dict) -> Quote:
    d = dict(d)
    d["key"] = tuple(d["key"])
    return Quote(**d)


def _r(x):
    return "" if x is None else round(float(x), 6)


def _pv(priced: dict, venue: str, node: dict):
    if venue == "polymarket":
        return priced.get(("poly", node.get("poly_token_id"), "BUY"))
    return priced.get(("kalshi", node.get("kalshi_ticker"), node.get("kalshi_side")))


def _best_bid(md: Any, venue: str, node: dict) -> Optional[float]:
    """Best resting bid to JOIN on the rest venue for this outcome (best-effort; None if unavailable).

    THE FALLBACK, not the path. Every call here is a full order-book HTTP round-trip for a book the
    pricing pass already downloaded this cycle, and it fired once per node per direction — ~2,059 of
    them, serially, through the client's 0.5s throttle. See ``_bid_for``."""
    try:
        if venue == "polymarket":
            fn = getattr(md, "poly_best_bid", None)
            return fn(node.get("poly_token_id")) if fn else None
        fn = getattr(md, "kalshi_best_bid", None)
        return fn(node.get("kalshi_ticker"), node.get("kalshi_side")) if fn else None
    except Exception:  # noqa: BLE001
        return None


def _bid_for(pv: Any, md: Any, venue: str, node: dict) -> Optional[float]:
    """Best bid from the ALREADY-FETCHED priced book, falling back to a fresh read only if the pricing
    pass did not carry one.

    ``pv.bid_read`` is the whole distinction: ``best_bid is None`` on a read book means "this side of the
    book is empty", which is an answer, and re-fetching it would be the same waste for the same None. Only
    an md that cannot report bids (an old injected double) leaves ``bid_read`` False and pays for a read."""
    if getattr(pv, "bid_read", False):
        return getattr(pv, "best_bid", None)
    return _best_bid(md, venue, node)


def _rest_mid(priced: dict, md: Any, q: Quote) -> Optional[float]:
    """(best_bid + best_ask)/2 on the rest venue for the quote's outcome — the adverse-selection probe.
    Falls back to whichever side is available."""
    if q.rest_venue == "polymarket":
        node = {"poly_token_id": q.rest_poly_token}
        pv = priced.get(("poly", q.rest_poly_token, "BUY"))
    else:
        node = {"kalshi_ticker": q.rest_kalshi_ticker, "kalshi_side": q.rest_kalshi_side}
        pv = priced.get(("kalshi", q.rest_kalshi_ticker, q.rest_kalshi_side))
    bid = _bid_for(pv, md, q.rest_venue, node)
    ask = pv.best_ask if pv else None
    if ask is None and bid is None:
        return None
    if ask is None:
        return bid
    if bid is None:
        return ask
    return (ask + bid) / 2.0


def run(markets, priced: dict, md: Any, now: datetime, cfg: gz_config.GenzConfig,
        log: Any = None, paths: Optional[gz_config.SportPaths] = None) -> None:
    """Drive one paper-maker cycle from the genz loop: load cross-cycle state, observe the pre-game
    markets, persist state + the daily CSV + the summary. Fully drop-safe (a dry-run measurement must
    never break the price cycle). Disabled via papermaker.enabled=false. ``paths`` selects the sport's
    isolated papermaker files (soccer default). Nodes flagged settlement_risk (the MLB rain rule) are
    NEVER quoted."""
    if not getattr(cfg, "papermaker_enabled", True):
        return
    paths = paths or gz_config.paths_for_sport(getattr(cfg, "sport", "soccer"))
    try:
        # Exclude settlement-risk markets (e.g. MLB rain-rule totals) from maker quoting entirely.
        quotable = [m for m in markets if not _market_settlement_risk(m)]
        pm = PaperMaker.load(paths.papermaker_state_path,
                             target_net_pct=float(getattr(cfg, "papermaker_target_net_pct", DEFAULT_TARGET_NET_PCT)),
                             ref_shares=float(getattr(cfg, "papermaker_ref_shares", DEFAULT_REF_SHARES)),
                             poly_rate=float(getattr(cfg, "poly_fee_rate", DEFAULT_POLY_FEE_RATE)))
        pm.observe(quotable, priced, md, now, log,
                   csv_path=paths.papermaker_path_for(now), summary_path=paths.papermaker_summary_path)
        pm.save_state(paths.papermaker_state_path)
    except Exception as exc:  # noqa: BLE001 - never let the paper maker break the cycle
        if log:
            log.warning("[PAPER] paper-maker run failed (%s) — skipped.", exc)


def _market_settlement_risk(m: Any) -> bool:
    """True when any side-node of the market carries a settlement_risk flag (the MLB rain rule) — such a
    market is not the same bet across venues, so the paper maker must never quote it."""
    try:
        return any((n or {}).get("settlement_risk") for n in getattr(m, "sides", {}).values())
    except Exception:  # noqa: BLE001
        return False
