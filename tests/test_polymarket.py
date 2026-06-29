"""Tests for the Polymarket-direct supplemental source: JSON-string-array parsing, the Yes-token
pick, groupItemTitle/question -> home/draw/away role parsing, CLOB best-ask pricing + size->limit,
the full discover->price->map pipeline (incl. cross-sport disambiguation by team-set + date), the
±1-day cross-midnight match, the OddsPapi-active override, and the shadow-only rollout gate in
run._scan. Mirrors tests/test_kalshi.py."""
from __future__ import annotations

from datetime import datetime, timezone

from src import polymarket
from src.catalog import build_clone_group_fn, build_market_specs
from src.config import Config, Secrets
from src.logsetup import get_logger
from src.normalize import parse_odds_payload
from src.run import EngineCtx, _scan

LOG = get_logger("test-polymarket")

# Canonical Full Time Result (1x2): oid 101='1'/home, 102='X'/draw, 103='2'/away (same as test_kalshi).
MARKETS_JSON = [
    {"marketId": 101, "sportId": 10, "marketType": "1x2", "period": "fulltime", "handicap": 0,
     "marketName": "Full Time Result",
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 102, "outcomeName": "X"},
                  {"outcomeId": 103, "outcomeName": "2"}]},
]
INDEX = polymarket.build_market_index(MARKETS_JSON, 10)

# Real in-window example: our fixture spells it "Ivory Coast"; Polymarket sends "Côte d'Ivoire".
BY_FIXTURE = {
    "idCIV": {"p1": "Ivory Coast", "p2": "Ecuador", "start_time": "2026-06-14T23:00:00.000Z",
              "status_id": 0, "tournament": "World Cup"},
}
NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# JSON-string-array parsing + Yes-token pick                                    #
# --------------------------------------------------------------------------- #
def test_as_list_parses_json_string_arrays():
    assert polymarket._as_list('["Yes", "No"]') == ["Yes", "No"]
    assert polymarket._as_list(["a", "b"]) == ["a", "b"]      # already a list
    assert polymarket._as_list('not json') == []
    assert polymarket._as_list(None) == []
    assert polymarket._as_list('{"k": 1}') == []              # JSON but not a list


def test_yes_token_pairs_yes_outcome_with_its_clob_token():
    m = {"outcomes": '["Yes", "No"]', "clobTokenIds": '["tok_yes", "tok_no"]'}
    assert polymarket.yes_token(m) == "tok_yes"
    # "No"-first ordering still pairs by index identity, never position.
    m2 = {"outcomes": '["No", "Yes"]', "clobTokenIds": '["tok_no", "tok_yes"]'}
    assert polymarket.yes_token(m2) == "tok_yes"
    # Not deployed to the CLOB yet -> no token.
    assert polymarket.yes_token({"outcomes": '["Yes", "No"]', "clobTokenIds": '["", ""]'}) is None


# --------------------------------------------------------------------------- #
# Role parsing — groupItemTitle (detail payload) AND question (search payload)   #
# --------------------------------------------------------------------------- #
def test_leg_role_from_group_item_title():
    assert polymarket.leg_role({"groupItemTitle": "Côte d'Ivoire"}) == ("team", "Côte d'Ivoire")
    assert polymarket.leg_role({"groupItemTitle": "Ecuador"}) == ("team", "Ecuador")
    # The draw market's groupItemTitle carries the "(A vs. B)" suffix.
    assert polymarket.leg_role({"groupItemTitle": "Draw (Côte d'Ivoire vs. Ecuador)"}) == ("draw", None)
    # A team groupItemTitle with a parenthetical suffix is stripped to the bare team name.
    assert polymarket.leg_role({"groupItemTitle": "Ecuador (Côte d'Ivoire vs. Ecuador)"}) == ("team", "Ecuador")


def test_leg_role_from_question_when_no_group_item_title():
    """Gamma's /public-search omits groupItemTitle; fall back to the question (same role + the date)."""
    assert polymarket.leg_role({"question": "Will Côte d'Ivoire win on 2026-06-14?"}) == ("team", "Côte d'Ivoire")
    assert polymarket.leg_role({"question": "Will Ecuador win on 2026-06-14?"}) == ("team", "Ecuador")
    assert polymarket.leg_role({"question": "Will Côte d'Ivoire vs. Ecuador end in a draw?"}) == ("draw", None)
    assert polymarket.leg_role({"question": "Totally unrelated market?"}) is None


# --------------------------------------------------------------------------- #
# CLOB best-ask pricing + size->limit (phantom-arb guard on units)              #
# --------------------------------------------------------------------------- #
def test_decimal_from_ask_units():
    assert polymarket.decimal_from_ask("0.32") == 3.125     # dollars in (0,1) -> 1/0.32, NOT /100
    assert polymarket.decimal_from_ask(0.5) == 2.0
    assert polymarket.decimal_from_ask("0") is None         # no two-sided ask
    assert polymarket.decimal_from_ask("1.0") is None
    assert polymarket.decimal_from_ask(None) is None


def test_leg_limit_is_size_times_price():
    assert polymarket.leg_limit("1000", "0.275") == 275.0   # shares × price = dollars
    assert polymarket.leg_limit(0, "0.32") == 0.0
    assert polymarket.leg_limit("bad", "0.32") == 0.0


def test_best_ask_picks_lowest_priced_level_with_its_size():
    book = {"asks": [{"price": "0.47", "size": "80"}, {"price": "0.46", "size": "200"}],
            "bids": [{"price": "0.44", "size": "100"}]}
    assert polymarket.best_ask(book) == (0.46, 200.0)       # lowest ask price = the buy price
    assert polymarket.best_ask({"asks": []}) is None
    # Degenerate $1.00 / out-of-range levels are ignored.
    assert polymarket.best_ask({"asks": [{"price": "1.0", "size": "5"}]}) is None


# --------------------------------------------------------------------------- #
# Gamma event dicts (search-payload shape: question, JSON-string arrays, no GIT) #
# --------------------------------------------------------------------------- #
def _gamma_market(question, yes_tok, no_tok, *, git=None, date="2026-06-14"):
    m = {"outcomes": '["Yes", "No"]', "clobTokenIds": f'["{yes_tok}", "{no_tok}"]',
         "question": question, "gameStartTime": f"{date} 23:00:00+00",
         "startDate": "2026-04-06T22:32:40Z"}     # startDate is a CREATION artifact — must be ignored
    if git is not None:
        m["groupItemTitle"] = git
    return m


def _civ_ecuador_event(*, eid="351725", date="2026-06-14", with_git=False):
    mk = lambda q, y, n, g: _gamma_market(q, y, n, git=(g if with_git else None), date=date)
    return {
        "id": eid, "title": "Côte d'Ivoire vs. Ecuador", "closed": False,
        "eventDate": date, "startTime": f"{date}T23:00:00Z",
        "startDate": "2026-04-06T22:34:39Z",      # creation artifact (must NOT be used as kickoff)
        "markets": [
            mk("Will Côte d'Ivoire win on 2026-06-14?", "civ_yes", "civ_no", "Côte d'Ivoire"),
            mk("Will Ecuador win on 2026-06-14?", "ecu_yes", "ecu_no", "Ecuador"),
            mk("Will Côte d'Ivoire vs. Ecuador end in a draw?", "draw_yes", "draw_no",
               "Draw (Côte d'Ivoire vs. Ecuador)"),
        ],
    }


