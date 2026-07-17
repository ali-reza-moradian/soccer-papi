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


def build_universe(trees: dict, now_ts: float, *, max_games: int = 20,
                   expire_before_kickoff_s: int = 120, horizon_hours: Optional[dict] = None) -> list:
    """Every settlement-clean, 2-outcome market of the nearest ``max_games`` games by kickoff, admitted
    from now until kickoff + the sport's in-play horizon. ``trees`` maps sport -> loaded tree dict;
    ``horizon_hours`` maps sport -> hours after kickoff a node stays admitted (default {} -> drop at
    kickoff, the pre-in-play behavior). The driver assigns each node its pre/gap/inplay phase at
    quote time; the universe only decides admission."""
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
    keep = {g for g, _ in sorted(game_kick.items(), key=lambda kv: kv[1])[:max(0, int(max_games))]}
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


def load_trees(paths_by_sport: Optional[dict] = None) -> dict:
    """Load the soccer + MLB + tennis match trees from disk (missing -> None). Returns {sport: tree|None}."""
    from .. import tree_builder
    out: dict = {}
    for sport in _SPORTS:
        p = gz_config.paths_for_sport(sport).tree_path
        out[sport] = tree_builder.load_tree(p) if os.path.exists(p) else None
    return out


def tree_mtimes() -> dict:
    """{sport: mtime} for the tree files (0 when absent) — the driver reloads on change (so quotes
    re-anchor to a refreshed start_utc when the hourly rebuild slides a tennis 'not before' time)."""
    out: dict = {}
    for sport in _SPORTS:
        p = gz_config.paths_for_sport(sport).tree_path
        out[sport] = os.path.getmtime(p) if os.path.exists(p) else 0.0
    return out
