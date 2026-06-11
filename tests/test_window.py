"""Rolling 2-day scan window (resolved in src/config.py) and its Telegram banner label."""
from __future__ import annotations

from datetime import datetime, timezone

from src import formatting as fmt
from src.config import _rolling_window, load_config


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


def test_load_config_defaults_to_rolling_window_when_no_dates_set(monkeypatch, tmp_path):
    # With no FROM_DATE/TO_DATE in the environment (the cron case), load_config must populate a
    # rolling window: from = a current UTC instant, to = end of (UTC today + 2 days) 23:59:59Z.
    for var in ("FROM_DATE", "TO_DATE"):
        monkeypatch.delenv(var, raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("tournaments:\n  pinned_ids: [16]\n", encoding="utf-8")

    cfg = load_config(str(cfg_file))
    now = datetime.now(timezone.utc)
    exp_from, exp_to = _rolling_window(now)
    # `to` is deterministic to the second within a single test run; `from` only to the day.
    assert cfg.to_utc == exp_to
    assert cfg.from_utc.startswith(now.strftime("%Y-%m-%d"))
    assert cfg.from_utc.endswith("Z")


def test_load_config_explicit_dispatch_override_still_wins(monkeypatch, tmp_path):
    # An explicit FROM_DATE/TO_DATE (workflow_dispatch) overrides the rolling default, per-field.
    monkeypatch.setenv("FROM_DATE", "2026-01-01T00:00:00Z")
    monkeypatch.setenv("TO_DATE", "2026-01-02T00:00:00Z")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("tournaments:\n  pinned_ids: [16]\n", encoding="utf-8")

    cfg = load_config(str(cfg_file))
    assert cfg.from_utc == "2026-01-01T00:00:00Z"
    assert cfg.to_utc == "2026-01-02T00:00:00Z"


def test_window_label_is_compact_utc():
    assert fmt.window_label("2026-06-10T00:01:00Z", "2026-06-12T23:59:59Z") == "Jun 10 – Jun 12 UTC"
    assert fmt.window_label(None, "2026-06-12T23:59:59Z") == ""
