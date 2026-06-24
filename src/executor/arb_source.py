"""Load detected CLEAN kalshi<->poly arbs for the executor (read-only).

Reads the scanner's output CSV (data/arbitrage_opportunities.csv) and yields only the CLEAN
two-venue arbs — exactly one kalshi leg + one polymarket leg, no 1xbet/other book. The executor
NEVER writes this file; it only consumes detected opportunities.
"""
from __future__ import annotations

import csv
import json
import os
from typing import Any

from .resolve import ResolveError, normalize_arb

DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "arbitrage_opportunities.csv")


def _row_to_arb(row: dict[str, str]) -> dict[str, Any]:
    """Map a scanner CSV row to the executor's arb-dict shape (legs parsed from legs_json)."""
    try:
        legs = json.loads(row.get("legs_json") or "[]")
    except (ValueError, TypeError):
        legs = []
    return {
        "match": row.get("match", ""),
        "fixture_id": row.get("fixture_id"),
        "market": row.get("market", ""),
        "signature": row.get("signature", ""),
        "detected_at": row.get("detected_at_et"),
        "roi_pct": _f(row.get("roi_pct")),
        "max_profit": _f(row.get("max_profit")),
        "legs": legs,
    }


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_clean_arbs(csv_path: str | None = None) -> list[dict[str, Any]]:
    """Return clean kalshi<->poly arbs from the CSV, best (highest ROI) first."""
    path = csv_path or DEFAULT_CSV
    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            arb = _row_to_arb(row)
            try:
                normalize_arb(arb)        # validates exactly one kalshi + one poly leg
            except ResolveError:
                continue
            out.append(arb)
    out.sort(key=lambda a: a.get("roi_pct", 0.0), reverse=True)
    return out
