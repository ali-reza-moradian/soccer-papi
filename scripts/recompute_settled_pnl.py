"""RECOMPUTE the lifetime SETTLED-P&L from venue truth — the authoritative fix for the 2026-07-24
100x-cents corruption.

The live settled reconciler read the Kalshi HANHAL payout as ``revenue=500`` and (via the old
``>=1000`` magnitude heuristic) booked it as $500.00 instead of $5.00, so the ledger recorded:

    KXATPMATCH-26JUL24HANHAL  match_winner  net +$495.1500  ROI +10209.28%   <-- CORRUPT

The truth, verified by hand against BOTH venues:

    TBTOR   26JUL231507TBTOR    ml2            net +$0.18  on $17.19  (+1.05%)
    HANHAL  KXATPMATCH-26JUL24HANHAL match_winner net +$0.07  on $4.93   (+1.42%)
      Kalshi  5 sh HAL YES @ 41c -> cost $2.13 (incl. ~8c taker fee), WON, settled $5.00 (5 x $1)
      Poly    5 sh hanfmann @ 56c -> cost $2.80, LOST, redeemed $0.00
    -----------------------------------------------------------------------------------------
    LIFETIME  net +$0.25  on cost $22.12  across 2 settled trades

This script is AUTHORITATIVE (it OVERWRITES the settled_* counters in maker_rt_tuning.json from the
venue-truth table above — it does not increment, so a corrupt starting value is replaced, not added
to) and idempotent. It ALSO rewrites the one corrupt ``trade_settled`` CSV row in place (backed up
first) to the corrected values so the append-only ledger stops carrying the $495 landmine.

Run with the maker STOPPED (deploy window) so it does not race the live tuning writer:

    python -m scripts.recompute_settled_pnl            # dry-run
    python -m scripts.recompute_settled_pnl --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.genz.maker_rt import config as mrt_config          # noqa: E402
from src.genz.maker_rt import state as mrt_state            # noqa: E402
from src.genz.maker_rt.settle import SettledLeg, sane_settled, settled_row  # noqa: E402

# The Poly rest-leg token for the HANHAL 'hanfmann' side (from the persisted watch-set).
_HANHAL_POLY_TOKEN = "110020958709782632057714889140492282106773198641370043462308107759160147580119"

# (game, market_key, sport, settled_ts, legs) — VENUE TRUTH. The lifetime settled counters are the SUM
# of these, computed fresh (NOT added to whatever is currently persisted).
VENUE_TRUTH = [
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
    {
        "sport": "tennis", "game": "KXATPMATCH-26JUL24HANHAL", "market_key": "match_winner",
        "settled_ts": "2026-07-24T12:26:46Z",
        "legs": [
            SettledLeg("kalshi", "KXATPMATCH-26JUL24HANHAL-HAL", "yes",
                       shares=5, cost_usd=2.13, settle_value_usd=5.00),       # HAL won -> 5 x $1 = $5.00
            SettledLeg("polymarket", _HANHAL_POLY_TOKEN, "buy",
                       shares=5, cost_usd=2.80, settle_value_usd=0.00),       # hanfmann lost -> $0
        ],
    },
]


def _rows_for_truth() -> list:
    """Build the corrected trade_settled row for each venue-truth trade."""
    out = []
    for item in VENUE_TRUTH:
        row = settled_row(sport=item["sport"], game=item["game"], market_key=item["market_key"],
                          legs=item["legs"], settled_ts=item["settled_ts"],
                          market_id=item["legs"][0].instrument)
        out.append((item, row))
    return out


def _fix_corrupt_csv_rows(apply: bool) -> int:
    """Rewrite any implausible ``trade_settled`` CSV row (|ROI|>50% or |net|>$100) to venue truth,
    keyed by (game, market_key). Backs up the file first. Returns how many rows were corrected."""
    truth_by_key = {(i["game"], i["market_key"]): r for i, r in _rows_for_truth()}
    corrected = 0
    days = {i["settled_ts"][:10].replace("-", "") for i in VENUE_TRUTH}
    for day in sorted(days):
        path = mrt_config.events_path_for(day)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames or mrt_state.CSV_COLUMNS
            rows = list(reader)
        changed = False
        for r in rows:
            if r.get("event") != "trade_settled":
                continue
            try:
                net = float(r.get("realized_pnl_usd") or 0.0)
                cost = float(r.get("settled_cost_usd") or 0.0)
            except (TypeError, ValueError):
                net, cost = 0.0, 0.0
            ok, _why = sane_settled(net, cost)
            if ok:
                continue                                      # already sane -> leave it (idempotent)
            truth = truth_by_key.get((r.get("game"), r.get("market_key")))
            if truth is None:
                # No known truth -> neutralize so it can never be counted, but keep an audit trail.
                r["event"] = "trade_settled_refused"
            else:
                r["realized_pnl_usd"] = truth["realized_pnl_usd"]
                r["settled_cost_usd"] = truth["settled_cost_usd"]
                r["reason"] = truth["reason"]
            corrected += 1
            changed = True
            print(f"[{'FIX ' if apply else 'DRY '}] CSV {os.path.basename(path)} {r.get('game')} "
                  f"{r.get('market_key')}: net {net:+.2f} -> {r.get('realized_pnl_usd')}")
        if changed and apply:
            shutil.copy2(path, path + ".pre_recompute.bak")
            tmp = path + ".recompute.tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            os.replace(tmp, path)
    return corrected


def _rewrite_tuning(total_net: float, total_cost: float, trades: int, apply: bool) -> None:
    """Authoritatively OVERWRITE the settled_* counters in maker_rt_tuning.json (preserving every other
    tuning field). This is the runtime authority the heartbeat/summary read via load_tuning()."""
    obj = mrt_state.load_tuning()
    before = float(obj.get("settled_pnl_lifetime", 0.0) or 0.0)
    obj["settled_pnl_lifetime"] = round(total_net, 4)
    obj["settled_cost_lifetime"] = round(total_cost, 4)
    obj["settled_trades"] = int(trades)
    print(f"\ntuning.json settled_pnl_lifetime: {before:+.4f} -> {total_net:+.4f} "
          f"(cost ${total_cost:.2f}, {trades} trades)")
    if apply:
        path = mrt_state._tuning_path()
        shutil.copy2(path, path + ".pre_recompute.bak") if os.path.exists(path) else None
        mrt_state._atomic_json(path, obj)


def main() -> int:
    ap = argparse.ArgumentParser(description="Recompute lifetime settled-pnl from venue truth.")
    ap.add_argument("--apply", action="store_true", help="write the fix (default: dry-run)")
    args = ap.parse_args()

    total_net = total_cost = 0.0
    for item, row in _rows_for_truth():
        net, cost = float(row["realized_pnl_usd"]), float(row["settled_cost_usd"])
        ok, why = sane_settled(net, cost)
        flag = "OK" if ok else f"REFUSED({why})"
        total_net += net
        total_cost += cost
        print(f"  {item['game']:<28} {item['market_key']:<12} net {net:+.4f}  cost ${cost:6.2f}  "
              f"ROI {net / cost * 100:+.2f}%  [{flag}]")

    corrected = _fix_corrupt_csv_rows(args.apply)
    _rewrite_tuning(total_net, total_cost, len(VENUE_TRUTH), args.apply)
    print(f"\nLIFETIME settled pnl: {total_net:+.4f} USD across {len(VENUE_TRUTH)} trades; "
          f"{corrected} corrupt CSV row(s) {'corrected' if args.apply else 'to correct'}.")
    if not args.apply:
        print("dry-run; pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
