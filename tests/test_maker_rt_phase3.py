"""AUDIT PHASE 3 — concentration, exposure truth, edge measurement.

Phase 2 made the loop fast. Phase 3 is about what the caps were actually counting, and the answer in
three of these cases was "not the thing the cap was named after":

  * ``max_open_quotes`` counted ORDERS, not GAMES — twelve slots on one match is one bet sized twelve
    times, and a single goal settles all of them together (N15).
  * ``max_daily_stake_usd`` checked only the ONE quote being placed, never the outstanding ones — 12
    slots x a $350 pair cap was $4,200 of committable exposure against an $800 "cap" (N14).
  * ``max_games`` was one nearest-by-kickoff queue that soccer's fixture density won outright: 97.5% of
    the observed universe, with UFC at queue position 172 (H4).

Every test here pins a REFUSAL or a COUNT on the incident's own numbers rather than a duration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt import gates
from src.genz.maker_rt.caps import LiveCaps, plan_size
from src.genz.maker_rt.universe import select_games

from .test_maker_rt_pregame import _Log, _Store, _cand, _dec, _exec

NOW = datetime(2026, 7, 30, 19, 0, 0, tzinfo=timezone.utc)
_STORE = _Store(kalshi_ask=0.60, poly_best_ask=0.55)
_PDEC = lambda: _dec(0.38, hedge_ask=0.60)          # noqa: E731 — hedge ask matches the store's book


def _live_exec(tmp_path, **caps):
    ex, _ = _exec(tmp_path)
    ex.roll_day(NOW)
    ex.log = _Log()
    for k, v in caps.items():
        setattr(ex.caps, k, v)
    return ex


def _c(game: str, market: str = "ml2", token: str = "TOK"):
    c = _cand(token=f"{token}-{game}-{market}")
    c.key = ("soccer", game, market, "Home", "rest-poly")
    c.game, c.sport, c.market_key = game, "soccer", market
    return c


# --------------------------------------------------------------------------- #
# N15 — per-game concentration                                                  #
# --------------------------------------------------------------------------- #
def test_one_game_cannot_hold_more_than_the_per_game_cap(tmp_path):
    """The 2026-07-29 shape: one match absorbed 112 placements across six correlated totals lines. The
    global slot cap has nothing to say about that — every one of them is 'within max_open_quotes'."""
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    ex.max_open_per_game = 3
    for i in range(6):
        ex.place_or_reprice(_c("BOUFCA", f"total_goals|{i}.5"), _PDEC(), None, _STORE, NOW, 100.0 + i, "pre")
    assert ex.open_on_game("BOUFCA") == 3, "the 4th, 5th and 6th line of one match were refused"
    assert ex.caps.open_quotes == 3
    refusals = [r for r in ex.state.rows if r.get("reason") == "max_open_per_game"]
    assert refusals, "and the refusal is RECORDED, not silent"


def test_a_different_game_still_gets_its_slots(tmp_path):
    """A per-GAME cap must not behave like a smaller global cap."""
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    ex.max_open_per_game = 3
    for i in range(4):
        ex.place_or_reprice(_c("BOUFCA", f"t|{i}"), _PDEC(), None, _STORE, NOW, 100.0 + i, "pre")
    for i in range(4):
        ex.place_or_reprice(_c("PAFHAJ", f"t|{i}"), _PDEC(), None, _STORE, NOW, 200.0 + i, "pre")
    assert ex.open_on_game("BOUFCA") == 3 and ex.open_on_game("PAFHAJ") == 3
    assert ex.caps.open_quotes == 6


def test_a_reprice_is_not_a_new_order_for_the_per_game_cap(tmp_path):
    """A reprice REPLACES one of this game's quotes, so it cannot raise concentration — refusing it
    would pin a game's quotes at whatever price they first rested at, which is worse than the cap."""
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    ex.max_open_per_game = 1
    c = _c("BOUFCA", "t|1")
    ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 100.0, "pre")
    assert ex.open_on_game("BOUFCA") == 1
    better = _dec(0.42, hedge_ask=0.60)
    ex.place_or_reprice(c, better, None, _STORE, NOW, 100.0 + ex.min_rest_s + 1, "pre")
    assert ex.open_on_game("BOUFCA") == 1
    assert ex.open_orders[c.key].price == 0.42, "the reprice went through"


