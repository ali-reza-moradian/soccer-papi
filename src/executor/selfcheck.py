"""Developer self-check: light up the executor + panel END-TO-END with NO live markets or orders.

It injects synthetic CLEAN kalshi<->poly arbs (a 2-leg and a 3-leg 1x2 variant) with fake-but-
well-formed venue IDs and INJECTED local order books, runs them through the REAL dry-run path
(``execute_arb(live=False)``) so the books are walked and slippage/fees/net-edge/arb_survived are
computed and written to data/executor/dryrun_log.csv (tagged ``source="selfcheck"``), and appends
two synthetic trade_ledger.csv rows (one filled, one leg-failure->unwind) so every panel section
has sample data to render. ``clear`` deletes ONLY the source="selfcheck" rows.

Places nothing live; changes no defaults. Used by `python -m src.executor.cli selfcheck`.
"""
from __future__ import annotations

import csv
import os
import time
from typing import Any, Optional

from . import config as exec_config
from .engine import DRYRUN_COLUMNS, execute_arb
from .ledger import LEDGER_COLUMNS, Ledger

SOURCE = "selfcheck"            # tag written into dryrun_log's source column
FP_PREFIX = "selfcheck|"        # fingerprint prefix marking selfcheck rows (dryrun + ledger)


class _InjectedBooks:
    """A MarketData stand-in serving the injected synthetic ladders by identifier (no network)."""

    def __init__(self, ladders: dict[str, list[tuple[float, float]]]) -> None:
        self.ladders = ladders

    def kalshi_ask_ladder(self, ticker: str, side: str = "YES") -> list[tuple[float, float]]:
        return list(self.ladders.get(ticker, []))

    def poly_ask_ladder(self, token_id: str) -> list[tuple[float, float]]:
        return list(self.ladders.get(token_id, []))


