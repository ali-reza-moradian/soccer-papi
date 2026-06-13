"""Tests for the the-odds-api supplemental source: normalization, market indexing, fixture
matching (the phantom-arb surfaces), gap-fill merge, spread sign mapping, and the shadow-only
rollout safety enforced in run._scan."""
from __future__ import annotations

from datetime import datetime, timezone

from src.config import Config, Secrets
from src.logsetup import get_logger
from src.normalize import parse_odds_payload
from src.run import EngineCtx, _scan
from src.catalog import build_clone_group_fn, build_market_specs
from src import theoddsapi as toa

LOG = get_logger("test-toa")

# --- a tiny markets catalog covering the three families we map ------------------
MARKETS_JSON = [
    {"marketId": 101, "sportId": 10, "marketType": "1x2", "period": "fulltime", "handicap": 0,
     "marketName": "Full Time Result",
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 102, "outcomeName": "X"},
                  {"outcomeId": 103, "outcomeName": "2"}]},
    {"marketId": 1010, "sportId": 10, "marketType": "totals", "period": "fulltime", "handicap": 2.5,
     "marketName": "Over Under Full Time",
     "outcomes": [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]},
    {"marketId": 1500, "sportId": 10, "marketType": "spreads", "period": "fulltime", "handicap": -1.5,
     "marketName": "Asian Handicap",
     "outcomes": [{"outcomeId": 1500, "outcomeName": "1"}, {"outcomeId": 1501, "outcomeName": "2"}]},
    {"marketId": 1502, "sportId": 10, "marketType": "spreads", "period": "fulltime", "handicap": 1.5,
     "marketName": "Asian Handicap",
     "outcomes": [{"outcomeId": 1502, "outcomeName": "1"}, {"outcomeId": 1503, "outcomeName": "2"}]},
]

BY_FIXTURE = {
    "idAAA": {"p1": "Australia", "p2": "Turkiye", "start_time": "2026-06-14T04:00:00.000Z", "status_id": 0,
              "tournament": "World Cup"},
    "idBBB": {"p1": "USA", "p2": "Paraguay", "start_time": "2026-06-13T01:00:00.000Z", "status_id": 0,
              "tournament": "World Cup"},
}


# --------------------------------------------------------------------------- #
# Normalization + equivalence                                                   #
# --------------------------------------------------------------------------- #
def test_team_equivalence_across_providers():
    assert toa.normalize_team("Turkey") == toa.normalize_team("Turkiye")
    assert toa.normalize_team("United States") == toa.normalize_team("USA")
    assert toa.normalize_team("Côte d'Ivoire") == toa.normalize_team("Ivory Coast")
    assert toa.normalize_team("Curaçao") == toa.normalize_team("Curacao")
    assert toa.normalize_team("South Korea") == toa.normalize_team("Korea Republic")
    assert toa.normalize_team("Czechia") == toa.normalize_team("Czech Republic")
    assert toa.normalize_team("Bosnia") == toa.normalize_team("Bosnia and Herzegovina")
    # Distinct teams must NOT collapse.
    assert toa.normalize_team("Korea Republic") != toa.normalize_team("Korea DPR")


def test_book_alias():
    assert toa.canonical_book_slug("onexbet") == "1xbet"
    assert toa.canonical_book_slug("BET365") == "bet365"
    assert toa.canonical_book_slug("unknownbook") == "unknownbook"  # identity fallthrough


# --------------------------------------------------------------------------- #
# Market reverse-index                                                           #
# --------------------------------------------------------------------------- #
def test_market_index():
    idx = toa.build_market_index(MARKETS_JSON, sport_id=10)
    assert idx.h2h == {"marketId": 101, "home_oid": 101, "draw_oid": 102, "away_oid": 103}
    assert idx.totals[2.5]["marketId"] == 1010
    assert idx.spreads[-1.5] == {"marketId": 1500, "home_oid": 1500, "away_oid": 1501}
    assert not idx.ambiguous


# --------------------------------------------------------------------------- #
# Fixture matching — the phantom-arb gate                                        #
# --------------------------------------------------------------------------- #
def test_match_ok_normal_and_flipped_orientation():
    # the-odds-api lists Turkiye as home; canonical p1 is Australia -> home_is_p1 must be False.
    m, reason = toa.match_event_to_fixture("Turkey", "Australia", "2026-06-14T04:00:00Z", BY_FIXTURE, 120)
    assert reason == "ok" and m.fixture_id == "idAAA" and m.home_is_p1 is False
    m2, _ = toa.match_event_to_fixture("Australia", "Turkiye", "2026-06-14T04:05:00Z", BY_FIXTURE, 120)
    assert m2.home_is_p1 is True


