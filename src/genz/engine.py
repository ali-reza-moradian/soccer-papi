"""JOB 2 — the PRICE ENGINE (fast, ~20s loop).

Loads the static match tree (Job 1) and, each cycle, reads LIVE prices ONLY for the kalshi_tickers /
poly_token_ids already in the tree — it never rediscovers markets. For every matched 2-OUTCOME
market it takes the best (cheapest) ask for EACH side across the two venues (best-of-both), using the
REAL walk-to-stake fill (bookmath.walk_book) rather than top-of-book, and flags an arb when the
implied cost (price_side_A + price_side_B) is below 1 after costs. 3-way moneyline and many-outcome
markets are SKIPPED in v1.

Execution is delegated to the EXECUTOR (src/executor/engine.execute_arb): it re-pulls books, walks
every leg, models fees + slippage, runs ALL guardrails, and — only when the executor flags are on
(enabled AND not dry_run) — fires the staged poly_exec v2 + kalshi_exec path. With the defaults
(enabled:false / dry_run:true) every detected arb is measured and logged to data/genz/genz_arbs.csv;
nothing is placed. GenZ adds ONE more rail on top: AT MOST ONE live trade attempt per cycle. Low-
confidence (player-prop) nodes are ALERT-ONLY and never auto-traded, even with the flags on.

A heartbeat is written each cycle to data/genz/genz_heartbeat.json.
"""
from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .. import bookmath
from ..arbitrage import Candidate, compute_arb
from ..executor import config as exec_config
from ..executor import engine as exec_engine
from ..executor.guardrails import Guardrails
from ..executor.ledger import Ledger
from ..executor.resolve import MarketData
from . import config as gz_config
from . import tree_builder

ARBS_COLUMNS = [
    "ts_utc", "game", "market_type", "market_key", "line",
    "side_a", "venue_a", "price_a", "side_b", "venue_b", "price_b",
    "implied_cost", "roi_pct", "net_edge_pct", "arb_survived", "would_trade",
    "exec_status", "confidence", "note",
]


# --------------------------------------------------------------------------- #
# Tree -> the 2-outcome markets the engine prices                               #
# --------------------------------------------------------------------------- #
@dataclass
class Market:
    game: str
    away: str
    home: str
    market_type: str
    market_key: str
    line: Optional[float]
    kind: str
    confidence: str
    kickoff: str = ""                            # game kickoff (UTC ISO) — gates out started games
    sides: dict = field(default_factory=dict)   # side_role -> tree node dict

    @property
    def two_outcome(self) -> bool:
        return self.kind == "2way" and len(self.sides) == 2


def collect_markets(tree: dict[str, Any]) -> list[Market]:
    """Group tree nodes into markets (by market_key). Only well-formed 2-outcome markets (kind 2way,
    exactly two distinct sides) are returned — 3-way and many-outcome families are recorded in the
    tree but never arbed by v1."""
    out: list[Market] = []
    for game_id, g in (tree.get("games") or {}).items():
        by_key: dict[str, Market] = {}
        for node in g.get("nodes") or []:
            if node.get("kind") != "2way":
                continue
            mk = node["market_key"]
            m = by_key.get(mk)
            if m is None:
                m = Market(game=game_id, away=g.get("away", ""), home=g.get("home", ""),
                           market_type=node["market_type"], market_key=mk, line=node.get("line"),
                           kind=node["kind"], confidence=node.get("confidence", "high"),
                           kickoff=str(g.get("kickoff_utc", "")))
                by_key[mk] = m
            m.sides[node["side"]] = node
        out.extend(m for m in by_key.values() if m.two_outcome)
    return out


# --------------------------------------------------------------------------- #
# Pricing (concurrent, walk-to-stake)                                           #
# --------------------------------------------------------------------------- #
@dataclass
class PricedVenue:
    venue: str
    identifier: str
    best_ask: Optional[float]
    fill: Optional[float]            # walk-to-stake avg fill price (the REAL price, not top-of-book)
    depth_usd: float                 # fillable dollar depth (Σ price*size of the ladder)
    open: Optional[bool] = None      # venue market open/tradeable; None = unknown (couldn't tell)

    @property
    def priced(self) -> bool:
        return self.fill is not None and 0.0 < self.fill < 1.0


