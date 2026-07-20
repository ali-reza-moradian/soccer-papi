"""State persistence + the per-sport cadence gate for the OG multi-sport scanner."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.og_multi import state

NOW = datetime(2026, 7, 19, 23, 0, 0, tzinfo=timezone.utc)


def test_is_due_and_mark_ran(tmp_path):
    b = str(tmp_path)
    assert state.is_due("mlb", 300, NOW, b) is True                 # never ran -> due
    state.mark_ran("mlb", NOW, b)
    assert state.is_due("mlb", 300, NOW + timedelta(seconds=100), b) is False   # within interval
    assert state.is_due("mlb", 300, NOW + timedelta(seconds=300), b) is True    # interval elapsed
    # UFC on a longer interval, same process, is independent of MLB's stamp.
    assert state.is_due("ufc", 600, NOW + timedelta(seconds=300), b) is True


def test_quota_daily_rollover(tmp_path):
    b = str(tmp_path)
    assert state.record_credits(3, NOW, b) == 3
    assert state.record_credits(2, NOW, b) == 5                     # accumulates within the UTC day
    assert state.record_credits(4, NOW + timedelta(days=1), b) == 4  # resets on the next UTC day


def test_tennis_keys_cache_freshness(tmp_path):
    b = str(tmp_path)
    assert state.load_tennis_keys(b) == {}
    state.save_tennis_keys("2026-07-19", ["tennis_atp_wimbledon"], True, b)
    cache = state.load_tennis_keys(b)
    assert cache["keys"] == ["tennis_atp_wimbledon"] and cache["billed"] is True
    assert state.tennis_keys_fresh(cache, NOW) is True
    assert state.tennis_keys_fresh(cache, NOW + timedelta(days=1)) is False   # next day -> re-discover


def test_capabilities_roundtrip_and_corrupt_selfheal(tmp_path):
    b = str(tmp_path)
    state.save_capabilities({"baseball_mlb": {"valid": ["h2h"], "dropped": {"totals": "2026-07-01T00:00:00Z"}}}, b)
    caps = state.load_capabilities(b)
    assert caps["baseball_mlb"]["dropped"]["totals"].startswith("2026-07-01")
    # A corrupt cache self-heals to the default instead of crashing a scan.
    import os
    with open(os.path.join(b, state.CAPABILITIES_NAME), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert state.load_capabilities(b) == {}


def test_og_current_path_is_data_sibling():
    p = state.og_current_path("tennis")
    assert p.endswith("og_current_tennis.json") and "og_multi" not in p   # data/ sibling, not data/og_multi/
