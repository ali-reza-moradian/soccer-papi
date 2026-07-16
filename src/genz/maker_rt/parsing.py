"""PURE parsers for the three websocket message streams -> normalized internal event dicts.

Kept separate from feeds.py so they can be unit-tested against real captured message shapes with no
socket. Every parser accepts an already-JSON-decoded payload (a dict, or — Polymarket batches — a
list) and returns a list of normalized events, each a dict with a ``kind`` discriminator. Malformed
fields are dropped, never raised (a bad frame must not kill the feed).
"""
from __future__ import annotations

from typing import Any, Optional


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _levels(rows: Any) -> dict:
    """[{'price','size'}] or [[price,size]] -> {price(float): size(float)} (positive sizes only)."""
    out: dict = {}
    for lvl in rows or []:
        if isinstance(lvl, dict):
            p, s = _f(lvl.get("price")), _f(lvl.get("size"))
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            p, s = _f(lvl[0]), _f(lvl[1])
        else:
            continue
        if p is not None and s is not None and s > 0:
            out[p] = s
    return out


# --------------------------------------------------------------------------- #
# Polymarket MARKET channel                                                      #
# --------------------------------------------------------------------------- #
def parse_poly_market(raw: Any) -> list:
    """book / price_change / last_trade_price / tick_size_change events (Poly batches as a list)."""
    events: list = []
    for m in (raw if isinstance(raw, list) else [raw]):
        if not isinstance(m, dict):
            continue
        et = m.get("event_type")
        token = m.get("asset_id") or m.get("token_id")
        if et == "book":
            events.append({"kind": "poly_book", "token": token,
                           "bids": _levels(m.get("bids")), "asks": _levels(m.get("asks"))})
        elif et == "price_change":
            changes = []
            rows = m.get("changes")
            if isinstance(rows, list):
                for c in rows:
                    if isinstance(c, dict):
                        p, s = _f(c.get("price")), _f(c.get("size"))
                        if p is not None and s is not None:
                            changes.append((p, str(c.get("side") or "").upper(), s))
            else:                                  # older single-change shape
                p, s = _f(m.get("price")), _f(m.get("size"))
                if p is not None and s is not None:
                    changes.append((p, str(m.get("side") or "").upper(), s))
            events.append({"kind": "poly_price", "token": token, "changes": changes})
        elif et == "last_trade_price":
            events.append({"kind": "poly_trade", "token": token, "price": _f(m.get("price")),
                           "side": str(m.get("side") or "").upper(), "size": _f(m.get("size"))})
        elif et == "tick_size_change":
            events.append({"kind": "poly_tick", "token": token,
                           "tick": _f(m.get("new_tick_size") or m.get("tick_size"))})
    return events


# --------------------------------------------------------------------------- #
# Polymarket USER channel (live only)                                            #
# --------------------------------------------------------------------------- #
def parse_poly_user(raw: Any) -> list:
    """order (PLACEMENT/UPDATE/CANCELLATION) + trade (OUR fill) events."""
    events: list = []
    for m in (raw if isinstance(raw, list) else [raw]):
        if not isinstance(m, dict):
            continue
        et = m.get("event_type")
        if et == "order":
            events.append({"kind": "poly_user_order", "token": m.get("asset_id"),
                           "order_id": m.get("id") or m.get("order_id"),
                           "type": str(m.get("type") or "").upper(),
                           "side": str(m.get("side") or "").upper(),
                           "price": _f(m.get("price")), "size_matched": _f(m.get("size_matched")),
                           "market": m.get("market"), "status": m.get("status")})
        elif et == "trade":
            events.append({"kind": "poly_user_trade", "token": m.get("asset_id"),
                           "order_id": m.get("taker_order_id") or m.get("id"),
                           "side": str(m.get("side") or "").upper(), "price": _f(m.get("price")),
                           "size": _f(m.get("size")), "status": m.get("status"),
                           "market": m.get("market"), "ts": m.get("timestamp")})
    return events


