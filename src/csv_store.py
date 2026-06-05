"""Append arbitrage opportunities to data/arbitrage_opportunities.csv.

History is never truncated. Rows are keyed by `signature`; if the same signature was
recorded within `dedup_minutes`, that row is UPDATED in place instead of duplicating.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

COLUMNS = [
    "detected_at_utc",
    "status",            # NEW | UPDATED
    "signature",
    "actionable",
    "bookmakers",
    "market",
    "event_date",
    "roi_pct",
    "max_liquidity",
    "match",
    "fixture_id",
    "tournament",
    "kickoff_utc",
    "market_id",
    "market_type",
    "period",
    "line",
    "legs_json",
    "arb_sum_S",
    "roi_decimal",
    "total_stake_max",
    "stake_split_json",
    "max_profit",
    "binding_book",
    "min_leg_limit",
    "shadow_books",
    "involves_exchange",
    "low_confidence",
    "suspicious",
    "bet_links_json",
]


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_rows(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_rows(path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def append_opportunities(
    path: str,
    new_rows: list[dict[str, Any]],
    now: datetime,
    dedup_minutes: float,
) -> dict[str, int]:
    """Merge `new_rows` into the CSV. Returns counts of new/updated rows.

    Each item in `new_rows` is a dict with at least the COLUMNS keys (minus status,
    which we set here). Dedup is by signature within the freshness window.
    """
    existing = _read_rows(path)

    # Index the most recent existing row per signature.
    last_idx_by_sig: dict[str, int] = {}
    for i, row in enumerate(existing):
        sig = row.get("signature")
        if sig:
            last_idx_by_sig[sig] = i

    counts = {"new": 0, "updated": 0}
    appended: list[dict[str, str]] = []

    for item in new_rows:
        sig = item.get("signature")
        item_row = {k: _stringify(item.get(k)) for k in COLUMNS}

        prev_i = last_idx_by_sig.get(sig)
        fresh = False
        if prev_i is not None:
            prev_dt = _parse_iso(existing[prev_i].get("detected_at_utc", ""))
            if prev_dt is not None:
                age_min = (now - prev_dt).total_seconds() / 60.0
                fresh = age_min <= dedup_minutes

        if fresh:
            item_row["status"] = "UPDATED"
            existing[prev_i] = item_row  # refresh the standing arb in place
            counts["updated"] += 1
        else:
            item_row["status"] = "NEW"
            appended.append(item_row)
            # New row becomes the latest for this signature.
            last_idx_by_sig[sig] = len(existing) + len(appended) - 1
            counts["new"] += 1

    _write_rows(path, existing + appended)
    return counts


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)
