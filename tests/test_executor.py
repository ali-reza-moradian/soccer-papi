"""Tests for the automated executor (src/executor/).

Network and trading SDKs are always mocked — these tests never touch a real venue. They assert
the safety posture (Phase 0), price/fill logic (Phase 1), dry-run no-op + edge math (Phase 2),
auto-unwind + leg-2 sizing (Phase 3), and every guardrail (Phase 4).
"""
from __future__ import annotations

import json
import os
import time

import pytest

from src.executor import config as exec_config


class _Log:
    def __init__(self):
        self.records = []

    def info(self, *a, **k):
        self.records.append(("info", a))

    def warning(self, *a, **k):
        self.records.append(("warning", a))

    def error(self, *a, **k):
        self.records.append(("error", a))


# =========================================================================
# PHASE 0 — isolation & safety
# =========================================================================
def test_defaults_are_safe():
    cfg = exec_config.ExecConfig()
    assert cfg.enabled is False
    assert cfg.dry_run is True
    assert cfg.require_human_confirm is True
    assert cfg.live_allowed is False          # disabled + dry-run => never live


def test_config_yaml_defaults_are_safe():
    # The shipped config.yaml MUST default to the safe state.
    cfg = exec_config.load_exec_config()
    assert cfg.enabled is False and cfg.dry_run is True and cfg.require_human_confirm is True


def test_live_allowed_only_when_enabled_and_not_dry_run():
    assert exec_config.ExecConfig(enabled=True, dry_run=True).live_allowed is False
    assert exec_config.ExecConfig(enabled=False, dry_run=False).live_allowed is False
    assert exec_config.ExecConfig(enabled=True, dry_run=False).live_allowed is True


def test_stop_file_roundtrip(tmp_path):
    stop = str(tmp_path / "STOP")
    assert exec_config.stop_file_present(stop) is False
    exec_config.trip_stop("test halt", stop)
    assert exec_config.stop_file_present(stop) is True
    assert "test halt" in open(stop).read()
    assert exec_config.clear_stop(stop) is True
    assert exec_config.stop_file_present(stop) is False


def test_overrides_win_over_file():
    cfg = exec_config.load_exec_config(overrides={"enabled": True, "dry_run": False})
    assert cfg.live_allowed is True


# =========================================================================
# PHASE 1 — adapters: price mapping + STRICT fill classification (test e)
# =========================================================================
from src.executor import kalshi_exec
from src.executor import poly_exec


def test_kalshi_yes_no_price_mapping():
    # YES backs at price; NO is the complement on the YES book (1 - price).
    assert kalshi_exec.yes_book_price("YES", 0.32) == pytest.approx(0.32)
    assert kalshi_exec.yes_book_price("NO", 0.32) == pytest.approx(0.68)
    assert kalshi_exec.yes_book_price("yes", 0.10) == pytest.approx(0.10)
    with pytest.raises(kalshi_exec.KalshiExecError):
        kalshi_exec.yes_book_price("MAYBE", 0.5)


def test_kalshi_wire_formats():
    assert kalshi_exec.fmt_count(7) == "7.00"
    assert kalshi_exec.fmt_price(0.3) == "0.3000"
    assert kalshi_exec.fmt_price(0.32555) == "0.3256"     # 4-decimal
    assert kalshi_exec.fmt_price(0.0) == "0.0001"          # clamped
    assert kalshi_exec.fmt_price(1.0) == "0.9999"          # clamped


