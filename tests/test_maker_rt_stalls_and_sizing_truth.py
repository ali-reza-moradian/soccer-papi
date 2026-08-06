"""T1 (off-loop stalls) + T3 (a refusal must name its true cause), 2026-08-06.

T1 — MEASURED: the reconcile watch-set reached 179 poly + 149 kalshi instruments, and
``_kalshi_position`` did ONE FULL ``GET /portfolio/positions`` PER TICKER. At a measured p50 of 380ms
that is 56.6s of a 65.1s pass, against a 45s worker deadline — so the pass ALWAYS blew its deadline,
the worker abandoned its thread, and the FILL POLL queued behind it went down with it. The fill poll is
the REST authority behind the websockets; when it stalls we are on sockets alone, which is exactly the
shape of the 2026-07-23 invisible-fills incident.

T3 — the in-play refusal at 0.94 that looked like broken arithmetic ("projected pair $0.00", "limiter
daily_stake" on a market with 9,713 of hedge depth) was TRUE: in-play's ring-fenced pool had $0.82 left
of $500 ($118.64 spent + $380.54 reserved by two open quotes). The money was right and the explanation
was wrong, which is worse than useless — it sends a human hunting a bug that is not there.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.genz.maker_rt.caps import plan_size

from .test_maker_rt_pregame import (_Guard, _Hedger, _KalshiOC, _Poly, _State, _Store, _cand, _dec,
                                    _exec_kalshi)

_DT = datetime(2026, 8, 6, 19, 21, 35, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# T1. ONE portfolio fetch per reconcile, not one per ticker                     #
# --------------------------------------------------------------------------- #
class _CountingKalshi:
    """Counts full-portfolio fetches, so the N+1 cannot come back unnoticed."""

    def __init__(self, positions=None, fail=False):
        self.calls = 0
        self.fail = fail
        self._positions = positions or {}

    def get_positions(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("venue unreachable")
        return {"market_positions": [{"ticker": t, "position": p}
                                     for t, p in self._positions.items()]}

    def get_balance(self):
        return {"balance_dollars": "1000.0000"}


def _ex(tmp_path, kalshi=None, poly=None):
    poly = poly or _Poly()
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                     hedge_fee=0.01, locked_pnl=0.11, unwind_cost=None, detail={}),
                     poly=poly)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=_KalshiOC(), kalshi=kalshi or _CountingKalshi(),
                         poly=poly, hedger=hedger, state=_State())
    ex.in_flight = _Guard()
    ex.roll_day(_DT)
    return ex, poly


def test_the_reconcile_job_fetches_the_kalshi_portfolio_EXACTLY_ONCE(tmp_path):
    """THE FIX. 149 tickers used to mean 149 full portfolio fetches — 56.6s of a 65.1s pass."""
    kx = _CountingKalshi({f"KX-{i}": float(i) for i in range(60)})
    ex, _poly = _ex(tmp_path, kalshi=kx)
    tickers = [f"KX-{i}" for i in range(60)]

    out = ex._reconcile_job([], tickers)

    assert kx.calls == 1, f"one fetch for 60 tickers, got {kx.calls}"
    assert out["kalshi"]["KX-7"] == 7.0
    assert out["kalshi"]["KX-59"] == 59.0


def test_a_ticker_absent_from_a_READABLE_portfolio_is_flat_not_unknown(tmp_path):
    kx = _CountingKalshi({"KX-1": 5.0})
    ex, _poly = _ex(tmp_path, kalshi=kx)
    out = ex._reconcile_job([], ["KX-1", "KX-ABSENT"])
    assert out["kalshi"]["KX-1"] == 5.0
    assert out["kalshi"]["KX-ABSENT"] == 0.0, "absent from a readable portfolio == genuinely flat"


def test_an_UNREADABLE_portfolio_marks_every_ticker_unknown_never_flat(tmp_path):
    """THE 2026-07-23 LESSON, preserved through the batching: 'we could not ask' must never arrive as
    'the venue says zero' — that is what let reconciliation prune real naked positions."""
    from src.genz.maker_rt.pregame_exec import _UNREAD
    kx = _CountingKalshi(fail=True)
    ex, _poly = _ex(tmp_path, kalshi=kx)
    out = ex._reconcile_job([], ["KX-1", "KX-2"])
    assert out["kalshi"]["KX-1"] is _UNREAD
    assert out["kalshi"]["KX-2"] is _UNREAD
    assert 0.0 not in out["kalshi"].values(), "an unreadable venue must NEVER read as flat"


def test_an_unparseable_row_is_unknown_not_flat(tmp_path):
    """A row we FOUND but cannot read is None (not flat) — unchanged by the batching."""
    class _Weird(_CountingKalshi):
        def get_positions(self):
            self.calls += 1
            return {"market_positions": [{"ticker": "KX-1", "some_future_name": "50.00"}]}
    ex, _poly = _ex(tmp_path, kalshi=_Weird())
    out = ex._reconcile_job([], ["KX-1"])
    assert out["kalshi"]["KX-1"] is None


def test_single_ticker_reads_still_work_for_the_non_batch_callers(tmp_path):
    """``_kalshi_position`` is still used one-at-a-time by the fill sweep and the re-verify guard."""
    kx = _CountingKalshi({"KX-9": 12.0})
    ex, _poly = _ex(tmp_path, kalshi=kx)
    assert ex._kalshi_position("KX-9") == 12.0
    assert ex._kalshi_position("KX-NONE") == 0.0


def test_gates_caches_immutable_files_and_returns_the_same_answer(tmp_path):
    """The gates scan covered 1.99 GB in 97.7s standalone and blew its 180s deadline under GIL
    contention, so the report never landed. Past-day files never change; only today's is re-read."""
    import csv
    import io

    from src.genz.maker_rt import gates
    f = tmp_path / "maker_rt_20260805.csv"
    with io.open(f, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ts", "mode", "event", "sport", "locked_net",
                                           "realized_pnl_usd"])
        w.writeheader()
        w.writerow({"ts": "2026-08-05T00:00:00Z", "mode": "live", "event": "hedge_locked",
                    "sport": "soccer", "locked_net": "0.8", "realized_pnl_usd": "0.4"})
    gates._ROW_CACHE.clear()
    first = gates._rows([str(f)])
    second = gates._rows([str(f)])
    assert len(first) == len(second) == 1
    assert first[0]["_locked"] == second[0]["_locked"] == 0.8
    assert str(f) in gates._ROW_CACHE, "the file was cached"

    # a REWRITTEN file must invalidate: size+mtime are in the key
    with io.open(f, "a", encoding="utf-8", newline="") as fh:
        fh.write("2026-08-05T01:00:00Z,live,hedge_locked,soccer,0.9,0.5\n")
    assert len(gates._rows([str(f)])) == 2, "a changed file is re-read, not served stale"


