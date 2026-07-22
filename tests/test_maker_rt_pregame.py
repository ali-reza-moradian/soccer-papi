"""Tests for the CONTINUOUS pre-game live executor (rest-poly): place-on-armed, never-crossable
re-check, reprice atomicity (cancel->confirm->place), fill->hedge routing (decline+unwind, partial),
every cap refusal (incl. projected daily-stake), user-feed-down halt+cancel, and the driver
integration (eligible -> live, disarm -> cancel). No network, no real orders."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt.caps import LiveCaps
from src.genz.maker_rt.pregame_exec import PregameLiveExecutor


# --------------------------------------------------------------------------- #
# fakes                                                                          #
# --------------------------------------------------------------------------- #
class _OrderClient:
    """PolyOrderClient stand-in: rest() returns a resting order id; cancel() confirms via the response."""
    def __init__(self):
        self.rests = []
        self.cancels = []
        self._n = 0
        self.rest_result = None            # override to force an immediate-fill / no-id result

    def rest(self, token, price, size, *, tick_size=None, neg_risk=None):
        self._n += 1
        oid = f"oid{self._n}"
        self.rests.append({"token": token, "price": price, "size": size, "oid": oid})
        if self.rest_result is not None:
            return dict(self.rest_result, order_id=self.rest_result.get("order_id", oid))
        return {"status": "resting", "shares": 0, "avg_price": price, "order_id": oid}

    def cancel(self, oid):
        self.cancels.append(oid)
        return {"canceled": [oid]}


class _Poly:
    """PolyExec stand-in for REST reads / unwind / neg-risk / cancel-all."""
    def __init__(self, *, order_status="CANCELED", size_matched=0.0, sell_price=0.45):
        self.order_status = order_status
        self.size_matched = size_matched
        self.sell_price = sell_price
        self.market_sells = []
        self.cancel_all_calls = 0

    def get_order(self, oid):
        return {"status": self.order_status, "size_matched": self.size_matched}

    def _tick_and_negrisk(self, token):
        return ("0.01", False)

    def place_market_sell(self, token, shares):
        self.market_sells.append({"token": token, "shares": shares})
        return {"status": "filled", "avg_price": self.sell_price, "shares": shares}

    def conditional_balance(self, token_id):
        return getattr(self, "position", 0.0)   # flat by default; set .position>0 to simulate an ORPHAN

    def cancel_all(self):
        self.cancel_all_calls += 1
        return {"canceled": []}


class _Hedger:
    def __init__(self, result, poly=None):
        self.result = result
        self.calls = []
        self.poly = poly

    def hedge(self, fill, spec):
        self.calls.append({"fill": fill, "spec": spec})
        return self.result

    def hedge_poly(self, fill, spec):                 # rest_kalshi hedge (poly FAK); same fake result
        self.calls.append({"fill": fill, "spec": spec, "venue": "poly"})
        return self.result


class _KalshiOC:
    """KalshiOrderClient stand-in for rest_kalshi placement/cancel/status."""
    def __init__(self):
        self.rests = []
        self.cancels = []
        self._n = 0
        self.status = {}                              # oid -> status dict (default {} = gone)

    def rest(self, ticker, side, price, count, client_order_id=None):
        self._n += 1
        oid = f"koid{self._n}"
        self.rests.append({"ticker": ticker, "side": side, "price": price, "count": count, "oid": oid})
        return {"status": "resting", "fill_count": 0, "avg_price": price, "order_id": oid}

    def cancel(self, oid):
        self.cancels.append(oid)
        return {"canceled": [oid]}

    def cancel_all(self):
        return 0

    def order_status(self, oid):
        return self.status.get(oid, {})


class _KalshiExec:
    """KalshiExec stand-in for rest_kalshi unwind + portfolio position reads."""
    def __init__(self, sell_price=0.45, unwind_flattens=True):
        self.market_sells = []
        self.sell_price = sell_price
        self.unwind_flattens = unwind_flattens
        self.positions: dict = {}                     # ticker -> net contracts

    def place_market_sell(self, ticker, side, count, client_order_id=None):
        self.market_sells.append({"ticker": ticker, "side": side, "count": count})
        if self.unwind_flattens:
            self.positions[ticker] = 0.0              # the IOC sell flattened us
        return {"status": "filled", "fill_count": count, "avg_price": self.sell_price}

    def get_positions(self):
        return {"market_positions": [{"ticker": t, "position": p} for t, p in self.positions.items()]}

    def cancel_all(self):
        return 0


class _Store:
    """poly_view/kalshi_view/poly_tick stand-in."""
    def __init__(self, *, poly_best_ask=0.55, kalshi_ask=0.50, poly_best_bid=None):
        self.poly_best_ask = poly_best_ask
        self.kalshi_ask = kalshi_ask
        self.poly_best_bid = poly_best_bid if poly_best_bid is not None else poly_best_ask - 0.02

    def poly_view(self, token):
        return SimpleNamespace(best_ask=self.poly_best_ask, best_bid=self.poly_best_bid,
                               ask_ladder=[(self.poly_best_ask, 500)])

    def kalshi_view(self, ticker, side):
        return SimpleNamespace(best_ask=self.kalshi_ask, best_bid=self.kalshi_ask - 0.01,
                               ask_ladder=[(self.kalshi_ask, 500)])

    def poly_tick(self, token, default=0.01):
        return 0.01

    def is_fresh(self, ident, now_ts, s):
        return True                       # books always "fresh" in the executor unit tests

    def node_fresh(self, ident, now_ts, conn_fresh_s, node_quiet_max_s):
        return getattr(self, "fresh", True)   # override .fresh=False to simulate a stale node


class _State:
    def __init__(self):
        self.rows = []

    def record(self, row, now):
        self.rows.append(row)


class _Guard:
    def __init__(self):
        self.held = None

    def acquire(self, who):
        if self.held is not None:
            return False
        self.held = who
        return True

    def release(self, who=None):
        self.held = None


def _cand(direction="rest-poly", token="TOK1"):
    return SimpleNamespace(
        key=("mlb", "G1", "ml2", "Home", direction), sport="mlb", game="G1", market_key="ml2",
        rest_side="Home", direction=direction, rest_ref=("polymarket", token, "BUY"),
        rest_venue="polymarket", hedge_venue="kalshi", poly_rate=0.05, rest_id=token, hedge_id="KX-1",
        hedge_lookup={"venue": "kalshi", "ticker": "KX-1", "side": "yes"})


def _cand_kalshi(ticker="KX-1", side="yes", htoken="TOKH"):
    return SimpleNamespace(
        key=("mlb", "G1", "ml2", "Home", "rest-kalshi"), sport="mlb", game="G1", market_key="ml2",
        rest_side="Home", direction="rest-kalshi", rest_ref=("kalshi", ticker, side),
        rest_venue="kalshi", hedge_venue="polymarket", poly_rate=0.05, rest_id=ticker, hedge_id=htoken,
        hedge_lookup={"venue": "polymarket", "token": htoken})


def _exec_kalshi(tmp_path, *, kalshi_oc=None, kalshi=None, poly=None, hedger=None, state=None):
    """Build an executor with rest-kalshi ENABLED + the Kalshi clients injected + kalshi feed UP."""
    poly = poly or _Poly()
    ex, cfg = _exec(tmp_path, poly=poly, hedger=hedger, state=state)
    ex.kalshi_order_client = kalshi_oc or _KalshiOC()
    ex.kalshi = kalshi or _KalshiExec()
    ex.directions = {"rest-poly", "rest-kalshi"}
    ex.kalshi_feed_ok = True
    return ex, cfg


def _dec(price=0.46, hedge_ask=0.50):
    return SimpleNamespace(quote_price=price, hedge_best_ask=hedge_ask, size_shares=5, at_best=True,
                           viable=True)


def _exec(tmp_path, *, caps=None, poly=None, hedger=None, order_client=None, state=None, guard=None):
    arm = tmp_path / "ARM_MAKER"
    arm.write_text("1")
    ip_arm = tmp_path / "ARM_MAKER_INPLAY"
    ip_arm.write_text("1")
    cfg = mrt_config.MakerRtConfig()
    cfg.live.enabled = True
    cfg.live.arm_file = str(arm)
    cfg.live_inplay.enabled = True                 # arm BOTH phases for the executor unit tests
    cfg.live_inplay.arm_file = str(ip_arm)
    caps = caps or LiveCaps(cfg.live)
    poly = poly or _Poly()
    hedger = hedger or _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                               locked_pnl=0.11, unwind_cost=None), poly=poly)
    order_client = order_client or _OrderClient()
    state = state or _State()
    ex = PregameLiveExecutor(cfg, gate=None, order_client=order_client, hedger=hedger, caps=caps,
                             poly=poly, in_flight=guard or _Guard(), telegram=None, state=state, log=None)
    ex.feed_ok = True                 # simulate the Poly USER socket being UP (set by _run in production)
    return ex, cfg


# --------------------------------------------------------------------------- #
# eligibility + place-on-armed                                                  #
# --------------------------------------------------------------------------- #
def test_eligible_only_restpoly_pre_armed_feedup(tmp_path):
    ex, _ = _exec(tmp_path)
    assert ex.eligible(_cand("rest-poly"), "pre") is True
    assert ex.eligible(_cand("rest-kalshi"), "pre") is False     # rest-kalshi stays shadow (both phases)
    assert ex.eligible(_cand("rest-kalshi"), "inplay", 1000.0) is False
    ex.feed_ok = False
    assert ex.eligible(_cand("rest-poly"), "pre") is False       # user feed down -> not eligible
    assert ex.eligible(_cand("rest-poly"), "inplay", 1000.0) is False


def test_place_on_armed(tmp_path):
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, _Store(poly_best_ask=0.55), now=None, now_ts=1.0)
    assert len(oc.rests) == 1 and oc.rests[0]["price"] == 0.46
    assert ex.open_count() == 1 and ex.caps.open_quotes == 1


def test_never_crossable_recheck_refuses(tmp_path):
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc)
    # live best_ask == price -> price > best_ask - tick -> would cross -> NO place.
    ex.place_or_reprice(_cand(), _dec(price=0.50), None, _Store(poly_best_ask=0.50), now=None, now_ts=1.0)
    assert oc.rests == [] and ex.open_count() == 0


# --------------------------------------------------------------------------- #
# reprice atomicity                                                             #
# --------------------------------------------------------------------------- #
def test_reprice_hysteresis_matrix(tmp_path):
    """VOLUNTARY reprice only on >= reprice_min_ticks (2) improvement AND rested >= min_rest_s (20)."""
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc, poly=_Poly(order_status="CANCELED"))
    store = _Store(poly_best_ask=0.60, poly_best_bid=0.45)     # our 0.46 sits AT best (>= best_bid)
    key = _cand().key
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, None, 1.0, "pre")   # placed at t=1
    # +2 ticks but rested only 1s (< min_rest) -> NO reprice (preserve queue position)
    ex.place_or_reprice(_cand(), _dec(price=0.48), None, store, None, 2.0, "pre")
    assert oc.cancels == [] and len(oc.rests) == 1 and ex.open_orders[key].price == 0.46
    # +1 tick after min_rest -> still NO reprice (< reprice_min_ticks)
    ex.place_or_reprice(_cand(), _dec(price=0.47), None, store, None, 30.0, "pre")
    assert oc.cancels == [] and len(oc.rests) == 1
    # +2 ticks AND rested >= min_rest -> VOLUNTARY reprice fires (cancel->confirm->place)
    ex.place_or_reprice(_cand(), _dec(price=0.48), None, store, None, 40.0, "pre")
    assert oc.cancels == ["oid1"] and len(oc.rests) == 2 and oc.rests[1]["price"] == 0.48
    assert ex.open_count() == 1 and ex.caps.open_quotes == 1     # never exceeded max_open mid-transition


def test_reprice_mandatory_on_floor_break_is_immediate(tmp_path):
    """A floor/never-crossable violation is an IMMEDIATE mandatory reprice, ignoring min_rest_s."""
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc, poly=_Poly(order_status="CANCELED"))
    store = _Store(poly_best_ask=0.60)
    d = _dec(price=0.46); d.floor = 0.50
    ex.place_or_reprice(_cand(), d, None, store, None, 1.0, "pre")   # rests at 0.46 (floor 0.50)
    d2 = _dec(price=0.44); d2.floor = 0.44          # floor dropped below our resting price -> uneconomic
    ex.place_or_reprice(_cand(), d2, None, store, None, 2.0, "pre")  # rested only 1s, but MANDATORY
    assert oc.cancels == ["oid1"] and len(oc.rests) == 2 and oc.rests[1]["price"] == 0.44


def test_reprice_not_confirmed_does_not_double_place(tmp_path):
    class _OCNoConfirm(_OrderClient):
        def cancel(self, oid):
            self.cancels.append(oid)
            return {"not_canceled": {oid: "x"}}       # explicitly NOT canceled

    oc = _OCNoConfirm()
    ex, _ = _exec(tmp_path, order_client=oc, poly=_Poly(order_status="LIVE"))
    store = _Store(poly_best_ask=0.60)
    d = _dec(price=0.46); d.floor = 0.50
    ex.place_or_reprice(_cand(), d, None, store, None, 1.0, "pre")
    d2 = _dec(price=0.44); d2.floor = 0.44          # MANDATORY reprice, but the cancel won't confirm
    ex.place_or_reprice(_cand(), d2, None, store, None, 2.0, "pre")
    assert oc.cancels == ["oid1"]
    assert len(oc.rests) == 1                          # did NOT re-place (cancel unconfirmed)
    assert ex.open_count() == 1


# --------------------------------------------------------------------------- #
# fill -> hedge routing                                                         #
# --------------------------------------------------------------------------- #
def test_fill_routes_to_hedge_locked(tmp_path):
    oc = _OrderClient()
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                     locked_pnl=0.11, unwind_cost=None))
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, now=None, now_ts=1.0)
    oid = oc.rests[0]["oid"]
    ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.46}, store, None, 2.0)
    assert len(hedger.calls) == 1 and hedger.calls[0]["fill"]["size"] == 5
    assert ex.caps.fills_today == 1 and ex.caps.pnl_today == pytest.approx(0.11)
    assert ex.open_count() == 0                        # fully filled -> no longer resting


def test_fill_declines_and_unwinds_when_hedge_too_dear(tmp_path):
    oc = _OrderClient()
    poly = _Poly(sell_price=0.44)
    hedger = _Hedger(SimpleNamespace(status="locked"), poly=poly)     # should NOT be reached
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, poly=poly)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.62)   # hedge ask 0.62 -> locked well below -1% floor
    ex.place_or_reprice(_cand(), _dec(price=0.46, hedge_ask=0.62), None, store, now=None, now_ts=1.0)
    oid = oc.rests[0]["oid"]
    ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.46}, store, None, 2.0)
    assert hedger.calls == []                          # DECLINED -> never legged into the bad hedge
    assert poly.market_sells and poly.market_sells[0]["shares"] == 5   # unwound the naked fill
    assert ex.caps.fills_today == 1                    # counts as a (losing) fill


def test_partial_fill_hedges_each_delta(tmp_path):
    oc = _OrderClient()
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=3, hedge_avg_price=0.50,
                                     locked_pnl=0.05, unwind_cost=None))
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, now=None, now_ts=1.0)
    oid = oc.rests[0]["oid"]
    ex.on_order_update({"order_id": oid, "size_matched": 3, "price": 0.46}, store, None, 2.0)
    assert ex.open_count() == 1 and hedger.calls[0]["fill"]["size"] == 3   # remainder still resting
    ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.46}, store, None, 3.0)
    assert len(hedger.calls) == 2 and hedger.calls[1]["fill"]["size"] == 2 # hedged only the delta
    assert ex.open_count() == 0


# --------------------------------------------------------------------------- #
# caps refusals (incl. projected daily-stake)                                   #
# --------------------------------------------------------------------------- #
def test_cap_refuses_on_projected_daily_stake_and_halts(tmp_path):
    caps = LiveCaps(mrt_config.LiveConfig(quote_usd_max=5, max_daily_stake_usd=100,
                                          max_open_quotes=9, max_fills_per_day=9))
    caps.commit_stake(98.0)
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc, caps=caps)
    # projected pair for 5@0.46 + hedge 5@0.50 ~ $4.80 -> 98 + 4.8 > 100 -> refuse + HALT
    ex.place_or_reprice(_cand(), _dec(price=0.46, hedge_ask=0.50), None, _Store(poly_best_ask=0.60),
                        now=None, now_ts=1.0)
    assert oc.rests == [] and caps.halted is True
    assert ex.eligible(_cand("rest-poly"), "pre") is False    # halted -> no longer eligible


def test_cap_refuses_on_max_open(tmp_path):
    caps = LiveCaps(mrt_config.LiveConfig(max_open_quotes=1, max_daily_stake_usd=1000, max_fills_per_day=9))
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc, caps=caps)
    store = _Store(poly_best_ask=0.60)
    ex.place_or_reprice(_cand(direction="rest-poly", token="A"), _dec(0.46), None, store, None, 1.0)
    c2 = _cand(direction="rest-poly", token="B"); c2.key = ("mlb", "G2", "ml2", "Home", "rest-poly")
    ex.place_or_reprice(c2, _dec(0.46), None, store, None, 2.0)
    assert len(oc.rests) == 1 and ex.open_count() == 1        # second refused by max_open_quotes


# --------------------------------------------------------------------------- #
# user-feed-down halt + cancel                                                  #
# --------------------------------------------------------------------------- #
def test_feed_down_cancels_all_and_halts(tmp_path):
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED")
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly)
    ex.place_or_reprice(_cand(), _dec(0.46), None, _Store(poly_best_ask=0.60), None, 1.0)
    assert ex.open_count() == 1
    ex.set_feed_ok(False)
    assert ex.open_count() == 0 and oc.cancels == ["oid1"]    # feed down -> open quote cancelled
    assert ex.feed_ok is False
    assert ex.eligible(_cand("rest-poly"), "pre") is False    # placement halted while feed down


def test_cancel_all_confirms(tmp_path):
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED")
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly)
    ex.place_or_reprice(_cand("rest-poly", "A"), _dec(0.46), None, _Store(poly_best_ask=0.60), None, 1.0)
    n = ex.cancel_all("shutdown")
    assert n == 1 and poly.cancel_all_calls == 1 and ex.open_count() == 0


# --------------------------------------------------------------------------- #
# DRIVER integration: eligible -> live (shadow suppressed); churn -> cancel      #
# --------------------------------------------------------------------------- #
class _FakePregame:
    def __init__(self):
        self.open_orders = {}
        self.placed = []
        self.cancelled = []

    def eligible(self, c, phase, now_ts=0.0):
        return phase == "pre" and c.direction == "rest-poly"

    def inplay_armed(self):
        return False

    def cooloff_ok(self, store, c, freeze_until_ts, now_ts):
        return True

    def place_or_reprice(self, c, dec, rest, store, now, now_ts, phase="pre"):
        self.placed.append((c.key, dec.quote_price))
        self.open_orders[c.key] = {"price": dec.quote_price}

    def cancel(self, c, now, reason):
        self.cancelled.append((c.key, reason))
        self.open_orders.pop(c.key, None)
        return True

    def cancel_key(self, key, now, reason):
        self.cancelled.append((key, reason))
        self.open_orders.pop(key, None)
        return True


class _DriverState:
    def __init__(self):
        self.rows = []

    def record(self, row, now):
        self.rows.append(row)

    def record_achievable(self, *a, **k):
        pass


def _mlb_universe():
    from src.genz.maker_rt.universe import build_universe
    future = "2027-01-01T00:00:00Z"
    n = lambda side, tok, kt, ks: {"market_type": "ml2", "market_key": "ml2", "side": side,  # noqa: E731
        "line": None, "kind": "2way", "poly_token_id": tok, "poly_side": side.title(),
        "poly_fee_rate": 0.05, "kalshi_ticker": kt, "kalshi_side": ks}
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": future, "nodes": [
        n("away", "TOK_A", "KX-1", "YES"), n("home", "TOK_B", "KX-1", "NO")]}}}
    return build_universe({"mlb": tree}, now_ts=0.0, max_games=20, expire_before_kickoff_s=120)


def _books():
    from src.genz.maker_rt import parsing
    from src.genz.maker_rt.store import BookStore
    bs = BookStore()
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "TOK_A",
        "bids": [{"price": "0.45", "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}))
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "5000"]],
                "no_dollars_fp": [["0.4500", "5000"]]}}))
    return bs


def test_driver_routes_restpoly_to_live_and_suppresses_shadow():
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    st = _DriverState()
    fake = _FakePregame()
    drv = QuoteDriver(mrt_config.MakerRtConfig(), st, pregame_exec=fake)
    drv.set_universe(_mlb_universe())
    now = datetime(2026, 7, 16, 18, 0, 0, tzinfo=timezone.utc)
    drv.refresh_quotes(_books(), now, now_ts=100.0)
    # the viable rest-poly candidate went LIVE (placed), at 0.46...
    assert any(k[4] == "rest-poly" and px == 0.46 for k, px in fake.placed)
    # ...and NO shadow 'quote' row was written for a rest-poly direction (shadow suppressed for live).
    assert not any(r.get("event") == "quote" and r.get("direction") == "rest-poly" for r in st.rows)
    # a public print through the live quote must NOT create a shadow fill (live fills come off the socket).
    drv.consume_prints([(("polymarket", "TOK_A", "BUY"), 0.45, 10.0)], _books(), now, now_ts=101.0)
    assert not any(r.get("event") == "fill" for r in st.rows)


def test_driver_churn_cancels_live_order():
    from datetime import datetime, timezone
    from src.genz.maker_rt.driver import QuoteDriver
    st = _DriverState()
    fake = _FakePregame()
    drv = QuoteDriver(mrt_config.MakerRtConfig(), st, pregame_exec=fake)
    drv.set_universe(_mlb_universe())
    now = datetime(2026, 7, 16, 18, 0, 0, tzinfo=timezone.utc)
    drv.refresh_quotes(_books(), now, now_ts=100.0)
    assert fake.open_orders                                   # a live order is resting
    drv.set_universe([], now=now)                             # the game vanished from the tree
    assert any(reason == "churn_gone" for _k, reason in fake.cancelled)
    assert not fake.open_orders                               # live order was cancelled on churn


# --------------------------------------------------------------------------- #
# IN-PLAY LIVE (phase-aware): eligibility, cool-off, shared caps, circuit        #
# --------------------------------------------------------------------------- #
_DT = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def test_inplay_eligibility_gated_by_gate_pause_halt(tmp_path):
    ex, cfg = _exec(tmp_path)                                  # both gates armed, feed_ok True
    c = _cand("rest-poly")
    assert ex.eligible(c, "inplay", 1000.0) is True
    assert ex.eligible(c, "inplay", 1000.0) and not ex.eligible(_cand("rest-kalshi"), "inplay", 1000.0)
    ex.inplay_pause_until = 2000.0                             # inside the first-fill pause
    assert ex.eligible(c, "inplay", 1000.0) is False
    assert ex.eligible(c, "pre", 1000.0) is True               # PRE unaffected by the in-play pause
    ex.inplay_pause_until = 0.0
    ex.inplay_halted = True                                    # in-play day-halt
    assert ex.eligible(c, "inplay", 1000.0) is False
    assert ex.eligible(c, "pre", 1000.0) is True               # PRE continues through an in-play halt
    ex.inplay_halted = False
    os.remove(cfg.live_inplay.arm_file)                        # in-play gate disarmed
    assert ex.eligible(c, "inplay", 1000.0) is False
    assert ex.eligible(c, "pre", 1000.0) is True               # PRE gate still armed


def test_inplay_cooloff_rail(tmp_path):
    ex, _ = _exec(tmp_path)
    c = _cand("rest-poly")
    store = _Store()
    assert ex.cooloff_ok(store, c, 0.0, 1000.0) is True        # never frozen -> ok
    assert ex.cooloff_ok(store, c, 1005.0, 1000.0) is False    # frozen until 1005 > now
    assert ex.cooloff_ok(store, c, 995.0, 1000.0) is False     # thawed only 5s ago (< 10 cool-off)
    assert ex.cooloff_ok(store, c, 985.0, 1000.0) is True      # thawed 15s ago (>= 10)


def test_shared_caps_across_pre_and_inplay(tmp_path):
    oc = _OrderClient()
    caps = LiveCaps(mrt_config.LiveConfig(quote_usd_max=5, max_daily_stake_usd=1000,
                                          max_open_quotes=9, max_fills_per_day=9))
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                     locked_pnl=0.10, unwind_cost=None))
    ex, _ = _exec(tmp_path, order_client=oc, caps=caps, hedger=hedger)
    assert ex.caps is caps                                     # ONE shared caps instance
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand("rest-poly", "A"), _dec(0.46), None, store, None, 1.0, "pre")
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 5, "price": 0.46}, store, None, 2.0)
    c2 = _cand("rest-poly", "B"); c2.key = ("mlb", "G2", "ml2", "Home", "rest-poly")
    ex.place_or_reprice(c2, _dec(0.46), None, store, None, 3.0, "inplay")
    ex.on_order_update({"order_id": oc.rests[1]["oid"], "size_matched": 5, "price": 0.46}, store, None, 4.0)
    assert caps.fills_today == 2                               # BOTH phases incremented the ONE budget
    assert caps.stake_today > 0


def test_inplay_first_fill_pauses_and_resumes(tmp_path):
    oc = _OrderClient()
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                     locked_pnl=0.10, unwind_cost=None))
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger)
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    c = _cand("rest-poly")
    ex.place_or_reprice(c, _dec(0.46), None, store, None, 100.0, "inplay")
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 5, "price": 0.46}, store, None, 101.0)
    assert ex.inplay_fills_today == 1
    assert ex.inplay_pause_until == 101.0 + 120.0             # paused first_fill_pause_s
    assert ex.eligible(c, "inplay", 150.0) is False           # within the pause
    assert ex.eligible(c, "pre", 150.0) is True                # PRE unaffected
    assert ex.eligible(c, "inplay", 101.0 + 121.0) is True     # auto-resumes after the pause


def test_inplay_day_halt_on_bad_locked_net(tmp_path):
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED", sell_price=0.30)
    hedger = _Hedger(SimpleNamespace(status="locked"), poly=poly)     # not reached (declined)
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, poly=poly)
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.62)       # locked_net ~ -9% <= -2% halt threshold
    c = _cand("rest-poly")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.62), None, store, None, 100.0, "inplay")
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 3, "price": 0.46}, store, None, 101.0)
    assert ex.inplay_halted is True
    assert ex.eligible(c, "inplay", 200.0) is False
    assert ex.eligible(_cand("rest-poly", "P"), "pre", 200.0) is True   # PRE continues


def test_one_in_flight_shared_pre_inplay_defers(tmp_path):
    oc = _OrderClient()
    guard = _Guard()
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                     locked_pnl=0.10, unwind_cost=None))
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, guard=guard)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    c = _cand("rest-poly")
    ex.place_or_reprice(c, _dec(0.46), None, store, None, 1.0, "inplay")
    oid = oc.rests[0]["oid"]
    guard.acquire(("live", ("some-pregame-fill",)))           # a pre-game hedge already in flight
    ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.46}, store, None, 2.0)
    assert hedger.calls == []                                  # in-play fill DEFERRED (guard busy)
    assert ex.open_orders[c.key].matched_seen == 0.0           # not advanced -> retried later
    guard.release()
    ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.46}, store, None, 3.0)
    assert len(hedger.calls) == 1                              # hedged once the guard freed


def test_driver_freeze_cancels_live_inplay_order():
    from src.genz.maker_rt import parsing
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.store import BookStore
    from src.genz.maker_rt.universe import _kickoff_ts, build_universe
    ko = "2026-07-17T20:00:00Z"; ko_ts = _kickoff_ts(ko)
    n = lambda side, tok, kt, ks: {"market_type": "ml2", "market_key": "ml2", "side": side,  # noqa: E731
        "line": None, "kind": "2way", "poly_token_id": tok, "poly_side": side.title(),
        "poly_fee_rate": 0.05, "kalshi_ticker": kt, "kalshi_side": ks}
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": ko, "sport": "mlb",
        "nodes": [n("away", "T_A", "KX-1", "YES"), n("home", "T_B", "KX-1", "NO")]}}}
    uni = build_universe({"mlb": tree}, ko_ts + 60, max_games=20, horizon_hours={"mlb": 4.5})
    cfg = mrt_config.MakerRtConfig(); cfg.inplay.persist_ms = 0
    st = _DriverState(); fake = _FakePregame()
    key = ("mlb", "G1", "ml2", "away", "rest-poly")
    fake.open_orders[key] = {"phase": "inplay"}                # a live in-play order resting on the node
    drv = QuoteDriver(cfg, st, pregame_exec=fake); drv.set_universe(uni)
    now = datetime(2026, 7, 17, 20, 1, 0, tzinfo=timezone.utc)
    store = BookStore(); t0 = ko_ts + 60
    store.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "T_A",
        "bids": [{"price": "0.45", "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}), t0)
    store.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "5000"]],
                "no_dollars_fp": [["0.5000", "5000"]]}}), t0)
    store.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 2,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.6000", "5000"]],
                "no_dollars_fp": [["0.4000", "5000"]]}}), t0 + 1)     # +0.10 shock
    drv.refresh_quotes(store, now, t0 + 1)                     # shock -> freeze -> cancel live order
    assert any(reason == "shock_freeze" for _k, reason in fake.cancelled)
    assert key not in fake.open_orders


# --------------------------------------------------------------------------- #
# Telegram digest + lifetime metrics                                            #
# --------------------------------------------------------------------------- #
def test_telegram_digest_batches_routine_and_passes_instant(tmp_path):
    sent = []
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc)                   # default hedger LOCKS the fill
    ex.telegram = sent.append
    assert ex.digest_min == 15.0                               # digest ON (default)
    store = _Store(poly_best_ask=0.60, poly_best_bid=0.45)
    ex.place_or_reprice(_cand(), _dec(0.46), None, store, None, 1.0, "pre")
    assert sent == []                                          # routine QUOTE batched, NOT sent instantly
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 5, "price": 0.46}, store, None, 2.0)
    assert any("HEDGE" in m or "FILL" in m for m in sent)      # instant fill/hedge passes through
    ex.maybe_flush_digest(1000.0)                              # init the digest window
    ex.maybe_flush_digest(1000.0 + 15 * 60 + 1)                # window elapsed -> ONE digest line
    assert any("DIGEST" in m and "quotes" in m and "fills" in m for m in sent)


def test_digest_off_sends_routine_instantly(tmp_path):
    sent = []
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc)
    ex.telegram = sent.append
    ex.digest_min = 0.0                                        # telegram_digest_min 0 -> old behavior
    ex.place_or_reprice(_cand(), _dec(0.46), None, _Store(poly_best_ask=0.60, poly_best_bid=0.45),
                        None, 1.0, "pre")
    assert any("QUOTE" in m for m in sent)                     # routine sent instantly when digest off


def test_lifetime_and_atbest_metrics(tmp_path):
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc, poly=_Poly(order_status="CANCELED"))
    store = _Store(poly_best_ask=0.60, poly_best_bid=0.45)     # our 0.46 sits AT best (>= best_bid)
    now0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    t0 = now0.timestamp()
    ex.place_or_reprice(_cand(), _dec(0.46), None, store, now0, t0, "pre")
    ex.sample_metrics(store, t0 + 1)
    ex.sample_metrics(store, t0 + 2)
    assert ex.snapshot(t0 + 3)["time_at_best_share"] == 1.0    # both samples were at best
    now1 = datetime(2026, 7, 22, 12, 0, 45, tzinfo=timezone.utc)
    ex.cancel(_cand(), now1, "expire")                         # the quote rested 45s
    assert ex.snapshot(t0 + 46)["median_quote_age_s"] == pytest.approx(45.0, abs=0.5)


# --------------------------------------------------------------------------- #
# VERIFY-OR-SCREAM unwind + position reconciliation (the -$2.35 orphan fix)      #
# --------------------------------------------------------------------------- #
def _hedger_missed():
    return _Hedger(SimpleNamespace(status="missed", hedged_shares=0, hedge_avg_price=None,
                                   locked_pnl=None, unwind_cost=None, detail={"kalshi": {}}))


def test_unwind_verified_flat_records_unwound(tmp_path):
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED", sell_price=0.44)
    poly.position = 0.0                                        # REST read confirms flat after the sell
    ex, _ = _exec(tmp_path, order_client=oc, hedger=_hedger_missed(), poly=poly)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)        # locked >= floor -> hedge FIRES -> miss -> unwind
    ex.place_or_reprice(_cand(), _dec(0.46, hedge_ask=0.50), None, store, None, 1.0, "pre")
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 5, "price": 0.46}, store, None, 2.0)
    assert poly.market_sells and poly.market_sells[0]["shares"] == 5    # a REAL unwind sell went out
    assert ex.orphan is None and ex.caps.halted is False               # verified flat -> no orphan
    rows = [r["event"] for r in ex.state.rows]
    assert "hedge_unwound" in rows and "unwind_FAILED" not in rows


def test_unwind_not_flat_screams_and_halts(tmp_path):
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED", sell_price=0.44)
    poly.position = 5.0                                        # the sell did NOT clear the position (the bug)
    ex, _ = _exec(tmp_path, order_client=oc, hedger=_hedger_missed(), poly=poly)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(0.46, hedge_ask=0.50), None, store, None, 1.0, "pre")
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 5, "price": 0.46}, store, None, 2.0)
    assert ex.orphan is not None and ex.caps.halted is True            # VERIFY-OR-SCREAM: orphan + halt-all
    assert "unwind_FAILED" in [r["event"] for r in ex.state.rows]
    assert ex.eligible(_cand(), "pre", 3.0) is False                   # halted -> no more live quoting


def test_reconciliation_catches_seeded_orphan(tmp_path):
    oc = _OrderClient()
    poly = _Poly()
    poly.position = 4.0                                        # a stranded position on a token we traded
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly)
    ex._traded_tokens.add("TOK1")                             # (no list_positions on the fake -> per-token read)
    orph = ex.reconcile_positions(datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc))
    assert orph is not None and ex.caps.halted is True and ex.caps.halt_reason == "orphan_position"


def test_reconcile_ignores_untraded_account_positions(tmp_path):
    """Regression guard for the list_positions landmine: the funder wallet holds hundreds of unrelated
    positions; reconciliation must ONLY flag tokens THIS maker traded, never a blanket account sweep."""
    poly = _Poly()
    poly.position = 9.0                                       # a big holding on tokens we never traded
    ex, _ = _exec(tmp_path, poly=poly)                        # _traded_tokens is empty
    orph = ex.reconcile_positions(datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc))
    assert orph is None and ex.caps.halted is False           # not our orphan -> no halt


def test_reconcile_prunes_confirmed_flat_token(tmp_path):
    """A traded token that reads flat AND isn't currently quoting is dropped from the watch-set (keeps it
    bounded on this hundreds-of-positions wallet) and the pruned set is persisted."""
    poly = _Poly()                                            # conditional_balance -> 0.0 (flat)
    ex, _ = _exec(tmp_path, poly=poly)
    ex._traded_tokens.add("TOKFLAT")
    ex._persist_traded_tokens()
    orph = ex.reconcile_positions(datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc))
    assert orph is None and "TOKFLAT" not in ex._traded_tokens


def test_traded_tokens_persist_across_restart_catches_orphan(tmp_path):
    """A position stranded by a CRASHED run must survive the restart: the watch-set is persisted next to
    the arm file, reloaded by a fresh executor, and the startup reconcile then catches the orphan."""
    ex1, _ = _exec(tmp_path, poly=_Poly())                    # "run 1"
    ex1._traded_tokens.add("TOKX")
    ex1._persist_traded_tokens()
    assert (tmp_path / "maker_rt_traded_tokens.json").exists()
    poly2 = _Poly()
    poly2.position = 3.0                                       # the crash left 3 naked shares on TOKX
    ex2, _ = _exec(tmp_path, poly=poly2)                       # "run 2" (restart) reloads the watch-set
    assert "TOKX" in ex2._traded_tokens
    orph = ex2.reconcile_positions(datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc))
    assert orph is not None and ex2.caps.halted is True and ex2.caps.halt_reason == "orphan_position"


def test_decline_branch_full_chain_and_circuit(tmp_path):
    sent = []
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED", sell_price=0.30)
    poly.position = 0.0
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly)
    ex.telegram = sent.append
    ex.digest_min = 0.0                                        # instant so we can assert the alerts
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.62)       # locked ~ -9% -> DECLINE + verified unwind
    ex.place_or_reprice(_cand(), _dec(0.46, hedge_ask=0.62), None, store, _DT, 100.0, "inplay")
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 5, "price": 0.46}, store, _DT, 101.0)
    rows = [r["event"] for r in ex.state.rows]
    assert "hedge_declined" in rows and poly.market_sells      # decline -> verified unwind sold
    assert any("FILL" in m for m in sent) and any("HEDGE_DECLINED" in m.upper() for m in sent)
    assert ex.inplay_fills_today == 1 and ex.inplay_pause_until > 0    # first-fill circuit fired on decline


def test_kalshi_eligible_respects_directions_and_feed(tmp_path):
    ex, _ = _exec(tmp_path, poly=_Poly())              # default directions = rest-poly only
    ck = _cand_kalshi()
    ex.kalshi_feed_ok = True
    assert ex.eligible(ck, "pre") is False             # rest-kalshi not in directions -> ineligible
    ex.directions = {"rest-poly", "rest-kalshi"}
    assert ex.eligible(ck, "pre") is True              # enabled + kalshi feed up -> eligible
    ex.kalshi_feed_ok = False
    assert ex.eligible(ck, "pre") is False             # kalshi FILL feed down -> ineligible


def test_kalshi_place_and_cancel_venue_dispatch(tmp_path):
    koc = _KalshiOC()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc)
    ex.roll_day(_DT)
    store = _Store(kalshi_ask=0.60)
    c = _cand_kalshi()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, store, _DT, 100.0, "pre")
    assert koc.rests and koc.rests[0]["ticker"] == "KX-1" and koc.rests[0]["side"] == "yes"
    lo = ex.open_orders[c.key]
    assert lo.rest_venue == "kalshi" and lo.kalshi_side == "yes" and "KX-1" in ex._traded_tickers
    assert ex.cancel_key(c.key, _DT, "test") is True and koc.cancels == [lo.order_id]


def test_kalshi_fill_decline_unwinds_via_kalshi_and_verifies_flat(tmp_path):
    koc = _KalshiOC()
    kex = _KalshiExec()                                # place_market_sell flattens by default
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex)
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.62, kalshi_ask=0.60)  # hedge on POLY @ 0.62 -> locked bad -> DECLINE
    c = _cand_kalshi()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.62), None, store, _DT, 100.0, "inplay")
    oid = koc.rests[0]["oid"]
    ex.on_kalshi_fill({"kind": "kalshi_fill", "order_id": oid, "count": 5}, store, _DT, 101.0)
    rows = [r["event"] for r in ex.state.rows]
    assert "hedge_declined" in rows and kex.market_sells                 # declined -> kalshi IOC unwind
    assert kex.market_sells[0]["ticker"] == "KX-1" and ex.orphan is None  # REST-verified flat -> no orphan


def test_kalshi_unwind_not_flat_screams_orphan(tmp_path):
    koc = _KalshiOC()
    kex = _KalshiExec(unwind_flattens=False)           # the IOC sell does NOT clear the position
    kex.positions["KX-1"] = 5.0
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex)
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.62, kalshi_ask=0.60)
    c = _cand_kalshi()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.62), None, store, _DT, 100.0, "inplay")
    oid = koc.rests[0]["oid"]
    ex.on_kalshi_fill({"kind": "kalshi_fill", "order_id": oid, "count": 5}, store, _DT, 101.0)
    rows = [r["event"] for r in ex.state.rows]
    assert "unwind_FAILED" in rows                                       # not flat -> SCREAM
    assert ex.orphan is not None and ex.caps.halted and ex.caps.halt_reason == "orphan_position"


def test_reconcile_catches_kalshi_orphan(tmp_path):
    kex = _KalshiExec()
    kex.positions["KX-9"] = 4.0                        # a stranded Kalshi position on a ticker we traded
    ex, _ = _exec_kalshi(tmp_path, kalshi=kex)
    ex._traded_tickers.add("KX-9")
    orph = ex.reconcile_positions(_DT)
    assert orph is not None and ex.caps.halted and ex.caps.halt_reason == "orphan_position"


def test_kalshi_order_client_rest_cancel_status():
    from src.genz.maker_rt.orders import KalshiOrderClient, KALSHI_COID_PREFIX

    class _Ex:
        def __init__(self):
            self.orders = []
            self.canceled = []

        def place_order(self, ticker, side, count, price, *, action="buy", time_in_force=None,
                        post_only=False, client_order_id=None):
            self.orders.append({"ticker": ticker, "side": side, "count": count, "price": price,
                                "action": action, "tif": time_in_force, "post_only": post_only,
                                "coid": client_order_id})
            return {"status": "resting", "fill_count": 0, "avg_price": price, "order_id": f"o{len(self.orders)}"}

        def cancel_order(self, oid):
            self.canceled.append(oid); return {"canceled": [oid]}

        def get_orders(self, **k):
            return {"orders": [{"order_id": "o1", "status": "resting"}]}

    ex = _Ex()
    koc = KalshiOrderClient(ex)
    res = koc.rest("KX-1", "yes", 0.40, 3)
    assert ex.orders[0]["tif"] == "good_till_canceled" and ex.orders[0]["post_only"] is True   # v2 resting maker
    assert ex.orders[0]["action"] == "buy"
    assert ex.orders[0]["coid"].startswith(KALSHI_COID_PREFIX) and ex.orders[0]["count"] == 3
    assert koc.order_status("o1")["status"] == "resting" and koc.order_status("zz") == {}
    koc.cancel(res["order_id"])
    assert ex.canceled == [res["order_id"]]


def test_hedge_poly_locks_and_reports_miss():
    from src.genz.maker_rt.hedge import LiveHedger

    class _PolyBuy:
        def __init__(self, shares):
            self.shares = shares

        def place_market_buy(self, token, size, **k):
            return {"status": "filled", "shares": self.shares, "avg_price": 0.50}

    h = LiveHedger(poly_client=_PolyBuy(5))            # full fill -> LOCKED
    r = h.hedge_poly({"price": 0.46, "size": 5}, {"token": "T", "best_ask": 0.50})
    assert r.status == "locked" and r.hedged_shares == 5 and r.locked_pnl is not None
    h2 = LiveHedger(poly_client=_PolyBuy(0))           # zero fill -> MISS (caller unwinds)
    r2 = h2.hedge_poly({"price": 0.46, "size": 5}, {"token": "T", "best_ask": 0.50})
    assert r2.status == "missed" and r2.freeze_market is True


def test_driver_now_behind_keeps_live_order():
    from datetime import datetime, timezone
    from src.genz.maker_rt import parsing
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.store import BookStore
    from src.genz.maker_rt.universe import build_universe
    future = "2027-01-01T00:00:00Z"
    n = lambda side, tok, kt, ks: {"market_type": "ml2", "market_key": "ml2", "side": side,  # noqa: E731
        "line": None, "kind": "2way", "poly_token_id": tok, "poly_side": side.title(),
        "poly_fee_rate": 0.05, "kalshi_ticker": kt, "kalshi_side": ks}
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": future, "nodes": [
        n("away", "TOK_A", "KX-1", "YES"), n("home", "TOK_B", "KX-1", "NO")]}}}
    uni = build_universe({"mlb": tree}, 0.0, max_games=20)
    st = _DriverState(); fake = _FakePregame()
    key = ("mlb", "G1", "ml2", "away", "rest-poly")
    fake.open_orders[key] = {"phase": "pre"}                  # a live order is resting
    drv = QuoteDriver(mrt_config.MakerRtConfig(), st, pregame_exec=fake)
    drv.set_universe(uni)
    bs = BookStore()
    # poly bid 0.50 with hedge NO ask 0.50 -> floor ~0.4725 -> quote 0.4725 < best_bid 0.50 = would_be_behind
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "TOK_A",
        "bids": [{"price": "0.50", "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}), 100.0)
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "5000"]],
                "no_dollars_fp": [["0.5000", "5000"]]}}), 100.0)
    drv.refresh_quotes(bs, datetime(2026, 7, 16, 18, tzinfo=timezone.utc), now_ts=100.0)
    assert any(k == key for k, _px in fake.placed)            # routed to place_or_reprice (KEPT), not cancelled
    assert not any(reason == "now_behind" for _k, reason in fake.cancelled)
    assert key in fake.open_orders


def test_driver_hedge_thin_cooldown():
    from src.genz.maker_rt.driver import QuoteDriver
    drv = QuoteDriver(mrt_config.MakerRtConfig(), _DriverState())
    node3 = ("mlb", "G1", "ml2")
    for i in range(3):
        drv._note_thin_refusal(node3, 100.0 + i)              # 3 refusals within the 10-min window
    assert drv.thin_cooldown_until.get(node3, 0.0) > 100.0    # -> node cooled down (15 min)
    assert drv.thin_cooldown_until[node3] == pytest.approx(102.0 + 900.0)


def test_driver_hedge_thin_persistence_prefilter():
    """A VIABLE pre-game rest-poly quote on a node that JUST refused hedge_too_thin must show >= 10s of
    CONTINUOUS hedge depth before it re-arms (anti arm-then-cancel churn = longer median quote lifetime).
    A healthy node (no recent thinness) still arms immediately -> no regression."""
    from datetime import datetime, timezone
    from src.genz.maker_rt import parsing
    from src.genz.maker_rt.driver import QuoteDriver
    from src.genz.maker_rt.store import BookStore
    from src.genz.maker_rt.universe import build_universe
    future = "2027-01-01T00:00:00Z"
    n = lambda side, tok, kt, ks: {"market_type": "ml2", "market_key": "ml2", "side": side,  # noqa: E731
        "line": None, "kind": "2way", "poly_token_id": tok, "poly_side": side.title(),
        "poly_fee_rate": 0.05, "kalshi_ticker": kt, "kalshi_side": ks}
    tree = {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": future, "nodes": [
        n("away", "TOK_A", "KX-1", "YES"), n("home", "TOK_B", "KX-1", "NO")]}}}
    uni = build_universe({"mlb": tree}, 0.0, max_games=20)
    key = ("mlb", "G1", "ml2", "away", "rest-poly")
    node3 = ("mlb", "G1", "ml2")
    now = datetime(2026, 7, 16, 18, tzinfo=timezone.utc)

    # Deep on BOTH sides (bid 0.40 / ask 0.60, hedge NO 0.50) -> away rest-poly quote 0.41 viable & not-behind,
    # and no sibling direction refuses hedge_too_thin (which would trip the node's cooldown and mask this test).
    def viable_book(bs, ts, seq):
        bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "TOK_A",
            "bids": [{"price": "0.40", "size": "5000"}], "asks": [{"price": "0.60", "size": "5000"}]}), ts)
        bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": seq,
            "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "5000"]],
                    "no_dollars_fp": [["0.5000", "5000"]]}}), ts)

    # HEALTHY node (no recent thin refusal) -> arms on the first viable tick (no delay, no regression).
    fake = _FakePregame()
    drv = QuoteDriver(mrt_config.MakerRtConfig(), _DriverState(), pregame_exec=fake)
    drv.set_universe(uni)
    bs = BookStore(); viable_book(bs, 100.0, 1)
    drv.refresh_quotes(bs, now, now_ts=100.0)
    assert any(k == key for k, _px in fake.placed)            # healthy -> immediate arm

    # RECENTLY-THIN node -> must prove 10s of continuous hedge depth before it re-arms.
    fake = _FakePregame()
    drv = QuoteDriver(mrt_config.MakerRtConfig(), _DriverState(), pregame_exec=fake)
    drv.set_universe(uni)
    drv.thin_refusals[node3] = [99.0]                         # refused hedge_too_thin ~1s ago -> pre-filter on
    bs = BookStore(); viable_book(bs, 100.0, 1)
    drv.refresh_quotes(bs, now, now_ts=100.0)
    assert not fake.placed and key in drv.viable_since        # first deep tick -> start timer, DON'T arm
    viable_book(bs, 105.0, 2); drv.refresh_quotes(bs, now, now_ts=105.0)
    assert not fake.placed                                    # 5s continuous (< 10s) -> still building
    viable_book(bs, 111.0, 3); drv.refresh_quotes(bs, now, now_ts=111.0)
    assert any(k == key for k, _px in fake.placed)            # >= 10s continuous depth -> arms

    # A thin flicker RESETS the timer (viable_since popped on hedge_too_thin) — churn stays suppressed.
    fake = _FakePregame()
    drv = QuoteDriver(mrt_config.MakerRtConfig(), _DriverState(), pregame_exec=fake)
    drv.set_universe(uni)
    drv.thin_refusals[node3] = [99.0]
    bs = BookStore(); viable_book(bs, 100.0, 1)
    drv.refresh_quotes(bs, now, now_ts=100.0)                 # timer starts at 100
    bs.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 2,  # hedge goes THIN
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "1"]],
                "no_dollars_fp": [["0.5000", "1"]]}}), 108.0)
    drv.refresh_quotes(bs, now, now_ts=108.0)
    assert key not in drv.viable_since                        # thin -> timer reset
    viable_book(bs, 112.0, 3); drv.refresh_quotes(bs, now, now_ts=112.0)
    assert not fake.placed                                    # only 4s since depth returned (< 10s) -> not armed yet