def test_strict_fill_classification():
    assert kalshi_exec.classify_fill(10, 10) == "filled"
    assert kalshi_exec.classify_fill(10, 4) == "partial"
    assert kalshi_exec.classify_fill(10, 0) == "none"
    # An accepted-but-unfilled (resting) order is NEVER "filled".
    assert kalshi_exec.classify_fill(10, 0) != "filled"
    assert kalshi_exec.classify_fill(0, 0) == "none"
    assert kalshi_exec.classify_fill(5, 9) == "filled"     # >= requested


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeSession:
    """Records requests and returns queued responses (for 429-retry + normalization tests)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "params": params})
        return self._responses.pop(0)


def test_kalshi_place_order_normalizes_and_classifies_partial():
    sess = _FakeSession([_FakeResp(200, {"order": {"order_id": "abc",
                                                   "fill_count": 4, "avg_fill_price": 33}})])
    k = kalshi_exec.KalshiExec(api_key_id="kid", signer=lambda m: "sig", session=sess)
    res = k.place_order("TICK", "YES", 10, 0.33, client_order_id="x")
    assert res["status"] == "partial" and res["fill_count"] == 4
    assert res["avg_price"] == pytest.approx(0.33)        # cents -> dollars
    assert res["order_id"] == "abc"
    # Wire format: count "N.00", side lowercase, action buy.
    body = sess.calls[0]["json"]["order"]
    assert body["count"] == "10.00" and body["side"] == "yes" and body["action"] == "buy"
    assert body["price"] == "0.3300"


def test_kalshi_retries_on_429_then_succeeds():
    sess = _FakeSession([_FakeResp(429, {}), _FakeResp(200, {"order": {"order_id": "z", "fill_count": 10}})])
    k = kalshi_exec.KalshiExec(api_key_id="kid", signer=lambda m: "sig", session=sess, max_retries=3)
    res = k.place_order("TICK", "YES", 10, 0.5)
    assert res["status"] == "filled" and res["fill_count"] == 10
    assert len(sess.calls) == 2                            # retried once


def test_kalshi_orderbook_normalizes_buy_ladder():
    # Buying YES consumes the complements of resting NO bids.
    raw = {"orderbook": {"yes": [[40, 100]], "no": [[55, 30], [50, 70]]}}
    sess = _FakeSession([_FakeResp(200, raw)])
    k = kalshi_exec.KalshiExec(api_key_id="kid", signer=lambda m: "sig", session=sess)
    ob = k.get_orderbook("TICK", side="YES")
    # NO bids at 55,50 -> YES offers at 0.45 (size 30) and 0.50 (size 70), ascending.
    assert ob["asks"][0] == (pytest.approx(0.45), 30)
    assert ob["asks"][1] == (pytest.approx(0.50), 70)


def test_kalshi_signature_message_strips_query():
    captured = {}

    def signer(msg):
        captured["msg"] = msg
        return "sig"

    sess = _FakeSession([_FakeResp(200, {"order": {"fill_count": 1}})])
    k = kalshi_exec.KalshiExec(api_base="https://api.elections.kalshi.com/trade-api/v2",
                               api_key_id="kid", signer=signer, session=sess)
    k.get_balance()
    # message = ts + METHOD + path, with the path containing NO query string.
    assert "GET/trade-api/v2/portfolio/balance" in captured["msg"]
    assert "?" not in captured["msg"]


def test_poly_min_shares_clears_dollar_floor():
    assert poly_exec.min_poly_shares(0.5) == 3            # ceil(1.05/0.5)=3
    assert poly_exec.min_poly_shares(0.05) == 21          # ceil(1.05/0.05)=21
    assert poly_exec.min_poly_shares(0.99) == 2           # ceil(1.05/0.99)=2


def test_poly_wallet_mismatch_detection():
    assert poly_exec.is_wallet_mismatch_error("signer address has to be the address of the API KEY")
    assert poly_exec.is_wallet_mismatch_error("maker address not allowed")
    assert not poly_exec.is_wallet_mismatch_error("insufficient balance")


class _FakePolyClient:
    """Minimal stand-in for ClobClient covering create_order/post_order + metadata reads."""

    def __init__(self, *, post_result, tick=0.01, neg=False, book=None):
        self.post_result = post_result
        self._tick = tick
        self._neg = neg
        self._book = book or {"asks": [{"price": "0.40", "size": "100"}],
                              "bids": [{"price": "0.39", "size": "100"}]}
        self.created = []

    def get_tick_size(self, t):
        return self._tick

    def get_neg_risk(self, t):
        return self._neg

    def create_order(self, args, options=None):
        self.created.append({"args": args, "options": options})
        return {"signed": True}

    def post_order(self, signed, order_type):
        return self.post_result

    def get_order_book(self, t):
        return self._book


def test_poly_place_order_fok_normalizes(monkeypatch):
    # Stub the SDK constants/classes the adapter imports lazily.
    import types
    clob_types = types.ModuleType("py_clob_client.clob_types")
    clob_types.OrderArgs = lambda **kw: types.SimpleNamespace(**kw)
    clob_types.OrderType = types.SimpleNamespace(FOK="FOK", GTC="GTC")
    constants = types.ModuleType("py_clob_client.order_builder.constants")
    constants.BUY, constants.SELL = "BUY", "SELL"
    monkeypatch.setitem(sys.modules, "py_clob_client.clob_types", clob_types)
    monkeypatch.setitem(sys.modules, "py_clob_client.order_builder.constants", constants)

    client = _FakePolyClient(post_result={"success": True, "size_matched": "5", "orderID": "p1"})
    p = poly_exec.PolyExec(client=client)
    res = p.place_order("TOK", 0.40, 5, "BUY", order_type="FOK")
    assert res["status"] == "filled" and res["shares"] == 5
    assert res["usd"] == pytest.approx(2.0) and res["order_id"] == "p1"
    # tick_size + neg_risk were fetched and passed to create_order.
    assert client.created[0]["options"] == {"tick_size": 0.01, "neg_risk": False}


import sys  # noqa: E402  (used by the monkeypatch stub above)


# =========================================================================
# PHASE 2 — dry-run engine: sizing, walk-the-book, edge survival, NO orders
# =========================================================================
from src.executor import fees_sizing
from src.executor import engine as exec_engine
from src.executor import resolve as exec_resolve
from src.executor import config as _cfg


def test_walk_book_vwap_and_partial():
    w = fees_sizing.walk_book([(0.40, 30), (0.42, 100)], 50)
    assert w.filled == 50 and w.fully_filled
    assert w.avg_price == pytest.approx((30 * 0.40 + 20 * 0.42) / 50)
    shallow = fees_sizing.walk_book([(0.40, 10)], 50)
    assert shallow.filled == 10 and not shallow.fully_filled


def test_kalshi_fee_official_formula():
    assert fees_sizing.kalshi_fee_cents(100, 0.50) == 175      # exact $1.75, no float bump
    assert fees_sizing.kalshi_fee_usd(100, 0.50) == pytest.approx(1.75)
    assert fees_sizing.kalshi_fee_cents(0, 0.5) == 0
    assert fees_sizing.poly_fee_usd(123.0) == 0.0


def test_marketable_limit_clamps_and_crosses():
    assert fees_sizing.marketable_limit(0.40, 0.01, side="buy") == pytest.approx(0.41)
    assert fees_sizing.marketable_limit(0.40, 0.01, side="sell") == pytest.approx(0.39)
    assert fees_sizing.marketable_limit(0.999, 0.01, side="buy") == 0.99   # clamp high
    assert fees_sizing.marketable_limit(0.001, 0.05, side="sell") == 0.01  # clamp low


def test_edge_after_costs_survives_and_dies():
    # p_k + p_p = 0.95 < 1 -> arb survives.
    live = fees_sizing.edge_after_costs(100, 0.47, 0.48, kalshi_count=100)
    assert live.arb_survived and live.net_profit > 0
    # p_k + p_p = 1.02 > 1 -> negative edge after costs.
    dead = fees_sizing.edge_after_costs(100, 0.52, 0.50, kalshi_count=100)
    assert not dead.arb_survived and dead.net_profit < 0


def _clean_arb(price_k=0.47, price_p=0.48):
    """A detected clean kalshi<->poly arb in the scanner's telegram_item leg shape."""
    return {
        "match": "Brazil vs Spain", "fixture_id": "F1", "market": "Full Time Result",
        "signature": "sig-abc", "detected_at": "2026-06-24T10:00:00Z",
        "legs": [
            {"book": "kalshi", "outcome": "Brazil", "decimal_odds": 1.0 / price_k,
             "limit": 500, "kalshi_ticker": "KXWCGAME-X-BRA"},
            {"book": "polymarket", "outcome": "not Brazil", "decimal_odds": 1.0 / price_p,
             "limit": 500, "poly_token_id": "0xtoken"},
        ],
    }


class _FakeMarketData:
    """Injectable read-only book source exposing the engine's two ladder methods."""

    def __init__(self, k_ladder, p_ladder):
        self.k_ladder, self.p_ladder = k_ladder, p_ladder

    def kalshi_ask_ladder(self, ticker, side="YES"):
        return list(self.k_ladder)

    def poly_ask_ladder(self, token_id):
        return list(self.p_ladder)


class _ExplodingAdapter:
    """Any order call blows up — proves dry-run never touches it."""

    def place_order(self, *a, **k):
        raise AssertionError("place_order must NOT be called in dry-run")

    def place_market_sell(self, *a, **k):
        raise AssertionError("place_market_sell must NOT be called in dry-run")

    def get_positions(self):
        raise AssertionError("get_positions must NOT be called in dry-run")


def test_normalize_rejects_non_clean_arb():
    bad = {"legs": [{"book": "kalshi"}, {"book": "1xbet"}]}
    with pytest.raises(exec_resolve.ResolveError):
        exec_resolve.normalize_arb(bad)


def test_dryrun_logs_and_places_nothing(tmp_path):
    log_path = str(tmp_path / "dryrun_log.csv")
    md = _FakeMarketData(k_ladder=[(0.47, 300)], p_ladder=[(0.48, 300)])
    cfg = _cfg.ExecConfig()  # safe defaults
    res = exec_engine.execute_arb(
        _clean_arb(), live=False, cfg=cfg, market_data=md,
        kalshi=_ExplodingAdapter(), poly=_ExplodingAdapter(),
        dryrun_log_path=log_path, log=_Log())
    assert res.status == "dryrun"
    assert res.arb_survived is True
    assert res.intended_size > 0
    # The dry-run log row exists with the computed economics.
    rows = list(__import__("csv").DictReader(open(log_path)))
    assert len(rows) == 1
    assert rows[0]["arb_survived"] == "True"
    assert float(rows[0]["net_edge_pct"]) > 0


def test_dryrun_slippage_recorded(tmp_path):
    log_path = str(tmp_path / "dryrun_log.csv")
    # Thin top-of-book forces the walk into a worse level -> positive slippage vs detected price.
    md = _FakeMarketData(k_ladder=[(0.47, 5), (0.55, 500)], p_ladder=[(0.48, 500)])
    res = exec_engine.execute_arb(_clean_arb(), live=False, cfg=_cfg.ExecConfig(),
                                  market_data=md, dryrun_log_path=log_path, log=_Log())
    assert res.status == "dryrun"
    row = list(__import__("csv").DictReader(open(log_path)))[0]
    assert float(row["kalshi_slippage"]) > 0      # paid up beyond the detected price


