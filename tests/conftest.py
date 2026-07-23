"""Shared pytest fixtures.

``_isolate_maker_rt_paths`` redirects EVERY maker_rt runtime path at a per-test tmp directory, so no
unit test can write into live trading state. It is autouse and global on purpose.

This is not hypothetical — it was found twice in one session:

  * ``MakerState.record()`` gained a ``persist_tuning()`` call, and three tests that record
    ``hedge_locked`` / ``hedge_unwound`` (including the pre-existing
    test_state_unwind_economics_and_amber_window) wrote their synthetic counters into the real
    ``data/ops/maker_rt_tuning.json``. The running maker loaded them on its next restart and the panel
    reported a fabricated fill rate and a 0.7% realized locked net that never happened.
  * ``record()`` ALSO appends to the dated events CSV under ``GENZ_DIR``, which the first version of
    this fixture did not cover. One suite run injected ~2,900 rows — 177 "fills", 166 "hedge_locked" —
    into the live ``maker_rt_20260723.csv`` trading ledger at future timestamps.

So isolate the whole namespace, not the one file that happened to break. ``HEARTBEAT_PATH`` and
``SUMMARY_PATH`` are module-level constants read at call time, so patching the attribute is enough;
``events_path_for()`` rebuilds its path from ``GENZ_DIR`` on every call.
"""
from __future__ import annotations

import os

import pytest

from src.genz.maker_rt import config as mrt_config


@pytest.fixture(autouse=True)
def _isolate_maker_rt_paths(tmp_path, monkeypatch):
    """Point every maker_rt runtime artifact (ops state, events CSV, heartbeat, summary) at tmp."""
    ops = tmp_path / "ops"
    genz = tmp_path / "genz"
    for d in (ops, genz):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mrt_config, "OPS_DIR", str(ops))
    monkeypatch.setattr(mrt_config, "GENZ_DIR", str(genz))
    monkeypatch.setattr(mrt_config, "HEARTBEAT_PATH", os.path.join(str(genz), "maker_rt_heartbeat.json"))
    monkeypatch.setattr(mrt_config, "SUMMARY_PATH", os.path.join(str(genz), "maker_rt_summary.json"))
    return tmp_path
