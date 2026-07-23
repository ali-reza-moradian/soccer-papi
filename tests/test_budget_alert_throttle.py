"""The budget-exhausted Telegram is throttled to once per 6h (src/run.py).

On an exhausted OddsPapi key the legacy soccer OG scanner fired 'Arb bot paused: only N API requests
left' every ~5 min forever. The scanner is retired (scripts/ops.py), but the throttle is a belt on the
alert itself so even a manual relaunch on a dead key cannot spam. State is persisted because run.py is
a fresh interpreter per scan cycle.
"""
from __future__ import annotations

import json

from src.run import _budget_alert_due


def test_first_call_alerts_then_suppresses_within_window(tmp_path):
    p = str(tmp_path / "og_budget_alert.json")
    t0 = 1_000_000.0
    assert _budget_alert_due(t0, path=p) is True             # first ever -> alert
    assert _budget_alert_due(t0 + 60, path=p) is False        # 1 min later -> suppressed
    assert _budget_alert_due(t0 + 5 * 3600, path=p) is False  # 5h later -> still suppressed
    assert _budget_alert_due(t0 + 6 * 3600 + 1, path=p) is True   # just past 6h -> alert again
    assert _budget_alert_due(t0 + 6 * 3600 + 61, path=p) is False  # and re-suppressed


def test_state_persists_across_a_fresh_interpreter(tmp_path):
    """The throttle must survive the process — a scan cycle is a brand-new python each time."""
    p = str(tmp_path / "og_budget_alert.json")
    assert _budget_alert_due(1000.0, path=p) is True
    # a "new process" only sees the file; the in-memory nothing carries over
    assert _budget_alert_due(2000.0, path=p) is False
    assert json.loads(open(p, encoding="utf-8").read())["last_ts"] == 1000.0


def test_corrupt_state_fails_open(tmp_path):
    """A missed throttle costs one extra alert; a swallowed alert could hide a real problem. Fail OPEN."""
    p = tmp_path / "og_budget_alert.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert _budget_alert_due(1000.0, path=str(p)) is True


def test_custom_window(tmp_path):
    p = str(tmp_path / "og_budget_alert.json")
    assert _budget_alert_due(0.0, path=p, every_s=100.0) is True
    assert _budget_alert_due(50.0, path=p, every_s=100.0) is False
    assert _budget_alert_due(101.0, path=p, every_s=100.0) is True
