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


def test_merge_persists_venue_execution_ids():
    """ADDITIVE: each kalshi leg carries venue/venueId(ticker)/venueSide for the executor, and the
    existing price/limit/changedAt fields are unchanged."""
    raw: dict = {}
    kalshi.merge_into(raw, BY_FIXTURE, INDEX, _usa_par_markets("normal"), now=NOW, log=LOG)
    outs = raw["idBBB"]["bookmakerOdds"]["kalshi"]["markets"]["101"]["outcomes"]
    home, draw, away = (outs["101"]["players"]["0"], outs["102"]["players"]["0"],
                        outs["103"]["players"]["0"])
    assert home["venue"] == "kalshi" and home["venueSide"] == "YES"
    assert home["venueId"] == "KXWCGAME-26JUN13USAPAR-USA"   # the EXACT market ticker priced
    assert draw["venueId"] == "KXWCGAME-26JUN13USAPAR-TIE"
    assert away["venueId"] == "KXWCGAME-26JUN13USAPAR-PAR"
    # existing fields untouched (no regression to the manual pipeline)
    assert home["price"] == 3.125 and home["limit"] == 160.0 and home["mainLine"] is True
    assert home["changedAt"] == "2026-06-13T10:00:00Z"


def test_merge_totals_btts_venue_side_yes_over_no_under():
    """Over/BTTS-Yes back YES (yes_ask); Under/BTTS-No back NO (no_ask) — venueSide reflects that,
    on the same per-market ticker."""
    raw: dict = {}
    markets = _usa_par_markets("normal") + [
        _total_mkt("2.5", "0.5000", "400", "0.5200", "300"),
        _btts_mkt("0.4000", "250", "0.6500", "100"),
    ]
    kalshi.merge_into(raw, BY_FIXTURE, INDEX_FULL, markets, now=NOW, log=LOG)
    mkts = raw["idBBB"]["bookmakerOdds"]["kalshi"]["markets"]
    over, under = mkts["1010"]["outcomes"]["1010"]["players"]["0"], mkts["1010"]["outcomes"]["1011"]["players"]["0"]
    assert over["venueSide"] == "YES" and under["venueSide"] == "NO"
    assert over["venueId"] == "KXWCTOTAL-26JUN13USAPAR-2.5" == under["venueId"]
    yes, no = mkts["104"]["outcomes"]["104"]["players"]["0"], mkts["104"]["outcomes"]["105"]["players"]["0"]
    assert yes["venueSide"] == "YES" and no["venueSide"] == "NO"
    assert yes["venueId"] == "KXWCBTTS-26JUN13USAPAR-BTTS"


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


def test_tie_leg_detected_by_word_boundary_draw_or_tie():
    """The tie leg is classified by CONTAINING 'tie'/'draw' as a WORD (not equality), so 'Draw' and
    'Tie (Regulation)' both yield 1 tie + 2 team legs — the semis bug had tie=False, teams=3."""
    cases = [
        ("Draw", "France", "Spain", "KXWCGAME-26JUL08FRASPA", "2026-07-08T19:00:00.000Z"),
        ("Tie (Regulation)", "England", "Argentina", "KXWCGAME-26JUL09ENGARG", "2026-07-09T19:00:00.000Z"),
    ]
    for tie_sub, away, home, ev, start in cases:
        markets = [_mkt(away, "0.3200", "500", event=ev),
                   _mkt(tie_sub, "0.4000", "100", event=ev),
                   _mkt(home, "0.2500", "200", event=ev)]
        by_fixture = {"fx": {"p1": away, "p2": home, "start_time": start, "status_id": 0,
                             "tournament": "World Cup"}}
        raw: dict = {}
        cov, _ = kalshi.merge_into(raw, by_fixture, INDEX, markets, now=NOW, log=LOG)
        assert cov.matched == 1, (tie_sub, cov.incomplete)     # 1 tie + 2 teams (tie NOT filed as a team)
        outs = raw["fx"]["bookmakerOdds"]["kalshi"]["markets"]["101"]["outcomes"]
        assert outs["102"]["players"]["0"]["price"] == 2.5     # the tie/draw leg -> X (0.40 -> 2.5)
        assert set(outs.keys()) == {"101", "102", "103"}       # exactly home / draw / away


