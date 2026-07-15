"""Round-trip tests for run._write_og_current -> data/og_current.json (honest walked current-state)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src import run
from src.arbitrage import Candidate, compute_arb
from src.catalog import MarketSpec

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


class _RecLog:
    def __init__(self):
        self.infos, self.warnings = [], []

    def info(self, msg, *a):
        self.infos.append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.warnings.append(msg % a if a else msg)

    def debug(self, *a, **k):
        pass


class _Cfg:
    """Minimal Config stand-in exposing only what _write_og_current touches (defaults -> no network:
    all-fixed legs never trigger a book fetch)."""
    def __init__(self, csv_path):
        self.csv_path = csv_path
        self.bankroll_total = 30000
        self.poly_fee_rate = 0.05

    def threshold(self, key, default):
        return default

    def get(self, *path, default=None):
        return default

    def polymarket_opt(self, key, default):
        return default

    def kalshi_opt(self, key, default):
        return default


def _spec():
    return MarketSpec(market_id=1010, label="Over Under Full Time", family="totals", period="fulltime",
                      line=2.5, n_way=2, outcome_ids=[1010, 1011],
                      outcome_names={1010: "Over", 1011: "Under"})


def _opp(res):
    return run.Opportunity(fixture_id="fx", match="A vs B", home_team="A", away_team="B", tournament="WC",
                           kickoff_utc="2026-07-14T19:00:00Z", spec=_spec(), res=res, actionable=True,
                           shadow_books=[], suspicious=False, bet_links={}, signature="sig")


def _fixed_arb(o_over, o_under, *, lim_over=1500, lim_under=5000):
    over = Candidate(outcome_id=1010, outcome_name="Over", book="pinnacle", clone_group="pinnacle",
                     decimal_odds=o_over, limit=lim_over)
    under = Candidate(outcome_id=1011, outcome_name="Under", book="1xbet", clone_group="1xbet",
                      decimal_odds=o_under, limit=lim_under)
    return compute_arb([over, under])


def test_og_current_round_trip(tmp_path):
    cfg = _Cfg(str(tmp_path / "arbitrage_opportunities.csv"))
    run._write_og_current([_opp(_fixed_arb(2.10, 2.05))], cfg, NOW, _RecLog())
    data = json.loads((tmp_path / "og_current.json").read_text(encoding="utf-8"))

    assert data["cycle_utc"] == "2026-07-14T12:00:00Z" and data["scan_interval_s"] == 300
    assert len(data["arbs"]) == 1
    a = data["arbs"][0]
    assert a["match"] == "A vs B" and a["profit"] > 100                     # honest walked profit
    assert {lg["book"] for lg in a["legs"]} == {"pinnacle", "1xbet"}
    assert all({"outcome", "book", "top_odds", "avg_fill_odds", "stake", "payout"} <= set(lg)
               for lg in a["legs"])
    assert a["total_stake"] > 0 and a["t_max_honest"] > 0


def test_og_current_below_floor_is_informational_near_miss(tmp_path):
    """A NET-positive arb whose HONEST boundary is a few dollars (tiny limits) is INCLUDED as an
    informational below_floor near-miss (not a placeable bet) — so an empty tab means 'nothing even
    close'. The 'survives only to $X' log still fires."""
    cfg = _Cfg(str(tmp_path / "arbitrage_opportunities.csv"))
    log = _RecLog()
    run._write_og_current([_opp(_fixed_arb(2.10, 2.05, lim_over=2, lim_under=2))], cfg, NOW, log)
    data = json.loads((tmp_path / "og_current.json").read_text(encoding="utf-8"))
    assert len(data["arbs"]) == 1
    a = data["arbs"][0]
    assert a["below_floor"] is True and a["fee_trap"] is False
    assert a["net_roi_pct"] > 0 and a["legs"] == [] and a["profit"] is None
    assert any("survives only to" in m for m in log.infos)


def _poly_3way():
    """A 3-way all-Polymarket arb: gross S=0.9885 (sub-1) but NET-negative after the poly taker fee."""
    from src.arbitrage import Candidate
    legs = [Candidate(outcome_id=1, outcome_name="H", book="polymarket", clone_group="polymarket",
                      decimal_odds=1 / 0.36, limit=100000, is_exchange=True),
            Candidate(outcome_id=2, outcome_name="X", book="polymarket", clone_group="polymarket",
                      decimal_odds=1 / 0.316, limit=100000, is_exchange=True),
            Candidate(outcome_id=3, outcome_name="A", book="polymarket", clone_group="polymarket",
                      decimal_odds=1 / 0.3125, limit=100000, is_exchange=True)]
    res = compute_arb(legs)
    spec = MarketSpec(market_id=101, label="Full Time Result", family="1x2", period="fulltime",
                      line=None, n_way=3, outcome_ids=[1, 2, 3], outcome_names={1: "H", 2: "X", 3: "A"})
    return run.Opportunity(fixture_id="fx", match="A vs B", home_team="A", away_team="B", tournament="WC",
                           kickoff_utc="2026-07-14T19:00:00Z", spec=spec, res=res, actionable=True,
                           shadow_books=[], suspicious=False, bet_links={}, signature="s3")


def test_og_current_fee_trap_row_included(tmp_path):
    """A gross-positive / NET-negative arb is INCLUDED as an amber fee_trap near-miss (not sized, not
    dropped) with its net numbers."""
    cfg = _Cfg(str(tmp_path / "arbitrage_opportunities.csv"))
    run._write_og_current([_poly_3way()], cfg, NOW, _RecLog())
    data = json.loads((tmp_path / "og_current.json").read_text(encoding="utf-8"))
    assert len(data["arbs"]) == 1
    a = data["arbs"][0]
    assert a["fee_trap"] is True and a["below_floor"] is False
    assert a["roi_pct"] > 0 and a["net_roi_pct"] < 0 and a["fee_pct"] > 0
    assert a["legs"] == [] and a["profit"] is None


def test_og_current_written_even_with_no_arbs(tmp_path):
    cfg = _Cfg(str(tmp_path / "arbitrage_opportunities.csv"))
    run._write_og_current([], cfg, NOW, _RecLog())
    data = json.loads((tmp_path / "og_current.json").read_text(encoding="utf-8"))
    assert data["arbs"] == [] and data["cycle_utc"] == "2026-07-14T12:00:00Z"
