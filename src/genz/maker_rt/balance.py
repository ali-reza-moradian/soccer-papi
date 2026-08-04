"""8-HOURLY BALANCE RECONCILIATION — the exchanges' cash, audited against the bot's own books.

The ledger this bot keeps is a CLAIM. The money sitting at Kalshi and Polymarket is the TRUTH. Every
lifetime-pnl number the panel shows, every settled row, every locked_net is downstream of code that has
been wrong before — the $500-for-$5 settlement, the F14 price-space inversion, the phantom +$15.94
walkover. Each of those was found by hand, late, because nothing was comparing the books to the banks.
So: three times a day, read both venues, subtract what the books SAY happened, and say out loud whether
the two agree.

**THIS JOB MAY NEVER TOUCH TRADING.** That is not a style preference — it is the exact failure we just
spent a day fixing. The measurement-gate report shared the single venue worker, spent 25.3s scanning
543 MB of CSVs every 15 minutes, and blacked out the REST fill-poll backstop ~4 times an hour for 17
hours. A reporting job that runs on someone else's thread is a reporting job that can stop the fill
detector. Hence, structurally:

  * its OWN :class:`~.offloop.Worker` thread (``maker-rt-balance``), shared with nothing;
  * its OWN venue clients — a separate ``KalshiExec`` means a separate ``requests.Session``, so the
    trading client's session still sees exactly one caller besides the loop (the Polymarket CLOB SDK
    uses a module-level ``httpx.Client``, which IS thread-safe, and the data-API read is a bare
    ``requests.get``);
  * a hard 30s job deadline, after which the Worker abandons the thread and keeps going;
  * every failure logged + alerted, and NONE of them able to halt, block or slow quoting. The only
    thing a failed balance check may do is say so;
  * the loop's entire involvement is a clock comparison and a submit — microseconds, and never a
    venue read.

WHAT "TOTAL" MEANS, stated because the two venues report positions differently and pretending otherwise
would make the discrepancy number a lie:

  * Kalshi ``/portfolio/balance`` gives cash (``balance_dollars``, with the integer-CENTS ``balance``
    as the fallback) and ``/portfolio/positions`` gives ``market_exposure_dollars`` per open market —
    the venue's own figure for money at risk, i.e. a COST basis. Kalshi's positions payload carries no
    mark, so this is what "positions" means on that side.
  * Polymarket collateral is a 1e6-scaled USDC string; positions come from the data API, which DOES
    give a live mark (``currentValue``). We report the funder wallet's total AND the maker-only subset
    (tokens in our traded/expected registries), because the funder wallet holds hundreds of positions
    this bot never opened — 172 of the 173 seen on 2026-07-31 were other people's resolved markets.

So a small drift between the two sides is expected while a pair is open, and the report says so rather
than crying wolf. What is NOT expected is cash moving without the books moving, which is exactly the
class of bug this exists to catch.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import alerts
from . import config as mrt_config
from .offloop import Worker
from .state import atomic_json, hedged_lifetime, preserve_unreadable

#: The fixed UTC snapshot slots. Fixed on purpose: "every 8 hours from process start" would drift with
#: every restart (the maker restarts 10-21x on a working day), and two snapshots taken 40 minutes apart
#: cannot be compared to a window.
DEFAULT_SLOTS_UTC = (0, 8, 16)

#: Hard per-run deadline. Generous against a healthy run (four venue reads, ~2s) and short enough that a
#: hung socket cannot sit on this thread until the next slot.
DEFAULT_TIMEOUT_S = 30.0

#: |venue - books| above this on either window is a REAL disagreement, not mark noise.
DEFAULT_ALERT_USD = 5.00

#: How long a FAILED run waits before trying its slot again. A failure must not burn the whole 8-hour
#: slot on one bad HTTP response, and it must not turn a persistent outage (bad credentials, a venue
#: down for an hour) into three venue reads every 2.5 seconds either.
RETRY_AFTER_S = 900.0

#: The data API pages; ask for a big page and follow the offset. A silent truncation would understate
#: the wallet and read as a withdrawal.
_POLY_PAGE = 500
_POLY_MAX_PAGES = 20

_JOB_KEY = ("balance",)


# --------------------------------------------------------------------------- #
# money parsers — one per venue field, each with its unit written down          #
# --------------------------------------------------------------------------- #
def kalshi_cash_usd(bal: Any) -> Optional[float]:
    """Spendable Kalshi cash in DOLLARS, or None if the payload is unrecognizable.

    ``balance_dollars`` is the precise string ("4519.7251"); the bare ``balance`` is integer CENTS of
    the same number (451972) and is the fallback. Reading the cents field as dollars is the 100x class
    of bug that already cost this system a $495 phantom settlement, so both paths are explicit."""
    if not isinstance(bal, dict) or "error" in bal:
        return None
    v = bal.get("balance_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    v = bal.get("balance")
    if v in (None, ""):
        return None
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return None


def poly_cash_usd(bal: Any) -> Optional[float]:
    """Polymarket collateral (pUSD/USDC) in DOLLARS. The CLOB returns base units (6 decimals)."""
    if not isinstance(bal, dict) or "error" in bal:
        return None
    v = bal.get("balance")
    if v in (None, ""):
        return None
    try:
        return float(v) / 1_000_000.0
    except (TypeError, ValueError):
        return None


def _kalshi_money(row: dict, name: str) -> float:
    """``<name>_dollars`` (dollars) if present, else ``<name>`` (CENTS). 0.0 when neither parses."""
    v = row.get(f"{name}_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    v = row.get(name)
    if v in (None, ""):
        return 0.0
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return 0.0


def kalshi_positions_value(rows: Any) -> dict:
    """{value_usd, n, tickers} over the NON-ZERO market positions.

    ``value_usd`` is the sum of ``market_exposure`` — Kalshi's own "money at risk in this market",
    a cost basis. Zero-position rows (settled markets we still appear in) are skipped: they hold no
    money and counting them would make the number move for no reason."""
    from ...executor.kalshi_exec import fp_num
    out = {"value_usd": 0.0, "n": 0, "tickers": []}
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        n = fp_num(p, "position", "net_position", "count", "market_position")
        if n is None or abs(n) < 1e-9:
            continue
        out["value_usd"] += _kalshi_money(p, "market_exposure")
        out["n"] += 1
        tk = p.get("ticker") or p.get("market_ticker")
        if tk:
            out["tickers"].append(str(tk))
    out["value_usd"] = round(out["value_usd"], 4)
    return out


def poly_positions_value(rows: Any, maker_assets: Any = ()) -> dict:
    """{total_usd, maker_usd, n_total, n_maker} over the funder wallet's positions.

    ``currentValue`` is the data API's live mark. The maker subset is the rows whose ``asset`` is in
    our traded/expected registries — reported SEPARATELY because this funder wallet is shared and its
    unrelated holdings are not ours to reconcile."""
    ours = {str(a) for a in (maker_assets or ())}
    out = {"total_usd": 0.0, "maker_usd": 0.0, "n_total": 0, "n_maker": 0, "maker_titles": {}}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            val = float(r.get("currentValue") or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        out["total_usd"] += val
        out["n_total"] += 1
        asset = str(r.get("asset") or "")
        if asset in ours:
            out["maker_usd"] += val
            out["n_maker"] += 1
            # The data API carries the REAL market name ("Birmingham City vs. FC Barcelona: O/U 4.5").
            # Keep it: every alert in this system is supposed to name the match, never an id.
            if r.get("title"):
                out["maker_titles"][asset] = str(r["title"])
    out["total_usd"] = round(out["total_usd"], 4)
    out["maker_usd"] = round(out["maker_usd"], 4)
    return out


# --------------------------------------------------------------------------- #
# the schedule — fixed UTC slots, persisted, catch-up-once                      #
# --------------------------------------------------------------------------- #
def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def slot_at_or_before(now: datetime, slots: Any = DEFAULT_SLOTS_UTC) -> datetime:
    """The most recent scheduled slot at or before ``now`` (UTC)."""
    hours = sorted({int(h) % 24 for h in (slots or DEFAULT_SLOTS_UTC)}) or [0]
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    best = None
    for h in hours:
        cand = day + timedelta(hours=h)
        if cand <= now and (best is None or cand > best):
            best = cand
    if best is None:                                   # before the day's first slot -> yesterday's last
        best = day - timedelta(days=1) + timedelta(hours=hours[-1])
    return best


def due_slot(now: datetime, last_slot: Optional[str],
             slots: Any = DEFAULT_SLOTS_UTC) -> Optional[datetime]:
    """The slot to run NOW, or None when the current one is already done.

    A process that was down across two slots runs ONCE on the next start (for the most recent slot) —
    replaying the missed ones would produce snapshots timestamped hours after the balances they claim
    to describe, which is worse than a gap that is visible in the file."""
    s = slot_at_or_before(now, slots)
    if last_slot and str(last_slot) >= iso(s):
        return None
    return s


# --------------------------------------------------------------------------- #
# the persisted file                                                            #
# --------------------------------------------------------------------------- #
def _read_json(path: str, log: Any = None, *, what: str = "state") -> Any:
    """utf-8-sig, always. A hand-edited file on Windows very likely carries a BOM, and a BOM has
    already cost this system a day's committed stake (2026-07-28)."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (ValueError, OSError, TypeError):
        preserve_unreadable(path, log, what=what)
        return None


