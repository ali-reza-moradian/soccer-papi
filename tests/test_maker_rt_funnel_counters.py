"""The PLACEMENT FUNNEL counters — pure instrumentation, and it must stay pure.

Why they exist: on 2026-08-06 the in-play engine was armed, clearing 0.5% edge FOUR TIMES more often
than pre-game, and placing ZERO orders — and nothing this build wrote could say where the evaluations
went. Every refusal signal was either quote-CONDITIONAL (``_expire_if_open`` writes nothing for a
candidate that never armed, so a path that never quotes is invisible by construction) or unevenly
throttled (``below_venue_minimum`` wrote a CSV row every tick while slot refusals wrote one per 300s,
which made the two look 3x apart when they were not). These counters are one increment per evaluation
at the branch that consumed it.

The tests that matter here are the NEGATIVE ones: a counter that changes what the maker does is not a
counter. See ``test_counters_change_nothing_*``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.genz.maker_rt.state import MakerState

from .test_maker_rt_pregame import (_Guard, _KalshiOC, _Poly, _State, _Store, _cand, _dec,
                                    _exec_kalshi, deliver_poly)

_DT = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. the counter itself                                                        #
# --------------------------------------------------------------------------- #
def test_record_funnel_accumulates_per_sport_and_phase():
    st = MakerState()
    st.record_funnel("soccer", "inplay", "eval", _DT)
    st.record_funnel("soccer", "inplay", "eval", _DT)
    st.record_funnel("soccer", "inplay", "size_refused:daily_stake", _DT)
    st.record_funnel("soccer", "pre", "eval", _DT)
    st.record_funnel("mlb", "inplay", "eval", _DT)

    ip = st._bucket("soccer", "inplay").funnel
    assert ip == {"eval": 2, "size_refused:daily_stake": 1}
    assert st._bucket("soccer", "pre").funnel == {"eval": 1}
    assert st._bucket("mlb", "inplay").funnel == {"eval": 1}


def test_funnel_reaches_the_summary_sorted_by_size():
    st = MakerState()
    for _ in range(5):
        st.record_funnel("soccer", "inplay", "eval", _DT)
    st.record_funnel("soccer", "inplay", "PLACED", _DT)
    for _ in range(3):
        st.record_funnel("soccer", "inplay", "cap_refused:max_open_quotes", _DT)

    stats = st._bucket("soccer", "inplay").stats()
    assert stats["funnel"] == {"eval": 5, "cap_refused:max_open_quotes": 3, "PLACED": 1}
    assert list(stats["funnel"]) == ["eval", "cap_refused:max_open_quotes", "PLACED"], "biggest first"


def test_funnel_survives_a_restart():
    """A funnel that resets on every restart cannot answer 'where did today's evaluations go' — this
    process restarts 10-21 times on a working day."""
    st = MakerState()
    st.record_funnel("soccer", "inplay", "eval", _DT)
    st.record_funnel("soccer", "inplay", "size_refused:daily_stake", _DT)
    saved = st._bucket("soccer", "inplay").achievable_state()

    st2 = MakerState()
    st2._bucket("soccer", "inplay").load_achievable_state(saved)
    assert st2._bucket("soccer", "inplay").funnel == {"eval": 1, "size_refused:daily_stake": 1}


def test_a_bad_stage_value_never_raises_into_the_quote_loop():
    st = MakerState()
    st.record_funnel(None, None, None, None)          # type: ignore[arg-type]
    st.record_funnel("soccer", "inplay", "ok", _DT)
    assert st._bucket("soccer", "inplay").funnel == {"ok": 1}


def test_pooling_sums_funnels_across_buckets():
    from src.genz.maker_rt.state import _pool
    st = MakerState()
    st.record_funnel("soccer", "inplay", "eval", _DT)
    st.record_funnel("mlb", "inplay", "eval", _DT)
    st.record_funnel("mlb", "inplay", "PLACED", _DT)
    pooled = _pool([st._bucket("soccer", "inplay"), st._bucket("mlb", "inplay")])
    assert pooled["funnel"] == {"eval": 2, "PLACED": 1}


# --------------------------------------------------------------------------- #
# 2. THE POINT: counting must change nothing                                   #
# --------------------------------------------------------------------------- #
def _armed(tmp_path, poly=None):
    poly = poly or _Poly()
    hedger_res = SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                 hedge_fee=0.01, locked_pnl=0.11, unwind_cost=None, detail={})
    from .test_maker_rt_pregame import _Hedger
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=_KalshiOC(), poly=poly,
                         hedger=_Hedger(hedger_res, poly=poly), state=_State())
    ex.in_flight = _Guard()
    ex.roll_day(_DT)
    return ex, poly


def test_counters_change_nothing_a_placement_still_places(tmp_path):
    """A candidate that placed before the counters still places, with the same order and the same size."""
    ex, poly = _armed(tmp_path)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.caps.quote_usd_max = 2.5
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    assert len(ex.order_client.rests) == 1
    assert ex.order_client.rests[0]["size"] == 5
    assert ex.caps.open_quotes == 1


def test_counters_change_nothing_a_refusal_still_refuses(tmp_path):
    """The daily budget fully committed -> the quote is still REFUSED, and the funnel names the LIMITER
    (`daily_stake`) rather than only the symptom (`below_venue_minimum`)."""
    ex, _poly = _armed(tmp_path)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    # Spend the PRE-GAME pool. Since the ring-fence, headroom is per-phase — setting only the global
    # ``stake_today`` would leave pre-game's own pool untouched and the quote would still place.
    ex.caps.commit_stake(ex.caps.max_daily_stake_usd, "pre")   # nothing left to spend
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")

    assert ex.order_client.rests == [], "a full budget must still refuse"
    fn = ex.state.funnel_calls
    assert any(s.startswith("size_refused:") for _sp, _ph, s in fn)
    assert any(s == "size_refused:daily_stake" for _sp, _ph, s in fn), \
        "the counter must carry the BINDING LIMITER, which is what the CSV row could never say"


def test_a_slot_refusal_is_counted_every_time_not_once_per_throttle_window(tmp_path):
    """THE MEASUREMENT BUG THIS FIXES. The CSV row for a slot refusal is throttled to one per 300s, so a
    funnel built from CSV rows under-counts it against the per-tick below_venue_minimum row it competes
    with. The counter must fire on EVERY refusal."""
    ex, _poly = _armed(tmp_path)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.caps.quote_usd_max = 2.5
    ex.caps.open_quotes = ex.caps.max_open_quotes          # every slot taken

    for i in range(5):
        ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0 + i, "pre")

    slot = [s for _sp, _ph, s in ex.state.funnel_calls if s.startswith("cap_refused:")]
    assert len(slot) == 5, f"expected one count per refusal, got {len(slot)}"
    assert set(slot) == {"cap_refused:max_open_quotes"}
    # ...while the throttled CSV row fired only once, which is exactly the discrepancy.
    rows = [r for r in ex.state.rows
            if r.get("event") == "expire" and r.get("reason") == "max_open_quotes"]
    assert len(rows) == 1, "the CSV row is still throttled — that is the behaviour being contrasted"


def test_placement_is_counted_as_PLACED_once(tmp_path):
    ex, _poly = _armed(tmp_path)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.caps.quote_usd_max = 2.5
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    placed = [s for _sp, _ph, s in ex.state.funnel_calls if s == "PLACED"]
    assert placed == ["PLACED"]


def test_the_executor_tolerates_no_state_at_all(tmp_path):
    """``state`` is optional on the executor; a counter must not be the thing that makes it required."""
    ex, _poly = _armed(tmp_path)
    ex.state = None
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.caps.quote_usd_max = 2.5
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    assert len(ex.order_client.rests) == 1, "placement still works with no state object"
