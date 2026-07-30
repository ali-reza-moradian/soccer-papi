"""THE MEASUREMENT GATE — the only authority for raising a cap.

The audit's Money §4 is the one conclusion that governs everything else: with space-corrected
``locked_net`` standard deviations, a +/-0.5% 95% CI on the mean edge needs roughly **10 clean hedged
fills for soccer, 13 for MLB, 33 for tennis**, and UFC has never had one. Below those counts the
measured edge is noise, and a cap raise on noise is just a larger bet on an unknown.

That conclusion was written in a report. This makes it a REPORT YOU CAN RUN, because a gate nobody can
evaluate on demand is a gate that gets rounded down to "it's probably fine by now".

Three rules the numbers here obey:

  * **Since the FIX, not since the report.** Everything booked before ``acb36a2`` (2026-07-30) is in the
    wrong price space (F14/N1: a Kalshi NO fill read in YES space turned a real -0.22% into a -49.91
    dollar "loss"). The restatement corrected the LEDGER; it could not rewrite the append-only event
    CSVs, so those rows are still on disk carrying the audit's three named fictions — +90.6657%,
    +29.7815% and -54.25%. Cutting at the report date instead of the fix date leaves all three in, and
    they drag soccer's mean to +5.955% while its median sits at -0.750%. The cut-off is a constant here,
    not a parameter, and anything excluded is COUNTED and shown rather than quietly dropped.
  * **HEDGED fills only.** ``fill_untracked`` windfalls (the +$42 UFC ghost) are luck on a naked
    position, not maker edge, and mixing them in is how a strategy congratulates itself.
  * **A verdict, in words.** MEASURING / EDGE-POSITIVE / EDGE-NEGATIVE, so the answer to "can we raise
    the cap" is a word rather than an interpretation of six numbers.
"""
from __future__ import annotations

import csv
import glob
import io
import os
import statistics
from typing import Any, Optional

from .. import config as gz_config

#: The F14 price-space fix (acb36a2). Fills booked before it are in the wrong price space and cannot be
#: averaged with corrected ones — see the module docstring.
RESTATEMENT_TS = "2026-07-30"

#: BELT, not the mechanism: a |locked_net| above this is arithmetic, not edge. The quoting path already
#: refuses an edge above ``max_plausible_edge_pct`` (5%) as a probable pricing bug, and phase 1 made
#: such a row unproducible — so anything above it on disk is pre-fix residue. Excluded rows are counted
#: into ``excluded`` and printed, because a filter that hides its own effect is how a bad number
#: survives a review.
SANITY_ABS_PCT = 5.0

#: Clean hedged fills needed per sport for a +/-0.5% 95% CI on the mean (audit Money §4, H6).
GATES: dict = {"soccer": 10, "mlb": 13, "tennis": 33, "ufc": 10}

#: The event that means "a pair actually locked" — the only row class that is maker edge.
LOCKED_EVENT = "hedge_locked"


