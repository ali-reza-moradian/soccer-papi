"""Automated pre-game arbitrage EXECUTOR for kalshi<->polymarket soccer arbs.

A FULLY SEPARATE module from the scan/detection pipeline. It only READS:
  (a) detected arbs (passed in as plain dicts), and
  (b) the read-only pricing in src/kalshi.py / src/polymarket.py (to re-pull live books).

It NEVER imports-modifies or changes the behavior of run.py / arbitrage.py / the scanner.
All executor state is isolated under data/executor/ (logs, ledger, dry-run log, STOP file).

SAFETY: the three master switches (executor.enabled / executor.dry_run /
executor.require_human_confirm) default to OFF / dry-run / confirm, and a data/executor/STOP
file halts everything. No real order is placed unless the operator explicitly flips the flags.
"""
from __future__ import annotations

__all__ = ["config", "engine", "ledger", "guardrails", "fees_sizing"]
