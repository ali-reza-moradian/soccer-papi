"""LEDGER TRUTH — retroactive `fill_untracked` rows for the 2026-07-23 invisible-fill incident.

Three real-money Kalshi executions never reached the events CSV. Two were genuinely INVISIBLE (the
bot rested them, the venue filled them, and every detection path was blind — see
tests/test_maker_rt_invisible_fills.py). One was the deliberate v2 order-migration SMOKE round-trip,
which is tracked by the smoke harness but was likewise never written to the daily ledger.

Every field below is VENUE TRUTH, pulled from Kalshi REST (/portfolio/fills, /portfolio/orders,
/portfolio/settlements) and reproduced in the docstring of each row so the numbers are auditable
without re-querying:

  1. mrt-84-1784773684564  order cf946ebb  KXMLSTEAMTOTAL-26JUL22SKCMIN-SKC3
     BUY 50 YES @ $0.0200 maker (fees $0), filled 2026-07-23T02:28:07.051777Z
     settled 02:46:34Z market_result=no, revenue $0        -> realized -$1.00
  2. mrt-941-1784776707907 order c99cef07  KXMLSTEAMTOTAL-26JUL22SJORL-ORL3
     BUY 5 YES @ $0.5900 maker (fees $0), filled 2026-07-23T03:18:29.218764Z
     settled 04:21:13Z market_result=yes, revenue $5.00    -> realized +$2.05
  3. exec-1784751509895 / mrt-smoke-unwind  KXMLBGAME-26JUL221910BALBOS-BOS  (SMOKE, intentional)
     BUY 6 YES @ $0.55 taker (fee $0.1040) 2026-07-22T20:18:29.965338Z, then
     SELL 6 YES @ $0.54 taker (fee $0.1044) 2026-07-22T20:18:30.448914Z, flat in 0.5s
     -> -3.30 + 3.24 - 0.2084                              -> realized -$0.2684

  NET = -1.00 + 2.05 - 0.2684 = +$0.7816 (~ +$0.78)

Idempotent: re-running never duplicates a row (each is keyed by its venue order id in `reason`).
Run:  python -m scripts.backfill_untracked_fills [--apply]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.genz.maker_rt.state import CSV_COLUMNS  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "genz")

# (day, row) — `reason` carries the venue order id, which is the idempotency key.
ROWS = [
    ("20260723", {
        "ts": "2026-07-23T02:28:07Z", "day": "20260723", "event": "fill_untracked", "mode": "live",
        "sport": "soccer", "phase": "inplay", "game": "26JUL22SKCMIN",
        "market_key": "team_total|kansas city|2.5", "side": "over", "direction": "rest-kalshi",
        "rest_venue": "kalshi", "hedge_venue": "polymarket", "quote_price": 0.02, "size": 50,
        "realized_pnl_usd": -1.0, "fill_ts": "2026-07-23T02:28:07Z",
        "reason": "cf946ebb-9a2d-47b5-b527-4961efc987f1 INVISIBLE_FILL unhedged; "
                  "settled no 2026-07-23T02:46:34Z revenue $0.00 cost $1.00",
    }),
    ("20260723", {
        "ts": "2026-07-23T03:18:29Z", "day": "20260723", "event": "fill_untracked", "mode": "live",
        "sport": "soccer", "phase": "inplay", "game": "26JUL22SJORL",
        "market_key": "team_total|orlando|2.5", "side": "over", "direction": "rest-kalshi",
        "rest_venue": "kalshi", "hedge_venue": "polymarket", "quote_price": 0.59, "size": 5,
        "realized_pnl_usd": 2.05, "fill_ts": "2026-07-23T03:18:29Z",
        "reason": "c99cef07-df2a-4675-948d-1120497410c6 INVISIBLE_FILL unhedged; "
                  "settled yes 2026-07-23T04:21:13Z revenue $5.00 cost $2.95",
    }),
    ("20260722", {
        "ts": "2026-07-22T20:18:30Z", "day": "20260722", "event": "fill_untracked", "mode": "live",
        "sport": "mlb", "phase": "pre", "game": "26JUL221910BALBOS", "market_key": "ml2",
        "side": "BOS", "direction": "rest-kalshi", "rest_venue": "kalshi", "hedge_venue": "polymarket",
        "quote_price": 0.55, "size": 6, "hedge_avg": 0.54, "hedge_fee": 0.2084,
        "realized_pnl_usd": -0.2684, "fill_ts": "2026-07-22T20:18:29Z",
        "reason": "exec-1784751509895/mrt-smoke-unwind SMOKE_ROUNDTRIP intentional taker buy+unwind, "
                  "flat in 0.5s; never written to the daily ledger",
    }),
]


def _header(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        try:
            return next(csv.reader(fh))
        except StopIteration:
            return []


def _existing_keys(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return {(r.get("event"), (r.get("reason") or "").split()[0] if r.get("reason") else "")
                for r in csv.DictReader(fh)}


def _drop_rows(path: str, event: str) -> int:
    """Remove every row with ``event`` (used to undo a misaligned append). Returns rows removed."""
    hdr = _header(path)
    keep, dropped = [], 0
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("event") == event:
                dropped += 1
                continue
            keep.append(r)
    if dropped:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=hdr, extrasaction="ignore")
            w.writeheader()
            w.writerows(keep)
        os.replace(tmp, path)
    return dropped


def _migrate_header(path: str) -> list:
    """Bring an older daily CSV up to the current CSV_COLUMNS, purely ADDITIVELY: existing columns keep
    their order and values, missing ones are appended empty. Needed because the 2026-07-22 file predates
    ``realized_pnl_usd``/``hedge_order_id`` — appending a current-schema row to it would MISALIGN every
    field past the first missing column. Keeps a .bak. Returns the resulting header."""
    hdr = _header(path)
    if not hdr or not [c for c in CSV_COLUMNS if c not in hdr]:
        return hdr or list(CSV_COLUMNS)
    added = [c for c in CSV_COLUMNS if c not in hdr]
    new_hdr = hdr + added
    print(f"[MIGRATE] {os.path.basename(path)}: +{len(added)} column(s) {added}")
    bak = path + ".bak"
    if not os.path.exists(bak):
        os.replace(path, bak)
    else:                                     # already backed up once; read from the live file
        os.replace(path, path + ".prev")
        bak = path + ".prev"
    n = 0
    with open(bak, "r", encoding="utf-8", errors="replace", newline="") as src, \
            open(path, "w", encoding="utf-8", newline="") as dst:
        rd = csv.DictReader(src)
        w = csv.DictWriter(dst, fieldnames=new_hdr, extrasaction="ignore")
        w.writeheader()
        for row in rd:
            w.writerow(row)
            n += 1
    print(f"[MIGRATE] {os.path.basename(path)}: {n} row(s) rewritten (backup {os.path.basename(bak)})")
    return new_hdr


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the rows (default: dry-run)")
    ap.add_argument("--undo", action="store_true",
                    help="remove every fill_untracked row (to redo a bad append) and exit")
    args = ap.parse_args()
    if args.undo:
        for day in sorted({d for d, _ in ROWS}):
            path = os.path.join(DATA_DIR, f"maker_rt_{day}.csv")
            print(f"[UNDO] {day}: removed {_drop_rows(path, 'fill_untracked')} row(s)")
        return 0
    total = 0.0
    written = 0
    for day, row in ROWS:
        path = os.path.join(DATA_DIR, f"maker_rt_{day}.csv")
        key = (row["event"], row["reason"].split()[0])
        total += float(row["realized_pnl_usd"])
        if key in _existing_keys(path):
            print(f"[SKIP] {day} {row['game']} already present ({key[1][:12]}...)")
            continue
        print(f"[{'WRITE' if args.apply else 'DRY '}] {day} {row['game']:<20} "
              f"{row['size']:>3} @ {row['quote_price']:<6} -> {row['realized_pnl_usd']:+.4f}")
        if args.apply:
            new = not os.path.exists(path)
            # ALWAYS write in the FILE's own column order — an older daily CSV can be missing columns,
            # and blindly using CSV_COLUMNS shifts every value past the gap into the wrong field.
            hdr = list(CSV_COLUMNS) if new else _migrate_header(path)
            with open(path, "a", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=hdr, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
            written += 1
    print(f"\nnet realized across the three untracked executions: {total:+.4f} USD")
    print(f"{written} row(s) written." if args.apply else "\ndry-run; pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
