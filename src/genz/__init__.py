"""GenZ engine — a STANDALONE Kalshi<->Polymarket soccer arbitrage system.

Fully separate from the OG scanner (src/run.py): its own data path (data/genz/), its own static
match tree (data/genz/match_tree.json), its own fast price loop, and its own auto-execution. It
NEVER imports or changes run.py / arbitrage.py's behavior.

It REUSES (read-only) the proven building blocks:
  * the venue READERS in src/kalshi.py (the *_dollars / orderbook_fp price+ladder reader, the
    KXWC* event-ticker helpers) and src/polymarket.py (CLOB book reader, sibling-event/spread/totals
    parsers, the fifwc-* slug builder);
  * src/arbitrage.py for the implied-cost / ROI math and src/bookmath.py walk_book for walk-to-stake;
  * the executor's SAFETY MACHINERY in src/executor/ — guardrails, sizing, ledger, dry-run, and the
    staged poly_exec v2 + kalshi_exec placement path. ALL executor defaults are unchanged
    (enabled:false / dry_run:true / live_enabled:false), so GenZ runs measure-only until an operator
    flips those exact flags.

Two jobs (mirrors a proven 10s cross-market bot):
  JOB 1  tree_builder.py  — slow/hourly: discover games + enumerate every market on both venues and
                            pair outcomes into a STATIC match tree (deterministic rules, no LLM).
  JOB 2  engine.py        — fast/~20s loop: read live prices ONLY for tree tokens, check 2-outcome
                            best-of-both arbs, and (under the executor flags) auto-execute.
"""
from __future__ import annotations

__all__ = ["match_rules", "tree_builder", "engine", "config"]
