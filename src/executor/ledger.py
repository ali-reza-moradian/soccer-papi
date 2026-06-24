"""Trade ledger (Phase 4): data/executor/trade_ledger.csv.

One row per LIVE execution attempt, keyed by a ``trade_id``. A full record is appended on SUBMIT
(fixture, market, both tickers/tokens, sides, intended size, quote prices, modeled edge,
fingerprint); the same row is then UPDATED with realized fills (per-leg count/usd/avg price,
slippage vs quote) and any unhedged_kalshi / unhedged_poly flags + unwind event and its cost.

Dry-run NEVER writes here (it writes data/executor/dryrun_log.csv instead) — the ledger is the
real-money record only. Schema mirrors the sibling bot's where sensible.
"""
from __future__ import annotations

import csv
import os
import time
from typing import Any

from . import config as exec_config

LEDGER_COLUMNS = [
    "trade_id", "submit_utc", "update_utc", "status", "fingerprint",
    "fixture", "market",
    "kalshi_ticker", "kalshi_side", "poly_token", "poly_side",
    "n_legs", "legs_json",
    "intended_size", "kalshi_quote_price", "poly_quote_price", "modeled_edge_pct",
    "kalshi_fill_count", "kalshi_fill_usd", "kalshi_avg_price", "kalshi_slippage",
    "poly_fill_shares", "poly_fill_usd", "poly_avg_price", "poly_slippage",
    "residual_shares",
    "unhedged_kalshi", "unhedged_poly", "unwind_event", "unwind_cost",
    "realized_pnl", "note",
]


class Ledger:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or exec_config.LEDGER_PATH

    def _read(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def _write_all(self, rows: list[dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in LEDGER_COLUMNS})

    def append_submit(self, record: dict[str, Any]) -> str:
        """Append a SUBMIT row; returns its trade_id (generated if not supplied)."""
        rows = self._read()
        trade_id = str(record.get("trade_id") or f"T{int(time.time()*1000)}")
        row = {k: record.get(k, "") for k in LEDGER_COLUMNS}
        row["trade_id"] = trade_id
        row["submit_utc"] = record.get("submit_utc") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row["status"] = record.get("status", "submitted")
        rows.append(row)
        self._write_all(rows)
        return trade_id

    def update(self, trade_id: str, **fields: Any) -> bool:
        """Update the row with ``trade_id`` in place. Returns True if a row was updated."""
        rows = self._read()
        found = False
        for r in rows:
            if r.get("trade_id") == trade_id:
                for k, v in fields.items():
                    if k in LEDGER_COLUMNS:
                        r[k] = v
                r["update_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                found = True
                break
        if found:
            self._write_all(rows)
        return found

    def rows(self) -> list[dict[str, Any]]:
        return self._read()

    def today_live_counters(self, day_utc: str | None = None) -> dict[str, float]:
        """Aggregate today's LIVE spend / loss / trade-count from the ledger (UTC day).

        Only rows that actually transacted (a non-zero fill or an unwind) count toward spend; a
        negative realized_pnl contributes to loss. Used by the daily-cap guardrails."""
        day = day_utc or time.strftime("%Y-%m-%d", time.gmtime())
        spend = loss = 0.0
        trades = 0
        for r in self._read():
            if not str(r.get("submit_utc", "")).startswith(day):
                continue
            trades += 1
            try:
                spend += float(r.get("kalshi_fill_usd") or 0) + float(r.get("poly_fill_usd") or 0)
            except ValueError:
                pass
            try:
                pnl = float(r.get("realized_pnl") or 0)
                if pnl < 0:
                    loss += -pnl
            except ValueError:
                pass
        return {"trades": trades, "spend_usd": spend, "loss_usd": loss}
