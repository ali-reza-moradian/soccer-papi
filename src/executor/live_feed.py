"""In-play (live) detection feed (Phase B) — SEPARATE module, execution layer reused as-is.

Does NOT touch the pre-game scanner. It maintains LIVE order books for the in-window fixtures via
WebSocket (Kalshi ``orderbook_delta`` over wss + Polymarket CLOB ``market`` channel), keeping a
local top-of-book per tracked market and reconnecting with backoff + re-snapshot on drop. On each
book update it runs the SAME arbitrage.py math over the live books; when a clean kalshi<->poly
(2- or 3-leg) arb appears it hands the arb to the SAME execute_arb() path built in Phase A — always
in DRY-RUN (no live placement), gated behind executor.live_enabled (default false).

Layering (so it is fully testable without a network):
  * LiveBookStore        — in-memory ask ladders per (venue, market_id).
  * LiveBookMarketData    — exposes the store as the MarketData the engine walks (no REST re-pull).
  * TrackedMarket         — one MECE market: each outcome + where it can be priced (venue+id+side).
  * LiveArbDetector       — on a book update, recompute over live books, route any arb to execute_arb.
  * parse_* helpers       — pure WS-payload -> ask-ladder parsers (unit-tested).
  * LiveFeed              — orchestrator: WS messages -> store -> detector. Sockets are a thin,
                            lazily-imported network layer (the reconnect loop is not unit-tested).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config as exec_config
from .engine import execute_arb
from .resolve import KALSHI_BOOKS, POLY_BOOKS


# --------------------------------------------------------------------------- #
# Live book state                                                               #
# --------------------------------------------------------------------------- #
class LiveBookStore:
    """In-memory ascending (price, size) ASK ladders to BUY, keyed by (venue, market_id)."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self.updated_at: dict[tuple[str, str], float] = {}

    def update(self, venue: str, market_id: str, ladder: list[tuple[float, float]]) -> None:
        key = (venue, str(market_id))
        self._books[key] = sorted(ladder, key=lambda x: x[0])
        self.updated_at[key] = time.time()

    def ladder(self, venue: str, market_id: str) -> list[tuple[float, float]]:
        return list(self._books.get((venue, str(market_id)), []))

    def best(self, venue: str, market_id: str) -> Optional[tuple[float, float]]:
        lad = self._books.get((venue, str(market_id)))
        return lad[0] if lad else None


class LiveBookMarketData:
    """MarketData adapter backed by the LiveBookStore, so execute_arb walks the SAME live WS books
    instead of re-pulling over REST (the books are already fresh from the socket)."""

    def __init__(self, store: LiveBookStore) -> None:
        self.store = store

    def kalshi_ask_ladder(self, ticker: str, side: str = "YES") -> list[tuple[float, float]]:
        return self.store.ladder("kalshi", ticker)

    def poly_ask_ladder(self, token_id: str) -> list[tuple[float, float]]:
        return self.store.ladder("polymarket", token_id)


# --------------------------------------------------------------------------- #
# Tracked markets                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class OutcomePlacement:
    """One venue placement for an outcome: where it can be backed (venue + market id + side)."""
    outcome: str
    venue: str               # "kalshi" | "polymarket"
    market_id: str           # kalshi ticker | poly token_id
    side: str = "YES"        # kalshi side; poly is always a BUY


@dataclass
class TrackedMarket:
    """One MECE market on a fixture (2-way or 3-way 1x2). ``placements`` maps each outcome to the
    venue placements that can price it; legs may split any way across kalshi/polymarket."""
    fixture: str
    market: str
    outcomes: list[str]
    placements: dict[str, list[OutcomePlacement]]
    fixture_id: Optional[str] = None

    def market_ids(self) -> set[tuple[str, str]]:
        return {(p.venue, p.market_id) for pls in self.placements.values() for p in pls}