def _ladder(md: Any, venue: str, ident: str, side: str) -> list[tuple[float, float]]:
    if venue == "kalshi":
        return md.kalshi_ask_ladder(ident, side)
    return md.poly_ask_ladder(ident)


def _venue_open(md: Any, venue: str, ident: str) -> Optional[bool]:
    """Optional per-venue open/settled status, if ``md`` exposes it (kalshi_market_open /
    poly_market_open). None when unknown — we never skip a node just because we can't tell."""
    hook = getattr(md, "kalshi_market_open" if venue == "kalshi" else "poly_market_open", None)
    if hook is None:
        return None
    try:
        return bool(hook(ident))
    except Exception:  # noqa: BLE001 - a status probe error is non-fatal -> unknown
        return None


def _price_one(md: Any, venue: str, ident: str, side: str, stake: float) -> PricedVenue:
    """Walk a live ladder to ``stake`` and report best ask + walk-to-stake fill + dollar depth +
    open status. FULLY exception-safe: ANY data/timeout error -> an UNPRICED result (never raises),
    so one bad book can't abort the cycle or feed a half price into an arb."""
    try:
        ladder = _ladder(md, venue, ident, side)
    except Exception:  # noqa: BLE001 - any data error -> unpriced (drop-safe)
        ladder = []
    is_open = _venue_open(md, venue, ident)
    if not ladder:
        return PricedVenue(venue, ident, None, None, 0.0, open=is_open)
    best = ladder[0][0]
    walk = bookmath.walk_book(ladder, stake)
    fill = walk.avg_price if walk.avg_price > 0 else best
    depth = sum(p * s for p, s in ladder)
    return PricedVenue(venue, ident, best, fill, depth, open=is_open)


def price_markets(md: Any, markets: list[Market], cfg: gz_config.GenzConfig) -> dict[tuple, PricedVenue]:
    """Concurrently price every (venue, identifier, side) that appears in the markets. Returns a map
    keyed by (venue, identifier, side)."""
    jobs: dict[tuple, tuple[str, str, str]] = {}
    for m in markets:
        for node in m.sides.values():
            if node.get("kalshi_ticker"):
                jobs[("kalshi", node["kalshi_ticker"], node["kalshi_side"])] = (
                    "kalshi", node["kalshi_ticker"], node["kalshi_side"])
            if node.get("poly_token_id"):
                jobs[("poly", node["poly_token_id"], "BUY")] = ("poly", node["poly_token_id"], "BUY")
    out: dict[tuple, PricedVenue] = {}
    if not jobs:
        return out
    # Submit each fetch as its own future and collect results individually: one future's exception
    # must NOT cancel the others or bubble out of the cycle (it becomes an unpriced node).
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, cfg.max_workers)) as ex:
        futures = {ex.submit(_price_one, md, *job, cfg.walk_stake_usd): key for key, job in jobs.items()}
        for fut in concurrent.futures.as_completed(futures):
            key = futures[fut]
            venue, ident, _ = jobs[key]
            try:
                out[key] = fut.result()
            except Exception:  # noqa: BLE001 - belt-and-suspenders; _price_one is already safe
                out[key] = PricedVenue(venue, ident, None, None, 0.0)
    return out


# --------------------------------------------------------------------------- #
# Best-of-both + arb check                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class SideQuote:
    venue: str
    node: dict
    price: float
    depth_usd: float


def _best_side(node: dict, priced: dict[tuple, PricedVenue]) -> Optional[SideQuote]:
    """Cheapest venue (lowest walk-to-stake fill) for one side, across Kalshi and Poly."""
    cands: list[SideQuote] = []
    kp = priced.get(("kalshi", node.get("kalshi_ticker"), node.get("kalshi_side")))
    if kp and kp.priced:
        cands.append(SideQuote("kalshi", node, kp.fill, kp.depth_usd))
    pp = priced.get(("poly", node.get("poly_token_id"), "BUY"))
    if pp and pp.priced:
        cands.append(SideQuote("polymarket", node, pp.fill, pp.depth_usd))
    return min(cands, key=lambda q: q.price) if cands else None


@dataclass
class ArbCandidate:
    market: Market
    side_a: str
    side_b: str
    quote_a: SideQuote
    quote_b: SideQuote
    implied_cost: float
    roi_pct: float

    @property
    def is_arb(self) -> bool:
        return self.implied_cost < 1.0


