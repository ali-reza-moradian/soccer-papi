"""THE 8-HOURLY BALANCE RECONCILIATION — the exchanges audited against the bot's own books.

The premise every test here defends: the bot's ledger is a CLAIM and the exchanges' cash is TRUTH.
Every incident this repo has a memory file about — the $500-for-$5 settlement, the F14 price-space
inversion, the phantom +$15.94 walkover, the -$38.08 stray-order leak — was a claim that disagreed with
the money and went unnoticed because nothing compared the two.

The venue payloads below are the REAL shapes, read live from both venues on 2026-07-31:

    Kalshi  /portfolio/balance    {"balance": 451972, "balance_dollars": "4519.7251", ...}
            /portfolio/positions  {"market_positions": [{"ticker": ..., "position_fp": "-100.00",
                                    "market_exposure_dollars": "70.000000"}], "cursor": ""}
    Poly    get_balance           {"balance": "2618991012"}   (1e6-scaled USDC)
            data-api /positions   [{"asset": ..., "currentValue": 27.91, "size": 101.4999, ...}]

and the units in them are exactly where this can go wrong: ``balance`` is CENTS while
``balance_dollars`` is dollars, and reading the wrong one is the same 100x class of error that once
booked a $5.00 payout as $500.00.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from src.genz.maker_rt import balance as bal
from src.genz.maker_rt import config as mrt_config

NOW = datetime(2026, 7, 31, 16, 0, 3, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# fakes — the real payload shapes, none of the network                          #
# --------------------------------------------------------------------------- #
class _Log:
    def __init__(self):
        self.infos: list = []
        self.warns: list = []

    def info(self, msg, *a):
        self.infos.append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.warns.append(msg % a if a else msg)

    def error(self, msg, *a):
        self.warns.append(msg % a if a else msg)

    def critical(self, msg, *a):
        self.warns.append(msg % a if a else msg)


class _Kalshi:
    """GET /portfolio/balance + /portfolio/positions, in the venue's own shapes."""

    def __init__(self, *, cash="4519.7251", positions=None, fail_balance=False, hang=None):
        self.cash = cash
        self.positions = positions if positions is not None else [
            {"ticker": "KXCLUBFTOTAL-26JUL31BCBAR-5", "position_fp": "-100.00",
             "market_exposure_dollars": "70.000000", "total_traded_dollars": "70.000000"}]
        self.fail_balance = fail_balance
        self.hang = hang

    def get_balance(self):
        if self.hang is not None:
            self.hang.wait(30)
        if self.fail_balance:
            raise RuntimeError("401 unauthorized")
        return {"balance": int(round(float(self.cash) * 100)), "balance_dollars": self.cash,
                "portfolio_value": 7000}

    def get_positions(self, *, limit=None, cursor=None):
        return {"market_positions": list(self.positions), "cursor": ""}


class _Poly:
    def __init__(self, *, cash="2618991012", positions=None, fail_balance=False):
        self.cash = cash
        self.positions = positions if positions is not None else []
        self.fail_balance = fail_balance

    def get_balance(self):
        if self.fail_balance:
            raise RuntimeError("clob 500")
        return {"balance": self.cash, "allowances": {}}


def _poly_row(asset, value, *, size=100.0, title="Birmingham City vs. FC Barcelona: O/U 4.5"):
    return {"asset": str(asset), "currentValue": value, "size": size, "curPrice": 0.275,
            "initialValue": 70.0, "redeemable": False, "title": title}


def _rec(tmp_path, *, kalshi=None, poly=None, poly_rows=(), telegram=None, alert_usd=5.0,
         log=None, truncated=False):
    """A reconciler with fake venues and its OWN snapshot file.

    The explicit ``path`` is not cosmetic: the worker thread outlives the test that started it (a hung
    job is abandoned, not killed), and the conftest patches OPS_DIR globally — so a leaked job finishing
    late would otherwise persist itself into the NEXT test's directory."""
    cfg = mrt_config.load_maker_rt_config(overrides=None)
    cfg.live.enabled = True
    cfg.balance.alert_usd = alert_usd
    rows = list(poly_rows)
    return bal.BalanceReconciler(
        cfg, telegram=telegram, log=log or _Log(), kalshi=kalshi or _Kalshi(), poly=poly or _Poly(),
        path=str(tmp_path / "balance_snapshots.json"),
        positions_readers=(lambda k: k.get_positions()["market_positions"],
                           lambda p: (rows, truncated)))


def _books(settled=0.0, *, untracked=0.0, exits=0.0, pnl_today=0.0, trades=0, open_legs=0):
    return {"settled_pnl_lifetime": settled,
            "settled_pnl_hedged_lifetime": settled - untracked - exits,
            "settled_pnl_untracked_lifetime": untracked, "settled_pnl_exits_lifetime": exits,
            "settled_trades": trades,
            "pnl_today": pnl_today, "fills_today": 0, "open_legs": open_legs}


def _store(rec):
    return bal.load_store(rec.path)


# --------------------------------------------------------------------------- #
# THE UNITS — cents vs dollars vs 1e6, each read from the field that says so    #
# --------------------------------------------------------------------------- #
def test_kalshi_cash_prefers_the_dollars_field():
    """451972 is CENTS of the same number ``balance_dollars`` states precisely. Reading the cents field
    as dollars is the exact 100x error that booked a $5.00 Kalshi payout as $500.00."""
    assert bal.kalshi_cash_usd({"balance": 451972, "balance_dollars": "4519.7251"}) == 4519.7251


def test_kalshi_cash_falls_back_to_cents_when_the_dollars_field_is_absent():
    assert bal.kalshi_cash_usd({"balance": 451972}) == 4519.72


def test_an_unreadable_balance_is_none_not_zero():
    """None means 'we could not ask'. Zero would mean 'the account is empty' — and the difference
    between those two is an $8,000 phantom withdrawal in the very next report."""
    for bad in ({}, {"error": "auth"}, {"balance": "n/a"}, None, "oops"):
        assert bal.kalshi_cash_usd(bad) is None
        assert bal.poly_cash_usd(bad) is None


def test_poly_cash_is_usdc_base_units():
    assert bal.poly_cash_usd({"balance": "2618991012"}) == 2618.991012


def test_kalshi_positions_value_sums_exposure_and_skips_flat_rows():
    """A settled market we still appear in holds no money; counting it would move the number for no
    reason. ``position_fp`` is the v2 name — the bare ``position`` read blind on a v2 payload is what
    made two filled orders invisible for 11.5 hours."""
    rows = [{"ticker": "A", "position_fp": "-100.00", "market_exposure_dollars": "70.000000"},
            {"ticker": "B", "position_fp": "0.00", "market_exposure_dollars": "0.000000"},
            {"ticker": "C", "position": 25, "market_exposure": 1250}]        # v1 shape: CENTS
    got = bal.kalshi_positions_value(rows)
    assert got["n"] == 2 and got["tickers"] == ["A", "C"]
    assert got["value_usd"] == 82.5                                          # 70.00 + 12.50


