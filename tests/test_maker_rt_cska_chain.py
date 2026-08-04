"""Regression suite for the 2026-07-28 17:21Z CSKA 1948 / Trnava chain (rest-kalshi -> hedge-poly).

Every number below is VENUE TRUTH, read back from Kalshi's own /portfolio endpoints after the fact —
not from our logs, which had already mis-booked it.

WHAT HAPPENED, in order:

  17:21:37Z  our resting BUY (order 329a9a89, 318 contracts @ $0.22 on KXUECLTOTAL-26JUL28CSKTRN-4)
             was hit for ``count_fp: "114.49"``. Kalshi fills FRACTIONAL contracts. Cost $25.1878,
             maker fee $0. 203.51 contracts stayed RESTING.
  17:21:43Z  the Poly hedge — a market BUY of 114.49 shares — came back 400:
             ``invalid amounts, the market buy orders maker amount supports a max accuracy of 2
             decimals, taker amount a max of 4 decimals``. The hedge token's tick is 0.001, so the
             order builder happily signed a 5-decimal USDC maker amount (0.762 x 114.49 = 87.24138)
             and the VENUE rejected it. Nothing to do with depth or the price floor: the hedge never
             reached the book. 114.49 contracts left naked.
  17:21:50Z  unwind attempt 1: IOC sell 114 @ $0.01 -> fill_count 0.00, remaining_count 0.00, CANCELED.
  17:21:52Z  unwind attempt 2: identical. Both were killed by ``self_trade_prevention_type:
             taker_at_cross`` — the sell crossed OUR OWN 203.51-contract bid, still resting.
  17:21:56Z  the orphan halt finally cancelled that bid. Four seconds too late.
  17:21:59Z  ORPHAN -> full halt, and the day booked -$25.1878 (the entire notional, worst case).
  17:33:36Z  a human sold the 114.49 @ $0.23 as a taker: proceeds $26.3327, fee $1.4194 -> $24.9133.
             TRUE outcome: -$0.2745. The bot stayed halted for three hours and kept the -$25.19.

So: the hedge failed on an ARITHMETIC rule, the unwind failed on our OWN order, and the ledger kept a
92x overstatement of a loss that had already resolved. Each gets a test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.executor.poly_exec import PolyExec, PolyExecError, market_buy_amounts
from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt.pregame_exec import MIN_TRADABLE, PregameLiveExecutor
from src.genz.maker_rt.settle import realized_from_kalshi_fills

from .test_maker_rt_pregame import (_Hedger, _KalshiExec, _KalshiOC, _Poly, _Store, _cand_kalshi, _dec,
                                    _exec_kalshi)

_DT = datetime(2026, 7, 28, 17, 21, 41, tzinfo=timezone.utc)

TICKER = "KXUECLTOTAL-26JUL28CSKTRN-4"
FILL_SHARES = 114.49          # count_fp on the maker fill — Kalshi fills fractional contracts
FILL_PRICE = 0.22
COST = 25.1878                # maker_fill_cost_dollars
TRUE_PNL = -0.2745            # after the 17:33Z manual close at 23c less the $1.4194 taker fee


def _dp(x: float) -> int:
    """Decimal places of ``x`` the way the CLOB counts them."""
    return abs(Decimal(str(x)).as_tuple().exponent)


# --------------------------------------------------------------------------- #
# 1. The hedge 400: market-BUY amounts must be venue-legal                       #
# --------------------------------------------------------------------------- #
def test_market_buy_amounts_land_on_a_whole_cent():
    """The venue's rule is about the PRODUCT, so the quantization has to be about the product."""
    price, shares = market_buy_amounts(0.762, FILL_SHARES)       # the exact CSKA hedge
    assert (price, shares) == (0.77, 114.0)
    assert _dp(round(price * shares, 6)) <= 2

    # Sweep the shapes that actually occur: every 0.001 tick against a fractional Kalshi fill count.
    for cents in range(1, 100):
        for frac in (0.0, 0.01, 0.49, 0.5, 0.99):
            p, n = market_buy_amounts(cents / 1000.0 * 7.7, 114 + frac)   # arbitrary 3-decimal prices
            maker = round(p * n, 10)
            assert _dp(maker) <= 2, f"maker amount {maker} from price {p} x {n} would 400"
            assert _dp(n) <= 4


