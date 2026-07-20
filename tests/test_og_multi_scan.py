"""End-to-end scan orchestration: one sport's cycle (tree -> toa -> match -> ladders -> rows -> file)
with the network boundaries monkeypatched, plus the per-sport cadence gate in run_cycle."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.logsetup import get_logger
from src.og_multi import scan, state, toa

LOG = get_logger("test-ogm-scan")
NOW = datetime(2026, 7, 19, 23, 0, 0, tzinfo=timezone.utc)
LAST = "2026-07-19T22:55:00Z"
LADDERS = {("kalshi", "KML-LAD", "YES"): [(0.50, 200), (0.505, 100000)],
           ("poly", "PML-LAD", "BUY"): [(0.53, 100000)],
           ("kalshi", "KML-NYY", "YES"): [(0.56, 100000)],
           ("poly", "PML-NYY", "BUY"): [(0.55, 100000)]}


def _tree():
    n = lambda side, label, kt, pt: {"market_type": "ml2", "market_key": "ml2", "side": side,
                                     "outcome_label": label, "line": None, "kind": "2way",
                                     "kalshi_ticker": kt, "kalshi_side": "YES", "poly_token_id": pt}
    return {"games": {"mlbG": {"home": "New York Yankees", "away": "Los Angeles Dodgers",
                               "date": "2026-07-19", "kickoff_utc": "2026-07-19T23:20:00Z",
                               "nodes": [n("away", "Los Angeles Dodgers", "KML-LAD", "PML-LAD"),
                                         n("home", "New York Yankees", "KML-NYY", "PML-NYY")]}}}


def _event():
    def m(k, outs):
        return {"key": k, "last_update": LAST, "outcomes": outs}
    return {"home_team": "New York Yankees", "away_team": "Los Angeles Dodgers",
            "commence_time": "2026-07-19T23:20:00Z", "bookmakers": [
                {"key": "pinnacle", "last_update": LAST, "markets": [m("h2h", [
                    {"name": "New York Yankees", "price": 1.95}, {"name": "Los Angeles Dodgers", "price": 1.95}])]},
                {"key": "onexbet", "last_update": LAST, "markets": [m("h2h", [
                    {"name": "New York Yankees", "price": 2.30}, {"name": "Los Angeles Dodgers", "price": 1.88}])]}]}


class _FakeMD:
    def kalshi_ask_ladder(self, ticker, side="YES"):
        return LADDERS.get(("kalshi", ticker, side), [])

    def poly_ask_ladder(self, token):
        return LADDERS.get(("poly", token, "BUY"), [])


def test_scan_sport_full_cycle_writes_tier_a_arb(tmp_path, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")            # so scan_sport takes the book path
    monkeypatch.setattr(scan, "_load_tree", lambda sport: _tree())
    monkeypatch.setattr(scan, "_market_data", lambda gz: _FakeMD())
    monkeypatch.setattr(scan.toa, "run_toa", lambda sport, **kw: toa.ToaFetch(
        sport=sport, events=[_event()], markets_served=["h2h"], sport_keys=["baseball_mlb"],
        credits=1, daily_total=1))
    monkeypatch.setattr(scan.state, "og_current_path",
                        lambda sport, **k: str(tmp_path / f"og_current_{sport}.json"))

    summary = scan.scan_sport("mlb", cfg=load_config(), now=NOW, log=LOG, base=str(tmp_path))

    data = json.loads((tmp_path / "og_current_mlb.json").read_text(encoding="utf-8"))
    assert data["sport"] == "mlb" and data["scan_interval_s"] == 300
    assert data["toa_capabilities"]["markets_served"] == ["h2h"]
    row = next(r for r in data["arbs"] if r["tier"] == "A" and r["family"] == "ml2")
    assert row["arb_sum_S"] < 1 and row["profit"] > 0
    assert {lg["book"] for lg in row["legs"]} == {"kalshi", "1xbet"}
    assert summary["matched"] == 1 and summary["arbs"] >= 1


def test_run_cycle_respects_per_sport_cadence(tmp_path, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(scan, "scan_sport", lambda sport, **kw: ran.append(sport) or {"sport": sport})
    state.mark_ran("ufc", NOW, str(tmp_path))                 # ufc just ran (interval 600s)
    scan.run_cycle(now=NOW + timedelta(seconds=310), log=LOG, base=str(tmp_path))
    # +310s: mlb/tennis (300s) are due; ufc (600s) is not -> skipped this cycle.
    assert "mlb" in ran and "tennis" in ran and "ufc" not in ran