def test_poly_positions_split_ours_from_the_wallets():
    """This funder wallet is shared: on 2026-07-31 it held 173 positions and exactly ONE was the
    maker's. Reporting the wallet total as 'our positions' would attribute other people's money — and
    other people's losses — to this bot."""
    ours = "944999629156353074632036345722208933376602673961893002239319316852814002023"
    rows = [_poly_row(ours, 27.91), _poly_row("999", 0.0), _poly_row("888", 12.5)]
    got = bal.poly_positions_value(rows, [ours])
    assert got["n_total"] == 3 and got["n_maker"] == 1
    assert got["total_usd"] == 40.41 and got["maker_usd"] == 27.91


# --------------------------------------------------------------------------- #
# THE SCHEDULE — fixed UTC slots, persisted, catch-up ONCE                       #
# --------------------------------------------------------------------------- #
def test_the_slots_are_fixed_utc_not_relative_to_start():
    at = lambda h, m=0: datetime(2026, 7, 31, h, m, tzinfo=timezone.utc)          # noqa: E731
    assert bal.slot_at_or_before(at(16, 5)) == at(16)
    assert bal.slot_at_or_before(at(15, 59)) == at(8)
    assert bal.slot_at_or_before(at(0, 0)) == at(0)
    # Before the day's first slot -> yesterday's last, so a 00:30 restart compares to 16:00 yesterday.
    assert bal.slot_at_or_before(datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)
                                 - __import__("datetime").timedelta(minutes=30)).hour == 16


def test_a_completed_slot_does_not_run_again():
    now = datetime(2026, 7, 31, 16, 30, tzinfo=timezone.utc)
    assert bal.due_slot(now, "2026-07-31T16:00:00Z") is None
    assert bal.due_slot(now, "2026-07-31T08:00:00Z") == datetime(2026, 7, 31, 16, tzinfo=timezone.utc)
    assert bal.due_slot(now, "") == datetime(2026, 7, 31, 16, tzinfo=timezone.utc)


def test_a_missed_slot_runs_once_on_the_next_start():
    """Down across 08:00 AND 16:00 -> ONE run, for the most recent slot. Replaying the missed one would
    produce a snapshot timestamped hours after the balances it claims to describe."""
    now = datetime(2026, 7, 31, 17, 0, tzinfo=timezone.utc)
    slot = bal.due_slot(now, "2026-07-31T00:00:00Z")
    assert slot == datetime(2026, 7, 31, 16, tzinfo=timezone.utc)
    assert bal.due_slot(now, bal.iso(slot)) is None, "and only once"


def test_the_schedule_is_read_from_disk_so_a_restart_cannot_duplicate(tmp_path):
    rec = _rec(tmp_path)
    assert rec.maybe_run(NOW, _books()) is True
    _settle(rec)
    rec.drain(100.0)
    fresh = _rec(tmp_path)                       # a NEW process, same files
    assert fresh.maybe_run(NOW, _books()) is False, "the slot is already recorded as done"
    rec.close(); fresh.close()


# --------------------------------------------------------------------------- #
# BASELINE — created once, never overwritten, never built on a half-read        #
# --------------------------------------------------------------------------- #
def _flat(tmp_path, **kw):
    """A reconciler whose venues hold NOTHING — the only kind of snapshot allowed to be a baseline."""
    return _rec(tmp_path, kalshi=_Kalshi(positions=[]), **kw)


def test_the_first_run_is_the_baseline_and_says_so(tmp_path):
    rec = _flat(tmp_path)
    out = rec.run_once(_books(settled=22.18), NOW, slot="2026-07-31T16:00:00Z")
    snap = out["snapshot"]
    assert snap["kalshi"]["cash"] == 4519.7251 and snap["poly"]["cash"] == 2618.991
    assert snap["total"] == round(4519.7251 + 2618.991, 4)
    assert out["report"]["is_baseline"] is True
    assert "BASELINE" in out["text"]
    assert _store(rec)["baseline"]["ts"] == snap["ts"]
    rec.close()


def test_a_snapshot_holding_an_open_pair_may_not_become_the_baseline(tmp_path):
    """A snapshot with a pair open values Kalshi at COST (``market_exposure`` — the venue publishes no
    mark) and Polymarket at MARK (``currentValue``). Its total is therefore part cost and part mark, and
    no later total can honestly be subtracted from it. The real 2026-07-31 baseline was taken exactly
    this way, with the Birmingham pair open, and carried $1.86 of pure valuation artefact into every
    lifetime comparison that followed."""
    rec = _rec(tmp_path, poly_rows=[_poly_row("X", 27.91)])
    out = rec.run_once(_books(settled=22.18), NOW, slot="2026-07-31T16:00:00Z")
    snap = out["snapshot"]
    assert snap["kalshi"]["positions"] == 70.0 and snap["poly"]["positions"] == 27.91
    assert snap["ok"] is True
    assert _store(rec)["baseline"] is None, "an open pair disqualifies it as an anchor"
    assert len(_store(rec)["snapshots"]) == 1, "still recorded — it is still a measurement"
    rec.close()


def test_the_baseline_is_never_overwritten(tmp_path):
    rec = _flat(tmp_path)
    rec.run_once(_books(), NOW, slot="2026-07-31T00:00:00Z")
    first = _store(rec)["baseline"]["ts"]
    rec._kalshi = _Kalshi(cash="9999.00", positions=[])
    rec.run_once(_books(), NOW, slot="2026-07-31T08:00:00Z")
    assert _store(rec)["baseline"]["ts"] == first
    assert _store(rec)["baseline"]["kalshi"]["cash"] == 4519.7251
    rec.close()


def test_a_bad_baseline_is_re_anchored_exactly_once(tmp_path):
    """The stored baseline was taken with a pair open, so it must be superseded — ONCE — by the first
    flat snapshot after that rule shipped. The old one is KEPT (``baseline_v1``): this log is the
    evidence trail, so a bad measurement is superseded, never deleted."""
    rec = _rec(tmp_path, poly_rows=[_poly_row("X", 27.91)])
    rec.run_once(_books(), NOW, slot="2026-07-31T00:00:00Z")
    _store(rec)  # baseline is None so far (not flat) — force one in, the way the old code would have
    store = _store(rec)
    store["baseline"] = store["snapshots"][0]
    bal.atomic_json(rec.path, store)

    rec._kalshi = _Kalshi(positions=[])                         # now FLAT
    out = rec.run_once(_books(), NOW, slot="2026-07-31T08:00:00Z")
    s = _store(rec)
    assert s["baseline"]["ts"] == out["snapshot"]["ts"], "re-anchored onto the flat snapshot"
    assert s["baseline_v1"]["kalshi"]["positions"] == 70.0, "the old one is kept for history"
    assert s["baseline_v2"] is True
    assert "NEW BASELINE" in out["text"] and "mixed a Kalshi COST basis" in out["text"]

    out2 = rec.run_once(_books(), NOW, slot="2026-07-31T16:00:00Z")
    assert _store(rec)["baseline"]["ts"] == s["baseline"]["ts"], "ONCE — the latch holds"
    assert "NEW BASELINE" not in out2["text"]
    rec.close()