def test_reg_time_prefix_stripped_from_team_labels():
    """Kalshi drifted the semis' subtitles to 'Reg Time: <Team>' / 'Reg Time: Tie'. The regulation
    prefix must be stripped so the TEAM labels still normalize to the fixture (else 0/2 unmatched)."""
    ev = "KXWCGAME-26JUL14FRAESP"
    markets = [_mkt("Reg Time: France", "0.3800", "500", event=ev),
               _mkt("Reg Time: Tie", "0.3200", "100", event=ev),
               _mkt("Reg Time: Spain", "0.3000", "200", event=ev)]
    by_fixture = {"fx": {"p1": "France", "p2": "Spain", "start_time": "2026-07-14T19:00:00.000Z",
                         "status_id": 0, "tournament": "World Cup"}}
    raw: dict = {}
    cov, _ = kalshi.merge_into(raw, by_fixture, INDEX, markets, now=NOW, log=LOG)
    assert cov.matched == 1, cov.incomplete                     # 1 tie + 2 teams, matched despite prefix
    outs = raw["fx"]["bookmakerOdds"]["kalshi"]["markets"]["101"]["outcomes"]
    assert set(outs.keys()) == {"101", "102", "103"}           # France(home) / Tie(draw) / Spain(away)


def test_incomplete_event_warning_prints_raw_subtitles():
    """When an event can't form 1 tie + 2 teams, the warning echoes the raw yes_sub_title list so the
    next label drift is self-diagnosing."""
    msgs: list = []

    class _L:
        def warning(self, msg, *a):
            msgs.append(msg % a if a else msg)
        def info(self, *a, **k):
            pass
        def debug(self, *a, **k):
            pass

    ev = "KXWCGAME-26JUL08FRASPA"
    markets = [_mkt("France", "0.3200", "500", event=ev), _mkt("Spain", "0.2500", "200", event=ev)]
    by_fixture = {"fx": {"p1": "France", "p2": "Spain", "start_time": "2026-07-08T19:00:00.000Z",
                         "status_id": 0}}
    kalshi.merge_into({}, by_fixture, INDEX, markets, now=NOW, log=_L())
    assert any("subtitles=" in m and "France" in m and "Spain" in m for m in msgs)


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
# Bug 1: override OddsPapi's SUSPENDED kalshi (markets present but no live price)  #
# --------------------------------------------------------------------------- #
def test_oddspapi_has_active_false_when_outcomes_suspended():
    from src.theoddsapi import _oddspapi_has_active
    # The shape OddsPapi sends for a suspended kalshi: markets present, but every outcome is
    # inactive / unpriced. A non-empty markets dict alone must NOT count as active.
    suspended = {"bookmakerIsActive": True, "suspended": False, "markets": {"101": {"marketActive": True,
        "outcomes": {"101": {"players": {"0": {"active": False, "price": 2.0}}},
                     "102": {"players": {"0": {"active": True, "price": None}}},
                     "103": {"players": {"0": {}}}}}}}
    assert _oddspapi_has_active(suspended) is False
    active = {"bookmakerIsActive": True, "suspended": False, "markets": {"101": {"marketActive": True,
        "outcomes": {"101": {"players": {"0": {"active": True, "price": 2.0}}}}}}}
    assert _oddspapi_has_active(active) is True


def test_merge_overrides_suspended_oddspapi_kalshi():
    """OddsPapi supplied kalshi with markets present but all outcomes suspended -> Kalshi-direct
    OVERRIDES it (recovers), rather than deferring (the live-scan bug)."""
    suspended_entry = {"bookmakerIsActive": True, "suspended": False, "markets": {"101": {"marketActive": True,
        "outcomes": {"101": {"players": {"0": {"active": False, "price": 2.0}}}}}}}
    raw = {"idBBB": {"fixtureId": "idBBB", "startTime": "2026-06-13T19:00:00.000Z",
                     "bookmakerOdds": {"kalshi": suspended_entry}}}
    cov, kbooks = kalshi.merge_into(raw, BY_FIXTURE, INDEX, _usa_par_markets("normal"), now=NOW, log=LOG)
    assert cov.recovered == 1 and cov.deferred == 0
    assert kbooks == {"idBBB": {"kalshi"}}
    _assert_usa_par_mapping(raw)   # suspended stub overwritten with our priced legs


