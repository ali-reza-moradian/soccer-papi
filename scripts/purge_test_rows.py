"""Remove unit-test rows that leaked into a live maker_rt events CSV.

Before tests/conftest.py isolated GENZ_DIR, ``MakerState.record()`` appended to the REAL dated events
CSV whenever a test recorded an event. One full suite run injected ~2,900 rows into
data/genz/maker_rt_20260723.csv — including 177 "fill" and 166 "hedge_locked" rows that never
happened — which corrupts every downstream read of quotes / fills / pnl.

Test rows are identified ONLY by a timestamp in the future relative to the run time. That is a safe,
conservative discriminator: the maker stamps rows with utcnow() as it writes them, so a real row can
never carry a future ts, while the fixed datetimes in the tests (18:00:00Z, 23:59:00Z) do. Rows are
never matched on content, so a real row that merely looks unusual is untouched.

Keeps a .bak and prints exactly what it removed. Dry-run by default.

Run:  python -m scripts.purge_test_rows data/genz/maker_rt_20260723.csv [--apply]
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="the dated maker_rt events CSV to clean")
    ap.add_argument("--apply", action="store_true", help="rewrite the file (default: dry-run)")
    ap.add_argument("--cutoff", default=None,
                    help="ISO ts; rows STRICTLY AFTER this are removed (default: now, UTC)")
    args = ap.parse_args()

    cutoff = args.cutoff or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"file   : {args.path}")
    print(f"cutoff : {cutoff}  (rows with ts strictly after this are test artifacts)")

    with open(args.path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        rd = csv.DictReader(fh)
        header = rd.fieldnames
        keep, drop = [], []
        for r in rd:
            (drop if (r.get("ts") or "") > cutoff else keep).append(r)

    if not drop:
        print("nothing to remove.")
        return 0
    by_ev = collections.Counter(r["event"] for r in drop)
    by_ts = collections.Counter((r.get("ts") or "") for r in drop)
    print(f"\nremoving {len(drop)} row(s), keeping {len(keep)}:")
    for ev, n in by_ev.most_common():
        print(f"    {ev:16} {n:>6}")
    print("  distinct future timestamps:")
    for ts, n in by_ts.most_common(10):
        print(f"    {ts}  x{n}")

    if not args.apply:
        print("\ndry-run; pass --apply to rewrite.")
        return 0
    bak = args.path + ".pretestpurge.bak"
    if not os.path.exists(bak):
        shutil.copy2(args.path, bak)
    tmp = args.path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(keep)
    os.replace(tmp, args.path)
    print(f"\nrewrote {args.path} ({len(keep)} rows); backup {os.path.basename(bak)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
