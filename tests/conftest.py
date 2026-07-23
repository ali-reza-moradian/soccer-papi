"""Shared pytest fixtures.

The important one is ``_isolate_ops_dir``: it points ``maker_rt.config.OPS_DIR`` at a per-test tmp
directory for EVERY test, so no unit test can ever write into the live ``data/ops`` state.

This is not hypothetical. When ``MakerState.record()`` gained a ``persist_tuning()`` call on fill
outcomes, three existing tests that record ``hedge_locked`` / ``hedge_unwound`` events immediately
started writing their synthetic counters into the real ``data/ops/maker_rt_tuning.json`` — and the
running maker loaded them on its next restart, so the live panel briefly reported a fabricated
``fills_per_100_quotes`` off 100 test quotes and a 0.7% locked net that never happened. Poisoning
production trading metrics from a unit test is exactly the class of bug that must be impossible
rather than remembered, so the isolation is autouse and global.
"""
from __future__ import annotations

import pytest

from src.genz.maker_rt import config as mrt_config


@pytest.fixture(autouse=True)
def _isolate_ops_dir(tmp_path, monkeypatch):
    """Redirect maker_rt's ops directory to a tmp dir for the duration of every test."""
    ops = tmp_path / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mrt_config, "OPS_DIR", str(ops))
    return ops
