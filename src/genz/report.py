"""Read + aggregate the genz_arbs feed so the dry-run evidence base is READABLE.

genz_arbs.csv appends a row every cycle (~20s), so one persistent arb produces many identical rows.
The RAW file(s) are the full evidence log; this module collapses them into UNIQUE arbs keyed by
(game, market_type, line, side_a, side_b), reporting how many cycles each PERSISTED across
(seen_count) plus first/last-seen and the latest + median implied cost.

Persistence across cycles is the key signal: a real, stable arb recurs cycle after cycle; noise /
stale prices flash once and vanish. So ranking by seen_count surfaces the arbs worth trusting.
"""
from __future__ import annotations

import csv
import os
from statistics import median
from typing import Any, Optional

from . import config as gz_config

# The identity of a UNIQUE arb — independent of ts_utc, price movement, or duplicate cycle logging.
UNIQUE_KEY = ("game", "market_type", "line", "side_a", "side_b")


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_rows(paths: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """All genz_arbs rows across the legacy + dated csv files (or the given paths)."""
    paths = paths if paths is not None else gz_config.arbs_csv_paths()
    rows: list[dict[str, Any]] = []
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, "r", encoding="utf-8", newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse raw rows into UNIQUE arbs. Each carries the key fields + seen_count (how many cycles it
    persisted), first_seen / last_seen, the LATEST sighting's prices/status, and the median implied
    cost across all sightings."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        key = tuple(str(r.get(k, "")) for k in UNIQUE_KEY)
        groups.setdefault(key, []).append(r)
    out: list[dict[str, Any]] = []
    for key, rs in groups.items():
        rs = sorted(rs, key=lambda r: str(r.get("ts_utc", "")))
        latest = rs[-1]
        implieds = [x for x in (_f(r.get("implied_cost")) for r in rs) if x is not None]
        out.append({
            **dict(zip(UNIQUE_KEY, key)),
            "seen_count": len(rs),
            "first_seen": rs[0].get("ts_utc", ""),
            "last_seen": latest.get("ts_utc", ""),
            "latest_implied_cost": latest.get("implied_cost", ""),
            "latest_roi_pct": latest.get("roi_pct", ""),
            "median_implied_cost": round(median(implieds), 4) if implieds else "",
            "venue_a": latest.get("venue_a", ""), "price_a": latest.get("price_a", ""),
            "venue_b": latest.get("venue_b", ""), "price_b": latest.get("price_b", ""),
            "latest_status": latest.get("exec_status", ""),
            "would_trade": latest.get("would_trade", ""),
            "confidence": latest.get("confidence", ""),
        })
    return out


def rank(uniques: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most PERSISTENT first (seen_count desc), then tightest median implied cost (asc)."""
    def med(u: dict[str, Any]) -> float:
        m = _f(u.get("median_implied_cost"))
        return m if m is not None else 1e9
    return sorted(uniques, key=lambda u: (-int(u.get("seen_count", 0)), med(u)))


def format_report(uniques: list[dict[str, Any]], limit: int = 50) -> str:
    """A compact ranked table of unique arbs (persistence first) for the CLI."""
    ranked = rank(uniques)[:limit]
    lines = [f"{len(uniques)} unique arb(s) in the feed (showing {len(ranked)} by persistence; "
             f"seen = # of cycles it recurred):",
             f"{'seen':>4}  {'med_impl':>8}  {'last_impl':>9}  {'roi%':>6}  {'wt':>2}  "
             f"{'game':<14}  {'market':<24}  legs (venue)"]
    for u in ranked:
        legs = f"{u['side_a']}({str(u.get('venue_a', '?'))[:1].upper()})+{u['side_b']}({str(u.get('venue_b', '?'))[:1].upper()})"
        mkt = f"{u['market_type']} {u['line']}".strip()
        lines.append(f"{u['seen_count']:>4}  {str(u.get('median_implied_cost', '')):>8}  "
                     f"{str(u.get('latest_implied_cost', '')):>9}  {str(u.get('latest_roi_pct', '')):>6}  "
                     f"{str(u.get('would_trade', ''))[:1]:>2}  {u['game'][:14]:<14}  {mkt[:24]:<24}  {legs}")
    return "\n".join(lines)