def test_a_venue_that_could_not_be_read_never_becomes_the_baseline(tmp_path):
    """A venue we cannot read looks EXACTLY like a total withdrawal. Anchoring lifetime comparisons on
    that would turn one bad HTTP response into a permanent $4,500 phantom."""
    rec = _rec(tmp_path, kalshi=_Kalshi(fail_balance=True))
    out = rec.run_once(_books(), NOW, slot="2026-07-31T16:00:00Z")
    assert out["snapshot"]["ok"] is False
    assert _store(rec)["baseline"] is None
    assert len(_store(rec)["snapshots"]) == 1, "still recorded — a failed audit is evidence"
    assert "COULD NOT READ" in out["text"] and "NOT compared" in out["text"]
    rec.close()


def test_an_incomplete_snapshot_is_not_used_as_the_comparison_anchor(tmp_path):
    rec = _flat(tmp_path)
    rec.run_once(_books(settled=10.0), NOW, slot="2026-07-31T00:00:00Z")     # good -> baseline
    rec._poly = _Poly(fail_balance=True)
    rec.run_once(_books(settled=10.0), NOW, slot="2026-07-31T08:00:00Z")     # bad
    rec._poly = _Poly()
    out = rec.run_once(_books(settled=10.0), NOW, slot="2026-07-31T16:00:00Z")
    w = out["report"]["windows"]["window"]
    assert w["since"] == _store(rec)["baseline"]["ts"], "compared to the last GOOD snapshot, not the bad one"
    rec.close()


# --------------------------------------------------------------------------- #
# THE COMPARISON — venue movement vs what the books claim happened              #
# --------------------------------------------------------------------------- #
def _snap(total_cash, *, settled, ts, untracked=0.0, exits=0.0, open_pairs=0,
          k_cash=None, k_pos=0.0, p_cash=None, p_pos=0.0):
    """A comparison-ready snapshot. ``open_pairs`` puts a maker position on BOTH venues, which is what
    makes a window unreliable; the per-venue cash/positions default to a split of the total so the
    breach line has something real to name."""
    k_cash = total_cash / 2.0 if k_cash is None else k_cash
    p_cash = total_cash - k_cash - k_pos - p_pos if p_cash is None else p_cash
    return {"ok": True, "ts": ts, "total": total_cash,
            "kalshi": {"ok": True, "cash": k_cash, "positions": k_pos, "n_positions": open_pairs},
            "poly": {"ok": True, "cash": p_cash, "positions": p_pos, "n_positions": open_pairs,
                     "n_maker": open_pairs},
            "books": {"settled_pnl_lifetime": settled,
                      "settled_pnl_hedged_lifetime": settled - untracked - exits,
                      "settled_pnl_untracked_lifetime": untracked,
                      "settled_pnl_exits_lifetime": exits}}


def test_agreement_is_reported_as_agreement():
    prev = _snap(7000.00, settled=10.00, ts="2026-07-31T08:00:00Z")
    cur = _snap(7004.55, settled=14.60, ts="2026-07-31T16:00:00Z")
    rep = bal.build_report(cur, prev=prev, baseline=prev)
    w = rep["windows"]["window"]
    assert w["venue_delta"] == 4.55 and w["book_delta"] == 4.60
    assert abs(w["discrepancy"]) < 0.06 and rep["alert"] is False
    assert "✅ match" in bal.render_report(rep)


def test_the_lifetime_window_is_measured_from_the_baseline():
    base = _snap(7000.00, settled=0.00, ts="2026-07-30T00:00:00Z")
    prev = _snap(7002.00, settled=2.00, ts="2026-07-31T08:00:00Z")
    cur = _snap(7004.60, settled=4.55, ts="2026-07-31T16:00:00Z")
    rep = bal.build_report(cur, prev=prev, baseline=base)
    assert rep["windows"]["window"]["venue_delta"] == 2.60          # 8h
    assert rep["windows"]["lifetime"]["venue_delta"] == 4.60        # since baseline
    assert rep["windows"]["lifetime"]["book_delta"] == 4.55
    txt = bal.render_report(rep)
    assert "Last 8h:" in txt and "Since baseline (Jul 30):" in txt


def test_the_hedged_and_untracked_split_travels_with_the_window():
    """The +$42 UFC ghost was luck on a naked position. It moves real cash, so it belongs in the
    comparison — and it is NOT maker edge, so it is reported apart from the hedged number."""
    prev = _snap(7000.0, settled=0.0, ts="2026-07-31T08:00:00Z", untracked=0.0)
    cur = _snap(7042.0, settled=42.0, ts="2026-07-31T16:00:00Z", untracked=42.0)
    w = bal.build_report(cur, prev=prev)["windows"]["window"]
    assert w["book_delta"] == 42.0 and w["book_untracked"] == 42.0 and w["book_hedged"] == 0.0
    assert w["discrepancy"] == 0.0


def test_a_gap_over_the_threshold_screams_on_the_8h_window():
    prev = _snap(7000.00, settled=10.00, ts="2026-07-31T08:00:00Z")
    cur = _snap(6980.00, settled=10.00, ts="2026-07-31T16:00:00Z")       # $20 gone, books silent
    rep = bal.build_report(cur, prev=prev, alert_usd=5.0)
    assert rep["alert"] is True
    txt = bal.render_report(rep)
    assert "BOOKS DISAGREE WITH THE EXCHANGES" in txt and "$20.00" in txt


def test_a_gap_over_the_threshold_screams_on_the_lifetime_window_too():
    """Both sides, because a slow leak never breaches a single 8h window — it only ever shows up
    against the baseline."""
    base = _snap(7000.00, settled=0.00, ts="2026-07-29T00:00:00Z")
    prev = _snap(6996.00, settled=0.00, ts="2026-07-31T08:00:00Z")
    cur = _snap(6993.00, settled=0.00, ts="2026-07-31T16:00:00Z")        # -$3 this window, -$7 lifetime
    rep = bal.build_report(cur, prev=prev, baseline=base, alert_usd=5.0)
    assert rep["windows"]["window"]["discrepancy"] == -3.0
    assert rep["windows"]["lifetime"]["discrepancy"] == -7.0
    assert [w["label"] for w in rep["breaches"]] == ["Since baseline (Jul 29)"]
    assert rep["alert"] is True


