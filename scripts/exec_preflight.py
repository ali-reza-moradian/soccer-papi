"""Executor credential preflight (mirrors the sibling bot's place_one_dollar_bets.py).

Prints both venues' resolved wallet/auth + balances. With the explicit --live flag it places a
ONE-DOLLAR test order on each venue so you can confirm credentials END-TO-END before wiring the
executor to live arbs. WITHOUT --live it only READS (auth + balances + preflight) and places
nothing.

Usage:
    python scripts/exec_preflight.py              # read-only: auth + balances, no orders
    python scripts/exec_preflight.py --live       # ALSO place a $1 test order on each venue
    python scripts/exec_preflight.py --live --kalshi-ticker TICK --poly-token TOKEN

Env required (same accounts as the sibling bot):
    KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY (PEM) or KALSHI_PRIVATE_KEY_PATH
    POLYGON_PRIVATE_KEY, POLY_SIGNATURE_TYPE (default 3), POLY_FUNDER_ADDRESS
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.executor import config as exec_config
from src.executor.kalshi_exec import KalshiExec, KalshiExecError
from src.executor import poly_exec
from src.executor.poly_exec import PolyExec, PolyExecError, min_poly_shares


def _hr(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def preflight_kalshi(cfg, live: bool, ticker: str | None) -> None:
    _hr("KALSHI")
    print(f"api_base       : {cfg.kalshi_api_base}")
    print(f"KALSHI_API_KEY_ID set: {bool(os.environ.get('KALSHI_API_KEY_ID') or os.environ.get('KALSHI_ACCESS_KEY'))}")
    print(f"private key set : {bool(os.environ.get('KALSHI_PRIVATE_KEY') or os.environ.get('KALSHI_PRIVATE_KEY_PATH'))}")
    try:
        k = KalshiExec(api_base=cfg.kalshi_api_base)
        bal = k.get_balance()
        print(f"balance        : {bal}")
    except KalshiExecError as exc:
        print(f"AUTH/BALANCE FAILED: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"AUTH/BALANCE FAILED: {exc}")
        return
    if not live:
        print("(read-only — pass --live and --kalshi-ticker to place a $1 test order)")
        return
    if not ticker:
        print("--live set but no --kalshi-ticker given; skipping Kalshi test order.")
        return
    try:
        ob = k.get_orderbook(ticker)
        best = ob["asks"][0][0] if ob["asks"] else 0.50
        count = max(1, int(round(1.0 / best)))     # ~$1 notional
        print(f"placing ~$1 test: BUY YES {count} @ {best:.4f} on {ticker} ...")
        res = k.place_order(ticker, "YES", count, best, client_order_id="preflight-1usd")
        print(f"  -> {res['status']} fill={res['fill_count']} avg={res['avg_price']} id={res['order_id']}")
    except Exception as exc:  # noqa: BLE001
        print(f"  TEST ORDER FAILED: {exc}")


def preflight_poly(live: bool, token: str | None) -> None:
    _hr("POLYMARKET")
    try:
        w = poly_exec.resolve_wallet()
        print(f"signer         : {w['signer_address']}")
        print(f"funder         : {w['funder']}")
        print(f"signature_type : {w['signature_type']}")
    except PolyExecError as exc:
        print(f"WALLET RESOLUTION FAILED: {exc}")
        return
    p = PolyExec()
    ok, reason = p.can_place_polymarket_orders()
    print(f"can place orders: {ok} — {reason}")
    if not ok:
        return
    try:
        print(f"balance        : {p.get_balance()}")
    except Exception as exc:  # noqa: BLE001
        print(f"balance read failed: {exc}")
    if not live:
        print("(read-only — pass --live and --poly-token to place a $1 test order)")
        return
    if not token:
        print("--live set but no --poly-token given; skipping Polymarket test order.")
        return
    try:
        ob = p.get_orderbook(token)
        best = ob["asks"][0][0] if ob["asks"] else 0.50
        shares = max(min_poly_shares(best), int(round(1.0 / best)))   # ~$1 notional, clears min
        print(f"placing ~$1 test: BUY {shares} @ {best:.4f} on token {token[:12]}... ...")
        res = p.place_order(token, best, shares, "BUY", order_type="FOK")
        print(f"  -> {res['status']} shares={res['shares']} usd={res['usd']} id={res['order_id']}")
    except Exception as exc:  # noqa: BLE001
        print(f"  TEST ORDER FAILED: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Executor credential preflight.")
    ap.add_argument("--live", action="store_true",
                    help="ALSO place a $1 test order on each venue (otherwise read-only).")
    ap.add_argument("--kalshi-ticker", default=os.environ.get("PREFLIGHT_KALSHI_TICKER"))
    ap.add_argument("--poly-token", default=os.environ.get("PREFLIGHT_POLY_TOKEN"))
    args = ap.parse_args()

    cfg = exec_config.load_exec_config()
    print(f"executor.enabled={cfg.enabled} dry_run={cfg.dry_run} require_human_confirm={cfg.require_human_confirm}")
    if args.live:
        print("\n*** --live: this WILL place real ~$1 orders if credentials resolve. ***")

    preflight_kalshi(cfg, args.live, args.kalshi_ticker)
    preflight_poly(args.live, args.poly_token)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
