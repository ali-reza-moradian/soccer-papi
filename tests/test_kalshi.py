"""Tests for the Kalshi-direct supplemental source: cents/dollars units, the 3-Yes-market ->
home/draw/away mapping by yes_sub_title IDENTITY (incl. a ticker-order-swapped case), the real
size×price limit, dropping an unmatched fixture, and the shadow-only rollout gate in run._scan."""
from __future__ import annotations

from datetime import datetime, timezone

from src import kalshi
from src.catalog import build_clone_group_fn, build_market_specs
from src.config import Config, Secrets
from src.logsetup import get_logger
from src.normalize import parse_odds_payload
from src.run import EngineCtx, _scan

LOG = get_logger("test-kalshi")

# Canonical Full Time Result (1x2): oid 101='1'/home, 102='X'/draw, 103='2'/away.
MARKETS_JSON = [
    {"marketId": 101, "sportId": 10, "marketType": "1x2", "period": "fulltime", "handicap": 0,
     "marketName": "Full Time Result",
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 102, "outcomeName": "X"},
                  {"outcomeId": 103, "outcomeName": "2"}]},
]
INDEX = kalshi.build_market_index(MARKETS_JSON, 10)

BY_FIXTURE = {
    "idBBB": {"p1": "USA", "p2": "Paraguay", "start_time": "2026-06-13T19:00:00.000Z",
              "status_id": 0, "tournament": "World Cup"},
}
NOW = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
EVENT = "KXWCGAME-26JUN13USAPAR"


def _mkt(yes_sub_title, yes_ask_dollars, size, *, status="active", event=EVENT):
    return {
        "event_ticker": event, "ticker": f"{event}-{yes_sub_title[:3].upper()}",
        "yes_sub_title": yes_sub_title, "status": status,
        "yes_ask_dollars": yes_ask_dollars, "yes_ask_size_fp": size,
        "yes_bid_dollars": "0.0100", "last_price_dollars": yes_ask_dollars,
    }


# USA 0.32->3.125, Tie 0.40->2.5, Paraguay 0.25->4.0; limits 500*.32=160, 100*.40=40, 200*.25=50.
def _usa_par_markets(order="normal"):
    usa = _mkt("USA", "0.3200", "500")
    tie = _mkt("Tie", "0.4000", "100")
    par = _mkt("Paraguay", "0.2500", "200")
    return {"normal": [usa, tie, par], "swapped": [par, tie, usa]}[order]


# --------------------------------------------------------------------------- #
# Units (phantom-arb guard)                                                     #
# --------------------------------------------------------------------------- #
def test_decimal_from_dollars_units():
    assert kalshi.decimal_from_dollars("0.3200") == 3.125    # dollars in -> 1/0.32, NOT /100 again
    assert kalshi.decimal_from_dollars(0.5) == 2.0
    assert kalshi.decimal_from_dollars("0") is None          # no two-sided ask
    assert kalshi.decimal_from_dollars("1.00") is None
    assert kalshi.decimal_from_dollars(None) is None


def test_leg_limit_is_size_times_price():
    assert kalshi.leg_limit("500", "0.3200") == 160.0
    assert kalshi.leg_limit(0, "0.3200") == 0.0
    assert kalshi.leg_limit("bad", "0.32") == 0.0


# --------------------------------------------------------------------------- #
# Mapping by yes_sub_title identity                                             #
# --------------------------------------------------------------------------- #
def _assert_usa_par_mapping(raw):
    outs = raw["idBBB"]["bookmakerOdds"]["kalshi"]["markets"]["101"]["outcomes"]
    home, draw, away = outs["101"]["players"]["0"], outs["102"]["players"]["0"], outs["103"]["players"]["0"]
    assert home["price"] == 3.125 and home["limit"] == 160.0      # USA  (p1 -> home)
    assert draw["price"] == 2.5 and draw["limit"] == 40.0         # Tie  -> draw
    assert away["price"] == 4.0 and away["limit"] == 50.0         # Paraguay (p2 -> away)
    # Real limits => legs carry a number, NOT None (so the engine won't blanket-mark low_confidence).
    assert all(p["limit"] is not None for p in (home, draw, away))
    assert home["changedAt"] == "2026-06-13T10:00:00Z"            # scan time, not a stale line


def test_merge_maps_by_yes_sub_title_identity():
    raw: dict = {}
    cov, kbooks = kalshi.merge_into(raw, BY_FIXTURE, INDEX, _usa_par_markets("normal"), now=NOW, log=LOG)
    assert cov.matched == 1 and cov.recovered == 1
    assert kbooks == {"idBBB": {"kalshi"}}
    _assert_usa_par_mapping(raw)


