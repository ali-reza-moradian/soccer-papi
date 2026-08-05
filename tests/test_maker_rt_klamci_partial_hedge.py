"""Regression tests for the 2026-08-05 KLAMCI PARTIAL-HEDGE incident.

THE INCIDENT. 05:51:16Z we rested 115 Over 5.5 @32c on Kalshi (KXCLUBFTOTAL-26AUG05KLAMCI-6). It filled
in full at 05:58:26Z. The Poly hedge — a FAK market BUY of the Under — filled only 80 of 115 for $54.48,
and the executor correctly computed a 35-share naked remainder. Then two things went wrong:

  1. NOTHING REGISTERED THE 80. The partial branch committed the stake, advanced ``hedged_seen`` and went
     straight to the unwind. ``_record_hedge_locked`` — the only path that writes the expected-position
     registry, the settled-pnl cost basis and the watch-set — is reached ONLY on a full hedge. So 80 real
     Under shares existed in no registry at all.
  2. THE UNWIND SOLD THE HEDGED LEG TOO. ``_verified_unwind_kalshi`` asked for 35, got them, re-read the
     position, saw the 80 contracts we were still holding ON PURPOSE, called that an unsold remainder and
     sold those as well (venue truth: order 1f475433 for 35 at 05:58:34Z, then f131dcd7 for 80 at
     05:58:37Z — 115 of a 115-share fill). The Kalshi leg was gone; its Poly counterpart was naked.

The 80 Under shares then rode a live match for nine hours, invisible to orphan detection, to the 5-minute
reconcile and to the maker-position line of the 8-hourly audit — all of which watch REGISTERED
instruments only. The match finished Under, so the naked position paid $80.00 against a $54.48 cost. It
was luck, not edge: had the pair been booked as a pair it would have settled at about -$0.08.

These tests replay the chain against a Kalshi fake that models the position honestly (a sell of N
decrements by N — the production fake flattened to zero on any sell, which is precisely the behaviour
that hid this bug), and pin the class: a partial hedge is a smaller PAIR plus a smaller UNWIND, and the
unwind stops at the shares we hold on purpose.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.genz.maker_rt.pregame_exec import PregameLiveExecutor

from .test_maker_rt_pregame import (_Guard, _KalshiOC, _Log, _State, _Store, _cand_kalshi, _dec,
                                    _exec_kalshi)

NOW = datetime(2026, 8, 5, 5, 58, 26, tzinfo=timezone.utc)
#: ``now_ts`` is UNIX seconds everywhere in this system (the fill sweep hands it straight to Kalshi as
#: ``min_ts``), and the 48h quoted scope ages off it — so the fixtures use the real epoch, not 1.0/2.0.
TS = NOW.timestamp()

TICKER = "KXCLUBFTOTAL-26AUG05KLAMCI-6"
TOKEN = "287693742604610352837246736397494293828572832807781106618027981215337538513"
FILL_SHARES = 115.0
FILL_PRICE = 0.32
HEDGE_SHARES = 80.0
HEDGE_PRICE = 0.681055                      # $54.4844 / 80 sh, from the venue's own makingAmount/taking


# --------------------------------------------------------------------------- #
# fakes that model the venues HONESTLY (the point of the whole incident)        #
# --------------------------------------------------------------------------- #
class _Kalshi:
    """KalshiExec stand-in whose position actually tracks the sells. ``unwind_flattens`` in the shared
    fake sets the position to 0.0 on ANY sell, which cannot express "I sold 35 of 115"."""

    def __init__(self, *, sell_price=0.30, position=0.0):
        self.market_sells: list = []
        self.sell_price = sell_price
        self.positions: dict = {}
        self.position = position
        self._n = 0

    def place_market_sell(self, ticker, side, count, client_order_id=None):
        self._n += 1
        n = min(float(count), float(self.positions.get(ticker, 0.0)))
        self.market_sells.append({"ticker": ticker, "side": side, "count": count, "filled": n})
        self.positions[ticker] = round(float(self.positions.get(ticker, 0.0)) - n, 6)
        return {"status": "filled", "fill_count": n, "avg_price": self.sell_price,
                "order_id": f"unwind-oid-{self._n}"}

    def get_positions(self):
        return {"market_positions": [{"ticker": t, "position": p} for t, p in self.positions.items()]}

    def cancel_all(self):
        return 0


class _PolyBal:
    """PolyExec stand-in with a per-token balance the hedge/unwind actually move."""

    def __init__(self, balances=None, sell_price=0.66):
        self.balances = dict(balances or {})
        self.market_sells: list = []
        self.sell_price = sell_price
        self.cancel_all_calls = 0

    def conditional_balance(self, token_id):
        return float(self.balances.get(str(token_id), 0.0))

    def place_market_sell(self, token, shares):
        n = min(float(shares), self.balances.get(str(token), 0.0))
        self.market_sells.append({"token": token, "shares": shares, "filled": n})
        self.balances[str(token)] = round(self.balances.get(str(token), 0.0) - n, 6)
        return {"status": "filled", "avg_price": self.sell_price, "shares": n}

    def _tick_and_negrisk(self, token):
        return ("0.01", False)

    def get_order(self, oid):
        return {"status": "CANCELED", "size_matched": 0.0}

    def cancel_all(self):
        self.cancel_all_calls += 1
        return {"canceled": []}


class _PartialHedger:
    """LiveHedger stand-in: the Poly FAK fills ``fill_ratio`` of what it was asked for, and CREDITS those
    shares to the wallet exactly as the venue does."""

    def __init__(self, poly, *, token=TOKEN, fill_ratio=HEDGE_SHARES / FILL_SHARES, price=HEDGE_PRICE):
        self.poly = poly
        self.token = token
        self.fill_ratio = fill_ratio
        self.price = price
        self.calls: list = []

    def hedge_poly(self, fill, spec):
        self.calls.append({"fill": dict(fill), "spec": dict(spec)})
        want = float(fill.get("size") or 0.0)
        got = round(want * self.fill_ratio, 6)
        tok = str(spec.get("token") or self.token)
        self.poly.balances[tok] = round(self.poly.balances.get(tok, 0.0) + got, 6)
        if got >= want - 1e-9:
            return SimpleNamespace(status="locked", hedged_shares=got, hedge_avg_price=self.price,
                                   hedge_fee=0.0, locked_pnl=0.0, detail={"poly": {"order_id": "H1"}})
        return SimpleNamespace(status=("partial" if got > 0 else "missed"), hedged_shares=got,
                               hedge_avg_price=(self.price if got > 0 else None), hedge_fee=0.0,
                               locked_pnl=None, freeze_market=True, detail={"poly": {"order_id": "H1"}})

    def hedge(self, fill, spec):                    # rest-poly direction; unused here
        raise AssertionError("the KLAMCI chain is rest-kalshi")


def _klamci(tmp_path, *, fill_ratio=HEDGE_SHARES / FILL_SHARES, fill_shares=FILL_SHARES,
            hedge_price=HEDGE_PRICE, kalshi_sell_price=0.30):
    """Rest 115 Over @32c on Kalshi, fill it, hedge it PARTIALLY on Poly. Returns the whole rig."""
    koc = _KalshiOC()
    kx = _Kalshi(sell_price=kalshi_sell_price)
    poly = _PolyBal()
    hedger = _PartialHedger(poly, fill_ratio=fill_ratio, price=hedge_price)
    state = _State()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx, poly=poly, hedger=hedger, state=state)
    ex.in_flight = _Guard()
    ex.log = _Log()
    ex.roll_day(NOW)
    c = _cand_kalshi(ticker=TICKER, side="yes", htoken=TOKEN)
    # A book on which 115 shares of Under walk at ~0.65 — the pre-hedge gate must APPROVE (as it did live
    # at +2.2%), so the chain reaches the hedge rather than declining before it.
    store = _Store(poly_best_ask=0.65, kalshi_ask=0.34)
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.65), None, store, NOW, TS, "pre")
    oid = ex.kalshi_order_client.rests[-1]["oid"]
    ex.open_orders[c.key].size = float(fill_shares)     # the 115-lot the incident actually rested
    kx.positions[TICKER] = float(fill_shares)          # the venue now shows the filled rest leg
    ex._on_fill_detected(c.key, float(fill_shares), FILL_PRICE, store, NOW, TS + 1)
    return SimpleNamespace(ex=ex, koc=koc, kx=kx, poly=poly, hedger=hedger, state=state, oid=oid, c=c)


def _sold(kx):
    return round(sum(s["filled"] for s in kx.market_sells), 6)


# --------------------------------------------------------------------------- #
# 1. THE INCIDENT, replayed: 80-pair registered, 35 unwound, nothing stranded    #
# --------------------------------------------------------------------------- #
def test_klamci_books_the_80_pair_and_unwinds_only_the_35(tmp_path):
    r = _klamci(tmp_path)
    assert r.ex._expected_shares("kalshi", TICKER) == pytest.approx(HEDGE_SHARES), \
        "the 80 hedged Kalshi contracts are a leg we HOLD ON PURPOSE and must be registered"
    assert r.ex._expected_shares("polymarket", TOKEN) == pytest.approx(HEDGE_SHARES), \
        "the 80 Poly Under shares are the other half of that pair — the leg that went missing"
    assert _sold(r.kx) == pytest.approx(FILL_SHARES - HEDGE_SHARES), \
        "ONLY the 35 naked contracts may be sold; 115 were sold on the day"
    assert r.kx.positions[TICKER] == pytest.approx(HEDGE_SHARES), "the hedged leg survives the unwind"


def test_klamci_leaves_zero_unregistered_shares(tmp_path):
    """The invariant the incident broke: every share the venues hold is accounted for somewhere."""
    r = _klamci(tmp_path)
    held_k = r.kx.positions[TICKER]
    held_p = r.poly.conditional_balance(TOKEN)
    assert held_k - r.ex._expected_shares("kalshi", TICKER) == pytest.approx(0.0, abs=0.5)
    assert held_p - r.ex._expected_shares("polymarket", TOKEN) == pytest.approx(0.0, abs=0.5)
    assert TOKEN in r.ex._traded_tokens or r.ex._expected_shares("polymarket", TOKEN) > 0, \
        "the hedge token must be reachable by reconciliation"


def test_klamci_ledgers_both_halves(tmp_path):
    r = _klamci(tmp_path)
    events = [row["event"] for row in r.state.rows]
    assert "fill" in events
    assert "hedge_locked" in events, "the filled 80 are a pair and are ledgered as one"
    assert "hedge_unwound" in events, "the naked 35 are still an exit and are ledgered as one"
    pair = next(r for r in r.state.rows if r["event"] == "hedge_locked")
    assert pair["size"] == pytest.approx(HEDGE_SHARES), "the pair row describes 80 shares, not 115"


def test_klamci_counts_one_fill_not_two(tmp_path):
    """fills_today is a daily CAP. One venue execution that both locks and unwinds must spend one slot."""
    r = _klamci(tmp_path)
    assert r.ex.caps.fills_today == 1


def test_klamci_names_the_split_in_the_log(tmp_path):
    r = _klamci(tmp_path)
    assert any("PARTIAL HEDGE" in w and "unwinding only" in w for w in r.ex.log.warns)


# --------------------------------------------------------------------------- #
# 2. the reconcile-to-flat loop: "flat" is not "zero"                           #
# --------------------------------------------------------------------------- #
def test_unwind_stops_at_the_shares_we_hold_on_purpose(tmp_path):
    """THE second half of the bug, isolated. A venue read that still shows the hedged leg is not an
    unsold remainder — selling it is what stranded the Poly side."""
    r = _klamci(tmp_path)
    assert len(r.kx.market_sells) == 1, "one sell, for the naked amount — not a chase down to zero"
    assert r.kx.market_sells[0]["count"] == 35


def test_a_second_fills_unwind_does_not_liquidate_the_first_fills_hedge(tmp_path):
    """The same shape with NO partial anywhere: fill #1 hedges in full, fill #2 misses. The unwind of #2
    must not sell #1's rest leg. This would have fired on any multi-fill order."""
    koc = _KalshiOC()
    kx = _Kalshi(sell_price=0.30)
    poly = _PolyBal()
    hedger = _PartialHedger(poly, fill_ratio=1.0, price=0.66)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx, poly=poly, hedger=hedger, state=_State())
    ex.in_flight, ex.log = _Guard(), _Log()
    ex.roll_day(NOW)
    c = _cand_kalshi(ticker=TICKER, side="yes", htoken=TOKEN)
    store = _Store(poly_best_ask=0.65, kalshi_ask=0.34)
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.65), None, store, NOW, TS, "pre")
    ex.open_orders[c.key].size = 100.0
    kx.positions[TICKER] = 60.0                       # fill #1: 60 contracts, hedged in full
    ex._on_fill_detected(c.key, 60.0, FILL_PRICE, store, NOW, TS + 1)
    assert ex._expected_shares("kalshi", TICKER) == pytest.approx(60.0)
    hedger.fill_ratio = 0.0                           # fill #2: 40 more, hedge MISSES entirely
    kx.positions[TICKER] = 100.0
    ex._on_fill_detected(c.key, 100.0, FILL_PRICE, store, NOW, TS + 2)
    assert _sold(kx) == pytest.approx(40.0), "only fill #2's 40 naked contracts are sold"
    assert kx.positions[TICKER] == pytest.approx(60.0), "fill #1's hedged leg is untouched"