def _synthetic() -> tuple[list[dict[str, Any]], _InjectedBooks]:
    """The synthetic 2-leg + 3-leg arbs and their injected books (all S < 1 -> survive)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    arb2 = {
        "match": "[SELFCHECK] Alpha vs Beta", "fixture_id": "SC2", "market": "Both Teams To Score",
        "signature": FP_PREFIX + "2leg", "detected_at": now, "source": SOURCE,
        "legs": [
            {"book": "kalshi", "venue": "kalshi", "outcome": "Yes", "decimal_odds": 1.0 / 0.47,
             "limit": 200, "venue_id": "SC-K-BTTS-YES", "venue_side": "YES"},
            {"book": "polymarket", "venue": "polymarket", "outcome": "No", "decimal_odds": 1.0 / 0.48,
             "limit": 200, "venue_id": "SC-P-BTTS-NO", "venue_side": "BUY",
             "neg_risk": True, "tick_size": 0.01},
        ],
    }
    arb3 = {
        "match": "[SELFCHECK] Gamma vs Delta", "fixture_id": "SC3", "market": "Full Time Result",
        "signature": FP_PREFIX + "3leg", "detected_at": now, "source": SOURCE,
        "legs": [
            {"book": "kalshi", "venue": "kalshi", "outcome": "Home", "decimal_odds": 1.0 / 0.30,
             "limit": 200, "venue_id": "SC-K-HOME", "venue_side": "YES"},
            {"book": "kalshi", "venue": "kalshi", "outcome": "Draw", "decimal_odds": 1.0 / 0.30,
             "limit": 200, "venue_id": "SC-K-DRAW", "venue_side": "YES"},
            {"book": "polymarket", "venue": "polymarket", "outcome": "Away", "decimal_odds": 1.0 / 0.30,
             "limit": 200, "venue_id": "SC-P-AWAY", "venue_side": "BUY",
             "neg_risk": True, "tick_size": 0.01},
        ],
    }
    books = _InjectedBooks({
        "SC-K-BTTS-YES": [(0.47, 300)], "SC-P-BTTS-NO": [(0.48, 300)],
        "SC-K-HOME": [(0.30, 300)], "SC-K-DRAW": [(0.30, 300)], "SC-P-AWAY": [(0.30, 300)],
    })
    return [arb2, arb3], books


def _count_dryrun_selfcheck(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return sum(1 for r in csv.DictReader(fh) if r.get("source") == SOURCE)


def _count_ledger_selfcheck(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return sum(1 for r in csv.DictReader(fh) if str(r.get("fingerprint", "")).startswith(FP_PREFIX))


def run_selfcheck(*, cfg: Optional[exec_config.ExecConfig] = None,
                  dryrun_log_path: Optional[str] = None, ledger_path: Optional[str] = None,
                  log: Any = None) -> dict[str, Any]:
    """Write the synthetic dry-run + ledger rows through the real (dry-run) path. Returns a summary."""
    cfg = cfg or exec_config.load_exec_config()
    dpath = dryrun_log_path or exec_config.DRYRUN_LOG_PATH
    lpath = ledger_path or exec_config.LEDGER_PATH
    exec_config.ensure_dirs()

    arbs, books = _synthetic()
    results = []
    for arb in arbs:
        res = execute_arb(arb, live=False, cfg=cfg, market_data=books,
                          dryrun_log_path=dpath, log=log)
        results.append(res)

    # Two synthetic ledger rows so the ledger panel renders both a clean fill and an unwind.
    led = Ledger(lpath)
    tid_ok = led.append_submit({
        "fingerprint": FP_PREFIX + "filled", "fixture": "[SELFCHECK] Alpha vs Beta",
        "market": "Both Teams To Score", "n_legs": 2, "intended_size": 80,
        "kalshi_ticker": "SC-K-BTTS-YES", "kalshi_side": "YES", "poly_token": "SC-P-BTTS-NO",
        "poly_side": "BUY", "modeled_edge_pct": 5.0, "status": "submitted", "note": "selfcheck sample",
    })
    led.update(tid_ok, status="filled", kalshi_fill_count=80, kalshi_fill_usd=37.6,
               kalshi_avg_price=0.47, poly_fill_shares=80, poly_fill_usd=38.4, poly_avg_price=0.48,
               residual_shares=0, unhedged_kalshi=0, unhedged_poly=0, realized_pnl=4.0,
               note="selfcheck sample: clean hedge")
    tid_uw = led.append_submit({
        "fingerprint": FP_PREFIX + "unwind", "fixture": "[SELFCHECK] Gamma vs Delta",
        "market": "Full Time Result", "n_legs": 3, "intended_size": 80,
        "kalshi_ticker": "SC-K-HOME", "kalshi_side": "YES", "poly_token": "SC-P-AWAY",
        "poly_side": "BUY", "modeled_edge_pct": 6.0, "status": "submitted", "note": "selfcheck sample",
    })
    led.update(tid_uw, status="leg_failure_unwind", kalshi_fill_count=80, kalshi_fill_usd=24.0,
               kalshi_avg_price=0.30, poly_fill_shares=0, unhedged_kalshi=1, unhedged_poly=0,
               unwind_event="kalshi_market_sell", unwind_cost=5.0, realized_pnl=-5.0,
               note="selfcheck sample: poly leg failed -> auto-unwind")

    return {
        "results": results,
        "dryrun_path": dpath, "ledger_path": lpath, "log_path": exec_config.LOG_PATH,
        "dryrun_selfcheck_rows": _count_dryrun_selfcheck(dpath),
        "ledger_selfcheck_rows": _count_ledger_selfcheck(lpath),
        "dryrun_exists": os.path.exists(dpath), "ledger_exists": os.path.exists(lpath),
        "all_survived": all(getattr(r, "arb_survived", None) for r in results),
        "flags": {"enabled": cfg.enabled, "dry_run": cfg.dry_run, "live_enabled": cfg.live_enabled},
    }


def _rewrite_csv(path: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in columns})


def clear_selfcheck(*, dryrun_log_path: Optional[str] = None,
                    ledger_path: Optional[str] = None) -> dict[str, int]:
    """Delete ONLY the selfcheck rows from dryrun_log.csv (source=="selfcheck") and
    trade_ledger.csv (fingerprint starts 'selfcheck|'). Other rows are preserved. Returns counts."""
    dpath = dryrun_log_path or exec_config.DRYRUN_LOG_PATH
    lpath = ledger_path or exec_config.LEDGER_PATH

    removed_dry = 0
    if os.path.exists(dpath):
        with open(dpath, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        keep = [r for r in rows if r.get("source") != SOURCE]
        removed_dry = len(rows) - len(keep)
        if removed_dry:
            _rewrite_csv(dpath, DRYRUN_COLUMNS, keep)

    removed_led = 0
    if os.path.exists(lpath):
        with open(lpath, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        keep = [r for r in rows if not str(r.get("fingerprint", "")).startswith(FP_PREFIX)]
        removed_led = len(rows) - len(keep)
        if removed_led:
            _rewrite_csv(lpath, LEDGER_COLUMNS, keep)

    return {"dryrun_removed": removed_dry, "ledger_removed": removed_led}