def find_arbs(markets: list[Market], priced: dict[tuple, PricedVenue]) -> list[ArbCandidate]:
    """One best-of-both arb candidate per 2-outcome market that has both sides priced. Sorted best
    (highest ROI) first. A candidate with implied_cost >= 1 is NOT an arb (kept out)."""
    found: list[ArbCandidate] = []
    for m in markets:
        sides = list(m.sides.items())
        (sa, na), (sb, nb) = sides[0], sides[1]
        qa, qb = _best_side(na, priced), _best_side(nb, priced)
        if qa is None or qb is None:
            continue
        implied = qa.price + qb.price
        cands = [
            Candidate(1, sa, qa.venue, "ga", 1.0 / qa.price, limit=qa.depth_usd),
            Candidate(2, sb, qb.venue, "gb", 1.0 / qb.price, limit=qb.depth_usd),
        ]
        res = compute_arb(cands)
        if res.is_arb:
            found.append(ArbCandidate(m, sa, sb, qa, qb, implied, res.roi_pct))
    found.sort(key=lambda a: a.roi_pct, reverse=True)
    return found


def _arb_dict(c: ArbCandidate) -> dict[str, Any]:
    """Build the executor's arb-dict (two cross-venue legs) from a best-of-both candidate."""
    def leg(side_role: str, q: SideQuote) -> dict[str, Any]:
        node = q.node
        if q.venue == "kalshi":
            return {"venue": "kalshi", "book": "kalshi", "outcome": side_role, "price": q.price,
                    "decimal_odds": 1.0 / q.price, "venue_id": node["kalshi_ticker"],
                    "venue_side": node["kalshi_side"], "limit": q.depth_usd}
        return {"venue": "polymarket", "book": "polymarket", "outcome": side_role, "price": q.price,
                "decimal_odds": 1.0 / q.price, "venue_id": node["poly_token_id"],
                "venue_side": "BUY", "limit": q.depth_usd}
    m = c.market
    return {"match": f"{m.away} vs {m.home}", "fixture_id": m.game,
            "market": f"{m.market_type} {m.line if m.line is not None else ''}".strip(),
            "signature": f"genz|{m.game}|{m.market_key}", "source": "genz", "roi_pct": c.roi_pct,
            "legs": [leg(c.side_a, c.quote_a), leg(c.side_b, c.quote_b)]}


# --------------------------------------------------------------------------- #
# Eligibility — GenZ is PRE-GAME only; skip started/desynced markets             #
# --------------------------------------------------------------------------- #
def _parse_kickoff(v: Any) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def _started(kickoff: str, now: datetime) -> bool:
    """True once ``kickoff`` (UTC ISO) is at/before ``now``. Unknown/empty kickoff -> not started
    (lenient). Both sides are compared as UTC POSIX seconds, so the timezones can't mismatch."""
    ts = _parse_kickoff(kickoff)
    return ts is not None and ts <= now.timestamp()


def _game_started(market: Market, now: datetime) -> bool:
    """True once the game has kicked off. GenZ is a PRE-GAME arb bot; half-period markets desync
    between venues the moment a game goes live, so a started game is NOT arbitrable."""
    return _started(market.kickoff, now)


# O/U totals families. A same-line/period Over+Under MUST sum to ~1.0 (like corners); a cross-venue
# best-of-both that sums well below 1 means the two legs are NOT the same outcome (a period/line
# mispairing), not a real arb. Corners and spread are verified correct and are NOT gated here.
_TOTAL_FAMILIES = frozenset({"total_goals", "1h_total", "2h_total", "team_total"})


def _venues_desynced(market: Market, priced: dict[tuple, "PricedVenue"]) -> bool:
    """True if, for any side, ONE venue's market is settled/closed (open=False) while the other is
    open — the two venues are pricing different states, so the node must be skipped, never traded."""
    for node in market.sides.values():
        k = priced.get(("kalshi", node.get("kalshi_ticker"), node.get("kalshi_side")))
        p = priced.get(("poly", node.get("poly_token_id"), "BUY"))
        ko = k.open if k else None
        po = p.open if p else None
        if (ko is False and po is True) or (po is False and ko is True):
            return True
    return False