# --------------------------------------------------------------------------- #
# 3. property: FAK fills at 0 / 30 / 70 / 100 %                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ratio", [0.0, 0.3, 0.7, 1.0])
def test_every_fak_fill_ratio_conserves_shares(tmp_path, ratio):
    """paired + unwound == filled, and NOTHING is left unregistered — at any hedge fill ratio."""
    r = _klamci(tmp_path, fill_ratio=ratio)
    paired = r.ex._expected_shares("kalshi", TICKER)
    unwound = _sold(r.kx)
    assert paired + unwound == pytest.approx(FILL_SHARES, abs=1.0), \
        f"every one of the {FILL_SHARES} filled shares is either paired or sold (ratio {ratio})"
    assert r.ex._expected_shares("polymarket", TOKEN) == pytest.approx(
        r.poly.conditional_balance(TOKEN), abs=0.5), "the Poly leg registered == the Poly leg held"
    assert r.kx.positions[TICKER] == pytest.approx(paired, abs=0.5), \
        "the Kalshi position left behind is exactly the registered pair"


@pytest.mark.parametrize("ratio", [0.0, 0.3, 0.7, 1.0])
def test_no_fak_fill_ratio_leaves_an_orphan_or_a_halt(tmp_path, ratio):
    r = _klamci(tmp_path, fill_ratio=ratio)
    assert r.ex.orphan is None, f"ratio {ratio} must not latch an orphan"
    assert r.ex.caps.halted is False, f"ratio {ratio} must not halt the bot"


