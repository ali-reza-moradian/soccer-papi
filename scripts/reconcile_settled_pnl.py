"""SETTLED-P&L BACKFILL — the venue-truth realized pnl of a hedged trade that settled BEFORE the live
settled-pnl reconciler existed (it can only reconcile pairs booked after it shipped).

The 2026-07-23 TBTOR trade was the maker's FIRST profitable hedged bet, but the fill-time ledger booked
it as pnl=-$0.96 (the phantom-unwind bug) and HALTED on a false orphan. The REAL outcome, verified by
hand from BOTH venues' settlement/redemption:

    26JUL231507TBTOR  ml2  (rest-kalshi; hedged on Polymarket)
      Kalshi  16 sh TB YES @ 6.1c  -> cost $0.98, settled market_result=no (TB lost), revenue $0.00
                                                                                      -> leg  -$0.98
      Poly   17.4 sh TOR   @ 93.3c -> cost $16.21, redeemed $17.37 (TOR won)          -> leg  +$1.16
      -------------------------------------------------------------------------------------------
      NET  +$0.18  on cost basis $17.19  =  +1.05% ROI

(The 16-vs-17.4 share imbalance is the accidental net-long ~1.4 shares left by the phantom Kalshi
unwind selling 1 contract it never should have — see the double-unwind fix. This row records the
ACTUAL realized result of the position as it truly settled.)

Routed THROUGH state.record so the CSV row AND the lifetime settled-pnl counter (the panel/summary
authority) update together. Idempotent: re-running never double-writes (keyed by game+market_key) and
never double-counts (it checks the CSV before recording). Run this with the maker STOPPED (deploy window)
so it does not race the live tuning writer.

Run:  python -m scripts.reconcile_settled_pnl [--apply]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.genz.maker_rt import config as mrt_config          # noqa: E402
from src.genz.maker_rt import state as mrt_state            # noqa: E402
from src.genz.maker_rt.settle import SettledLeg, settled_row  # noqa: E402

# (game, market_key, sport, settled_ts, legs) — VENUE TRUTH, reproduced in the module docstring.
SETTLED = [
    {
        "sport": "mlb", "game": "26JUL231507TBTOR", "market_key": "ml2",
        "settled_ts": "2026-07-23T22:41:00Z",
        "legs": [
            SettledLeg("kalshi", "KXMLBGAME-26JUL231507TBTOR-TB", "yes",
                       shares=16, cost_usd=0.98, settle_value_usd=0.00),      # TB lost -> $0
            SettledLeg("polymarket", "TOR", "buy",
                       shares=17.4, cost_usd=16.21, settle_value_usd=17.37),  # TOR won -> redeemed
        ],
    },
]


def _existing_settled_keys(path: str) -> set:
    """(game, market_key) of every trade_settled row already in the CSV — the idempotency key."""
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return {(r.get("game"), r.get("market_key")) for r in csv.DictReader(fh)
                if r.get("event") == "trade_settled"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill venue-truth trade_settled rows.")
    ap.add_argument("--apply", action="store_true", help="write the rows (default: dry-run)")
    args = ap.parse_args()

    st = mrt_state.MakerState()
    st.load_tuning()
    total = 0.0
    written = 0
    for item in SETTLED:
        row = settled_row(sport=item["sport"], game=item["game"], market_key=item["market_key"],
                          legs=item["legs"], settled_ts=item["settled_ts"],
                          market_id=item["legs"][0].instrument)
        day = item["settled_ts"][:10].replace("-", "")
        path = mrt_config.events_path_for(day)
        key = (item["game"], item["market_key"])
        net = float(row["realized_pnl_usd"])
        total += net
        if key in _existing_settled_keys(path):
            print(f"[SKIP] {item['game']} {item['market_key']} already settled in the ledger.")
            continue
        print(f"[{'WRITE' if args.apply else 'DRY '}] {item['game']} {item['market_key']:<6} "
              f"-> net {net:+.4f}  cost ${row['settled_cost_usd']:.2f}  "
              f"ROI {net / float(row['settled_cost_usd']) * 100:+.2f}%")
        print(f"          {row['reason']}")
        if args.apply:
            now = datetime.strptime(item["settled_ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            st.record(row, now)          # writes the CSV row AND updates the lifetime settled-pnl counter
            written += 1
    print(f"\nnet realized across {len(SETTLED)} settled trade(s): {total:+.4f} USD")
    if args.apply:
        print(f"{written} row(s) written; lifetime settled pnl now ${st.settled_pnl_lifetime:+.4f} "
              f"across {st.settled_trades} trade(s).")
    else:
        print("dry-run; pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