# --------------------------------------------------------------------------- #
# Cycle                                                                          #
# --------------------------------------------------------------------------- #
@dataclass
class CycleResult:
    games: int
    nodes_priced: int
    arbs_found: int
    would_trade: int
    nodes_unpriced: int = 0
    markets_skipped: int = 0          # markets skipped as ineligible (game started / venues desynced)
    rows: list = field(default_factory=list)


def _base_row(c: ArbCandidate, now: datetime) -> dict[str, Any]:
    """The common genz_arbs.csv fields for a detected arb (execution/guard fields filled by the caller)."""
    m = c.market
    return {
        "ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "game": m.game,
        "market_type": m.market_type, "market_key": m.market_key, "line": m.line,
        "side_a": c.side_a, "venue_a": c.quote_a.venue, "price_a": round(c.quote_a.price, 4),
        "side_b": c.side_b, "venue_b": c.quote_b.venue, "price_b": round(c.quote_b.price, 4),
        "implied_cost": round(c.implied_cost, 4), "roi_pct": round(c.roi_pct, 4),
        "net_edge_pct": "", "arb_survived": "", "would_trade": False, "exec_status": "",
        "confidence": m.confidence, "note": "",
    }


def run_cycle(tree: dict[str, Any], md: Any, gz_cfg: gz_config.GenzConfig,
              exec_cfg: exec_config.ExecConfig, *, now: Optional[datetime] = None,
              kalshi: Any = None, poly: Any = None, ledger: Any = None, guard: Any = None,
              write: bool = True, arbs_path: Optional[str] = None, heartbeat_path: Optional[str] = None,
              log: Any = None) -> CycleResult:
    """One price cycle: price every tree token, find best-of-both 2-outcome arbs, and (for high-
    confidence ones) hand them to the executor — AT MOST ONE live attempt per cycle. Records every
    detected arb to genz_arbs.csv and writes the heartbeat. Pure w.r.t. injected md/kalshi/poly."""
    now = now or datetime.now(timezone.utc)
    markets = collect_markets(tree)

    # ELIGIBILITY GATE 1 (BEFORE pricing): GenZ trades PRE-GAME only. Skip ALL nodes of a started game
    # up front — a live game's half-period markets desync between venues and throw phantom edges.
    pre_game: list[Market] = []
    markets_skipped = 0
    for mkt in markets:
        if _game_started(mkt, now):
            markets_skipped += 1
            if log:
                log.info("[GENZ] %s %s: game started (kickoff %s) — skipping all nodes (pre-game only).",
                         mkt.game, mkt.market_key, mkt.kickoff)
            continue
        pre_game.append(mkt)

    priced = price_markets(md, pre_game, gz_cfg)            # price ONLY pre-game markets
    nodes_priced = sum(1 for pv in priced.values() if pv.priced)
    nodes_unpriced = sum(1 for pv in priced.values() if not pv.priced)

    # ELIGIBILITY GATE 2 (after pricing): skip a market where the two venues are pricing different
    # states (one settled, one open) — a desync that produces phantom edges.
    eligible: list[Market] = []
    for mkt in pre_game:
        if _venues_desynced(mkt, priced):
            markets_skipped += 1
            if log:
                log.info("[GENZ] %s %s: venues desynced (one settled, one open) — skipping.",
                         mkt.game, mkt.market_key)
            continue
        eligible.append(mkt)
    arbs = find_arbs(eligible, priced)

    rows: list[dict[str, Any]] = []
    attempted_trade = False                    # the one-trade-per-cycle rail
    would_trade = 0
    for c in arbs:
        m = c.market
        row = _base_row(c, now)
        # SANITY GUARD: a 2-outcome arb on these markets is low-single-digit %; an ROI above the
        # plausible bound (or an implied cost < 0.5) is almost certainly a PAIRING bug, not a real
        # arb. Reject it: never auto-trade, flag would_trade=False, and log a warning for review.
        if c.roi_pct > gz_cfg.max_plausible_roi_pct or c.implied_cost < 0.5:
            if log:
                log.warning("[GENZ] REJECTED implausible arb %s %s: roi=%.1f%% implied_cost=%.4f — "
                            "review pairing (legs likely not complementary).",
                            m.game, m.market_key, c.roi_pct, c.implied_cost)
            row.update(would_trade=False, exec_status="rejected_implausible",
                       note="roi exceeds plausible bound — review pairing")
            rows.append(row)
            continue
        # TOTALS COMPLEMENTARITY GUARD: a same-line/period Over+Under must sum to ~1.0 (like corners).
        # A totals O/U node summing below min_total_implied is a period/line MISPAIRING (the two legs
        # are not the same outcome), NOT a real arb — reject it so the phantom can never auto-trade.
        if m.market_type in _TOTAL_FAMILIES and c.implied_cost < gz_cfg.min_total_implied:
            if log:
                log.warning("[GENZ] REJECTED totals mispairing %s %s: over+under=%.4f < %.2f — legs not "
                            "complementary (period/line mismatch).",
                            m.game, m.market_key, c.implied_cost, gz_cfg.min_total_implied)
            row.update(would_trade=False, exec_status="rejected_total_mismatch",
                       note=f"totals over+under {c.implied_cost:.4f} < {gz_cfg.min_total_implied} — "
                            f"not complementary (same line/period must sum ~1.0)")
            rows.append(row)
            continue
        high_conf = (m.confidence == "high")
        # Only high-confidence markets are eligible to auto-trade, and only ONE live attempt per cycle.
        want_live = high_conf and exec_cfg.live_allowed and not attempted_trade
        result = exec_engine.execute_arb(
            _arb_dict(c), live=want_live, cfg=exec_cfg, market_data=md, kalshi=kalshi, poly=poly,
            ledger=ledger, guard=guard, write_log=False, log=log)
        if want_live:
            attempted_trade = True             # consumed the single per-cycle attempt
        survived = bool(result.arb_survived)
        wt = high_conf and survived and c.roi_pct >= gz_cfg.min_edge_pct
        would_trade += 1 if wt else 0
        row.update(net_edge_pct=result.detail.get("net_edge_pct", ""), arb_survived=survived,
                   would_trade=wt, exec_status=result.status,
                   note="alert-only (low confidence)" if not high_conf else result.reason)
        rows.append(row)

    res = CycleResult(games=len(tree.get("games") or {}), nodes_priced=nodes_priced,
                      arbs_found=len(arbs), would_trade=would_trade, nodes_unpriced=nodes_unpriced,
                      markets_skipped=markets_skipped, rows=rows)
    if write:
        if rows:
            # Rotate daily: append to genz_arbs_YYYYMMDD.csv (unless a path is given explicitly).
            append_arbs(rows, arbs_path or gz_config.arbs_path_for(now))
        write_heartbeat(res, now=now, path=heartbeat_path)
    if log:
        log.info("[GENZ] cycle: %d game(s), %d node(s) priced (%d unpriced), %d market(s) skipped, "
                 "%d arb(s), %d would-trade.", res.games, res.nodes_priced, res.nodes_unpriced,
                 res.markets_skipped, res.arbs_found, res.would_trade)
    return res