def _rows(paths: Optional[list] = None) -> list:
    """Every LIVE ``hedge_locked`` row at or after the F14 fix, newest file last."""
    files = paths if paths is not None else sorted(
        glob.glob(os.path.join(gz_config.GENZ_DIR, "maker_rt_2*.csv")))
    out: list = []
    for f in files:
        try:
            with io.open(f, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r.get("mode") != "live" or r.get("event") != LOCKED_EVENT:
                        continue
                    if str(r.get("ts") or "") < RESTATEMENT_TS:
                        continue
                    try:
                        r["_locked"] = float(r.get("locked_net"))
                    except (TypeError, ValueError):
                        continue
                    r["_implausible"] = abs(r["_locked"]) > SANITY_ABS_PCT
                    out.append(r)
        except OSError:
            continue
    return out


def _verdict(n: int, need: int, mean_pct: Optional[float]) -> str:
    """MEASURING until the gate is met; then the sign of the mean decides.

    Deliberately NOT "positive mean -> raise the cap": clearing the gate means the number is finally
    readable, not that it is good. EDGE-POSITIVE is a licence to have the conversation."""
    if n < need or mean_pct is None:
        return "MEASURING"
    return "EDGE-POSITIVE" if mean_pct > 0 else "EDGE-NEGATIVE"


def report(paths: Optional[list] = None) -> dict:
    """{sport: {n, need, mean_pct, p50_pct, worst_pct, total_usd, verdict}} plus an ``all`` roll-up."""
    rows = _rows(paths)
    by_sport: dict = {}
    for r in rows:
        by_sport.setdefault(str(r.get("sport") or "?"), []).append(r)
    out: dict = {}
    for sport in sorted(set(GATES) | set(by_sport)):
        rs = by_sport.get(sport, [])
        excluded = sum(1 for r in rs if r["_implausible"])
        rs = [r for r in rs if not r["_implausible"]]
        nets = [r["_locked"] for r in rs]
        usd = 0.0
        for r in rs:
            try:
                usd += float(r.get("realized_pnl_usd") or 0.0)
            except (TypeError, ValueError):
                pass
        need = int(GATES.get(sport, 10))
        mean = round(statistics.fmean(nets), 4) if nets else None
        out[sport] = {
            "n": len(nets), "need": need,
            "mean_pct": mean,
            "p50_pct": round(statistics.median(nets), 4) if nets else None,
            "worst_pct": round(min(nets), 4) if nets else None,
            "total_usd": round(usd, 4),
            "verdict": _verdict(len(nets), need, mean),
            "short_by": max(0, need - len(nets)),
            "excluded": excluded,
        }
    all_nets = [r["_locked"] for r in rows if not r["_implausible"]]
    out["all"] = {
        "n": len(all_nets),
        "mean_pct": round(statistics.fmean(all_nets), 4) if all_nets else None,
        "p50_pct": round(statistics.median(all_nets), 4) if all_nets else None,
        "since": RESTATEMENT_TS,
        "gated_sports": sorted(s for s, v in out.items()
                               if s != "all" and v["verdict"] == "MEASURING"),
    }
    return out


def render(rep: Optional[dict] = None) -> str:
    """The human report. One line per sport, then the one sentence that matters."""
    rep = rep if rep is not None else report()
    hdr = (f"MEASUREMENT GATES — clean HEDGED fills since the {RESTATEMENT_TS} restatement\n"
           f"(audit Money §4: below these counts the mean edge is noise, and a cap raise on noise is a "
           f"bigger bet on an unknown)\n")
    lines = [hdr,
             f"  {'sport':<8}{'n':>4}/{'need':<5}{'mean%':>9}{'p50%':>9}{'worst%':>9}{'$':>10}   verdict",
             f"  {'-'*8}{'-'*10}{'-'*9}{'-'*9}{'-'*9}{'-'*10}   {'-'*13}"]
    for sport in sorted(k for k in rep if k != "all"):
        v = rep[sport]
        f = lambda x: "     —" if x is None else f"{x:>9.3f}"   # noqa: E731
        lines.append(f"  {sport:<8}{v['n']:>4}/{v['need']:<5}{f(v['mean_pct'])}{f(v['p50_pct'])}"
                     f"{f(v['worst_pct'])}{v['total_usd']:>10.2f}   {v['verdict']}"
                     + (f"  (need {v['short_by']} more)" if v["short_by"] else "")
                     + (f"  [{v['excluded']} pre-fix row(s) excluded]" if v.get("excluded") else ""))
    a = rep["all"]
    lines.append("")
    lines.append(f"  ALL: {a['n']} clean hedged fill(s), mean "
                 + ("—" if a["mean_pct"] is None else f"{a['mean_pct']:.3f}%")
                 + ", p50 " + ("—" if a["p50_pct"] is None else f"{a['p50_pct']:.3f}%"))
    if a["gated_sports"]:
        lines.append(f"  STILL MEASURING: {', '.join(a['gated_sports'])} — no cap raise is justified for "
                     f"these by the data that exists.")
    else:
        lines.append("  Every sport has cleared its gate; the mean edge is finally readable.")
    return "\n".join(lines)


def summary_line(rep: Optional[dict] = None) -> str:
    """One compact line for the panel / digest: 'gates soccer 2/10 MEASURING · mlb 0/13 MEASURING'."""
    rep = rep if rep is not None else report()
    parts = [f"{s} {rep[s]['n']}/{rep[s]['need']} {rep[s]['verdict']}"
             for s in sorted(k for k in rep if k != "all")]
    return "gates · " + " · ".join(parts)


def run(log: Any = None) -> int:
    """``python -m src.genz.maker_rt --gates`` — print the report. Read-only; places nothing."""
    print(render())
    return 0