def test_a_window_that_is_not_eight_hours_does_not_claim_to_be():
    """A restart, a missed slot or a manual run produces some other window, and calling a 15-minute gap
    'Last 8h' would misstate the one thing this report exists to state."""
    prev = _snap(7000.0, settled=0.0, ts="2026-07-31T15:45:00Z")
    cur = _snap(7000.0, settled=0.0, ts="2026-07-31T16:00:00Z")
    txt = bal.render_report(bal.build_report(cur, prev=prev))
    assert "Since the last check (15m 0s):" in txt and "Last 8h" not in txt


def test_a_window_with_a_pair_open_at_either_end_may_not_fire_red():
    """The +/-$19-25 red MISMATCH alerts of 2026-08-01/02 fired on windows whose endpoints held open
    pairs — Kalshi valued at COST against Polymarket valued at MARK. Nothing was wrong; the two sides
    simply were not measured on the same basis. An alarm the operator cannot tell from noise is worse
    than no alarm, so an unreliable window is LABELLED and judgement is deferred, never shouted."""
    for open_at, who in ((("start",), dict(prev_open=1, cur_open=0)),
                         (("end",), dict(prev_open=0, cur_open=1)),
                         (("start", "end"), dict(prev_open=1, cur_open=1))):
        prev = _snap(7000.00, settled=10.00, ts="2026-07-31T08:00:00Z", open_pairs=who["prev_open"])
        cur = _snap(6975.00, settled=10.00, ts="2026-07-31T16:00:00Z", open_pairs=who["cur_open"])
        rep = bal.build_report(cur, prev=prev, alert_usd=5.0)
        w = rep["windows"]["window"]
        assert w["discrepancy"] == -25.0, "the number is still measured and still reported"
        assert w["reliable"] is False and tuple(w["open_at"]) == open_at
        assert rep["alert"] is False and rep["breaches"] == []
        txt = bal.render_report(rep)
        assert "unreliable while pairs are open (marks move)" in txt
        assert "🔴" not in txt
        assert "next flat-to-flat" in txt


def test_flatness_asks_whether_a_VALUATION_can_move_not_whether_anything_is_held():
    """2026-08-04T14:45Z: the wallet held $459.07 of a Jeju/Bayern leg that had already WON and settled
    in the books. The data API marks a resolved position at $1.00 face, so it is worth exactly what it
    will convert to — no cost-vs-mark mismatch, nothing to distort a subtraction. That snapshot is a
    legitimate anchor; an OPEN pair is not."""
    resolved = {"ok": True, "ts": "T",
                "kalshi": {"ok": True, "n_positions": 0},
                "poly": {"ok": True, "n_positions": 177, "n_maker": 0, "positions": 459.07}}
    assert bal.is_flat(resolved) is True
    resolved["poly"]["n_maker"] = 1                      # ...an OPEN maker leg, and it is not
    assert bal.is_flat(resolved) is False
    assert bal.is_flat({"ok": False, "kalshi": {}, "poly": {}}) is False, "a half-read is never flat"
    assert bal.is_flat(None) is False


def test_a_flat_to_flat_window_keeps_the_five_dollar_threshold():
    """The rule narrows WHEN the alarm may fire, not how hard it fires. Flat-to-flat is unchanged."""
    prev = _snap(7000.00, settled=10.00, ts="2026-07-31T08:00:00Z", open_pairs=0)
    cur = _snap(6975.00, settled=10.00, ts="2026-07-31T16:00:00Z", open_pairs=0)
    rep = bal.build_report(cur, prev=prev, alert_usd=5.0)
    assert rep["windows"]["window"]["reliable"] is True and rep["alert"] is True
    assert "not mark noise" in bal.render_report(rep)


def test_every_breach_line_names_the_per_venue_split():
    """'$25 apart' sends a human to two dashboards to find out which venue and whether it was cash or a
    mark. The numbers are already in the snapshots."""
    prev = _snap(7000.00, settled=0.0, ts="2026-07-31T08:00:00Z", k_cash=4000.0, p_cash=3000.0)
    cur = _snap(6975.00, settled=0.0, ts="2026-07-31T16:00:00Z", k_cash=3990.0, p_cash=2985.0)
    txt = bal.render_report(bal.build_report(cur, prev=prev, alert_usd=5.0))
    assert "where: Kalshi cash -$10.00/positions +$0.00 · Polymarket cash -$15.00/positions +$0.00" in txt


def test_the_split_is_printed_even_when_nothing_is_wrong():
    """It is one short line and it is the first question anyone asks. Printing it only on a breach means
    the operator learns to read it only in a panic."""
    prev = _snap(7000.00, settled=0.0, ts="2026-07-31T08:00:00Z")
    cur = _snap(7000.00, settled=0.0, ts="2026-07-31T16:00:00Z")
    assert "where: Kalshi cash" in bal.render_report(bal.build_report(cur, prev=prev))


def _restated(usd, applied, effective, key="exits-20260804"):
    return {"key": key, "usd": usd, "applied_ts": applied, "effective_ts": effective,
            "note": "three verified unwinds"}


def test_a_book_correction_is_not_reported_as_a_discrepancy():
    """The -$6.99 restatement moves the BOOKS at the moment it is applied, for cash the venues moved on
    Aug 2. To this audit that is indistinguishable from $6.99 leaving the account — so unless it is
    declared, our own fix fires the exact false alarm this work exists to remove."""
    prev = _snap(7000.0, settled=32.3847, ts="2026-08-04T14:45:52Z")
    cur = _snap(7000.0, settled=25.3947, ts="2026-08-04T16:00:00Z")
    cur["books"]["restatement_log"] = [_restated(-6.99, "2026-08-04T14:52:00Z", "2026-08-02T22:00:43Z")]
    rep = bal.build_report(cur, prev=prev, alert_usd=5.0)
    w = rep["windows"]["window"]
    assert w["book_delta"] == -6.99, "the books really did move"
    assert w["restated_usd"] == -6.99
    assert w["discrepancy"] == 0.0, "...but no cash did, and the audit knows why"
    assert rep["alert"] is False
    txt = bal.render_report(rep)
    assert "one-time correction to older books, not cash that moved now" in txt
    assert "📘 -$6.99 book correction applied 2026-08-04T14:52 for cash that moved 2026-08-02" in txt


def test_a_correction_whose_cash_moved_inside_the_window_is_not_subtracted():
    """If the venue movement is ALSO in this window then the window already contains it, and removing
    the correction as well would double-count — turning a clean audit into a phantom gap."""
    prev = _snap(7000.0, settled=10.0, ts="2026-08-01T00:00:00Z")
    cur = _snap(6993.01, settled=3.01, ts="2026-08-04T16:00:00Z")
    cur["books"]["restatement_log"] = [_restated(-6.99, "2026-08-04T14:52:00Z", "2026-08-02T22:00:43Z")]
    w = bal.build_report(cur, prev=prev)["windows"]["window"]
    assert w["restated_usd"] == 0.0, "the cash moved inside this window too"
    assert w["discrepancy"] == 0.0