# --------------------------------------------------------------------------- #
# Debug — make the started-game gate + totals legs INSPECTABLE                    #
# --------------------------------------------------------------------------- #
def debug_gate(tree: dict[str, Any], *, now: Optional[datetime] = None, md: Any = None,
               gz_cfg: Optional[gz_config.GenzConfig] = None, out: Any = print) -> None:
    """Print, per game, kickoff_utc / now_utc / started — so the started-game gate is SEEN to evaluate
    correctly. With ``md``, also dump every TOTALS node's EXACT legs (Kalshi ticker+side+price, Poly
    token+side+price, line/period) and the Over+Under best-of-both sum (must be ~1.0), to prove both
    legs reference the same line and the same period."""
    now = now or datetime.now(timezone.utc)
    nows = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    games = tree.get("games") or {}
    out(f"=== GenZ gate debug @ {nows}  ({len(games)} game(s)) ===")
    for gid, g in games.items():
        ko = str(g.get("kickoff_utc", ""))
        out(f"[GATE] {gid}: kickoff_utc={ko!r}  now_utc={nows}  started={_started(ko, now)}")
    if md is None:
        return
    cfg = gz_cfg or gz_config.load_genz_config()
    totals = [m for m in collect_markets(tree) if m.market_type in _TOTAL_FAMILIES]
    priced = price_markets(md, totals, cfg)
    for m in totals:
        out(f"[TOTALS] {m.game} {m.market_key} (line={m.line}):")
        for side, node in m.sides.items():
            kp = priced.get(("kalshi", node.get("kalshi_ticker"), node.get("kalshi_side")))
            pp = priced.get(("poly", node.get("poly_token_id"), "BUY"))
            kpx = f"{kp.fill:.4f}" if (kp and kp.priced) else "—"
            ppx = f"{pp.fill:.4f}" if (pp and pp.priced) else "—"
            out(f"   {side:5s}: KALSHI ticker={node.get('kalshi_ticker')} side={node.get('kalshi_side')} "
                f"price={kpx}  |  POLY token={node.get('poly_token_id')} side={node.get('poly_side')!r} price={ppx}")
        over, under = _best_side(m.sides.get("over", {}), priced), _best_side(m.sides.get("under", {}), priced)
        if over and under:
            s = over.price + under.price
            flag = "OK ~1.0" if s >= cfg.min_total_implied else f"MISPAIR (<{cfg.min_total_implied})"
            out(f"   best-of-both: over({over.venue})={over.price:.4f} + under({under.venue})={under.price:.4f} "
                f"= {s:.4f}  -> {flag}")


