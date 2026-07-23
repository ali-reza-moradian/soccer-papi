"""Shared pytest fixtures — and the belt to the guard's braces.

A test must never be able to write live trading state. That is enforced in TWO independent layers:

  1. STRUCTURAL (src/genz/maker_rt/config.py) — every runtime artifact is named in ``RUNTIME_FILES``
     and resolved by ``runtime_path()``, which calls ``assert_writable()``. Under pytest that RAISES
     for any path outside a temp dir. The same guard is repeated at each write site
     (``_atomic_json``, ``_append_csv``, the two hand-rolled atomic writes in pregame_exec), so an
     explicit path argument or a brand-new call site is covered too.
  2. THIS FIXTURE — points ``GENZ_DIR``/``OPS_DIR`` at a per-test tmp dir so tests have somewhere
     real to write and the guard never has to fire in normal use.

Layer 2 alone was what we had before, and it was not enough: it is a convention, and conventions get
forgotten. It was forgotten twice in one session — first for the tuning file, then for the events
CSV, where a single suite run injected 2,904 rows (including 177 fabricated "fill" rows) into the
live ledger and the running maker read them back as a 0.7% realized locked_net that never happened.
Layer 1 turns "remember to isolate" into "cannot write". If a future refactor drops this fixture, the
guard fails the test loudly instead of corrupting production data.
"""
from __future__ import annotations

import pytest

from src.genz.maker_rt import config as mrt_config


@pytest.fixture(autouse=True)
def _isolate_maker_rt_paths(tmp_path, monkeypatch):
    """Point every maker_rt runtime artifact at a per-test tmp dir.

    Only the two BASE DIRECTORIES are patched — every individual path derives from them through
    ``config.runtime_path()``, so this cannot drift out of sync with the file list the way patching
    per-file constants did."""
    ops = tmp_path / "ops"
    genz = tmp_path / "genz"
    for d in (ops, genz):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mrt_config, "OPS_DIR", str(ops))
    monkeypatch.setattr(mrt_config, "GENZ_DIR", str(genz))
    return tmp_path