def test_sizing_subtracts_what_the_game_already_committed(tmp_path):
    """The per-PAIR cap is enforced per quote, so six lines on one match are six separate 'within the
    cap' decisions. The game allowance is what makes them one decision."""
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20,
                    max_pair_stake_usd=100.0)
    ex.max_open_per_game = 3
    assert ex.game_stake_cap() == 300.0                      # 3 x the pair cap
    assert ex.game_stake_headroom("BOUFCA") == 300.0
    ex.place_or_reprice(_c("BOUFCA", "t|1"), _PDEC(), None, _STORE, NOW, 100.0, "pre")
    used = ex.game_reserved("BOUFCA")
    assert used > 0, "a resting quote commits its projected pair to the game"
    assert abs(ex.game_stake_headroom("BOUFCA") - (300.0 - used)) < 1e-6


def test_the_game_allowance_binds_sizing(tmp_path):
    """plan_size reports game_cap as the binding constraint when the game is what limits the size."""
    plan = plan_size(0.40, 0.55, quote_usd_max=1000.0, max_pair_stake_usd=1000.0,
                     daily_stake_headroom=1000.0, game_stake_headroom=19.0,
                     hedge_depth=None, book_depth=None, venue_minimum=5)
    assert plan["binding"] == "game_cap"
    assert plan["size"] == 20                                # 19 / (0.40+0.55) = 20.0


def test_the_per_game_cap_is_off_when_set_to_zero(tmp_path):
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    ex.max_open_per_game = 0
    for i in range(5):
        ex.place_or_reprice(_c("BOUFCA", f"t|{i}"), _PDEC(), None, _STORE, NOW, 100.0 + i, "pre")
    assert ex.open_on_game("BOUFCA") == 5
    assert ex.game_stake_headroom("BOUFCA") is None


# --------------------------------------------------------------------------- #
# N14 — exposure-true stake reservation                                         #
# --------------------------------------------------------------------------- #
def test_open_orders_reserve_their_projected_pair_against_the_daily_cap():
    """The measured hole: the daily cap checked ONE quote against money already SPENT, so twelve slots
    at a $350 pair cap were $4,200 of committable exposure against an $800 'cap'. A resting order is a
    promise to spend its pair cost; it is counted from the moment it rests."""
    caps = LiveCaps(mrt_config.LiveConfig(max_daily_stake_usd=800.0, max_open_quotes=12,
                                          max_pair_stake_usd=350.0, max_fills_per_day=20))
    for _ in range(2):
        assert caps.can_place(350.0)[0] is True
        caps.on_open(350.0)
    assert caps.committed_stake() == 700.0
    ok, reason = caps.can_place(350.0)
    assert ok is False and reason == "daily_stake_reserved", "the third $350 pair would be $1,050 of $800"
    assert caps.halted is False, "a reservation is not a spent budget — it must not halt the day"


def test_a_cancelled_quote_gives_its_reservation_back():
    caps = LiveCaps(mrt_config.LiveConfig(max_daily_stake_usd=800.0, max_open_quotes=12,
                                          max_pair_stake_usd=350.0, max_fills_per_day=20))
    caps.on_open(350.0)
    caps.on_open(350.0)
    assert caps.can_place(350.0)[0] is False
    caps.on_close(350.0)
    assert caps.can_place(350.0)[0] is True
    assert caps.reserved_stake == 350.0