# --------------------------------------------------------------------------- #
# Persistence                                                                   #
# --------------------------------------------------------------------------- #
def append_arbs(rows: list[dict[str, Any]], path: Optional[str] = None) -> None:
    """Append detected-arb rows to data/genz/genz_arbs.csv (header written once)."""
    path = path or gz_config.ARBS_CSV_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ARBS_COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ARBS_COLUMNS})


def write_heartbeat(res: CycleResult, *, now: datetime, path: Optional[str] = None) -> None:
    path = path or gz_config.HEARTBEAT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"last_cycle_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "games": res.games,
                   "nodes_priced": res.nodes_priced, "nodes_unpriced": res.nodes_unpriced,
                   "markets_skipped": res.markets_skipped, "arbs_found": res.arbs_found,
                   "would_trade": res.would_trade}, fh, indent=2)


# --------------------------------------------------------------------------- #
# Loop                                                                          #
# --------------------------------------------------------------------------- #
def run_loop(gz_cfg: gz_config.GenzConfig, exec_cfg: exec_config.ExecConfig, *,
             interval: Optional[float] = None, once: bool = False, log: Any = None,
             md: Any = None, kalshi: Any = None, poly: Any = None) -> None:
    """Read the tree and run cycles forever (or once). The tree is RELOADED each cycle so a fresh
    hourly build (Job 1) is picked up without a restart. Books are read via the executor's read-only
    MarketData (the src/kalshi.py + src/polymarket.py readers)."""
    gz_config.ensure_dirs()
    # Build the read-only book source with the GenZ (fail-fast) HTTP timeout so a hung call can't
    # stall a cycle. MarketData accepts injected clients; tests pass their own md.
    if md is None:
        from .. import kalshi as _ks
        from .. import polymarket as _pm
        md = MarketData(
            kalshi_client=_ks.KalshiClient(base_url=gz_cfg.kalshi_base_url,
                                           timeout=gz_cfg.http_timeout_seconds),
            poly_client=_pm.PolymarketClient(gamma_base=gz_cfg.gamma_base, clob_base=gz_cfg.clob_base,
                                             timeout=gz_cfg.http_timeout_seconds))
    ledger = Ledger()
    guard = Guardrails(exec_cfg, ledger=ledger)
    interval = interval if interval is not None else gz_cfg.interval_seconds
    while True:
        # Only a STOP file (or Ctrl-C) ends the loop; transient errors never do.
        if exec_config.stop_file_present():
            if log:
                log.warning("[GENZ] STOP file present — halting loop.")
            return
        start = time.monotonic()
        try:
            tree = tree_builder.load_tree()
            run_cycle(tree, md, gz_cfg, exec_cfg, kalshi=kalshi, poly=poly, ledger=ledger,
                      guard=guard, log=log)
        except Exception as exc:  # noqa: BLE001 - a cycle error must never kill the loop
            if log:
                log.warning("[GENZ] cycle error (%s) — continuing to next cycle.", exc)
        if once:
            return
        time.sleep(max(0.0, interval - (time.monotonic() - start)))