def test_match_rejects_unknown_team_and_bad_time():
    assert toa.match_event_to_fixture("Australia", "Narnia", "2026-06-14T04:00:00Z", BY_FIXTURE, 120)[1] == "unmatched_name"
    assert toa.match_event_to_fixture("Australia", "Turkiye", "2026-06-14T12:00:00Z", BY_FIXTURE, 120)[1] == "time_mismatch"


# --------------------------------------------------------------------------- #
# Merge — gap-fill, identity-based home/away, spread sign                        #
# --------------------------------------------------------------------------- #
def _event(home, away, commence, bookmakers):
    return {"home_team": home, "away_team": away, "commence_time": commence, "bookmakers": bookmakers}


def _bm(key, markets, last_update="2026-06-14T03:50:00Z"):
    return {"key": key, "last_update": last_update, "markets": markets}


def test_merge_h2h_maps_by_identity_not_tag():
    idx = toa.build_market_index(MARKETS_JSON, 10)
    # Provider lists home=Turkiye away=Australia (reversed vs canonical p1=Australia/p2=Turkiye).
    payload = [_event("Turkiye", "Australia", "2026-06-14T04:00:00Z", [
        _bm("onexbet", [{"key": "h2h", "outcomes": [
            {"name": "Turkiye", "price": 2.5}, {"name": "Australia", "price": 3.0}, {"name": "Draw", "price": 3.2}]}]),
    ])]
    raw: dict = {}
    cov, toa_books = toa.merge_into(raw, BY_FIXTURE, idx, {"1xbet"}, payload,
                                    tolerance_minutes=120, allow_books={"1xbet"}, cost_credits=3, log=LOG)
    assert cov.matched == 1 and toa_books["idAAA"] == {"1xbet"}
    legs = raw["idAAA"]["bookmakerOdds"]["1xbet"]["markets"]["101"]["outcomes"]
    # Australia is canonical p1 -> outcomeId 101 regardless of the provider's home tag.
    assert legs["101"]["players"]["0"]["price"] == 3.0   # Australia (p1)
    assert legs["103"]["players"]["0"]["price"] == 2.5   # Turkiye (p2)
    assert legs["102"]["players"]["0"]["price"] == 3.2   # Draw
    # Every leg low_confidence: limit None, changedAt = last_update.
    assert legs["101"]["players"]["0"]["limit"] is None
    assert legs["101"]["players"]["0"]["changedAt"] == "2026-06-14T03:50:00Z"


def test_merge_spread_sign_maps_home_point_to_handicap():
    idx = toa.build_market_index(MARKETS_JSON, 10)
    # Australia (p1) at -1.5; Turkiye (p2) at +1.5 -> OddsPapi marketId 1500 (handicap -1.5 on '1').
    payload = [_event("Australia", "Turkiye", "2026-06-14T04:00:00Z", [
        _bm("onexbet", [{"key": "spreads", "outcomes": [
            {"name": "Australia", "price": 1.9, "point": -1.5}, {"name": "Turkiye", "price": 1.9, "point": 1.5}]}]),
    ])]
    raw: dict = {}
    toa.merge_into(raw, BY_FIXTURE, idx, {"1xbet"}, payload,
                   tolerance_minutes=120, allow_books={"1xbet"}, cost_credits=3, log=LOG)
    mkt = raw["idAAA"]["bookmakerOdds"]["1xbet"]["markets"]["1500"]["outcomes"]
    assert mkt["1500"]["players"]["0"]["price"] == 1.9   # home/p1 line
    assert mkt["1501"]["players"]["0"]["price"] == 1.9   # away/p2 line


def test_merge_spread_rejects_asymmetric_points():
    idx = toa.build_market_index(MARKETS_JSON, 10)
    payload = [_event("Australia", "Turkiye", "2026-06-14T04:00:00Z", [
        _bm("onexbet", [{"key": "spreads", "outcomes": [
            {"name": "Australia", "price": 1.9, "point": -1.5}, {"name": "Turkiye", "price": 1.9, "point": 2.0}]}]),
    ])]
    raw: dict = {}
    cov, toa_books = toa.merge_into(raw, BY_FIXTURE, idx, {"1xbet"}, payload,
                                    tolerance_minutes=120, allow_books={"1xbet"}, cost_credits=3, log=LOG)
    assert "idAAA" not in toa_books  # asymmetric -> no leg emitted


def test_merge_defers_to_active_oddspapi_book():
    idx = toa.build_market_index(MARKETS_JSON, 10)
    payload = [_event("Australia", "Turkiye", "2026-06-14T04:00:00Z", [
        _bm("onexbet", [{"key": "h2h", "outcomes": [
            {"name": "Australia", "price": 3.0}, {"name": "Turkiye", "price": 2.5}, {"name": "Draw", "price": 3.2}]}]),
    ])]
    # OddsPapi already supplied an active 1xbet for this fixture -> defer, do not overwrite.
    raw = {"idAAA": {"fixtureId": "idAAA", "startTime": "2026-06-14T04:00:00.000Z",
                     "bookmakerOdds": {"1xbet": {"bookmakerIsActive": True, "suspended": False,
                                                 "markets": {"101": {"marketActive": True, "outcomes": {}}}}}}}
    cov, toa_books = toa.merge_into(raw, BY_FIXTURE, idx, {"1xbet"}, payload,
                                    tolerance_minutes=120, allow_books={"1xbet"}, cost_credits=3, log=LOG)
    assert cov.deferred.get("1xbet") == 1 and "idAAA" not in toa_books


def test_merge_skips_books_not_in_catalog():
    idx = toa.build_market_index(MARKETS_JSON, 10)
    payload = [_event("Australia", "Turkiye", "2026-06-14T04:00:00Z", [
        _bm("mysterybook", [{"key": "h2h", "outcomes": [
            {"name": "Australia", "price": 3.0}, {"name": "Turkiye", "price": 2.5}, {"name": "Draw", "price": 3.2}]}]),
    ])]
    raw: dict = {}
    cov, toa_books = toa.merge_into(raw, BY_FIXTURE, idx, {"1xbet"}, payload,
                                    tolerance_minutes=120, allow_books=None, cost_credits=3, log=LOG)
    assert cov.unknown_books.get("mysterybook") == 1 and not toa_books


# --------------------------------------------------------------------------- #
# Rollout safety — the-odds-api leg cannot be actionable while flag is false     #
# --------------------------------------------------------------------------- #
def _cfg(theoddsapi_actionable=False, actionable_books=None):
    toa: dict = {"actionable": theoddsapi_actionable}
    if actionable_books is not None:
        toa["actionable_books"] = actionable_books
    raw = {
        "target_window": {"from_utc": "2026-06-10T00:00:00Z", "to_utc": "2026-06-16T23:59:59Z"},
        "thresholds": {"min_roi_pct": 0.5, "roi_suspicious_pct": 8.0, "min_total_stake": 20,
                       "max_leg_age_far_minutes": 360, "max_leg_age_mid_minutes": 60,
                       "max_leg_age_near_minutes": 20, "stale_far_horizon_hours": 6,
                       "stale_near_horizon_hours": 1, "near_miss_ceiling_S": 1.02},
        "markets": {"allow_quarter_lines": False},
        "theoddsapi": toa,
    }
    return Config(raw=raw, secrets=Secrets(None, None, None))


def _ctx(actionable, tracked):
    return EngineCtx(actionable=set(actionable), tracked=set(tracked), exchanges=set(),
                     commission={}, clone_group_of=build_clone_group_fn([]),
                     reference_books=[])


def _h2h_fixture_with_arb():
    """pinnacle (suspended) + the-odds-api 1xbet + pinnacle... build a fixture where 1xbet(toa) and
    pinnacle together would be a >0 ROI arb on the 1x2 market."""
    now = datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc)
    ko = "2026-06-14T04:00:00.000Z"
    cu = "2026-06-14T03:50:00Z"

    def book(markets):
        return {"bookmakerIsActive": True, "suspended": False, "markets": markets}

    def leg(price):
        return {"players": {"0": {"price": price, "limit": None, "changedAt": cu, "mainLine": True, "active": True}}}

    # Cross-book arb on 1x2 that REQUIRES the 1xbet(toa) leg: pinnacle alone is no arb (S=1.14),
    # but best-of-both (pinnacle home + 1xbet draw/away) gives S=0.99. No pure-actionable arb exists.
    pinn = book({"101": {"marketActive": True, "outcomes": {"101": leg(2.1), "102": leg(3.0), "103": leg(3.0)}}})
    onex = book({"101": {"marketActive": True, "outcomes": {"101": leg(1.9), "102": leg(3.6), "103": leg(4.2)}}})
    raw = [{"fixtureId": "idAAA", "startTime": ko, "statusId": 0, "hasOdds": True,
            "bookmakerOdds": {"pinnacle": pinn, "1xbet": onex}}]
    return now, parse_odds_payload(raw)


def test_toa_leg_not_actionable_when_flag_false():
    now, feeds = _h2h_fixture_with_arb()
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    ctx = _ctx(actionable=["pinnacle", "1xbet"], tracked=["pinnacle", "1xbet"])
    toa_books = {"idAAA": {"1xbet"}}  # 1xbet came from the-odds-api on this fixture

    opps, stats = _scan(feeds, specs, ctx, _cfg(theoddsapi_actionable=False), BY_FIXTURE, {}, now, LOG, toa_books)
    # A shadow arb exists, but nothing actionable (the 1xbet leg is the-odds-api-sourced).
    assert stats["shadow_arbs"] >= 1
    assert all(o.actionable is False for o in opps)


def test_toa_leg_actionable_when_flag_true():
    now, feeds = _h2h_fixture_with_arb()
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    ctx = _ctx(actionable=["pinnacle", "1xbet"], tracked=["pinnacle", "1xbet"])
    toa_books = {"idAAA": {"1xbet"}}

    opps, stats = _scan(feeds, specs, ctx, _cfg(theoddsapi_actionable=True), BY_FIXTURE, {}, now, LOG, toa_books)
    # h2h is not a spread, so with the flag on the recovered 1xbet leg may now be actionable.
    assert any(o.actionable for o in opps)


def _two_recovered_book_fixtures():
    """Two 1x2 fixtures, each a >0-ROI arb that REQUIRES its recovered the-odds-api leg:
    idAAA recovered via 1xbet(toa), idBBB recovered via coolbet(toa). pinnacle alone is no arb
    (S>1); best-of-both clears S<1 only by using the recovered leg."""
    now = datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc)
    ko = "2026-06-13T08:00:00.000Z"          # 8h out -> far staleness bucket
    cu = "2026-06-12T23:50:00Z"              # 10 min before `now` -> fresh

    def book(markets):
        return {"bookmakerIsActive": True, "suspended": False, "markets": markets}

    def leg(price):
        return {"players": {"0": {"price": price, "limit": None, "changedAt": cu, "mainLine": True, "active": True}}}

    def mkt(h, d, a):
        return {"101": {"marketActive": True, "outcomes": {"101": leg(h), "102": leg(d), "103": leg(a)}}}

    pinn = book(mkt(2.1, 3.0, 3.0))
    soft = book(mkt(1.9, 3.6, 4.2))          # best home@pinn + draw/away@soft -> S~0.99
    raw = [
        {"fixtureId": "idAAA", "startTime": ko, "statusId": 0, "hasOdds": True,
         "bookmakerOdds": {"pinnacle": pinn, "1xbet": soft}},
        {"fixtureId": "idBBB", "startTime": ko, "statusId": 0, "hasOdds": True,
         "bookmakerOdds": {"pinnacle": pinn, "coolbet": soft}},
    ]
    return now, parse_odds_payload(raw)


def test_actionable_books_gate_allows_1xbet_blocks_soft_book():
    """Master switch on + theoddsapi.actionable_books=[1xbet]: a recovered 1xBet leg may turn an
    arb actionable, but a recovered soft book (coolbet) — even one placed in the actionable
    UNIVERSE — can NEVER make an arb actionable. The per-book gate is the real control."""
    now, feeds = _two_recovered_book_fixtures()
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    # Both recovered books sit in the actionable universe; only the per-book allow-list differs them.
    ctx = _ctx(actionable=["pinnacle", "1xbet", "coolbet"], tracked=["pinnacle", "1xbet", "coolbet"])
    toa_books = {"idAAA": {"1xbet"}, "idBBB": {"coolbet"}}
    cfg = _cfg(theoddsapi_actionable=True, actionable_books=["1xbet"])

    opps, _ = _scan(feeds, specs, ctx, cfg, BY_FIXTURE, {}, now, LOG, toa_books)
    by_match = {o.match: o for o in opps}
    onexbet_opp = by_match["Australia vs Turkiye"]   # idAAA — recovered via 1xbet (allow-listed)
    coolbet_opp = by_match["USA vs Paraguay"]         # idBBB — recovered via coolbet (soft, blocked)
    assert onexbet_opp.actionable is True
    assert coolbet_opp.actionable is False