# --------------------------------------------------------------------------- #
# T3. A REFUSAL MUST NAME ITS TRUE CAUSE                                        #
# --------------------------------------------------------------------------- #
def test_the_0_94_refusal_reproduces_and_the_limiter_is_daily_stake():
    """The exact 19:21:35Z shape: price 0.94, deep hedge book, and a pool with $0.82 left. The refusal
    was TRUE — `below_venue_minimum` was the symptom, `daily_stake` the cause."""
    p = plan_size(0.94, 0.05, quote_usd_max=210.0, max_pair_stake_usd=1050.0,
                  daily_stake_headroom=0.82, hedge_depth=9713, book_depth=117385, venue_minimum=5)
    assert p["refused"] is True
    assert p["limiter"] == "daily_stake", "the pool, not the venue floor, is what bound it"
    assert p["size"] == 0

    # ...and with the pool genuinely free, the SAME inputs place a full-size quote.
    q = plan_size(0.94, 0.05, quote_usd_max=210.0, max_pair_stake_usd=1050.0,
                  daily_stake_headroom=381.0, hedge_depth=9713, book_depth=117385, venue_minimum=5)
    assert q["refused"] is False and q["size"] == 223 and q["limiter"] == "quote_usd_max"


# 0.99 is deliberately absent: a 0.99 maker bid needs an ask ABOVE it, and there is none on a
# $0..$1 book, so the never-cross guard refuses it before the sizer is ever reached. 0.98 is the
# genuine top extreme for a resting bid.
@pytest.mark.parametrize("price", [0.01, 0.02, 0.50, 0.94, 0.97, 0.98])
def test_the_refusal_reason_is_TRUE_at_every_price(tmp_path, price):
    """THE PROPERTY: when the sizer refuses, the reason it records must be the constraint that actually
    bound — checked across the price extremes, where the venue-minimum arithmetic changes shape
    (Poly's floor is max(5, ceil($1/price)), so it is 100 shares at 1c and 5 at 94c)."""
    ex, _poly = _ex(tmp_path)
    ex.caps.inplay_pool_usd = 500.0
    ex.roll_day(_DT)
    ex.caps.commit_stake(499.18, "inplay")             # the live 19:21Z state: $0.82 left
    store = _Store(poly_best_ask=price + 0.01, kalshi_ask=max(0.01, 0.99 - price))
    ex.place_or_reprice(_cand(), _dec(price=price, hedge_ask=max(0.01, 0.99 - price)),
                        None, store, _DT, 1.0, "inplay")

    assert ex.order_client.rests == [], f"the pool has $0.82 — nothing may place at {price}"
    rows = [r for r in ex.state.rows
            if str(r.get("reason") or "").startswith("below_venue_minimum")]
    assert rows, f"a refusal was recorded at {price}"
    reason = rows[-1]["reason"]
    assert reason == "below_venue_minimum:daily_stake", (
        f"at {price} the reason must name the pool that actually bound, got {reason!r}")