# --------------------------------------------------------------------------- #
# Bug 2: cross-midnight-UTC fixture (US-local ticker date one day behind UTC)    #
# --------------------------------------------------------------------------- #
def test_merge_matches_cross_midnight_utc_fixture():
    """Haiti–Scotland: ticker date 26JUN13 (US-local) but kickoff 2026-06-14T01:00Z (next UTC day).
    Must match on team identity within ±1 day, not drop as time_mismatch."""
    by_fixture = {"idHS": {"p1": "Haiti", "p2": "Scotland", "start_time": "2026-06-14T01:00:00.000Z",
                           "status_id": 0, "tournament": "World Cup"}}
    event = "KXWCGAME-26JUN13HTISCO"
    markets = [_mkt("Haiti", "0.5000", "100", event=event),
               _mkt("Tie", "0.4000", "100", event=event),
               _mkt("Scotland", "0.2500", "100", event=event)]
    raw: dict = {}
    cov, kbooks = kalshi.merge_into(raw, by_fixture, INDEX, markets, now=NOW, log=LOG)
    assert cov.matched == 1 and cov.time_mismatch == []
    assert kbooks == {"idHS": {"kalshi"}}
    outs = raw["idHS"]["bookmakerOdds"]["kalshi"]["markets"]["101"]["outcomes"]
    assert outs["101"]["players"]["0"]["price"] == 2.0      # Haiti 1/0.50 (p1 -> home)
    assert outs["102"]["players"]["0"]["price"] == 2.5      # Tie -> draw
    assert outs["103"]["players"]["0"]["price"] == 4.0      # Scotland 1/0.25 (p2 -> away)


# --------------------------------------------------------------------------- #
# NEW *_dollars SCHEMA: one-sided WC legs (yes side null, priced only on the     #
# deep NO side) + orderbook_fp ladders. Real CIV-NOR shape from the live bug.    #
# --------------------------------------------------------------------------- #
# KXWCGAME-26JUN30CIVNOR: Norway/Tie/Ivory Coast, each quoted ONLY on the NO side.
# yes_ask = 1 - no_bid_dollars -> Norway 0.47, Tie 0.29, Ivory Coast 0.27.
CN_EVENT = "KXWCGAME-26JUN30CIVNOR"
BY_FIXTURE_CN = {
    "idCN": {"p1": "Norway", "p2": "Ivory Coast", "start_time": "2026-06-30T18:00:00.000Z",
             "status_id": 0, "tournament": "World Cup"},
}
NOW_CN = datetime(2026, 6, 30, 10, 0, 0, tzinfo=timezone.utc)


def _mkt_no_side(yes_sub_title, no_bid, no_ask, no_bid_size, *, event=CN_EVENT, status="active"):
    """A market in the live one-sided shape: the YES fields are null; price + a deep ladder live only
    on the NO side (no_*_dollars + orderbook_fp.no_dollars), exactly like KXWCGAME-26JUN30CIVNOR."""
    deeper = f"{float(no_bid) - 0.01:.4f}"
    return {
        "event_ticker": event, "ticker": f"{event}-{yes_sub_title[:3].upper()}",
        "yes_sub_title": yes_sub_title, "status": status,
        "yes_ask_dollars": None, "yes_bid_dollars": None, "yes_ask_size_fp": None,
        "no_ask_dollars": no_ask, "no_bid_dollars": no_bid, "no_bid_size_fp": no_bid_size,
        "last_price_dollars": "0.4700",
        "orderbook_fp": {"no_dollars": [[no_bid, no_bid_size], [deeper, "2000000"]], "yes_dollars": []},
    }


def _civ_nor_markets():
    return [
        _mkt_no_side("Norway", "0.5300", "0.5400", "1000000"),       # yes_ask = 1-0.53 = 0.47
        _mkt_no_side("Tie", "0.7100", "0.7200", "500000"),           # yes_ask = 1-0.71 = 0.29
        _mkt_no_side("Ivory Coast", "0.7300", "0.7400", "300000"),   # yes_ask = 1-0.73 = 0.27
    ]