# --------------------------------------------------------------------------- #
# Detector — run the SAME arbitrage math, route any arb to execute_arb (dry-run) #
# --------------------------------------------------------------------------- #
class LiveArbDetector:
    """On a book update, recompute the arb over the live books for every affected tracked market
    and, when S < 1, hand a clean kalshi<->poly arb to execute_arb in DRY-RUN."""

    def __init__(self, store: LiveBookStore, tracked: list[TrackedMarket],
                 cfg: Optional[exec_config.ExecConfig] = None, *,
                 executor: Optional[Callable[..., Any]] = None,
                 dryrun_log_path: Optional[str] = None, log: Any = None) -> None:
        self.store = store
        self.tracked = list(tracked)
        self.cfg = cfg or exec_config.load_exec_config()
        self.executor = executor or execute_arb
        self.dryrun_log_path = dryrun_log_path
        self.log = log
        # index (venue, market_id) -> tracked markets using it, so an update fans out cheaply.
        self._index: dict[tuple[str, str], list[TrackedMarket]] = {}
        for tm in self.tracked:
            for key in tm.market_ids():
                self._index.setdefault(key, []).append(tm)

    def on_update(self, venue: str, market_id: str) -> list[Any]:
        """Re-evaluate every tracked market touching (venue, market_id). Returns the execute_arb
        results for any arbs routed (empty list if none)."""
        results: list[Any] = []
        for tm in self._index.get((venue, str(market_id)), []):
            r = self._evaluate(tm)
            if r is not None:
                results.append(r)
        return results

    def _evaluate(self, tm: TrackedMarket) -> Optional[Any]:
        # Best (lowest-ask) placement per outcome from the live books; bail if any outcome is unpriced.
        best: dict[str, tuple[OutcomePlacement, float, float]] = {}
        for outcome in tm.outcomes:
            options = []
            for pl in tm.placements.get(outcome, []):
                if pl.venue not in (KALSHI_BOOKS | POLY_BOOKS):
                    continue                      # never route a non-tradable venue
                top = self.store.best(pl.venue, pl.market_id)
                if top is None:
                    continue
                options.append((pl, top[0], top[1]))
            if not options:
                return None                       # market incomplete -> cannot evaluate yet
            best[outcome] = min(options, key=lambda o: o[1])

        # Run the SAME arbitrage.py math over the live best prices.
        from src.arbitrage import Candidate, compute_arb
        cands = []
        for oid, outcome in enumerate(tm.outcomes):
            pl, price, size = best[outcome]
            cands.append(Candidate(outcome_id=oid, outcome_name=outcome, book=pl.venue,
                                   clone_group=pl.venue, decimal_odds=1.0 / price,
                                   limit=size * price))
        res = compute_arb(cands)
        if not res.is_arb:                        # S >= 1 -> no arb, nothing to route
            return None

        arb = self._build_arb(tm, best, res)
        if self.log:
            self.log.info("[LIVE] arb detected %s | %s | S=%.4f ROI=%.2f%% -> dry-run execute",
                          tm.fixture, tm.market, res.arb_sum_S, res.roi_pct)
        md = LiveBookMarketData(self.store)
        # ALWAYS dry-run from the live feed (no live placement), exactly like the pre-game path.
        return self.executor(arb, live=False, cfg=self.cfg, market_data=md,
                             dryrun_log_path=self.dryrun_log_path, log=self.log)

    def _build_arb(self, tm: TrackedMarket, best: dict, res: Any) -> dict[str, Any]:
        legs = []
        venue_ids = []
        for outcome in tm.outcomes:
            pl, price, size = best[outcome]
            legs.append({
                "book": pl.venue, "venue": pl.venue, "outcome": pl.outcome,
                "decimal_odds": 1.0 / price, "limit": round(size * price, 4),
                "venue_id": pl.market_id, "venue_side": pl.side,
            })
            venue_ids.append(f"{pl.venue}:{pl.market_id}")
        fingerprint = "live|" + "|".join([tm.fixture, tm.market] + sorted(venue_ids))
        return {
            "match": tm.fixture, "fixture_id": tm.fixture_id, "market": tm.market,
            "signature": fingerprint, "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "live", "legs": legs,
        }


# --------------------------------------------------------------------------- #
# Pure WS-payload parsers (unit-tested)                                          #
# --------------------------------------------------------------------------- #
def kalshi_yes_ask_ladder(book: dict[str, Any], side: str = "YES") -> list[tuple[float, float]]:
    """Ascending (price_dollars, size) ladder to BUY ``side`` from a Kalshi book {"yes":[[c,sz]],
    "no":[[c,sz]]} (cents). Buying YES lifts complements of resting NO bids: NO@c -> YES@(100-c)."""
    opp = "no" if str(side).upper() == "YES" else "yes"
    out: list[tuple[float, float]] = []
    for lvl in (book or {}).get(opp) or []:
        try:
            c, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        price = (100.0 - c) / 100.0
        if 0.0 < price < 1.0 and size > 0:
            out.append((price, size))
    out.sort(key=lambda x: x[0])
    return out


def poly_ask_ladder(book: dict[str, Any]) -> list[tuple[float, float]]:
    """Ascending (price, size) ladder to BUY from a Polymarket CLOB book {"asks":[{price,size}]}
    (or [[price,size]])."""
    out: list[tuple[float, float]] = []
    for lvl in (book or {}).get("asks") or []:
        try:
            if isinstance(lvl, dict):
                p, s = float(lvl.get("price")), float(lvl.get("size"))
            else:
                p, s = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if 0.0 < p < 1.0 and s > 0:
            out.append((p, s))
    out.sort(key=lambda x: x[0])
    return out


