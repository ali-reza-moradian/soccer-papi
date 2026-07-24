"""CANARY: the guard that makes writing live trading state from a test structurally impossible.

Every test in this file deliberately defeats the isolation fixture (by restoring the REAL GENZ_DIR /
OPS_DIR) and then asserts the write is REFUSED. If any of these ever fails, a test run can silently
corrupt production data — which is not hypothetical: one suite run injected 2,904 rows including 177
fabricated "fill" rows into the live events ledger, and the running maker read them back as a 0.7%
realized locked_net that never happened.

Nothing here writes anything. That is the point: each case must raise BEFORE touching the filesystem.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt.state import MakerState, _atomic_json, bump_restart

NOW = datetime(2026, 7, 23, 18, 0, 0, tzinfo=timezone.utc)
#: the real, on-disk production directories — restored inside each test to simulate a missing fixture
REAL_GENZ = os.path.join(mrt_config.REPO_ROOT, "data", "genz")
REAL_OPS = os.path.join(mrt_config.REPO_ROOT, "data", "ops")


@pytest.fixture
def live_dirs(monkeypatch):
    """Undo the autouse isolation: point maker_rt back at the REAL data dirs, as a broken/absent
    conftest would. The guard is then the ONLY thing standing between a test and production data."""
    monkeypatch.setattr(mrt_config, "GENZ_DIR", REAL_GENZ)
    monkeypatch.setattr(mrt_config, "OPS_DIR", REAL_OPS)


def _snapshot(paths):
    return {p: (os.path.exists(p), os.path.getmtime(p) if os.path.exists(p) else None) for p in paths}


# --------------------------------------------------------------------------- #
# the resolver refuses to even HAND OUT a live path under pytest                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind,fmt", [
    ("events", {"day": "20260723"}), ("heartbeat", {}), ("summary", {}),
    ("runstate", {}), ("tuning", {}), ("traded_tokens", {}), ("orphan", {}), ("stop_all", {}),
])
def test_runtime_path_refuses_every_live_artifact(live_dirs, kind, fmt):
    with pytest.raises(mrt_config.LiveStateWriteUnderTest) as ei:
        mrt_config.runtime_path(kind, **fmt)
    msg = str(ei.value)
    assert "REFUSED" in msg, "the message must be unmistakable"
    assert "conftest" in msg, "and must point at the fix"


def test_runtime_path_allows_tmp(tmp_path):
    """The isolation fixture's paths resolve normally — the guard only blocks LIVE ones."""
    p = mrt_config.runtime_path("tuning")
    assert mrt_config.under_tmp(p) and str(tmp_path) in p


def test_unknown_runtime_kind_is_a_hard_error():
    with pytest.raises(KeyError):
        mrt_config.runtime_path("not_a_real_artifact")


# --------------------------------------------------------------------------- #
# the WRITE SITES refuse too — an explicit path argument is not an escape hatch  #
# --------------------------------------------------------------------------- #
def test_atomic_json_refuses_an_explicit_live_path():
    """write_summary/write_heartbeat accept an explicit path; that must not bypass the guard."""
    victim = os.path.join(REAL_GENZ, "maker_rt_summary.json")
    before = _snapshot([victim])
    with pytest.raises(mrt_config.LiveStateWriteUnderTest):
        _atomic_json(victim, {"pwned": True})
    assert _snapshot([victim]) == before, "the live file must be untouched"


def test_write_heartbeat_and_summary_refuse_explicit_live_paths():
    st = MakerState()
    for name in ("maker_rt_heartbeat.json", "maker_rt_summary.json"):
        victim = os.path.join(REAL_GENZ, name)
        before = _snapshot([victim])
        with pytest.raises(mrt_config.LiveStateWriteUnderTest):
            if "heartbeat" in name:
                st.write_heartbeat("live", {}, 0, NOW, path=victim)
            else:
                st.write_summary("live", {}, NOW, path=victim)
        assert _snapshot([victim]) == before


# --------------------------------------------------------------------------- #
# the real-world regressions, replayed against the live dirs                     #
# --------------------------------------------------------------------------- #
def test_recording_an_event_cannot_append_to_the_live_events_csv(live_dirs):
    """THE incident: MakerState.record() appended 2,904 rows to the live ledger."""
    victim = os.path.join(REAL_GENZ, f"maker_rt_{NOW:%Y%m%d}.csv")
    before = _snapshot([victim])
    st = MakerState()
    with pytest.raises(mrt_config.LiveStateWriteUnderTest):
        st.record({"event": "fill", "sport": "mlb", "game": "G", "market_key": "ml2",
                   "locked_net": 0.7, "locked_pnl": 0.1}, NOW)
    assert _snapshot([victim]) == before, "not one row may reach the live ledger"


def test_persist_tuning_cannot_write_live_ops(live_dirs):
    """The first regression: synthetic counters reached data/ops and the maker loaded them."""
    victim = os.path.join(REAL_OPS, "maker_rt_tuning.json")
    before = _snapshot([victim])
    st = MakerState()
    st.lifetime_quotes, st.lifetime_fills = 999, 42
    with pytest.raises(mrt_config.LiveStateWriteUnderTest):
        st.persist_tuning()
    assert _snapshot([victim]) == before


def test_bump_restart_cannot_write_live_runstate(live_dirs):
    victim = os.path.join(REAL_OPS, "maker_rt_runstate.json")
    before = _snapshot([victim])
    with pytest.raises(mrt_config.LiveStateWriteUnderTest):
        bump_restart(NOW)
    assert _snapshot([victim]) == before


# --------------------------------------------------------------------------- #
# the guard's own logic                                                          #
# --------------------------------------------------------------------------- #
def test_guard_is_inert_outside_pytest(live_dirs, monkeypatch):
    """In production PYTEST_CURRENT_TEST is unset and the guard must never interfere."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    p = os.path.join(REAL_GENZ, "maker_rt_summary.json")
    assert mrt_config.assert_writable(p) == p          # returns the path, does not raise


def test_under_tmp_classification(tmp_path):
    assert mrt_config.under_tmp(str(tmp_path / "x.json")) is True
    assert mrt_config.under_tmp(os.path.join(REAL_GENZ, "maker_rt_summary.json")) is False
    assert mrt_config.under_tmp(os.path.join(REAL_OPS, "maker_rt_tuning.json")) is False


def test_every_runtime_file_is_registered():
    """The registry is the inventory of things that must be isolated — keep it complete."""
    assert set(mrt_config.RUNTIME_FILES) == {
        "events", "heartbeat", "summary", "runstate", "tuning",
        "traded_tokens", "orphan", "settled_ledger", "expected_positions", "stop_all",
    }
