"""The SPORT ADAPTER seam for the GenZ tree builder.

`build_tree` (tree_builder.py) is sport-agnostic: it asks a :class:`SportSpec` to (1) discover the
games in the window plus whatever per-build Polymarket context it needs, and (2) turn each game into
one tree entry (its paired nodes + unmatched list + coverage/meta). Soccer's spec (SoccerSpec, in
tree_builder.py) wraps the EXISTING functions verbatim so its output is byte-for-byte unchanged;
MLBSpec (sports_mlb.py) implements the same three methods against the MLB market shapes.

A tree entry (the value stored at tree['games'][game_id]) is the dict the engine's collect_markets
consumes: it MUST carry at least {home, away, kickoff_utc, nodes, unmatched}. Each node is the
existing tree-node shape (market_type, market_key, side, line, kind, confidence, kalshi_ticker,
kalshi_side, poly_token_id, poly_side, + optional poly_fee_*/settlement fields).
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from . import config as gz_config


@runtime_checkable
class SportSpec(Protocol):
    """One sport's discovery + pairing rules. Duck-typed; the Protocol is for documentation/checking."""

    name: str

    def paths(self) -> gz_config.SportPaths:
        """The sport's isolated runtime-file set (tree/snapshot/heartbeat/arbs/papermaker)."""
        ...

    def game_id(self, game: Any) -> str:
        """Stable per-game key used for tree['games'][...] (soccer: the Kalshi event suffix)."""
        ...

    def discover_games(self, kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
                       now: Any, log: Any = None) -> tuple[list[Any], Any]:
        """Return (games, poly_context). ``poly_context`` is handed back to :meth:`pair_markets`
        unchanged (soccer: the whole series' events fetched once; mlb: same, for the fallback scan)."""
        ...

    def pair_markets(self, kalshi_client: Any, poly_client: Any, game: Any, poly_ctx: Any,
                     cfg: gz_config.GenzConfig, *, log: Any = None) -> Optional[dict[str, Any]]:
        """Build ONE game's tree entry (paired nodes + unmatched + coverage/meta), or None to skip it."""
        ...