def test_a_correction_applied_before_the_window_does_nothing():
    """Both endpoints already include it, so there is nothing to correct for."""
    prev = _snap(7000.0, settled=25.3947, ts="2026-08-05T00:00:00Z")
    cur = _snap(7001.0, settled=26.3947, ts="2026-08-05T08:00:00Z")
    cur["books"]["restatement_log"] = [_restated(-6.99, "2026-08-04T14:52:00Z", "2026-08-02T22:00:43Z")]
    w = bal.build_report(cur, prev=prev)["windows"]["window"]
    assert w["restated_usd"] == 0.0 and w["discrepancy"] == 0.0


def test_a_correction_cannot_mask_a_real_leak_of_a_different_size():
    """It subtracts exactly what it declares and not a cent more."""
    prev = _snap(7000.0, settled=32.3847, ts="2026-08-04T14:45:52Z")
    cur = _snap(6980.0, settled=25.3947, ts="2026-08-04T16:00:00Z")     # $20 really gone
    cur["books"]["restatement_log"] = [_restated(-6.99, "2026-08-04T14:52:00Z", "2026-08-02T22:00:43Z")]
    rep = bal.build_report(cur, prev=prev, alert_usd=5.0)
    assert rep["windows"]["window"]["discrepancy"] == -20.0
    assert rep["alert"] is True


def test_the_exit_toll_travels_with_the_window():
    """Lifetime falls by the exit toll while hedged does not move. The window has to carry the split or
    a lifetime that dropped while every pair profited looks like a bug."""
    prev = _snap(7000.0, settled=10.0, ts="2026-07-31T08:00:00Z", exits=0.0)
    cur = _snap(6993.01, settled=3.01, ts="2026-07-31T16:00:00Z", exits=-6.99)
    w = bal.build_report(cur, prev=prev)["windows"]["window"]
    assert w["book_delta"] == -6.99 and w["book_exits"] == -6.99
    assert w["book_hedged"] == 0.0, "hedged edge is untouched by an exit"
    assert w["discrepancy"] == 0.0, "and the exchanges agree, which is the entire point"


def test_exactly_at_the_threshold_is_not_a_breach():
    prev = _snap(7000.00, settled=0.0, ts="2026-07-31T08:00:00Z")
    cur = _snap(6995.00, settled=0.0, ts="2026-07-31T16:00:00Z")
    assert bal.build_report(cur, prev=prev, alert_usd=5.0)["alert"] is False
    assert bal.build_report(cur, prev=prev, alert_usd=4.99)["alert"] is True


# --------------------------------------------------------------------------- #
# ADJUSTMENTS — subtracted, and NAMED                                           #
# --------------------------------------------------------------------------- #
def test_a_deposit_is_subtracted_before_the_discrepancy_and_shown():
    """$1,000 arriving is not a $1,000 disagreement. But absorbing it silently would make the report a
    place where money can hide, which is the opposite of the point."""
    prev = _snap(7000.0, settled=0.0, ts="2026-07-31T08:00:00Z")
    cur = _snap(8000.0, settled=0.0, ts="2026-07-31T16:00:00Z")
    adj = [{"ts": "2026-07-31T12:00:00Z", "venue": "kalshi", "usd": 1000.0, "note": "deposit"}]
    rep = bal.build_report(cur, prev=prev, adjustments=adj, alert_usd=5.0)
    w = rep["windows"]["window"]
    assert w["venue_delta"] == 1000.0 and w["adjust_usd"] == 1000.0 and w["discrepancy"] == 0.0
    assert rep["alert"] is False
    txt = bal.render_report(rep)
    assert "+$1,000.00 on Polymarket" not in txt
    assert "+$1,000.00 on Kalshi — deposit" in txt


def test_a_withdrawal_outside_the_window_does_not_touch_it():
    prev = _snap(7000.0, settled=0.0, ts="2026-07-31T08:00:00Z")
    cur = _snap(7000.0, settled=0.0, ts="2026-07-31T16:00:00Z")
    adj = [{"ts": "2026-07-30T09:00:00Z", "usd": -250.0, "note": "withdrawal"}]
    w = bal.build_report(cur, prev=prev, adjustments=adj)["windows"]["window"]
    assert w["adjust_usd"] == 0.0 and w["adjustments"] == []


def test_no_adjustments_file_says_so_rather_than_staying_silent():
    rep = bal.build_report(_snap(7000.0, settled=0.0, ts="2026-07-31T16:00:00Z"), adjustments=[])
    assert "no adjustments recorded" in bal.render_report(rep)


def test_an_unparseable_adjustment_is_kept_and_flagged(tmp_path):
    """Dropping it would be the same failure as dropping a discrepancy: money that stopped being
    mentioned."""
    p = mrt_config.runtime_path("balance_adjustments")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump([{"date": "2026-07-31", "usd": "one thousand", "note": "typo"}], fh)
    rows = bal.load_adjustments()
    assert rows[0]["unreadable"] is True and rows[0]["usd"] == 0.0
    assert rows[0]["ts"] == "2026-07-31T00:00:00Z", "a bare date means the start of that UTC day"
    prev = _snap(7000.0, settled=0.0, ts="2026-07-30T16:00:00Z")
    cur = _snap(7000.0, settled=0.0, ts="2026-07-31T16:00:00Z")
    txt = bal.render_report(bal.build_report(cur, prev=prev, adjustments=rows))
    assert "UNREADABLE AMOUNT" in txt


def test_a_bom_in_the_adjustments_file_still_parses(tmp_path):
    """PowerShell's Out-File writes a BOM by default, and a BOM has already cost this system a day's
    committed stake (2026-07-28)."""
    p = mrt_config.runtime_path("balance_adjustments")
    with open(p, "w", encoding="utf-8-sig") as fh:
        json.dump([{"ts": "2026-07-31T12:00:00Z", "usd": 5.0, "note": "hand trade"}], fh)
    assert bal.load_adjustments()[0]["usd"] == 5.0


