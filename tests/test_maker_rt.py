"""Tests for src/genz/maker_rt/ — the realtime maker/hedger.

Covers the PURE, testable core: floor + never-crossable quote math (both rest directions), the
queue-model shadow fills (traded-through / queue-consumed / partial print / reprice resets queue),
Kalshi seq-gap resync, parsing fixtures for all three sockets against REAL captured message shapes,
tick_size_change, the live gate refusing without an arm file, the hedge-fail unwind with fakes, and
the git-HEAD self-exit. No socket, no order, ever placed.
"""
from __future__ import annotations

import json
import os

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt import gitguard, hedge, live, orders, parsing, quotes
from src.genz.maker_rt.books import SideView
from src.genz.maker_rt.fills import ShadowFillModel
from src.genz.maker_rt.store import BookStore


def _cfg():
    return mrt_config.MakerRtConfig()


# --------------------------------------------------------------------------- #
# QUOTE MATH — floor + never-crossable, both directions                          #
# --------------------------------------------------------------------------- #
def test_hedge_taker_fee_both_venues():
    assert abs(quotes.hedge_taker_fee("kalshi", 0.5) - 0.0175) < 1e-9         # 0.07*.5*.5
    assert abs(quotes.hedge_taker_fee("polymarket", 0.4, 0.05) - 0.05 * 0.4) < 1e-9
    assert quotes.hedge_taker_fee("other", 0.5) == 0.0


def test_floor_and_quote_rest_poly_hedge_kalshi():
    """Rest on Polymarket, hedge by lifting Kalshi: floor uses the Kalshi taker fee."""
    hedge_ask = 0.50
    floor = quotes.compute_floor(hedge_ask, "kalshi", target_net=0.010)
    assert abs(floor - (1 - 0.50 - 0.0175 - 0.010)) < 1e-9                    # 0.4725
    rest = SideView(best_bid=0.45, best_ask=0.55, bid_sizes={0.45: 300}, ask_ladder=[(0.55, 100)])
    hedgev = SideView(best_bid=0.49, best_ask=0.50, bid_sizes={}, ask_ladder=[(0.50, 500), (0.51, 200)])
    d = quotes.compute_quote(rest, hedgev, hedge_venue="kalshi", tick=0.01, target_net=0.010,
                             quote_usd=10.0)
    assert d.viable and d.reason == "ok"
    assert d.quote_price == 0.46 and d.at_best is True                        # join/improve: bid+tick, <= floor
    assert abs(d.net_at_quote - (1 - 0.46 - 0.50 - 0.0175)) < 1e-9


def test_floor_and_quote_rest_kalshi_hedge_poly():
    """Rest on Kalshi, hedge by lifting Polymarket: floor uses the Polymarket taker fee."""
    hedge_ask = 0.50
    floor = quotes.compute_floor(hedge_ask, "polymarket", target_net=0.010, poly_rate=0.05)
    assert abs(floor - (1 - 0.50 - 0.05 * 0.50 - 0.010)) < 1e-9               # 0.465
    rest = SideView(best_bid=0.44, best_ask=0.55, bid_sizes={0.44: 100}, ask_ladder=[(0.55, 100)])
    hedgev = SideView(best_bid=0.49, best_ask=0.50, bid_sizes={}, ask_ladder=[(0.50, 500)])
    d = quotes.compute_quote(rest, hedgev, hedge_venue="polymarket", tick=0.01, target_net=0.010,
                             quote_usd=10.0, poly_rate=0.05)
    assert d.viable and d.quote_price == 0.45                                 # min(floor 0.465, bid+tick 0.45)


def test_quote_never_crossable_skips():
    """A quote that would sit at/above (rest_ask − tick) is crossable -> refused (never a taker)."""
    rest = SideView(best_bid=0.45, best_ask=0.46, bid_sizes={0.45: 100}, ask_ladder=[(0.46, 100)])
    hedgev = SideView(best_bid=0.49, best_ask=0.50, bid_sizes={}, ask_ladder=[(0.50, 500)])
    d = quotes.compute_quote(rest, hedgev, hedge_venue="kalshi", tick=0.01, target_net=0.001,
                             quote_usd=10.0)
    assert not d.viable and d.reason == "would_cross"                         # 0.46 > 0.46 - 0.01


def test_quote_behind_best_counted_not_filled():
    """A floor below the best bid -> would sit BEHIND best: counted (would_be_behind) but not viable."""
    rest = SideView(best_bid=0.60, best_ask=0.70, bid_sizes={0.60: 100}, ask_ladder=[(0.70, 100)])
    hedgev = SideView(best_bid=0.49, best_ask=0.50, bid_sizes={}, ask_ladder=[(0.50, 500)])
    d = quotes.compute_quote(rest, hedgev, hedge_venue="kalshi", tick=0.01, target_net=0.010,
                             quote_usd=10.0)
    assert not d.viable and d.would_be_behind and d.quote_price < 0.60


def test_quote_hedge_precondition_thin_book():
    """No quote when the hedge book can't cover the quote size within one tick of the ask."""
    rest = SideView(best_bid=0.45, best_ask=0.55, bid_sizes={0.45: 100}, ask_ladder=[(0.55, 100)])
    thin = SideView(best_bid=0.49, best_ask=0.50, bid_sizes={}, ask_ladder=[(0.50, 3), (0.60, 9999)])
    d = quotes.compute_quote(thin_or_rest(rest), thin, hedge_venue="kalshi", tick=0.01,
                             target_net=0.010, quote_usd=100.0)   # ~200 shares needed, only 3 near ask
    assert not d.viable and d.reason == "hedge_too_thin"