def test_market_buy_price_is_a_ceiling_and_never_exceeds_an_approved_cap():
    """A BUY limit is a ceiling, so it rounds UP to stay marketable on a 0.001-tick book — but the
    pre-hedge gate hands us a price already floored to the cent, so the cap is never breached."""
    assert market_buy_amounts(0.7615, 10)[0] == 0.77             # uncapped: stays marketable
    assert market_buy_amounts(0.76, 10)[0] == 0.76               # gate-approved cap: unchanged
    assert market_buy_amounts(0.9999, 10)[0] == 0.99             # clamped inside (0,1)


class _StrictClob:
    """A CLOB stand-in that enforces the SAME two rules the live venue enforced on us: it rounds like
    py_clob_client_v2's builder, then rejects a market BUY whose maker amount carries >2 decimals."""

    ROUND = {"0.1": (1, 2), "0.01": (2, 2), "0.001": (3, 2), "0.0001": (4, 2)}

    def __init__(self, tick="0.001"):
        self.tick = tick
        self.posted = []

    def get_tick_size(self, token):
        return self.tick

    def get_neg_risk(self, token):
        return False

    def create_order(self, args, options):
        pdp, sdp = self.ROUND[options.tick_size]
        price = round(args.price, pdp)
        taker = float(int(args.size * 10 ** sdp)) / 10 ** sdp    # round_down(size, 2)
        return {"price": price, "taker": taker, "maker": round(price * taker, 10)}

    def create_market_order(self, args, options):
        """The AMOUNT path, rounded exactly as py_clob_client_v2's ``get_market_order_amounts`` does:
        ``raw_maker_amt = round_down(amount, round_config.size)`` (size is 2 for every tick size, which
        is why this path satisfies the venue's 2-decimal maker-amount rule by construction) and
        ``raw_taker_amt = raw_maker_amt / round_down(price, price_dp)``."""
        pdp, sdp = self.ROUND[options.tick_size]
        price = float(int(args.price * 10 ** pdp)) / 10 ** pdp   # round_down(price, pdp)
        maker = float(int(args.amount * 10 ** sdp)) / 10 ** sdp  # round_down(amount, 2)
        return {"price": price, "taker": round(maker / price, 4), "maker": maker,
                "amount": args.amount}

    def post_order(self, signed, order_type):
        if _dp(signed["maker"]) > 2:
            raise PolyExecError(
                'invalid amounts, the market buy orders maker amount supports a max accuracy of 2 '
                'decimals, taker amount a max of 4 decimals')
        if _dp(signed["taker"]) > 4:
            raise PolyExecError("invalid amounts, taker amount a max of 4 decimals")
        self.posted.append(signed)
        return {"status": "matched", "makingAmount": signed["maker"], "takingAmount": signed["taker"],
                "price": signed["price"]}


def test_fractional_kalshi_fill_no_longer_400s_the_poly_hedge():
    """THE bug. 114.49 shares at the gate-approved 76c is 87.0124 USDC — four decimals — and the venue
    refuses it. The hedge must now post and be accepted."""
    clob = _StrictClob(tick="0.001")
    ex = PolyExec(client=clob)
    res = ex.place_market_buy("TOKH", FILL_SHARES, price=0.76, expected_price=0.755)
    assert clob.posted, "the hedge never reached the venue"
    assert res["status"] != "error"
    assert _dp(clob.posted[0]["maker"]) <= 2

    # And prove the old shape really would have been rejected, so this test can't pass vacuously.
    with pytest.raises(PolyExecError, match="max accuracy of 2 decimals"):
        clob.post_order({"maker": 87.0124, "taker": 114.49, "price": 0.76}, "FAK")