# =========================================================================
# PHASE 3 — live executor + AUTO-UNWIND (mock adapters)
# =========================================================================
from src.executor.ledger import Ledger
from src.executor.guardrails import Guardrails


class _FakeKalshi:
    def __init__(self, *, fill_count, avg_price=0.47, sell_fill=None, sell_price=0.46, positions=None):
        self._fill_count = fill_count
        self._avg = avg_price
        self._sell_fill = sell_fill if sell_fill is not None else fill_count
        self._sell_price = sell_price
        self._positions = positions if positions is not None else {"market_positions": []}
        self.orders = []
        self.sells = []

    def place_order(self, ticker, side, count, price, **kw):
        self.orders.append({"ticker": ticker, "side": side, "count": count, "price": price})
        return {"status": "filled" if self._fill_count >= count else "partial",
                "fill_count": self._fill_count, "avg_price": self._avg, "order_id": "k1"}

    def place_market_sell(self, ticker, side, count, **kw):
        self.sells.append({"ticker": ticker, "side": side, "count": count})
        return {"status": "filled", "fill_count": min(self._sell_fill, count),
                "avg_price": self._sell_price, "order_id": "ks"}

    def get_positions(self):
        return self._positions


class _FakePoly:
    def __init__(self, *, shares, avg_price=0.48):
        self._shares = shares
        self._avg = avg_price
        self.orders = []

    def place_order(self, token_id, price, size, side="BUY", **kw):
        self.orders.append({"token_id": token_id, "price": price, "size": size, "side": side})
        return {"status": "filled" if self._shares >= size else ("partial" if self._shares > 0 else "none"),
                "shares": self._shares, "usd": self._shares * self._avg,
                "avg_price": self._avg, "order_id": "p1"}


def _live_cfg(**over):
    base = dict(enabled=True, dry_run=False, require_human_confirm=False,
                max_per_trade_usd=10.0, min_book_liquidity_mult=1.25,
                min_net_edge_pct_after_costs=0.0, cooldown_seconds=0.0, dedupe_minutes=0.0)
    base.update(over)
    return _cfg.ExecConfig(**base)


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """Redirect the STOP file + ledger into tmp so live tests never touch the repo."""
    stop = str(tmp_path / "STOP")
    monkeypatch.setattr(_cfg, "STOP_FILE", stop)
    ledger = Ledger(str(tmp_path / "ledger.csv"))
    md = _FakeMarketData(k_ladder=[(0.47, 300)], p_ladder=[(0.48, 300)])
    return {"stop": stop, "ledger": ledger, "md": md, "tmp": tmp_path}


def _run_live(env, kalshi, poly, cfg=None, confirm=None):
    cfg = cfg or _live_cfg()
    guard = Guardrails(cfg, ledger=env["ledger"], stop_path=env["stop"])
    return exec_engine.execute_arb(
        _clean_arb(), live=True, cfg=cfg, market_data=env["md"],
        kalshi=kalshi, poly=poly, ledger=env["ledger"], guard=guard,
        confirm=confirm, log=_Log())


def test_live_happy_path_both_fill(live_env):
    k = _FakeKalshi(fill_count=10)
    p = _FakePoly(shares=10)
    res = _run_live(live_env, k, p)
    assert res.status == "filled"
    assert len(k.orders) == 1 and len(p.orders) == 1
    assert not k.sells                                 # no unwind needed


def test_live_kalshi_no_fill_aborts_with_no_poly(live_env):
    k = _FakeKalshi(fill_count=0)
    p = _FakePoly(shares=0)
    res = _run_live(live_env, k, p)
    assert res.status == "kalshi_no_fill"
    assert len(p.orders) == 0                          # never touched poly -> no exposure
    assert not k.sells


def test_autounwind_fires_when_poly_fails(live_env):
    """(a) Poly leg fails after a Kalshi fill -> immediate kalshi market-sell of the FULL fill."""
    k = _FakeKalshi(fill_count=10)
    p = _FakePoly(shares=0)                            # FOK killed
    res = _run_live(live_env, k, p)
    assert res.status == "leg_failure_unwind"
    assert len(k.sells) == 1
    assert k.sells[0]["count"] == 10                   # unwind size == full unhedged kalshi fill
    # Ledger recorded the unwind event + cost.
    row = live_env["ledger"].rows()[0]
    assert row["status"] == "leg_failure_unwind"
    assert row["unwind_event"] == "kalshi_market_sell"
    assert float(row["unwind_cost"]) >= 0


def test_autounwind_size_on_partial_poly(live_env):
    """(a) Poly under-fills (3 of 10) -> unwind exactly the 7 uncovered kalshi contracts."""
    k = _FakeKalshi(fill_count=10)
    p = _FakePoly(shares=3)
    res = _run_live(live_env, k, p)
    assert res.status == "leg_failure_unwind"
    assert k.sells[0]["count"] == 7                    # 10 filled - 3 hedged = 7 naked


def test_leg2_sized_to_actual_kalshi_fill_incl_partial(live_env):
    """(b) Kalshi partially fills (4 of 10) -> Poly FOK sized to the ACTUAL 4 (>= poly min)."""
    k = _FakeKalshi(fill_count=4)
    p = _FakePoly(shares=4)
    res = _run_live(live_env, k, p)
    expected = max(4, fees_sizing.min_poly_shares(p._avg))
    assert p.orders[0]["size"] == expected
    assert res.status == "filled"                      # poly covered the 4-contract kalshi fill


def test_posthoc_naked_leg_triggers_second_unwind_and_halt(live_env):
    """(spec 6) A naked leg detected post-hoc -> one more unwind, then STOP file set."""
    # Both legs "fill" 10, but positions report 15 held -> 5 naked beyond the 10 poly hedge.
    k = _FakeKalshi(fill_count=10, positions={"market_positions": [
        {"ticker": "KXWCGAME-X-BRA", "position": 15}]})
    p = _FakePoly(shares=10)
    res = _run_live(live_env, k, p)
    assert res.status == "unhedged_halt"
    assert len(k.sells) == 1 and k.sells[0]["count"] == 5
    assert _cfg.stop_file_present(live_env["stop"]) is True   # HALT engaged


def test_live_blocked_when_not_allowed(live_env):
    cfg = _live_cfg(enabled=False)                     # master kill switch off
    k, p = _FakeKalshi(fill_count=10), _FakePoly(shares=10)
    res = _run_live(live_env, k, p, cfg=cfg)
    assert res.status == "blocked"
    assert not k.orders and not p.orders               # nothing placed


def test_human_confirm_gate_blocks_without_yes(live_env):
    cfg = _live_cfg(require_human_confirm=True)
    k, p = _FakeKalshi(fill_count=10), _FakePoly(shares=10)
    res = _run_live(live_env, k, p, cfg=cfg, confirm=lambda n, s: False)
    assert res.status == "aborted"
    assert not k.orders                                # declined -> nothing placed


