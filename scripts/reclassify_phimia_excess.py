"""RECLASSIFY the 2026-07-28 PHIMIA settlement: split its naked excess out of HEDGED lifetime P&L.

The live reconciler booked the whole market as one HEDGED pair:

    KXMLBGAME-26JUL271840PHIMIA-PHI SETTLED net $+8.1627 ROI +6.68% on $122.23
      [kalshi yes 122sh cost $116.03 -> $0.00; polymarket buy 130.393sh cost $6.20 -> $130.39]

But the legs are UNEQUAL. They are a hedge only up to 122 shares; the extra 8.393 Poly shares had no
Kalshi complement at all — an over-fill from the $-sized FAK sweep that happened to land on the
winning side. Splitting on venue truth (cost/settlement pro-rated by share count):

    HEDGED pair   122 sh  cost $121.83  -> $122.00   net  +$0.169   (+0.14%)   <-- the maker's edge
    NAKED excess  8.393sh cost $  0.40  -> $  8.393  net  +$7.994   (+2003%)   <-- luck, not edge
    ------------------------------------------------------------------------------------------
    total                                              net  +$8.163   (= the venue-truth number)

So a +0.14% hedged pair was being published as "+6.68% ROI". Same rule as the 2026-07-25 UFC ghost:
the money is real and stays in ``settled_pnl_lifetime``, but it belongs in the UNTRACKED bucket so the
HEDGED-only number (lifetime - untracked) is not flattered by a windfall the strategy did not earn.

This script only RECLASSIFIES — it never changes the total. ``settled.py`` now performs this split
automatically for any future unequal-leg settlement; this is the one-off backfill for the row that was
already booked. Idempotent (guarded by a marker key).

Run with the maker STOPPED (deploy window) so it does not race the live tuning writer:

    python -m scripts.reclassify_phimia_excess            # dry-run
    python -m scripts.reclassify_phimia_excess --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.genz.maker_rt import config as mrt_config          # noqa: E402

MARKER = "phimia_excess_reclassified"

# Venue truth, straight off the 01:56:05Z settlement line.
K_SHARES, K_COST, K_SETTLE = 122.0, 116.03, 0.0
P_SHARES, P_COST, P_SETTLE = 130.393, 6.20, 130.393


def split() -> dict:
    """The hedged/naked split, pro-rated by share count (same arithmetic as settle._reconcile_pair)."""
    paired = min(K_SHARES, P_SHARES)
    excess = P_SHARES - paired
    p_cost_ps = P_COST / P_SHARES
    hedged_cost = K_COST + p_cost_ps * paired
    hedged_rev = K_SETTLE + (P_SETTLE / P_SHARES) * paired
    naked_cost = p_cost_ps * excess
    naked_rev = (P_SETTLE / P_SHARES) * excess
    return {"paired": paired, "excess": excess,
            "hedged_net": hedged_rev - hedged_cost, "hedged_cost": hedged_cost,
            "naked_net": naked_rev - naked_cost, "naked_cost": naked_cost}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry-run)")
    args = ap.parse_args()

    path = mrt_config.runtime_path("tuning")
    if not path or not os.path.exists(path):
        print(f"tuning file not found: {path}")
        return 1
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    s = split()
    print(f"  paired {s['paired']:g} sh -> hedged net ${s['hedged_net']:+.4f} on ${s['hedged_cost']:.2f}")
    print(f"  excess {s['excess']:g} sh -> naked  net ${s['naked_net']:+.4f} on ${s['naked_cost']:.2f}")
    print(f"  total  ${s['hedged_net'] + s['naked_net']:+.4f} (unchanged by this script)")

    if data.get(MARKER):
        print("\nalready reclassified — nothing to do (idempotent).")
        return 0

    total = float(data.get("settled_pnl_lifetime", 0.0) or 0.0)
    unt_before = float(data.get("settled_pnl_untracked_lifetime", 0.0) or 0.0)
    unt_after = unt_before + s["naked_net"]
    trades_before = int(data.get("settled_trades", 0) or 0)

    print(f"\n  settled_pnl_lifetime            ${total:.4f}  (UNCHANGED — real money, real total)")
    print(f"  settled_pnl_untracked_lifetime  ${unt_before:.4f} -> ${unt_after:.4f}")
    print(f"  HEDGED-only (lifetime - untracked) ${total - unt_before:+.4f} -> ${total - unt_after:+.4f}")
    print(f"  settled_trades                  {trades_before} -> {trades_before + 1}  (the market is now 2 rows)")

    if not args.apply:
        print("\ndry-run — re-run with --apply to write.")
        return 0

    shutil.copyfile(path, path + ".pre_phimia_reclass.bak")
    data["settled_pnl_untracked_lifetime"] = round(unt_after, 4)
    data["settled_trades"] = trades_before + 1
    data[MARKER] = True
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    print(f"\napplied. backup: {os.path.basename(path)}.pre_phimia_reclass.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