def test_the_amount_path_is_venue_legal_for_every_price_and_fractional_size():
    """The CSKA rule is about the maker AMOUNT, and the amount path is the one the venue defines for a
    market buy — so sweep the same shapes the old quantizer was swept over and prove it holds."""
    clob = _StrictClob(tick="0.001")
    ex = PolyExec(client=clob)
    for cents in range(1, 100):
        for frac in (0.0, 0.01, 0.49, 0.5, 0.99):
            clob.posted.clear()
            px = cents / 100.0
            ex.place_market_buy("TOKH", 114 + frac, price=min(0.99, px + 0.02), expected_price=px)
            assert clob.posted, f"nothing posted at {px}"
            sent = clob.posted[0]
            assert _dp(sent["maker"]) <= 2, f"maker {sent['maker']} at price {px} would 400"
            assert _dp(sent["taker"]) <= 4


def test_market_buy_of_sub_share_dust_never_reaches_the_venue():
    clob = _StrictClob()
    res = PolyExec(client=clob).place_market_buy("TOKH", 0.49, price=0.76)
    assert res["shares"] == 0.0 and res["order_id"] is None
    assert clob.posted == []


# --------------------------------------------------------------------------- #
# 2. The unwind: self-trade prevention, not a thin book                          #
# --------------------------------------------------------------------------- #
def test_self_trade_killed_recognises_the_venue_signature():
    """0 filled AND 0 remaining AND canceled == the taker was killed at the cross. An ordinary miss on a
    thin book leaves the count somewhere else, and an error/fill must never be mistaken for it."""
    k = PregameLiveExecutor._self_trade_killed
    assert k({"raw": {"fill_count_fp": "0.00", "remaining_count_fp": "0.00", "status": "canceled"}}) is True
    assert k({"fill_count_fp": "0.00", "remaining_count_fp": "0.00", "status": "canceled"}) is True
    assert k({"raw": {"fill_count_fp": "0.00", "remaining_count_fp": "114.00",
                      "status": "canceled"}}) is False          # ordinary IOC miss, book was thin
    assert k({"raw": {"fill_count_fp": "114.00", "remaining_count_fp": "0.00",
                      "status": "executed"}}) is False          # it filled
    assert k({"status": "error"}) is False
    assert k(None) is False


class _StpKalshiExec(_KalshiExec):
    """Kalshi that behaves like the real one did: while ANY of our orders rests on the ticker, an unwind
    sell is CANCELLED at the cross with nothing filled and nothing left."""

    def __init__(self, *, resting_gate, **kw):
        super().__init__(**kw)
        self.resting_gate = resting_gate            # callable -> True while our bid is still up
        self.killed = 0

    def place_market_sell(self, ticker, side, count, client_order_id=None):
        self.market_sells.append({"ticker": ticker, "side": side, "count": count})
        if self.resting_gate():
            self.killed += 1
            return {"status": "none", "fill_count": 0, "avg_price": None,
                    "raw": {"fill_count_fp": "0.00", "remaining_count_fp": "0.00", "status": "canceled"}}
        if not self.unwind_flattens:                 # the book was genuinely empty -> still holding
            return {"status": "none", "fill_count": 0, "avg_price": None,
                    "raw": {"fill_count_fp": "0.00", "remaining_count_fp": "114.00", "status": "canceled"}}
        self.positions[ticker] = 0.0
        return {"status": "filled", "fill_count": count, "avg_price": self.sell_price}


