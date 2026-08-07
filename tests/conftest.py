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

import dataclasses
import os
import time

import pytest

from src.genz import config as gz_config
from src.genz.maker_rt import config as mrt_config

#: THE TIME-BOMB DETECTOR (opt-in; does nothing unless ``TIMESHIFT_DAYS`` is set).
#:
#: On 2026-08-07 two KLAMCI sweep tests went red and four more went green VACUOUSLY, because the
#: fixtures pin ``NOW`` to the 2026-08-05 incident while the code aged its 48h scope against the WALL
#: clock. 56.5h of real time later the scope pruned itself empty: the tests that demanded a find failed,
#: and — far worse — the tests asserting "the sweep stays quiet" passed for the wrong reason, so the
#: guard against the next KLAMCI silently stopped guarding.
#:
#: A CONSTANT offset leaves every elapsed-time delta correct, so the only thing it can break is code
#: comparing the wall clock to a PINNED absolute date — exactly that class. Run it periodically:
#:
#:     TIMESHIFT_DAYS=365  python -m pytest -q        # fixtures far in the PAST
#:     TIMESHIFT_DAYS=-365 python -m pytest -q        # fixtures far in the FUTURE
#:
#: KNOWN FALSE POSITIVE: anything comparing ``time.time()`` to a FILE MTIME (``catalog.file_age_hours``,
#: e.g. test_group_markets.py::test_slug_map_cache_hit_skips_network) trips this, because the shift moves
#: the process clock but not the filesystem. Those are self-consistent in real use and cannot age out.
_TIMESHIFT_DAYS = float(os.environ.get("TIMESHIFT_DAYS") or 0.0)
if _TIMESHIFT_DAYS:
    _real_time = time.time
    time.time = lambda: _real_time() + _TIMESHIFT_DAYS * 86400.0


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


@pytest.fixture(autouse=True)
def _isolate_genz_sport_paths(tmp_path, monkeypatch):
    """The SAME isolation for the genz cycle's own artifacts (snapshot / heartbeat / papermaker).

    maker_rt's files were isolated; the scanner's were not, and ``run_cycle(write=True)`` derives its
    snapshot and papermaker paths from ``paths_for_sport`` rather than from an argument. So a test that
    ran a cycle wrote into the LIVE ``data/genz`` — where a scanner process is writing the same files.
    That is two problems in one: the suite can clobber the panel's snapshot, and the suite is flaky,
    because ``os.replace`` onto a file another process holds open raises WinError 32 (it did, mid-phase-2:
    test_nothing_executes_under_default_flags failed roughly one run in eight for exactly this reason).

    ``SportPaths`` is a frozen dataclass built from module constants at IMPORT time, so patching
    ``GENZ_DIR`` alone would not move them — each instance is rebuilt with its basenames under tmp."""
    root = tmp_path / "genz_data"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gz_config, "GENZ_DIR", str(root))
    moved = {}
    for sport, paths in gz_config.SPORT_PATHS.items():
        fields = {f.name: getattr(paths, f.name) for f in dataclasses.fields(paths)}
        for key in ("tree_path", "meta_path", "snapshot_path", "heartbeat_path",
                    "papermaker_summary_path", "papermaker_state_path"):
            fields[key] = os.path.join(str(root), os.path.basename(fields[key]))
        moved[sport] = gz_config.SportPaths(**fields)
    monkeypatch.setattr(gz_config, "SPORT_PATHS", moved)
    monkeypatch.setattr(gz_config, "_SOCCER_PATHS", moved["soccer"])
    return root