# --------------------------------------------------------------------------- #
# THE REPORT — plain language, and the open pairs that explain small diffs      #
# --------------------------------------------------------------------------- #
def test_the_report_reads_like_the_other_alerts(tmp_path):
    with open(mrt_config.runtime_path("traded_tokens"), "w", encoding="utf-8") as fh:
        json.dump({"tokens": ["OURS"], "tickers": []}, fh)
    rec = _rec(tmp_path, poly_rows=[_poly_row("OURS", 27.91), _poly_row("theirs", 5.0)])
    txt = rec.run_once(_books(settled=22.18, untracked=5.0, pnl_today=0.68), NOW,
                       slot="2026-07-31T16:00:00Z")["text"]
    assert txt.startswith("💰 8h BALANCE CHECK")
    assert "   Kalshi $4,589.73 (cash $4,519.73 + positions $70.00)" in txt
    assert "   Polymarket $2,651.90 (cash $2,618.99 + positions $32.91; of which ours $27.91)" in txt
    assert "   TOTAL $7,241.63" in txt          # 4,519.7251 + 70 + 2,618.991 + 32.91
    # Both book numbers, kept apart: settled is venue truth, today's locked is an estimate of money
    # that has not settled and is therefore still counted in the positions above.
    assert ("settled lifetime +$22.18 (+$17.18 from hedged pairs, +$5.00 luck, "
            "+$0.00 exit costs)") in txt
    assert "today's locked estimate +$0.68" in txt
    rec.close()


def test_the_books_line_names_the_exit_toll(tmp_path):
    """The unwind toll is inside ``settled_pnl_lifetime`` (that is what makes lifetime track venue cash),
    so the line has to say it is there — otherwise a lifetime that went DOWN while every pair profited
    is unexplainable from the report alone."""
    rec = _flat(tmp_path)
    books = _books(settled=25.3947, untracked=8.3788, exits=-6.99)
    txt = rec.run_once(books, NOW, slot="2026-07-31T16:00:00Z")["text"]
    assert ("settled lifetime +$25.39 (+$24.01 from hedged pairs, +$8.38 luck, "
            "-$6.99 exit costs)") in txt
    rec.close()


def test_the_report_still_prints_on_a_console_that_cannot_encode_it(capsys, monkeypatch):
    """Windows hands a bare ``python -m src.genz.maker_rt --balance`` a cp1252 stdout, and every line of
    this report opens with an emoji. The raise landed AFTER the venue reads, the persist and the
    baseline re-anchor — so the audit ran, changed state, and printed a traceback instead of its
    answer."""
    class _Cp1252:
        encoding = "cp1252"

        def __init__(self):
            self.out = []

        def write(self, s):
            s.encode("cp1252")            # what the real console does
            self.out.append(s)

        def flush(self):
            pass

    fake = _Cp1252()
    monkeypatch.setattr(bal.sys, "stdout", fake)
    bal._print("💰 8h BALANCE CHECK\n   TOTAL $7,241.28")
    text = "".join(fake.out)
    assert "8h BALANCE CHECK" in text and "TOTAL $7,241.28" in text


def test_the_second_measurement_does_not_print_the_same_window_twice(tmp_path):
    """While the baseline IS the previous snapshot the two windows are arithmetically identical, and a
    duplicated line reads as a bug rather than as a confirmation."""
    rec = _rec(tmp_path)
    rec.run_once(_books(), NOW, slot="2026-07-31T08:00:00Z")
    out = rec.run_once(_books(), NOW, slot="2026-07-31T16:00:00Z")
    assert "lifetime" not in out["report"]["windows"]
    assert out["text"].count("bot's books say") == 1
    rec.close()


def test_the_verdict_word_and_the_alarm_use_the_same_threshold():
    """A row that says MISMATCH while no alert fires (or the reverse) is a report that argues with
    itself. Both are derived from the configured threshold."""
    prev = _snap(7000.0, settled=0.0, ts="2026-07-31T08:00:00Z")
    for gap, expect in ((0.20, "✅ match"), (3.00, "🟡 within tolerance"), (9.00, "🔴 MISMATCH")):
        cur = _snap(7000.0 - gap, settled=0.0, ts="2026-07-31T16:00:00Z")
        rep = bal.build_report(cur, prev=prev, alert_usd=5.0)
        txt = bal.render_report(rep)
        assert expect in txt, (gap, txt)
        assert rep["alert"] is (expect == "🔴 MISMATCH")


def test_open_pairs_are_named_as_the_likely_explanation(tmp_path):
    with open(mrt_config.runtime_path("expected_positions"), "w", encoding="utf-8") as fh:
        json.dump({"kalshi\x1fKX-1": {"venue": "kalshi", "instrument": "KX-1", "shares": 100,
                                      "game": "26JUL31BCBAR", "market_key": "total_goals|4.5"}}, fh)
    rec = _rec(tmp_path)
    txt = rec.run_once(_books(), NOW, slot="2026-07-31T16:00:00Z")["text"]
    assert "Open pairs at snapshot: 1" in txt and "26JUL31BCBAR" in txt
    assert "marks move" in txt
    rec.close()


def test_an_open_pair_is_named_by_its_real_match_when_the_wallet_knows_it(tmp_path):
    """The registries know it as '26JUL31BCBAR total_goals|4.5'. The data API knows the same position
    as 'Birmingham City vs. FC Barcelona: O/U 4.5' — and this system's alerts name the match, never
    an id."""
    tok = "9449996291563530746320363457222089333766026739618930022393193168528140020"
    with open(mrt_config.runtime_path("expected_positions"), "w", encoding="utf-8") as fh:
        json.dump({f"polymarket\x1f{tok}": {"venue": "polymarket", "instrument": tok,
                                            "game": "26JUL31BCBAR",
                                            "market_key": "total_goals|4.5"}}, fh)
    rec = _rec(tmp_path, poly_rows=[_poly_row(tok, 27.91)])
    txt = rec.run_once(_books(), NOW, slot="2026-07-31T16:00:00Z")["text"]
    assert "Open pairs at snapshot: 1 (Birmingham City vs. FC Barcelona: O/U 4.5)" in txt
    assert "26JUL31BCBAR" not in txt
    rec.close()


def test_the_maker_subset_uses_both_registries(tmp_path):
    """traded_tokens holds what we have RESTED on; expected_positions holds what we HOLD. The one
    position on the wallet on 2026-07-31 was in the second and not the first — either alone under-reports
    'ours'."""
    with open(mrt_config.runtime_path("traded_tokens"), "w", encoding="utf-8") as fh:
        json.dump({"tokens": ["T1"], "tickers": ["KX-1"]}, fh)
    with open(mrt_config.runtime_path("expected_positions"), "w", encoding="utf-8") as fh:
        json.dump({"polymarket\x1fT2": {"venue": "polymarket", "instrument": "T2", "game": "G",
                                        "market_key": "ml"}}, fh)
    toks, tickers, pairs, labels = bal.maker_assets()
    assert toks == ("T1", "T2") and tickers == ("KX-1",) and pairs == ["G ml"]
    assert labels == {"T2": "G ml"}
    rec = _rec(tmp_path, poly_rows=[_poly_row("T1", 10.0), _poly_row("T2", 20.0),
                                    _poly_row("STRANGER", 500.0)])
    snap = rec.run_once(_books(), NOW, slot="2026-07-31T16:00:00Z")["snapshot"]
    assert snap["poly"]["positions"] == 530.0 and snap["poly"]["positions_maker"] == 30.0
    rec.close()


