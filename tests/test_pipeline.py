"""End-to-end-ish tests: normalize an odds payload, run the scan, check CSV dedup."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

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
    {"slug": "cloudbet", "bookmakerName": "Cloudbet", "cloneOf": None},
    {"slug": "polymarket", "bookmakerName": "Polymarket", "cloneOf": None},
    {"slug": "bcgame", "bookmakerName": "BC.Game", "cloneOf": None},
]

# A 3-way 1x2 market (outcomeId 101='1'/home, 102='X'/draw, 103='2'/away) for mapping-guard tests.
MARKETS_1X2 = [
    {"marketId": 101, "marketName": "Full Time Result", "marketType": "1x2",
     "sportId": 10, "period": "fulltime", "handicap": 0, "playerProp": False,
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"},
                  {"outcomeId": 102, "outcomeName": "X"},
                  {"outcomeId": 103, "outcomeName": "2"}]},
]


def _book_1x2(home, draw, away, limit=3000):
    return {"bookmakerIsActive": True, "suspended": False, "fixturePath": "x",
            "markets": {"101": {"marketActive": True, "outcomes": {
                "101": {"players": _player(home, limit)},
                "102": {"players": _player(draw, limit)},
                "103": {"players": _player(away, limit)}}}}}


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


def _ctx(group_of, actionable, tracked, reference_books=(), min_favorite_ratio=1.5):
    return EngineCtx(
        actionable=set(actionable), tracked=set(tracked), exchanges=set(),
        commission={}, clone_group_of=group_of, max_leg_age_minutes=20, unknown_limit_fallback=100,
        reference_books=list(reference_books), min_favorite_ratio=min_favorite_ratio,
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


def test_scan_pairs_two_books_without_forcing_prediction_market():
    """A clean pinnacle+1xbet arb is caught even when Polymarket is present with worse odds —
    no crypto prediction market (Polymarket/Kalshi) is forced into the arb (change #3)."""
    payload = _payload("pinnacle", "1xbet")
    # Same fixture also quotes Polymarket, but with deliberately worse prices on both outcomes.
    payload[0]["bookmakerOdds"]["polymarket"] = {
        "bookmakerIsActive": True, "suspended": False, "fixturePath": "pm",
        "markets": {"106": {"marketActive": True, "outcomes": {
            "1": {"players": _player(1.40, 9000)},
            "2": {"players": _player(1.40, 9000)}}}}}
    feeds = parse_odds_payload(payload)
    specs, _ = build_market_specs(MARKETS_JSON, 10, ["double chance"])
    group_of = build_clone_group_fn(BOOKS_JSON)
    ctx = _ctx(group_of, ["pinnacle", "1xbet", "polymarket"],
               ["pinnacle", "1xbet", "polymarket", "stake", "cloudbet"])
    opps, stats = _scan(feeds, specs, ctx, _cfg(), {}, {"35": "USA", "34": "Germany"},
                        NOW, get_logger("test"))
    assert stats["real_arbs"] == 1
    # Polymarket is in the universe but NOT selected — the arb is pure pinnacle+1xbet.
    assert {leg.book for leg in opps[0].res.legs} == {"pinnacle", "1xbet"}


def test_scan_finds_arb_between_two_tracked_only_books():
    """A perfect Stake+Cloudbet arb is caught (as a shadow arb here) — any pair works (change #3)."""
    payload = [{
        "fixtureId": "fx3", "participant1Id": 35, "participant2Id": 34, "tournamentId": 17,
        "statusId": 0, "hasOdds": True, "startTime": KICKOFF, "updatedAt": RECENT,
        "bookmakerOdds": {
            "stake": {"bookmakerIsActive": True, "suspended": False, "fixturePath": "s",
                      "markets": {"106": {"marketActive": True, "outcomes": {
                          "1": {"players": _player(2.10, 3000)},
                          "2": {"players": _player(1.50, 3000)}}}}},
            "cloudbet": {"bookmakerIsActive": True, "suspended": False, "fixturePath": "c",
                         "markets": {"106": {"marketActive": True, "outcomes": {
                             "1": {"players": _player(1.50, 3000)},
                             "2": {"players": _player(2.05, 3000)}}}}},
        },
    }]
    feeds = parse_odds_payload(payload)
    specs, _ = build_market_specs(MARKETS_JSON, 10, ["double chance"])
    group_of = build_clone_group_fn(BOOKS_JSON)
    ctx = _ctx(group_of, ["pinnacle", "1xbet", "polymarket"],
               ["pinnacle", "1xbet", "polymarket", "stake", "cloudbet"])
    opps, stats = _scan(feeds, specs, ctx, _cfg(), {}, {"35": "USA", "34": "Germany"},
                        NOW, get_logger("test"))
    assert stats["shadow_arbs"] == 1
    assert stats["real_arbs"] == 0
    assert {leg.book for leg in opps[0].res.legs} == {"stake", "cloudbet"}
    assert opps[0].res.is_arb


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
        client, _cfg(), [17], ["pinnacle", "1xbet"], log=get_logger("t"))
    assert client.billable_count == 2
    assert fetched == ["pinnacle", "1xbet"]
    assert returning == ["pinnacle", "1xbet"]
    assert len(feeds) == 1
    # Both single-book calls merged onto the same canonical fixture.
    assert feeds[0].books_present == {"pinnacle", "1xbet"}


