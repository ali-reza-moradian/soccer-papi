"""The quotable universe: union of soccer + MLB tree markets that are settlement-clean, pre-game, and
among the nearest ``max_games`` games by kickoff (across BOTH sports combined).

Reuses the price engine's market collection + the SAME settlement guards (settlement_risk / period
mismatch) so a node the engine would refuse to trade is never quoted here either. Trees are reloaded
by the driver on file-mtime change; this module is pure w.r.t. the tree dicts passed in.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .. import config as gz_config
from ..engine import _market_settlement_risk, _period_disagrees, collect_markets


@dataclass
class Node:
    side: str
    poly_token_id: Optional[str]
    poly_side: Optional[str]
    poly_fee_rate: float
    kalshi_ticker: Optional[str]
    kalshi_side: Optional[str]


@dataclass
class QuoteMarket:
    sport: str
    game: str
    teams: str
    market_type: str
    market_key: str
    line: Any
    kickoff_ts: float
    kickoff_iso: str
    sides: dict = field(default_factory=dict)     # side name -> Node

    def other(self, side: str) -> Optional[Node]:
        for s, n in self.sides.items():
            if s != side:
                return n
        return None


def _kickoff_ts(iso: Any) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def _node(nd: dict) -> Node:
    return Node(side=nd.get("side"), poly_token_id=nd.get("poly_token_id"), poly_side=nd.get("poly_side"),
                poly_fee_rate=float(nd.get("poly_fee_rate") or 0.0),
                kalshi_ticker=nd.get("kalshi_ticker"), kalshi_side=nd.get("kalshi_side"))


def select_games(game_kick: dict, *, max_games: int, per_sport: Optional[dict] = None) -> set:
    """Which ``(sport, game)`` keys to admit, given each one's kickoff. PURE, so it is unit-tested.

    Without ``per_sport`` this is one nearest-by-kickoff queue across every sport — and soccer's fixture
    density wins it outright (the observed universe was 92% soccer, 120 of 130 markets, while the next
    UFC card sat at queue position 172 and became quotable only on fight day). With it, each sport gets
    its own nearest-N queue.

    A sport ABSENT from the map is not capped per-sport; it competes for whatever the global
    ``max_games`` backstop leaves. That direction is deliberate — an unlisted sport can never grow the
    universe past the global cap, and a map that forgot to name a sport does not silently delete it."""
    ordered = sorted(game_kick.items(), key=lambda kv: kv[1])
    if not per_sport:
        return {g for g, _ in ordered[:max(0, int(max_games))]}
    kept: list = []
    taken: dict = {}
    for key, ts in ordered:
        sport = key[0]
        limit = per_sport.get(sport)
        if limit is not None and taken.get(sport, 0) >= max(0, int(limit)):
            continue
        taken[sport] = taken.get(sport, 0) + 1
        kept.append((key, ts))
    return {g for g, _ in kept[:max(0, int(max_games))]}      # global backstop still applies


def build_universe(trees: dict, now_ts: float, *, max_games: int = 20,
                   expire_before_kickoff_s: int = 120, horizon_hours: Optional[dict] = None,
                   max_games_per_sport: Optional[dict] = None) -> list:
    """Every settlement-clean, 2-outcome market of the nearest ``max_games`` games by kickoff, admitted
    from now until kickoff + the sport's in-play horizon. ``trees`` maps sport -> loaded tree dict;
    ``horizon_hours`` maps sport -> hours after kickoff a node stays admitted (default {} -> drop at
    kickoff, the pre-in-play behavior). ``max_games_per_sport`` gives each sport its own nearest-N queue
    (see ``select_games``). The driver assigns each node its pre/gap/inplay phase at quote time; the
    universe only decides admission."""
    horizon_hours = horizon_hours or {}
    per_market: list = []
    game_kick: dict = {}
    for sport, tree in (trees or {}).items():
        if not tree:
            continue
        horizon_s = float(horizon_hours.get(sport, 0.0)) * 3600.0
        for m in collect_markets(tree):
            ts = _kickoff_ts(m.kickoff)
            if ts is None or now_ts >= ts + horizon_s:
                continue                                   # no kickoff, or beyond the in-play horizon
            if _market_settlement_risk(m) or _period_disagrees(m):
                continue                                   # not the same bet across venues -> never quote
            if not m.two_outcome:
                continue
            per_market.append((sport, ts, m))
            game_kick[(sport, m.game)] = min(ts, game_kick.get((sport, m.game), ts))
    keep = select_games(game_kick, max_games=max_games, per_sport=max_games_per_sport)
    out: list = []
    for sport, ts, m in per_market:
        if (sport, m.game) not in keep:
            continue
        out.append(QuoteMarket(
            sport=sport, game=m.game, teams=f"{m.away} vs {m.home}", market_type=m.market_type,
            market_key=m.market_key, line=m.line, kickoff_ts=ts, kickoff_iso=str(m.kickoff),
            sides={s: _node(nd) for s, nd in m.sides.items()}))
    return out


def poly_tokens(universe: list) -> list:
    """Every distinct Polymarket token id across the universe (poly market-channel subscription)."""
    seen: dict = {}
    for qm in universe:
        for n in qm.sides.values():
            if n.poly_token_id:
                seen[n.poly_token_id] = 1
    return list(seen)


def kalshi_tickers(universe: list) -> list:
    """Every distinct Kalshi ticker across the universe (Kalshi WS subscription)."""
    seen: dict = {}
    for qm in universe:
        for n in qm.sides.values():
            if n.kalshi_ticker:
                seen[n.kalshi_ticker] = 1
    return list(seen)


_SPORTS = ("soccer", "mlb", "tennis", "ufc")


def load_trees(paths_by_sport: Optional[dict] = None, *, previous: Optional[dict] = None,
               log: Any = None) -> dict:
    """Load every sport's match tree from disk (missing -> None). Returns {sport: tree|None}.

    A tree that will not PARSE right now does NOT empty that sport's universe: we keep the tree we
    already had for it (``previous``) and say so loudly. Before this, a JSONDecodeError from a torn read
    propagated out of here, through ``build_universe``, into maker_rt's event loop and killed the LIVE
    process — three times on 2026-07-29, cancelling every resting order each time. The writer is atomic
    now (``tree_builder._atomic_write_json``), so a torn read should be impossible; this is the belt to
    that braces, and it also covers a genuinely corrupt file on disk, which atomicity cannot."""
    from .. import tree_builder
    prev = previous or {}
    out: dict = {}
    failed: list = []
    for sport in _SPORTS:
        p = gz_config.paths_for_sport(sport).tree_path
        if not os.path.exists(p):
            out[sport] = None
            continue
        try:
            out[sport] = tree_builder.load_tree(p)
        except (ValueError, OSError) as exc:        # unparseable / unreadable -> keep what we had
            out[sport] = prev.get(sport)
            failed.append((sport, exc))
    if failed and log:
        log.error("[MAKER_RT] %d tree(s) unreadable this pass — kept the PREVIOUS tree for each rather "
                  "than dropping its markets: %s",
                  len(failed), "; ".join(f"{s}: {e}" for s, e in failed))
    return out


def tree_mtimes() -> dict:
    """{sport: mtime} for the tree files (0 when absent) — the driver reloads on change (so quotes
    re-anchor to a refreshed start_utc when the hourly rebuild slides a tennis 'not before' time)."""
    out: dict = {}
    for sport in _SPORTS:
        p = gz_config.paths_for_sport(sport).tree_path
        out[sport] = os.path.getmtime(p) if os.path.exists(p) else 0.0
    return out