def test_a_fill_converts_a_reservation_into_real_stake_without_double_counting():
    """on_close releases the hold; commit_stake books the legs. Both firing must not spend it twice."""
    caps = LiveCaps(mrt_config.LiveConfig(max_daily_stake_usd=800.0, max_open_quotes=12,
                                          max_pair_stake_usd=350.0, max_fills_per_day=20))
    caps.on_open(300.0)
    caps.on_close(300.0)                                    # the quote filled and stopped resting
    caps.commit_stake(120.0)                                 # the legs that actually traded
    caps.commit_stake(140.0)
    assert caps.reserved_stake == 0.0
    assert caps.committed_stake() == 260.0, "the real legs, not the projection plus the real legs"


def test_spent_stake_still_halts_the_day():
    """The reservation path must not soften the rail that money ALREADY SPENT trips."""
    caps = LiveCaps(mrt_config.LiveConfig(max_daily_stake_usd=800.0, max_open_quotes=12,
                                          max_pair_stake_usd=350.0, max_fills_per_day=20))
    caps.commit_stake(700.0)
    ok, reason = caps.can_place(200.0)
    assert (ok, reason) == (False, "max_daily_stake_usd")
    assert caps.halted is True and caps.halt_reason == "max_daily_stake_usd"


def test_open_quotes_cannot_exceed_the_days_remaining_fills():
    """Every resting quote is a fill waiting to happen. Holding 12 opens against 3 remaining fills is a
    promise the caps cannot keep — 9 of them must be cancelled or breach the rail."""
    caps = LiveCaps(mrt_config.LiveConfig(max_daily_stake_usd=10_000.0, max_open_quotes=12,
                                          max_pair_stake_usd=350.0, max_fills_per_day=5))
    caps.fills_today = 3                                     # 2 fills left today
    caps.open_quotes = 2
    ok, reason = caps.can_place(10.0)
    assert (ok, reason) == (False, "fills_per_day_headroom")
    assert caps.halted is False, "it frees itself as quotes close — not a day-halt"
    caps.open_quotes = 1
    assert caps.can_place(10.0)[0] is True


# --------------------------------------------------------------------------- #
# H4 — the per-sport universe map                                               #
# --------------------------------------------------------------------------- #
def _kick(**per_sport):
    """{(sport, game): kickoff_ts} with each sport's games spaced so soccer is always nearest."""
    out, t = {}, 1000.0
    for sport, n in per_sport.items():
        for i in range(n):
            out[(sport, f"{sport}{i}")] = t
            t += 1.0
    return out


def test_one_queue_lets_the_densest_sport_take_everything():
    """The behaviour being replaced: soccer's fixture density wins a single nearest-by-kickoff queue
    outright. Measured live at 97.5% of the universe, with UFC at queue position 172."""
    keep = select_games(_kick(soccer=40, mlb=6, tennis=4, ufc=2), max_games=30)
    assert sum(1 for s, _ in keep if s == "soccer") == 30
    assert not [k for k in keep if k[0] in ("mlb", "tennis", "ufc")]


def test_the_per_sport_map_gives_every_sport_its_own_queue():
    keep = select_games(_kick(soccer=40, mlb=6, tennis=4, ufc=2), max_games=30,
                        per_sport={"soccer": 18, "mlb": 6, "tennis": 4, "ufc": 2})
    got = {s: sum(1 for a, _ in keep if a == s) for s in ("soccer", "mlb", "tennis", "ufc")}
    assert got == {"soccer": 18, "mlb": 6, "tennis": 4, "ufc": 2}
    assert len(keep) == 30, "the same TOTAL number of games, redistributed"


def test_within_a_sport_the_nearest_kickoffs_still_win():
    kick = {("soccer", "late"): 9000.0, ("soccer", "soon"): 10.0, ("soccer", "next"): 20.0}
    keep = select_games(kick, max_games=30, per_sport={"soccer": 2})
    assert keep == {("soccer", "soon"), ("soccer", "next")}