# --------------------------------------------------------------------------- #
# 4. the unwind's ledger row states the size that actually left the account      #
# --------------------------------------------------------------------------- #
def test_hedge_unwound_records_the_total_actually_sold(tmp_path):
    """On the day the row said 35 while 115 had been sold. The cost was right; the label was not, and the
    label is what a human reconstructs an incident from."""
    r = _klamci(tmp_path)
    row = next(x for x in r.state.rows if x["event"] == "hedge_unwound")
    assert row["size"] == pytest.approx(_sold(r.kx)), "the row's size IS the total unwound"


def test_hedge_unwound_size_follows_a_multi_sweep_unwind(tmp_path):
    """A thin book that needs two sweeps still reports the true total, not the first ask."""
    r = _klamci(tmp_path, fill_ratio=0.0)                 # whole 115 naked
    row = next(x for x in r.state.rows if x["event"] == "hedge_unwound")
    assert row["size"] == pytest.approx(_sold(r.kx)) == pytest.approx(FILL_SHARES)


# --------------------------------------------------------------------------- #
# 5. our own exits recognise themselves in the account fill sweep                #
# --------------------------------------------------------------------------- #
def test_our_unwind_fills_are_not_reported_as_untracked(tmp_path):
    """The unwind's executions came back through /portfolio/fills on order ids we never rested and were
    ledgered as three ``fill_untracked`` rows — the one bucket that is supposed to mean 'a position we did
    not know about'."""
    r = _klamci(tmp_path)
    unwind_oid = r.kx.market_sells[0].get("count") and "unwind-oid-1"
    r.ex.poll_kalshi_fills(_Store(), NOW, 100.0)          # prime the dedupe set
    r.koc.fills = [{"fill_id": "f1", "order_id": unwind_oid, "count_fp": "35.00", "side": "no",
                    "no_price_dollars": "0.7000", "ticker": TICKER}]
    r.ex.poll_kalshi_fills(_Store(), NOW, 110.0)
    events = [x["event"] for x in r.state.rows]
    assert "unwind_confirmed" in events, "our own exit is labelled as our own exit"
    assert "fill_untracked" not in events, "and never as an unexplained surprise"
    assert r.ex.orphan is None and r.ex.caps.halted is False


