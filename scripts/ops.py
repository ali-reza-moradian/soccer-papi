"""Pure decision logic for the run_all supervisor (scripts/run_all.ps1).

Extracted here so the start-if-missing / STOP_ALL / heartbeat rules are unit-testable WITHOUT spawning
processes. run_all.ps1 owns the OS actions (enumerate processes via Get-CimInstance, Start-Process
HIDDEN, kill) but delegates the "which components are missing?" decision to this module (`missing`
subcommand), so the rule lives in ONE place. The COMPONENTS match substrings below are the contract.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable, Optional

# (name, cmdline match substring) — ONE persistent process per component. genz/og/tree are their .ps1
# WRAPPERS (the fresh-interpreter loops — never `run --loop`); http is the python static file server.
COMPONENTS: list[tuple[str, str]] = [
    ("http", "http.server 8080"),
    ("tree", "run_tree_loop.ps1"),
    ("genz", "run_genz_loop.ps1"),
    ("og", "run_og_loop.ps1"),
]
STOP_FLAG = "STOP_ALL"


def missing_components(running: Iterable[Optional[str]],
                       components: list[tuple[str, str]] = COMPONENTS) -> list[str]:
    """Component names whose match substring appears in NO running command line -> (re)start these."""
    cmds = [c for c in running if c]
    return [name for name, match in components if not any(match in c for c in cmds)]


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
    the names of the components that need (re)starting. `stop-requested <dir>` exits 0 if STOP_ALL."""
    cmd = argv[0] if argv else ""
    if cmd == "missing":
        for name in missing_components(ln.rstrip("\r\n") for ln in sys.stdin):
            print(name)
        return 0
    if cmd == "stop-requested":
        return 0 if stop_requested(argv[1] if len(argv) > 1 else ".") else 1
    sys.stderr.write("usage: python -m scripts.ops {missing|stop-requested <dir>}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
