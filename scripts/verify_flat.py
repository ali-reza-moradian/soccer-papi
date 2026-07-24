"""READ-ONLY venue position check — proves a maker instrument is flat (or shows what we still hold)
before clearing an ORPHAN banner. Places NOTHING. Reads Kalshi /portfolio/positions and the Poly CLOB
conditional balance for the given instruments.

    python -m scripts.verify_flat KALSHI:<ticker> POLY:<token> [...]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.genz.maker_rt import config as mrt_config          # noqa: E402
from src.genz.maker_rt.__main__ import _load_env            # noqa: E402
from src.genz.maker_rt.clients import build_pregame_order_clients  # noqa: E402


def _kalshi_pos(kalshi, ticker: str):
    from src.executor.kalshi_exec import fp_num
    resp = kalshi.get_positions()
    rows = resp.get("market_positions") if isinstance(resp, dict) else (resp or [])
    if not rows and isinstance(resp, dict):
        rows = resp.get("positions") or resp.get("data") or []
    for p in rows or []:
        if isinstance(p, dict) and (p.get("ticker") == ticker or p.get("market_ticker") == ticker):
            n = fp_num(p, "position", "net_position", "count", "market_position")
            return "UNREADABLE" if n is None else abs(n)
    return 0.0                                                # absent == flat


def main() -> int:
    _load_env()
    cfg = mrt_config.load_maker_rt_config()
    kalshi, poly = build_pregame_order_clients(cfg, log=None)
    if kalshi is None or poly is None:
        print("live.enabled is false — no clients built; cannot verify.")
        return 2
    all_flat = True
    for arg in sys.argv[1:]:
        venue, _, ident = arg.partition(":")
        try:
            if venue.upper() == "KALSHI":
                pos = _kalshi_pos(kalshi, ident)
            else:
                pos = poly.conditional_balance(ident)
        except Exception as exc:  # noqa: BLE001
            print(f"  {arg}: READ FAILED ({exc})")
            all_flat = False
            continue
        flat = isinstance(pos, (int, float)) and abs(pos) <= 0.5
        all_flat = all_flat and flat
        print(f"  {venue.upper():7} {ident[:40]:<40} position={pos}  -> {'FLAT' if flat else 'HELD/UNKNOWN'}")
    print("ALL FLAT" if all_flat else "NOT ALL FLAT")
    return 0 if all_flat else 1


if __name__ == "__main__":
    raise SystemExit(main())
