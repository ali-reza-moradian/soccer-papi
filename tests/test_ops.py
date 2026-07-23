"""Tests for scripts/ops.py — the run_all supervisor's pure start-if-missing / STOP_ALL decision logic
(testable without spawning any process)."""
from __future__ import annotations

import io

from scripts import ops


# The legacy soccer OG ('og') was RETIRED 2026-07-23 (World Cup over; OddsPapi free tier exhausted;
# og_multi covers MLB/tennis/UFC). It is intentionally absent from every list below.
_ALL = ["http", "tree", "genz", "mlb_tree", "mlb", "tennis_tree", "tennis",
        "ufc_tree", "ufc", "maker_rt", "og_multi"]


def test_og_is_retired_not_supervised():
    """The retired soccer OG must NOT be a supervised component (so run_all never respawns it)."""
    names = [c.name for c in ops.COMPONENTS]
    assert "og" not in names
    assert names == _ALL


def test_missing_components_start_if_missing():
    running = ["python.exe -m http.server 8080"]
    # only http alive -> every OTHER live component is missing (og is not a component at all)
    assert ops.missing_components(running) == ["tree", "genz", "mlb_tree", "mlb", "tennis_tree",
                                               "tennis", "ufc_tree", "ufc", "maker_rt", "og_multi"]


def test_a_running_og_loop_does_not_count_as_a_component():
    """Even if a stray run_og_loop.ps1 is still running, it maps to no component and never suppresses
    a real one from being (re)started."""
    running = ["z run_og_loop.ps1", "a http.server 8080", "b run_tree_loop.ps1", "c run_genz_loop.ps1",
               "e run_mlb_tree_loop.ps1", "f run_mlb_loop.ps1", "g run_tennis_tree_loop.ps1",
               "h run_tennis_loop.ps1", "i run_ufc_tree_loop.ps1", "j run_ufc_loop.ps1",
               "k run_maker_rt_loop.ps1", "l run_og_multi_loop.ps1"]
    assert ops.missing_components(running) == []      # all real components present; og is ignored


def test_all_running_none_missing():
    running = ["a http.server 8080", "b run_tree_loop.ps1", "c run_genz_loop.ps1",
               "e run_mlb_tree_loop.ps1", "f run_mlb_loop.ps1", "g run_tennis_tree_loop.ps1",
               "h run_tennis_loop.ps1", "i run_ufc_tree_loop.ps1", "j run_ufc_loop.ps1",
               "k run_maker_rt_loop.ps1", "l run_og_multi_loop.ps1"]
    assert ops.missing_components(running) == []


def test_all_missing_when_nothing_running():
    assert ops.missing_components([]) == _ALL
    assert ops.missing_components([None, ""]) == _ALL


def test_component_specs_include_all_sports_and_fill_templates():
    """The launch specs (consumed by run_all.ps1) list every component with a {py}/{repo}/{data}
    templated command, so adding a component needs no edit to run_all.ps1."""
    specs = ops.component_specs()
    names = [s["name"] for s in specs]
    assert names == _ALL
    assert "og" not in names                          # retired
    mlb = next(s for s in specs if s["name"] == "mlb")
    assert "{repo}" in mlb["cmd"] and "run_mlb_loop.ps1" in mlb["cmd"]
    tennis_tree = next(s for s in specs if s["name"] == "tennis_tree")
    assert "run_tennis_tree_loop.ps1" in tennis_tree["cmd"]
    ufc = next(s for s in specs if s["name"] == "ufc")
    assert "run_ufc_loop.ps1" in ufc["cmd"]
    ufc_tree = next(s for s in specs if s["name"] == "ufc_tree")
    assert "run_ufc_tree_loop.ps1" in ufc_tree["cmd"]
    maker = next(s for s in specs if s["name"] == "maker_rt")
    assert "run_maker_rt_loop.ps1" in maker["cmd"]
    og_multi = next(s for s in specs if s["name"] == "og_multi")
    assert "run_og_multi_loop.ps1" in og_multi["cmd"]


def test_stop_requested(tmp_path):
    assert ops.stop_requested(str(tmp_path)) is False
    (tmp_path / ops.STOP_FLAG).write_text("")
    assert ops.stop_requested(str(tmp_path)) is True


def test_double_supervised():
    assert ops.double_supervised(["powershell -NoProfile -File C:/x/scripts/run_all.ps1"]) is True
    assert ops.double_supervised(["python -m http.server 8080", None, ""]) is False


def test_heartbeat_payload():
    hb = ops.heartbeat_payload("2026-07-15T00:00:00Z", {"http": 111, "tree": 222, "genz": None})
    assert hb == {"ts": "2026-07-15T00:00:00Z",
                  "components": {"http": 111, "tree": 222, "genz": None}}


def test_missing_cli_reads_stdin(monkeypatch, capsys):
    """`python -m scripts.ops missing` reads running command lines from stdin, prints the missing ones."""
    monkeypatch.setattr("sys.stdin", io.StringIO("x http.server 8080\ny run_genz_loop.ps1\n"))
    rc = ops._main(["missing"])
    assert rc == 0
    assert capsys.readouterr().out.split() == ["tree", "mlb_tree", "mlb", "tennis_tree",
                                               "tennis", "ufc_tree", "ufc", "maker_rt", "og_multi"]  # http + genz alive


def test_stop_requested_cli(tmp_path):
    assert ops._main(["stop-requested", str(tmp_path)]) == 1        # no flag -> exit 1
    (tmp_path / ops.STOP_FLAG).write_text("")
    assert ops._main(["stop-requested", str(tmp_path)]) == 0        # flag present -> exit 0