def test_a_genuinely_unknown_fill_is_still_untracked_and_still_halts(tmp_path):
    """The self-recognition must not blind the check it sits in front of."""
    r = _klamci(tmp_path)
    r.ex.poll_kalshi_fills(_Store(), NOW, 100.0)
    r.ex._unwind_inflight.clear()                          # the exit window has long since closed
    r.kx.positions["KX-OTHER"] = 9.0
    r.koc.fills = [{"fill_id": "f9", "order_id": "a-stranger", "count_fp": "9.00", "side": "yes",
                    "yes_price_dollars": "0.5000", "ticker": "KX-OTHER"}]
    r.ex.poll_kalshi_fills(_Store(), NOW, 110.0)
    assert any(x["event"] == "fill_untracked" for x in r.state.rows)
    assert r.ex.orphan is not None and r.ex.caps.halted is True


def test_own_exit_marker_expires(tmp_path):
    r = _klamci(tmp_path)
    assert r.ex._is_own_exit(None, TICKER) is True
    r.ex._unwind_inflight[TICKER] = 0.0                    # epoch 0 == long expired
    assert r.ex._is_own_exit(None, TICKER) is False
    assert r.ex._is_own_exit("unwind-oid-1", TICKER) is True, "the order id is remembered regardless"


# --------------------------------------------------------------------------- #
# 6. the blind spot: a position nothing registered is now FOUND                  #
# --------------------------------------------------------------------------- #
def _quoted_only(tmp_path):
    """An executor that has QUOTED KLAMCI but has registered nothing — the state the old code left."""
    koc = _KalshiOC()
    kx = _Kalshi()
    poly = _PolyBal()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx, poly=poly, state=_State())
    ex.in_flight, ex.log = _Guard(), _Log()
    ex.roll_day(NOW)
    sent: list = []
    ex._send_telegram = sent.append
    c = _cand_kalshi(ticker=TICKER, side="yes", htoken=TOKEN)
    ex.place_or_reprice(c, _dec(FILL_PRICE, hedge_ask=0.65), None,
                        _Store(poly_best_ask=0.65, kalshi_ask=0.34), NOW, TS, "pre")
    for k in list(ex.open_orders):                        # drop the quote; keep only the 48h scope
        ex.open_orders.pop(k)
    ex._traded_tokens.clear()
    ex._traded_tickers.clear()
    ex._expected.clear()
    return ex, poly, kx, sent


def test_the_sweep_finds_the_unregistered_poly_leg(tmp_path):
    """The 2026-08-05 state, exactly: 80 Under shares in the wallet, nothing in any registry."""
    ex, poly, _kx, sent = _quoted_only(tmp_path)
    poly.balances[TOKEN] = 80.0
    found = ex._sweep_unregistered(NOW)
    assert [f["instrument"] for f in found] == [TOKEN]
    assert found[0]["shares"] == pytest.approx(80.0)
    assert any("UNTRACKED POSITION" in m for m in sent), "a RED alert, naming the market"
    assert any("G1" in m or "ml2" in m or "Home" in m for m in sent), "and it names WHICH market"


def test_the_sweep_alerts_but_does_not_halt(tmp_path):
    """An unregistered position needs a human, not a stopped maker — halting over one that may well be
    perfectly hedged trades a silent failure for a loud useless one."""
    ex, poly, _kx, _sent = _quoted_only(tmp_path)
    poly.balances[TOKEN] = 80.0
    ex._sweep_unregistered(NOW)
    assert ex.caps.halted is False and ex.orphan is None


def test_the_sweep_ignores_positions_on_markets_we_never_quoted(tmp_path):
    """This funder wallet holds ~180 positions that are not ours. None of them may ever trigger this."""
    ex, poly, _kx, sent = _quoted_only(tmp_path)
    poly.balances["some-other-bitcoin-token"] = 5000.0
    assert ex._sweep_unregistered(NOW) == []
    assert sent == []


def test_the_sweep_is_quiet_about_registered_legs(tmp_path):
    """A properly booked pair is not a surprise — the sweep must not double-report what reconciliation
    already watches, or it becomes noise nobody reads."""
    r = _klamci(tmp_path)
    sent: list = []
    r.ex._send_telegram = sent.append
    assert r.ex._sweep_unregistered(NOW) == []
    assert sent == []


def test_the_sweep_scope_ages_out(tmp_path):
    ex, poly, _kx, sent = _quoted_only(tmp_path)
    poly.balances[TOKEN] = 80.0
    for rec in ex._quoted.values():
        rec["ts"] = 1.0                                    # quoted at the dawn of the epoch
    assert ex._sweep_unregistered(NOW) == []
    assert sent == []


def test_the_sweep_re_alert_is_throttled(tmp_path):
    ex, poly, _kx, sent = _quoted_only(tmp_path)
    poly.balances[TOKEN] = 80.0
    ex._sweep_unregistered(NOW)
    ex._sweep_unregistered(NOW)
    assert len([m for m in sent if "UNTRACKED POSITION" in m]) == 1


def test_the_quoted_scope_covers_both_legs_and_survives_a_restart(tmp_path):
    ex, _poly, _kx, _sent = _quoted_only(tmp_path)
    assert set(ex._quoted) == {TICKER, TOKEN}, "the leg we rest AND the leg we would hedge into"
    ex2, _ = _exec_kalshi(tmp_path, state=_State())
    assert set(ex2._quoted) == {TICKER, TOKEN}, "a deploy must not reopen the blind spot"
