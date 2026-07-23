"""RE-ARM GATE PROOF — seed a real tiny naked Kalshi position and prove the guards catch it.

This is the receipt required before re-arming after the 2026-07-23 invisible-fill incident. It uses
REAL MONEY (a ~$0.60 taker round trip) because the whole failure was that our *beliefs* were fine and
only the venue disagreed. Nothing here is mocked.

What it proves, in order:
  1. FILL-POLL ON A REAL STATE TRANSITION — the seeded BUY is read back through the SAME
     ``_order_matched`` path that returned None for 11.5h, and must now report the true fill count off
     the v2 ``fill_count_fp`` field.
  2. POSITION READ — ``_kalshi_position`` must see the real naked position (it read every position as
     flat before, because it looked for v1 count names that v2 no longer sends).
  3. RECONCILIATION CATCHES + HALTS — with the ticker in the maker scope file, ``reconcile_positions``
     must latch an ORPHAN, set caps.halted, and write the panel banner file. Telegram is deliberately
     wired to RAISE, proving detection/halt does not depend on the alert channel.
  4. SCOPE + ORPHAN PERSISTENCE ACROSS A REAL RESTART — a brand-new executor over the same ops dir
     must come up already HALTED with the ticker still in scope.

It ALWAYS tries to flatten the seeded position in a finally block, and prints the venue-confirmed
flat read plus the realized round-trip cost.

Run:  python -m scripts.prove_orphan_detection            # dry-run, no orders
      python -m scripts.prove_orphan_detection --apply    # places REAL orders
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from src.executor.kalshi_exec import KalshiExec, fp_num          # noqa: E402
from src.genz.maker_rt.caps import LiveCaps                      # noqa: E402
from src.genz.maker_rt.config import load_maker_rt_config        # noqa: E402
from src.genz.maker_rt.orders import KalshiOrderClient           # noqa: E402
from src.genz.maker_rt.pregame_exec import PregameLiveExecutor   # noqa: E402

COUNT = 2                       # contracts — keeps the seeded stake near $0.60
OK, BAD = "  [PASS]", "  [FAIL]"


class _Log:
    def info(self, m, *a):
        print("    log.info  " + (m % a if a else m))

    def warning(self, m, *a):
        print("    log.WARN  " + (m % a if a else m))

    def error(self, m, *a):
        print("    log.ERROR " + (m % a if a else m))


def _boom(_text):
    """Telegram stand-in that FAILS, exactly like the 1,682 HTTP 429s in the incident window."""
    raise RuntimeError("429 Too Many Requests (simulated: alerting is down)")


def _pick_market(ex) -> tuple:
    """The tightest, deepest book in our live universe — cheapest possible real round trip."""
    from src.genz.maker_rt.__main__ import kalshi_tickers, load_trees
    from src.genz.maker_rt.universe import build_universe
    cfg = load_maker_rt_config()
    u = build_universe(load_trees(), time.time(), max_games=cfg.max_games,
                       expire_before_kickoff_s=cfg.expire_before_kickoff_s,
                       horizon_hours=cfg.inplay.horizon_hours)
    best = None
    for t in kalshi_tickers(u):
        try:
            b = ex.get_orderbook(t, side="YES")
        except Exception:                                     # noqa: BLE001
            continue
        ob = (b.get("raw") or {}).get("orderbook_fp") or {}
        ybids = [(float(x[0]), float(x[1])) for x in (ob.get("yes_dollars") or [])]
        if not b["asks"] or not ybids:
            continue
        ask, asksz = b["asks"][0]
        bid = max(p for p, _ in ybids)
        bidsz = sum(s for p, s in ybids if p == bid)
        if bidsz < COUNT * 50 or asksz < COUNT * 50 or not (0.15 <= bid <= 0.85):
            continue                                          # need depth on BOTH sides to exit cheaply
        cand = (round(ask - bid, 4), -bidsz, t, bid, ask)
        if best is None or cand < best:
            best = cand
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="place REAL orders (default: dry-run)")
    args = ap.parse_args()

    cfg = load_maker_rt_config()
    ops = os.path.dirname(cfg.live.arm_file)
    scope_p = os.path.join(ops, "maker_rt_traded_tokens.json")
    orphan_p = os.path.join(ops, "maker_rt_ORPHAN.json")
    ex = KalshiExec(log=None)

    print("=" * 78)
    print("RE-ARM GATE PROOF — seeded naked position")
    print("=" * 78)
    pick = _pick_market(ex)
    if not pick:
        print("no suitable market (need depth on both sides) — retry when books are live.")
        return 1
    _spr, _d, ticker, bid, ask = pick
    print(f"market   : {ticker}")
    print(f"book     : yes_bid {bid:.2f} / yes_ask {ask:.2f}  (seeding {COUNT} YES ~${ask*COUNT:.2f})")
    if not args.apply:
        print("\ndry-run; pass --apply to place the real orders.")
        return 0

    # Back up whatever the scope/orphan files hold so the proof cannot corrupt live state.
    backups = {}
    for p in (scope_p, orphan_p):
        if os.path.exists(p):
            backups[p] = p + ".proofbak"
            shutil.copy2(p, backups[p])

    bal0 = float((ex.get_balance() or {}).get("balance_dollars") or 0)
    failures = []
    buy = None
    try:
        # ---------------------------------------------------------------- seed
        print("\n-- SEED: taker BUY (this creates a REAL naked position) ------------------")
        buy = ex.place_order(ticker, "yes", COUNT, min(0.99, ask + 0.02),
                             time_in_force="immediate_or_cancel",
                             client_order_id=f"proof-seed-{int(time.time())}")
        print(f"  buy -> status={buy['status']} fill_count={buy['fill_count']} "
              f"avg={buy['avg_price']} order_id={buy['order_id']}")
        if int(buy.get("fill_count") or 0) < COUNT:
            print(BAD + " seed did not fill; nothing to prove against.")
            return 1

        # ------------------------------------------------ 1. fill-poll read path
        print("\n-- PROOF 1: fill poll reads a REAL executed order --------------------------")
        koc = KalshiOrderClient(ex, log=None)
        o = koc.order_status(buy["order_id"])
        print(f"  GET order -> status={o.get('status')} "
              f"keys={[k for k in sorted(o) if 'count' in k]}")
        lo = type("LO", (), {"rest_venue": "kalshi", "size": float(COUNT), "matched_seen": 0.0})()
        matched = PregameLiveExecutor._order_matched(lo, o)
        print(f"  _order_matched -> {matched}   (pre-fix this returned None for 11.5h)")
        (print(OK) if matched == COUNT else failures.append("fill-poll read")) or None
        if matched != COUNT:
            print(BAD + f" expected {COUNT}, got {matched}")

        # ------------------------------------------------- 2. real position read
        print("\n-- PROOF 2: _kalshi_position sees the naked position -----------------------")
        raw = ex.get_positions()
        row = next((p for p in (raw.get("market_positions") or [])
                    if p.get("ticker") == ticker), None)
        print(f"  venue row: {json.dumps(row)}")
        print(f"  fp_num position -> {fp_num(row or {}, 'position', 'net_position', 'count')}")

        # Build the executor EXACTLY as production does, with Telegram DELIBERATELY BROKEN.
        with open(scope_p, "w", encoding="utf-8") as fh:      # seed the maker scope
            json.dump({"tokens": [], "tickers": [ticker]}, fh)
        if os.path.exists(orphan_p):
            os.remove(orphan_p)
        caps = LiveCaps(cfg.live)
        exec1 = PregameLiveExecutor(cfg, gate=None, order_client=None, hedger=None, caps=caps,
                                    poly=None, in_flight=None, telegram=_boom, state=None, log=_Log())
        exec1.kalshi = ex
        exec1.kalshi_order_client = koc
        pos = exec1._kalshi_position(ticker)
        print(f"  _kalshi_position -> {pos}")
        if pos and abs(pos) >= COUNT - 1e-9:
            print(OK)
        else:
            failures.append("position read")
            print(BAD + f" expected >= {COUNT}, got {pos}")

        # ------------------------------------- 3. reconciliation catches + halts
        print("\n-- PROOF 3: reconciliation catches, HALTS, banners (Telegram RAISING) ------")
        print(f"  scope file: {json.load(open(scope_p))}")
        orph = exec1.reconcile_positions(datetime.now(timezone.utc))
        print(f"  reconcile_positions -> {json.dumps(orph, default=str)}")
        print(f"  caps.halted={exec1.caps.halted} halt_reason={exec1.caps.halt_reason}")
        banner = json.load(open(orphan_p)) if os.path.exists(orphan_p) else None
        print(f"  ORPHAN banner file: {json.dumps(banner, default=str)}")
        if orph and exec1.caps.halted and banner:
            print(OK + "  detection + halt + banner all landed WITHOUT Telegram")
        else:
            failures.append("reconcile halt/banner")
            print(BAD)

        # ------------------------------- 4. persistence across a REAL restart
        print("\n-- PROOF 4: scope + orphan survive a restart -------------------------------")
        exec2 = PregameLiveExecutor(cfg, gate=None, order_client=None, hedger=None,
                                    caps=LiveCaps(cfg.live), poly=None, in_flight=None,
                                    telegram=None, state=None, log=_Log())
        print(f"  fresh executor: orphan={json.dumps(exec2.orphan, default=str)}")
        print(f"  fresh executor: halted={exec2.caps.halted} scope_tickers={sorted(exec2._traded_tickers)}")
        if exec2.orphan and exec2.caps.halted and ticker in exec2._traded_tickers:
            print(OK + "  a naked position cannot be cleared by a restart")
        else:
            failures.append("restart persistence")
            print(BAD)
    finally:
        # ------------------------------------------------------------- flatten
        print("\n-- CLEANUP: flatten the seeded position ------------------------------------")
        if buy and int(buy.get("fill_count") or 0) > 0:
            try:
                sell = ex.place_market_sell(ticker, "yes", COUNT,
                                            client_order_id=f"proof-flat-{int(time.time())}")
                print(f"  sell -> status={sell['status']} fill_count={sell['fill_count']} "
                      f"avg={sell['avg_price']}")
            except Exception as exc:                          # noqa: BLE001
                print(f"  !! SELL FAILED: {exc} — FLATTEN BY HAND: {ticker}")
            for _ in range(6):
                time.sleep(0.5)
                rows = (ex.get_positions() or {}).get("market_positions") or []
                left = next((fp_num(p, "position", "net_position", "count") or 0.0
                             for p in rows if p.get("ticker") == ticker), 0.0)
                if abs(left) <= 0.5:
                    break
            print(f"  venue position now: {left}  ->  {'FLAT' if abs(left) <= 0.5 else 'NOT FLAT !!'}")
            if abs(left) > 0.5:
                failures.append("FLATTEN — MANUAL ACTION REQUIRED")
        for p, b in backups.items():                          # restore live state
            shutil.move(b, p)
        if not backups.get(scope_p) and os.path.exists(scope_p):
            with open(scope_p, "w", encoding="utf-8") as fh:
                json.dump({"tokens": [], "tickers": []}, fh)
        if not backups.get(orphan_p) and os.path.exists(orphan_p):
            os.remove(orphan_p)
        print("  live scope/orphan files restored to their pre-proof contents.")
        bal1 = float((ex.get_balance() or {}).get("balance_dollars") or 0)
        print(f"\n  balance {bal0:.4f} -> {bal1:.4f}   round-trip cost ${bal0 - bal1:+.4f}")

    print("\n" + "=" * 78)
    print("RESULT: " + ("ALL PROOFS PASSED" if not failures else f"FAILED: {failures}"))
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