def test_an_unlisted_sport_is_not_deleted_by_omission():
    """A map that forgot to name a sport must not silently drop it — it competes for the global cap."""
    keep = select_games(_kick(soccer=5, cricket=3), max_games=30, per_sport={"soccer": 2})
    assert sum(1 for s, _ in keep if s == "cricket") == 3
    assert sum(1 for s, _ in keep if s == "soccer") == 2


def test_the_global_cap_still_backstops_the_per_sport_map():
    keep = select_games(_kick(soccer=20, mlb=20), max_games=5,
                        per_sport={"soccer": 18, "mlb": 18})
    assert len(keep) == 5


def test_the_shipped_map_sums_to_the_configured_total():
    """The default is a REDISTRIBUTION, not a universe raise — that would be a cap change."""
    cfg = mrt_config.load_maker_rt_config()
    assert sum(cfg.max_games_per_sport.values()) == cfg.max_games


# --------------------------------------------------------------------------- #
# F13 — per-sport live/shadow switches                                          #
# --------------------------------------------------------------------------- #
def test_the_switch_matrix():
    cfg = mrt_config.MakerRtConfig()
    cfg.sports = {"tennis": {"live": False, "live_inplay": True},
                  "mlb": {"live": True, "live_inplay": False}}
    assert cfg.sport_live("tennis", "pre") is False
    assert cfg.sport_live("tennis", "inplay") is True
    assert cfg.sport_live("mlb", "pre") is True
    assert cfg.sport_live("mlb", "inplay") is False
    assert cfg.sport_live("soccer", "pre") is True, "absent -> live, never disarmed by omission"
    assert cfg.sport_live("soccer", "inplay") is True


def test_a_sport_switched_off_places_nothing(tmp_path):
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    ex._sport_live = lambda sport, phase="pre": sport != "soccer"
    c = _c("BOUFCA", "t|1")
    assert ex.eligible(c, "pre") is False
    c2 = _c("NYYBOS", "t|1")
    c2.sport = "mlb"
    assert ex.eligible(c2, "pre") is True


def test_a_switched_off_sport_still_measures(tmp_path):
    """The point of switching a sport off is to stop RISKING on it, not to stop MEASURING it — the
    ladder that would justify turning it back on has to keep filling."""
    from src.genz.maker_rt.state import MakerState
    st = MakerState(log=_Log())
    for _ in range(50):
        st.record_achievable("tennis", "pre", 0.004, NOW, rails_ok=True)
    assert st.summary("live", {}, NOW)["by_sport"]["tennis"]["achievable"]["n"] == 50


def test_an_unreadable_switch_never_disarms_a_live_sport(tmp_path):
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)

    def _boom(sport, phase="pre"):
        raise RuntimeError("config went away")
    ex._sport_live = _boom
    assert ex.sport_live("soccer", "pre") is True


# --------------------------------------------------------------------------- #
# N28 — the hedge-drift reprice (the CERBVB stale-quote pickoff)                 #
# --------------------------------------------------------------------------- #
class _DriftStore(_Store):
    """A store whose HEDGE ladder can be thinned/moved while the rest book sits perfectly still."""

    def __init__(self, *, hedge_ladder=None, **kw):
        super().__init__(**kw)
        self.hedge_ladder = hedge_ladder if hedge_ladder is not None else [(0.60, 5000)]

    def kalshi_view(self, ticker, side):
        return SimpleNamespace(best_ask=self.hedge_ladder[0][0],
                               best_bid=self.hedge_ladder[0][0] - 0.01,
                               ask_ladder=list(self.hedge_ladder))


