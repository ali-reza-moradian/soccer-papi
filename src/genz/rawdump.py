"""RAW EVIDENCE dumper — the ground truth every pairing rule is written against.

`python -m src.genz.cli dump-raw --sport <s> --game <game_id>` writes two files under data/genz/raw/:

    <sport>_<gid>_kalshi.json   the full Kalshi event: every market with its COMPLETE rules texts
    <sport>_<gid>_poly.json     the full Polymarket gamma event: every market with its description

These are PUBLIC market metadata (no secrets, no positions) and are copied into tests/fixtures/raw_*
so the family-registry selectors + settlement-facet parsers are tested against REAL captured texts,
never assumptions. Nothing here prices, pairs, or trades — it only fetches + serializes.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from . import config as gz_config

RAW_DIR = os.path.join(gz_config.GENZ_DIR, "raw")


def _spec_for(sport: str):
    if sport == "mlb":
        from . import sports_mlb
        return sports_mlb.MLB_SPEC
    if sport == "tennis":
        from . import sports_tennis
        return sports_tennis.TENNIS_SPEC
    if sport == "ufc":
        from . import sports_ufc
        return sports_ufc.UFC_SPEC
    from . import tree_builder
    return tree_builder.SOCCER_SPEC


def _kalshi_raw(sport: str, game: Any) -> dict[str, Any]:
    """The raw Kalshi side: the event ticker + every raw market dict (each carries rules_primary /
    rules_secondary — the full settlement texts). Shape differs per sport (MLB splits game vs total)."""
    if sport == "mlb":
        return {"sport": "mlb", "event_suffix": getattr(game, "event_suffix", ""),
                "home": game.home, "away": game.away,
                "game_markets": list(getattr(game, "game_markets", [])),
                "total_markets": list(getattr(game, "total_markets", []))}
    if sport == "soccer":
        return {"sport": "soccer", "game_id": getattr(game, "game_id", ""),
                "note": "soccer Kalshi markets are pulled per-series at pair time; re-run build-tree "
                        "with logging for the full per-series dump", "markets": []}
    # UFC / tennis: one event, all per-player markets on game.markets. UFC also carries the sibling-series
    # markets (distance/method/rounds) that feed the family registry, captured here as full evidence.
    out = {"sport": sport, "event_ticker": getattr(game, "event_ticker", getattr(game, "game_id", "")),
           "players": [getattr(game, "fighter_a", getattr(game, "player_a", "")),
                       getattr(game, "fighter_b", getattr(game, "player_b", ""))],
           "markets": list(getattr(game, "markets", []))}
    extra = list(getattr(game, "extra_markets", []) or [])
    if extra:
        out["extra_markets"] = extra                       # KXUFCDISTANCE / KXUFCMOV / KXUFCROUNDS
    return out


def _poly_event(sport: str, game: Any, poly_ctx: Any, poly_client: Any, log: Any) -> Optional[dict]:
    """Resolve + return the full raw Polymarket gamma event for this game via the sport's own resolver."""
    series_events = poly_ctx.get("series_events") if isinstance(poly_ctx, dict) else (poly_ctx or [])
    if sport == "ufc":
        from . import sports_ufc
        ev, _method = sports_ufc.resolve_poly_event(game, series_events, log)
        return ev
    if sport == "tennis":
        from . import sports_tennis
        ev, _method = sports_tennis.resolve_poly_event(game, series_events, poly_client, log)
        return ev
    if sport == "mlb":
        from . import sports_mlb
        learned = poly_ctx.get("learned") if isinstance(poly_ctx, dict) else {}
        return sports_mlb.resolve_poly_event(game, series_events, learned or {}, poly_client, log)
    if sport == "soccer":
        from . import tree_builder
        sibs = tree_builder.game_sibling_events(series_events, game)
        return {"slug": getattr(game, "poly_base_slug", ""), "sibling_events": sibs}
    return None


def dump_raw(sport: str, game_id: str, kalshi_client: Any, poly_client: Any,
             cfg: gz_config.GenzConfig, *, now: Optional[datetime] = None, log: Any = None,
             out_dir: Optional[str] = None) -> dict[str, Any]:
    """Discover the sport's games, locate ``game_id``, and write its full raw Kalshi + Poly dumps.
    Returns {found, game_id, kalshi_path, poly_path, available} (available = all game ids when not
    found). Never raises on a missing game — it lists what IS available so the caller can retry."""
    now = now or datetime.now(timezone.utc)
    spec = _spec_for(sport)
    games, poly_ctx = spec.discover_games(kalshi_client, poly_client, cfg, now=now, log=log)
    game = next((g for g in games if spec.game_id(g) == game_id), None)
    if game is None:
        return {"found": False, "game_id": game_id, "available": [spec.game_id(g) for g in games]}
    kalshi_raw = _kalshi_raw(sport, game)
    poly_raw = _poly_event(sport, game, poly_ctx, poly_client, log)
    out_dir = out_dir or RAW_DIR
    os.makedirs(out_dir, exist_ok=True)
    safe = str(game_id).replace("/", "_").replace(":", "_")
    kpath = os.path.join(out_dir, f"{sport}_{safe}_kalshi.json")
    ppath = os.path.join(out_dir, f"{sport}_{safe}_poly.json")
    _write(kpath, {"fetched_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **kalshi_raw})
    _write(ppath, {"fetched_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sport": sport,
                   "game_id": game_id, "event": poly_raw})
    return {"found": True, "game_id": game_id, "kalshi_path": kpath, "poly_path": ppath,
            "kalshi_market_count": _count_markets(kalshi_raw),
            "poly_market_count": len((poly_raw or {}).get("markets") or []) if isinstance(poly_raw, dict) else 0}


def _count_markets(kraw: dict) -> int:
    return (len(kraw.get("markets") or []) + len(kraw.get("game_markets") or [])
            + len(kraw.get("total_markets") or []))


def _write(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