def test_fetch_all_books_no_cap():
    """Every requested book is fetched — there is no per-cycle budget cap anymore."""
    from src.run import _fetch_odds_per_book
    client = _FakeOddsClient({b: _one_book_payload(b, 2.0, 2.0) for b in ["a", "b", "c", "d"]})
    feeds, fetched, returning = _fetch_odds_per_book(
        client, _cfg(), [17], ["a", "b", "c", "d"], log=get_logger("t"))
    assert client.billable_count == 4
    assert fetched == ["a", "b", "c", "d"]


def test_fetch_propagates_quota_exceeded():
    """A 429/403 mid-fetch bubbles up (handled cleanly by main), not swallowed."""
    from src.oddspapi import QuotaExceeded
    from src.run import _fetch_odds_per_book

    class _Quota:
        billable_count = 1
        def odds_by_tournaments(self, *a, **k):
            raise QuotaExceeded("429 on /v4/odds-by-tournaments")

    with pytest.raises(QuotaExceeded):
        _fetch_odds_per_book(_Quota(), _cfg(), [17], ["pinnacle", "1xbet"], log=get_logger("t"))


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
        row = {"signature": "abc", "detected_at_et": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
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


# --------------------------------------------------------------------------- #
# Outcome-mapping guard (BC.Game-style home/away flip protection)               #
# --------------------------------------------------------------------------- #
def test_mapping_guard_flags_flipped_book():
    """A book whose favourite/underdog are swapped vs the reference is flagged suspect."""
    from src.run import _detect_mapping_suspects
    book_prices = {                                  # oid 101=home(fav), 102=draw, 103=away(dog)
        "pinnacle": {101: 1.65, 102: 3.8, 103: 5.0},
        "1xbet":    {101: 1.66, 102: 3.7, 103: 4.9},
        "stake":    {101: 1.64, 102: 3.9, 103: 5.1},
        "bcgame":   {101: 5.0,  102: 3.8, 103: 1.65},   # home/away swapped upstream
    }
    suspects, ref, extremes = _detect_mapping_suspects(book_prices, ["pinnacle", "1xbet"], 1.5)
    assert ref == "pinnacle"
    assert extremes == (101, 103)
    assert suspects == frozenset({"bcgame"})


def test_mapping_guard_near_even_market_is_not_judged():
    """No clear favourite (ratio gate fails) -> ordering is noise -> never flag."""
    from src.run import _detect_mapping_suspects
    book_prices = {"pinnacle": {101: 1.95, 103: 1.95}, "bcgame": {101: 1.96, 103: 1.94}}
    suspects, ref, extremes = _detect_mapping_suspects(book_prices, ["pinnacle"], 1.5)
    assert suspects == frozenset() and extremes is None


def test_mapping_guard_no_reference_present():
    """If no configured reference book priced the market, we cannot judge -> no flags."""
    from src.run import _detect_mapping_suspects
    book_prices = {"bcgame": {101: 5.0, 103: 1.65}, "stake": {101: 1.65, 103: 5.0}}
    suspects, ref, _ = _detect_mapping_suspects(book_prices, ["pinnacle"], 1.5)
    assert ref is None and suspects == frozenset()


def test_mapping_guard_two_books_defers_to_reference():
    """Reference + one flipped book = 1-vs-1 tie -> trust the reference, flag the other."""
    from src.run import _detect_mapping_suspects
    book_prices = {"pinnacle": {101: 1.65, 103: 5.0}, "bcgame": {101: 5.0, 103: 1.65}}
    suspects, _, _ = _detect_mapping_suspects(book_prices, ["pinnacle"], 1.5)
    assert suspects == frozenset({"bcgame"})


def test_mapping_guard_flags_a_flipped_reference_against_majority():
    """Robustness: if the reference itself is flipped and the majority disagrees, the lone
    reference is flagged — the correct majority is never marked suspect."""
    from src.run import _detect_mapping_suspects
    book_prices = {
        "pinnacle": {101: 5.0,  103: 1.65},   # flipped reference, stands alone
        "1xbet":    {101: 1.66, 103: 4.9},
        "stake":    {101: 1.64, 103: 5.1},
        "cloudbet": {101: 1.65, 103: 5.0},
    }
    suspects, ref, _ = _detect_mapping_suspects(book_prices, ["pinnacle"], 1.5)
    assert ref == "pinnacle"
    assert suspects == frozenset({"pinnacle"})


def test_scan_drops_flipped_book_phantom_arb():
    """End-to-end: a home/away-swapped book would mint a phantom arb; the guard drops it."""
    payload = [{
        "fixtureId": "fxch", "participant1Id": 35, "participant2Id": 34, "tournamentId": 17,
        "statusId": 0, "hasOdds": True, "startTime": KICKOFF, "updatedAt": RECENT,
        "bookmakerOdds": {
            "pinnacle": _book_1x2(1.65, 3.8, 5.0),     # correct
            "bcgame":   _book_1x2(5.0, 3.8, 1.65),     # home/away swapped upstream
        },
    }]
    feeds = parse_odds_payload(payload)
    specs, _ = build_market_specs(MARKETS_1X2, 10, ["double chance"])
    group_of = build_clone_group_fn(BOOKS_JSON)
    names = {"35": "Switzerland", "34": "Australia"}

    # Guard OFF: the swapped book's 'home @ 5.0' pairs with the real 'away @ 5.0' -> phantom arb.
    ctx_off = _ctx(group_of, ["pinnacle", "bcgame"], ["pinnacle", "bcgame"])
    _, stats_off = _scan(feeds, specs, ctx_off, _cfg(), {}, names, NOW, get_logger("test"))
    assert stats_off["real_arbs"] + stats_off["shadow_arbs"] >= 1

    # Guard ON (Pinnacle reference): bcgame flagged + dropped -> no phantom arb survives.
    ctx_on = _ctx(group_of, ["pinnacle", "bcgame"], ["pinnacle", "bcgame"], reference_books=["pinnacle"])
    _, stats_on = _scan(feeds, specs, ctx_on, _cfg(), {}, names, NOW, get_logger("test"))
    assert stats_on["real_arbs"] == 0 and stats_on["shadow_arbs"] == 0
    assert stats_on["mapping_suspect_flags"]["bcgame"] == 1


def test_dump_book_outcomes_runs_without_error():
    from src.run import _dump_book_outcomes
    specs, _ = build_market_specs(MARKETS_1X2, 10, [])
    _dump_book_outcomes(get_logger("t"), "Switzerland vs Australia", specs[101],
                        "Switzerland", "Australia", "bcgame", {101: 5.0, 102: 3.8, 103: 1.65})
