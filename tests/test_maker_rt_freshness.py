"""Connection-based freshness (the quote-churn fix): a QUIET book on a HEALTHY socket is FRESH; a
down/quiet-too-long connection or a pending seq-gap resync is STALE."""
from __future__ import annotations

from src.genz.maker_rt import parsing
from src.genz.maker_rt.store import BookStore

CF, NQ = 30.0, 180.0                    # conn_fresh_s, node_quiet_max_s


def _poly_book(store, token, ts, bid="0.45"):
    store.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": token,
        "bids": [{"price": bid, "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}), ts)


def test_quiet_book_stays_fresh_on_alive_connection():
    s = BookStore()
    _poly_book(s, "T", 100.0)                       # book CHANGE at t=100
    s.mark_activity("poly", 158.0)                  # 58s later: NO book change, but the socket pinged/ponged
    # node_fresh at t=160: connection fresh (activity 2s ago) AND book ticked 60s ago (< 180) -> FRESH.
    assert s.node_fresh("T", 160.0, CF, NQ) is True
    # the OLD book-change freshness (10s) wrongly called this stale -> the churn bug.
    assert s.is_fresh("T", 160.0, 10.0) is False


def test_connection_down_makes_node_stale():
    s = BookStore()
    _poly_book(s, "T", 100.0)                       # last activity at t=100
    assert s.node_fresh("T", 140.0, CF, NQ) is False    # 40s with NO activity > conn_fresh_s (30) -> down


def test_node_quiet_too_long_is_suspect_even_on_live_connection():
    s = BookStore()
    _poly_book(s, "T", 100.0)                       # book last CHANGED at t=100
    s.mark_activity("poly", 300.0)                  # connection alive at t=300
    # book quiet for 200s (> node_quiet_max_s 180) -> suspect even though the socket is healthy.
    assert s.conn_fresh("poly", 300.0, CF) is True
    assert s.node_fresh("T", 300.0, CF, NQ) is False


def test_seq_gap_pending_resync_makes_kalshi_stale():
    s = BookStore()
    s.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX", "yes_dollars_fp": [["0.50", "100"]],
                "no_dollars_fp": [["0.45", "100"]]}}), 100.0)
    assert s.conn_fresh("kalshi", 100.0, CF) is True
    # a delta with a seq GAP (expected 2, got 5) drops the books + flags a full resubscribe.
    s.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_delta", "sid": 1, "seq": 5,
        "msg": {"market_ticker": "KX", "price_dollars": "0.50", "delta_fp": "10", "side": "yes"}}), 101.0)
    assert s.need_resync() is True
    assert s.conn_fresh("kalshi", 101.0, CF) is False   # pending resync -> data unreliable -> NOT fresh
    s.clear_resync()
    _poly_book(s, "irrelevant", 102.0)                  # (poly activity)
    s.mark_activity("kalshi", 102.0)                    # a fresh snapshot heals it
    assert s.conn_fresh("kalshi", 102.0, CF) is True