def test_a_quote_whose_hedge_moved_under_it_is_pulled(tmp_path):
    """CERBVB: the same price re-rested for ~9h and was taken 486s after its last placement, by which
    point the hedge cost had moved and the pair was already a loss. The existing floor check compares
    our price to a floor solved at the hedge's BEST ASK, so a ladder that thinned below the top level
    is invisible to it. This walks the book for the size we are actually resting."""
    store = _DriftStore(poly_best_ask=0.55, hedge_ladder=[(0.60, 5000)])
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    c = _c("CERBVB", "t|1")
    ex.place_or_reprice(c, _PDEC(), None, store, NOW, 100.0, "pre")
    lo = ex.open_orders[c.key]
    assert ex.hedge_drift_breaches_floor(lo, store) is False, "healthy book: no drift breach"
    # The hedge ladder moves away — same rest book, same resting price, a pair that no longer locks.
    store.hedge_ladder = [(0.80, 5000)]
    assert ex.hedge_drift_breaches_floor(lo, store) is True
    ex.place_or_reprice(c, _PDEC(), None, store, NOW, 101.0, "pre")
    assert ex._hedge_drift_repriced >= 1
    assert any("HEDGE-DRIFT" in m for m in ex.log.infos)


def test_a_hedge_too_thin_to_cover_our_size_is_a_breach(tmp_path):
    """A walk that runs out of book must not be priced off the shallow part it could fill — that is the
    PHIMIA shape, where a ~5c partial walk passed a gate and then swept to 7c."""
    store = _DriftStore(poly_best_ask=0.55, hedge_ladder=[(0.60, 5000)])
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    c = _c("PHIMIA", "t|1")
    ex.place_or_reprice(c, _PDEC(), None, store, NOW, 100.0, "pre")
    lo = ex.open_orders[c.key]
    store.hedge_ladder = [(0.60, 1)]                         # one share of depth against our size
    assert ex.hedge_drift(lo, store) == float("-inf")
    assert ex.hedge_drift_breaches_floor(lo, store) is True


def test_an_unreadable_hedge_book_does_not_trigger_a_cancel_storm(tmp_path):
    """'I could not check' must not become a cancel on every node the moment a feed hiccups — the
    fill-time pre-hedge gate is still the hard rail underneath."""
    store = _DriftStore(poly_best_ask=0.55, hedge_ladder=[(0.60, 5000)])
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    c = _c("BOUFCA", "t|1")
    ex.place_or_reprice(c, _PDEC(), None, store, NOW, 100.0, "pre")
    lo = ex.open_orders[c.key]
    store.hedge_ladder = []
    assert ex.hedge_drift(lo, store) is None
    assert ex.hedge_drift_breaches_floor(lo, store) is False


def test_live_rows_carry_quote_age_and_the_walked_hedge_net(tmp_path):
    """N28: ``quote_age_s`` was empty on every live fill row, so 'were we picked off?' — a correlation
    between how long a quote rested and how far the hedge had moved — could not be asked at all."""
    store = _DriftStore(poly_best_ask=0.55, hedge_ladder=[(0.60, 5000)])
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    c = _c("BOUFCA", "t|1")
    ex.place_or_reprice(c, _PDEC(), None, store, NOW, 100.0, "pre")
    lo = ex.open_orders[c.key]
    lo.placed_ts = NOW.timestamp() - 486.0                   # CERBVB's own number
    ex._record_fill(lo, 10.0, 0.38, NOW, store)
    row = [r for r in ex.state.rows if r.get("event") == "fill"][-1]
    assert row["quote_age_s"] == 486.0
    assert "hedge_locked_now" in row


# --------------------------------------------------------------------------- #
# the measurement gate                                                          #
# --------------------------------------------------------------------------- #
def _csv(tmp_path, rows):
    p = tmp_path / "maker_rt_20260730.csv"
    import csv as _csvmod
    from src.genz.maker_rt.state import CSV_COLUMNS
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = _csvmod.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return [str(p)]


def _locked(ts, sport, net, usd=0.0):
    return {"ts": ts, "mode": "live", "event": "hedge_locked", "sport": sport,
            "locked_net": net, "realized_pnl_usd": usd}