def test_a_genuine_venue_minimum_refusal_still_names_its_own_cause(tmp_path):
    """The control: when the REST-LEG CAP is what makes the size too small, the reason must say so and
    NOT blame the pool. A reason that always said the same thing would be no better than the symptom."""
    ex, _poly = _ex(tmp_path)
    ex.caps.quote_usd_max = 0.01                       # the leg cap, not the pool, is the binder
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    rows = [r for r in ex.state.rows
            if str(r.get("reason") or "").startswith("below_venue_minimum")]
    assert rows and rows[-1]["reason"] == "below_venue_minimum:quote_usd_max"


def test_the_refuse_log_reports_the_pool_arithmetic_not_a_zero(tmp_path):
    """It logged "projected pair $0.00" because no size exists yet at that point. The zero is what made
    a correct refusal look like broken arithmetic."""
    from .test_maker_rt_pregame import _Log
    ex, _poly = _ex(tmp_path)
    ex.log = _Log()
    ex.caps.inplay_pool_usd = 500.0
    ex.roll_day(_DT)
    ex.caps.commit_stake(499.18, "inplay")
    store = _Store(poly_best_ask=0.96, kalshi_ask=0.05)
    ex.place_or_reprice(_cand(), _dec(price=0.94, hedge_ask=0.05), None, store, _DT, 1.0, "inplay")

    refuse = [m for m in ex.log.infos + ex.log.warns if "REFUSE" in m and "max fittable" in m]
    assert refuse, "the sizing refusal was logged"
    msg = refuse[-1]
    assert "limiter daily_stake" in msg
    assert "pool $0.82 free of $500" in msg, f"the log must show the pool arithmetic: {msg}"
    assert "reserved" in msg and "spent" in msg
    # and the throttled one-liner no longer claims a projected pair it does not have
    line = [m for m in ex.log.infos + ex.log.warns
            if "REFUSE" in m and "below_venue_minimum:" in m]
    if line:
        assert "projected pair $0.00" not in line[-1]