def _cska_executor(tmp_path, *, hedge_result=None, sell_price=0.23, auto_flatten_max_usd=0.0):
    """A rest-kalshi executor mid-incident: 114.49 contracts filled at 22c, our bid still resting."""
    koc = _KalshiOC()
    poly = _Poly()
    poly.position = 0.0
    kex = _StpKalshiExec(resting_gate=lambda: bool(koc.resting_orders(ticker=TICKER)),
                         sell_price=sell_price)
    hedger = _Hedger(hedge_result or SimpleNamespace(status="missed", hedged_shares=0.0,
                                                     hedge_avg_price=None, locked_pnl=None,
                                                     detail={"poly": {"error": "400 invalid amounts"}},
                                                     freeze_market=True), poly=poly)
    ex, cfg = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex, poly=poly, hedger=hedger)
    ex.auto_flatten_max_usd = auto_flatten_max_usd
    # PRODUCTION SIZING. The real CSKA order was a 318-lot at 22c (~$70, the live quote_usd_max) of which
    # 114.49 filled — a genuine PARTIAL. Sizing the test order at the default $5 cap instead would make it
    # a 22-lot "filling" 114.49, which a resting order cannot do; the executor now says so out loud, and a
    # regression suite should reproduce the incident rather than an impossible shorthand for it.
    ex.caps.quote_usd_max = 70.0
    ex.caps.max_pair_stake_usd = 350.0
    ex.caps.max_daily_stake_usd = 800.0
    kex.positions[TICKER] = FILL_SHARES
    return ex, koc, kex, poly


def _fill_cska(ex, koc, store):
    c = _cand_kalshi(ticker=TICKER, htoken="TOKH")
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    oid = koc.rests[0]["oid"]
    assert ex.open_orders[c.key].size >= FILL_SHARES, "the test order must be able to hold this fill"
    ex.on_kalshi_fill({"order_id": oid, "count": FILL_SHARES}, store, _DT, 2.0)
    return oid


def test_unwind_pulls_our_own_resting_bid_before_selling(tmp_path):
    """With the bid still up, every sell is killed at the cross. Pulling it first is what makes the
    unwind possible at all — and it must happen BEFORE the first sell, not after the halt."""
    ex, koc, kex, _ = _cska_executor(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.30)
    oid = _fill_cska(ex, koc, store)

    assert oid in koc.cancels, "our resting bid was not cancelled before the unwind"
    assert kex.killed == 0, "a sell was fired while our own order was still on the book"
    assert kex.market_sells and kex.market_sells[0]["count"] == 114        # floored, never 115
    assert ex.orphan is None, "unwound cleanly — this must not halt"
    assert abs(kex.positions[TICKER]) < MIN_TRADABLE["kalshi"]


def test_unwind_floors_the_count_and_never_oversells(tmp_path):
    """int(round(114.6)) is 115 — selling a contract we do not hold is an opening SHORT, not an unwind."""
    ex, koc, kex, _ = _cska_executor(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.30)
    c = _cand_kalshi(ticker=TICKER, htoken="TOKH")
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    kex.positions[TICKER] = 114.6
    ex.on_kalshi_fill({"order_id": koc.rests[0]["oid"], "count": 114.6}, store, _DT, 2.0)
    assert kex.market_sells[0]["count"] == 114


def test_sub_contract_dust_is_booked_not_orphaned(tmp_path):
    """A 0.49-contract residue has no order that could close it. Halting the bot over it is a bug."""
    ex, koc, kex, poly = _cska_executor(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.30)
    c = _cand_kalshi(ticker=TICKER, htoken="TOKH")
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    lo = ex.open_orders[c.key]
    res = ex._unwind_and_record(lo, 0.49, FILL_PRICE, None, "hedge_unwound", _DT)

    assert ex.orphan is None
    assert res["pnl"] == 0.0
    assert kex.market_sells == [], "there is no order this small to place"
    assert ex._expected_shares("kalshi", TICKER) == pytest.approx(0.49)
    assert TICKER in ex._provisional, "dust must still be reconciled at settlement"


