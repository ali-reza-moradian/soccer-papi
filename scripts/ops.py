"""Pure decision logic for the run_all supervisor (scripts/run_all.ps1).

Extracted here so the start-if-missing / STOP_ALL / heartbeat rules — AND the full component list
(name + process-match substring + launch command) — are unit-testable WITHOUT spawning processes and
live in ONE place. run_all.ps1 owns the OS actions (enumerate processes, Start-Process HIDDEN, kill)
but delegates BOTH "which components are missing?" (`missing`) and "how do I launch each?" (`specs`)
to this module, so ADDING a component (e.g. the MLB loops) needs NO edit to run_all.ps1.

Each component's launch command is a template with {py} / {repo} / {data} placeholders the supervisor
fills in. genz/og/tree/mlb are their .ps1 WRAPPERS (fresh-interpreter loops — never `run --loop`);
http is the python static file server.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Component:
    name: str
    match: str      # substring that identifies this component's process in a command line
    cmd: str        # inner PowerShell launch command; {py}/{repo}/{data} filled by the supervisor


COMPONENTS: list[Component] = [
    Component("http", "http.server 8080", "Set-Location '{data}'; & '{py}' -m http.server 8080"),
    Component("tree", "run_tree_loop.ps1", "& '{repo}\\scripts\\run_tree_loop.ps1'"),
    Component("genz", "run_genz_loop.ps1", "& '{repo}\\scripts\\run_genz_loop.ps1'"),
    Component("og", "run_og_loop.ps1", "& '{repo}\\scripts\\run_og_loop.ps1'"),
    # MLB — a SECOND sport, fully isolated from soccer (own tree/price loops, own data files, own logs).
    Component("mlb_tree", "run_mlb_tree_loop.ps1", "& '{repo}\\scripts\\run_mlb_tree_loop.ps1'"),
    Component("mlb", "run_mlb_loop.ps1", "& '{repo}\\scripts\\run_mlb_loop.ps1'"),
    # TENNIS — a THIRD sport, fully isolated (own tree/price loops, own data files, own logs).
    Component("tennis_tree", "run_tennis_tree_loop.ps1", "& '{repo}\\scripts\\run_tennis_tree_loop.ps1'"),
    Component("tennis", "run_tennis_loop.ps1", "& '{repo}\\scripts\\run_tennis_loop.ps1'"),
    # UFC — a FOURTH sport, fully isolated (own tree/price loops, own data files, own logs).
    Component("ufc_tree", "run_ufc_tree_loop.ps1", "& '{repo}\\scripts\\run_ufc_tree_loop.ps1'"),
    Component("ufc", "run_ufc_loop.ps1", "& '{repo}\\scripts\\run_ufc_loop.ps1'"),
    # maker_rt (#11) — the realtime maker/hedger (SHADOW: real sockets, paper quotes, zero orders).
    Component("maker_rt", "run_maker_rt_loop.ps1", "& '{repo}\\scripts\\run_maker_rt_loop.ps1'"),
]
STOP_FLAG = "STOP_ALL"


def missing_components(running: Iterable[Optional[str]],
                       components: list[Component] = COMPONENTS) -> list[str]:
    """Component names whose match substring appears in NO running command line -> (re)start these."""
    cmds = [c for c in running if c]
    return [c.name for c in components if not any(c.match in cmd for cmd in cmds)]


def component_specs(components: list[Component] = COMPONENTS) -> list[dict]:
    """The launch spec for every component: [{name, match, cmd}] — consumed by run_all.ps1 (`specs`)."""
    return [{"name": c.name, "match": c.match, "cmd": c.cmd} for c in components]


def stop_requested(ops_dir: str) -> bool:
    """True when the STOP_ALL flag file exists -> the supervisor kills all components and exits."""
    return os.path.exists(os.path.join(ops_dir, STOP_FLAG))


def double_supervised(running_excluding_self: Iterable[Optional[str]]) -> bool:
    """True if ANOTHER run_all.ps1 supervisor is already running (refuse to start a second)."""
    return any(c and "run_all.ps1" in c for c in running_excluding_self)


def heartbeat_payload(ts: str, pids: dict[str, Optional[int]]) -> dict:
    """The supervisor heartbeat written each sweep: {ts, components:{name: pid|null}}."""
    return {"ts": ts, "components": dict(pids)}


def _main(argv: list[str]) -> int:
    """`python -m scripts.ops missing` reads running command lines (one per line) from stdin and prints
    the names of the components that need (re)starting. `specs` prints the launch specs as JSON.
    `stop-requested <dir>` exits 0 if STOP_ALL."""
    cmd = argv[0] if argv else ""
    if cmd == "missing":
        for name in missing_components(ln.rstrip("\r\n") for ln in sys.stdin):
            print(name)
        return 0
    if cmd == "specs":
        print(json.dumps(component_specs()))
        return 0
    if cmd == "stop-requested":
        return 0 if stop_requested(argv[1] if len(argv) > 1 else ".") else 1
    sys.stderr.write("usage: python -m scripts.ops {missing|specs|stop-requested <dir>}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