def thin_or_rest(r):
    return r


def test_needs_reprice_on_tick_move():
    a = quotes.QuoteDecision(True, "ok", quote_price=0.46, floor=0.47)
    same = quotes.QuoteDecision(True, "ok", quote_price=0.46, floor=0.471)
    moved = quotes.QuoteDecision(True, "ok", quote_price=0.45, floor=0.46)
    assert quotes.needs_reprice(a, same, 0.01) is False
    assert quotes.needs_reprice(a, moved, 0.01) is True


# --------------------------------------------------------------------------- #
# SHADOW FILLS — queue model                                                     #
# --------------------------------------------------------------------------- #
REF = ("polymarket", "tok", "BUY")


def test_fill_traded_through():
    m = ShadowFillModel()
    m.arm(("g", "mk", "over", "rest-poly"), REF, 0.46, 50, queue_ahead=100, at_best=True, ts=0.0)
    fills = m.consume_print(REF, 0.45, 10, ts=1.0)               # a print BELOW our price
    assert len(fills) == 1 and fills[0].trigger == "traded_through"
    assert fills[0].quote_age_s == 1.0 and not m.open_keys()


def test_fill_queue_consumed_after_queue_plus_size():
    m = ShadowFillModel()
    key = ("g", "mk", "over", "rest-poly")
    m.arm(key, REF, 0.46, 50, queue_ahead=100, at_best=True, ts=0.0)
    assert m.consume_print(REF, 0.46, 80, ts=1.0) == []          # 80 < 100 (still in queue) -> no fill
    fills = m.consume_print(REF, 0.46, 80, ts=2.0)               # cum 160 >= 100+50 -> FILL
    assert len(fills) == 1 and fills[0].trigger == "queue_consumed"
    assert not m.open_keys()                                     # filled + removed


def test_fill_partial_print_then_completes():
    m = ShadowFillModel()
    key = ("g", "mk", "over", "rest-poly")
    m.arm(key, REF, 0.46, 50, queue_ahead=100, at_best=True, ts=0.0)
    assert m.consume_print(REF, 0.46, 120, ts=1.0) == []         # partial: cum 120 < 150
    fills = m.consume_print(REF, 0.46, 40, ts=2.0)               # cum 160 >= 150 -> FILL
    assert len(fills) == 1 and fills[0].trigger == "queue_consumed"


def test_reprice_resets_queue():
    m = ShadowFillModel()
    key = ("g", "mk", "over", "rest-poly")
    m.arm(key, REF, 0.46, 50, queue_ahead=100, at_best=True, ts=0.0)
    m.consume_print(REF, 0.46, 90, ts=1.0)                       # cum 90 (< 150), no fill
    m.arm(key, REF, 0.45, 50, queue_ahead=200, at_best=True, ts=2.0)   # REPRICE to a new price
    assert m.quotes[key].cumulative_at_price == 0.0             # queue progress reset
    assert m.consume_print(REF, 0.45, 100, ts=3.0) == []        # 100 < 250 -> still no fill (reset held)


def test_same_price_rearm_does_not_double_count_trades():
    """REGRESSION: a same-price re-arm (a >=1-tick floor move that rounds to the same quote) must NOT
    reset queue_ahead to the currently-displayed size — the prints that shrank the display are already
    in cumulative_at_price, so doing so double-counts them and fills ~2x early (breaks the lower bound).
    Here: arm(q=100, size=50); a 60 print (no fill); re-arm same price with displayed=40; a 30 print
    (cum 90) MUST NOT fill; a further 60 (cum 150) fills."""
    m = ShadowFillModel()
    key = ("g", "mk", "over", "rest-poly")
    m.arm(key, REF, 0.46, 50, queue_ahead=100, at_best=True, ts=0.0)
    assert m.consume_print(REF, 0.46, 60, ts=1.0) == []          # cum 60 < 150
    m.arm(key, REF, 0.46, 50, queue_ahead=40, at_best=True, ts=2.0)   # RE-ARM same price, display shrank
    assert m.quotes[key].queue_ahead == 100                      # original queue preserved (not 40)
    assert m.consume_print(REF, 0.46, 30, ts=3.0) == []          # cum 90 < 150 -> NO fill (old code fills)
    fills = m.consume_print(REF, 0.46, 60, ts=4.0)               # cum 150 >= 100+50 -> fill
    assert len(fills) == 1 and fills[0].trigger == "queue_consumed"


def test_same_price_rearm_credits_cancels_ahead():
    """A same-price re-arm where the display shrank with NO prints = cancels ahead of us -> credit them
    once (queue_ahead drops), so we fill sooner. arm(q=100, size=50); no prints; re-arm displayed=30
    (70 canceled ahead) -> queue_ahead=30; an 80 print (30 ahead + our 50) fills."""
    m = ShadowFillModel()
    key = ("g", "mk", "over", "rest-poly")
    m.arm(key, REF, 0.46, 50, queue_ahead=100, at_best=True, ts=0.0)
    m.arm(key, REF, 0.46, 50, queue_ahead=30, at_best=True, ts=1.0)   # 70 canceled ahead (no prints)
    assert m.quotes[key].queue_ahead == 30                       # cancels credited
    fills = m.consume_print(REF, 0.46, 80, ts=2.0)               # cum 80 >= 30+50 -> fill
    assert len(fills) == 1 and fills[0].trigger == "queue_consumed"