# =========================================================================
# PHASE A — N-leg generalization (3-way 1x2 winner arbs)
# =========================================================================
def _three_leg_arb(*, home_v="kalshi", draw_v="kalshi", away_v="polymarket"):
    """A 3-way 1x2 winner arb (Home/Draw/Away), MECE, S = 0.30*3 = 0.90 < 1. Legs split across
    venues per the args; each leg carries its persisted execution id."""
    def leg(outcome, venue, vid, side):
        d = {"book": venue, "venue": venue, "outcome": outcome, "decimal_odds": 1.0 / 0.30,
             "limit": 200, "venue_id": vid, "venue_side": side}
        if venue == "polymarket":
            d["neg_risk"] = True
            d["tick_size"] = 0.01
        return d
    return {
        "match": "Brazil vs Spain", "fixture_id": "F9", "market": "Full Time Result",
        "signature": "sig-3way", "detected_at": "2026-06-24T10:00:00Z",
        "legs": [
            leg("Home", home_v, "K-HOME" if home_v == "kalshi" else "P-HOME", "YES"),
            leg("Draw", draw_v, "K-DRAW" if draw_v == "kalshi" else "P-DRAW", "YES"),
            leg("Away", away_v, "P-AWAY" if away_v == "polymarket" else "K-AWAY", "YES"),
        ],
    }


class _MultiMarketData:
    """Per-identifier live ladders for N-leg tests."""

    def __init__(self, ladders: dict):
        self.ladders = ladders

    def kalshi_ask_ladder(self, ticker, side="YES"):
        return list(self.ladders[ticker])

    def poly_ask_ladder(self, token_id):
        return list(self.ladders[token_id])


class _MultiKalshi:
    """Fills per ticker (cap); place_order returns min(cap, requested). Records orders + sells."""

    def __init__(self, caps: dict, *, avg=0.30, sell_price=0.29, positions=None):
        self.caps, self.avg, self.sell_price = caps, avg, sell_price
        self._positions = positions
        self.orders, self.sells = [], []

    def place_order(self, ticker, side, count, price, **kw):
        self.orders.append({"ticker": ticker, "side": side, "count": count})
        f = min(self.caps.get(ticker, count), count)
        return {"status": "filled" if f >= count else "partial", "fill_count": f,
                "avg_price": self.avg, "order_id": "k"}

    def place_market_sell(self, ticker, side, count, **kw):
        self.sells.append({"ticker": ticker, "side": side, "count": count})
        return {"status": "filled", "fill_count": int(count), "avg_price": self.sell_price, "order_id": "ks"}

    def get_positions(self):
        return self._positions if self._positions is not None else {"market_positions": []}


class _MultiPoly:
    """FOK fills per token (fully or 0). Records orders + sells."""

    def __init__(self, caps: dict, *, avg=0.30, sell_price=0.29):
        self.caps, self.avg, self.sell_price = caps, avg, sell_price
        self.orders, self.sells = [], []

    def place_order(self, token_id, price, size, side="BUY", **kw):
        self.orders.append({"token_id": token_id, "size": size})
        cap = self.caps.get(token_id, size)
        shares = size if cap >= size else 0          # FOK: all or nothing
        return {"status": "filled" if shares >= size else "none", "shares": shares,
                "usd": shares * self.avg, "avg_price": self.avg, "order_id": "p"}

    def place_market_sell(self, token_id, shares, **kw):
        self.sells.append({"token_id": token_id, "shares": shares})
        return {"status": "filled", "shares": shares, "avg_price": self.sell_price}


_3LEG_LADDERS = {"K-HOME": [(0.30, 100)], "K-DRAW": [(0.30, 100)], "P-AWAY": [(0.30, 100)]}


def _run_live_n(env, arb, kalshi, poly, ladders, cfg=None, confirm=None):
    cfg = cfg or _live_cfg(max_per_trade_usd=1000.0)   # cap high so size is depth-bound (= 80)
    md = _MultiMarketData(ladders)
    guard = Guardrails(cfg, ledger=env["ledger"], stop_path=env["stop"])
    return exec_engine.execute_arb(arb, live=True, cfg=cfg, market_data=md, kalshi=kalshi,
                                   poly=poly, ledger=env["ledger"], guard=guard, confirm=confirm,
                                   log=_Log())


def test_3leg_dryrun_places_nothing(tmp_path):
    """(d) N-leg dry-run still places nothing and records n_legs=3."""
    log_path = str(tmp_path / "d.csv")
    md = _MultiMarketData(_3LEG_LADDERS)
    res = exec_engine.execute_arb(_three_leg_arb(), live=False, cfg=_cfg.ExecConfig(),
                                  market_data=md, kalshi=_ExplodingAdapter(), poly=_ExplodingAdapter(),
                                  dryrun_log_path=log_path, log=_Log())
    assert res.status == "dryrun" and res.arb_survived is True
    row = list(__import__("csv").DictReader(open(log_path)))[0]
    assert row["n_legs"] == "3" and float(row["net_edge_pct"]) > 0


def test_3way_with_1xbet_leg_rejected_untradable(tmp_path):
    """(c) A 3-way arb containing a non-tradable (1xbet) leg is skipped as untradable."""
    arb = _three_leg_arb()
    arb["legs"][2] = {"book": "1xbet", "outcome": "Away", "decimal_odds": 3.0}
    k, p = _MultiKalshi({}), _MultiPoly({})
    res = exec_engine.execute_arb(arb, live=False, cfg=_cfg.ExecConfig(),
                                  market_data=_MultiMarketData(_3LEG_LADDERS), kalshi=k, poly=p,
                                  dryrun_log_path=str(tmp_path / "d.csv"), log=_Log())
    assert res.status == "skipped" and "untradable_venue" in res.reason
    assert not k.orders and not p.orders


def test_3leg_autounwind_closes_both_kalshi_when_poly_fails(live_env):
    """(a) Home+Draw on Kalshi fill; Away (Poly) FOK fails -> unwind BOTH kalshi legs -> flat."""
    k = _MultiKalshi({"K-HOME": 999, "K-DRAW": 999})        # both kalshi fill fully
    p = _MultiPoly({"P-AWAY": 0})                            # poly FOK killed
    res = _run_live_n(live_env, _three_leg_arb(), k, p, _3LEG_LADDERS)
    assert res.status == "leg_failure_unwind"
    # Both already-filled kalshi legs were closed back to flat; poly never needed unwinding.
    assert len(k.sells) == 2 and {s["ticker"] for s in k.sells} == {"K-HOME", "K-DRAW"}
    assert all(s["count"] == 80 for s in k.sells)           # size = haircut(100,0.8)=80
    assert not p.sells
    row = live_env["ledger"].rows()[0]
    assert row["status"] == "leg_failure_unwind" and "kalshi_market_sell" in row["unwind_event"]


def test_3leg_partial_resizes_later_legs_and_balances(live_env):
    """(b) Leg-2 (Kalshi Draw) partial-fills 50 -> leg-3 (Poly) sized down to 50, leg-1 excess
    unwound, so all three end at the reduced matched size of 50."""
    k = _MultiKalshi({"K-HOME": 999, "K-DRAW": 50})         # draw caps at 50 (partial of 80)
    p = _MultiPoly({"P-AWAY": 999})
    res = _run_live_n(live_env, _three_leg_arb(), k, p, _3LEG_LADDERS)
    assert res.status == "leg_failure_unwind"
    assert res.detail["matched"] == 50
    # leg-3 (poly) was sized down to the realized minimum (50), not the original 80.
    assert p.orders[0]["size"] == 50
    # leg-1 (home) over-filled to 80 -> excess 30 unwound; draw/poly already at 50.
    assert len(k.sells) == 1 and k.sells[0]["ticker"] == "K-HOME" and k.sells[0]["count"] == 30