# --------------------------------------------------------------------------- #
# 3. The ledger: worst case while open, VENUE TRUTH once resolved                #
# --------------------------------------------------------------------------- #
# Copied verbatim from GET /portfolio/fills for this ticker (ids and timestamps trimmed).
CSKA_FILLS = [
    {"action": "buy", "book_side": "bid", "count_fp": "114.49", "fee_cost": "0.000000",
     "is_taker": False, "outcome_side": "yes", "side": "yes", "ticker": TICKER,
     "yes_price_dollars": "0.2200", "no_price_dollars": "0.7800"},
    {"action": "sell", "book_side": "ask", "count_fp": "114.49", "fee_cost": "1.419400",
     "is_taker": True, "outcome_side": "no", "side": "no", "ticker": TICKER,
     "yes_price_dollars": "0.2300", "no_price_dollars": "0.7700"},
]
CSKA_SETTLEMENT = {"ticker": TICKER, "market_result": "no", "revenue": 0, "value": 0}


def test_realized_from_kalshi_fills_reproduces_the_true_cska_outcome():
    """-$0.2745, from the venue's own fills, unedited. The bot had booked -$25.1878."""
    assert realized_from_kalshi_fills(CSKA_FILLS, CSKA_SETTLEMENT) == pytest.approx(TRUE_PNL, abs=1e-4)
    assert realized_from_kalshi_fills([], None) is None
    assert realized_from_kalshi_fills([CSKA_FILLS[0]], None) == pytest.approx(-COST, abs=1e-4)


def test_closing_fill_is_read_off_book_side_not_the_action_pair():
    """The trap this row sets: ``action: sell, outcome_side: no, no_price 0.77`` read literally says we
    took in $88.16. We did not — we sold 114.49 YES for $26.33. ``book_side: ask`` is the truth."""
    closing = realized_from_kalshi_fills([CSKA_FILLS[1]], None)
    assert closing == pytest.approx(114.49 * 0.23 - 1.4194, abs=1e-4)
    assert closing < 30.0, "read off action/outcome_side this comes out ~+$86.74"


def test_realized_includes_settlement_revenue_when_held_to_the_end():
    """A naked leg carried into settlement is paid in CENTS by Kalshi — 11449 means $114.49."""
    got = realized_from_kalshi_fills([CSKA_FILLS[0]], {"ticker": TICKER, "revenue": 11449})
    assert got == pytest.approx(114.49 - COST, abs=1e-4)


class _TruthKalshi(_KalshiExec):
    """Kalshi that can answer 'what did this actually come to' — fills + settlements."""

    def __init__(self, fills, settlement=None, **kw):
        super().__init__(**kw)
        self._fills = fills
        self._settlement = settlement

    def get_fills(self, *, ticker=None, **kw):
        return [f for f in self._fills if ticker is None or f.get("ticker") == ticker]

    def get_settlements(self, *, limit=None):
        return {"settlements": [self._settlement] if self._settlement else []}


def test_orphan_booked_at_worst_case_is_rebooked_at_venue_truth(tmp_path):
    """THE RULE. While the position is open the day carries the conservative -$25.19. The moment the
    venue says we are flat, the day is corrected to the real -$0.27 and lifetime records the truth."""
    ex, koc, kex, poly = _cska_executor(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.30)
    c = _cand_kalshi(ticker=TICKER, htoken="TOKH")
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    lo = ex.open_orders[c.key]

    # Force the incident's own failure: the unwind cannot flatten (STP), so the orphan latches.
    kex.unwind_flattens = False
    kex.positions[TICKER] = FILL_SHARES
    ex._unwind_and_record(lo, FILL_SHARES, FILL_PRICE, None, "hedge_unwound", _DT)

    assert ex.orphan is not None and ex.caps.halted is True
    assert ex.caps.pnl_today == pytest.approx(-COST, abs=1e-3)   # worst case, while it is still open
    assert TICKER in ex._provisional

    # A human cashes it out. The venue now knows the answer; nothing else changed.
    fills_before = ex.caps.fills_today
    ex.kalshi = _TruthKalshi(CSKA_FILLS, CSKA_SETTLEMENT)
    ex.kalshi.positions[TICKER] = 0.0
    rows = ex.settle_provisional_marks(_DT)

    assert len(rows) == 1
    assert rows[0]["realized_pnl_usd"] == pytest.approx(TRUE_PNL, abs=1e-3)
    assert rows[0]["correction_usd"] == pytest.approx(COST + TRUE_PNL, abs=1e-3)
    assert ex.caps.pnl_today == pytest.approx(TRUE_PNL, abs=1e-3)
    assert TICKER not in ex._provisional, "a corrected mark must never be applied twice"
    assert ex.settle_provisional_marks(_DT) == []
    assert ex.caps.fills_today == fills_before, \
        "a bookkeeping restatement must not spend one of the day's max_fills_per_day slots"


