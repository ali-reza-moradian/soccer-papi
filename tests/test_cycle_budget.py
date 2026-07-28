"""The cycle-budget instrumentation: is a detection cycle still finishing inside its interval?

Measured 2026-07-28 after the 19-league expansion: a soccer cycle takes ~340s against a config that
still claimed a 20s interval. The per-phase split is the point — pricing all 120 games / 584 two-way
nodes (1,168 orderbook reads) is only ~51s of it; the other ~290s is the snapshot build and papermaker
pass. Nothing in the code checked, and the panel's fixed 90s SLOW badge could not tell a healthy
6-minute cadence from a dead loop.

These pin the two things that make that visible: the heartbeat carries the sport's own cadence, and the
panel derives its thresholds from it instead of one hardcoded number for four sports.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from src.genz import config as gz_config
from src.genz.engine import CycleResult, write_heartbeat

NOW = datetime(2026, 7, 28, 21, 0, 0, tzinfo=timezone.utc)
PANEL = os.path.join(os.path.dirname(__file__), "..", "data", "genz", "papi_panel.html")


def _res() -> CycleResult:
    return CycleResult(games=120, nodes_priced=1146, nodes_unpriced=22, markets_skipped=0,
                       arbs_found=1, would_trade=0)


def test_heartbeat_carries_the_sports_own_cadence(tmp_path):
    """Without interval_s on the heartbeat the panel has nothing to judge freshness against."""
    p = tmp_path / "hb.json"
    write_heartbeat(_res(), now=NOW, path=str(p), interval_s=360.0, cycle_s=341.7)
    hb = json.loads(p.read_text(encoding="utf-8"))
    assert hb["interval_s"] == 360.0
    assert hb["cycle_s"] == 341.7
    assert hb["games"] == 120


def test_heartbeat_omits_cadence_when_unknown(tmp_path):
    """An older/partial caller must not inject a zero interval — the panel falls back to its defaults."""
    p = tmp_path / "hb.json"
    write_heartbeat(_res(), now=NOW, path=str(p))
    hb = json.loads(p.read_text(encoding="utf-8"))
    assert "interval_s" not in hb and "cycle_s" not in hb


def test_soccer_interval_is_the_measured_cadence_not_the_old_target():
    """20s was a leftover from the 2-league era: every panel read LIVE while the data was 5 cycles old.
    Whatever this is set to, it has to admit the ~340s a cycle actually takes."""
    cfg = gz_config.load_genz_config()
    assert cfg.interval_seconds >= 300, (
        f"interval_seconds={cfg.interval_seconds} is below the ~340s a measured soccer cycle takes — "
        f"the panel would report a healthy loop as SLOW/STALE forever")


def test_panel_derives_slow_and_stale_from_the_interval():
    """A fixed 90s SLOW is wrong in both directions once sports have different cadences: it can never
    fire for a 90s-interval sport, and it fires constantly for a 6-minute one."""
    html = open(PANEL, encoding="utf-8").read()
    assert "function freshLimits(" in html, "the panel still hardcodes its freshness thresholds"
    m = re.search(r"function freshLimits\(hb\)\{(.*?)\n\}", html, re.S)
    assert m, "freshLimits is not in the expected shape"
    body = m.group(1)
    assert "hb.interval_s" in body, "freshLimits ignores the cadence the engine publishes"
    assert "Math.max(90," in body and "Math.max(300," in body, \
        "the old 90/300 thresholds must stay as FLOORS so no fast sport gets less sensitive"
    assert "iv*2" in body and "iv*4" in body
    # And the caller actually uses them.
    assert re.search(r"lim\s*=\s*freshLimits\(HB\)", html)
    assert "s>lim.stale" in html and "s>lim.slow" in html
    assert not re.search(r"if\(s>300\)", html), "a hardcoded 300s STALE check survived"