def test_3leg_happy_path_all_fill_no_unwind(live_env):
    k = _MultiKalshi({"K-HOME": 999, "K-DRAW": 999})
    p = _MultiPoly({"P-AWAY": 999})
    res = _run_live_n(live_env, _three_leg_arb(), k, p, _3LEG_LADDERS)
    assert res.status == "filled"
    assert len(k.orders) == 2 and len(p.orders) == 1
    assert not k.sells and not p.sells                      # clean hedge, no unwind


def test_3leg_guardrails_cover_total_capital_and_every_book():
    """Per-trade cap is over ALL three legs' capital; min-liquidity must hold on EVERY leg's book."""
    narb = exec_resolve.normalize_arb(_three_leg_arb())
    assert narb.n_legs == 3
    leg_ladders = [(leg, [(0.30, 100)]) for leg in narb.legs]
    g = Guardrails(_live_cfg(max_per_trade_usd=5.0))
    # 3-leg total cost $9 > $5 cap -> blocked (cap spans all legs, not per-leg).
    d = g.pre_trade_check_n(narb, _edge(total_cost=9.0), leg_ladders, 10, now=1000)
    assert not d.allowed and "per-trade cap" in d.reason
    # Thin book on the THIRD leg alone trips min-liquidity.
    g2 = Guardrails(_live_cfg())
    thin = [(narb.legs[0], [(0.30, 100)]), (narb.legs[1], [(0.30, 100)]),
            (narb.legs[2], [(0.30, 5)])]
    d2 = g2.pre_trade_check_n(narb, _edge(), thin, 10, now=1000)
    assert not d2.allowed and "liquidity" in d2.reason


# =========================================================================
# PHASE 4 — guardrails: every cap trips correctly (test c)
# =========================================================================
def _norm():
    return exec_resolve.normalize_arb(_clean_arb())


def _edge(total_cost=9.0, net_edge_pct=3.0):
    from src.executor.fees_sizing import EdgeResult
    return EdgeResult(size=10, kalshi_fill_price=0.47, poly_fill_price=0.48,
                      kalshi_cost=4.7, poly_cost=4.8, kalshi_fee=0.18, poly_fee=0.0,
                      total_cost=total_cost, payout=10, net_profit=10 - total_cost,
                      net_edge_pct=net_edge_pct, arb_survived=net_edge_pct > 0)


_DEEP = [(0.47, 300)]     # plenty of depth at target
_THIN = [(0.47, 5)]       # too thin for the liquidity multiplier


def test_guard_per_trade_cap_trips():
    g = Guardrails(_live_cfg(max_per_trade_usd=10.0))
    d = g.pre_trade_check(_norm(), _edge(total_cost=11.0), _DEEP, _DEEP, 10, now=1000)
    assert not d.allowed and "per-trade cap" in d.reason


def test_guard_min_liquidity_trips_each_side():
    g = Guardrails(_live_cfg())
    d_k = g.pre_trade_check(_norm(), _edge(), _THIN, _DEEP, 10, now=1000)
    assert not d_k.allowed and "kalshi liquidity" in d_k.reason
    d_p = g.pre_trade_check(_norm(), _edge(), _DEEP, _THIN, 10, now=1000)
    assert not d_p.allowed and "poly liquidity" in d_p.reason


def test_guard_min_net_edge_trips():
    g = Guardrails(_live_cfg(min_net_edge_pct_after_costs=1.0))
    d = g.pre_trade_check(_norm(), _edge(net_edge_pct=0.4), _DEEP, _DEEP, 10, now=1000)
    assert not d.allowed and "net edge" in d.reason


def test_guard_dedupe_trips_within_window():
    g = Guardrails(_live_cfg(dedupe_minutes=5.0))
    n = _norm()
    g.mark_fired(n.fingerprint, now=1000)
    d = g.pre_trade_check(n, _edge(), _DEEP, _DEEP, 10, now=1000 + 60)   # 1 min later
    assert not d.allowed and "dedupe" in d.reason


def test_guard_cooldown_trips():
    g = Guardrails(_live_cfg(cooldown_seconds=60.0, dedupe_minutes=0.0))
    g.mark_fired("other-fp", now=1000)
    d = g.pre_trade_check(_norm(), _edge(), _DEEP, _DEEP, 10, now=1000 + 10)
    assert not d.allowed and "cooldown" in d.reason


def test_guard_consecutive_error_halts():
    g = Guardrails(_live_cfg(max_consecutive_errors=3))
    assert g.record_error().allowed                    # 1
    assert g.record_error().allowed                    # 2
    third = g.record_error()                           # 3 -> halt
    assert not third.allowed and third.halt


def test_guard_stop_file_halts(tmp_path):
    stop = str(tmp_path / "STOP")
    exec_config.trip_stop("manual", stop)
    g = Guardrails(_live_cfg(), stop_path=stop)
    d = g.pre_trade_check(_norm(), _edge(), _DEEP, _DEEP, 10, now=1000)
    assert not d.allowed and d.halt and "STOP" in d.reason


def test_guard_daily_caps_from_ledger(tmp_path):
    led = Ledger(str(tmp_path / "l.csv"))
    # Pre-load today's ledger with spend near the cap.
    import time as _t
    today = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    led.append_submit({"submit_utc": today, "kalshi_fill_usd": 95, "poly_fill_usd": 95,
                       "realized_pnl": 0})
    g = Guardrails(_live_cfg(max_daily_spend_usd=200.0, max_per_trade_usd=50.0), ledger=led)
    d = g.pre_trade_check(_norm(), _edge(total_cost=20.0), _DEEP, _DEEP, 10, now=1000)
    assert not d.allowed and "daily spend cap" in d.reason   # 190 + 20 > 200


def test_guard_daily_loss_cap_halts(tmp_path):
    led = Ledger(str(tmp_path / "l.csv"))
    import time as _t
    today = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    led.append_submit({"submit_utc": today, "realized_pnl": -60})   # already lost $60
    g = Guardrails(_live_cfg(max_daily_loss_usd=50.0), ledger=led)
    d = g.pre_trade_check(_norm(), _edge(), _DEEP, _DEEP, 10, now=1000)
    assert not d.allowed and d.halt and "daily loss" in d.reason


def test_guard_max_trades_per_day(tmp_path):
    led = Ledger(str(tmp_path / "l.csv"))
    import time as _t
    today = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    for _ in range(3):
        led.append_submit({"submit_utc": today, "realized_pnl": 0})
    g = Guardrails(_live_cfg(max_trades_per_day=3), ledger=led)
    d = g.pre_trade_check(_norm(), _edge(), _DEEP, _DEEP, 10, now=1000)
    assert not d.allowed and "max_trades_per_day" in d.reason


def test_guard_all_pass_when_healthy(tmp_path):
    led = Ledger(str(tmp_path / "l.csv"))
    g = Guardrails(_live_cfg(), ledger=led, stop_path=str(tmp_path / "STOP"))
    d = g.pre_trade_check(_norm(), _edge(net_edge_pct=3.0), _DEEP, _DEEP, 10, now=1000)
    assert d.allowed and d.reason == "ok"