def test_a_truncated_position_page_is_declared(tmp_path):
    """The data API paged at 100 rows against a wallet holding 173. A silent floor reads as a
    withdrawal on the very next window."""
    rec = _rec(tmp_path, poly_rows=[_poly_row("A", 1.0)], truncated=True)
    txt = rec.run_once(_books(), NOW, slot="2026-07-31T16:00:00Z")["text"]
    assert "truncated" in txt and "FLOOR" in txt
    rec.close()


# --------------------------------------------------------------------------- #
# ISOLATION — this job may never touch, slow, or halt trading                   #
# --------------------------------------------------------------------------- #
def test_it_runs_on_its_own_thread_not_the_trading_workers(tmp_path):
    """THE CONSTRAINT. The measurement-gate report sharing the venue worker blacked out the fill-poll
    backstop ~4x an hour for 17 hours; this job does venue I/O, so sharing that thread would be worse."""
    from src.genz.maker_rt.offloop import Worker
    rec = _rec(tmp_path)
    assert isinstance(rec.worker, Worker)
    assert rec.worker._name == "maker-rt-balance"
    assert rec.worker.job_timeout_s == 30.0
    rec.close()


def test_a_hung_venue_read_is_abandoned_at_the_deadline(tmp_path):
    """Python cannot kill the thread; it can stop it owning the queue. The next slot must be able to
    run even if this one never returns."""
    gate = threading.Event()
    rec = _rec(tmp_path, kalshi=_Kalshi(hang=gate))
    rec.worker.job_timeout_s = 0.2
    assert rec.maybe_run(NOW, _books()) is True
    time.sleep(0.4)
    assert rec.worker.submit(("balance",), lambda: "fresh") is True, "the key was released"
    assert rec.worker.abandoned >= 1
    gate.set()
    rec.close()


def test_a_failed_run_keeps_its_slot_and_retries_on_a_cooldown(tmp_path):
    """One bad HTTP response must not cost an eight-hour hole in the audit trail — and a venue that is
    down for an hour must not turn into three venue reads every 2.5 seconds."""
    rec = _rec(tmp_path, kalshi=_Kalshi(fail_balance=True))
    assert rec.maybe_run(NOW, _books()) is True
    _settle(rec)
    rec.drain(1.0)
    assert _store(rec)["last_slot"] == "", "the slot is NOT marked done on a failed read"
    assert rec.maybe_run(NOW, _books()) is False, "...but it does not retry on the next heartbeat"
    later = NOW.replace(hour=16, minute=20)                      # past the 15-minute cooldown
    rec._kalshi = _Kalshi()
    assert rec.maybe_run(later, _books()) is True
    _settle(rec)
    rec.drain(2.0)
    assert _store(rec)["last_slot"] == "2026-07-31T16:00:00Z"
    assert rec.maybe_run(later, _books()) is False
    rec.close()


def test_a_failed_run_alerts_and_changes_nothing_else(tmp_path):
    """It has no authority over quoting, hedging, caps or halts — by construction there is nothing for
    it to touch."""
    sent: list = []
    log = _Log()
    rec = _rec(tmp_path, kalshi=_Kalshi(fail_balance=True), telegram=sent.append, log=log)
    rec.maybe_run(NOW, _books())
    _settle(rec)
    rec.drain(100.0)
    assert rec.failures == 1 and rec.last_ok_ts == 0.0
    assert any("BALANCE" in w for w in log.warns)
    assert not hasattr(rec, "caps") and not hasattr(rec, "halted")
    assert sent, "a failed audit still says so"
    rec.close()


def test_the_loop_side_calls_do_no_venue_io(tmp_path):
    """``maybe_run`` is a clock comparison and a queue put; ``drain`` takes finished results off a
    mailbox. If either could read a venue, the audit would be back on the loop's critical path."""
    calls = {"n": 0}

    class _Counting(_Kalshi):
        def get_balance(self):
            calls["n"] += 1
            return super().get_balance()

    rec = _rec(tmp_path, kalshi=_Counting())
    rec.enabled = False                       # nothing due -> nothing submitted
    assert rec.maybe_run(NOW, _books()) is False
    rec.drain(1.0)
    assert calls["n"] == 0
    rec.close()


def test_the_telegram_alert_is_sent_from_the_worker_not_the_loop(tmp_path):
    sent: list = []
    rec = _rec(tmp_path, telegram=sent.append)
    rec.maybe_run(NOW, _books(settled=22.18))
    assert sent == [], "nothing has been sent on the submitting thread"
    _settle(rec)
    rec.drain(500.0)
    assert len(sent) == 1 and sent[0].startswith("💰 8h BALANCE CHECK")
    rec.close()


def test_no_live_runtime_state_is_written_under_pytest():
    """Layer 1 of the write guard: the snapshot + adjustment paths resolve through runtime_path, so a
    test cannot reach data/ops even by passing an explicit path."""
    import pytest
    for kind in ("balance_snapshots", "balance_adjustments"):
        assert mrt_config.under_tmp(mrt_config.runtime_path(kind))
        with pytest.raises(mrt_config.LiveStateWriteUnderTest):
            mrt_config.assert_writable(f"C:\\bots\\soccer-papi\\data\\ops\\{kind}.json")


# --------------------------------------------------------------------------- #
# THE SAFETY ROW — a silent safety net must be VISIBLY silent                   #
# --------------------------------------------------------------------------- #
def test_the_balance_check_reports_its_own_freshness(tmp_path):
    rec = _rec(tmp_path)
    assert rec.safety(1000.0)["overdue"] is False, "never-run is not overdue"
    rec.maybe_run(NOW, _books())
    _settle(rec)
    rec.drain(1000.0)
    assert rec.safety(1000.0)["age_s"] == 0.0 and rec.safety(1000.0)["overdue"] is False
    assert rec.safety(1000.0 + 13 * 3600)["overdue"] is True
    rec.close()


def test_the_executor_publishes_when_each_safety_pass_last_landed(tmp_path):
    from .test_maker_rt_pregame import _exec
    ex, _ = _exec(tmp_path)
    sf = ex.safety_snapshot(10_000.0)
    assert set(sf) == {"fill_poll", "reconcile", "settle", "gates"}
    assert all(v["age_s"] is None and v["overdue"] is False for v in sf.values()), "never-run is not red"
    ex._fill_poll_applied_ts = 10_000.0 - 5 * ex.fill_poll_s
    ex._reconcile_applied_ts = 10_000.0 - 10.0
    sf = ex.safety_snapshot(10_000.0)
    assert sf["fill_poll"]["overdue"] is True and sf["reconcile"]["overdue"] is False
    assert sf["fill_poll"]["cadence_s"] == ex.fill_poll_s