def test_print_only_fills_same_book():
    m = ShadowFillModel()
    m.arm(("g", "mk", "over", "rest-poly"), REF, 0.46, 50, queue_ahead=0, at_best=True, ts=0.0)
    assert m.consume_print(("kalshi", "T", "yes"), 0.30, 999, ts=1.0) == []   # different book -> nothing


# --------------------------------------------------------------------------- #
# PARSING — real captured message shapes for all three sockets                   #
# --------------------------------------------------------------------------- #
def test_parse_poly_market_book_and_trade():
    book = {"event_type": "book", "asset_id": "tok1", "market": "0xcond",
            "bids": [{"price": "0.47", "size": "100"}, {"price": "0.46", "size": "50"}],
            "asks": [{"price": "0.52", "size": "200"}], "tick_size": "0.01", "hash": "h",
            "timestamp": "1784227013148"}
    evs = parsing.parse_poly_market([book])
    assert evs[0]["kind"] == "poly_book" and evs[0]["token"] == "tok1"
    assert evs[0]["bids"] == {0.47: 100.0, 0.46: 50.0} and evs[0]["asks"] == {0.52: 200.0}
    trade = {"event_type": "last_trade_price", "asset_id": "tok1", "price": "0.48",
             "side": "BUY", "size": "12", "timestamp": "1784227013148"}
    tev = parsing.parse_poly_market(trade)
    assert tev[0]["kind"] == "poly_trade" and tev[0]["price"] == 0.48 and tev[0]["size"] == 12.0


def test_parse_poly_price_change_and_tick_size_change():
    pc = {"event_type": "price_change", "asset_id": "tok1",
          "changes": [{"price": "0.47", "side": "BUY", "size": "150"},
                      {"price": "0.52", "side": "SELL", "size": "0"}], "timestamp": "t"}
    evs = parsing.parse_poly_market(pc)
    assert evs[0]["kind"] == "poly_price"
    assert (0.47, "BUY", 150.0) in evs[0]["changes"] and (0.52, "SELL", 0.0) in evs[0]["changes"]
    tk = {"event_type": "tick_size_change", "asset_id": "tok1", "old_tick_size": "0.01",
          "new_tick_size": "0.001", "timestamp": "t"}
    tev = parsing.parse_poly_market(tk)
    assert tev[0]["kind"] == "poly_tick" and tev[0]["tick"] == 0.001


def test_tick_size_change_updates_store():
    bs = BookStore()
    assert bs.poly_tick("tok1") == 0.01
    bs.apply_poly(parsing.parse_poly_market(
        {"event_type": "tick_size_change", "asset_id": "tok1", "new_tick_size": "0.001"}))
    assert bs.poly_tick("tok1") == 0.001


def test_parse_poly_user_trade_is_our_fill():
    ev = {"event_type": "trade", "asset_id": "tok1", "market": "0xcond", "side": "BUY",
          "size": "50", "price": "0.46", "status": "MATCHED", "taker_order_id": "OID", "timestamp": "t"}
    out = parsing.parse_poly_user(ev)
    assert out[0]["kind"] == "poly_user_trade" and out[0]["order_id"] == "OID"
    assert out[0]["size"] == 50.0 and out[0]["price"] == 0.46
    order = {"event_type": "order", "asset_id": "tok1", "id": "OID", "type": "PLACEMENT",
             "side": "BUY", "price": "0.46", "size_matched": "0", "status": "LIVE"}
    oo = parsing.parse_poly_user(order)
    assert oo[0]["kind"] == "poly_user_order" and oo[0]["type"] == "PLACEMENT"


def test_parse_kalshi_real_shapes():
    """Real captured shapes: yes_dollars_fp / no_dollars_fp (dollar strings), delta price_dollars/
    delta_fp, per-sid seq."""
    snap = {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {"market_ticker": "KX-T", "market_id": "id",
                    "yes_dollars_fp": [["0.4700", "100.00"], ["0.4600", "50.00"]],
                    "no_dollars_fp": [["0.5000", "200.00"]]}}
    ev = parsing.parse_kalshi(snap)[0]
    assert ev["kind"] == "kalshi_snapshot" and (47, 100.0) in ev["yes"] and (50, 200.0) in ev["no"]
    assert ev["seq"] == 1 and ev["sid"] == 1
    delta = {"type": "orderbook_delta", "sid": 1, "seq": 2,
             "msg": {"market_ticker": "KX-T", "price_dollars": "0.4700", "delta_fp": "-40.00", "side": "yes"}}
    dv = parsing.parse_kalshi(delta)[0]
    assert dv["kind"] == "kalshi_delta" and dv["price"] == 47 and dv["delta"] == -40.0 and dv["side"] == "yes"
    trade = {"type": "trade", "sid": 2, "seq": 1,
             "msg": {"market_ticker": "KX-T", "yes_price": "0.47", "no_price": "0.53", "count": "25"}}
    tv = parsing.parse_kalshi(trade)[0]
    assert tv["kind"] == "kalshi_trade" and tv["yes_price"] == 0.47 and tv["no_price"] == 0.53 and tv["count"] == 25.0
    assert parsing.parse_kalshi({"type": "error", "msg": {"code": 1}})[0]["kind"] == "kalshi_error"