def test_parse_event_legs_shape_and_date():
    ev = polymarket.parse_event_legs(_civ_ecuador_event())
    assert ev is not None
    assert ev.commence_iso == "2026-06-14T12:00:00Z"        # from eventDate, noon-anchored (not startDate)
    roles = sorted((leg.role, leg.team, leg.yes_token) for leg in ev.legs)
    assert roles == [("draw", "Draw", "draw_yes"),
                     ("team", "Côte d'Ivoire", "civ_yes"),
                     ("team", "Ecuador", "ecu_yes")]


def test_parse_event_legs_rejects_non_match_shape():
    # Only 2 markets (no draw) -> not the 1 draw + 2 team shape -> None (drops cross-sport noise).
    ev = {"id": "x", "title": "Two-way", "markets": [
        _gamma_market("Will A win on 2026-06-14?", "a", "an"),
        _gamma_market("Will B win on 2026-06-14?", "b", "bn")]}
    assert polymarket.parse_event_legs(ev) is None


# --------------------------------------------------------------------------- #
# Pricing one leg from a fake CLOB client                                       #
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Stand-in for PolymarketClient: canned search() + events_by_slug() + book() responses, no
    network. ``events_by_slug`` maps an exact slug -> the list of events Gamma's /events?slug= returns
    (a 404/miss is an empty list); ``slug_errors`` is a set of slugs that raise PolymarketError."""
    def __init__(self, events_by_term=None, books_by_token=None, events_by_slug=None, slug_errors=None):
        self.events_by_term = events_by_term or {}
        self.books_by_token = books_by_token or {}
        self.slugs = events_by_slug or {}
        self.slug_errors = set(slug_errors or ())

    def search(self, q):
        return {"events": self.events_by_term.get(q, [])}

    def events_by_slug(self, slug):
        if slug in self.slug_errors:
            raise polymarket.PolymarketError(f"404 no event for {slug}")
        return self.slugs.get(slug, [])

    def book(self, token_id):
        b = self.books_by_token.get(token_id)
        if b is None:
            raise polymarket.PolymarketError(f"404 no book for {token_id}")
        return b

    def price_tokens(self, tokens, max_workers=8, *, depth_pricing=False,
                     slippage_pct=polymarket.DEFAULT_DEPTH_SLIPPAGE_PCT,
                     reference_size=polymarket.DEFAULT_WALK_STAKE):
        """Mirror PolymarketClient.price_tokens over the canned books (sequential; no network)."""
        out = {}
        for t in dict.fromkeys(tokens):
            if not t:
                continue
            out[t] = polymarket.price_leg(self, t, depth_pricing=depth_pricing, slippage_pct=slippage_pct,
                                          reference_size=reference_size)
        return out


def _book(ask_price, ask_size):
    return {"asks": [{"price": str(ask_price), "size": str(ask_size)}],
            "bids": [{"price": "0.01", "size": "10"}]}


def test_price_tokens_concurrent_dedups_and_prices(monkeypatch):
    """The concurrent CLOB pricer returns (decimal, limit) per token, de-duplicates, and drops empties
    and tokens with no live ask — without the per-request throttle."""
    import src.polymarket as pm
    canned = {"a": _book(0.50, 100), "b": _book(0.25, 200), "empty": {"asks": []}}

    class _Resp:
        status_code = 200

        def __init__(self, j):
            self._j = j

        def json(self):
            return self._j

    class _Sess:
        def __init__(self):
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            return _Resp(canned.get((params or {}).get("token_id"), {"asks": []}))

    monkeypatch.setattr(pm.requests, "Session", lambda: _Sess())
    res = pm.PolymarketClient().price_tokens(["a", "b", "a", "", "empty", "missing"], max_workers=4)
    assert set(res) == {"a", "b", "empty", "missing"}        # de-duped 'a'; '' dropped
    assert res["a"] == (2.0, 50.0) and res["b"] == (4.0, 50.0)
    assert res["empty"] is None and res["missing"] is None


# civ 0.275->3.636 ($275), ecu 0.395->2.532 ($395), draw 0.335->2.985 ($335).
_CIV_BOOKS = {"civ_yes": _book(0.275, 1000), "ecu_yes": _book(0.395, 1000), "draw_yes": _book(0.335, 1000)}


def test_price_leg_from_clob_best_ask():
    client = _FakeClient(books_by_token=_CIV_BOOKS)
    dec, lim = polymarket.price_leg(client, "civ_yes")
    assert round(dec, 3) == 3.636 and lim == 275.0
    assert polymarket.price_leg(client, "missing_token") is None   # 4xx / no book -> drop-safe None


def test_parse_event_legs_labels_draw_and_never_emits_none_team():
    """The draw leg gets the canonical "Draw" label (was None) so it aligns with the other books' tie
    leg and never crashes None formatting; the draw's pricing/decimal/limit stay untouched."""
    ev = polymarket.parse_event_legs(_civ_ecuador_event())
    assert ev is not None
    by_role = {leg.role: leg for leg in ev.legs}
    assert {leg.team for leg in ev.legs} == {"Côte d'Ivoire", "Draw", "Ecuador"}
    assert all(leg.team is not None for leg in ev.legs)            # guard: no leg ever carries None
    assert by_role["draw"].team == polymarket.DRAW_LABEL == "Draw"
    # Pricing is unchanged by the label fix: the draw token still prices to the same decimal/limit.
    dec, lim = polymarket.price_leg(_FakeClient(books_by_token=_CIV_BOOKS), by_role["draw"].yes_token)
    assert round(dec, 3) == 2.985 and lim == 335.0


def test_leg_never_carries_none_team():
    """Guard: a Leg can never be emitted with team=None — draw falls back to the canonical "Draw"
    label, any other role to the outcome string (its role); an explicit label is preserved."""
    assert polymarket.Leg(role="draw").team == "Draw"
    assert polymarket.Leg(role="over").team == "over"             # extra leg -> outcome string, not None
    assert polymarket.Leg(role="team", team="Ecuador").team == "Ecuador"


# --------------------------------------------------------------------------- #
# SAFETY: we BUY all three Yes legs, so each leg MUST price at the best ASK     #
# (what you pay to buy), never the best bid. Pricing the bid manufactures false #
# arbs: bids can sum < 1 (phantom) while the real ask buy-cost sums >= 1 (loss).#
# Polymarket's /price?side=buy returns the BID (inverted), so we read /book.    #
# --------------------------------------------------------------------------- #
def _bidask_book(bid, ask, *, ask_size=1000.0, bid_size=2000.0):
    """A CLOB book with the best bid one tick below the best ask. The best ask is deliberately NOT
    the first asks element, so best_ask() must compute the minimum, not trust array order."""
    return {"bids": [{"price": str(bid), "size": str(bid_size)},
                     {"price": str(round(bid - 0.01, 2)), "size": "500"}],
            "asks": [{"price": str(round(ask + 0.01, 2)), "size": "500"},
                     {"price": str(ask), "size": str(ask_size)}]}


def test_best_ask_uses_ask_not_bid_even_when_bid_is_just_below():
    # France Yes: bid 0.66 / ask 0.67. The buy price (and polymarket.com) is the 0.67 ASK.
    assert polymarket.best_ask(_bidask_book(0.66, 0.67, ask_size=1381930.45)) == (0.67, 1381930.45)