def test_the_loop_block_that_drives_all_of_this_actually_runs(tmp_path):
    """The exact five calls ``__main__`` makes every heartbeat, against the REAL objects.

    Committing is deploying in this repo (gitguard restarts the maker on any HEAD change), so a
    NameError in that block would not be a failed test — it would be a maker that will not start."""
    from src.genz.maker_rt.state import MakerState

    from .test_maker_rt_pregame import _exec
    ex, _cfg = _exec(tmp_path)
    state = MakerState()
    state.live = ex.snapshot(1000.0)
    rec = _rec(tmp_path)
    rec.drain(1000.0)
    rec.maybe_run(NOW, bal.books_from(state, ex.caps,
                                      int((state.live or {}).get("expected_positions") or 0)))
    safety = ex.safety_snapshot(1000.0)
    safety["balance"] = rec.safety(1000.0)
    state.safety = safety
    hb = state.heartbeat("live", {"kalshi": True}, 0, NOW)
    assert set(hb["safety"]) == {"fill_poll", "reconcile", "settle", "gates", "balance"}
    _settle(rec)
    assert rec.drain(1000.0) is not None, "and the result lands back on the loop"
    rec.close()


def test_the_books_snapshot_reads_the_same_counters_the_rails_use(tmp_path):
    """``pnl_today`` has to come from LiveCaps — the object that actually halts the day — and not from
    a second set of counters that merely shares its names (that mismatch is audit finding F2)."""
    from src.genz.maker_rt.state import MakerState

    from .test_maker_rt_pregame import _exec
    ex, _cfg = _exec(tmp_path)
    ex.caps.pnl_today, ex.caps.fills_today = -1.25, 3
    state = MakerState()
    state.settled_pnl_lifetime, state.settled_pnl_untracked_lifetime = 22.18, 5.0
    b = bal.books_from(state, ex.caps, 2)
    assert b["pnl_today"] == -1.25 and b["fills_today"] == 3 and b["open_legs"] == 2
    assert b["settled_pnl_lifetime"] == 22.18 and b["settled_pnl_hedged_lifetime"] == 17.18


def test_the_heartbeat_and_summary_both_carry_the_safety_row():
    from src.genz.maker_rt.state import MakerState
    st = MakerState()
    st.safety = {"fill_poll": {"age_s": 3.0, "cadence_s": 10.0, "overdue": False}}
    now = NOW
    assert st.heartbeat("live", {}, 0, now)["safety"]["fill_poll"]["age_s"] == 3.0
    assert st.summary("live", {}, now)["safety"]["fill_poll"]["overdue"] is False


def test_the_panel_renders_the_safety_row_and_reddens_what_is_overdue(tmp_path):
    """The panel is the surface a human actually looks at, so the row has to RENDER — an untested
    string built in HTML is exactly where a silent-safety-net fix goes to die."""
    import re
    import shutil
    import subprocess

    import pytest
    if shutil.which("node") is None:
        pytest.skip("node not available")
    panel = os.path.join(os.path.dirname(__file__), "..", "data", "genz", "papi_panel.html")
    with open(panel, encoding="utf-8") as fh:
        js = re.search(r"<script>(.*)</script>", fh.read(), re.S).group(1)
    js = js[:js.index("readHash();applyState();")]
    script = tmp_path / "safety.js"
    script.write_text(
        "function __el(){return {innerHTML:'',textContent:'',value:'',className:'',style:{},"
        "classList:{add(){},remove(){},toggle(){},contains(){return false;}},"
        "querySelectorAll(){return [];},querySelector(){return null;},getAttribute(){return null;},"
        "setAttribute(){},appendChild(){},addEventListener(){}};}\n"
        "var __els={};\n"
        "var document={getElementById(id){return __els[id]||(__els[id]=__el());},"
        "querySelectorAll(){return [];},querySelector(){return null;},addEventListener(){},"
        "createElement(){return __el();},body:__el()};\n"
        "var window={},history={replaceState(){}},location={hash:''};\n"
        "function fetch(){var P={then(){return P;},catch(){return P;}};return P;}\n"
        "function setInterval(){}function setTimeout(){}\n" + js + "\n"
        "var sf={fill_poll:{age_s:3.2,cadence_s:10,overdue:false},"
        "gates:{age_s:8000,cadence_s:900,overdue:true}};\n"
        "console.log('ROW<<'+mrtSafetyRow(sf)+'>>');\n"
        "console.log('NONE<<'+mrtSafetyRow(null)+'>>');\n"
        "console.log('STRIP<<'+(makerStripHtml({mode:'live',gates:{},sockets:{},safety:sf,"
        "by_sport:{}}).indexOf('safety:')>0)+'>>');\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    row = re.search(r"ROW<<(.*?)>>", out.stdout, re.S).group(1)
    assert "fill-poll <b>3s</b>" in row, "a healthy pass shows its age, plainly"
    assert 'gates <b style="color:#e5484d">2.2h' in row, "an overdue pass is RED"
    assert "1 OVERDUE" in row
    assert re.search(r"NONE<<(.*?)>>", out.stdout, re.S).group(1) == "", "no data -> no row, not a lie"
    assert re.search(r"STRIP<<(.*?)>>", out.stdout, re.S).group(1) == "true"


# --------------------------------------------------------------------------- #
# CONFIG                                                                        #
# --------------------------------------------------------------------------- #
def test_the_threshold_and_slots_come_from_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("maker_rt:\n  balance:\n    balance_alert_usd: 12.5\n    slots_utc: [2, 14]\n",
                 encoding="utf-8")
    cfg = mrt_config.load_maker_rt_config(str(p))
    assert cfg.balance.alert_usd == 12.5, "the requirement's own key name must not silently do nothing"
    assert cfg.balance.slots_utc == (2, 14)
    assert bal.slot_at_or_before(datetime(2026, 7, 31, 13, tzinfo=timezone.utc),
                                 cfg.balance.slots_utc).hour == 2


def test_the_defaults_are_the_documented_ones():
    cfg = mrt_config.MakerRtConfig()
    assert cfg.balance.enabled is True and cfg.balance.alert_usd == 5.00
    assert cfg.balance.slots_utc == (0, 8, 16) and cfg.balance.timeout_s == 30.0


def test_a_shadow_process_never_builds_a_venue_client(tmp_path):
    """The structural rule the whole live path rests on: no client in a process that is not live."""
    cfg = mrt_config.load_maker_rt_config()
    cfg.live.enabled = False
    rec = bal.BalanceReconciler(cfg, log=_Log())
    out = rec.run_once(_books(), NOW, slot="")
    assert out["snapshot"]["ok"] is False
    assert "must never construct one" in out["snapshot"]["kalshi"]["error"]
    rec.close()


# --------------------------------------------------------------------------- #
def _settle(rec, tries=400):
    for _ in range(tries):
        if rec.worker.pending() == 0:
            return True
        time.sleep(0.01)
    return False