def test_kalshi_book_view_geometry():
    bs = BookStore()
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-T", "yes_dollars_fp": [["0.4700", "100"], ["0.4600", "50"]],
                "no_dollars_fp": [["0.5000", "200"], ["0.4900", "80"]]}}))
    yv = bs.kalshi_view("KX-T", "yes")
    assert yv.best_bid == 0.47 and yv.best_ask == 0.50          # ask = complement of best NO bid
    assert yv.ask_ladder[0] == (0.50, 200.0)
    nv = bs.kalshi_view("KX-T", "no")
    assert nv.best_bid == 0.50 and nv.best_ask == 0.53          # 100 - 47 = 53c


# --------------------------------------------------------------------------- #
# SEQ-GAP resync                                                                 #
# --------------------------------------------------------------------------- #
def test_seq_gap_helper():
    assert parsing.seq_gap(None, 5) is False        # snapshot / first
    assert parsing.seq_gap(5, 6) is False           # in-order
    assert parsing.seq_gap(5, 8) is True            # gap
    assert parsing.seq_gap(5, None) is False


def test_seq_gap_drops_all_books_and_flags_resync():
    bs = BookStore()
    for tk in ("KX-A", "KX-B"):
        bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {"market_ticker": tk, "yes_dollars_fp": [["0.47", "100"]], "no_dollars_fp": [["0.50", "100"]]}}))
    # a delta whose per-sid seq JUMPS (missed messages) -> every book dropped + full resubscribe flagged
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_delta", "sid": 1, "seq": 9,
        "msg": {"market_ticker": "KX-A", "price_dollars": "0.47", "delta_fp": "1", "side": "yes"}}))
    assert bs.need_resync() is True and len(bs.kalshi) == 0
    bs.clear_resync()
    assert bs.need_resync() is False


# --------------------------------------------------------------------------- #
# LIVE GATE — refuse without enable / arm file / self-check                       #
# --------------------------------------------------------------------------- #
class _OkClient:
    def get_balance(self):
        return {"balance": 1000}

    def can_place_polymarket_orders(self):
        return (True, "ok")


class _BadClient:
    def get_balance(self):
        raise RuntimeError("no balance")


def test_live_gate_refuses_when_disabled():
    cfg = _cfg()                                     # live.enabled default False
    g = live.LiveGate(cfg).evaluate()
    assert g.armed is False and "false" in g.reason


def test_live_gate_refuses_without_arm_file(tmp_path):
    cfg = _cfg()
    cfg.live.enabled = True
    cfg.live.arm_file = str(tmp_path / "ARM_MAKER")   # absent
    g = live.LiveGate(cfg, kalshi_client=_OkClient(), poly_client=_OkClient()).evaluate()
    assert g.armed is False and "arm file missing" in g.reason


def test_live_gate_refuses_when_selfcheck_fails(tmp_path):
    arm = tmp_path / "ARM_MAKER"
    arm.write_text("1")
    cfg = _cfg(); cfg.live.enabled = True; cfg.live.arm_file = str(arm)
    g = live.LiveGate(cfg, kalshi_client=_BadClient(), poly_client=_OkClient()).evaluate()
    assert g.armed is False and "self-check failed" in g.reason


def test_live_gate_arms_when_all_pass(tmp_path):
    arm = tmp_path / "ARM_MAKER"
    arm.write_text("1")
    cfg = _cfg(); cfg.live.enabled = True; cfg.live.arm_file = str(arm)
    g = live.LiveGate(cfg, kalshi_client=_OkClient(), poly_client=_OkClient()).evaluate()
    assert g.armed is True and g.reason == "armed"
    assert g.checks.get("kalshi_balance") and g.checks.get("poly_balance")


# --------------------------------------------------------------------------- #
# HEDGE — shadow marking + live hedge-fail unwind                                #
# --------------------------------------------------------------------------- #
def test_mark_hedge_and_locked_net():
    m = hedge.mark_hedge([(0.50, 100), (0.51, 100)], 50, "kalshi", 0.05)
    assert m and abs(m["avg_price"] - 0.50) < 1e-9
    assert m["fee"] > 0 and m["cost_per_share"] > 0.50
    net = hedge.locked_net(0.46, m["cost_per_share"])
    assert net < 1 - 0.46 - 0.50                     # fee makes it worse than the pre-fee sum


class _KalshiFullFill:
    def place_order(self, ticker, side, count, price, **kw):
        return {"status": "filled", "fill_count": count, "avg_price": price}


class _KalshiMiss:
    def place_order(self, ticker, side, count, price, **kw):
        return {"status": "none", "fill_count": 0, "avg_price": None}


class _PolyUnwind:
    def __init__(self):
        self.sold = []

    def place_market_sell(self, token, shares, **kw):
        self.sold.append((token, shares))
        return {"status": "filled", "shares": shares, "avg_price": 0.44}


def test_live_hedge_locks_on_full_fill():
    h = hedge.LiveHedger(kalshi_client=_KalshiFullFill(), poly_client=_PolyUnwind())
    r = h.hedge({"token_id": "tok", "side": "BUY", "price": 0.46, "size": 50},
                {"ticker": "KX-T", "side": "no", "best_ask": 0.50})
    assert r.status == "locked" and r.hedged_shares == 50 and r.locked_pnl is not None
    assert r.freeze_market is False


def test_live_hedge_miss_reports_shortfall_and_does_not_unwind():
    """On a Kalshi miss the hedger reports status 'missed' + freeze — it NO LONGER unwinds itself (the
    executor owns the ONE verified unwind; the in-hedger unwind logged fake success off avg_price)."""
    poly = _PolyUnwind()
    h = hedge.LiveHedger(kalshi_client=_KalshiMiss(), poly_client=poly)
    r = h.hedge({"token_id": "tok", "side": "BUY", "price": 0.46, "size": 50},
                {"ticker": "KX-T", "side": "no", "best_ask": 0.50})
    assert r.status == "missed" and r.hedged_shares == 0 and r.freeze_market is True
    assert poly.sold == []                           # the hedger does NOT sell -- the executor verifies + unwinds