def load_store(path: Optional[str] = None, log: Any = None) -> dict:
    """The snapshot store: {schema, baseline, baseline_v1, baseline_v2, last_slot, snapshots[]}.

    ``baseline_v1`` is a SUPERSEDED baseline kept for history and ``baseline_v2`` is the latch saying the
    one-time re-anchor has happened. Both must survive the round trip: this function rebuilds the dict
    field by field, so a key it forgets is a key the next write DELETES — and a dropped latch is a
    baseline that re-anchors every eight hours forever. Never raises."""
    p = path or mrt_config.runtime_path("balance_snapshots")
    obj = _read_json(p, log, what="the balance snapshots")
    if not isinstance(obj, dict):
        obj = {}
    snaps = obj.get("snapshots")
    out = {"schema": 1, "baseline": obj.get("baseline") if isinstance(obj.get("baseline"), dict) else None,
           "last_slot": str(obj.get("last_slot") or ""),
           "snapshots": [s for s in (snaps or []) if isinstance(s, dict)]}
    if isinstance(obj.get("baseline_v1"), dict):
        out["baseline_v1"] = obj["baseline_v1"]
    if obj.get("baseline_v2"):
        out["baseline_v2"] = True
    return out


def load_adjustments(path: Optional[str] = None, log: Any = None) -> list:
    """Manual entries — deposits, withdrawals, hand-placed trades — that the books never saw.

    Each is ``{ts|date, venue?, usd, note}`` with ``usd`` SIGNED the way the cash moved (+1000 for a
    deposit, -250 for a withdrawal). Anything unparseable is kept with ``usd=0`` and shown in the
    report as unreadable rather than dropped: an adjustment that vanishes silently is the same failure
    as a discrepancy that vanishes silently."""
    p = path or mrt_config.runtime_path("balance_adjustments")
    obj = _read_json(p, log, what="the balance adjustments")
    rows = obj if isinstance(obj, list) else (obj or {}).get("adjustments") if isinstance(obj, dict) else None
    out: list = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        ts = str(r.get("ts") or r.get("date") or "")
        if len(ts) == 10:                              # a bare date means the start of that UTC day
            ts = f"{ts}T00:00:00Z"
        try:
            usd = float(r.get("usd"))
            bad = False
        except (TypeError, ValueError):
            usd, bad = 0.0, True
        out.append({"ts": ts, "venue": str(r.get("venue") or "").lower(), "usd": usd,
                    "note": str(r.get("note") or ""), "unreadable": bad})
    return sorted(out, key=lambda a: a["ts"])


def adjustments_in(adjustments: list, since_ts: str, until_ts: str) -> list:
    """The entries that landed in ``(since_ts, until_ts]`` — the window their cash moved in."""
    return [a for a in (adjustments or [])
            if a.get("ts") and str(since_ts) < str(a["ts"]) <= str(until_ts)]