def test_a_still_open_position_keeps_its_conservative_mark(tmp_path):
    """Only a RESOLVED position gets rebooked — an open one must not be flattered."""
    ex, koc, kex, poly = _cska_executor(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.30)
    c = _cand_kalshi(ticker=TICKER, htoken="TOKH")
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    lo = ex.open_orders[c.key]
    kex.unwind_flattens = False
    kex.positions[TICKER] = FILL_SHARES
    ex._unwind_and_record(lo, FILL_SHARES, FILL_PRICE, None, "hedge_unwound", _DT)

    ex.kalshi = _TruthKalshi(CSKA_FILLS, CSKA_SETTLEMENT)
    ex.kalshi.positions[TICKER] = FILL_SHARES                    # still holding
    assert ex.settle_provisional_marks(_DT) == []
    assert ex.caps.pnl_today == pytest.approx(-COST, abs=1e-3)


def test_provisional_marks_survive_a_restart(tmp_path, monkeypatch):
    """The maker restarts ~10x/day and positions resolve hours later. A mark that dies with the process
    is a permanent overstatement."""
    monkeypatch.setattr(mrt_config, "OPS_DIR", str(tmp_path))
    monkeypatch.setattr(mrt_config, "GENZ_DIR", str(tmp_path))
    ex, koc, kex, poly = _cska_executor(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.30)
    c = _cand_kalshi(ticker=TICKER, htoken="TOKH")
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    lo = ex.open_orders[c.key]
    kex.unwind_flattens = False
    kex.positions[TICKER] = FILL_SHARES
    ex._unwind_and_record(lo, FILL_SHARES, FILL_PRICE, None, "hedge_unwound", _DT)

    ex2, _, _, _ = _cska_executor(tmp_path)
    assert TICKER in ex2._provisional
    assert ex2._provisional[TICKER]["booked_pnl"] == pytest.approx(-COST, abs=1e-3)


# --------------------------------------------------------------------------- #
# 4. Bounded AUTO-FLATTEN — the end of the three-hour halt                       #
# --------------------------------------------------------------------------- #
def _orphan_case(tmp_path, *, cap, flattens=True):
    ex, koc, kex, poly = _cska_executor(tmp_path, auto_flatten_max_usd=cap)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.30)
    c = _cand_kalshi(ticker=TICKER, htoken="TOKH")
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    lo = ex.open_orders[c.key]
    kex.unwind_flattens = False                       # the FIRST unwind fails (that's what makes it an orphan)
    kex.positions[TICKER] = FILL_SHARES

    real_unwind = ex._verified_unwind
    calls = {"n": 0}

    def _staged(lo_, shares, price):                  # 1st call = the failed unwind; 2nd = the flatten
        calls["n"] += 1
        if calls["n"] >= 2 and flattens:
            kex.unwind_flattens = True
        return real_unwind(lo_, shares, price)

    ex._verified_unwind = _staged
    res = ex._unwind_and_record(lo, FILL_SHARES, FILL_PRICE, None, "hedge_unwound", _DT)
    return ex, kex, res