# --------------------------------------------------------------------------- #
# ORDER CLIENTS (locked) — pure sizing helpers                                    #
# --------------------------------------------------------------------------- #
def test_poly_order_clamp_size():
    assert orders.PolyOrderClient.clamp_size(3, 0.5) == 5.0        # min 5 shares
    assert orders.PolyOrderClient.clamp_size(100, 0.5) == 100.0
    assert orders.PolyOrderClient.clamp_size(5, 0.01) == 100.0     # >= $1 notional at 1c => 100 shares


def test_kalshi_marketable_limit():
    kc = orders.KalshiOrderClient(exec_client=None, buffer=0.01)
    assert abs(kc.marketable_limit(0.50) - 0.51) < 1e-9
    assert kc.marketable_limit(0.995) == 0.99                      # clamped


# --------------------------------------------------------------------------- #
# GIT-HEAD self-exit guard                                                        #
# --------------------------------------------------------------------------- #
def test_head_changed():
    assert gitguard.head_changed("aaa", "bbb") is True
    assert gitguard.head_changed("aaa", "aaa") is False
    assert gitguard.head_changed(None, "bbb") is False            # unknown -> never force restart
    assert gitguard.head_changed("aaa", None) is False


def test_read_head_sha_ref_and_detached(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("deadbeef" * 5 + "\n")
    assert gitguard.read_head_sha(str(tmp_path)) == "deadbeef" * 5
    # packed-refs fallback (loose ref removed)
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack\nabc1234def5678 refs/heads/main\n")
    assert gitguard.read_head_sha(str(tmp_path)) == "abc1234def5678"
    # detached HEAD
    (git / "HEAD").write_text("cafebabe" * 5 + "\n")
    assert gitguard.read_head_sha(str(tmp_path)) == "cafebabe" * 5


# --------------------------------------------------------------------------- #
# CONFIG + STATE + UNIVERSE                                                       #
# --------------------------------------------------------------------------- #
def test_config_defaults_and_live_locked(tmp_path):
    # CONFIG-PIN BAN: build the config from an EMPTY yaml, NEVER the live config.yaml (whose tunable
    # values — max_games, caps, horizons — drift and must not be pinned in a test). Assert the DATACLASS
    # DEFAULTS instead. Any test asserting a specific config value must load from an empty/fixture file.
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n")
    cfg = mrt_config.load_maker_rt_config(config_path=str(empty))
    assert cfg.max_games == 20 and cfg.quote_usd == 100.0 and cfg.target_net == 0.010
    assert cfg.live.enabled is False                              # ships LOCKED
    # PILOT caps (dataclass defaults): rest-leg $5, daily-stake $100, 10 fills/day, 2 open, $25 loss.
    assert cfg.live.quote_usd_max == 5.0 and cfg.live.max_open_quotes == 2
    assert cfg.live.max_daily_stake_usd == 100.0 and cfg.live.max_fills_per_day == 10
    assert cfg.live.max_daily_loss_usd == 25.0
    # in-play rail defaults come from the dataclass, not the live file
    assert cfg.inplay.fresh_s == 10.0 and cfg.inplay.shock_move == 0.05 and cfg.inplay.persist_ms == 1500
    assert cfg.poly_leg_cap == {"tennis": 0.65, "ufc": 0.65}


def test_state_summary_and_heartbeat(tmp_path):
    from datetime import datetime, timezone
    from src.genz.maker_rt.state import MakerState
    st = MakerState()
    now = datetime(2026, 7, 16, 18, 0, 0, tzinfo=timezone.utc)
    st.record({"event": "quote", "at_best": True}, now)
    st.record({"event": "behind"}, now)
    st.record({"event": "fill", "sport": "mlb", "game": "G1", "market_key": "ml2",
               "locked_net": 1.5, "locked_pnl": 0.75}, now)
    summ = st.summary("shadow", {"poly_market": True, "kalshi": True, "poly_user": False}, now)
    assert summ["quotes"] == 1 and summ["fills"] == 1 and summ["behind_best"] == 1
    assert summ["at_best_share"] == 1.0 and summ["median_net_at_fill"] == 1.5
    assert summ["mode"] == "shadow" and summ["schema"] == 3      # schema 3: rails-gated achv + restarts + gates
    assert "by_sport" in summ and "by_phase" in summ
    assert "restarts_today" in summ and "gates" in summ
    hb = st.heartbeat("shadow", {"poly_market": True}, 3, now)
    assert hb["open_quotes"] == 3 and hb["fills_today"] == 1


def test_build_universe_filters_settlement_and_pregame():
    from src.genz.maker_rt.universe import build_universe, kalshi_tickers, poly_tokens
    node = lambda side, tok, kt, risk=None: {   # noqa: E731
        "market_type": "total_runs", "market_key": "total_runs|8.5", "side": side, "line": 8.5,
        "kind": "2way", "poly_token_id": tok, "poly_side": side.title(), "poly_fee_rate": 0.05,
        "kalshi_ticker": kt, "kalshi_side": "YES" if side == "over" else "NO",
        **({"settlement_risk": risk} if risk else {})}
    future = "2027-01-01T00:00:00Z"
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": future, "nodes": [
        node("over", "t_o", "KX-1"), node("under", "t_u", "KX-1")]},
        "G2": {"away": "C", "home": "D", "kickoff_utc": future, "nodes": [
            node("over", "r_o", "KX-2", risk="mlb_rain_rule"), node("under", "r_u", "KX-2", risk="mlb_rain_rule")]}}}
    uni = build_universe({"mlb": tree}, now_ts=0.0, max_games=20, expire_before_kickoff_s=120)
    assert len(uni) == 1 and uni[0].game == "G1"                 # rain-risk G2 excluded
    assert set(poly_tokens(uni)) == {"t_o", "t_u"} and kalshi_tickers(uni) == ["KX-1"]