def restatements_in(log: Any, since_ts: str, until_ts: str) -> list:
    """One-time corrections whose BOOKS moved inside this window but whose CASH did not.

    An adjustment is cash the books never saw; a restatement is the mirror image — a book correction the
    cash never saw, because the cash moved earlier and was simply never recorded. To this audit the two
    are indistinguishable from a leak unless they are declared, and declaring them is the difference
    between "our own fix" and "$6.99 went missing".

    So an entry counts here only when ``applied_ts`` (the books moving) is inside the window and
    ``effective_ts`` (the cash moving) is NOT. When both are inside, the window already contains the
    real venue movement and subtracting the correction would double-count it; when neither is, both
    endpoints already include it and there is nothing to do."""
    out = []
    for r in log or []:
        if not isinstance(r, dict):
            continue
        applied, eff = str(r.get("applied_ts") or ""), str(r.get("effective_ts") or "")
        if applied and str(since_ts) < applied <= str(until_ts) \
                and not (eff and str(since_ts) < eff <= str(until_ts)):
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# the snapshot + the comparison (PURE — the reads are the caller's)             #
# --------------------------------------------------------------------------- #
def build_snapshot(*, kalshi_bal: Any, kalshi_positions: Any, poly_bal: Any, poly_positions: Any,
                   maker_assets: Any, books: Optional[dict], now: datetime, slot: str = "",
                   manual: bool = False, errors: Optional[dict] = None,
                   truncated: bool = False) -> dict:
    """One snapshot, from already-fetched venue payloads. PURE, so the whole shape is unit-tested.

    ``ok`` is False when EITHER venue failed to read. An incomplete snapshot is still persisted (it is
    evidence), but it can never become the baseline or the anchor of a comparison: a missing venue
    looks exactly like a total withdrawal, and reporting that as a discrepancy would be worse than
    reporting nothing."""
    errors = dict(errors or {})
    k_cash = kalshi_cash_usd(kalshi_bal)
    p_cash = poly_cash_usd(poly_bal)
    kp = kalshi_positions_value(kalshi_positions)
    pp = poly_positions_value(poly_positions, maker_assets)
    k_ok = k_cash is not None and not errors.get("kalshi")
    p_ok = p_cash is not None and not errors.get("poly")
    kalshi = {"cash": round(k_cash, 4) if k_cash is not None else None,
              "positions": kp["value_usd"], "n_positions": kp["n"], "tickers": kp["tickers"],
              "total": round((k_cash or 0.0) + kp["value_usd"], 4) if k_ok else None,
              "ok": bool(k_ok), "error": str(errors.get("kalshi") or "")}
    poly = {"cash": round(p_cash, 4) if p_cash is not None else None,
            "positions": pp["total_usd"], "positions_maker": pp["maker_usd"],
            "n_positions": pp["n_total"], "n_maker": pp["n_maker"], "truncated": bool(truncated),
            "maker_titles": dict(pp.get("maker_titles") or {}),
            "total": round((p_cash or 0.0) + pp["total_usd"], 4) if p_ok else None,
            "ok": bool(p_ok), "error": str(errors.get("poly") or "")}
    ok = bool(k_ok and p_ok)
    return {"schema": 1, "ts": iso(now), "slot": str(slot or ""), "manual": bool(manual),
            "kalshi": kalshi, "poly": poly,
            "total": round((kalshi["total"] or 0.0) + (poly["total"] or 0.0), 4) if ok else None,
            "books": dict(books or {}), "ok": ok}


def books_from(state: Any, caps: Any = None, open_legs: int = 0) -> dict:
    """The BOT'S CLAIM at this instant, read on the loop thread and passed into the job by value.

    ``settled_pnl_lifetime`` is the only number comparable to venue cash: it is venue-truth realized
    pnl, it accumulates for the life of the bot, and it moves exactly when money actually settles.
    ``pnl_today`` is the fill-time LOCKED ESTIMATE of pairs that mostly have NOT settled — that money
    is still sitting in POSITIONS, which is why it is carried as context and not as the comparison."""
    g = lambda o, n, d=0.0: float(getattr(o, n, d) or 0.0) if o is not None else float(d)  # noqa: E731
    life = g(state, "settled_pnl_lifetime")
    untracked = g(state, "settled_pnl_untracked_lifetime")
    exits = g(state, "settled_pnl_exits_lifetime")
    return {"settled_pnl_lifetime": round(life, 4),
            "settled_pnl_hedged_lifetime": hedged_lifetime(life, untracked, exits),
            "settled_pnl_untracked_lifetime": round(untracked, 4),
            # THE EXIT TOLL, carried explicitly. It is already inside ``settled_pnl_lifetime`` (that is the
            # whole point — lifetime has to track venue cash), and it is named here so the report can say
            # where a negative lifetime movement came from instead of leaving it to be guessed at.
            "settled_pnl_exits_lifetime": round(exits, 4),
            # The DATED one-time corrections, so a window can tell "we fixed our own books" apart from
            # "cash left and nobody noticed" — see restatements_in().
            "restatement_log": list(getattr(state, "restatement_log", None) or []),
            "settled_trades": int(getattr(state, "settled_trades", 0) or 0) if state is not None else 0,
            "pnl_today": round(g(caps, "pnl_today"), 4),
            "fills_today": int(getattr(caps, "fills_today", 0) or 0) if caps is not None else 0,
            # LEGS, not pairs: the expected-position registry holds one entry per leg (a
            # hedged pair is two). The report names the PAIRS separately, from their
            # distinct game+market labels.
            "open_legs": int(open_legs or 0)}


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(ts: Any) -> str:
    """'2026-07-31T16:00:00Z' -> 'Jul 31'. Falls back to the raw date so a odd string still says
    something rather than throwing inside a report."""
    s = str(ts or "")
    try:
        return f"{_MONTHS[int(s[5:7]) - 1]} {int(s[8:10])}"
    except (ValueError, IndexError):
        return s[:10] or "?"


