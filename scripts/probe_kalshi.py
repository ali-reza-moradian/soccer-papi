"""One-off discovery probe for the Kalshi-direct source (B1). NO key, NO auth headers.

Usage:
    python -m scripts.probe_kalshi

TARGETED + rate-limit-safe: Kalshi's unauthenticated read limit is TIGHT, so we do NOT sweep all
open markets. We query the one series we care about (KXWCGAME = the per-match World Cup RESULT
series; NOT KXMENWORLDCUP, the tournament winner outright). The client throttles to >=0.5s between
requests and retries 429/5xx with exponential backoff (see src/kalshi.py).

Each KXWCGAME event is one match (event_ticker KXWCGAME-<YYMMMDD><HOME3><AWAY3>, e.g.
KXWCGAME-26JUN12USAPAR) with 3 mutually-exclusive Yes markets — home win / Tie / away win,
settled at 90 min + stoppage (regulation 1X2).

Steps:
  1. GET /series/KXWCGAME — confirm it exists, print its title.
  2. GET /markets?series_ticker=KXWCGAME&status=open&limit=100 (cursor only if needed) — ONE
     targeted call, not a sweep.
  3. Group markets by event_ticker. Per event, print each market's ticker, the outcome-label field
     (yes_sub_title / title / subtitle — whichever carries the team or "Tie" name), yes_ask,
     yes_bid, last_price, volume.
  4. For USA–Paraguay (KXWCGAME-26JUN12USAPAR), GET /markets/{ticker}/orderbook per market and print
     the depth so we can see real liquidity per leg.
  5. Dump the raw JSON of one market and one orderbook so we can see exact field names + units
     (cents vs dollars). Then STOP — paste the output for the B2 mapping.
"""
from __future__ import annotations

import json
import sys

from src.kalshi import KalshiClient, KalshiError

SERIES = "KXWCGAME"
USA_PAR_EVENT = "KXWCGAME-26JUN12USAPAR"   # the known in-window match we drill into


def _label(m: dict) -> str:
    """The field that carries the human outcome name (team or 'Tie') — show whichever is set."""
    for k in ("yes_sub_title", "subtitle", "title"):
        v = m.get(k)
        if v:
            return f"{k}={v!r}"
    return "(no label field)"


def _pp(label: str, obj) -> None:
    print(f"\n----- {label} (raw JSON) -----")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:4000])


def main() -> int:
    client = KalshiClient()   # throttle + retry/backoff built in

    # 1) Confirm the series exists -----------------------------------------------------------
    print(f"=== GET /series/{SERIES} ===")
    try:
        ser = client.series(SERIES)
    except KalshiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    s = ser.get("series", ser) if isinstance(ser, dict) else ser
    title = s.get("title") if isinstance(s, dict) else None
    print(f"  ok — title={title!r}")
    _pp(f"series {SERIES}", ser)

    # 2) One targeted /markets call for this series (cursor only if there's more) -------------
    print(f"\n=== GET /markets?series_ticker={SERIES}&status=open&limit=100 ===")
    markets: list[dict] = []
    cursor = None
    for page_no in range(1, 11):   # hard cap; expect 1–2 pages for a single series
        try:
            page = client.markets(series_ticker=SERIES, status="open", limit=100, cursor=cursor)
        except KalshiError as exc:
            print(f"ERROR on page {page_no}: {exc}", file=sys.stderr)
            break
        batch = [m for m in (page or {}).get("markets") or [] if isinstance(m, dict)]
        markets.extend(batch)
        cursor = (page or {}).get("cursor") or None
        print(f"  page {page_no}: {len(batch)} market(s)" + (" (more via cursor)" if cursor else ""))
        if not cursor or not batch:
            break
    print(f"  total: {len(markets)} open market(s) in {SERIES}")

    # 3) Group by event_ticker, print the 3 outcomes per event -------------------------------
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker") or "(no event_ticker)", []).append(m)

    print(f"\n=== {len(by_event)} event(s) (each should hold 3 markets: home win / Tie / away win) ===")
    for ev in sorted(by_event):
        ms = by_event[ev]
        print(f"\n  event_ticker={ev}  ({len(ms)} market(s))")
        for m in ms:
            print(f"    ticker={m.get('ticker')} | {_label(m)} | "
                  f"yes_ask={m.get('yes_ask')} yes_bid={m.get('yes_bid')} "
                  f"last_price={m.get('last_price')} volume={m.get('volume')}")

    # 4) USA–Paraguay: order-book depth per leg ----------------------------------------------
    target = by_event.get(USA_PAR_EVENT)
    print(f"\n=== order books for {USA_PAR_EVENT} (real liquidity per leg) ===")
    if not target:
        print(f"  {USA_PAR_EVENT} not among open markets — check the event ticker / that it's open.")
    else:
        for m in target:
            tkr = m.get("ticker")
            print(f"\n  ticker={tkr} | {_label(m)}")
            try:
                ob = client.orderbook(tkr)
                print(f"    orderbook: {json.dumps(ob, ensure_ascii=False)[:1500]}")
            except KalshiError as exc:
                print(f"    orderbook ERROR: {exc}")

    # 5) Raw shapes so B2 can pin field names + units ----------------------------------------
    if markets:
        _pp("one market object", markets[0])
        sample = (target[0] if target else markets[0]).get("ticker")
        if sample:
            try:
                _pp(f"orderbook {sample}", client.orderbook(sample))
            except KalshiError as exc:
                print(f"orderbook dump ERROR: {exc}")

    print("\n=== DONE — paste everything above (esp. the outcome-label field + price/depth units) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