def test_yes_ask_price_derives_from_no_side_when_yes_is_null():
    """THE live bug: the YES fields are None, so the legacy reader returned None for every leg. The
    reader must DERIVE the yes ask from the deep NO side (yes_ask = 1 - no_bid_dollars)."""
    nor, tie, civ = _civ_nor_markets()
    assert abs(kalshi.yes_ask_price(nor) - 0.47) < 1e-9     # Norway
    assert abs(kalshi.yes_ask_price(tie) - 0.29) < 1e-9     # Tie
    assert abs(kalshi.yes_ask_price(civ) - 0.27) < 1e-9     # Ivory Coast
    # And the decimal is 1 / that effective yes ask (point 4), never None now.
    assert abs(kalshi.decimal_from_dollars(kalshi.yes_ask_price(nor)) - 1 / 0.47) < 1e-9
    # Legacy fallback still works for any market still on the old integer-cent schema.
    assert abs(kalshi.yes_ask_price({"yes_ask": 32}) - 0.32) < 1e-9
    assert abs(kalshi.yes_ask_price({"no_bid": 53}) - 0.47) < 1e-9   # legacy NO-derived


def test_ask_ladder_reads_orderbook_fp_buy_yes_from_no_dollars():
    """Buying YES walks the orderbook_fp.no_dollars ladder, complemented (1 - no_price). Non-empty,
    real depth — not the empty result the legacy orderbook.yes/.no reader gave on the new schema."""
    nor = _civ_nor_markets()[0]
    ladder = kalshi.ask_ladder({"orderbook_fp": nor["orderbook_fp"]}, "YES")
    assert ladder                                            # NON-EMPTY depth (was empty before)
    assert abs(ladder[0][0] - 0.47) < 1e-9 and ladder[0][1] == 1000000.0   # best yes ask = 1-0.53
    assert abs(ladder[1][0] - 0.48) < 1e-9                  # next level = 1-0.52, ascending
    assert sum(p * s for p, s in ladder) > 1_000_000        # real dollar depth
    # LEGACY integer-cent orderbook still parses (back-compat).
    legacy = kalshi.ask_ladder({"orderbook": {"yes": [[40, 100]], "no": [[55, 30], [50, 70]]}}, "YES")
    assert legacy == [(0.45, 30.0), (0.50, 70.0)]           # NO bids 55,50 -> YES asks 0.45,0.50


def test_merge_maps_civ_nor_one_sided_new_schema():
    """End-to-end on the real CIV-NOR shape: every leg prices (none read None) and maps to the
    canonical 1x2 with a real, non-zero limit derived from the NO-bid size."""
    raw: dict = {}
    cov, kbooks = kalshi.merge_into(raw, BY_FIXTURE_CN, INDEX, _civ_nor_markets(), now=NOW_CN, log=LOG)
    assert cov.matched == 1 and cov.recovered == 1 and kbooks == {"idCN": {"kalshi"}}
    outs = raw["idCN"]["bookmakerOdds"]["kalshi"]["markets"]["101"]["outcomes"]
    home, draw, away = (outs["101"]["players"]["0"], outs["102"]["players"]["0"],
                        outs["103"]["players"]["0"])
    assert round(home["price"], 3) == 2.128                 # Norway 1/0.47 (p1 -> home)
    assert round(draw["price"], 3) == 3.448                 # Tie 1/0.29 -> draw
    assert round(away["price"], 3) == 3.704                 # Ivory Coast 1/0.27 (p2 -> away)
    assert abs(home["limit"] - 470000.0) < 0.1             # 1,000,000 NO-bid contracts × 0.47
    assert abs(draw["limit"] - 145000.0) < 0.1 and abs(away["limit"] - 81000.0) < 0.1
    assert all(p["limit"] for p in (home, draw, away))      # non-zero depth, not low_confidence