# --------------------------------------------------------------------------- #
# DRIVER end-to-end (shadow): universe -> quote -> print fill -> hedge mark -> drift #
# --------------------------------------------------------------------------- #
class _RecState:
    def __init__(self):
        self.rows = []
        self.achv = []                                   # (sport, phase, value) achievable accumulations

    def record(self, row, now):
        self.rows.append(row)

    def record_achievable(self, sport, phase, value, now, rails_ok=True):
        self.achv.append((sport, phase, value))


def test_driver_quotes_and_shadow_fills_end_to_end():
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.universe import build_universe
    future = "2027-01-01T00:00:00Z"
    n = lambda side, tok, kt, ks: {"market_type": "ml2", "market_key": "ml2", "side": side,  # noqa: E731
        "line": None, "kind": "2way", "poly_token_id": tok, "poly_side": side.title(),
        "poly_fee_rate": 0.05, "kalshi_ticker": kt, "kalshi_side": ks}
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": future, "nodes": [
        n("away", "TOK_A", "KX-1", "YES"), n("home", "TOK_B", "KX-1", "NO")]}}}
    uni = build_universe({"mlb": tree}, now_ts=0.0, max_games=20, expire_before_kickoff_s=120)
    st = _RecState()
    drv = QuoteDriver(_cfg(), st)
    drv.set_universe(uni)
    # Books: rest on Poly TOK_A (bid 0.45), hedge = lift Kalshi NO complement (best ask 0.50 deep).
    bs = BookStore()
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "TOK_A",
        "bids": [{"price": "0.45", "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}))
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-1",
                "yes_dollars_fp": [["0.5000", "5000"]],       # -> NO ask = 100-50 = 0.50 (hedge lifts NO)
                "no_dollars_fp": [["0.4500", "5000"]]}}))     # -> YES ask = 0.55; NO best bid = 0.45
    now = datetime(2026, 7, 16, 18, 0, 0, tzinfo=timezone.utc)
    drv.refresh_quotes(bs, now, now_ts=100.0)
    quote_rows = [r for r in st.rows if r["event"] == "quote"]
    assert any(r["direction"] == "rest-poly" and r["quote_price"] == 0.46 for r in quote_rows)
    # A public print BELOW our 0.46 quote on TOK_A -> shadow fill + hedge mark + locked net recorded.
    drv.consume_prints([(("polymarket", "TOK_A", "BUY"), 0.45, 10.0)], bs, now, now_ts=101.0)
    fill_rows = [r for r in st.rows if r["event"] == "fill"]
    assert len(fill_rows) == 1 and fill_rows[0]["trigger"] == "traded_through"
    assert fill_rows[0]["locked_net"] is not None and fill_rows[0]["hedge_avg"] == 0.5
    # Drift marks fire after the max window -> a PAIRED fill_drift event (linked by fill_ts + identity).
    drv.process_drift(bs, now, now_ts=101.0 + max(drv.cfg.drift_marks_s) + 1)
    fd = [r for r in st.rows if r["event"] == "fill_drift"]
    assert len(fd) == 1 and fd[0]["fill_ts"] and fd[0]["drift_30"] is not None   # baseline captured -> real number


def test_driver_behind_best_is_deduped_not_per_tick():
    """An efficient book leaves every candidate BEHIND best; the driver must record 'behind' ONCE on
    the transition, not once per 250ms refresh tick (which would flood the CSV)."""
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.universe import build_universe
    future = "2027-01-01T00:00:00Z"
    n = lambda side, tok, kt, ks: {"market_type": "ml2", "market_key": "ml2", "side": side,  # noqa: E731
        "line": None, "kind": "2way", "poly_token_id": tok, "poly_side": side.title(),
        "poly_fee_rate": 0.05, "kalshi_ticker": kt, "kalshi_side": ks}
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": future, "nodes": [
        n("away", "TOK_A", "KX-1", "YES"), n("home", "TOK_B", "KX-1", "NO")]}}}
    uni = build_universe({"mlb": tree}, now_ts=0.0, max_games=20, expire_before_kickoff_s=120)
    st = _RecState()
    drv = QuoteDriver(_cfg(), st)
    drv.set_universe(uni)
    bs = BookStore()
    # An efficient book: poly away bid is HIGH (0.60) so the hedge-limited floor sits behind it.
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "TOK_A",
        "bids": [{"price": "0.60", "size": "300"}], "asks": [{"price": "0.62", "size": "300"}]}))
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "5000"]],
                "no_dollars_fp": [["0.4500", "5000"]]}}))
    now = datetime(2026, 7, 16, 18, 0, 0, tzinfo=timezone.utc)
    for i in range(5):                                   # five refresh ticks, books unchanged
        drv.refresh_quotes(bs, now, now_ts=100.0 + i * 0.25)
    behind = [r for r in st.rows if r["event"] == "behind" and r["direction"] == "rest-poly"]
    assert len(behind) == 1                              # deduped to the single transition


