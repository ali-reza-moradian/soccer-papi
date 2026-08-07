"""Regression tests for the 2026-07-24 FALSE-ORPHAN halt (3rd occurrence).

At 06:59:38Z a rest-poly fill hedged cleanly on Kalshi (LOCKED +$0.07, hedge 41c). Seven seconds later
the account fill-sweep saw THAT VERY HEDGE fill on an order id it never rested, read the Kalshi
position as 5 contracts, and screamed:

    "UNTRACKED venue fill (order e3c01ca5-...) on a NON-FLAT position (read=5.0)"  -> HALT everything.

The hedge leg is an EXPECTED position until its market settles. The fix registers every rest leg AND
every live hedge as an expected position the moment the pair locks (persisted across restarts), and
reconciliation only screams for a holding that EXCEEDS what we expect to hold. These tests replay the
exact chain and pin the surrounding behaviour so the false halt cannot come back.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.genz.maker_rt.pregame_exec import PregameLiveExecutor

from .test_maker_rt_pregame import (_Guard, _Hedger, _KalshiExec, _KalshiOC, _Log, _Poly, _State,
                                    _Store, _cand, _dec, _exec_kalshi, deliver_poly)

NOW = datetime(2026, 7, 24, 6, 59, 38, tzinfo=timezone.utc)
#: books on which a 0.46 rest-poly fill hedges cleanly on Kalshi at 0.53 (pair $0.99/share,
#: locked ~+1%), i.e. the LOCKED path. The pair MUST sum to about $1.00: the booking invariant
#: (book_refuse_reason) refuses anything outside it, so a fixture priced 0.46/0.41 would quarantine.
_HEDGEABLE = _Store(poly_best_ask=0.60, kalshi_ask=0.50)


def _locked_rest_poly_pair(tmp_path, *, rest_token="TOKR", hedge_ticker="KX-1"):
    """Place a rest-poly order, fill it, and let it hedge+lock on Kalshi. Returns (ex, koc, kx, poly)."""
    koc = _KalshiOC()
    kx = _KalshiExec()
    poly = _Poly()
    hedger = _Hedger(__import__("types").SimpleNamespace(status="locked", hedged_shares=5,
                     hedge_avg_price=0.53, locked_pnl=0.05, unwind_cost=None), poly=poly)
    state = _State()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx, poly=poly, hedger=hedger, state=state)
    ex.in_flight = _Guard()
    ex.roll_day(NOW)
    c = _cand(direction="rest-poly", token=rest_token)
    c.hedge_lookup = {"venue": "kalshi", "ticker": hedge_ticker, "side": "yes"}
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, _HEDGEABLE, NOW, 1.0, "pre")
    oid = ex.order_client.rests[-1]["oid"]
    deliver_poly(poly, 5)                       # VENUE TRUTH: the rest leg really was delivered
    ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.46}, _HEDGEABLE, NOW, 2.0)
    return ex, koc, kx, poly, state


# --------------------------------------------------------------------------- #
# 1. a locked pair registers BOTH legs as expected positions                    #
# --------------------------------------------------------------------------- #
def test_locking_a_pair_registers_rest_and_hedge_as_expected(tmp_path):
    ex, _koc, _kx, _poly, _state = _locked_rest_poly_pair(tmp_path)
    assert ex._expected_shares("polymarket", "TOKR") == 5.0, "the filled rest leg is expected"
    assert ex._expected_shares("kalshi", "KX-1") == 5.0, "the live hedge leg is expected"
    assert ex.caps.fills_today == 1 and ex.orphan is None


# --------------------------------------------------------------------------- #
# 2. THE incident: the hedge fill lands in the sweep -> NO halt                  #
# --------------------------------------------------------------------------- #
def test_hedge_fill_in_account_sweep_is_not_a_false_orphan(tmp_path):
    """rest-poly fill -> kalshi hedge -> the hedge fill reaches poll_kalshi_fills untracked -> NO halt."""
    ex, koc, kx, _poly, state = _locked_rest_poly_pair(tmp_path)
    ex.log = _Log()
    kx.positions["KX-1"] = 5.0                        # the venue shows the hedge we deliberately hold
    ex.poll_kalshi_fills(_Store(), NOW, 100.0)        # prime the dedupe set (no fills yet)
    koc.fills = [{"fill_id": "hedgefill", "order_id": "e3c01ca5-hedge", "count_fp": "5.00",
                  "side": "yes", "yes_price_dollars": "0.5300", "ticker": "KX-1"}]
    ex.poll_kalshi_fills(_Store(), NOW, 110.0)
    assert ex.orphan is None, "our own hedge must NOT latch a false orphan"
    assert ex.caps.halted is False, "the bot must stay ARMED"
    # The hedge leg matches a REGISTERED expected position -> it is booked hedge_confirmed, NOT dumped in
    # the untracked bucket (the ZHELAN/TORBOS mislabel fix). Only genuinely-naked fills are fill_untracked.
    assert any(r["event"] == "hedge_confirmed" for r in state.rows), "the hedge is ledgered as confirmed"
    assert not any(r["event"] == "fill_untracked" for r in state.rows), "an expected hedge is NOT untracked"
    assert any("EXPECTED" in i and "no orphan" in i for i in ex.log.infos), "explained-by-expected logged"


# --------------------------------------------------------------------------- #
# 3. the 5-min reconcile does not flag the held rest+hedge legs                  #
# --------------------------------------------------------------------------- #
def test_reconcile_does_not_flag_held_expected_legs(tmp_path):
    ex, _koc, kx, poly, _state = _locked_rest_poly_pair(tmp_path)
    poly.position = 5.0                               # we hold the poly rest leg
    kx.positions["KX-1"] = 5.0                        # ...and the kalshi hedge
    ex._traded_tickers.add("KX-1")                    # (the sweep adds the ticker to the watch-set)
    assert ex.reconcile_positions(NOW) is None, "held rest+hedge legs are expected, not orphans"
    assert ex.caps.halted is False


# --------------------------------------------------------------------------- #
# 4. a REAL naked surplus BEYOND expected still halts (guard didn't blind us)    #
# --------------------------------------------------------------------------- #
def test_reconcile_still_flags_surplus_beyond_expected(tmp_path):
    ex, _koc, kx, _poly, _state = _locked_rest_poly_pair(tmp_path)
    ex._traded_tickers.add("KX-1")
    kx.positions["KX-1"] = 8.0                        # expected 5, but the venue shows 8 -> 3 unexplained
    orph = ex.reconcile_positions(NOW)
    assert orph is not None, "a holding beyond the expected hedge is a genuine orphan"
    assert ex.caps.halted is True


def test_untracked_fill_beyond_expected_still_halts(tmp_path):
    """A fill leaving us with MORE than the expected hedge is still naked on the surplus."""
    ex, koc, kx, _poly, _state = _locked_rest_poly_pair(tmp_path)
    ex.log = _Log()
    kx.positions["KX-1"] = 12.0                       # expected 5, venue shows 12 -> 7 unexplained
    ex.poll_kalshi_fills(_Store(), NOW, 100.0)
    koc.fills = [{"fill_id": "surprise", "order_id": "ghost", "count_fp": "7.00", "side": "yes",
                  "yes_price_dollars": "0.5300", "ticker": "KX-1"}]
    ex.poll_kalshi_fills(_Store(), NOW, 110.0)
    assert ex.orphan is not None and ex.caps.halted is True


# --------------------------------------------------------------------------- #
# 5. expected positions survive a restart and clear on settlement                #
# --------------------------------------------------------------------------- #
def test_expected_positions_persist_across_restart(tmp_path):
    ex, _koc, _kx, _poly, _state = _locked_rest_poly_pair(tmp_path)
    assert ex._expected_shares("kalshi", "KX-1") == 5.0
    # A brand-new executor over the SAME ops dir (a restart) reloads the expected registry.
    ex2, _cfg = _exec_kalshi(tmp_path)
    assert ex2._expected_shares("kalshi", "KX-1") == 5.0, "expected legs must survive a restart"
    assert ex2._expected_shares("polymarket", "TOKR") == 5.0


def test_settlement_releases_only_the_legs_the_venue_says_are_gone(tmp_path):
    """Settlement releases a leg as it REDEEMS, not on the settlement alone.

    This used to drop both legs the moment the market settled, on the premise that "the winning leg
    redeemed to cash (balance -> 0)". False: between Kalshi settling and Polymarket resolving, the Poly
    leg is still really in the wallet — and deleting the registration of a position we really hold is
    what made the sweep scream UNTRACKED after every won pair (26AUG06CFRTIL x3, 26AUG07AVLBMU)."""
    ex, _koc, kx, poly, _state = _locked_rest_poly_pair(tmp_path)
    # Seed the settlement of BOTH legs' market (Kalshi HAL won) and reconcile. (Production passes the
    # Kalshi client into the constructor; this fake injects it after, so point the reconciler at it.)
    kx.get_settlements = lambda **_kw: {"settlements": [{"ticker": "KX-1", "market_result": "yes",
                                                         "revenue": 500}]}
    ex._settle_reconciler.kalshi = kx
    poly.position = 5.0                                  # Polymarket has NOT redeemed yet
    ex.reconcile_settlements(NOW)
    assert ex._expected_shares("kalshi", "KX-1") == 0.0, "settled AND flat -> released"
    assert ex._expected_shares("polymarket", "TOKR") == 5.0, "settled but still HELD -> still ours"
    # ...and the conservatism is not a leak: redemption lands, and the next reconcile lets it go.
    poly.position = 0.0
    ex._release_settled_expected()
    assert ex._expected_shares("polymarket", "TOKR") == 0.0
    assert "TOKR" not in ex._traded_tokens


def test_settled_losing_poly_leg_is_not_reorphaned(tmp_path):
    """The stranded-HANHAL class: after settlement the LOSING Poly leg is worthless but its token
    balance stays non-zero in the wallet. The next reconcile must NOT read it as a naked orphan.

    That used to be achieved by FORGETTING the leg. The leg now stays REGISTERED instead (see above),
    so the protection comes from the other side of the same subtraction: ``abs(position) - expected``
    is zero, so there is nothing unexplained to orphan on. Same invariant, different mechanism — and
    this asserts the invariant, which is the part that matters."""
    ex, _koc, kx, poly, _state = _locked_rest_poly_pair(tmp_path)
    assert "TOKR" in ex._traded_tokens, "the rest leg is watched while the pair is open"
    kx.get_settlements = lambda **_kw: {"settlements": [{"ticker": "KX-1", "market_result": "yes",
                                                         "revenue": 500}]}   # HAL won -> hanfmann lost
    ex._settle_reconciler.kalshi = kx
    poly.position = 5.0            # the worthless losing tokens stay in the wallet forever
    ex.reconcile_settlements(NOW)
    assert ex.reconcile_positions(NOW) is None, "explained, not unexplained"
    assert ex.caps.halted is False and ex.orphan is None
    assert ex._is_registered("polymarket", "TOKR") is True, "and not an UNTRACKED surprise either"