# --------------------------------------------------------------------------- #
# DISCOVERY: build the event ticker from teams+date, pull by event_ticker        #
# --------------------------------------------------------------------------- #
class _FakeKalshiClient:
    """Stand-in for KalshiClient: canned markets keyed by event_ticker (targeted) and by series
    (sweep fallback), recording every call so the targeted-vs-sweep path can be asserted."""
    def __init__(self, by_event=None, by_series=None):
        self.by_event = by_event or {}
        self.by_series = by_series or {}
        self.event_calls: list = []
        self.series_calls: list = []

    def markets(self, *, series_ticker=None, event_ticker=None, status="open", limit=100, cursor=None):
        if event_ticker is not None:
            self.event_calls.append(event_ticker)
            return {"markets": list(self.by_event.get(event_ticker, []))}
        self.series_calls.append(series_ticker)
        return {"markets": list(self.by_series.get(series_ticker, []))}

    def iter_markets(self, *, series_ticker=None, status="open", limit=100, max_pages=50):
        self.series_calls.append(series_ticker)
        return list(self.by_series.get(series_ticker, []))


def test_event_ticker_suffixes_both_orderings_and_prior_day():
    sufs = kalshi._event_ticker_suffixes(BY_FIXTURE_CN["idCN"])
    assert "26JUN30CIVNOR" in sufs and "26JUN30NORCIV" in sufs   # both team orderings (date 26JUN30)
    assert "26JUN29CIVNOR" in sufs                               # prior US-local day, too
    # A non-coded team yields no addressable ticker.
    assert kalshi._event_ticker_suffixes({"p1": "Atlantis", "p2": "Narnia",
                                          "start_time": "2026-06-30T18:00:00.000Z"}) == []


def test_discover_markets_addresses_event_by_ticker_no_sweep():
    """Discovery builds KXWCGAME-26JUN30CIVNOR from teams+date and pulls it (plus the same game
    suffix for KXWCTOTAL) directly by event_ticker — no series sweep needed."""
    total = _total_mkt("2.5", "0.5000", "400", "0.5200", "300")
    total = dict(total, event_ticker="KXWCTOTAL-26JUN30CIVNOR", ticker="KXWCTOTAL-26JUN30CIVNOR-2.5")
    client = _FakeKalshiClient(by_event={CN_EVENT: _civ_nor_markets(),
                                         "KXWCTOTAL-26JUN30CIVNOR": [total]})
    markets = kalshi.discover_markets(client, BY_FIXTURE_CN, result_series="KXWCGAME",
                                      extra_series=("KXWCTOTAL", "KXWCBTTS"), log=LOG)
    assert CN_EVENT in client.event_calls                       # the right ticker was built + tried
    assert "KXWCTOTAL-26JUN30CIVNOR" in client.event_calls      # sibling series, same game suffix
    assert client.series_calls == []                            # resolved by ticker -> no fallback sweep
    assert len(markets) == 4                                    # 3 result legs + 1 total line

    # And the discovered markets merge through cleanly (one-sided legs price + map).
    raw: dict = {}
    cov, kbooks = kalshi.merge_into(raw, BY_FIXTURE_CN, INDEX_FULL, markets, now=NOW_CN, log=LOG)
    assert cov.recovered == 1 and cov.totals_lines == 1 and kbooks == {"idCN": {"kalshi"}}


def test_discover_markets_sweeps_when_fixture_not_addressable():
    """A non-coded fixture can't be addressed by ticker -> discovery falls back to the series sweep
    (so nothing regresses vs the old sweep-only path)."""
    fixtures = {"idX": {"p1": "Atlantis", "p2": "Narnia", "start_time": "2026-06-30T18:00:00.000Z"}}
    client = _FakeKalshiClient(by_series={"KXWCGAME": _civ_nor_markets()})
    markets = kalshi.discover_markets(client, fixtures, result_series="KXWCGAME", extra_series=(), log=LOG)
    assert client.event_calls == []                             # nothing addressable
    assert client.series_calls == ["KXWCGAME"]                  # swept as fallback
    assert len(markets) == 3


# --------------------------------------------------------------------------- #
# SAFE TIER: total goals O/U (KXWCTOTAL) + BTTS (KXWCBTTS) injection             #
# --------------------------------------------------------------------------- #
# Canonical index extended with totals 2.5 (1010/1011) and BTTS (104/105).
MARKETS_FULL = MARKETS_JSON + [
    {"marketId": 1010, "sportId": 10, "marketType": "totals", "period": "fulltime", "handicap": 2.5,
     "marketName": "Over Under Full Time",
     "outcomes": [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]},
    {"marketId": 104, "sportId": 10, "marketType": "bothteamsscore", "period": "fulltime", "handicap": 0,
     "marketName": "Both Teams To Score",
     "outcomes": [{"outcomeId": 104, "outcomeName": "Yes"}, {"outcomeId": 105, "outcomeName": "No"}]},
]
INDEX_FULL = kalshi.build_market_index(MARKETS_FULL, 10)