# --------------------------------------------------------------------------- #
# TENNIS coverage: universe admission, the poly-leg cap, start_utc re-anchor      #
# --------------------------------------------------------------------------- #
def _tennis_tree(kickoff, note="walkover_50_50", risk=None):
    # Real tennis nodes: each player has their OWN Kalshi YES market (kalshi_side='YES' for both).
    n = lambda side, tok, kt: {   # noqa: E731
        "market_type": "match_winner", "market_key": "match_winner", "side": side, "line": None,
        "kind": "2way", "poly_token_id": tok, "poly_side": side.title(), "poly_fee_rate": 0.05,
        "kalshi_ticker": kt, "kalshi_side": "YES",
        **({"settlement_note": note} if note else {}), **({"settlement_risk": risk} if risk else {})}
    return {"games": {"KXATPMATCH-X": {"away": "Dzumhur", "home": "Molcan", "kickoff_utc": kickoff,
            "sport": "tennis", "nodes": [n("dzumhur", "T_D", "KXATPMATCH-X-DZU"),
                                         n("molcan", "T_M", "KXATPMATCH-X-MOL")]}}}


def test_tennis_poly_leg_cap_helper_skips_high_and_admits_low():
    from src.genz.maker_rt.quotes import poly_leg_exceeds_cap
    cap = 0.65
    assert poly_leg_exceeds_cap("polymarket", 0.70, None, cap) is True       # rest-poly leg 0.70 -> skip
    assert poly_leg_exceeds_cap("kalshi", 0.48, 0.70, cap) is True            # hedge-poly ask 0.70 -> skip
    assert poly_leg_exceeds_cap("polymarket", 0.60, None, cap) is False       # 0.60 admitted
    assert poly_leg_exceeds_cap("kalshi", 0.48, 0.60, cap) is False
    assert poly_leg_exceeds_cap("polymarket", 0.99, None, None) is False      # non-tennis (no cap)


def test_tennis_walkover_note_nodes_enter_universe():
    from src.genz.maker_rt.universe import build_universe, poly_tokens
    uni = build_universe({"tennis": _tennis_tree("2027-01-01T00:00:00Z")}, now_ts=0.0,
                         max_games=20, expire_before_kickoff_s=120)
    assert len(uni) == 1 and uni[0].sport == "tennis"                         # walkover_50_50 note ADMITTED
    assert set(poly_tokens(uni)) == {"T_D", "T_M"}
    # a settlement_risk tennis node is EXCLUDED (conservative), same as MLB rain-risk.
    risk = build_universe({"tennis": _tennis_tree("2027-01-01T00:00:00Z", note=None,
                          risk="tennis_unparsed_settlement")}, now_ts=0.0, max_games=20,
                          expire_before_kickoff_s=120)
    assert risk == []


def _fav_books():
    """A Dzumhur-FAVORITE book where rest-poly on Dzumhur would quote at-best ~0.67: poly T_D bid 0.66,
    and the Molcan hedge (Kalshi YES) asks 0.30 so the floor allows ~0.67."""
    bs = BookStore()
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "T_D",
        "bids": [{"price": "0.66", "size": "300"}], "asks": [{"price": "0.68", "size": "300"}]}))
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "T_M",
        "bids": [{"price": "0.30", "size": "300"}], "asks": [{"price": "0.34", "size": "300"}]}))
    # Molcan Kalshi YES ask = 1 - NO_bid = 1 - 0.70 = 0.30 (deep) -> Dzumhur-poly hedge lifts it cheaply.
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KXATPMATCH-X-MOL", "yes_dollars_fp": [["0.3000", "5000"]],
                "no_dollars_fp": [["0.7000", "5000"]]}}))
    # Dzumhur Kalshi YES ask = 1 - 0.34 = 0.66 (deep) — irrelevant to the rest-poly-Dzumhur direction.
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 2,
        "msg": {"market_ticker": "KXATPMATCH-X-DZU", "yes_dollars_fp": [["0.6600", "5000"]],
                "no_dollars_fp": [["0.3400", "5000"]]}}))
    return bs


def test_tennis_driver_skips_direction_when_poly_leg_over_cap():
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.universe import build_universe
    uni = build_universe({"tennis": _tennis_tree("2027-01-01T00:00:00Z")}, now_ts=0.0,
                         max_games=20, expire_before_kickoff_s=120)
    now = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)

    def restpoly_dzumhur_quoted(cap):
        st = _RecState()
        cfg = _cfg(); cfg.poly_leg_cap = {"tennis": cap}
        drv = QuoteDriver(cfg, st); drv.set_universe(uni)
        drv.refresh_quotes(_fav_books(), now, now_ts=100.0)
        return any(r["event"] == "quote" and r["direction"] == "rest-poly" and r["side"] == "dzumhur"
                   for r in st.rows)

    # With a HIGH cap the favorite's rest-poly leg (~0.67) quotes; the 0.65 cap SKIPS it (no quote row).
    assert restpoly_dzumhur_quoted(cap=0.99) is True
    assert restpoly_dzumhur_quoted(cap=0.65) is False


