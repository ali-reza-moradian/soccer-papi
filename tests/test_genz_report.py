"""Tests for the GenZ genz_arbs readability layer (src/genz/report.py): the feed appends a row every
cycle, so the report DEDUPES on (game, market_type, line, side_a, side_b) and ranks by persistence
(seen_count), reading across the legacy + daily-rotated csv files."""
from __future__ import annotations

import csv

from src.genz import config as gz_config
from src.genz import report


def _row(game, mt, line, ts, impl, *, side_a="over", side_b="under", va="kalshi", vb="polymarket"):
    return {"ts_utc": ts, "game": game, "market_type": mt, "line": str(line),
            "side_a": side_a, "venue_a": va, "price_a": "0.45",
            "side_b": side_b, "venue_b": vb, "price_b": "0.50",
            "implied_cost": str(impl), "roi_pct": "5.0", "would_trade": "True",
            "exec_status": "dryrun", "confidence": "high"}


def test_aggregate_dedups_and_counts_persistence():
    rows = [
        _row("CANMAR", "corners", 8.5, "2026-07-03T16:00:00Z", 0.94),
        _row("CANMAR", "corners", 8.5, "2026-07-03T16:00:20Z", 0.95),
        _row("CANMAR", "corners", 8.5, "2026-07-03T16:00:40Z", 0.9398),   # latest
        _row("BELSEN", "total_goals", 2.5, "2026-07-03T16:00:30Z", 0.99),
    ]
    by = {a["game"]: a for a in report.aggregate(rows)}
    can = by["CANMAR"]
    assert can["seen_count"] == 3                                         # 3 cycle rows -> ONE unique arb
    assert can["first_seen"] == "2026-07-03T16:00:00Z"
    assert can["last_seen"] == "2026-07-03T16:00:40Z"
    assert can["latest_implied_cost"] == "0.9398"                        # LATEST price, not the first
    assert abs(float(can["median_implied_cost"]) - 0.94) < 1e-9          # median(0.94, 0.95, 0.9398)
    assert by["BELSEN"]["seen_count"] == 1                               # flashed once (noise signal)


def test_rank_orders_by_persistence_then_tightness():
    rows = ([_row("A", "corners", 8.5, f"2026-07-03T16:0{i}:00Z", 0.97) for i in range(5)]   # seen 5
            + [_row("B", "corners", 9.5, "2026-07-03T16:00:00Z", 0.90)])                      # seen 1, tighter
    ranked = report.rank(report.aggregate(rows))
    assert ranked[0]["game"] == "A" and ranked[0]["seen_count"] == 5     # persistence beats tightness
    assert ranked[1]["game"] == "B"


def test_unique_key_separates_lines_and_sides():
    rows = [
        _row("G", "corners", 8.5, "t1", 0.97),
        _row("G", "corners", 9.5, "t2", 0.97),                           # different line -> different arb
        _row("G", "corners", 8.5, "t3", 0.97, side_a="under", side_b="over"),   # different sides
    ]
    assert len(report.aggregate(rows)) == 3


def test_read_rows_globs_legacy_and_dated_files(tmp_path, monkeypatch):
    """The feed rotates daily; read_rows must aggregate across the legacy base file AND every
    genz_arbs_YYYYMMDD.csv, so one arb spanning days collapses to a single unique row."""
    monkeypatch.setattr(gz_config, "GENZ_DIR", str(tmp_path))
    cols = list(_row("X", "corners", 8.5, "t", 0.97).keys())
    for name, ts in (("genz_arbs.csv", "t0"), ("genz_arbs_20260703.csv", "t1"),
                     ("genz_arbs_20260704.csv", "t2")):
        with open(tmp_path / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerow(_row("X", "corners", 8.5, ts, 0.97))
    rows = report.read_rows()                                            # default -> glob all files
    assert len(rows) == 3
    assert len(report.aggregate(rows)) == 1 and report.aggregate(rows)[0]["seen_count"] == 3
