"""Rolling 2-day scan window (change #2) and its Telegram banner label."""
from __future__ import annotations

from datetime import datetime, timezone

from src import formatting as fmt
from src.config import Config, Secrets
from src.run import _resolve_window, _rolling_window


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_window_covers_today_plus_two_days_through_end_of_day():
    # Anytime Wednesday (00:01 or 23:59) -> Wed + Thu + Fri, through Fri 23:59:59Z.
    assert _rolling_window(_utc(2026, 6, 10, 0, 1)) == (
        "2026-06-10T00:01:00Z", "2026-06-12T23:59:59Z")
    assert _rolling_window(_utc(2026, 6, 10, 23, 59)) == (
        "2026-06-10T23:59:00Z", "2026-06-12T23:59:59Z")
    # Thursday 03:00 -> through Saturday 23:59:59Z.
    assert _rolling_window(_utc(2026, 6, 11, 3, 0)) == (
        "2026-06-11T03:00:00Z", "2026-06-13T23:59:59Z")


def test_window_rolls_over_month_and_year_boundaries():
    # Jun 29 -> Jul 1; Jun 30 -> Jul 2 (month-end).
    assert _rolling_window(_utc(2026, 6, 29, 12))[1] == "2026-07-01T23:59:59Z"
    assert _rolling_window(_utc(2026, 6, 30, 12))[1] == "2026-07-02T23:59:59Z"
    # Dec 30 -> Jan 1 of the next year.
    assert _rolling_window(_utc(2026, 12, 30, 12))[1] == "2027-01-01T23:59:59Z"


def test_window_uses_utc_even_for_naive_input():
    # A naive datetime is treated as UTC (no local-timezone math leaks in).
    assert _rolling_window(datetime(2026, 6, 10, 0, 1)) == (
        "2026-06-10T00:01:00Z", "2026-06-12T23:59:59Z")


def test_resolve_window_prefers_explicit_override():
    cfg = Config(raw={"target_window": {"from_utc": "2026-01-01T00:00:00Z",
                                        "to_utc": "2026-01-02T00:00:00Z"}},
                 secrets=Secrets(None, None, None))
    assert _resolve_window(cfg, _utc(2026, 6, 10, 0, 1)) == (
        "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")


def test_resolve_window_falls_back_to_rolling_when_no_dates_pinned():
    cfg = Config(raw={}, secrets=Secrets(None, None, None))
    assert _resolve_window(cfg, _utc(2026, 6, 10, 0, 1)) == (
        "2026-06-10T00:01:00Z", "2026-06-12T23:59:59Z")


def test_window_label_is_compact_utc():
    assert fmt.window_label("2026-06-10T00:01:00Z", "2026-06-12T23:59:59Z") == "Jun 10 – Jun 12 UTC"
    assert fmt.window_label(None, "2026-06-12T23:59:59Z") == ""