def test_small_orphan_auto_flattens_and_keeps_trading(tmp_path):
    """$25.19 at risk, well under the $120 ceiling: sweep it out, prove flat, carry on."""
    ex, kex, res = _orphan_case(tmp_path, cap=120.0)
    assert ex.orphan is None, "a bounded, flattened orphan must not halt"
    assert ex.caps.halted is False
    assert abs(kex.positions[TICKER]) < MIN_TRADABLE["kalshi"]
    events = [r["event"] for r in ex.state.rows]
    assert "auto_flattened" in events


def test_orphan_above_the_ceiling_still_halts(tmp_path):
    """The ceiling is the whole point: unbounded exposure still stops the bot for a human."""
    ex, kex, res = _orphan_case(tmp_path, cap=10.0)              # $25.19 > $10
    assert ex.orphan is not None
    assert ex.caps.halted is True and ex.caps.halt_reason == "orphan_position"


def test_auto_flatten_disabled_by_zero_still_halts(tmp_path):
    ex, kex, res = _orphan_case(tmp_path, cap=0.0)
    assert ex.orphan is not None and ex.caps.halted is True


def test_daily_caps_survive_a_hand_edited_file_with_a_bom(tmp_path, monkeypatch):
    """Repairing the fill counter by hand on 2026-07-28 wrote the file with PowerShell's Out-File, which
    prepends a BOM. json.load raised at position 0, the failure was swallowed, and the maker started the
    day at zero and re-persisted it — $329.96 of committed stake gone and the whole daily budget reopened.
    Reading utf-8-sig costs nothing and accepts both shapes."""
    monkeypatch.setattr(mrt_config, "OPS_DIR", str(tmp_path))
    monkeypatch.setattr(mrt_config, "GENZ_DIR", str(tmp_path))
    from src.genz.maker_rt.state import utcnow
    payload = ('{"day": "%s", "stake_today": 329.9584, "fills_today": 7, "pnl_today": 1.4586}'
               % utcnow().strftime("%Y%m%d"))
    (tmp_path / "maker_rt_daily_caps.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    ex, _, _, _ = _cska_executor(tmp_path)
    assert ex.caps.stake_today == pytest.approx(329.9584)
    assert ex.caps.fills_today == 7
    assert ex.caps.pnl_today == pytest.approx(1.4586)


def test_unreadable_daily_caps_screams_instead_of_silently_reopening_the_budget(tmp_path, monkeypatch):
    """A file we cannot read means this process trades with a fresh stake/loss budget. That may be
    unavoidable; being quiet about it is not."""
    monkeypatch.setattr(mrt_config, "OPS_DIR", str(tmp_path))
    monkeypatch.setattr(mrt_config, "GENZ_DIR", str(tmp_path))
    (tmp_path / "maker_rt_daily_caps.json").write_text("{not json at all", encoding="utf-8")

    class _Log:
        def __init__(self):
            self.errors = []

        def info(self, m, *a):
            pass
        warning = info

        def error(self, m, *a):
            self.errors.append(m % a if a else m)

    log = _Log()
    from src.genz.maker_rt.caps import LiveCaps
    cfg = mrt_config.MakerRtConfig()
    cfg.live.enabled = True
    PregameLiveExecutor(cfg, gate=None, order_client=None, hedger=None, caps=LiveCaps(cfg.live),
                        poly=_Poly(), telegram=None, state=None, log=log)
    assert any("COULD NOT RESTORE" in e and "REOPENED" in e for e in log.errors), log.errors


def test_auto_flatten_failure_fails_closed(tmp_path):
    """If the sweep cannot PROVE flat, we are still naked — halt exactly as before."""
    ex, kex, res = _orphan_case(tmp_path, cap=120.0, flattens=False)
    assert ex.orphan is not None
    assert ex.caps.halted is True
    assert "auto_flattened" not in [r["event"] for r in ex.state.rows]