def _total_mkt(line, yes_ask, yes_size, no_ask, no_size, *, status="active"):
    ev = "KXWCTOTAL-26JUN13USAPAR"
    return {"event_ticker": ev, "ticker": f"{ev}-{line}", "status": status,
            "yes_sub_title": f"Over {line} goals scored", "title": f"Will over {line} goals be scored?",
            "yes_ask_dollars": yes_ask, "yes_ask_size_fp": yes_size,
            "no_ask_dollars": no_ask, "no_ask_size_fp": no_size}


def _btts_mkt(yes_ask, yes_size, no_ask, no_size, *, status="active"):
    ev = "KXWCBTTS-26JUN13USAPAR"
    return {"event_ticker": ev, "ticker": f"{ev}-BTTS", "status": status,
            "yes_sub_title": "Both Teams To Score", "title": "Will both teams score?",
            "yes_ask_dollars": yes_ask, "yes_ask_size_fp": yes_size,
            "no_ask_dollars": no_ask, "no_ask_size_fp": no_size}


def test_build_market_index_indexes_btts():
    assert INDEX_FULL.btts == {"marketId": 104, "yes_oid": 104, "no_oid": 105,
                               "scope": kalshi.SCOPE_PER_GAME}
    assert 2.5 in INDEX_FULL.totals


def test_merge_injects_totals_and_btts_on_the_matched_fixture():
    raw: dict = {}
    markets = _usa_par_markets("normal") + [
        _total_mkt("2.5", "0.5000", "400", "0.5200", "300"),   # Over 1/.50=2.0, Under 1/.52
        _total_mkt("9.5", "0.0100", "100", "0.9900", "50"),    # line not in index -> skipped
        _btts_mkt("0.4000", "250", "0.6500", None),            # No has no size -> UNVERIFIED limit 0
    ]
    cov, kbooks = kalshi.merge_into(raw, BY_FIXTURE, INDEX_FULL, markets, now=NOW, log=LOG)
    assert cov.recovered == 1 and kbooks == {"idBBB": {"kalshi"}}
    assert cov.totals_fixtures == 1 and cov.totals_lines == 1   # only the indexed 2.5 line
    assert cov.btts_fixtures == 1

    mkts = raw["idBBB"]["bookmakerOdds"]["kalshi"]["markets"]
    assert "101" in mkts                                        # 1x2 still injected
    t = mkts["1010"]["outcomes"]
    assert t["1010"]["players"]["0"]["price"] == 2.0           # Over from yes_ask
    assert abs(t["1011"]["players"]["0"]["price"] - 1 / 0.52) < 1e-9   # Under from no_ask
    assert t["1010"]["players"]["0"]["limit"] == 400 * 0.50    # real size×price = 200
    b = mkts["104"]["outcomes"]
    assert b["104"]["players"]["0"]["price"] == 2.5            # BTTS Yes from yes_ask
    assert abs(b["105"]["players"]["0"]["price"] - 1 / 0.65) < 1e-9    # BTTS No from no_ask
    assert b["105"]["players"]["0"]["limit"] == 0.0           # no_ask_size_fp None -> UNVERIFIED


def test_totals_skipped_when_result_unmatched():
    """Totals/BTTS ride on the 1x2 fixture match; with no matching fixture, nothing is injected."""
    by_fixture = {"idAAA": {"p1": "Australia", "p2": "Turkiye",
                            "start_time": "2026-06-13T19:00:00.000Z", "status_id": 0}}
    raw: dict = {}
    markets = _usa_par_markets("normal") + [_total_mkt("2.5", "0.5000", "400", "0.5200", "300")]
    cov, kbooks = kalshi.merge_into(raw, by_fixture, INDEX_FULL, markets, now=NOW, log=LOG)
    assert cov.recovered == 0 and cov.totals_lines == 0 and raw == {}


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