def test_a_sport_below_its_gate_reads_MEASURING(tmp_path):
    paths = _csv(tmp_path, [_locked("2026-07-30T10:00:00Z", "soccer", 1.2, 0.5),
                            _locked("2026-07-30T11:00:00Z", "soccer", 0.8, 0.3)])
    rep = gates.report(paths)
    assert rep["soccer"]["n"] == 2 and rep["soccer"]["need"] == 10
    assert rep["soccer"]["verdict"] == "MEASURING" and rep["soccer"]["short_by"] == 8


def test_clearing_the_gate_with_a_positive_mean_reads_EDGE_POSITIVE(tmp_path):
    paths = _csv(tmp_path, [_locked(f"2026-07-30T10:{i:02d}:00Z", "soccer", 1.0, 0.1)
                            for i in range(10)])
    rep = gates.report(paths)
    assert rep["soccer"]["n"] == 10 and rep["soccer"]["verdict"] == "EDGE-POSITIVE"


def test_clearing_the_gate_with_a_negative_mean_reads_EDGE_NEGATIVE(tmp_path):
    paths = _csv(tmp_path, [_locked(f"2026-07-30T10:{i:02d}:00Z", "soccer", -0.4, -0.1)
                            for i in range(10)])
    assert gates.report(paths)["soccer"]["verdict"] == "EDGE-NEGATIVE"


def test_pre_fix_rows_are_excluded_and_counted_not_silently_dropped(tmp_path):
    """The audit's three named fictions (+90.6657%, +29.7815%, -54.25%) are still on disk in the
    append-only CSVs: the restatement corrected the ledger, it could not rewrite history. Including
    them drags soccer's mean to +5.955% while its median sits at -0.750%."""
    paths = _csv(tmp_path, [
        _locked("2026-07-29T01:21:06Z", "soccer", 90.6657, 320.05),   # before the fix -> cut by date
        _locked("2026-07-30T09:00:44Z", "soccer", -54.25, -49.91),    # after, but impossible -> counted
        _locked("2026-07-30T10:00:00Z", "soccer", 1.0, 0.5),
    ])
    rep = gates.report(paths)
    assert rep["soccer"]["n"] == 1, "only the plausible post-fix row is measured"
    assert rep["soccer"]["excluded"] == 1, "and the exclusion is visible, not silent"
    assert rep["soccer"]["mean_pct"] == 1.0
    assert "excluded" in gates.render(rep)


def test_untracked_windfalls_are_not_maker_edge(tmp_path):
    """The +$42 UFC ghost was luck on a naked position. Averaging it into the maker's edge is how a
    strategy congratulates itself."""
    paths = _csv(tmp_path, [
        {"ts": "2026-07-30T10:00:00Z", "mode": "live", "event": "fill_untracked", "sport": "ufc",
         "locked_net": 40.0, "realized_pnl_usd": 42.0},
        _locked("2026-07-30T11:00:00Z", "soccer", 1.0, 0.5),
    ])
    rep = gates.report(paths)
    assert rep["ufc"]["n"] == 0 and rep["all"]["n"] == 1


def test_the_report_names_every_gated_sport(tmp_path):
    paths = _csv(tmp_path, [_locked("2026-07-30T10:00:00Z", "soccer", 1.0, 0.5)])
    rep = gates.report(paths)
    assert set(rep["all"]["gated_sports"]) == {"soccer", "mlb", "tennis", "ufc"}
    text = gates.render(rep)
    assert "STILL MEASURING" in text and "no cap raise is justified" in text
    assert "soccer" in gates.summary_line(rep)