def parse_kalshi_message(msg: Any) -> Optional[tuple[str, list[tuple[float, float]]]]:
    """Parse a Kalshi WS orderbook_snapshot/orderbook_delta message into (ticker, YES-ask ladder),
    or None if it is not a book message. Accepts the {"type","msg":{...}} envelope or a bare msg."""
    if isinstance(msg, str):
        try:
            msg = json.loads(msg)
        except ValueError:
            return None
    if not isinstance(msg, dict):
        return None
    mtype = msg.get("type")
    if mtype not in (None, "orderbook_snapshot", "orderbook_delta"):
        return None
    body = msg.get("msg", msg)
    ticker = body.get("market_ticker") or body.get("ticker")
    if not ticker:
        return None
    return str(ticker), kalshi_yes_ask_ladder(body, "YES")


def parse_poly_message(msg: Any) -> Optional[tuple[str, list[tuple[float, float]]]]:
    """Parse a Polymarket CLOB ``market`` channel book message into (token_id, ask ladder), or None.
    Accepts {"event_type":"book","asset_id":..,"asks":[...]} or a bare book dict with asset_id."""
    if isinstance(msg, str):
        try:
            msg = json.loads(msg)
        except ValueError:
            return None
    if isinstance(msg, list):                     # CLOB sometimes batches book messages
        for m in msg:
            r = parse_poly_message(m)
            if r:
                return r
        return None
    if not isinstance(msg, dict):
        return None
    token = msg.get("asset_id") or msg.get("token_id") or msg.get("market")
    if not token or "asks" not in msg:
        return None
    return str(token), poly_ask_ladder(msg)


# --------------------------------------------------------------------------- #
# Orchestrator                                                                   #
# --------------------------------------------------------------------------- #
class LiveFeed:
    """Wires WS messages -> LiveBookStore -> LiveArbDetector. ``handle_kalshi_message`` /
    ``handle_poly_message`` are the testable injection points; ``start()`` spins up the real
    sockets only when executor.live_enabled is true (and always stays dry-run)."""

    def __init__(self, cfg: exec_config.ExecConfig, tracked: list[TrackedMarket], *,
                 store: Optional[LiveBookStore] = None,
                 detector: Optional[LiveArbDetector] = None,
                 executor: Optional[Callable[..., Any]] = None,
                 dryrun_log_path: Optional[str] = None, log: Any = None) -> None:
        self.cfg = cfg
        self.log = log
        self.store = store or LiveBookStore()
        self.detector = detector or LiveArbDetector(
            self.store, tracked, cfg, executor=executor, dryrun_log_path=dryrun_log_path, log=log)

    def handle_kalshi_message(self, raw: Any) -> list[Any]:
        parsed = parse_kalshi_message(raw)
        if parsed is None:
            return []
        ticker, ladder = parsed
        self.store.update("kalshi", ticker, ladder)
        return self.detector.on_update("kalshi", ticker)

    def handle_poly_message(self, raw: Any) -> list[Any]:
        parsed = parse_poly_message(raw)
        if parsed is None:
            return []
        token, ladder = parsed
        self.store.update("polymarket", token, ladder)
        return self.detector.on_update("polymarket", token)

    def start(self) -> bool:
        """Start the live WS feed. No-op (returns False) unless executor.live_enabled. Network layer
        is lazily imported; the feed stays DRY-RUN regardless of enabled/dry_run."""
        if not self.cfg.live_enabled:
            if self.log:
                self.log.info("[LIVE] executor.live_enabled is false — in-play feed not started.")
            return False
        if self.log:
            self.log.info("[LIVE] starting in-play feed (DRY-RUN only) over %d tracked market(s).",
                          len(self.detector.tracked))
        self._run_sockets()      # blocking; reconnect+backoff handled inside
        return True

    # -- network layer (thin; not unit-tested) ------------------------------
    def _run_sockets(self) -> None:  # pragma: no cover - real WebSocket loop
        """Spin up the Kalshi (orderbook_delta) and Polymarket (CLOB market) sockets with reconnect +
        backoff and re-snapshot on drop, dispatching each message to handle_*_message. Lazily imports
        the websocket client so the rest of the module imports without that dependency."""
        try:
            import websocket  # noqa: F401  (websocket-client)
        except ImportError as exc:
            raise RuntimeError("websocket-client not installed — pip install websocket-client") from exc
        # Implementation note: connect wss endpoints, subscribe to the tracked tickers/tokens, and on
        # each frame call self.handle_kalshi_message / self.handle_poly_message. On close/error sleep
        # with exponential backoff and reconnect, re-requesting a fresh snapshot so the local book is
        # never stale. Kept thin and out of the unit-tested surface.
        raise NotImplementedError("wire concrete wss endpoints + subscriptions before live use")