def test_tennis_start_utc_refresh_reanchors_expiry():
    """On a tree reload with a SLID start time, set_universe rebuilds candidates with the new kickoff,
    so expire_kickoff re-anchors to the refreshed start_utc (tennis 'not before' times slide)."""
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.universe import build_universe
    st = _RecState()
    drv = QuoteDriver(_cfg(), st)
    # First build: kickoff far future -> candidates anchored there.
    uni_a = build_universe({"tennis": _tennis_tree("2027-01-01T00:00:00Z")}, now_ts=0.0,
                           max_games=20, expire_before_kickoff_s=120)
    drv.set_universe(uni_a)
    ka = drv._cands[0].kickoff_ts
    # Rebuild with the match SLID two hours later -> candidates re-anchor to the new start.
    uni_b = build_universe({"tennis": _tennis_tree("2027-01-01T02:00:00Z")}, now_ts=0.0,
                           max_games=20, expire_before_kickoff_s=120)
    drv.set_universe(uni_b)
    kb = drv._cands[0].kickoff_ts
    assert abs((kb - ka) - 7200.0) < 1.0                                     # +2h re-anchor


# --------------------------------------------------------------------------- #
# UFC coverage: per-sport cap map, universe admission, CHURN disarm on reload     #
# --------------------------------------------------------------------------- #
def _ufc_tree(kickoff, game="KXUFCFIGHT-A", note="dnc_50_50", risk=None):
    n = lambda side, tok, kt: {   # noqa: E731
        "market_type": "fight_winner", "market_key": "fight_winner", "side": side, "line": None,
        "kind": "2way", "poly_token_id": tok, "poly_side": side.title(), "poly_fee_rate": 0.05,
        "kalshi_ticker": kt, "kalshi_side": "YES",
        **({"settlement_note": note} if note else {}), **({"settlement_risk": risk} if risk else {})}
    return {"games": {game: {"away": "Usman", "home": "Du Plessis", "kickoff_utc": kickoff,
            "sport": "ufc", "nodes": [n("usman", "U_U", game + "-USM"),
                                      n("du plessis", "U_D", game + "-DU")]}}}


def test_ufc_per_sport_cap_from_map():
    from src.genz.maker_rt.quotes import poly_leg_exceeds_cap
    cap = {"tennis": 0.65, "ufc": 0.70}
    assert poly_leg_exceeds_cap("polymarket", 0.72, None, cap["ufc"]) is True    # ufc rest-poly 0.72 > 0.70
    assert poly_leg_exceeds_cap("kalshi", 0.48, 0.72, cap["ufc"]) is True         # ufc hedge-poly 0.72
    assert poly_leg_exceeds_cap("polymarket", 0.60, None, cap["ufc"]) is False    # 0.60 admitted


def test_ufc_dnc_note_nodes_enter_universe():
    from src.genz.maker_rt.universe import build_universe, poly_tokens
    uni = build_universe({"ufc": _ufc_tree("2027-01-01T00:00:00Z")}, now_ts=0.0,
                         max_games=20, expire_before_kickoff_s=120)
    assert len(uni) == 1 and uni[0].sport == "ufc"                               # dnc_50_50 note ADMITTED
    assert set(poly_tokens(uni)) == {"U_U", "U_D"}
    risk = build_universe({"ufc": _ufc_tree("2027-01-01T00:00:00Z", note=None,
                          risk="ufc_unparsed_settlement")}, now_ts=0.0, max_games=20,
                          expire_before_kickoff_s=120)
    assert risk == []


def test_ufc_driver_uses_ufc_cap():
    """The driver reads the per-sport cap from cfg.poly_leg_cap['ufc'] for a ufc market."""
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.universe import build_universe
    uni = build_universe({"ufc": _ufc_tree("2027-01-01T00:00:00Z")}, now_ts=0.0,
                         max_games=20, expire_before_kickoff_s=120)
    st = _RecState()
    cfg = _cfg(); cfg.poly_leg_cap = {"ufc": 0.65}
    drv = QuoteDriver(cfg, st); drv.set_universe(uni)
    assert all(c.poly_leg_cap == 0.65 for c in drv._cands)                       # ufc candidates carry the cap


def test_churn_disarms_quotes_for_dropped_fight_on_reload():
    """CHURN: a UFC fight that vanishes (cancel/opponent swap) between rebuilds must have its shadow
    quote DISARMED on set_universe, so a later print can NEVER fill it on a stale book."""
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.universe import build_universe
    st = _RecState()
    drv = QuoteDriver(_cfg(), st)
    uni_a = build_universe({"ufc": _ufc_tree("2027-01-01T00:00:00Z", game="KXUFCFIGHT-A")},
                           now_ts=0.0, max_games=20, expire_before_kickoff_s=120)
    drv.set_universe(uni_a)
    # Arm a shadow quote for the Usman rest-poly candidate directly.
    key = ("ufc", "KXUFCFIGHT-A", "fight_winner", "usman", "rest-poly")
    ref = ("polymarket", "U_U", "BUY")
    drv.fills.arm(key, ref, 0.46, 50, queue_ahead=0, at_best=True, ts=0.0,
                  hedge_ctx={"lookup": {}, "hedge_venue": "kalshi", "poly_rate": 0.05})
    assert key in drv.fills.quotes
    # Reload with the fight GONE (a different fight replaces it) -> churn disarm fires.
    uni_b = build_universe({"ufc": _ufc_tree("2027-01-01T00:00:00Z", game="KXUFCFIGHT-B")},
                           now_ts=0.0, max_games=20, expire_before_kickoff_s=120)
    drv.set_universe(uni_b, now=datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc))
    assert key not in drv.fills.quotes                                          # disarmed
    assert any(r["event"] == "expire" and r["reason"] == "churn_gone" for r in st.rows)
    # A print at the old level can no longer fill it (it's gone).
    assert drv.fills.consume_print(ref, 0.45, 999, ts=1.0) == []