def test_merge_maps_regardless_of_market_order():
    """Outcomes are keyed by yes_sub_title identity, never ticker order: a swapped list maps the same."""
    raw: dict = {}
    cov, kbooks = kalshi.merge_into(raw, BY_FIXTURE, INDEX, _usa_par_markets("swapped"), now=NOW, log=LOG)
    assert cov.matched == 1 and kbooks == {"idBBB": {"kalshi"}}
    _assert_usa_par_mapping(raw)   # identical home/draw/away despite [Paraguay, Tie, USA] order


# --------------------------------------------------------------------------- #
# Safety: never guess an unmatched fixture; skip non-active markets             #
# --------------------------------------------------------------------------- #
def test_merge_drops_unmatched_fixture():
    by_fixture = {"idAAA": {"p1": "Australia", "p2": "Turkiye",
                            "start_time": "2026-06-13T19:00:00.000Z", "status_id": 0}}
    raw: dict = {}
    cov, kbooks = kalshi.merge_into(raw, by_fixture, INDEX, _usa_par_markets("normal"), now=NOW, log=LOG)
    assert cov.matched == 0 and cov.recovered == 0
    assert kbooks == {} and raw == {}                            # injected nothing, created no envelope
    assert EVENT in cov.unmatched_name


def test_merge_skips_non_active_market_as_incomplete():
    markets = [_mkt("USA", "0.3200", "500"), _mkt("Tie", "0.4000", "100", status="settled"),
               _mkt("Paraguay", "0.2500", "200")]
    raw: dict = {}
    cov, kbooks = kalshi.merge_into(raw, BY_FIXTURE, INDEX, markets, now=NOW, log=LOG)
    assert cov.recovered == 0 and kbooks == {}                  # Tie inactive -> not 1 Tie + 2 teams
    assert EVENT in cov.incomplete


# --------------------------------------------------------------------------- #
# Shadow rollout gate in run._scan                                              #
# --------------------------------------------------------------------------- #
def _cfg(kalshi_actionable=False):
    raw = {
        "target_window": {"from_utc": "2026-06-10T00:00:00Z", "to_utc": "2026-06-16T23:59:59Z"},
        "thresholds": {"min_roi_pct": 0.5, "roi_suspicious_pct": 8.0, "min_total_stake": 20,
                       "max_leg_age_far_minutes": 360, "max_leg_age_mid_minutes": 60,
                       "max_leg_age_near_minutes": 20, "stale_far_horizon_hours": 6,
                       "stale_near_horizon_hours": 1, "near_miss_ceiling_S": 1.02},
        "markets": {"allow_quarter_lines": False},
        "kalshi": {"actionable": kalshi_actionable},
    }
    return Config(raw=raw, secrets=Secrets(None, None, None))


def _kalshi_arb_feeds():
    """1x2 fixture where kalshi(direct) + pinnacle is a >0-ROI arb (pinnacle alone is no arb)."""
    ko = "2026-06-13T19:00:00.000Z"
    cu = "2026-06-13T09:50:00Z"

    def book(h, d, a):
        def leg(p):
            return {"players": {"0": {"price": p, "limit": 500, "changedAt": cu, "mainLine": True, "active": True}}}
        return {"bookmakerIsActive": True, "suspended": False,
                "markets": {"101": {"marketActive": True, "outcomes": {"101": leg(h), "102": leg(d), "103": leg(a)}}}}

    raw = [{"fixtureId": "idBBB", "startTime": ko, "statusId": 0, "hasOdds": True,
            "bookmakerOdds": {"pinnacle": book(2.1, 3.0, 3.0), "kalshi": book(1.9, 3.6, 4.2)}}]
    return parse_odds_payload(raw)


def _ctx():
    return EngineCtx(actionable={"pinnacle", "kalshi"}, tracked={"pinnacle", "kalshi"},
                     exchanges=set(), commission={}, clone_group_of=build_clone_group_fn([]),
                     reference_books=[])


def test_kalshi_leg_not_actionable_while_shadow():
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    kbooks = {"idBBB": {"kalshi"}}
    opps, stats = _scan(_kalshi_arb_feeds(), specs, _ctx(), _cfg(kalshi_actionable=False),
                        BY_FIXTURE, {}, NOW, LOG, {}, kbooks)
    assert stats["shadow_arbs"] >= 1
    assert all(o.actionable is False for o in opps)   # kalshi-direct leg forced shadow


def test_kalshi_leg_actionable_when_gate_open():
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    kbooks = {"idBBB": {"kalshi"}}
    opps, _ = _scan(_kalshi_arb_feeds(), specs, _ctx(), _cfg(kalshi_actionable=True),
                    BY_FIXTURE, {}, NOW, LOG, {}, kbooks)
    assert any(o.actionable for o in opps)
