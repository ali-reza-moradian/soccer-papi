"""Tests for scripts/ops.py — the run_all supervisor's pure start-if-missing / STOP_ALL decision logic
(testable without spawning any process)."""
from __future__ import annotations

import io

from scripts import ops


def test_missing_components_start_if_missing():
    running = ["powershell -File C:/bots/soccer-papi/scripts/run_og_loop.ps1", "python.exe -m http.server 8080"]
    assert ops.missing_components(running) == ["tree", "genz"]      # http + og alive -> only tree, genz


def test_all_running_none_missing():
    running = ["a http.server 8080", "b run_tree_loop.ps1", "c run_genz_loop.ps1", "d run_og_loop.ps1"]
    assert ops.missing_components(running) == []


def test_all_missing_when_nothing_running():
    assert ops.missing_components([]) == ["http", "tree", "genz", "og"]
    assert ops.missing_components([None, ""]) == ["http", "tree", "genz", "og"]   # blanks ignored


def test_stop_requested(tmp_path):
    assert ops.stop_requested(str(tmp_path)) is False
    (tmp_path / ops.STOP_FLAG).write_text("")
    assert ops.stop_requested(str(tmp_path)) is True


def test_double_supervised():
    assert ops.double_supervised(["powershell -NoProfile -File C:/x/scripts/run_all.ps1"]) is True
    assert ops.double_supervised(["python -m http.server 8080", None, ""]) is False


def test_heartbeat_payload():
    hb = ops.heartbeat_payload("2026-07-15T00:00:00Z", {"http": 111, "tree": 222, "genz": None, "og": 333})
    assert hb == {"ts": "2026-07-15T00:00:00Z",
                  "components": {"http": 111, "tree": 222, "genz": None, "og": 333}}


def test_missing_cli_reads_stdin(monkeypatch, capsys):
    """`python -m scripts.ops missing` reads running command lines from stdin, prints the missing ones."""
    monkeypatch.setattr("sys.stdin", io.StringIO("x http.server 8080\ny run_genz_loop.ps1\n"))
    rc = ops._main(["missing"])
    assert rc == 0
    assert capsys.readouterr().out.split() == ["tree", "og"]        # http + genz alive


def test_stop_requested_cli(tmp_path):
    assert ops._main(["stop-requested", str(tmp_path)]) == 1        # no flag -> exit 1
    (tmp_path / ops.STOP_FLAG).write_text("")
    assert ops._main(["stop-requested", str(tmp_path)]) == 0        # flag present -> exit 0