# --------------------------------------------------------------------------- #
# F2 / N27 — reporting windows and units                                        #
# --------------------------------------------------------------------------- #
def test_the_daily_trio_comes_from_the_rail_that_enforces_it():
    """``n_fills``/``pnl_today`` on MakerState reset with the PROCESS (10-21 restarts/day), so a panel
    reading them saw a 'day' that started at the last deploy while the caps counted from midnight."""
    from src.genz.maker_rt.state import MakerState
    st = MakerState(log=_Log())
    st.n_fills, st.pnl_today = 1, 0.25                     # since this restart
    st.live = {"fills_today": 7, "pnl_today": 3.5, "stake_today": 412.0}   # since midnight (LiveCaps)
    s = st.summary("live", {}, NOW)
    assert (s["fills_today"], s["pnl_today"], s["stake_today"]) == (7, 3.5, 412.0)
    assert (s["fills_since_restart"], s["pnl_since_restart"]) == (1, 0.25)
    assert s["windows"]["fills_today"].startswith("utc_day")
    hb = st.heartbeat("live", {}, 0, NOW)
    assert hb["fills_today"] == 7 and hb["fills_since_restart"] == 1


def test_the_trio_falls_back_to_since_restart_when_there_is_no_caps_snapshot():
    from src.genz.maker_rt.state import MakerState
    st = MakerState(log=_Log())
    st.n_fills, st.pnl_today = 2, 1.0
    s = st.summary("live", {}, NOW)
    assert s["fills_today"] == 2 and s["pnl_today"] == 1.0


def test_the_achievable_ladder_states_its_units():
    """Two edge numbers sat side by side in one payload differing by 100x, with nothing saying so."""
    from src.genz.maker_rt.state import MakerState
    st = MakerState(log=_Log())
    for _ in range(10):
        st.record_achievable("soccer", "pre", 0.0105, NOW, rails_ok=True)
    a = st.summary("live", {}, NOW)["by_sport"]["soccer"]["achievable"]
    assert a["p50"] == 0.0105 and a["p50_pct"] == 1.05
    assert a["units"]["p50_pct"] == "percent"
    assert 0.0 <= a["share_ge_100bp"] <= 1.0


def test_achievable_ladders_survive_a_restart(tmp_path):
    """Restarts run 10-21x/day and the ladder is the only evidence that says whether a sport's market
    bears the target at all — resetting it every deploy meant it could never reach a readable n."""
    from src.genz.maker_rt.state import MakerState
    st = MakerState(log=_Log())
    for i in range(40):
        st.record_achievable("soccer", "pre", 0.002 * (i % 5), NOW, rails_ok=True)
    st.persist_tuning()
    st2 = MakerState(log=_Log())
    st2.load_tuning()
    b = st2.summary("live", {}, NOW)["by_sport"]["soccer"]["achievable"]
    assert b["n"] == 40, "the ladder came back with its count intact"
    assert b["p50"] is not None


def test_a_reservation_is_released_by_the_order_that_made_it(tmp_path):
    """The bug this pins was found by the test above and would have been invisible in production until
    the budget silently closed: ``on_open`` held the projected pair but the order never carried the
    number, so ``on_close`` released ZERO. Reserved stake would then only ever grow, and after enough
    cancelled quotes every placement refuses with ``daily_stake_reserved`` on an empty book."""
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    ex.max_open_per_game = 3
    c = _c("BOUFCA", "t|1")
    ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 100.0, "pre")
    held = ex.open_orders[c.key].projected_pair
    assert held > 0 and abs(ex.caps.reserved_stake - held) < 1e-6
    ex.cancel(c, NOW, "expire")
    assert ex.caps.reserved_stake == 0.0, "the hold and the release are the same number"
    assert ex.game_reserved("BOUFCA") == 0.0


def test_repeated_place_and_cancel_cycles_do_not_leak_the_budget(tmp_path):
    """The failure mode stated as a loop: twenty quote/cancel cycles must leave the budget where it
    started, not twenty pair-costs poorer."""
    ex = _live_exec(tmp_path, max_open_quotes=12, max_daily_stake_usd=10_000.0, max_fills_per_day=20)
    for i in range(20):
        c = _c("BOUFCA", f"t|{i}")
        ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 100.0 + i, "pre")
        ex.cancel(c, NOW, "expire")
    assert ex.caps.reserved_stake == 0.0
    assert ex.caps.daily_stake_headroom() == ex.caps.max_daily_stake_usd - ex.caps.stake_today