def test_price_leg_emits_ask_decimal_and_ask_size_limit():
    client = _FakeClient(books_by_token={"t": _bidask_book(0.66, 0.67, ask_size=1000.0)})
    dec, lim = polymarket.price_leg(client, "t")
    assert round(dec, 3) == 1.493     # 1/0.67 (ASK), NOT 1/0.66 = 1.515 (the bid -> phantom arb)
    assert lim == 670.0               # ask_size 1000 × ask 0.67 (ask-based), NOT bid-based


def test_no_phantom_arb_when_asks_sum_above_one():
    """France vs Senegal (live-observed): the three best BIDS sum to 0.99 (a phantom 1% 'arb'), but
    the three best ASKS — the real buy cost — sum to 1.02 (a 2% loss). The source prices the ASK, so
    S >= 1 and NO arb is minted."""
    books = {"fra": _bidask_book(0.66, 0.67), "drw": _bidask_book(0.21, 0.22), "sen": _bidask_book(0.12, 0.13)}
    client = _FakeClient(books_by_token=books)
    decs = [polymarket.price_leg(client, t)[0] for t in ("fra", "drw", "sen")]
    assert [round(d, 3) for d in decs] == [1.493, 4.545, 7.692]   # exact ASK-based decimals
    s_ask = sum(1.0 / d for d in decs)                            # == best-ask sum 0.67+0.22+0.13
    assert round(s_ask, 2) == 1.02 and s_ask > 1.0               # NO arb (S>=1)
    s_bid = 0.66 + 0.21 + 0.12                                   # the phantom we DON'T mint (pricing bids)
    assert round(s_bid, 2) == 0.99 and s_bid < 1.0


