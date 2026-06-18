"""Tests for the shadow-book scoreboard (src/scoreboard.py): unlock metric, window, suspicious."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

from src import scoreboard

FUNDED = {"kalshi", "polymarket", "pinnacle", "1xbet"}
NOW = datetime(2026, 6, 17, 20, 0, 0, tzinfo=timezone.utc)


def _legs(*pairs):
    return json.dumps([{"book": b, "outcome": o} for b, o in pairs])


def _write_csv(path, rows):
    cols = ["detected_at_et", "match", "market", "roi_pct", "suspicious", "legs_json"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _row(dt, match, roi, legs, suspicious="false", market="1x2"):
    return {"detected_at_et": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "match": match, "market": market, "roi_pct": roi, "suspicious": suspicious,
            "legs_json": legs}


def test_unlock_metric_counts_single_missing_book(tmp_path):
    path = str(tmp_path / "c.csv")
    _write_csv(path, [
        # skybet is the ONLY non-funded leg -> opening skybet unlocks this arb.
        _row(NOW - timedelta(hours=1), "A vs B", 0.8, _legs(("skybet", "1"), ("kalshi", "X"), ("1xbet", "2"))),
        # skybet again on a different game -> unlock #2.
        _row(NOW - timedelta(hours=2), "C vs D", 1.2, _legs(("skybet", "1"), ("polymarket", "2"))),
        # unibet + matchbook BOTH non-funded -> neither alone unlocks (len(nonfunded)==2).
        _row(NOW - timedelta(hours=3), "E vs F", 2.0, _legs(("unibet", "1"), ("matchbook", "2"))),
    ])
    rows = scoreboard.build_scoreboard(path, FUNDED, window_hours=48, now=NOW)
    by = {r["book"]: r for r in rows}
    assert by["skybet"]["unlock_count"] == 2
    assert by["skybet"]["distinct_games"] == 2
    assert by["skybet"]["avg_roi_pct"] == 1.0 and by["skybet"]["best_roi_pct"] == 1.2
    # unibet/matchbook appear but unlock nothing on their own.
    assert by["unibet"]["unlock_count"] == 0 and by["unibet"]["total_appearances"] == 1
    assert by["matchbook"]["unlock_count"] == 0
    # Ranked by unlock_count desc -> skybet first.
    assert rows[0]["book"] == "skybet"


def test_window_and_suspicious_excluded(tmp_path):
    path = str(tmp_path / "c.csv")
    _write_csv(path, [
        _row(NOW - timedelta(hours=1), "A vs B", 0.8, _legs(("skybet", "1"), ("kalshi", "2"))),
        _row(NOW - timedelta(hours=99), "C vs D", 9.9, _legs(("skybet", "1"), ("kalshi", "2"))),  # too old
        _row(NOW - timedelta(hours=1), "E vs F", 150.0, _legs(("skybet", "1"), ("kalshi", "2")), suspicious="true"),
    ])
    rows = scoreboard.build_scoreboard(path, FUNDED, window_hours=48, now=NOW)
    by = {r["book"]: r for r in rows}
    assert by["skybet"]["unlock_count"] == 1          # only the in-window, non-suspicious one
    assert by["skybet"]["best_roi_pct"] == 0.8


def test_distinct_arb_dedup_keeps_best_roi(tmp_path):
    path = str(tmp_path / "c.csv")
    legs = _legs(("skybet", "1"), ("kalshi", "2"))
    _write_csv(path, [
        _row(NOW - timedelta(hours=2), "A vs B", 0.5, legs),   # same structural arb, two scans
        _row(NOW - timedelta(hours=1), "A vs B", 0.9, legs),
    ])
    rows = scoreboard.build_scoreboard(path, FUNDED, window_hours=48, now=NOW)
    sky = next(r for r in rows if r["book"] == "skybet")
    assert sky["unlock_count"] == 1 and sky["best_roi_pct"] == 0.9   # deduped, best ROI kept


def test_format_digest_and_empty():
    rows = [{"book": "skybet", "unlock_count": 12, "total_appearances": 15, "distinct_games": 7,
             "avg_roi_pct": 0.8, "best_roi_pct": 2.1},
            {"book": "unibet", "unlock_count": 0, "total_appearances": 3, "distinct_games": 0,
             "avg_roi_pct": 0.0, "best_roi_pct": 0.0}]
    msg = scoreboard.format_digest(rows, 48)
    assert "Books to fund next" in msg and "skybet" in msg and "unlock 12 arb" in msg
    assert "unibet" not in msg                          # 0 unlocks -> not suggested
    assert scoreboard.format_digest([], 48) is None
