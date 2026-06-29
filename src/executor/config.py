"""Executor configuration + master safety switches + isolated paths (Phase 0).

Reads ONLY the ``executor:`` block of config.yaml (never touches the scanner's window/secret
logic) and exposes a typed ``ExecConfig`` plus the isolated data/executor/ paths and the STOP
kill-switch check. Defaults are the SAFE state: disabled + dry-run + human-confirm.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")

# Auto-load the repo-root .env on import so every executor entry point (cli, preflight, dashboard,
# selfcheck) gets KALSHI_*/POLYGON_*/POLY_* without the user re-sourcing it each terminal. real
# environment variables WIN (override=False), and the repo root is derived from __file__ so the
# current working directory does not matter. python-dotenv is a hard executor dependency; if it is
# somehow absent we degrade silently to "use whatever is already in the environment".
try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=os.path.join(REPO_ROOT, ".env"), override=False)
except ImportError:  # pragma: no cover - dotenv missing -> rely on the ambient environment
    pass

# All executor state is isolated here — no shared mutable state with the scanner.
EXEC_DIR = os.path.join(REPO_ROOT, "data", "executor")
STOP_FILE = os.path.join(EXEC_DIR, "STOP")
LEDGER_PATH = os.path.join(EXEC_DIR, "trade_ledger.csv")
DRYRUN_LOG_PATH = os.path.join(EXEC_DIR, "dryrun_log.csv")
LOG_PATH = os.path.join(EXEC_DIR, "executor.log")
HEARTBEAT_PATH = os.path.join(EXEC_DIR, "loop_heartbeat.json")


def ensure_dirs() -> None:
    """Create data/executor/ if absent. Safe to call repeatedly."""
    os.makedirs(EXEC_DIR, exist_ok=True)


@dataclass
class ExecConfig:
    """Typed view of the ``executor:`` config block. Construct via :func:`load_exec_config`."""

    # master switches
    enabled: bool = False
    dry_run: bool = True
    require_human_confirm: bool = True
    # in-play live WebSocket detection feed (Phase B), separate from the pre-game path. Default OFF.
    live_enabled: bool = False
    # guardrails
    max_per_trade_usd: float = 10.0
    max_trades_per_day: int = 20
    max_daily_spend_usd: float = 200.0
    max_daily_loss_usd: float = 50.0
    min_book_liquidity_mult: float = 1.25
    max_consecutive_errors: int = 3
    cooldown_seconds: float = 60.0
    dedupe_minutes: float = 5.0
    min_net_edge_pct_after_costs: float = 1.0
    # sizing
    volume_haircut: float = 0.80
    marketable_buffer: float = 0.01
    # endpoints
    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    poly_signature_type: int = 3

    @property
    def live_allowed(self) -> bool:
        """True only when a REAL order may be placed: master switch ON and dry-run OFF.
        (The per-placement human-confirm gate and the STOP file are checked separately.)"""
        return bool(self.enabled and not self.dry_run)


def _block(config_path: str | None) -> dict[str, Any]:
    path = config_path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    blk = raw.get("executor")
    return blk if isinstance(blk, dict) else {}


def load_exec_config(config_path: str | None = None, *, overrides: dict[str, Any] | None = None) -> ExecConfig:
    """Load ``executor:`` from config.yaml into an :class:`ExecConfig` (missing keys -> safe
    defaults). ``overrides`` (e.g. CLI flags) win over the file. Unknown keys are ignored."""
    data = dict(_block(config_path))
    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})
    fields = ExecConfig.__dataclass_fields__  # noqa: SLF001 - intentional introspection
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        if name in data and data[name] is not None:
            val = data[name]
            # Coerce to the declared type (yaml may give ints/strs).
            if f.type == "bool":
                val = bool(val)
            elif f.type == "int":
                val = int(val)
            elif f.type == "float":
                val = float(val)
            kwargs[name] = val
    return ExecConfig(**kwargs)


def stop_file_present(path: str | None = None) -> bool:
    """True if the kill-switch file exists — the executor must halt immediately when it does.
    Resolves STOP_FILE at call time so tests can redirect the module global."""
    return os.path.exists(path or STOP_FILE)


def trip_stop(reason: str, path: str | None = None) -> None:
    """Create the STOP kill-switch file with a reason, halting all future cycles. Idempotent."""
    target = path or STOP_FILE
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(f"HALTED: {reason}\n")


def clear_stop(path: str | None = None) -> bool:
    """Remove the STOP file (operator re-arm). Returns True if a file was removed."""
    target = path or STOP_FILE
    if os.path.exists(target):
        os.remove(target)
        return True
    return False