# --------------------------------------------------------------------------- #
# DEPTH-AWARE Poly leg pricing — quote the VWAP over the depth band, not the    #
# single best-ask tick, so thin/flickering legs stop minting phantom REAL arbs. #
# --------------------------------------------------------------------------- #
def _multi_book(asks):
    """A CLOB book from a list of (price, size) ask levels (a token bid one tick under the best ask)."""
    best = min(p for p, _ in asks)
    return {"asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "bids": [{"price": str(round(best - 0.01, 4)), "size": "10"}]}


def test_depth_pricing_thin_leg_worse_odds_kills_phantom_arb():
    """(a) A thin, fast-decaying draw book (tiny top order, most depth a band-width worse) prices the
    decimal off the WALK-TO-STAKE avg fill, not the single best-ask tick. That worse, real number fed
    into the SAME 1x2 arb flips arb_sum_S from < 1 (a phantom 'REAL' arb) to >= 1 (no arb) — it stops
    firing. The limit stays the fillable dollar depth within the slippage band."""
    from src.arbitrage import Candidate, compute_arb
    from src import bookmath as bm
    # Best ask 0.119 (8.40), tiny 500 there, then a wall at 0.1249, then deeper junk.
    levels = [(0.119, 500), (0.1249, 40000), (0.140, 100000)]
    thin = _multi_book(levels)
    client = _FakeClient(books_by_token={"draw": thin})

    top_dec, top_lim = polymarket.price_leg(client, "draw")                       # legacy top tick
    dep_dec, dep_lim = polymarket.price_leg(client, "draw", depth_pricing=True, slippage_pct=5.0)
    walk_dec = 1.0 / bm.walk_book(levels, polymarket.DEFAULT_WALK_STAKE).avg_price
    assert round(top_dec, 2) == 8.40                                              # 1/0.119 (top tick)
    assert dep_dec < top_dec and dep_dec == walk_dec                              # walk-to-stake avg fill
    assert round(dep_dec, 2) == 8.03                                              # worse than the 8.40 tick
    assert dep_lim > top_lim                                                      # real band depth, $ filled

    def arb(draw_dec):
        cands = [
            Candidate(101, "1", "polymarket", "cg1", 1.20, limit=50000.0),        # deep favorite
            Candidate(102, "X", "polymarket", "cg2", draw_dec, limit=dep_lim),    # the thin draw
            Candidate(103, "2", "1xbet", "cg3", 23.0, limit=None),               # longshot, unverified
        ]
        return compute_arb(cands)

    assert arb(top_dec).is_arb                                                    # phantom arb at the top tick
    assert not arb(dep_dec).is_arb                                                # depth-aware: S>=1, gone
    assert arb(dep_dec).roi_pct < arb(top_dec).roi_pct                            # edge strictly shrinks


def test_depth_pricing_deep_favorite_essentially_unchanged():
    """(b) A deep favorite book (a wall of size at ~the best ask) keeps ~the same odds (within a tick)
    and a large fillable limit under depth-aware pricing."""
    deep = _multi_book([(0.827, 50000), (0.828, 100000), (0.829, 200000)])
    client = _FakeClient(books_by_token={"fav": deep})
    top_dec, _ = polymarket.price_leg(client, "fav")
    dep_dec, dep_lim = polymarket.price_leg(client, "fav", depth_pricing=True, slippage_pct=2.0)
    assert abs(dep_dec - top_dec) < 0.01                                          # within a tick
    assert dep_lim > 100_000                                                      # large real depth


def test_depth_pricing_false_restores_exact_top_of_book():
    """(d) poly_depth_pricing OFF (the default) reproduces the EXACT legacy top-of-book quote on a
    multi-level book: decimal = 1/best_ask, limit = best_ask_size × best_ask (deeper levels ignored)."""
    book = _multi_book([(0.20, 1000), (0.21, 9_000_000), (0.25, 9_000_000)])
    client = _FakeClient(books_by_token={"t": book})
    assert polymarket.price_leg(client, "t") == (1.0 / 0.20, 0.20 * 1000)         # default = legacy
    assert polymarket.price_leg(client, "t", depth_pricing=False) == (1.0 / 0.20, 0.20 * 1000)


def test_depth_pricing_quotes_walk_to_stake_avg_fill_not_best_ask():
    """(e) THE Germany-Paraguay fix: a thin draw book whose best-ask tick is tiny but whose remaining
    depth is worse. The depth-aware decimal must equal the size-weighted WALK-TO-STAKE avg fill
    (bookmath.walk_book to the reference stake) — the worse, real number Polymarket fills at — NOT the
    optimistic 1/best_ask. A deep favorite (a wall at the best ask) stays ~unchanged."""
    from src import bookmath as bm
    # Thin draw: only 800 shares at the 0.19 best ask (5.2632), the rest a band-width worse.
    thin_levels = [(0.19, 800), (0.196, 60000), (0.21, 200000)]
    client = _FakeClient(books_by_token={"draw": thin_levels and _multi_book(thin_levels)})

    dec, lim = polymarket.price_leg(client, "draw", depth_pricing=True, slippage_pct=5.0)
    best_ask_dec = 1.0 / 0.19                                                     # the optimistic top tick
    walk = bm.walk_book(thin_levels, polymarket.DEFAULT_WALK_STAKE)               # walk to the $10k stake
    assert walk.avg_price > 0.19                                                  # the walk really blends worse
    assert dec == 1.0 / walk.avg_price                                            # quote = avg fill...
    assert dec < best_ask_dec - 0.1                                               # ...the WORSE, real number
    assert round(best_ask_dec, 4) == 5.2632                                       # the tick the bot used to quote
    # limit stays the fillable dollar depth within the 5% slippage band (not the best-ask tick alone).
    assert lim > 0.19 * 800

    # A deep favorite: a wall of size right at the best ask -> walk fills ~entirely there -> ~unchanged.
    deep = _multi_book([(0.827, 80000), (0.828, 200000), (0.829, 400000)])
    fav_client = _FakeClient(books_by_token={"fav": deep})
    fav_dec, _ = polymarket.price_leg(fav_client, "fav", depth_pricing=True, slippage_pct=2.0)
    assert abs(fav_dec - 1.0 / 0.827) < 0.01                                      # within a tick of best ask


# --------------------------------------------------------------------------- #
# Full pipeline: discover -> price -> map, with cross-sport disambiguation      #
# --------------------------------------------------------------------------- #
def _cricket_noise_event():
    """An "Ivory Coast" search also returns a T20-cricket WC event — well-formed shape, but its
    team-set never matches our soccer fixture, so it must drop at the fixture-match step."""
    mk = lambda q, y, n: _gamma_market(q, y, n)
    return {"id": "534875", "title": "Sierra Leone vs Ivory Coast", "closed": False,
            "eventDate": "2026-06-14",
            "markets": [mk("Will Sierra Leone win on 2026-06-14?", "sl_yes", "sl_no"),
                        mk("Will Ivory Coast win on 2026-06-14?", "civc_yes", "civc_no"),
                        mk("Will Sierra Leone vs Ivory Coast end in a draw?", "crd_yes", "crd_no")]}


def test_fetch_then_merge_maps_soccer_and_drops_cross_sport_noise():
    books = dict(_CIV_BOOKS, sl_yes=_book(0.5, 100), civc_yes=_book(0.4, 100), crd_yes=_book(0.3, 100))
    # _search_terms yields ["FIFA World Cup", "Ivory Coast", "Ecuador"] for BY_FIXTURE.
    client = _FakeClient(
        events_by_term={"Ivory Coast": [_cricket_noise_event()], "Ecuador": [_civ_ecuador_event()]},
        books_by_token=books)

    events = polymarket.fetch_wc_events(client, BY_FIXTURE, LOG)
    # FAST PATH: the cricket noise has no in-window fixture match, so it is dropped at fetch (never
    # priced) — only the soccer event survives.
    assert len(events) == 1
    assert "Sierra Leone" not in events[0].title            # cricket noise excluded before pricing

    raw: dict = {}
    cov, pbooks = polymarket.merge_into(raw, BY_FIXTURE, INDEX, events, now=NOW, log=LOG)
    assert cov.recovered == 1 and cov.matched == 1           # only the soccer event maps
    assert pbooks == {"idCIV": {"polymarket"}}

    outs = raw["idCIV"]["bookmakerOdds"]["polymarket"]["markets"]["101"]["outcomes"]
    home, draw, away = outs["101"]["players"]["0"], outs["102"]["players"]["0"], outs["103"]["players"]["0"]
    assert round(home["price"], 3) == 3.636 and home["limit"] == 275.0   # Côte d'Ivoire (p1 -> home)
    assert round(draw["price"], 3) == 2.985 and draw["limit"] == 335.0   # Draw
    assert round(away["price"], 3) == 2.532 and away["limit"] == 395.0   # Ecuador (p2 -> away)
    assert all(p["limit"] is not None for p in (home, draw, away))       # real limits, not low_confidence
    assert home["changedAt"] == "2026-06-14T10:00:00Z"                   # scan time, not a stale line


# --------------------------------------------------------------------------- #
# Slug-deterministic discovery: every WC fixture is found by its match slug,    #
# and a fixture the slug path can't resolve is logged UNMATCHED (not silent).   #
# --------------------------------------------------------------------------- #
# 8 in-window WC fixtures, like the live cache where keyword search built only 5.
_WC8 = [("Brazil", "Japan"), ("France", "Sweden"), ("Ivory Coast", "Norway"),
        ("Argentina", "Mexico"), ("Spain", "Germany"), ("England", "Italy"),
        ("Portugal", "Netherlands"), ("USA", "Canada")]
_WC_DATE = "2026-06-29"


def _wc_fixtures():
    return {f"id{i}": {"p1": h, "p2": a, "start_time": f"{_WC_DATE}T18:00:00.000Z",
                       "status_id": 0, "tournament": "World Cup"}
            for i, (h, a) in enumerate(_WC8)}


def _wc_event_and_books(home, away, eid):
    """A priceable 1x2 WC event dict for home vs away + the canned books for its three Yes tokens."""
    h, d, a = f"{eid}_h", f"{eid}_d", f"{eid}_a"
    ev = {"id": eid, "title": f"{home} vs. {away}", "closed": False,
          "eventDate": _WC_DATE, "startTime": f"{_WC_DATE}T18:00:00Z",
          "markets": [_gamma_market(f"Will {home} win on {_WC_DATE}?", h, f"{eid}_hn", date=_WC_DATE),
                      _gamma_market(f"Will {away} win on {_WC_DATE}?", a, f"{eid}_an", date=_WC_DATE),
                      _gamma_market(f"Will {home} vs. {away} end in a draw?", d, f"{eid}_dn", date=_WC_DATE)]}
    return ev, {h: _book(0.40, 100), d: _book(0.30, 100), a: _book(0.35, 100)}


def test_discovery_builds_every_wc_fixture_by_slug():
    """All 8 fixtures resolve via their deterministic match slug (fifwc-<a3>-<b3>-<date>) — keyword
    search is never needed and NO game is silently dropped (the live bug built only 5 of 8)."""
    fixtures = _wc_fixtures()
    slug_map, books = {}, {}
    for fid, info in fixtures.items():
        ev, bk = _wc_event_and_books(info["p1"], info["p2"], fid)
        slug_map[polymarket._wc_slug_candidates(info)[0]] = [ev]    # event sits at its primary slug
        books.update(bk)
    client = _FakeClient(events_by_slug=slug_map, books_by_token=books)

    events = polymarket.fetch_wc_events(client, fixtures, LOG)
    assert len(events) == 8                                          # every WC fixture built
    titles = " | ".join(e.title for e in events)
    for home, _ in _WC8:
        assert home in titles                                       # none dropped


def test_unmatched_fixture_logs_warning_instead_of_silent_drop(caplog):
    """A fixture whose every slug candidate 404s is NOT silently skipped: it logs an UNMATCHED warning
    naming the tried slug, and the other 7 fixtures still build."""
    fixtures = _wc_fixtures()
    slug_map, books, errors = {}, {}, set()
    drop = "id0"                                                     # Brazil vs Japan — make it 404
    for fid, info in fixtures.items():
        ev, bk = _wc_event_and_books(info["p1"], info["p2"], fid)
        if fid == drop:
            errors.update(polymarket._wc_slug_candidates(info))     # every ordering/date 404s
            continue
        slug_map[polymarket._wc_slug_candidates(info)[0]] = [ev]
        books.update(bk)
    client = _FakeClient(events_by_slug=slug_map, books_by_token=books, slug_errors=errors)

    with caplog.at_level("WARNING"):
        events = polymarket.fetch_wc_events(client, fixtures, LOG)
    assert len(events) == 7                                          # the rest survive
    assert "Brazil" not in " | ".join(e.title for e in events)      # the 404 game is absent...
    assert "UNMATCHED fixture Brazil vs Japan" in caplog.text       # ...but visible, not silent
    assert "fifwc-bra-jpn-2026-06-29" in caplog.text                # names the slug it tried


# --------------------------------------------------------------------------- #
# merge_into units: priced PolyEvents in, canonical book out                     #
# --------------------------------------------------------------------------- #
def _priced_event(title, legs, commence="2026-06-14T12:00:00Z", eid="1"):
    return polymarket.PolyEvent(event_id=eid, title=title, commence_iso=commence,
                                legs=[polymarket.Leg(role=r, team=t, decimal=d, limit=l) for (r, t, d, l) in legs])


def _civ_priced(commence="2026-06-14T12:00:00Z"):
    return _priced_event("Côte d'Ivoire vs. Ecuador", [
        ("team", "Côte d'Ivoire", 3.636, 275.0), ("team", "Ecuador", 2.532, 395.0),
        ("draw", None, 2.985, 335.0)], commence=commence)


def test_merge_normalizes_full_team_names_to_fixture():
    raw: dict = {}
    cov, pbooks = polymarket.merge_into(raw, BY_FIXTURE, INDEX, [_civ_priced()], now=NOW, log=LOG)
    assert cov.matched == 1 and cov.recovered == 1 and pbooks == {"idCIV": {"polymarket"}}
    outs = raw["idCIV"]["bookmakerOdds"]["polymarket"]["markets"]["101"]["outcomes"]
    assert round(outs["101"]["players"]["0"]["price"], 3) == 3.636   # Côte d'Ivoire -> p1/home


def test_merge_persists_venue_execution_ids():
    """ADDITIVE: each poly leg carries venue/venueId(token)/venueSide=BUY + negRisk/tickSize for the
    executor; existing price/limit/changedAt fields are unchanged."""
    legs = [polymarket.Leg(role="team", team="Côte d'Ivoire", yes_token="0xCIV",
                           decimal=3.636, limit=275.0, neg_risk=True, tick_size=0.01),
            polymarket.Leg(role="team", team="Ecuador", yes_token="0xECU",
                           decimal=2.532, limit=395.0, neg_risk=True, tick_size=0.01),
            polymarket.Leg(role="draw", team=None, yes_token="0xDRAW",
                           decimal=2.985, limit=335.0, neg_risk=True, tick_size=0.01)]
    ev = polymarket.PolyEvent(event_id="1", title="Côte d'Ivoire vs. Ecuador",
                              commence_iso="2026-06-14T12:00:00Z", legs=legs)
    raw: dict = {}
    polymarket.merge_into(raw, BY_FIXTURE, INDEX, [ev], now=NOW, log=LOG)
    home = raw["idCIV"]["bookmakerOdds"]["polymarket"]["markets"]["101"]["outcomes"]["101"]["players"]["0"]
    assert home["venue"] == "polymarket" and home["venueSide"] == "BUY"
    assert home["venueId"] == "0xCIV"               # the EXACT CLOB token priced
    assert home["negRisk"] is True and home["tickSize"] == 0.01
    # existing fields untouched
    assert round(home["price"], 3) == 3.636 and home["limit"] == 275.0 and home["mainLine"] is True


def test_merge_matches_cross_midnight_utc_fixture():
    """Kickoff 2026-06-15T01:00Z (next UTC day) vs event date 2026-06-14 -> must match within ±1 day."""
    by_fixture = {"idX": {"p1": "Ivory Coast", "p2": "Ecuador",
                          "start_time": "2026-06-15T01:00:00.000Z", "status_id": 0}}
    raw: dict = {}
    cov, pbooks = polymarket.merge_into(raw, by_fixture, INDEX, [_civ_priced()], now=NOW, log=LOG)
    assert cov.matched == 1 and cov.time_mismatch == [] and pbooks == {"idX": {"polymarket"}}


def test_merge_drops_unmatched_fixture():
    by_fixture = {"idAAA": {"p1": "Australia", "p2": "Türkiye",
                            "start_time": "2026-06-14T23:00:00.000Z", "status_id": 0}}
    raw: dict = {}
    cov, pbooks = polymarket.merge_into(raw, by_fixture, INDEX, [_civ_priced()], now=NOW, log=LOG)
    assert cov.matched == 0 and cov.recovered == 0
    assert pbooks == {} and raw == {}                                # injected nothing, created no envelope
    assert "Côte d'Ivoire vs. Ecuador" in cov.unmatched_name


# --------------------------------------------------------------------------- #
# Override gate: defer to an already-active OddsPapi polymarket book             #
# --------------------------------------------------------------------------- #
def test_merge_defers_to_active_oddspapi_polymarket():
    active = {"bookmakerIsActive": True, "suspended": False, "markets": {"101": {"marketActive": True,
        "outcomes": {"101": {"players": {"0": {"active": True, "price": 2.0}}}}}}}
    raw = {"idCIV": {"fixtureId": "idCIV", "startTime": "2026-06-14T23:00:00.000Z",
                     "bookmakerOdds": {"polymarket": active}}}
    cov, pbooks = polymarket.merge_into(raw, BY_FIXTURE, INDEX, [_civ_priced()], now=NOW, log=LOG)
    assert cov.deferred == 1 and cov.recovered == 0 and pbooks == {}
    # OddsPapi's active entry is left untouched (not overwritten with our legs).
    assert raw["idCIV"]["bookmakerOdds"]["polymarket"] is active


def test_merge_fills_when_oddspapi_polymarket_suspended():
    """OddsPapi supplied polymarket with markets present but every outcome suspended -> direct FILLS it."""
    suspended = {"bookmakerIsActive": True, "suspended": False, "markets": {"101": {"marketActive": True,
        "outcomes": {"101": {"players": {"0": {"active": False, "price": 2.0}}}}}}}
    raw = {"idCIV": {"fixtureId": "idCIV", "startTime": "2026-06-14T23:00:00.000Z",
                     "bookmakerOdds": {"polymarket": suspended}}}
    cov, pbooks = polymarket.merge_into(raw, BY_FIXTURE, INDEX, [_civ_priced()], now=NOW, log=LOG)
    assert cov.recovered == 1 and cov.deferred == 0 and pbooks == {"idCIV": {"polymarket"}}
    outs = raw["idCIV"]["bookmakerOdds"]["polymarket"]["markets"]["101"]["outcomes"]
    assert round(outs["101"]["players"]["0"]["price"], 3) == 3.636   # suspended stub overwritten


# --------------------------------------------------------------------------- #
# SAFE TIER: parse the '-more-markets' sibling (full-game totals O/U + BTTS)     #
# --------------------------------------------------------------------------- #
# Index extended with totals 2.5 (1010/1011) and BTTS (104/105) like test_kalshi.
MARKETS_FULL = MARKETS_JSON + [
    {"marketId": 1010, "sportId": 10, "marketType": "totals", "period": "fulltime", "handicap": 2.5,
     "marketName": "Over Under Full Time",
     "outcomes": [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]},
    {"marketId": 104, "sportId": 10, "marketType": "bothteamsscore", "period": "fulltime", "handicap": 0,
     "marketName": "Both Teams To Score",
     "outcomes": [{"outcomeId": 104, "outcomeName": "Yes"}, {"outcomeId": 105, "outcomeName": "No"}]},
]
INDEX_FULL = polymarket.build_market_index(MARKETS_FULL, 10)


def _binary_market(git, out_labels, tokens):
    return {"groupItemTitle": git, "question": f"Czechia vs. South Africa: {git}",
            "outcomes": out_labels, "clobTokenIds": tokens}


def _more_markets_event():
    """A '-more-markets' sibling holding full-game O/U + BTTS plus half/team variants that MUST be
    excluded (different scope)."""
    return {"markets": [
        _binary_market("O/U 2.5", '["Over", "Under"]', '["t_o25", "t_u25"]'),
        _binary_market("O/U 4.5", '["Over", "Under"]', '["t_o45", "t_u45"]'),
        _binary_market("Both Teams to Score", '["Yes", "No"]', '["t_btts_y", "t_btts_n"]'),
        # excluded: half + team variants
        _binary_market("1st Half O/U 2.5", '["Over", "Under"]', '["t_h1", "t_h2"]'),
        _binary_market("Czechia O/U 1.5", '["Over", "Under"]', '["t_ct1", "t_ct2"]'),
        _binary_market("Both Teams to Score in First Half", '["Yes", "No"]', '["t_hb1", "t_hb2"]'),
    ]}


def test_parse_more_markets_tokens_full_game_only():
    totals, btts = polymarket.parse_more_markets_tokens(_more_markets_event())
    assert totals == {2.5: ("t_o25", "t_u25"), 4.5: ("t_o45", "t_u45")}   # half/team excluded
    assert btts == ("t_btts_y", "t_btts_n")                               # half BTTS excluded


def test_merge_injects_polymarket_totals_and_btts():
    """A PolyEvent carrying priced totals + BTTS legs injects them onto the matched fixture's
    polymarket book under the canonical totals/BTTS marketIds (only index-known lines)."""
    ev = _civ_priced()
    ev.totals = {
        2.5: (polymarket.Leg(role="over", decimal=2.27, limit=112000.0),
              polymarket.Leg(role="under", decimal=1.75, limit=130000.0)),
        9.5: (polymarket.Leg(role="over", decimal=50.0, limit=10.0),       # line not in index -> dropped
              polymarket.Leg(role="under", decimal=1.01, limit=5.0)),
    }
    ev.btts = (polymarket.Leg(role="yes", decimal=2.0, limit=151000.0),
               polymarket.Leg(role="no", decimal=1.96, limit=66000.0))
    raw: dict = {}
    cov, pbooks = polymarket.merge_into(raw, BY_FIXTURE, INDEX_FULL, [ev], now=NOW, log=LOG)
    assert cov.recovered == 1 and pbooks == {"idCIV": {"polymarket"}}
    assert cov.totals_fixtures == 1 and cov.totals_lines == 1 and cov.btts_fixtures == 1
    mkts = raw["idCIV"]["bookmakerOdds"]["polymarket"]["markets"]
    assert "101" in mkts                                          # 1x2 still present
    t = mkts["1010"]["outcomes"]
    assert t["1010"]["players"]["0"]["price"] == 2.27 and t["1010"]["players"]["0"]["limit"] == 112000.0
    assert t["1011"]["players"]["0"]["price"] == 1.75            # Under
    assert "9.5" not in str(mkts)                                # uncatalogued line not injected
    b = mkts["104"]["outcomes"]
    assert b["104"]["players"]["0"]["price"] == 2.0 and b["105"]["players"]["0"]["price"] == 1.96


# --------------------------------------------------------------------------- #
# STEP 4: spreads/handicap line-for-line + sign gate + no-push half-lines       #
# --------------------------------------------------------------------------- #
from src import theoddsapi  # noqa: E402

# Catalog with 1x2, totals 2.5/3.5, AH -1.5/+1.5 (half) and AH -1.0 (whole-number, push-prone).
SPREAD_MARKETS = MARKETS_JSON + [
    {"marketId": 1010, "sportId": 10, "marketType": "totals", "period": "fulltime", "handicap": 2.5,
     "marketName": "Over Under Full Time",
     "outcomes": [{"outcomeId": 1010, "outcomeName": "Over"}, {"outcomeId": 1011, "outcomeName": "Under"}]},
    {"marketId": 1012, "sportId": 10, "marketType": "totals", "period": "fulltime", "handicap": 3.5,
     "marketName": "Over Under Full Time",
     "outcomes": [{"outcomeId": 1012, "outcomeName": "Over"}, {"outcomeId": 1013, "outcomeName": "Under"}]},
    {"marketId": 1500, "sportId": 10, "marketType": "spreads", "period": "fulltime", "handicap": -1.5,
     "marketName": "Asian Handicap",
     "outcomes": [{"outcomeId": 1500, "outcomeName": "1"}, {"outcomeId": 1501, "outcomeName": "2"}]},
    {"marketId": 1502, "sportId": 10, "marketType": "spreads", "period": "fulltime", "handicap": 1.5,
     "marketName": "Asian Handicap",
     "outcomes": [{"outcomeId": 1502, "outcomeName": "1"}, {"outcomeId": 1503, "outcomeName": "2"}]},
    {"marketId": 1600, "sportId": 10, "marketType": "spreads", "period": "fulltime", "handicap": -1.0,
     "marketName": "Asian Handicap",
     "outcomes": [{"outcomeId": 1600, "outcomeName": "1"}, {"outcomeId": 1601, "outcomeName": "2"}]},
]
INDEX_SPREADS = polymarket.build_market_index(SPREAD_MARKETS, 10)
BY_FIXTURE_CZE = {"idCZE": {"p1": "Czechia", "p2": "South Africa",
                            "start_time": "2026-06-14T23:00:00.000Z", "status_id": 0, "tournament": "WC"}}


def _spread_leg(d, l):
    return polymarket.Leg(role="spread", decimal=d, limit=l)


def _cze_event_with_spreads(spreads):
    ev = _priced_event("Czechia vs. South Africa",
                       [("team", "Czechia", 1.9, 500.0), ("team", "South Africa", 4.2, 500.0),
                        ("draw", None, 3.6, 500.0)], commence="2026-06-14T12:00:00Z")
    ev.spreads = spreads
    return ev


def test_parse_spread_markets_extracts_team_and_line():
    ev = {"markets": [
        _binary_market("Czechia (-1.5)", '["Czechia", "South Africa"]', '["t_cze", "t_rsa"]'),
        _binary_market("South Africa (-2.5)", '["Czechia", "South Africa"]', '["t_cze2", "t_rsa2"]'),
        _binary_market("O/U 2.5", '["Over", "Under"]', '["t_o", "t_u"]'),    # not a spread
    ]}
    sp = polymarket.parse_spread_markets(ev)
    assert {(s["named"], s["line"]) for s in sp} == {("Czechia", -1.5), ("South Africa", -2.5)}
    cze = next(s for s in sp if s["named"] == "Czechia")
    assert cze["named_token"] == "t_cze" and cze["opp_token"] == "t_rsa" and cze["opp"] == "South Africa"


def test_spread_maps_line_for_line_both_directions():
    # Czechia (-1.5): Czechia=p1 covers -1.5 -> canonical spreads[-1.5] (mid 1500), home=Czechia.
    ev = _cze_event_with_spreads([{"named": "Czechia", "line": -1.5, "opp": "South Africa",
                                   "named_leg": _spread_leg(2.10, 500), "opp_leg": _spread_leg(1.95, 500)}])
    raw: dict = {}
    cov, _ = polymarket.merge_into(raw, BY_FIXTURE_CZE, INDEX_SPREADS, [ev], now=NOW, log=LOG)
    assert cov.spreads_lines == 1 and cov.spreads_fixtures == 1
    o = raw["idCZE"]["bookmakerOdds"]["polymarket"]["markets"]["1500"]["outcomes"]
    assert o["1500"]["players"]["0"]["price"] == 2.10   # Czechia covers -1.5 -> home_oid
    assert o["1501"]["players"]["0"]["price"] == 1.95   # South Africa covers +1.5 -> away_oid

    # South Africa (-1.5): SA=p2 covers -1.5 -> p1(Czechia) covers +1.5 -> spreads[+1.5] (mid 1502).
    ev2 = _cze_event_with_spreads([{"named": "South Africa", "line": -1.5, "opp": "Czechia",
                                    "named_leg": _spread_leg(3.0, 500), "opp_leg": _spread_leg(1.4, 500)}])
    raw2: dict = {}
    polymarket.merge_into(raw2, BY_FIXTURE_CZE, INDEX_SPREADS, [ev2], now=NOW, log=LOG)
    o2 = raw2["idCZE"]["bookmakerOdds"]["polymarket"]["markets"]["1502"]["outcomes"]
    assert o2["1502"]["players"]["0"]["price"] == 1.4   # Czechia covers +1.5 -> home_oid
    assert o2["1503"]["players"]["0"]["price"] == 3.0   # South Africa covers -1.5 -> away_oid


def test_spread_sign_gate_rejects_wrong_teams():
    """A flipped/mislabelled pair whose two outcomes are NOT exactly this fixture's two teams is
    rejected — it can never land on a complementary canonical outcome (no phantom)."""
    ev = _cze_event_with_spreads([{"named": "Czechia", "line": -1.5, "opp": "Mexico",   # wrong opponent
                                   "named_leg": _spread_leg(2.0, 500), "opp_leg": _spread_leg(2.0, 500)}])
    raw: dict = {}
    cov, _ = polymarket.merge_into(raw, BY_FIXTURE_CZE, INDEX_SPREADS, [ev], now=NOW, log=LOG)
    assert cov.spreads_lines == 0
    assert "1500" not in raw["idCZE"]["bookmakerOdds"]["polymarket"]["markets"]


def test_spread_whole_number_line_rejected_at_mapping():
    """No-push rule: a whole-number handicap (-1.0) is never ingested (push refunds the stake)."""
    ev = _cze_event_with_spreads([{"named": "Czechia", "line": -1.0, "opp": "South Africa",
                                   "named_leg": _spread_leg(2.0, 500), "opp_leg": _spread_leg(2.0, 500)}])
    raw: dict = {}
    cov, _ = polymarket.merge_into(raw, BY_FIXTURE_CZE, INDEX_SPREADS, [ev], now=NOW, log=LOG)
    assert cov.spreads_lines == 0 and "1600" not in raw["idCZE"]["bookmakerOdds"]["polymarket"]["markets"]


# --------------------------------------------------------------------------- #
# Engine-level: no-push half-line guard + scope + totals same/diff line          #
# --------------------------------------------------------------------------- #
def _scan_cfg():
    return Config(raw={
        "target_window": {"from_utc": "2026-06-10T00:00:00Z", "to_utc": "2026-06-16T23:59:59Z"},
        "thresholds": {"min_roi_pct": 0.5, "roi_suspicious_pct": 8.0, "min_total_stake": 20,
                       "max_leg_age_far_minutes": 360, "max_leg_age_mid_minutes": 60,
                       "max_leg_age_near_minutes": 20, "stale_far_horizon_hours": 6,
                       "stale_near_horizon_hours": 1, "near_miss_ceiling_S": 1.02},
        "markets": {"allow_quarter_lines": False},
    }, secrets=Secrets(None, None, None))


def _scan_ctx():
    return EngineCtx(actionable={"kalshi", "polymarket"}, tracked={"kalshi", "polymarket"},
                     exchanges=set(), commission={}, clone_group_of=build_clone_group_fn([]),
                     reference_books=[])


def _two_book_market(mid, oid_a, price_a, oid_b, price_b, book_a="kalshi", book_b="polymarket"):
    """Raw feed: book_a prices outcome oid_a, book_b prices oid_b, on the SAME fixture/marketId."""
    cu = "2026-06-14T09:50:00Z"

    def leg(p):
        return {"players": {"0": {"price": p, "limit": 500, "changedAt": cu, "mainLine": True, "active": True}}}
    return [{"fixtureId": "idCZE", "startTime": "2026-06-14T23:00:00.000Z", "statusId": 0, "hasOdds": True,
             "bookmakerOdds": {
                 book_a: {"bookmakerIsActive": True, "suspended": False,
                          "markets": {str(mid): {"marketActive": True, "outcomes": {str(oid_a): leg(price_a)}}}},
                 book_b: {"bookmakerIsActive": True, "suspended": False,
                          "markets": {str(mid): {"marketActive": True, "outcomes": {str(oid_b): leg(price_b)}}}}}}]


def test_totals_same_line_emits_balanced_reg_time_arb():
    specs, _ = build_market_specs(SPREAD_MARKETS, 10, [], [])
    # kalshi Over 2.5 @2.10 + polymarket Under 2.5 @2.10 on mid 1010 -> S=0.952 -> arb.
    feeds = parse_odds_payload(_two_book_market(1010, 1010, 2.10, 1011, 2.10))
    opps, stats = _scan(feeds, specs, _scan_ctx(), _scan_cfg(), BY_FIXTURE_CZE, {}, NOW, LOG, {}, {}, {})
    assert stats["real_arbs"] + stats["shadow_arbs"] >= 1
    arb = opps[0].res
    assert arb.is_arb and abs(sum(l.stake for l in arb.legs) - arb.t_max) < 1.0   # balanced


def test_totals_mismatched_lines_never_pair():
    specs, _ = build_market_specs(SPREAD_MARKETS, 10, [], [])
    # kalshi Over 2.5 (mid 1010) + polymarket Under 3.5 (mid 1012) -> different markets, each incomplete.
    feeds = parse_odds_payload([
        {"fixtureId": "idCZE", "startTime": "2026-06-14T23:00:00.000Z", "statusId": 0, "hasOdds": True,
         "bookmakerOdds": {
             "kalshi": {"bookmakerIsActive": True, "suspended": False, "markets": {"1010": {"marketActive": True,
                 "outcomes": {"1010": {"players": {"0": {"price": 2.10, "limit": 500,
                     "changedAt": "2026-06-14T09:50:00Z", "active": True}}}}}}},
             "polymarket": {"bookmakerIsActive": True, "suspended": False, "markets": {"1012": {"marketActive": True,
                 "outcomes": {"1013": {"players": {"0": {"price": 2.10, "limit": 500,
                     "changedAt": "2026-06-14T09:50:00Z", "active": True}}}}}}}}}])
    opps, stats = _scan(feeds, specs, _scan_ctx(), _scan_cfg(), BY_FIXTURE_CZE, {}, NOW, LOG, {}, {}, {})
    assert stats["real_arbs"] == 0 and stats["shadow_arbs"] == 0   # different lines never pair


def test_scan_skips_whole_number_handicap_even_if_priced():
    """No-push guard: a whole-number AH (mid 1600, line -1.0) with BOTH outcomes priced and a
    crossing S<1 is still NOT scanned (push-prone), so no arb is emitted."""
    specs, _ = build_market_specs(SPREAD_MARKETS, 10, [], [])
    assert specs[1600].family == "asian_handicap" and specs[1600].is_whole_line   # sanity
    feeds = parse_odds_payload(_two_book_market(1600, 1600, 2.10, 1601, 2.10))    # S=0.952 if scanned
    opps, stats = _scan(feeds, specs, _scan_ctx(), _scan_cfg(), BY_FIXTURE_CZE, {}, NOW, LOG, {}, {}, {})
    assert stats["real_arbs"] == 0 and stats["shadow_arbs"] == 0
    # the half-line market (1500) WOULD scan, proving it's the whole-line that's excluded:
    feeds2 = parse_odds_payload(_two_book_market(1500, 1500, 2.10, 1501, 2.10))
    _, stats2 = _scan(feeds2, specs, _scan_ctx(), _scan_cfg(), BY_FIXTURE_CZE, {}, NOW, LOG, {}, {}, {})
    assert stats2["real_arbs"] + stats2["shadow_arbs"] >= 1


def test_scope_rule_tournament_event_not_ingested():
    """PER-GAME SCOPE: a tournament-long event (World Cup Winner: many team markets, no draw) is not a
    valid per-game event -> parse_event_legs returns None -> never ingested."""
    tournament = {"id": "30615", "slug": "world-cup-winner", "title": "World Cup Winner",
                  "markets": [
                      {"groupItemTitle": "Brazil", "question": "Will Brazil win the World Cup?",
                       "outcomes": '["Yes", "No"]', "clobTokenIds": '["a", "b"]'},
                      {"groupItemTitle": "Argentina", "question": "Will Argentina win the World Cup?",
                       "outcomes": '["Yes", "No"]', "clobTokenIds": '["c", "d"]'}]}
    assert polymarket.parse_event_legs(tournament) is None


# --------------------------------------------------------------------------- #
# Shadow rollout gate in run._scan                                              #
# --------------------------------------------------------------------------- #
def _cfg(poly_actionable=False):
    raw = {
        "target_window": {"from_utc": "2026-06-10T00:00:00Z", "to_utc": "2026-06-16T23:59:59Z"},
        "thresholds": {"min_roi_pct": 0.5, "roi_suspicious_pct": 8.0, "min_total_stake": 20,
                       "max_leg_age_far_minutes": 360, "max_leg_age_mid_minutes": 60,
                       "max_leg_age_near_minutes": 20, "stale_far_horizon_hours": 6,
                       "stale_near_horizon_hours": 1, "near_miss_ceiling_S": 1.02},
        "markets": {"allow_quarter_lines": False},
        "polymarket": {"actionable": poly_actionable},
    }
    return Config(raw=raw, secrets=Secrets(None, None, None))


def _poly_arb_feeds():
    """1x2 fixture where polymarket(direct) + pinnacle is a >0-ROI arb (pinnacle alone is no arb)."""
    ko = "2026-06-14T23:00:00.000Z"
    cu = "2026-06-14T09:50:00Z"

    def book(h, d, a):
        def leg(p):
            return {"players": {"0": {"price": p, "limit": 500, "changedAt": cu, "mainLine": True, "active": True}}}
        return {"bookmakerIsActive": True, "suspended": False,
                "markets": {"101": {"marketActive": True, "outcomes": {"101": leg(h), "102": leg(d), "103": leg(a)}}}}

    raw = [{"fixtureId": "idCIV", "startTime": ko, "statusId": 0, "hasOdds": True,
            "bookmakerOdds": {"pinnacle": book(2.1, 3.0, 3.0), "polymarket": book(1.9, 3.6, 4.2)}}]
    return parse_odds_payload(raw)


def _ctx():
    return EngineCtx(actionable={"pinnacle", "polymarket"}, tracked={"pinnacle", "polymarket"},
                     exchanges=set(), commission={}, clone_group_of=build_clone_group_fn([]),
                     reference_books=[])


def test_polymarket_leg_not_actionable_while_shadow():
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    pbooks = {"idCIV": {"polymarket"}}
    opps, stats = _scan(_poly_arb_feeds(), specs, _ctx(), _cfg(poly_actionable=False),
                        BY_FIXTURE, {}, NOW, LOG, {}, {}, pbooks)
    assert stats["shadow_arbs"] >= 1
    assert all(o.actionable is False for o in opps)    # polymarket-direct leg forced shadow


def test_polymarket_leg_actionable_when_gate_open():
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    pbooks = {"idCIV": {"polymarket"}}
    opps, _ = _scan(_poly_arb_feeds(), specs, _ctx(), _cfg(poly_actionable=True),
                    BY_FIXTURE, {}, NOW, LOG, {}, {}, pbooks)
    assert any(o.actionable for o in opps)


def test_three_way_arb_pairs_polymarket_draw_across_books_no_leg_dropped():
    """End-to-end: a priced Polymarket event whose draw leg arrived team=None is labeled by the guard,
    merged at the canonical draw outcomeId (102), and survives a cross-book 3-way 1x2 arb — all three
    outcomes (home/draw/away) covered with NO leg dropped, the draw sourced from Polymarket."""
    poly = _priced_event("Côte d'Ivoire vs. Ecuador", [
        ("team", "Côte d'Ivoire", 3.636, 275.0), ("team", "Ecuador", 2.532, 395.0),
        ("draw", None, 2.985, 335.0)])                         # draw arrives None -> guard labels "Draw"
    assert [leg.team for leg in poly.legs if leg.role == "draw"] == ["Draw"]
    raw: dict = {}
    polymarket.merge_into(raw, BY_FIXTURE, INDEX, [poly], now=NOW, log=LOG)

    # A second book quotes all three 1x2 outcomes with strong home/away but a WEAK draw, so the best
    # draw across the two books is Polymarket's — it must be picked, not dropped.
    cu = "2026-06-14T09:50:00Z"
    leg = lambda p: {"players": {"0": {"price": p, "limit": 500, "changedAt": cu,
                                       "mainLine": True, "active": True}}}
    raw["idCIV"]["bookmakerOdds"]["pinnacle"] = {
        "bookmakerIsActive": True, "suspended": False,
        "markets": {"101": {"marketActive": True,
                            "outcomes": {"101": leg(3.70), "102": leg(2.50), "103": leg(2.70)}}}}

    feeds = parse_odds_payload([raw["idCIV"]])
    specs, _ = build_market_specs(MARKETS_JSON, 10, [], [])
    opps, stats = _scan(feeds, specs, _ctx(), _cfg(poly_actionable=True),
                        BY_FIXTURE, {}, NOW, LOG, {}, {}, {"idCIV": {"polymarket"}})

    arb = next(o for o in opps if o.res.is_arb)
    assert {l.outcome_id for l in arb.res.legs} == {101, 102, 103}    # all three outcomes, none dropped
    draw_leg = next(l for l in arb.res.legs if l.outcome_id == 102)
    assert draw_leg.book == "polymarket"                              # Polymarket's draw leg paired in