def test_ledger_submit_then_update_roundtrip(tmp_path):
    led = Ledger(str(tmp_path / "l.csv"))
    tid = led.append_submit({"fixture": "A vs B", "intended_size": 10, "status": "submitted"})
    assert led.update(tid, status="filled", kalshi_fill_count=10, realized_pnl=0.5)
    row = led.rows()[0]
    assert row["status"] == "filled" and row["kalshi_fill_count"] == "10"
    assert led.update("nonexistent", status="x") is False


# =========================================================================
# PHASE 5 — CLI + arb source
# =========================================================================
from src.executor import arb_source
from src.executor import cli as exec_cli


def _write_csv(path, rows):
    import csv as _csv
    cols = ["detected_at_et", "signature", "match", "market", "fixture_id", "roi_pct",
            "max_profit", "bookmakers", "legs_json"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_arb_source_filters_to_clean(tmp_path):
    import json
    csv_path = str(tmp_path / "arbs.csv")
    clean_legs = json.dumps([
        {"book": "kalshi", "outcome": "A", "decimal_odds": 2.1, "kalshi_ticker": "T"},
        {"book": "polymarket", "outcome": "B", "decimal_odds": 2.1, "poly_token_id": "0x"}])
    dirty_legs = json.dumps([
        {"book": "kalshi", "outcome": "A", "decimal_odds": 2.1},
        {"book": "1xbet", "outcome": "B", "decimal_odds": 2.1}])
    _write_csv(csv_path, [
        {"signature": "clean", "match": "A vs B", "market": "FTR", "roi_pct": "3.0",
         "bookmakers": "kalshi, polymarket", "legs_json": clean_legs},
        {"signature": "dirty", "match": "C vs D", "market": "FTR", "roi_pct": "9.0",
         "bookmakers": "kalshi, 1xbet", "legs_json": dirty_legs},
    ])
    arbs = arb_source.load_clean_arbs(csv_path)
    assert len(arbs) == 1 and arbs[0]["signature"] == "clean"


def test_cli_status_reports_safe_defaults(capsys):
    rc = exec_cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "enabled=False" in out and "dry_run=True" in out and "live_allowed=False" in out
    assert "STOP file present:" in out


def test_cli_dryrun_handles_no_arbs(tmp_path):
    empty = str(tmp_path / "none.csv")          # nonexistent -> no arbs
    rc = exec_cli.main(["--csv", empty, "dryrun"])
    assert rc == 0


# =========================================================================
# IDENTIFIER PERSISTENCE — scanner-emitted venue IDs flow into the executor
# =========================================================================
def _clean_arb_with_venue(price_k=0.47, price_p=0.48):
    """A detected clean arb whose legs carry the NEW persisted execution identifiers."""
    return {
        "match": "Brazil vs Spain", "fixture_id": "F1", "market": "Full Time Result",
        "signature": "sig-ids", "detected_at": "2026-06-24T10:00:00Z",
        "legs": [
            {"book": "kalshi", "outcome": "Brazil", "decimal_odds": 1.0 / price_k, "limit": 500,
             "venue": "kalshi", "venue_id": "KXWCGAME-26JUN24BRASPA-BRA", "venue_side": "YES"},
            {"book": "polymarket", "outcome": "not Brazil", "decimal_odds": 1.0 / price_p, "limit": 500,
             "venue": "polymarket", "venue_id": "0xbeef", "venue_side": "BUY",
             "neg_risk": True, "tick_size": 0.01},
        ],
    }


def test_resolve_reads_persisted_ids():
    narb = exec_resolve.normalize_arb(_clean_arb_with_venue())
    assert narb.kalshi.identifier == "KXWCGAME-26JUN24BRASPA-BRA"
    assert narb.kalshi.side == "YES" and narb.kalshi.from_detection is True
    assert narb.poly.identifier == "0xbeef" and narb.poly.side == "BUY"
    assert narb.poly.neg_risk is True and narb.poly.tick_size == 0.01
    assert narb.poly.from_detection is True


def test_resolve_classifies_by_venue_field_over_book():
    """Even if `book` were absent, the persisted `venue` field classifies the leg."""
    arb = _clean_arb_with_venue()
    for leg in arb["legs"]:
        leg.pop("book")                              # only `venue` remains
    narb = exec_resolve.normalize_arb(arb)
    assert narb.kalshi.identifier and narb.poly.identifier


def test_dryrun_proceeds_with_persisted_ids(tmp_path):
    """With IDs present, the dry-run fetches books, walks them, and logs a row (no skip)."""
    log_path = str(tmp_path / "dryrun_log.csv")
    md = _FakeMarketData(k_ladder=[(0.47, 300)], p_ladder=[(0.48, 300)])
    res = exec_engine.execute_arb(_clean_arb_with_venue(), live=False, cfg=_cfg.ExecConfig(),
                                  market_data=md, dryrun_log_path=log_path, log=_Log())
    assert res.status == "dryrun" and res.intended_size > 0
    row = list(__import__("csv").DictReader(open(log_path)))[0]
    assert row["fingerprint"] == "sig-ids" and float(row["net_edge_pct"]) != 0


def test_legacy_row_without_ids_still_skips_cleanly(tmp_path):
    """Older rows lacking venue_id are skipped with a clear reason (not crashed)."""
    legacy = {"match": "X", "market": "M", "signature": "old",
              "legs": [{"book": "kalshi", "decimal_odds": 2.1},
                       {"book": "polymarket", "decimal_odds": 2.1}]}
    res = exec_engine.execute_arb(legacy, live=False, cfg=_cfg.ExecConfig(),
                                  market_data=_FakeMarketData([(0.4, 9)], [(0.4, 9)]),
                                  dryrun_log_path=str(tmp_path / "d.csv"), log=_Log())
    assert res.status == "skipped" and "identifier" in res.reason


# =========================================================================
# PHASE B — in-play live feed routes WS book updates into execute_arb (dry-run)
# =========================================================================
from src.executor import live_feed as lf


def _spy_executor():
    calls = []

    def _exec(arb, **kw):
        calls.append({"arb": arb, "kw": kw})
        # mimic the real engine return shape minimally
        return exec_engine.ExecResult("dryrun", live=kw.get("live", False),
                                      arb_survived=True, intended_size=10)
    _exec.calls = calls
    return _exec


def _2way_tracked():
    return lf.TrackedMarket(
        fixture="Brazil vs Spain", fixture_id="F1", market="Both Teams To Score",
        outcomes=["Yes", "No"],
        placements={
            "Yes": [lf.OutcomePlacement("Yes", "kalshi", "K-BTTS-YES", "YES")],
            "No": [lf.OutcomePlacement("No", "polymarket", "P-BTTS-NO", "BUY")],
        })


def _3way_tracked():
    return lf.TrackedMarket(
        fixture="Brazil vs Spain", fixture_id="F1", market="Full Time Result",
        outcomes=["Home", "Draw", "Away"],
        placements={
            "Home": [lf.OutcomePlacement("Home", "kalshi", "K-HOME", "YES")],
            "Draw": [lf.OutcomePlacement("Draw", "kalshi", "K-DRAW", "YES")],
            "Away": [lf.OutcomePlacement("Away", "polymarket", "P-AWAY", "BUY")],
        })


def test_live_feed_routes_arb_into_execute_arb_dry_run():
    """A WS book update that completes a sub-1.0 (S<1) market routes ONE arb into execute_arb,
    and crucially with live=False (dry-run)."""
    spy = _spy_executor()
    feed = lf.LiveFeed(_cfg.ExecConfig(), [_2way_tracked()], executor=spy, log=_Log())
    # Kalshi YES book: NO bid @ 53c -> YES ask 0.47. (no arb yet: only one leg priced.)
    assert feed.handle_kalshi_message({"type": "orderbook_snapshot",
                                       "msg": {"market_ticker": "K-BTTS-YES",
                                               "no": [[53, 300]]}}) == []
    assert spy.calls == []                          # market incomplete -> no route yet
    # Poly book completes the market: ask 0.48 -> S = 0.47 + 0.48 = 0.95 < 1 -> arb.
    results = feed.handle_poly_message({"event_type": "book", "asset_id": "P-BTTS-NO",
                                        "asks": [{"price": "0.48", "size": "300"}]})
    assert len(results) == 1 and spy.calls
    routed = spy.calls[0]
    assert routed["kw"]["live"] is False           # ALWAYS dry-run from the live feed
    legs = routed["arb"]["legs"]
    assert {l["venue"] for l in legs} == {"kalshi", "polymarket"}
    assert routed["arb"]["source"] == "live"


def test_live_feed_no_route_when_no_arb():
    """S >= 1 (no edge) -> nothing routed."""
    spy = _spy_executor()
    feed = lf.LiveFeed(_cfg.ExecConfig(), [_2way_tracked()], executor=spy, log=_Log())
    feed.handle_kalshi_message({"msg": {"market_ticker": "K-BTTS-YES", "no": [[45, 300]]}})  # YES 0.55
    feed.handle_poly_message({"asset_id": "P-BTTS-NO", "asks": [[0.55, 300]]})               # 0.55
    assert spy.calls == []                          # S = 1.10 >= 1 -> no arb


def test_live_feed_routes_3leg_arb():
    """A 3-way 1x2 split across venues routes once all three outcomes are priced and S<1."""
    spy = _spy_executor()
    feed = lf.LiveFeed(_cfg.ExecConfig(), [_3way_tracked()], executor=spy, log=_Log())
    feed.handle_kalshi_message({"msg": {"market_ticker": "K-HOME", "no": [[70, 200]]}})   # YES 0.30
    feed.handle_kalshi_message({"msg": {"market_ticker": "K-DRAW", "no": [[70, 200]]}})   # YES 0.30
    assert spy.calls == []                          # away not priced yet
    res = feed.handle_poly_message({"asset_id": "P-AWAY", "asks": [[0.30, 200]]})         # 0.30
    assert len(res) == 1                            # S = 0.90 < 1 -> arb routed
    assert len(spy.calls[0]["arb"]["legs"]) == 3
    assert spy.calls[0]["kw"]["live"] is False


def test_live_feed_end_to_end_writes_dryrun_log(tmp_path):
    """Without a spy, the real execute_arb runs in dry-run off the live store and logs a row."""
    log_path = str(tmp_path / "live_dryrun.csv")
    feed = lf.LiveFeed(_cfg.ExecConfig(), [_2way_tracked()], dryrun_log_path=log_path, log=_Log())
    feed.handle_kalshi_message({"msg": {"market_ticker": "K-BTTS-YES", "no": [[53, 300]]}})
    results = feed.handle_poly_message({"asset_id": "P-BTTS-NO", "asks": [[0.48, 300]]})
    assert results and results[0].status == "dryrun"
    rows = list(__import__("csv").DictReader(open(log_path)))
    assert len(rows) == 1 and rows[0]["arb_survived"] == "True"


def test_live_feed_start_noop_when_live_disabled():
    """start() is a no-op (returns False) while executor.live_enabled is false (the default)."""
    feed = lf.LiveFeed(_cfg.ExecConfig(), [_2way_tracked()], log=_Log())
    assert _cfg.ExecConfig().live_enabled is False
    assert feed.start() is False                     # never opens a socket / places anything


def test_parse_kalshi_and_poly_messages():
    tk, lad = lf.parse_kalshi_message({"msg": {"market_ticker": "T", "no": [[53, 300], [50, 100]]}})
    assert tk == "T" and lad[0] == (pytest.approx(0.47), 300)   # complement of NO bid, ascending
    tok, lad2 = lf.parse_poly_message({"asset_id": "X", "asks": [{"price": "0.48", "size": "200"}]})
    assert tok == "X" and lad2 == [(pytest.approx(0.48), 200)]
    assert lf.parse_kalshi_message({"type": "ping"}) is None    # non-book message ignored
    assert lf.parse_poly_message({"asset_id": "X"}) is None     # no asks -> ignored


# =========================================================================
# DASHBOARD — read-only monitoring panel helpers + the single STOP write
# =========================================================================
from src.executor import dashboard as dash


def test_tail_csv_missing_and_empty(tmp_path):
    assert dash.tail_csv(str(tmp_path / "nope.csv")) == []          # missing -> no data
    p = str(tmp_path / "e.csv")
    open(p, "w", encoding="utf-8").write("a,b\n")                    # header only
    assert dash.tail_csv(p) == []


def test_tail_csv_newest_first_and_limit(tmp_path):
    p = str(tmp_path / "d.csv")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("n\n" + "\n".join(str(i) for i in range(10)) + "\n")
    rows = dash.tail_csv(p, n=3)
    assert [r["n"] for r in rows] == ["9", "8", "7"]                 # newest first, last 3
    assert [r["n"] for r in dash.tail_csv(p, n=3, newest_first=False)] == ["7", "8", "9"]


def test_tail_lines_missing_and_tail(tmp_path):
    assert dash.tail_lines(str(tmp_path / "x.log")) == []
    p = str(tmp_path / "e.log")
    open(p, "w", encoding="utf-8").write("l1\nl2\nl3\n")
    assert dash.tail_lines(p, 2) == ["l2", "l3"]


def test_flag_badges_levels_safe_by_default():
    badges = {b["label"]: b for b in dash.flag_badges(_cfg.ExecConfig())}
    assert badges["enabled"]["level"] == "safe" and badges["enabled"]["value"] is False
    assert badges["dry_run"]["level"] == "safe" and badges["dry_run"]["value"] is True
    assert badges["live_enabled"]["level"] == "safe"
    armed = {b["label"]: b for b in dash.flag_badges(
        _cfg.ExecConfig(enabled=True, dry_run=False, live_enabled=True))}
    assert armed["enabled"]["level"] == "danger"
    assert armed["dry_run"]["level"] == "danger"
    assert armed["live_enabled"]["level"] == "warn"


def test_counter_summary_from_ledger(tmp_path):
    led = Ledger(str(tmp_path / "l.csv"))
    today = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    led.append_submit({"submit_utc": today, "kalshi_fill_usd": 30, "poly_fill_usd": 30,
                       "realized_pnl": -45})
    cs = dash.counter_summary(_cfg.ExecConfig(max_daily_loss_usd=50.0), led)
    assert cs["trades"] == 1 and cs["spend_usd"] == 60.0
    assert cs["loss_usd"] == 45.0 and cs["loss_near_cap"] is True and cs["loss_cap_hit"] is False


def test_infer_source_and_summarize_legs():
    assert dash.infer_source({"fingerprint": "live|x"}) == "live-feed"
    assert dash.infer_source({"fingerprint": "abc123"}) == "pre-game"
    legs = json.dumps([{"venue": "kalshi", "outcome": "Home"},
                       {"venue": "polymarket", "outcome": "Away"}])
    assert dash.summarize_legs(legs) == "kalshi:Home, poly:Away"
    assert dash.summarize_legs("") == "" and dash.summarize_legs("not json") == ""


def test_dryrun_view_adds_source_and_bool(tmp_path):
    rows = [{"ts_utc": "t", "fixture": "A vs B", "market": "FTR", "fingerprint": "live|1",
             "n_legs": "3", "intended_size": "10", "net_edge_pct": "2.5", "arb_survived": "True",
             "legs_json": json.dumps([{"venue": "kalshi", "outcome": "H"}])}]
    v = dash.dryrun_view(rows)[0]
    assert v["source"] == "live-feed" and v["arb_survived"] is True and v["legs"] == "kalshi:H"


def test_ledger_view_alert_and_classify():
    assert dash.ledger_row_alert({"status": "leg_failure_unwind"}) is True
    assert dash.ledger_row_alert({"status": "unhedged_halt"}) is True
    assert dash.ledger_row_alert({"status": "filled", "unwind_event": "kalshi_market_sell"}) is True
    assert dash.ledger_row_alert({"status": "filled"}) is False
    assert dash.classify_ledger_row({"status": "unhedged_halt"}) == "halt"
    assert dash.classify_ledger_row({"status": "kalshi_no_fill"}) == "no_fill"
    assert dash.classify_ledger_row({"status": "filled"}) == "filled"
    v = dash.ledger_view([{"status": "unhedged_halt", "fixture": "A", "legs_json": "[]"}])[0]
    assert v["alert"] is True and v["status"] == "unhedged_halt"


def test_filter_event_lines_keeps_decisions_newest_first():
    lines = ["10:00 INFO scan started",
             "10:01 INFO [EXEC] guardrail skip: per-trade cap: $20 > $10",
             "10:02 WARNING [EXEC] cooldown: < 60s since last placement",
             "10:03 INFO unrelated chatter",
             "10:04 WARNING untradable_venue: ['1xbet']"]
    ev = dash.filter_event_lines(lines, only_events=True)
    assert ev[0].endswith("['1xbet']")                              # newest first
    assert all(("guardrail" in l or "cooldown" in l or "untradable" in l) for l in ev)
    assert len(dash.filter_event_lines(lines, only_events=False)) == 5


def test_read_cache_ttl_and_error_capture():
    calls = {"n": 0}
    clock = {"t": 1000.0}

    def producer():
        calls["n"] += 1
        return calls["n"]

    cache = dash.ReadCache(ttl=10.0, clock=lambda: clock["t"])
    assert cache.get("k", producer) == 1
    assert cache.get("k", producer) == 1                            # cached, producer not re-called
    assert calls["n"] == 1
    clock["t"] += 11                                                 # past TTL
    assert cache.get("k", producer) == 2

    def boom():
        raise RuntimeError("no creds")

    val = cache.get("err", boom)
    assert val == {"error": "no creds"}                             # error captured, not raised


def test_stop_toggle_is_the_only_write(tmp_path, monkeypatch):
    stop = str(tmp_path / "STOP")
    monkeypatch.setattr(_cfg, "STOP_FILE", stop)
    assert dash.stop_state() is False
    dash.set_stop("manual via dashboard")                           # THE one write: create
    assert dash.stop_state() is True
    assert "manual via dashboard" in open(stop).read()
    assert dash.clear_stop() is True                                # the one write: remove
    assert dash.stop_state() is False


# =========================================================================
# SELFCHECK — synthetic end-to-end light-up + targeted --clear
# =========================================================================
from src.executor import selfcheck as sc


def test_selfcheck_writes_synthetic_rows(tmp_path):
    dpath, lpath = str(tmp_path / "dryrun.csv"), str(tmp_path / "ledger.csv")
    s = sc.run_selfcheck(cfg=_cfg.ExecConfig(), dryrun_log_path=dpath, ledger_path=lpath, log=_Log())
    # Two dry-run arbs (2-leg + 3-leg) walked the injected books and survived.
    assert s["dryrun_selfcheck_rows"] == 2 and s["all_survived"] is True
    assert s["ledger_selfcheck_rows"] == 2
    rows = list(__import__("csv").DictReader(open(dpath)))
    assert all(r["source"] == "selfcheck" for r in rows)
    assert {r["n_legs"] for r in rows} == {"2", "3"}            # both variants present
    assert all(r["arb_survived"] == "True" for r in rows)
    # Ledger has one filled + one leg-failure-unwind, both rendered for the panel.
    led_rows = list(__import__("csv").DictReader(open(lpath)))
    statuses = {r["status"] for r in led_rows}
    assert "filled" in statuses and "leg_failure_unwind" in statuses
    assert any(r["unwind_event"] == "kalshi_market_sell" for r in led_rows)
    # Master flags untouched by the self-check.
    assert s["flags"] == {"enabled": False, "dry_run": True, "live_enabled": False}


def test_selfcheck_clear_removes_only_selfcheck_rows(tmp_path):
    dpath, lpath = str(tmp_path / "dryrun.csv"), str(tmp_path / "ledger.csv")
    cfg = _cfg.ExecConfig()
    # Seed a REAL (non-selfcheck) dry-run row + a real ledger row that must SURVIVE the clear.
    md = _FakeMarketData(k_ladder=[(0.47, 300)], p_ladder=[(0.48, 300)])
    exec_engine.execute_arb(_clean_arb_with_venue(), live=False, cfg=cfg, market_data=md,
                            dryrun_log_path=dpath, log=_Log())
    Ledger(lpath).append_submit({"fingerprint": "real-1", "fixture": "Real", "status": "filled"})

    sc.run_selfcheck(cfg=cfg, dryrun_log_path=dpath, ledger_path=lpath, log=_Log())
    dry_before = list(__import__("csv").DictReader(open(dpath)))
    led_before = list(__import__("csv").DictReader(open(lpath)))
    assert len(dry_before) == 3 and len(led_before) == 3      # 1 real + 2 selfcheck each

    counts = sc.clear_selfcheck(dryrun_log_path=dpath, ledger_path=lpath)
    assert counts == {"dryrun_removed": 2, "ledger_removed": 2}
    dry_after = list(__import__("csv").DictReader(open(dpath)))
    led_after = list(__import__("csv").DictReader(open(lpath)))
    # Exactly the real rows remain; no selfcheck rows survive.
    assert len(dry_after) == 1 and dry_after[0]["source"] != "selfcheck"
    assert len(led_after) == 1 and led_after[0]["fingerprint"] == "real-1"


def test_selfcheck_clear_safe_when_files_missing(tmp_path):
    counts = sc.clear_selfcheck(dryrun_log_path=str(tmp_path / "none.csv"),
                                ledger_path=str(tmp_path / "none2.csv"))
    assert counts == {"dryrun_removed": 0, "ledger_removed": 0}


def test_selfcheck_rows_render_in_panel_views(tmp_path):
    dpath, lpath = str(tmp_path / "dryrun.csv"), str(tmp_path / "ledger.csv")
    sc.run_selfcheck(cfg=_cfg.ExecConfig(), dryrun_log_path=dpath, ledger_path=lpath, log=_Log())
    arbs = dash.dryrun_view(dash.tail_csv(dpath, 50))
    assert arbs and all(a["source"] == "selfcheck" for a in arbs)   # source column surfaces
    led = dash.ledger_view(dash.tail_csv(lpath, 50))
    assert any(r["alert"] for r in led)                              # the unwind row is flagged