def _elapsed_s(a: Any, b: Any) -> Optional[float]:
    """Seconds between two snapshot timestamps, or None if either will not parse."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(str(b), fmt) - datetime.strptime(str(a), fmt)).total_seconds()
    except (ValueError, TypeError):
        return None


def _window_label(prev_ts: Any, cur_ts: Any) -> str:
    """'Last 8h' when the window really is about eight hours, else what it actually was.

    A restart, a missed slot or a manual ``--balance`` produces a window of some other length, and
    calling a fifteen-minute gap "Last 8h" would misstate the very thing the report exists to state."""
    secs = _elapsed_s(prev_ts, cur_ts)
    if secs is None:
        return "Since the last check"
    if 6 * 3600 <= secs <= 10 * 3600:
        return "Last 8h"
    return f"Since the last check ({alerts.dur(secs)})"


def is_flat(snap: Optional[dict]) -> bool:
    """True when this snapshot was taken with NO maker position outstanding at either venue.

    A snapshot with an open pair values the two legs on DIFFERENT bases — Kalshi positions are a COST
    basis (``market_exposure``, the venue publishes no mark) while Polymarket's are a live MARK
    (``currentValue``) — so its ``total`` is part cost and part mark. That is not a number a later
    total can be honestly subtracted from. The 2026-07-31 baseline was snapped with the Birmingham
    pair open and carried $1.86 of exactly this error into every lifetime comparison after it.
    ``n_maker`` (not ``n_positions``) is the Poly test: that funder wallet holds hundreds of positions
    this bot never opened.

    A RESOLVED-BUT-UNREDEEMED leg is deliberately still "flat". On 2026-08-04T14:45Z the wallet held
    $459.07 of Jeju/Bayern Over 1.5 that had already won and settled in the books; the data API marks a
    resolved position at $1.00 face, so it is worth exactly what it will convert to and there is no
    basis mismatch left to distort. What this predicate is for is a position whose VALUATION will move,
    not any position at all."""
    if not (snap and snap.get("ok")):
        return False
    k, p = snap.get("kalshi") or {}, snap.get("poly") or {}
    return int(k.get("n_positions") or 0) == 0 and int(p.get("n_maker") or 0) == 0


def _split(prev: dict, cur: dict) -> dict:
    """Per-venue Δcash and Δpositions across a window — so a breach line can say WHERE the gap lives."""
    out: dict = {}
    for venue in ("kalshi", "poly"):
        a, b = prev.get(venue) or {}, cur.get(venue) or {}
        def d(field: str) -> Optional[float]:
            x, y = a.get(field), b.get(field)
            if x is None or y is None:
                return None
            try:
                return round(float(y) - float(x), 4)
            except (TypeError, ValueError):
                return None
        out[venue] = {"cash": d("cash"), "positions": d("positions")}
    return out


def _window(prev: Optional[dict], cur: dict, adjustments: list, label: str) -> Optional[dict]:
    """One comparison window: venue delta, adjustments, book delta, and what is left over.

    ``reliable`` is False when EITHER endpoint held an open maker pair. Such a window mixes a cost
    basis against a mark and its discrepancy is not evidence of anything — see :func:`is_flat`. The
    window is still computed and still reported (it is a measurement, and hiding it would be its own
    dishonesty); it simply may not raise the alarm."""
    if not (prev and prev.get("ok") and cur.get("ok")):
        return None
    venue = float(cur["total"]) - float(prev["total"])
    entries = adjustments_in(adjustments, prev.get("ts", ""), cur.get("ts", ""))
    adj = sum(float(a.get("usd") or 0.0) for a in entries)
    b0, b1 = prev.get("books") or {}, cur.get("books") or {}
    book = float(b1.get("settled_pnl_lifetime") or 0.0) - float(b0.get("settled_pnl_lifetime") or 0.0)
    hedged = float(b1.get("settled_pnl_hedged_lifetime") or 0.0) - float(b0.get("settled_pnl_hedged_lifetime") or 0.0)
    untracked = float(b1.get("settled_pnl_untracked_lifetime") or 0.0) - float(b0.get("settled_pnl_untracked_lifetime") or 0.0)
    exits = float(b1.get("settled_pnl_exits_lifetime") or 0.0) - float(b0.get("settled_pnl_exits_lifetime") or 0.0)
    open_at = [w for w, s in (("start", prev), ("end", cur)) if not is_flat(s)]
    # BOOK-SIDE corrections: money the venues moved BEFORE this window that the books only recorded
    # inside it. Removed from the book delta for the same reason a deposit is removed from the venue
    # delta — otherwise the audit reports our own correction as a discrepancy.
    rest = restatements_in(b1.get("restatement_log"), prev.get("ts", ""), cur.get("ts", ""))
    restated = sum(float(r.get("usd") or 0.0) for r in rest)
    return {"label": label, "since": prev.get("ts", ""), "venue_delta": round(venue, 4),
            "adjust_usd": round(adj, 4), "adjustments": entries,
            "book_delta": round(book, 4), "book_hedged": round(hedged, 4),
            "book_untracked": round(untracked, 4), "book_exits": round(exits, 4),
            "restated_usd": round(restated, 4), "restatements": rest,
            "discrepancy": round(venue - adj - (book - restated), 4),
            "reliable": not open_at, "open_at": open_at, "split": _split(prev, cur)}


def build_report(cur: dict, *, prev: Optional[dict] = None, baseline: Optional[dict] = None,
                 adjustments: Optional[list] = None, alert_usd: float = DEFAULT_ALERT_USD,
                 open_positions: Optional[list] = None, rebaselined: str = "") -> dict:
    """The whole comparison: this snapshot vs the previous one, vs the baseline, vs the books."""
    adjustments = list(adjustments or [])
    windows = {}
    w8 = _window(prev, cur, adjustments,
                 _window_label((prev or {}).get("ts"), cur.get("ts")))
    if w8:
        windows["window"] = w8
    # The lifetime window is SKIPPED while the baseline still IS the previous snapshot: on the second
    # measurement of the bot's life the two are arithmetically identical, and two identical lines read
    # as a bug rather than as a confirmation.
    same = bool(prev and baseline and prev.get("ts") == baseline.get("ts"))
    if baseline is not None and not same and baseline.get("ts") != cur.get("ts"):
        wl = _window(baseline, cur, adjustments, f"Since baseline ({_day(baseline.get('ts'))})")
        if wl:
            windows["lifetime"] = wl
    # A window may only RAISE THE ALARM if both its endpoints were flat. An open pair puts a Kalshi cost
    # basis on one side of the subtraction and a Polymarket mark on the other, so its "discrepancy" is
    # part measurement and part valuation convention — and on 2026-08-01/02 that produced two
    # +/-$19-25 red MISMATCH alerts with nothing actually wrong. Noise the operator cannot distinguish
    # from a real drift is worse than no alarm: it teaches them to ignore the one that matters. The
    # unreliable window is still shown, still labelled, and judgement is deferred to the next flat one.
    breaches = [w for w in windows.values()
                if w.get("reliable") and abs(float(w["discrepancy"])) > float(alert_usd) + 1e-9]
    deferred = [w for w in windows.values()
                if not w.get("reliable") and abs(float(w["discrepancy"])) > float(alert_usd) + 1e-9]
    return {"snapshot": cur, "windows": windows, "alert_usd": float(alert_usd),
            "breaches": breaches, "alert": bool(breaches), "deferred": deferred,
            "is_baseline": bool(cur.get("ok") and not windows),
            "adjustments_known": bool(adjustments),
            "rebaselined": str(rebaselined or ""),
            "open_positions": list(open_positions or [])}


# --------------------------------------------------------------------------- #
# rendering — the same words on Telegram and on the console                     #
# --------------------------------------------------------------------------- #
def _usd(v: Any) -> str:
    """'$7,313.15' — grouped, because these are ACCOUNT-sized numbers. ``alerts.money`` is deliberately
    left alone: it formats per-bet amounts, where a comma would only ever appear in a bug."""
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "$—"


def _signed(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "$—"
    return f"+{_usd(f)}" if f >= 0 else f"-{_usd(abs(f))}"


def _venue_line(name: str, v: dict, *, maker: bool = False) -> str:
    if not v.get("ok"):
        return f"   {name} — COULD NOT READ ({v.get('error') or 'unknown error'})"
    tail = f"; of which ours {_usd(v.get('positions_maker'))}" if maker else ""
    return (f"   {name} {_usd(v.get('total'))} (cash {_usd(v.get('cash'))} + "
            f"positions {_usd(v.get('positions'))}{tail})")


def _window_line(w: dict, alert_usd: float) -> str:
    """One window. The verdict word is derived from the SAME threshold that decides the alert, so the
    line and the alarm can never tell different stories about the same number."""
    diff = abs(float(w["discrepancy"]))
    if not w.get("reliable"):
        # NOT a verdict — a refusal to give one. Say why, and say what will answer it.
        where = " and ".join(str(x) for x in (w.get("open_at") or []))
        mark = (f"⚪ unreliable while pairs are open (marks move) — pairs open at the {where} of this "
                f"window; judgement deferred to the next flat-to-flat one")
    elif diff > float(alert_usd) + 1e-9:
        mark = "🔴 MISMATCH"
    elif diff <= max(0.50, float(alert_usd) * 0.1):
        mark = "✅ match"
    else:
        mark = "🟡 within tolerance"
    adj = (f" · manual adjustments {_signed(w['adjust_usd'])}" if w.get("adjustments") else "")
    res = (f" · of which {_signed(w['restated_usd'])} is a one-time correction to older books, not "
           f"cash that moved now" if w.get("restatements") else "")
    return (f"   {w['label']}: {_signed(w['venue_delta'])}{adj} · bot's books say "
            f"{_signed(w['book_delta'])}{res} · {mark} (diff {_usd(diff)})")


def _split_line(w: dict) -> str:
    """WHERE the gap lives: each venue's Δcash and Δpositions over the window.

    A bare "$12 apart" sends a human to two dashboards to find out which venue and whether it was cash
    or a mark. The numbers are already in the snapshots; printing them is free and answers the first
    question that gets asked."""
    sp = w.get("split") or {}
    def one(name: str, key: str) -> str:
        d = sp.get(key) or {}
        c, p = d.get("cash"), d.get("positions")
        return (f"{name} cash {_signed(c) if c is not None else '$—'}/positions "
                f"{_signed(p) if p is not None else '$—'}")
    return f"      where: {one('Kalshi', 'kalshi')} · {one('Polymarket', 'poly')}"


def _books_line(books: dict) -> str:
    """What the books CLAIM, with the two numbers kept apart on purpose.

    ``settled`` is venue-truth realized money and is the only thing comparable to cash movement.
    ``pnl_today`` is the fill-time locked ESTIMATE of pairs that have mostly not settled — that money
    is still sitting in POSITIONS, which is exactly why the totals above include them."""
    life = float(books.get("settled_pnl_lifetime") or 0.0)
    hedged = float(books.get("settled_pnl_hedged_lifetime") or 0.0)
    untracked = float(books.get("settled_pnl_untracked_lifetime") or 0.0)
    exits = float(books.get("settled_pnl_exits_lifetime") or 0.0)
    return (f"   My books: settled lifetime {_signed(life)} ({_signed(hedged)} from hedged pairs, "
            f"{_signed(untracked)} luck, {_signed(exits)} exit costs) · today's locked estimate "
            f"{_signed(books.get('pnl_today'))} (not settled yet — it is still in the positions above)")


def render_report(rep: dict) -> str:
    """The plain-language report — Telegram and ``--balance`` print the SAME words on purpose."""
    s = rep["snapshot"]
    lines = ["💰 8h BALANCE CHECK" + (" (on demand)" if s.get("manual") else "")]
    lines.append(_venue_line("Kalshi", s.get("kalshi") or {}))
    lines.append(_venue_line("Polymarket", s.get("poly") or {}, maker=True))
    if s.get("ok"):
        lines.append(f"   TOTAL {_usd(s.get('total'))}")
    else:
        lines.append("   TOTAL — incomplete: a venue did not answer, so this snapshot is NOT compared "
                     "to anything (a venue I cannot read looks exactly like a total withdrawal).")
    if rep.get("is_baseline"):
        lines.append("   📌 This is the BASELINE — the first measurement. Everything from here is "
                     "measured against it; it is never overwritten.")
    for key in ("window", "lifetime"):
        w = (rep.get("windows") or {}).get(key)
        if w:
            lines.append(_window_line(w, rep.get("alert_usd", DEFAULT_ALERT_USD)))
            lines.append(_split_line(w))
    if rep.get("rebaselined"):
        lines.append(f"   📌 NEW BASELINE (baseline_v2). The old baseline ({_day(rep['rebaselined'])}) was "
                     f"snapped with a pair still open, so its total mixed a Kalshi COST basis with a "
                     f"Polymarket MARK and every lifetime comparison inherited that error. It is kept in "
                     f"the store for history; lifetime is measured from this flat snapshot onward.")
    if s.get("books"):
        lines.append(_books_line(s["books"]))
    # Every adjustment is NAMED. One absorbed silently is indistinguishable from a bug we hid.
    seen = []
    for w in (rep.get("windows") or {}).values():
        for a in w.get("adjustments") or []:
            if a in seen:
                continue
            seen.append(a)
            note = a.get("note") or "manual entry"
            where = f" on {alerts.venue_full(a['venue'])}" if a.get("venue") else ""
            flag = " ⚠️ UNREADABLE AMOUNT" if a.get("unreadable") else ""
            lines.append(f"   ✍️ {_signed(a.get('usd'))}{where} — {note} "
                         f"({str(a.get('ts'))[:16]}){flag}")
    if not rep.get("adjustments_known"):
        lines.append("   ✍️ no adjustments recorded")
    # Every restatement is NAMED too. A book correction absorbed silently is the same failure as a
    # discrepancy absorbed silently — the operator has to be able to see that we moved our own number.
    seen_r: list = []
    for w in (rep.get("windows") or {}).values():
        for r in w.get("restatements") or []:
            if r in seen_r:
                continue
            seen_r.append(r)
            lines.append(f"   📘 {_signed(r.get('usd'))} book correction applied "
                         f"{str(r.get('applied_ts'))[:16]} for cash that moved "
                         f"{str(r.get('effective_ts'))[:10]} — {r.get('note') or r.get('key')}")
    op = rep.get("open_positions") or []
    if op:
        shown = ", ".join(str(x) for x in op[:3]) + (f" +{len(op) - 3} more" if len(op) > 3 else "")
        lines.append(f"   Open pairs at snapshot: {len(op)} ({shown}) — marks move, so small diffs "
                     f"are normal")
    elif s.get("ok"):
        lines.append("   Open pairs at snapshot: 0 — nothing outstanding, so the numbers should agree "
                     "closely")
    for w in rep.get("breaches") or []:
        lines.append(
            f"🔴 BOOKS DISAGREE WITH THE EXCHANGES — over '{w['label'].lower()}' the exchanges moved "
            f"{_signed(w['venue_delta'])} but my books say "
            f"{_signed(w['book_delta'])}, a gap of "
            f"{_usd(abs(float(w['discrepancy'])))}. That is more than the "
            f"{_usd(rep.get('alert_usd'))} I treat as normal drift. Both ends of this window were flat, "
            f"so it is not mark noise. Investigate.")
        lines.append(_split_line(w))
    for w in rep.get("deferred") or []:
        lines.append(
            f"⚪ over '{w['label'].lower()}' the exchanges and my books are "
            f"{_usd(abs(float(w['discrepancy'])))} apart, but a pair was open at the "
            f"{' and '.join(str(x) for x in (w.get('open_at') or []))} of it, so the two sides are not "
            f"measured on the same basis and I will NOT call that a mismatch. The next flat-to-flat "
            f"check answers it.")
    if s.get("poly", {}).get("truncated"):
        lines.append("   ⚠️ the Polymarket position list was truncated — the total is a FLOOR, not a "
                     "complete figure.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the runner                                                                    #
# --------------------------------------------------------------------------- #
def maker_assets(log: Any = None) -> tuple:
    """(poly token ids, kalshi tickers, open-pair labels, {instrument: label}) from the maker's own
    persisted registries.

    Read from the FILES rather than from the live executor objects on purpose: this runs on another
    thread and in a separate ``--balance`` process, and reaching into running trading state from either
    would be a shared mutable it does not need. The registries are the same two files reconciliation
    already treats as the maker-scoped watch-set."""
    toks: set = set()
    tickers: set = set()
    pairs: list = []
    labels: dict = {}
    traded = _read_json(mrt_config.runtime_path("traded_tokens"), log, what="the traded-token registry")
    if isinstance(traded, dict):
        toks.update(str(t) for t in (traded.get("tokens") or []) if t)
        tickers.update(str(t) for t in (traded.get("tickers") or []) if t)
    exp = _read_json(mrt_config.runtime_path("expected_positions"), log,
                     what="the expected-position registry")
    if isinstance(exp, dict):
        for rec in exp.values():
            if not isinstance(rec, dict):
                continue
            inst = str(rec.get("instrument") or "")
            if not inst:
                continue
            (toks if str(rec.get("venue")) == "polymarket" else tickers).add(inst)
            label = " ".join(x for x in (str(rec.get("game") or ""), str(rec.get("market_key") or "")) if x)
            if not label:
                continue
            labels[inst] = label
            if label not in pairs:
                pairs.append(label)
    return tuple(sorted(toks)), tuple(sorted(tickers)), pairs, labels


class BalanceReconciler:
    """The 8-hourly venue-truth audit. Owns a thread; owns its clients; owns nothing else."""

    def __init__(self, cfg: Any, *, telegram: Any = None, log: Any = None,
                 kalshi: Any = None, poly: Any = None, path: Optional[str] = None,
                 positions_readers: Optional[tuple] = None) -> None:
        """``kalshi``/``poly``/``positions_readers`` are the injection seam (fakes in tests), exactly as
        LiveGate injects its clients; production passes none of them and the reconciler builds its own."""
        blk = getattr(cfg, "balance", None)
        self.cfg = cfg
        self.enabled = bool(getattr(blk, "enabled", True))
        self.alert_usd = float(getattr(blk, "alert_usd", DEFAULT_ALERT_USD))
        self.slots = tuple(getattr(blk, "slots_utc", DEFAULT_SLOTS_UTC))
        self.timeout_s = float(getattr(blk, "timeout_s", DEFAULT_TIMEOUT_S))
        self.telegram = telegram
        self.log = log
        self.path = path
        self._kalshi = kalshi                 # injected in tests; built lazily in production
        self._poly = poly
        kfn, pfn = positions_readers or (None, None)
        self._read_kalshi_positions = kfn or self._kalshi_positions
        self._read_poly_positions = pfn or self._poly_positions
        # ITS OWN THREAD. Not the venue worker (that one owes a 10s fill poll), not the report worker,
        # not the loop. The deadline is what makes a hung venue read survivable.
        self.worker = Worker(name="maker-rt-balance", log=log, job_timeout_s=self.timeout_s)
        self.last_ok_ts = 0.0                 # monotonic-ish: unix seconds of the last COMPLETED run
        self.last_run_iso = ""
        self.last_error = ""
        self.runs = 0
        self.failures = 0
        self.retry_s = RETRY_AFTER_S
        self._retry_not_before = 0.0          # set on the LOOP at submit time (see maybe_run)
        self._persist_lock = threading.Lock()

    # -- clients (its own, never the trading path's) -------------------------
    def _clients(self) -> tuple:
        from .clients import live_enabled
        if not live_enabled(self.cfg):
            raise RuntimeError("maker_rt.live.enabled is false — no venue clients exist to read "
                               "balances with (a shadow process must never construct one).")
        if self._kalshi is None:
            from ...executor import config as exec_config
            from ...executor.kalshi_exec import KalshiExec
            # A SEPARATE instance = a separate requests.Session, so the trading client's session still
            # sees exactly one caller besides the loop.
            self._kalshi = KalshiExec(api_base=exec_config.load_exec_config().kalshi_api_base,
                                      log=self.log, timeout=10.0)
        if self._poly is None:
            from ...executor.poly_exec import PolyExec
            self._poly = PolyExec(log=self.log)
        return self._kalshi, self._poly

    # -- the venue reads (OFF-LOOP, always) ----------------------------------
    def read_venues(self, books: dict, now: datetime, slot: str, manual: bool) -> dict:
        """Four reads + a snapshot. Every failure is CAUGHT and recorded on the snapshot: this function
        must not be able to raise a venue's bad day into anything that matters."""
        errors: dict = {}
        kbal = kpos = pbal = ppos = None
        truncated = False
        try:
            kalshi, poly = self._clients()
        except Exception as exc:  # noqa: BLE001
            errors["kalshi"] = errors["poly"] = str(exc)
            kalshi = poly = None
        if kalshi is not None:
            try:
                kbal = kalshi.get_balance()
            except Exception as exc:  # noqa: BLE001
                errors["kalshi"] = f"balance read failed: {exc}"
            try:
                kpos = self._read_kalshi_positions(kalshi)
            except Exception as exc:  # noqa: BLE001
                errors["kalshi"] = errors.get("kalshi") or f"positions read failed: {exc}"
        if poly is not None:
            try:
                pbal = poly.get_balance()
            except Exception as exc:  # noqa: BLE001
                errors["poly"] = f"balance read failed: {exc}"
            try:
                ppos, truncated = self._read_poly_positions(poly)
            except Exception as exc:  # noqa: BLE001
                errors["poly"] = errors.get("poly") or f"positions read failed: {exc}"
        toks = maker_assets(self.log)[0]
        return build_snapshot(kalshi_bal=kbal, kalshi_positions=kpos, poly_bal=pbal,
                              poly_positions=ppos, maker_assets=toks, books=books, now=now,
                              slot=slot, manual=manual, errors=errors, truncated=truncated)

    @staticmethod
    def _kalshi_positions(kalshi: Any) -> list:
        """Every market position, cursor-paged. An unpaged read would silently drop positions once the
        account holds more than a page of them — i.e. exactly when the number starts to matter."""
        rows: list = []
        cursor = None
        for _ in range(10):
            resp = kalshi.get_positions(limit=200, cursor=cursor)
            page = resp.get("market_positions") if isinstance(resp, dict) else (resp or [])
            rows.extend(p for p in (page or []) if isinstance(p, dict))
            cursor = resp.get("cursor") if isinstance(resp, dict) else None
            if not cursor:
                break
        return rows

    @staticmethod
    def _poly_positions(poly: Any) -> tuple:
        """(rows, truncated) from the Polymarket data API, offset-paged.

        Paged HERE rather than through ``PolyExec.list_positions`` because that helper takes the API's
        default page (100 rows on 2026-07-31, against a wallet holding 173) and this number must not be
        a silent floor. Nothing on the trading path changes."""
        import requests
        from ...executor.poly_exec import resolve_wallet
        funder = resolve_wallet().get("funder")
        if not funder:
            return [], False
        rows: list = []
        offset = 0
        for _ in range(_POLY_MAX_PAGES):
            r = requests.get("https://data-api.polymarket.com/positions",
                             params={"user": funder, "sizeThreshold": 0.1,
                                     "limit": _POLY_PAGE, "offset": offset}, timeout=12)
            r.raise_for_status()
            data = r.json()
            page = data if isinstance(data, list) else (data.get("positions") or data.get("data") or [])
            rows.extend(x for x in (page or []) if isinstance(x, dict))
            if len(page or []) < _POLY_PAGE:
                return rows, False
            offset += len(page)
        return rows, True

    # -- persistence ---------------------------------------------------------
    def _persist(self, snap: dict, *, consume_slot: bool) -> tuple:
        """Append the snapshot; set (or ONCE re-anchor) the baseline. Returns ``(store, rebased_from)``.

        A snapshot that could not read a venue is still appended (it is the record that we tried and
        failed) but may not become the baseline — a baseline built on a half-read account would poison
        every lifetime comparison after it.

        ONLY A FLAT SNAPSHOT MAY BE THE BASELINE. A snapshot holding an open maker pair values Kalshi at
        COST and Polymarket at MARK, so its total is not a figure a later total can be subtracted from —
        the 2026-07-31 baseline was taken with the Birmingham pair open and injected $1.86 of pure
        valuation artefact into every lifetime comparison that followed. When the stored baseline is one
        of those, the FIRST flat, ok snapshot after this ships replaces it (``baseline_v2``) and the old
        one is kept under ``baseline_v1`` — the audit trail is append-only, so a bad measurement is
        superseded, never deleted. This happens AT MOST ONCE: ``baseline_v2`` existing is the latch.

        Read-modify-write under a LOCK: a job abandoned at its deadline is not killed, so it can still
        be running when its replacement finishes, and two interleaved appends would drop one of them.
        (The lock is in-process only; the ``--balance`` CLI runs in another process, but it is a manual
        action taken minutes at a time, not a concurrent writer.)"""
        with self._persist_lock:
            store = load_store(self.path, self.log)
            rebased_from = ""
            base = store.get("baseline")
            if snap.get("ok") and is_flat(snap):
                if base is None:
                    snap = {**snap, "baseline": True}  # flagged BEFORE it is stored in both places, so
                    store["baseline"] = snap           # the two copies cannot disagree about what it is
                elif not store.get("baseline_v2") and not is_flat(base):
                    rebased_from = str(base.get("ts") or "")
                    store["baseline_v1"] = base        # kept for history — never deleted
                    snap = {**snap, "baseline": True, "baseline_v2": True}
                    store["baseline"] = snap
                    store["baseline_v2"] = True
                    if self.log:
                        self.log.warning(
                            "[MAKER_RT][BALANCE] RE-ANCHORED the baseline: the old one (%s) was taken "
                            "with a maker pair open, so it mixed a Kalshi cost basis with a Polymarket "
                            "mark. Lifetime is now measured from %s, which is flat. The old baseline is "
                            "kept as baseline_v1.", rebased_from, snap.get("ts"))
            store["snapshots"].append(snap)
            if consume_slot and snap.get("slot"):
                store["last_slot"] = snap["slot"]
            atomic_json(self.path or mrt_config.runtime_path("balance_snapshots"), store)
            return store, rebased_from

    # -- one full run --------------------------------------------------------
    def run_once(self, books: dict, now: datetime, *, slot: str = "", manual: bool = False) -> dict:
        """Read, persist, compare, render. Returns {snapshot, report, text}. NEVER raises."""
        snap = self.read_venues(books, now, slot, manual)
        prev = None
        store = load_store(self.path, self.log)
        for s in reversed(store.get("snapshots") or []):
            if s.get("ok"):
                prev = s
                break
        # A FAILED run does not consume its slot. Marking the slot done on a venue we could not read
        # would trade one bad HTTP response for an eight-hour hole in the audit trail.
        store, rebased_from = self._persist(snap, consume_slot=bool(snap.get("ok")) and not manual)
        # The baseline is read AFTER the persist so a run that re-anchors compares against the baseline
        # it just set, not the one it just superseded.
        baseline = store.get("baseline")
        rep = build_report(snap, prev=prev, baseline=baseline if baseline else None,
                           adjustments=load_adjustments(log=self.log), alert_usd=self.alert_usd,
                           open_positions=self._open_pair_names(snap), rebaselined=rebased_from)
        return {"snapshot": snap, "report": rep, "text": render_report(rep)}

    def _open_pair_names(self, snap: dict) -> list:
        """The open pairs, named the way a human names them.

        The registries know a pair as ``26JUL31BCBAR total_goals|4.5``; the Polymarket data API knows the
        SAME position as "Birmingham City vs. FC Barcelona: O/U 4.5". Every alert in this system is
        supposed to say the second one — so where a maker position gives us the real title, use it, and
        fall back to the registry label for a leg the wallet cannot name (a Kalshi-only holding)."""
        _toks, _tk, pairs, labels = maker_assets(self.log)
        titles = (snap.get("poly") or {}).get("maker_titles") or {}
        best: dict = {}
        for inst, label in labels.items():
            if titles.get(inst):
                best[label] = titles[inst]
        return [best.get(p, p) for p in pairs]

    # -- loop-side surface: a clock check and a submit ------------------------
    def maybe_run(self, now: datetime, books: dict) -> bool:
        """LOOP THREAD. Submit a run when a slot is due. Costs a comparison and a queue put.

        The cooldown is stamped HERE, on the submitting thread, rather than when the result lands: a
        failed run leaves its slot unconsumed (so it is retried), and without a cooldown owned by the
        loop the next heartbeat 2.5s later would submit it again, and again."""
        if not self.enabled:
            return False
        ts = now.timestamp()
        if ts < self._retry_not_before:
            return False
        slot = due_slot(now, self._last_slot(), self.slots)
        if slot is None:
            return False
        slot_iso = iso(slot)
        b = dict(books or {})
        submitted = self.worker.submit(_JOB_KEY, lambda: self._job(b, slot_iso, False))
        if submitted:
            self._retry_not_before = ts + self.retry_s
        return submitted

    def run_manual(self, now: datetime, books: dict) -> bool:
        """LOOP THREAD. Force a run that does NOT consume the schedule (for an operator asking now)."""
        b = dict(books or {})
        return self.worker.submit(_JOB_KEY, lambda: self._job(b, "", True))

    def _last_slot(self) -> str:
        """Read the persisted last slot. Done on the loop, but it is one small JSON read every 2.5s at
        worst — so it is cached until the file's mtime changes."""
        p = self.path or mrt_config.runtime_path("balance_snapshots")
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = -1.0
        if getattr(self, "_last_slot_mtime", None) == mtime:
            return getattr(self, "_last_slot_val", "")
        val = load_store(self.path, self.log).get("last_slot") or ""
        self._last_slot_mtime = mtime            # type: ignore[attr-defined]
        self._last_slot_val = val                # type: ignore[attr-defined]
        return val

    def _job(self, books: dict, slot_iso: str, manual: bool) -> dict:
        """OFF-LOOP. The whole run, including the alert. Decides nothing about trading."""
        now = datetime.now(timezone.utc)
        out = self.run_once(books, now, slot=slot_iso, manual=manual)
        text = out["text"]
        if self.telegram is not None:
            try:
                self.telegram(text)
            except Exception as exc:  # noqa: BLE001 — an unreachable chat is not a failed audit
                if self.log:
                    self.log.warning("[MAKER_RT][BALANCE] telegram send failed: %s", exc)
        return out

    def drain(self, now_ts: float) -> Optional[dict]:
        """LOOP THREAD. Collect finished runs so the mailbox cannot grow, and log the outcome.

        A run that FAILED is loud (log + Telegram) and changes nothing else: this subsystem has no
        authority over quoting, hedging, caps or halts, by construction."""
        out = None
        for _key, res, exc in self.worker.drain():
            self.runs += 1
            if exc is not None or not isinstance(res, dict):
                self.failures += 1
                self.last_error = str(exc or "no result")
                if self.log:
                    self.log.error("[MAKER_RT][BALANCE] the balance check FAILED (%s) — trading is "
                                   "unaffected; the next slot retries.", self.last_error)
                self._alert_failure(self.last_error)
                continue
            snap = res.get("snapshot") or {}
            self.last_run_iso = str(snap.get("ts") or "")
            if snap.get("ok"):
                self.last_ok_ts = float(now_ts or 0.0)
                self.last_error = ""
            else:
                # A venue that did not answer is a FAILED audit, not a quiet one. The report itself has
                # already gone to Telegram saying which venue and why (it is the same words), so this is
                # the log side of the same statement — never a second alert for one event.
                self.failures += 1
                self.last_error = (snap.get("kalshi", {}).get("error")
                                   or snap.get("poly", {}).get("error") or "incomplete read")
                if self.log:
                    self.log.error("[MAKER_RT][BALANCE] the balance check could not read a venue (%s) "
                                   "— nothing was compared; trading is unaffected.", self.last_error)
            if self.log:
                for line in str(res.get("text") or "").splitlines():
                    self.log.info("[MAKER_RT][BALANCE] %s", line)
            out = res
        return out

    def _alert_failure(self, err: str) -> None:
        if self.telegram is None:
            return
        try:
            self.telegram(alerts.format_event("problem", detail=(
                "My 8-hourly check of the exchange balances did not complete, so I could not compare "
                "the exchanges' cash against my own books this time. Trading is unaffected and the "
                "next check runs on schedule.")))
        except Exception:  # noqa: BLE001
            pass
        if self.log:
            self.log.warning("[MAKER_RT][BALANCE] failure detail (log only): %s", err)

    # -- the safety row ------------------------------------------------------
    def safety(self, now_ts: float) -> dict:
        """{last, age_s, cadence_s, overdue} for the panel's safety-systems row."""
        cadence = 8 * 3600.0
        age = (now_ts - self.last_ok_ts) if (self.last_ok_ts and now_ts) else None
        return {"last": self.last_run_iso, "age_s": round(age, 1) if age is not None else None,
                "cadence_s": cadence,
                # Never run YET is not overdue — a fresh process has not missed anything.
                "overdue": bool(age is not None and age > cadence * 1.5),
                "enabled": self.enabled, "runs": self.runs, "failures": self.failures,
                "error": self.last_error}

    def close(self) -> None:
        try:
            self.worker.close()
        except Exception:  # noqa: BLE001 — shutdown must never raise out of here
            pass


