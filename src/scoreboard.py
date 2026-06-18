"""Shadow-book scoreboard: 'which non-funded book should I open an account at next?'

Aggregates arbs from the opportunities CSV over a rolling window and ranks every NON-funded book by
how many arbs it would directly UNLOCK — i.e. arbs where that book is the ONLY non-funded leg, so
every other leg is already on a funded book (kalshi/polymarket/pinnacle/1xbet) and opening an account
there turns the arb bettable. Suspicious arbs are excluded.

Output feeds a "shadow_scoreboard" sheet (src/excel_log) and a once-daily Telegram digest.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from . import formatting as fmt


def _read_csv(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _arb_key(game: str, market: str, legs: list[dict]) -> tuple:
    """Structural identity of an arb: game + market + sorted (book, outcome) pairs (odds-independent),
    so the same standing arb seen across many scans is counted once."""
    pairs = tuple(sorted((str(l.get("book")), str(l.get("outcome"))) for l in legs))
    return (game, market, pairs)


def build_scoreboard(csv_path: str, funded_books: set[str], window_hours: float,
                     now: datetime) -> list[dict[str, Any]]:
    """Rank non-funded books by # of unlock-arbs within the rolling window. Excludes suspicious arbs.

    Returns a list of dicts (sorted: unlock_count desc, then avg_roi desc) with keys: book,
    unlock_count, total_appearances, distinct_games, avg_roi_pct, best_roi_pct."""
    funded = set(funded_books)
    cutoff = now - timedelta(hours=window_hours)

    # Collapse to distinct arbs in the window, keeping the best (max ROI) instance of each.
    arbs: dict[tuple, dict] = {}
    for r in _read_csv(csv_path):
        if str(r.get("suspicious", "")).lower() == "true":
            continue
        dt = fmt.parse_iso(r.get("detected_at_et"))
        if dt is None or dt < cutoff or dt > now:
            continue
        try:
            legs = json.loads(r.get("legs_json") or "[]")
        except (ValueError, TypeError):
            continue
        leg_books = [str(l.get("book")) for l in legs if l.get("book")]
        if not leg_books:
            continue
        game, market = r.get("match", ""), r.get("market", "")
        try:
            roi = float(r.get("roi_pct") or 0.0)
        except (ValueError, TypeError):
            roi = 0.0
        key = _arb_key(game, market, legs)
        prev = arbs.get(key)
        if prev is None or roi > prev["roi"]:
            arbs[key] = {"game": game, "leg_books": leg_books, "roi": roi}

    appearances: dict[str, set] = defaultdict(set)  # non-funded book -> distinct arb keys it's a leg in
    unlock: dict[str, list] = defaultdict(list)      # non-funded book -> [(game, roi)] it alone unlocks
    for key, a in arbs.items():
        nonfunded = set(a["leg_books"]) - funded
        for b in nonfunded:
            appearances[b].add(key)
        if len(nonfunded) == 1:                      # exactly one missing book -> opening it unlocks
            unlock[next(iter(nonfunded))].append((a["game"], a["roi"]))

    out: list[dict[str, Any]] = []
    for book, keys in appearances.items():
        ul = unlock.get(book, [])
        rois = [roi for _, roi in ul]
        out.append({
            "book": book,
            "unlock_count": len(ul),
            "total_appearances": len(keys),
            "distinct_games": len({g for g, _ in ul}),
            "avg_roi_pct": round(sum(rois) / len(rois), 2) if rois else 0.0,
            "best_roi_pct": round(max(rois), 2) if rois else 0.0,
        })
    out.sort(key=lambda d: (d["unlock_count"], d["avg_roi_pct"]), reverse=True)
    return out


def format_digest(rows: list[dict[str, Any]], window_hours: float, top_n: int = 5) -> str | None:
    """Telegram digest of the top unlock books. None when nothing would be unlocked."""
    ranked = [r for r in rows if r["unlock_count"] > 0][:top_n]
    if not ranked:
        return None
    lines = [f"📊 <b>Books to fund next</b> (last {int(window_hours)}h)"]
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"{i}. <b>{r['book']}</b> — would unlock {r['unlock_count']} arb(s), "
            f"avg ROI {r['avg_roi_pct']:.2f}% (best {r['best_roi_pct']:.2f}%), "
            f"{r['distinct_games']} game(s)")
    return "\n".join(lines)