# --------------------------------------------------------------------------- #
# Kalshi WS                                                                      #
# --------------------------------------------------------------------------- #
def _cents(v: Any) -> Optional[int]:
    """A Kalshi price -> integer cents. Accepts a dollar string/float ('0.5000' -> 50) or raw cents."""
    f = _f(v)
    if f is None:
        return None
    return int(round(f * 100)) if 0.0 <= f <= 1.0 else int(round(f))


def _dollars(v: Any) -> Optional[float]:
    """A Kalshi price -> dollars in (0,1). Accepts a dollar string/float or raw cents."""
    f = _f(v)
    if f is None:
        return None
    return f if 0.0 <= f <= 1.0 else f / 100.0


def _kalshi_levels(rows: Any) -> list:
    """Kalshi WS book levels ([['0.5000','100.00'], ...] dollar strings, or [[cents,size]]) ->
    [(int cents, float size)] (positive sizes only)."""
    out: list = []
    for lvl in rows or []:
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            c, s = _cents(lvl[0]), _f(lvl[1])
            if c is not None and s is not None and s > 0:
                out.append((c, s))
    return out


def parse_kalshi(raw: Any) -> list:
    """subscribed / orderbook_snapshot / orderbook_delta / trade / fill / error -> normalized events.
    Real WS shapes (captured 2026-07): book levels are ``yes_dollars_fp``/``no_dollars_fp`` (dollar
    strings), a delta is {price_dollars, delta_fp, side}, and ``seq`` is PER-CONNECTION-CHANNEL (sid),
    monotonic across ALL tickers — a gap means a missed message somewhere, so drop the books + resub."""
    if not isinstance(raw, dict):
        return []
    t = raw.get("type")
    msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else {}
    seq = raw.get("seq")
    sid = raw.get("sid")
    if t == "subscribed":
        return [{"kind": "kalshi_subscribed", "id": raw.get("id"),
                 "channel": msg.get("channel"), "sid": msg.get("sid")}]
    if t == "orderbook_snapshot":
        yes = _kalshi_levels(msg.get("yes_dollars_fp") if msg.get("yes_dollars_fp") is not None else msg.get("yes"))
        no = _kalshi_levels(msg.get("no_dollars_fp") if msg.get("no_dollars_fp") is not None else msg.get("no"))
        return [{"kind": "kalshi_snapshot", "ticker": msg.get("market_ticker"),
                 "yes": yes, "no": no, "seq": seq, "sid": sid}]
    if t == "orderbook_delta":
        price = _cents(msg.get("price_dollars") if msg.get("price_dollars") is not None else msg.get("price"))
        delta = _f(msg.get("delta_fp") if msg.get("delta_fp") is not None else msg.get("delta"))
        return [{"kind": "kalshi_delta", "ticker": msg.get("market_ticker"),
                 "side": str(msg.get("side") or "").lower(), "price": price,
                 "delta": delta, "seq": seq, "sid": sid}]
    if t == "trade":
        yp = msg.get("yes_price_dollars", msg.get("yes_price"))
        np_ = msg.get("no_price_dollars", msg.get("no_price"))
        return [{"kind": "kalshi_trade", "ticker": msg.get("market_ticker"),
                 "yes_price": _dollars(yp), "no_price": _dollars(np_),
                 "count": _f(msg.get("count")), "taker_side": str(msg.get("taker_side") or "").lower(),
                 "seq": seq, "sid": sid, "ts": msg.get("ts")}]
    if t == "fill":
        return [{"kind": "kalshi_fill", "ticker": msg.get("market_ticker"),
                 "order_id": msg.get("order_id"), "side": str(msg.get("side") or "").lower(),
                 "count": _f(msg.get("count")), "yes_price": msg.get("yes_price"),
                 "no_price": msg.get("no_price"), "is_taker": msg.get("is_taker"), "ts": msg.get("ts")}]
    if t == "error":
        return [{"kind": "kalshi_error", "detail": raw.get("msg")}]
    return []


def seq_gap(prev_seq: Optional[int], new_seq: Optional[int]) -> bool:
    """True when Kalshi's per-connection sequence jumped (a dropped message) -> drop book + resubscribe.
    A snapshot (prev None) or a missing seq never counts as a gap."""
    if prev_seq is None or new_seq is None:
        return False
    return int(new_seq) != int(prev_seq) + 1