# --------------------------------------------------------------------------- #
# `python -m src.genz.maker_rt --balance`                                        #
# --------------------------------------------------------------------------- #
def _print(text: str) -> None:
    """Print the report on a console that may not be able to encode it.

    Windows hands a bare ``python -m ...`` a cp1252 stdout, and every line of this report starts with an
    emoji. ``print`` then raises UnicodeEncodeError *after* the venue reads, the persist and the
    re-anchor have already happened — so the audit ran, changed state, and reported a traceback instead
    of its answer. Re-encode to whatever the console does support and keep the words."""
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def run_cli(cfg: Any, log: Any = None) -> int:
    """Run the audit ON DEMAND and print the same report the 8-hourly one sends.

    Persists the snapshot (it is a real measurement, and if no baseline exists yet this creates it) but
    does NOT consume a scheduled slot — an operator looking now must not move the schedule the running
    process is keeping."""
    from .state import load_tuning
    tun = load_tuning(log)
    life = float(tun.get("settled_pnl_lifetime", 0.0) or 0.0)
    untracked = float(tun.get("settled_pnl_untracked_lifetime", 0.0) or 0.0)
    exits = float(tun.get("settled_pnl_exits_lifetime", 0.0) or 0.0)
    caps = _read_json(mrt_config.runtime_path("daily_caps"), log, what="the daily caps") or {}
    exp = _read_json(mrt_config.runtime_path("expected_positions"), log,
                     what="the expected-position registry") or {}
    books = {"settled_pnl_lifetime": round(life, 4),
             "settled_pnl_hedged_lifetime": hedged_lifetime(life, untracked, exits),
             "settled_pnl_untracked_lifetime": round(untracked, 4),
             "settled_pnl_exits_lifetime": round(exits, 4),
             "restatement_log": [r for r in (tun.get("restatement_log") or []) if isinstance(r, dict)],
             "settled_trades": int(tun.get("settled_trades", 0) or 0),
             "pnl_today": round(float(caps.get("pnl_today", 0.0) or 0.0), 4),
             "fills_today": int(caps.get("fills_today", 0) or 0),
             "open_legs": len(exp) if isinstance(exp, dict) else 0}
    rec = BalanceReconciler(cfg, telegram=None, log=log)
    out = rec.run_once(books, datetime.now(timezone.utc), slot="", manual=True)
    _print(out["text"])
    snap = out["snapshot"]
    if not snap.get("ok"):
        print("\n(one or both venues could not be read — see the errors above; nothing was compared.)")
        return 1
    return 0
