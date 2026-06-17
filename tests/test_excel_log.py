"""Tests for the Excel running log (src/excel_log.py): header, flatten, new_or_repeat."""
from __future__ import annotations

import os

from openpyxl import load_workbook

from src import excel_log


def _row(arb_id, scan_utc, game="A vs B", legs=None):
    return {
        "scan_time_local": "2026-06-17T10:00:00-04:00",
        "scan_time_utc": scan_utc,
        "game": game,
        "kickoff_time": "2026-06-17T15:00:00-04:00",
        "tournament": "World Cup",
        "market": "1x2 Full Time Result",
        "legs": legs or [
            {"book": "pinnacle", "outcome": "A", "odds": 2.10, "limit": 1500, "stake": 700},
            {"book": "1xbet", "outcome": "B", "odds": 2.05, "limit": "UNVERIFIED", "stake": 716},
        ],
        "S": 0.964, "ROI_pct": 3.73, "T_max": 1416, "total_investment": 1416,
        "guaranteed_profit": 53, "type": "REAL", "low_confidence": "N",
        "unverified_limit_books": "1xbet", "arb_id": arb_id,
    }


def test_creates_file_with_header_and_flattens_legs(tmp_path):
    path = str(tmp_path / "arbs_log.xlsx")
    n = excel_log.append_arbs(path, [_row("id1", "2026-06-17T10:00:00Z")], _Log())
    assert n == 1 and os.path.exists(path)

    wb = load_workbook(path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == excel_log.columns()
    assert "leg1_book" in header and "leg2_limit" in header and "arb_id" in header

    row = {h: v for h, v in zip(header, [c.value for c in ws[2]])}
    assert row["game"] == "A vs B"
    assert row["leg1_book"] == "pinnacle" and row["leg1_stake"] == 700
    assert row["leg2_limit"] == "UNVERIFIED"            # string preserved
    assert row["leg3_book"] is None                      # only 2 legs -> leg3 empty
    assert row["new_or_repeat"] == "NEW"                 # nothing before it


def test_new_or_repeat_against_previous_scan(tmp_path):
    path = str(tmp_path / "arbs_log.xlsx")
    log = _Log()
    # Scan 1: two arbs.
    excel_log.append_arbs(path, [_row("keep", "2026-06-17T10:00:00Z"),
                                 _row("gone", "2026-06-17T10:00:00Z")], log)
    # Scan 2: one repeat (keep) + one brand-new (fresh).
    excel_log.append_arbs(path, [_row("keep", "2026-06-17T10:15:00Z"),
                                 _row("fresh", "2026-06-17T10:15:00Z")], log)

    wb = load_workbook(path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    rows = [{h: v for h, v in zip(header, [c.value for c in r])} for r in ws.iter_rows(min_row=2)]
    scan2 = {r["arb_id"]: r["new_or_repeat"] for r in rows if r["scan_time_utc"] == "2026-06-17T10:15:00Z"}
    assert scan2 == {"keep": "REPEAT", "fresh": "NEW"}


class _Log:
    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass
