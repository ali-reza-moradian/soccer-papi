"""End-to-end-ish tests: normalize an odds payload, run the scan, check CSV dedup."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.catalog import build_clone_group_fn, build_market_specs
from src.config import Config, Secrets
from src.csv_store import append_opportunities
from src.logsetup import get_logger
from src.normalize import parse_odds_payload, seen_market_ids
from src.run import EngineCtx, _scan

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
STALE = (NOW - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
KICKOFF = "2026-06-07T19:00:00.000Z"


def _player(price, limit, changed=RECENT):
    return {"0": {"active": True, "price": price, "priceAmerican": "100", "limit": limit,
                  "changedAt": changed, "mainLine": True, "exchangeMeta": None}}


def _book_block(over_price, under_price, over_limit=1500, under_limit=5000, market="106"):
    return {
        "bookmakerIsActive": True, "suspended": False, "fixturePath": "https://x/y",
        "markets": {market: {"marketActive": True, "outcomes": {
            "1": {"players": _player(over_price, over_limit)},
            "2": {"players": _player(under_price, under_limit)},
        }}},
    }


MARKETS_JSON = [
    {"marketId": 106, "marketName": "Total Goals Over/Under 2.5", "marketType": "totals",
     "sportId": 10, "period": "fulltime", "handicap": 2.5, "playerProp": False,
     "outcomes": [{"outcomeId": 1, "outcomeName": "Over"}, {"outcomeId": 2, "outcomeName": "Under"}]},
]
BOOKS_JSON = [
    {"slug": "pinnacle", "bookmakerName": "Pinnacle", "cloneOf": None},
    {"slug": "1xbet", "bookmakerName": "1xBet", "cloneOf": None},
    {"slug": "stake", "bookmakerName": "Stake", "cloneOf": None},
]


def _payload(over_book, under_book):
    """Two outcomes priced across two books to make a clean arb."""
    return [{
        "fixtureId": "fx1", "participant1Id": 35, "participant2Id": 34, "tournamentId": 17,
        "statusId": 0, "hasOdds": True, "startTime": KICKOFF, "updatedAt": RECENT,
        "bookmakerOdds": {
            over_book: {"bookmakerIsActive": True, "suspended": False, "fixturePath": "https://o",
                        "markets": {"106": {"marketActive": True, "outcomes": {
                            "1": {"players": _player(2.10, 1500)},
                            "2": {"players": _player(1.50, 1500)}}}}},
            under_book: {"bookmakerIsActive": True, "suspended": False, "fixturePath": "https://u",
                         "markets": {"106": {"marketActive": True, "outcomes": {
                             "1": {"players": _player(1.50, 5000)},
                             "2": {"players": _player(2.05, 5000)}}}}},
        },
    }]


def _ctx(group_of, actionable, tracked):
    return EngineCtx(
        actionable=set(actionable), tracked=set(tracked), exchanges=set(),
        commission={}, clone_group_of=group_of, max_leg_age_minutes=20, unknown_limit_fallback=100,
    )


def _cfg():
    raw = {
        "target_window": {"from_utc": "2026-06-05T00:00:00Z", "to_utc": "2026-06-08T23:59:59Z"},
        "thresholds": {"min_roi_pct": 0.5, "roi_suspicious_pct": 8.0, "min_total_stake": 20,
                       "max_leg_age_minutes": 20, "near_miss_ceiling_S": 1.02},
        "markets": {"allow_quarter_lines": False},
        "telegram": {"rank_by": "profit"},
    }
    return Config(raw=raw, secrets=Secrets(None, None, None))


def test_normalize_parses_books_and_markets():
    feeds = parse_odds_payload(_payload("pinnacle", "1xbet"))
    assert len(feeds) == 1
    f = feeds[0]
    assert f.fixture_id == "fx1"
    assert f.books_present == {"pinnacle", "1xbet"}
    assert seen_market_ids(feeds) == {106}
    assert f.markets[106][1]  # Over has candidates
    assert f.markets[106][2]  # Under has candidates


def test_scan_finds_real_arb():
    feeds = parse_odds_payload(_payload("pinnacle", "1xbet"))
    specs, _ = build_market_specs(MARKETS_JSON, 10, ["double chance"])
    group_of = build_clone_group_fn(BOOKS_JSON)
    ctx = _ctx(group_of, ["pinnacle", "1xbet"], ["pinnacle", "1xbet", "stake"])
    opps, stats = _scan(feeds, specs, ctx, _cfg(), {}, {"35": "USA", "34": "Germany"},
                        NOW, get_logger("test"))
    assert stats["real_arbs"] == 1
    opp = opps[0]
    assert opp.actionable
    assert opp.res.is_arb
    assert opp.match == "USA vs Germany"
    # Best Over @ pinnacle 2.10, best Under @ 1xbet 2.05 -> the worked-example arb.
    books = {leg.book for leg in opp.res.legs}
    assert books == {"pinnacle", "1xbet"}


def test_shadow_arb_when_best_leg_is_unfunded():
    # Best Under is on 'stake' (unfunded). pinnacle/1xbet only have weak under prices.
    payload = [{
        "fixtureId": "fx2", "participant1Id": 35, "participant2Id": 34, "tournamentId": 17,
        "statusId": 0, "hasOdds": True, "startTime": KICKOFF, "updatedAt": RECENT,
        "bookmakerOdds": {
            "pinnacle": {"bookmakerIsActive": True, "suspended": False, "fixturePath": "p",
                         "markets": {"106": {"marketActive": True, "outcomes": {
                             "1": {"players": _player(2.10, 1500)},
                             "2": {"players": _player(1.80, 1500)}}}}},
            "stake": {"bookmakerIsActive": True, "suspended": False, "fixturePath": "s",
                      "markets": {"106": {"marketActive": True, "outcomes": {
                          "1": {"players": _player(1.80, 4000)},
                          "2": {"players": _player(2.05, 4000)}}}}},
        },
    }]
    feeds = parse_odds_payload(payload)
    specs, _ = build_market_specs(MARKETS_JSON, 10, ["double chance"])
    group_of = build_clone_group_fn(BOOKS_JSON)
    ctx = _ctx(group_of, ["pinnacle", "1xbet"], ["pinnacle", "1xbet", "stake"])
    opps, stats = _scan(feeds, specs, ctx, _cfg(), {}, {"35": "USA", "34": "Germany"},
                        NOW, get_logger("test"))
    # Actionable-only (pinnacle vs pinnacle) is not a valid 2-source arb here, so it's a shadow arb.
    assert stats["shadow_arbs"] == 1
    assert stats["real_arbs"] == 0
    assert stats["shadow_book_counter"]["stake"] == 1


def test_stale_legs_are_skipped():
    payload = _payload("pinnacle", "1xbet")
    # Make the 1xbet Under price stale.
    payload[0]["bookmakerOdds"]["1xbet"]["markets"]["106"]["outcomes"]["2"]["players"]["0"]["changedAt"] = STALE
    feeds = parse_odds_payload(payload)
    specs, _ = build_market_specs(MARKETS_JSON, 10, ["double chance"])
    group_of = build_clone_group_fn(BOOKS_JSON)
    ctx = _ctx(group_of, ["pinnacle", "1xbet"], ["pinnacle", "1xbet", "stake"])
    opps, stats = _scan(feeds, specs, ctx, _cfg(), {}, {"35": "USA", "34": "Germany"},
                        NOW, get_logger("test"))
    # Under now only available stale -> incomplete market -> no arb.
    assert stats["real_arbs"] == 0


class _FakeOddsClient:
    """Returns a different single-book payload per `bookmaker`, like the free tier does."""

    def __init__(self, per_book):
        self.per_book = per_book
        self.billable_count = 0

    def odds_by_tournaments(self, ids, bookmaker=None, verbosity=3, odds_format="decimal", language="en"):
        self.billable_count += 1
        return self.per_book.get(bookmaker, [])


def _one_book_payload(book, over_price, under_price):
    return [{
        "fixtureId": "fx1", "participant1Id": 35, "participant2Id": 34, "tournamentId": 17,
        "statusId": 0, "hasOdds": True, "startTime": KICKOFF, "updatedAt": RECENT,
        "bookmakerOdds": {book: {"bookmakerIsActive": True, "suspended": False, "fixturePath": book,
            "markets": {"106": {"marketActive": True, "outcomes": {
                "1": {"players": _player(over_price, 1500)},
                "2": {"players": _player(under_price, 5000)}}}}}},
    }]


def test_fetch_odds_per_book_merges_books_onto_one_fixture():
    from src.run import _fetch_odds_per_book
    client = _FakeOddsClient({
        "pinnacle": _one_book_payload("pinnacle", 2.10, 1.55),
        "1xbet": _one_book_payload("1xbet", 1.55, 2.05),
    })
    feeds, fetched, returning = _fetch_odds_per_book(
        client, _cfg(), [17], ["pinnacle", "1xbet"], start_remaining=200, safety=15, log=get_logger("t"))
    assert client.billable_count == 2
    assert fetched == ["pinnacle", "1xbet"]
    assert returning == ["pinnacle", "1xbet"]
    assert len(feeds) == 1
    # Both single-book calls merged onto the same canonical fixture.
    assert feeds[0].books_present == {"pinnacle", "1xbet"}


def test_fetch_stops_at_budget_margin():
    from src.run import _fetch_odds_per_book
    client = _FakeOddsClient({b: _one_book_payload(b, 2.0, 2.0) for b in ["a", "b", "c", "d"]})
    # start_remaining 17, safety 15 -> can afford exactly 2 calls (17->16->stop at 15).
    feeds, fetched, returning = _fetch_odds_per_book(
        client, _cfg(), [17], ["a", "b", "c", "d"], start_remaining=17, safety=15, log=get_logger("t"))
    assert client.billable_count == 2
    assert fetched == ["a", "b"]


def test_fixture_list_handles_shapes():
    from src.run import _fixture_list
    assert _fixture_list([{"fixtureId": "x"}]) == [{"fixtureId": "x"}]
    assert _fixture_list({"fixtures": [{"fixtureId": "y"}]}) == [{"fixtureId": "y"}]
    keyed = {"id1": {"fixtureId": "id1", "bookmakerOdds": {}}}
    assert _fixture_list(keyed) == [{"fixtureId": "id1", "bookmakerOdds": {}}]
    assert _fixture_list(None) == []


def test_csv_dedup_updates_within_window():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "arb.csv")
        row = {"signature": "abc", "detected_at_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "roi_pct": 3.7, "match": "USA vs Germany", "market": "O/U 2.5"}
        c1 = append_opportunities(path, [row], NOW, dedup_minutes=90)
        assert c1 == {"new": 1, "updated": 0}
        # 10 minutes later, same signature -> update in place.
        later = NOW + timedelta(minutes=10)
        c2 = append_opportunities(path, [row], later, dedup_minutes=90)
        assert c2 == {"new": 0, "updated": 1}
        # 2 hours later -> outside window -> new row appended.
        much_later = NOW + timedelta(minutes=200)
        c3 = append_opportunities(path, [row], much_later, dedup_minutes=90)
        assert c3 == {"new": 1, "updated": 0}
