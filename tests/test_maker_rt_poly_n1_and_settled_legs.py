"""Two bugs that shared a victim: the reconcile's Polymarket N+1, and the alarm it kept crying wolf on.

THE N+1 (2026-08-06 23:59:52Z -> 2026-08-07 14:20Z, still live when it was found). Reconciliation read
Poly balances ONE CLOB GET PER TOKEN: 317-320 per pass, 175 passes, 54,065 requests in 14.6h (~1/sec
sustained). Cloudflare 429'd 6,466 of them — 12% — and answered each with a 137-line "error 1015 — rate
limited" HTML page that the client logged in full. Nothing halted and nothing traded wrong, which is
what made it easy to file as noise. It was not noise: a 429 raises, which reconciliation records as
_UNREAD, so ~12% of Poly position reads were BLIND on every pass. An unregistered position on a token
that happened to 429 was invisible that cycle — the exact blind spot the sweep exists to close. Same N+1
shape the Kalshi side shed in 5cbd190; same fix, one wallet snapshot per pass.

THE FALSE ALARM (26AUG06CFRTIL x3, then 26AUG07AVLBMU at 14:51Z). ``_forget_settled_instruments`` dropped
BOTH legs of a settled market on the premise that "the winning leg redeemed to cash (balance -> 0)". That
premise is false in the window between Kalshi settling and Polymarket resolving/redeeming. AVLBMU settled
+$1.82, and about two minutes later the sweep screamed "holding 295 shares on Polymarket that my own books
do not know about" — because the books that knew about it had just been deleted. It fired after EVERY
settled pair whose Poly leg won.

Both halves are tested against a rig that models the venues honestly, and the false-alarm half is pinned
from BOTH sides: a settled-but-unredeemed winning leg must stay quiet, and a genuinely naked position
must still scream. An alarm that cries wolf on every win is an alarm nobody reads, and this one is the
guard against the next KLAMCI.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.genz.maker_rt import balance as bal_mod
from src.genz.maker_rt.pregame_exec import _UNREAD

from .test_maker_rt_pregame import _Guard, _KalshiExec, _KalshiOC, _Log, _State, _exec_kalshi

NOW = datetime(2026, 8, 7, 14, 51, 0, tzinfo=timezone.utc)
TS = NOW.timestamp()

#: the AVLBMU pair, as it actually stood when the false alarm fired
GAME = "26AUG07AVLBMU"
MARKET = "total_goals|4.5"
POLY_TOKEN = "2104117667112381035964580919969294003959387808447923429939540961570562166272"
KALSHI_TICKER = "KXCLUBFTOTAL-26AUG07AVLBMU-5"
SHARES = 295.0


class _CountingPoly:
    """PolyExec stand-in that COUNTS per-token CLOB reads — the thing the N+1 fix is supposed to stop."""

    def __init__(self, balances=None):
        self.balances = dict(balances or {})
        self.reads: list = []
        self.raise_on_read = False

    def conditional_balance(self, token_id):
        self.reads.append(str(token_id))
        if self.raise_on_read:
            raise RuntimeError("429 Too Many Requests")
        return float(self.balances.get(str(token_id), 0.0))

    def get_order(self, oid):
        return {"status": "CANCELED", "size_matched": 0.0}

    def _tick_and_negrisk(self, token):
        return ("0.01", False)

    def cancel_all(self):
        return {"canceled": []}


def _wallet(monkeypatch, rows, *, truncated=False, boom=False):
    """Point the shared wallet reader at a fixture, and count how often it is called."""
    calls: list = []

    def fake():
        calls.append(1)
        if boom:
            raise RuntimeError("data api down")
        return [{"asset": a, "size": s} for a, s in rows.items()], truncated

    monkeypatch.setattr(bal_mod, "poly_wallet_positions", lambda **k: fake())
    return calls


def _rig(tmp_path, *, balances=None):
    poly = _CountingPoly(balances=balances)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=_KalshiOC(), kalshi=_KalshiExec(), poly=poly,
                         state=_State())
    ex.in_flight, ex.log = _Guard(), _Log()
    ex.roll_day(NOW)
    return ex, poly


# --------------------------------------------------------------------------- #
# 1. the N+1: one wallet snapshot, not one GET per token                        #
# --------------------------------------------------------------------------- #
def test_the_reconcile_reads_the_wallet_once_not_once_per_token(tmp_path, monkeypatch):
    ex, poly = _rig(tmp_path)
    toks = [f"tok{i}" for i in range(50)]
    calls = _wallet(monkeypatch, {t: 3.0 for t in toks})
    out = ex._reconcile_job(toks, [])
    assert len(calls) == 1, "ONE wallet snapshot for the whole pass"
    assert poly.reads == [], "and NOT one CLOB /balance-allowance GET per token"
    assert out["polymarket"]["tok7"] == pytest.approx(3.0)


def test_a_token_absent_from_a_complete_snapshot_is_flat(tmp_path, monkeypatch):
    """The data API drops rows under 0.1sh, so absence from a COMPLETE read means flat."""
    ex, _poly = _rig(tmp_path)
    _wallet(monkeypatch, {"held": 12.0})
    out = ex._reconcile_job(["held", "gone"], [])
    assert out["polymarket"]["held"] == pytest.approx(12.0)
    assert out["polymarket"]["gone"] == 0.0


def test_a_failed_wallet_read_is_unread_never_flat(tmp_path, monkeypatch):
    """'We could not ask' and 'the venue says zero' must not arrive as the same answer — that distinction
    is the whole reason a network blip cannot prune a watched leg or invent an orphan."""
    ex, _poly = _rig(tmp_path)
    _wallet(monkeypatch, {}, boom=True)
    out = ex._reconcile_job(["a", "b"], [])
    assert out["polymarket"] == {"a": _UNREAD, "b": _UNREAD}


def test_a_truncated_wallet_read_is_unread_never_flat(tmp_path, monkeypatch):
    """A truncated page walk UNDERSTATES the wallet. Reading the unseen remainder as zero would prune
    real legs and hide real positions — the one direction of error that costs money."""
    ex, _poly = _rig(tmp_path)
    _wallet(monkeypatch, {"seen": 5.0}, truncated=True)
    out = ex._reconcile_job(["seen", "unseen"], [])
    assert out["polymarket"] == {"seen": _UNREAD, "unseen": _UNREAD}


def test_an_expected_leg_absent_from_the_snapshot_gets_a_targeted_confirm(tmp_path, monkeypatch):
    """Absence is exactly the reading that would deregister a leg we hold on purpose, so where we EXPECT
    shares we pay for one read rather than trust a silence."""
    ex, poly = _rig(tmp_path, balances={POLY_TOKEN: SHARES})
    ex._register_expected("polymarket", POLY_TOKEN, "under", SHARES, GAME, MARKET, NOW)
    _wallet(monkeypatch, {"unrelated": 1.0})
    out = ex._reconcile_job([POLY_TOKEN, "unrelated"], [])
    assert poly.reads == [POLY_TOKEN], "one targeted confirm, only for the expected-but-absent leg"
    assert out["polymarket"][POLY_TOKEN] == pytest.approx(SHARES), "and it found the shares"


def test_the_targeted_confirm_budget_cannot_rebuild_the_n_plus_1(tmp_path, monkeypatch):
    """Over budget reads UNKNOWN, never 'flat' — a bounded blind spot beats an unbounded request storm,
    and beats a silent deregistration outright."""
    toks = [f"exp{i}" for i in range(25 + 15)]
    # every leg really holds 7 shares, so a "0.0" anywhere below would mean ASSUMED flat, not read flat
    ex, poly = _rig(tmp_path, balances={t: 7.0 for t in toks})
    assert len(toks) > ex.POLY_CONFIRM_MAX, "the fixture has to exceed the budget to test it"
    for t in toks:
        ex._register_expected("polymarket", t, "under", 10.0, GAME, MARKET, NOW)
    _wallet(monkeypatch, {})
    out = ex._reconcile_job(toks, [])
    assert len(poly.reads) == ex.POLY_CONFIRM_MAX, "the per-token path is BOUNDED"
    within = [out["polymarket"][t] for t in toks[:ex.POLY_CONFIRM_MAX]]
    beyond = [out["polymarket"][t] for t in toks[ex.POLY_CONFIRM_MAX:]]
    assert within == [pytest.approx(7.0)] * ex.POLY_CONFIRM_MAX, "confirmed legs carry VENUE truth"
    assert all(v is _UNREAD for v in beyond), "over budget is UNKNOWN — never silently 'flat'"


# --------------------------------------------------------------------------- #
# 2. the false alarm: a settled leg is not gone until the venue says it is      #
# --------------------------------------------------------------------------- #
def _settled_pair(tmp_path, *, poly_balance):
    """The AVLBMU state: a booked pair whose market has settled, with the Poly leg still in the wallet."""
    ex, poly = _rig(tmp_path, balances={POLY_TOKEN: poly_balance})
    ex._register_expected("polymarket", POLY_TOKEN, "under", SHARES, GAME, MARKET, NOW)
    ex._register_expected("kalshi", KALSHI_TICKER, "YES", SHARES, GAME, MARKET, NOW)
    ex._traded_tokens.add(POLY_TOKEN)
    ex._traded_tickers.add(KALSHI_TICKER)
    rec = {"game": GAME, "market_key": MARKET,
           "kalshi": {"ticker": KALSHI_TICKER}, "poly": {"token": POLY_TOKEN}}
    sent: list = []
    ex._send_telegram = sent.append
    return ex, poly, rec, sent


def test_a_settled_but_unredeemed_winning_leg_is_not_an_untracked_position(tmp_path):
    """THE 26AUG07AVLBMU FALSE ALARM, pinned. Kalshi has settled; Polymarket has not redeemed yet, so the
    295 shares are still really there. They are ours, we know about them, and saying otherwise two
    minutes after booking +$1.82 is how an alarm gets ignored."""
    ex, _poly, rec, sent = _settled_pair(tmp_path, poly_balance=SHARES)
    ex._prune_expected(GAME, MARKET)
    ex._forget_settled_instruments(rec)
    assert POLY_TOKEN in ex._traded_tokens, "still held -> still registered"
    assert ex._expected_shares_any(POLY_TOKEN) == pytest.approx(SHARES)
    assert ex._is_registered("polymarket", POLY_TOKEN) is True
    assert not [m for m in sent if "UNTRACKED" in m]


def test_the_settled_leg_that_really_is_gone_is_released(tmp_path):
    """The Kalshi leg DID settle to nothing, so it goes — the fix is 'prove it', not 'never let go'."""
    ex, _poly, rec, _sent = _settled_pair(tmp_path, poly_balance=SHARES)
    ex._prune_expected(GAME, MARKET)
    ex._forget_settled_instruments(rec)
    assert KALSHI_TICKER not in ex._traded_tickers, "reads flat -> deregistered"
    assert ex._expected_shares_any(KALSHI_TICKER) == 0.0


def test_the_kept_leg_is_released_once_it_finally_reads_flat(tmp_path):
    """Conservatism must not become a leak: the settled-pnl record that triggered the release is already
    consumed, so something has to keep re-asking. This runs on every reconcile, off the batch."""
    ex, poly, rec, _sent = _settled_pair(tmp_path, poly_balance=SHARES)
    ex._prune_expected(GAME, MARKET)
    ex._forget_settled_instruments(rec)
    assert POLY_TOKEN in ex._traded_tokens
    ex._settle_reconciler.mark_settled(GAME, MARKET)
    poly.balances[POLY_TOKEN] = 0.0                       # redemption lands
    ex._release_settled_expected()
    assert POLY_TOKEN not in ex._traded_tokens, "redeemed -> finally forgotten"
    assert ex._expected_shares_any(POLY_TOKEN) == 0.0


def test_an_unreadable_settled_leg_stays_registered(tmp_path):
    """'I could not ask' may never authorise a deregistration — this is the one decision where being
    wrong strips a real position of its safety net."""
    ex, poly, rec, _sent = _settled_pair(tmp_path, poly_balance=SHARES)
    poly.raise_on_read = True
    ex._prune_expected(GAME, MARKET)
    ex._forget_settled_instruments(rec)
    assert POLY_TOKEN in ex._traded_tokens
    assert ex._expected_shares_any(POLY_TOKEN) == pytest.approx(SHARES)


def _many_quoted(ex, n=200):
    for i in range(n):
        ex._quoted[f"tok{i}"] = {"venue": "polymarket", "game": GAME, "market_key": MARKET,
                                 "name": "x", "ts": TS}


def test_the_startup_reconcile_is_batched_like_the_periodic_one(tmp_path, monkeypatch):
    """THE HALF THE FIRST FIX MISSED. Startup called reconcile_positions with NO batch, which sends every
    read down the per-instrument path. The quoted scope was 581 instruments, so startup fired that many
    /balance-allowance GETs in a burst and Cloudflare 429'd 46 of them at 15:52:00Z on 2026-08-07 --
    AFTER the periodic pass had been fixed. Same N+1, second entrance."""
    ex, poly = _rig(tmp_path)
    _many_quoted(ex)
    calls = _wallet(monkeypatch, {"tok3": 40.0})
    ex.reconcile_startup(NOW)
    assert len(calls) == 1, "ONE wallet snapshot for the whole startup pass"
    assert poly.reads == [], "and NOT one CLOB read per quoted market"


def test_the_sweep_never_fetches_a_wallet_snapshot_itself(tmp_path, monkeypatch):
    """All reconciliation venue I/O belongs to _reconcile_job -- the one place a caller (and a test)
    controls by handing in `balances`. A sweep that fetched its own snapshot reached the network from
    inside the unit tests the moment another test happened to leave a signing key in the environment."""
    ex, _poly = _rig(tmp_path)
    _many_quoted(ex, 5)
    calls = _wallet(monkeypatch, {"tok1": 9.0})
    ex._sweep_unregistered(NOW, now_ts=TS)
    assert calls == [], "the sweep does no wallet I/O of its own"


def test_the_unbatched_sweep_stays_bounded(tmp_path, monkeypatch):
    """With no batch there is no way to know except live reads, so they are BUDGETED: a bounded blind
    spot beats a request storm, and over budget reads UNKNOWN rather than inventing a position."""
    ex, poly = _rig(tmp_path)
    _many_quoted(ex)
    found = ex._sweep_unregistered(NOW, now_ts=TS)
    assert len(poly.reads) <= ex.POLY_CONFIRM_MAX, "bounded, not 200"
    assert found == [], "nothing held -> nothing invented"


def test_a_genuinely_naked_position_still_screams(tmp_path):
    """THE CONTROL, and the reason this alarm exists. Quieting the false positive must not quiet the true
    one: a position on a market we quoted, in NO registry, is still a red alert naming the market."""
    ex, poly = _rig(tmp_path, balances={POLY_TOKEN: SHARES})
    sent: list = []
    ex._send_telegram = sent.append
    ex._quoted[POLY_TOKEN] = {"venue": "polymarket", "game": GAME, "market_key": MARKET,
                              "name": "Aston Villa vs Bayern Munich: O/U 4.5", "ts": TS}
    found = ex._sweep_unregistered(NOW, now_ts=TS)
    assert [f["instrument"] for f in found] == [POLY_TOKEN]
    assert found[0]["shares"] == pytest.approx(SHARES)
    assert any("UNTRACKED POSITION" in m for m in sent)
    assert any("Aston Villa" in m for m in sent), "and it names the match, not a token id"
