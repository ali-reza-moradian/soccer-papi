"""Tests for the GenZ tree builder (src/genz/tree_builder.py) on the real CIV-NOR JSON shapes.

The Polymarket side now enumerates the WHOLE `soccer-fifwc` series (events_by_series) and filters a
game's events by slug prefix, so EVERY sibling event is loaded (-more-markets, -total-corners,
-exact-score, -halftime-result, -second-half-result, -first-to-score, -player-props). The builder
must pair Kalshi outcomes to their Polymarket twins across all of these, producing dozens of 2-way
nodes (not just the 3 moneyline ones), with the unmatched list containing BOTH venues."""
from __future__ import annotations

from datetime import datetime, timezone

from src.genz import match_rules as mr
from src.genz import tree_builder as tb
from src.genz.config import GenzConfig
from src.genz.sports_base import pairing_alert

NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)   # game kicks off 2026-06-30 (within 48h)
SUF = "26JUN30CIVNOR"
BASE = "fifwc-civ-nor-2026-06-30"


# --------------------------------------------------------------------------- #
# Kalshi canned markets (one per-type series for the game)                      #
# --------------------------------------------------------------------------- #
def _km(series, sub, title=None):
    et = f"{series}-{SUF}"
    return {"event_ticker": et, "ticker": f"{et}-{abs(hash(sub + (title or ''))) % 9999}",
            "yes_sub_title": sub, "title": title if title is not None else sub, "status": "active"}


_GAME_MARKETS = [_km("KXWCGAME", "Norway"), _km("KXWCGAME", "Tie"), _km("KXWCGAME", "Ivory Coast")]
_KALSHI_BY_EVENT = {
    f"KXWCGAME-{SUF}": _GAME_MARKETS,
    f"KXWCTOTAL-{SUF}": [_km("KXWCTOTAL", "Over 1.5 goals"), _km("KXWCTOTAL", "Over 2.5 goals"),
                         _km("KXWCTOTAL", "Over 3.5 goals")],
    f"KXWCTEAMTOTAL-{SUF}": [_km("KXWCTEAMTOTAL", "Norway over 1.5 goals")],
    # misleading yes_sub_title (the opponent); the covering team must come from the TITLE.
    f"KXWCSPREAD-{SUF}": [_km("KXWCSPREAD", "Côte d'Ivoire",
                              title="Goal Diff Reg Time: Norway wins by more than 1.5 goals")],
    f"KXWCBTTS-{SUF}": [_km("KXWCBTTS", "Both teams to score")],
    f"KXWC1HBTTS-{SUF}": [_km("KXWC1HBTTS", "Both teams to score")],
    f"KXWC2HBTTS-{SUF}": [_km("KXWC2HBTTS", "Both teams to score")],
    f"KXWC1HTOTAL-{SUF}": [_km("KXWC1HTOTAL", "Over 1.5 goals")],
    f"KXWC2HTOTAL-{SUF}": [_km("KXWC2HTOTAL", "Over 0.5 goals")],
    f"KXWCTCORNERS-{SUF}": [_km("KXWCTCORNERS", "9+ corners")],
    f"KXWCCORNERS-{SUF}": [_km("KXWCCORNERS", "Norway 4+ corners")],
    # raw/ticker-ish subs; the true scoreline is parsed from the TITLE text.
    f"KXWCSCORE-{SUF}": [_km("KXWCSCORE", "NOR2CIV1", title="Reg Time: Norway wins 2-1"),
                         _km("KXWCSCORE", "draw11", title="Reg Time: Draw 1-1"),
                         _km("KXWCSCORE", "CIV2NOR1", title="Reg Time: Ivory Coast wins 2-1")],
    f"KXWC1H-{SUF}": [_km("KXWC1H", "Tie 1st Half"), _km("KXWC1H", "Norway wins 1st Half"),
                      _km("KXWC1H", "Ivory Coast wins 1st Half")],
    f"KXWC2H-{SUF}": [_km("KXWC2H", "Tie 2nd Half"), _km("KXWC2H", "Norway wins 2nd Half"),
                      _km("KXWC2H", "Ivory Coast wins 2nd Half")],
    f"KXWCADVANCE-{SUF}": [_km("KXWCADVANCE", "Norway advances")],
    f"KXWCFTTS-{SUF}": [_km("KXWCFTTS", "Norway"), _km("KXWCFTTS", "Ivory Coast"), _km("KXWCFTTS", "No Goal")],
    f"KXWCGOAL-{SUF}": [_km("KXWCGOAL", "Erling Haaland 2+")],
    f"KXWCSOA-{SUF}": [_km("KXWCSOA", "Erling Haaland")],          # no Poly twin -> unmatched (kalshi)
}


class _FakeKalshi:
    def iter_markets(self, *, series_ticker=None, status="open", limit=100, max_pages=50):
        return list(_GAME_MARKETS) if series_ticker == "KXWCGAME" else []

    def markets(self, *, series_ticker=None, event_ticker=None, status="open", limit=100, cursor=None):
        return {"markets": list(_KALSHI_BY_EVENT.get(event_ticker, []))}


# --------------------------------------------------------------------------- #
# Polymarket canned events (the whole series; builder filters by slug prefix)    #
# --------------------------------------------------------------------------- #
def _single(git, tok):                                   # a single-outcome Yes market
    return {"groupItemTitle": git, "question": f"Will {git}?", "outcomes": ["Yes", "No"],
            "clobTokenIds": [tok, f"{tok}_no"]}


def _ou(git, over, under):                               # a binary Over/Under market
    return {"groupItemTitle": git, "outcomes": ["Over", "Under"], "clobTokenIds": [over, under]}


def _yn(git, yes, no):                                   # a binary Yes/No market
    return {"groupItemTitle": git, "outcomes": ["Yes", "No"], "clobTokenIds": [yes, no]}


def _ev(slug, markets):
    return {"id": slug, "slug": slug, "closed": False, "eventDate": "2026-06-30",
            "title": slug, "markets": markets}


_SERIES_EVENTS = [
    _ev(BASE, [_single("Norway", "nor_y"), _single("Côte d'Ivoire", "civ_y"),
               {"groupItemTitle": "Draw (Côte d'Ivoire vs. Norway)",
                "question": "Will the match end in a draw?", "outcomes": ["Yes", "No"],
                "clobTokenIds": ["drw_y", "drw_n"]}]),
    _ev(f"{BASE}-more-markets", [
        _ou("O/U 1.5", "ov15", "un15"), _ou("O/U 2.5", "ov25", "un25"), _ou("O/U 3.5", "ov35", "un35"),
        _ou("O/U 5.5", "ov55", "un55"),                     # no Kalshi twin -> unmatched (poly)
        {"groupItemTitle": "Norway (-1.5)", "outcomes": ["Norway", "Côte d'Ivoire"],
         "clobTokenIds": ["sp_nor", "sp_civ"]},
        _ou("Norway O/U 1.5", "tt_nor_o", "tt_nor_u"),
        _yn("Both Teams to Score", "btts_y", "btts_n"),
        _yn("Both Teams to Score - First Half", "btts1_y", "btts1_n"),
        _yn("Both Teams to Score - Second Half", "btts2_y", "btts2_n"),
        _ou("1st Half O/U 1.5", "h1o15", "h1u15"),
        _ou("2nd Half O/U 0.5", "h2o05", "h2u05"),
        _yn("Norway to Advance", "adv_nor", "adv_civ"),
    ]),
    _ev(f"{BASE}-total-corners", [_ou("O/U 8.5", "co_o", "co_u"), _ou("Norway O/U 3.5", "tc_nor_o", "tc_nor_u")]),
    # Poly exact-score titles are 'Côte d'Ivoire A - B Norway' (CIV scored A, Norway scored B).
    _ev(f"{BASE}-exact-score", [_single("Côte d'Ivoire 2 - 1 Norway", "sc_civ21"),
                                _single("1 - 1", "sc_11"), _single("Côte d'Ivoire 1 - 2 Norway", "sc_nor21")]),
    _ev(f"{BASE}-halftime-result", [_single("Norway", "h_nor"), _single("Côte d'Ivoire", "h_civ"),
                                    _single("Draw", "h_drw")]),
    _ev(f"{BASE}-second-half-result", [_single("Norway", "s_nor"), _single("Côte d'Ivoire", "s_civ"),
                                       _single("Draw", "s_drw")]),
    _ev(f"{BASE}-first-to-score", [_single("Norway", "fts_nor"), _single("Côte d'Ivoire", "fts_civ"),
                                   _single("Neither team", "fts_none")]),
    _ev(f"{BASE}-player-props", [_single("Haaland: 2+ goals", "pp_haal2"),
                                 _single("Haaland: 3+ goals", "pp_haal3")]),   # 3+ -> unmatched (poly)
]


class _FakePoly:
    def events_by_series(self, series_slug, *, closed=False, page_limit=100, max_pages=50):
        return list(_SERIES_EVENTS)


def _build():
    return tb.build_tree(_FakeKalshi(), _FakePoly(), GenzConfig(), now=NOW)


def _by_type(tree):
    by = {}
    for n in tree["games"][SUF]["nodes"]:
        by.setdefault(n["market_type"], []).append(n)
    return by


# --------------------------------------------------------------------------- #
# Tests                                                                          #
# --------------------------------------------------------------------------- #
def test_builds_two_way_nodes_for_all_sibling_market_types():
    by = _by_type(_build())
    # The whole point of the fix: not just moneyline — every sibling market type pairs.
    for mt in ("total_goals", "team_total", "spread", "btts", "1h_btts", "2h_btts",
               "1h_total", "2h_total", "corners", "team_corners", "exact_score",
               "1h_result", "2h_result", "first_to_score", "advance"):
        assert mt in by, f"missing matched nodes for {mt}"
    assert len(by["total_goals"]) >= 4                       # 1.5/2.5/3.5 lines x over+under
    assert {n["side"] for n in by["spread"]} == {"cover", "plus"}   # complements of ONE team's line
    assert {n["side"] for n in by["btts"]} == {"yes", "no"}
    # all the 2-way families are flagged 2way (engine-tradeable); moneyline/halves stay 3-way.
    for mt in ("total_goals", "corners", "spread", "btts", "1h_total", "advance"):
        assert all(n["kind"] == "2way" for n in by[mt])
    assert all(n["kind"] == "3way" for n in by["moneyline"])
    assert all(n["kind"] == "3way" for n in by["1h_result"] + by["2h_result"])


def test_exact_score_and_spread_pair_correct_team_ordering():
    """The two team-ordering fixes, end to end: CIV-wins-2-1 pairs to Poly 'CIV 2 - 1 NOR' (not the
    Norway scoreline), and Norway -1.5 pairs to the Norway-handicap Poly token (not Côte d'Ivoire)."""
    by = _by_type(_build())
    civ21 = next(n for n in by["exact_score"] if n["side"] == "2-1")        # CIV wins 2-1 (away-home)
    assert civ21["poly_token_id"] == "sc_civ21"
    nor21 = next(n for n in by["exact_score"] if n["side"] == "1-2")        # NOR wins 2-1
    assert nor21["poly_token_id"] == "sc_nor21"
    # Spread: keyed by the FAVORED team (Norway). 'cover' = Norway -1.5 (Kalshi YES + Poly sp_nor),
    # 'plus' = Côte d'Ivoire +1.5 (Kalshi NO + Poly sp_civ) — genuine complements of the SAME line.
    cover = next(n for n in by["spread"] if n["side"] == "cover")
    assert cover["market_key"] == "spread|1.5|norway"
    assert cover["poly_token_id"] == "sp_nor" and cover["kalshi_side"] == "YES"
    plus = next(n for n in by["spread"] if n["side"] == "plus")
    assert plus["market_key"] == "spread|1.5|norway"
    assert plus["poly_token_id"] == "sp_civ" and plus["kalshi_side"] == "NO"


def test_spread_legs_are_complements_of_one_team_line_not_two_favorites():
    """A spread market's two legs must be cover vs plus of ONE team's handicap (Norway -1.5 / +1.5),
    sourced from the matching venue side — NOT two different teams' -1.5 favorites (the 935% bug)."""
    by = _by_type(_build())
    spread = by["spread"]
    assert len(spread) == 2 and {n["side"] for n in spread} == {"cover", "plus"}
    assert all(n["market_key"] == "spread|1.5|norway" for n in spread)        # one favored-team line
    cover = next(n for n in spread if n["side"] == "cover")
    plus = next(n for n in spread if n["side"] == "plus")
    # cover (Norway -1.5) and plus (Côte d'Ivoire +1.5) are different tokens / opposite Kalshi sides.
    assert cover["poly_token_id"] != plus["poly_token_id"]
    assert {cover["kalshi_side"], plus["kalshi_side"]} == {"YES", "NO"}


def test_corners_threshold_pairs_kalshi_9plus_to_poly_8_5():
    by = _by_type(_build())
    over = next(n for n in by["corners"] if n["side"] == "over")
    assert over["line"] == 8.5 and over["poly_token_id"] == "co_o"   # 9+ (>=9) == O/U 8.5 OVER


def test_total_goals_node_carries_both_identifiers():
    by = _by_type(_build())
    over25 = next(n for n in by["total_goals"] if n["line"] == 2.5 and n["side"] == "over")
    assert over25["kalshi_ticker"] and over25["kalshi_side"] == "YES"
    assert over25["poly_token_id"] == "ov25" and over25["poly_side"] == "Over"


def test_unmatched_contains_both_venues():
    unmatched = _build()["games"][SUF]["unmatched"]
    venues = {u["venue"] for u in unmatched}
    assert "kalshi" in venues and "polymarket" in venues          # NOT 100% kalshi anymore
    # Kalshi SOA has no Poly twin; Poly O/U 5.5 + Haaland 3+ have no Kalshi twin.
    assert any(u["venue"] == "kalshi" and u["market_type"] == "soa" for u in unmatched)
    assert any(u["venue"] == "polymarket" for u in unmatched)


def test_many_more_two_way_nodes_than_moneyline():
    nodes = _build()["games"][SUF]["nodes"]
    two_way = [n for n in nodes if n["kind"] == "2way"]
    assert len(two_way) >= 20                                     # dozens of 2-way nodes, not 3


def test_total_goals_over_under_are_complements_of_one_market():
    """Same-line Over/Under (full game AND each half) come from ONE Kalshi market — Under is its NO
    side (1 - yes of the same Over), not a separate market/line/period — so they're true complements."""
    by = _by_type(_build())
    for mt, line in (("total_goals", 2.5), ("2h_total", 0.5), ("1h_total", 1.5)):
        sides = [n for n in by[mt] if n["line"] == line]
        over = next(n for n in sides if n["side"] == "over")
        under = next(n for n in sides if n["side"] == "under")
        assert over["kalshi_ticker"] == under["kalshi_ticker"]        # SAME Kalshi Over market
        assert {over["kalshi_side"], under["kalshi_side"]} == {"YES", "NO"}   # YES=Over, NO=Under
        assert over["poly_token_id"] != under["poly_token_id"]        # distinct Over/Under tokens
        assert over["market_key"] == under["market_key"]             # one line/period


def test_game_wide_half_total_pairs_only_with_no_team_poly():
    """THE totals bug: Kalshi's GAME-WIDE 1H total (KXWC1HTOTAL = any first-half goal, both teams)
    must pair ONLY with the no-team Poly '1st Half O/U'. A Poly '<Team> 1st Half O/U' is a single-team
    half-total — it must NEVER populate the game-wide 1h_total node (it becomes an unmatched
    1h_team_total instead)."""
    game = tb.Game(game_id="26JUN30BELSEN", kalshi_suffix="26JUN30BELSEN", date="2026-06-30",
                   home="Belgium", away="Senegal", kickoff_iso="", poly_base_slug="b")
    ev = {"slug": "b-more-markets", "closed": False, "markets": [
        {"groupItemTitle": "1st Half O/U 0.5", "outcomes": ["Over", "Under"], "clobTokenIds": ["gw_o", "gw_u"]},
        {"groupItemTitle": "Senegal 1st Half O/U 0.5", "outcomes": ["Over", "Under"], "clobTokenIds": ["sen_o", "sen_u"]},
    ]}
    p_opts: dict = {}
    for o, tok, side in tb._poly_event_outcomes(ev, "more", game):
        p_opts.setdefault(o.twin_key(), {
            "market_type": o.market_type, "market_key": o.group, "side": o.side, "line": o.line,
            "kind": o.kind, "confidence": o.confidence, "outcome_label": side,
            "poly_token_id": tok, "poly_side": side})
    km = {"event_ticker": "KXWC1HTOTAL-26JUN30BELSEN", "ticker": "KXWC1HTOTAL-X",
          "yes_sub_title": "Over 0.5 goals", "title": "Over 0.5 goals"}
    k_opts: dict = {}
    for o, ks in mr.kalshi_outcomes(km, game.ctx):
        k_opts.setdefault(o.twin_key(), {
            "market_type": o.market_type, "market_key": o.group, "side": o.side, "line": o.line,
            "kind": o.kind, "confidence": o.confidence, "outcome_label": o.label or o.side,
            "kalshi_ticker": km["ticker"], "kalshi_side": ks})

    nodes, unmatched = tb.join_game(k_opts, p_opts)
    gw = [n for n in nodes if n["market_type"] == "1h_total"]
    assert gw and all(n["market_key"] == "1h_total|0.5" for n in gw)
    # the game-wide node's Poly legs are the NO-TEAM tokens, NEVER the Senegal ones.
    assert all(n["poly_token_id"] in ("gw_o", "gw_u") for n in gw)
    assert next(n for n in gw if n["side"] == "over")["poly_token_id"] == "gw_o"
    # the single-team half-total never lands in a game-wide node; it is an unmatched 1h_team_total.
    assert not any(n["market_type"] == "1h_team_total" for n in nodes)
    assert any(u["venue"] == "polymarket" and u["market_type"] == "1h_team_total" for u in unmatched)


def test_poly_half_total_period_detected_from_question_not_just_grouptitle():
    """Robust period detection: when groupItemTitle is generic ('O/U 0.5') but the period is in the
    question, the market must still classify as 1h_total (NOT mis-paired to the full-game total)."""
    game = tb.Game(game_id="g", kalshi_suffix=SUF, date="2026-06-30", home="Norway",
                   away="Côte d'Ivoire", kickoff_iso="", poly_base_slug=BASE)
    ev = {"slug": f"{BASE}-more-markets", "closed": False, "markets": [
        {"groupItemTitle": "O/U 0.5", "question": "Will there be over 0.5 goals in the first half?",
         "outcomes": ["Over", "Under"], "clobTokenIds": ["o", "u"]}]}
    keys = {o.twin_key() for o, _, _ in tb._poly_event_outcomes(ev, "more", game)}
    assert "1h_total|0.5##over" in keys and "1h_total|0.5##under" in keys
    assert "total_goals|0.5##over" not in keys              # NOT mis-classified as full-game


def test_kickoff_enriched_from_poly_precise_start_time():
    """The stored kickoff_utc must be the PRECISE Polymarket startTime (time-of-day) — not the noon
    fallback and never the Kalshi expiration (which is AFTER the game and broke the started-game gate)."""
    base = {"id": BASE, "slug": BASE, "closed": False, "eventDate": "2026-06-30",
            "startTime": "2026-06-30T18:00:00Z", "markets": [
                _single("Norway", "n"), _single("Côte d'Ivoire", "c"),
                {"groupItemTitle": "Draw (Côte d'Ivoire vs. Norway)", "question": "draw?",
                 "outcomes": ["Yes", "No"], "clobTokenIds": ["d", "dn"]}]}

    class _PolyWithStart:
        def events_by_series(self, *a, **k):
            return [base]

    tree = tb.build_tree(_FakeKalshi(), _PolyWithStart(), GenzConfig(), now=NOW)
    assert tree["games"][SUF]["kickoff_utc"] == "2026-06-30T18:00:00Z"   # precise, not noon/expiration


def test_build_resilient_to_one_series_fetch_timeout():
    """A read-timeout on ONE Kalshi series must not abort the build: the other series/games still
    build, and the failure is recorded in coverage/meta for the dashboard."""
    class _FlakyKalshi(_FakeKalshi):
        def markets(self, *, series_ticker=None, event_ticker=None, status="open", limit=100, cursor=None):
            if event_ticker == f"KXWC2HSPREAD-{SUF}":
                raise TimeoutError("read timeout")               # NOT a KalshiError — must still be caught
            return super().markets(event_ticker=event_ticker, status=status)

    tree = tb.build_tree(_FlakyKalshi(), _FakePoly(), GenzConfig(), now=NOW)
    by = _by_type(tree)
    assert "total_goals" in by and "moneyline" in by and "corners" in by   # rest of the build survived
    cov = tree["games"][SUF]["coverage"]
    assert "KXWC2HSPREAD" in cov["kalshi_failed"] and cov["kalshi_ok"] > 0
    meta = tb.build_meta(tree, now=NOW)
    assert "KXWC2HSPREAD" in meta["kalshi_series_failed_any_game"]


def test_meta_and_round_trip(tmp_path):
    tree = _build()
    tp, mp = tmp_path / "match_tree.json", tmp_path / "tree_meta.json"
    tb.write_tree(tree, now=NOW, tree_path=str(tp), meta_path=str(mp))
    assert set(tb.load_tree(str(tp))["games"]) == {SUF}
    assert tb.build_meta(tree, now=NOW)["games"] == [SUF]


# --------------------------------------------------------------------------- #
# Kalshi<->Polymarket 3-letter CODE MISMATCH: Kalshi uses FIFA codes (POR, SUI), #
# Polymarket uses ISO 3166-1 alpha-3 (PRT, CHE). The Poly slug must still resolve #
# the game's event (via the table OR the series scan), never silently drop it.    #
# --------------------------------------------------------------------------- #
_POR_NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)   # POR-ESP kicks off 2026-07-06
_POR_SUF = "26JUL06PORESP"                                       # Kalshi: POR (FIFA)
_PRT_BASE = "fifwc-prt-esp-2026-07-06"                           # Polymarket: PRT (ISO)


class _RecLog:
    """Records WARNING lines so a test can assert the fallback logged (not silent)."""
    def __init__(self):
        self.warnings = []

    def warning(self, fmt, *a):
        self.warnings.append(fmt % a if a else fmt)

    def info(self, *a, **k):
        pass


def _kgame(sub):
    et = f"KXWCGAME-{_POR_SUF}"
    return {"event_ticker": et, "ticker": f"{et}-{abs(hash(sub)) % 999}", "yes_sub_title": sub,
            "title": sub, "status": "active"}


_POR_GAME = [_kgame("Portugal"), _kgame("Tie"), _kgame("Spain")]
_POR_KALSHI = {
    f"KXWCGAME-{_POR_SUF}": _POR_GAME,
    f"KXWCTCORNERS-{_POR_SUF}": [{"event_ticker": f"KXWCTCORNERS-{_POR_SUF}",
                                  "ticker": f"KXWCTCORNERS-{_POR_SUF}-c", "yes_sub_title": "9+ corners",
                                  "title": "9+ corners", "status": "active"}],
}


class _PorKalshi:
    def iter_markets(self, *, series_ticker=None, status="open", limit=100, max_pages=50):
        return list(_POR_GAME) if series_ticker == "KXWCGAME" else []

    def markets(self, *, series_ticker=None, event_ticker=None, status="open", limit=100, cursor=None):
        return {"markets": list(_POR_KALSHI.get(event_ticker, []))}


def _prt_series():
    """Polymarket events for the game under the ISO slug (PRT), base 1x2 (names Portugal/Spain) +
    the total-corners sibling."""
    base = _ev(_PRT_BASE, [_single("Portugal", "p"), _single("Spain", "s"),
                           {"groupItemTitle": "Draw (Portugal vs. Spain)", "question": "draw?",
                            "outcomes": ["Yes", "No"], "clobTokenIds": ["d", "dn"]}])
    corners = _ev(f"{_PRT_BASE}-total-corners", [_ou("O/U 8.5", "co", "cu")])
    return [base, corners]


class _PrtPoly:
    def events_by_series(self, *a, **k):
        return _prt_series()


def test_poly_slug_built_from_translated_iso_code():
    """The primary Poly slug uses the ISO code (Kalshi POR -> Poly PRT), keeping the raw code as an
    alternate — so a known code mismatch resolves on the primary path."""
    games = tb.discover_games(_PorKalshi(), now=_POR_NOW, lookahead_hours=48)
    g = next(x for x in games if x.game_id == _POR_SUF)
    assert g.poly_base_slug == _PRT_BASE                        # fifwc-prt-esp-... (translated), not -por-
    assert any("-por-" in s for s in g.poly_slug_alts)         # raw FIFA code kept as a fallback alt


def test_code_mismatch_game_builds_corners_not_empty():
    """POR/PRT (and SUI/CHE, same class) builds real nodes (moneyline + corners) — NOT silently dropped."""
    tree = tb.build_tree(_PorKalshi(), _PrtPoly(), GenzConfig(), now=_POR_NOW)
    g = tree["games"][_POR_SUF]
    types = {n["market_type"] for n in g["nodes"]}
    assert "corners" in types and "moneyline" in types
    assert len(g["nodes"]) >= 4


def test_resolve_poly_slug_fallback_via_event_names_when_code_unlisted(monkeypatch):
    """Robustness (point 2): even a code mismatch NOT in the translation table resolves via
    Polymarket's OWN team names (series scan), with a WARNING logged so it is never silent."""
    monkeypatch.delitem(tb._KALSHI_TO_POLY_CODE, "por", raising=False)   # simulate an UNLISTED mismatch
    monkeypatch.setattr(tb, "_POLY_TO_KALSHI_CODE", {v: k for k, v in tb._KALSHI_TO_POLY_CODE.items()})
    game = tb.Game(game_id=_POR_SUF, kalshi_suffix=_POR_SUF, date="2026-07-06", home="Spain",
                   away="Portugal", kickoff_iso="", poly_base_slug="fifwc-por-esp-2026-07-06",
                   poly_slug_alts=["fifwc-por-esp-2026-07-06", "fifwc-esp-por-2026-07-06"])
    series = _prt_series()
    assert tb.game_sibling_events(series, game) == []          # primary slug misses (POR not translated)
    rec = _RecLog()
    assert tb.resolve_poly_slug(game, series, rec) is True      # fallback resolves it...
    assert game.poly_base_slug == _PRT_BASE                     # ...to the correct ISO slug
    assert tb.game_sibling_events(series, game)                 # which now finds the events
    assert any("resolved via" in w for w in rec.warnings)      # and logged a WARNING (not silent)


def test_no_poly_event_logs_warning_not_silent():
    """A game with genuinely no Poly event stays one-sided AND logs a WARNING (visible, not silent)."""
    game = tb.Game(game_id="26JUL06XXXYYY", kalshi_suffix="26JUL06XXXYYY", date="2026-07-06",
                   home="Y", away="X", kickoff_iso="", poly_base_slug="fifwc-xxx-yyy-2026-07-06",
                   poly_slug_alts=["fifwc-xxx-yyy-2026-07-06"])
    rec = _RecLog()
    assert tb.resolve_poly_slug(game, _prt_series(), rec) is False
    assert any("NO Poly event matched" in w for w in rec.warnings)


# --------------------------------------------------------------------------- #
# SETTLEMENT-PERIOD MISMATCH: Kalshi settles count markets on the FULL game       #
# (incl. extra time); Poly on 90'+stoppage only. In a knockout both legs can lose #
# — such a pair must NOT be emitted as an arb (settlement_period_mismatch).        #
# --------------------------------------------------------------------------- #
def _corners_opts(kalshi_period, poly_period, line=8.5):
    key_o, key_u = f"corners|{line}##over", f"corners|{line}##under"
    k = {key_o: {"market_type": "corners", "market_key": f"corners|{line}", "side": "over", "line": line,
                 "kind": "2way", "confidence": "high", "outcome_label": "over", "kalshi_ticker": "KT",
                 "kalshi_side": "YES", "settle_period": kalshi_period},
         key_u: {"market_type": "corners", "market_key": f"corners|{line}", "side": "under", "line": line,
                 "kind": "2way", "confidence": "high", "outcome_label": "under", "kalshi_ticker": "KT",
                 "kalshi_side": "NO", "settle_period": kalshi_period}}
    p = {key_o: {"market_type": "corners", "market_key": f"corners|{line}", "side": "over", "line": line,
                 "kind": "2way", "confidence": "high", "outcome_label": "over", "poly_token_id": "po",
                 "poly_side": "Over", "settle_period": poly_period},
         key_u: {"market_type": "corners", "market_key": f"corners|{line}", "side": "under", "line": line,
                 "kind": "2way", "confidence": "high", "outcome_label": "under", "poly_token_id": "pu",
                 "poly_side": "Under", "settle_period": poly_period}}
    return k, p


def test_corners_period_mismatch_not_paired():
    """Kalshi 'incl. extra time' (full_game) vs Poly '90 min only' (regulation) is NOT the same bet:
    it is NOT paired; both sides go to unmatched with reason settlement_period_mismatch + a WARNING."""
    k, p = _corners_opts("full_game", "regulation")
    rec = _RecLog()
    nodes, unmatched = tb.join_game(k, p, log=rec, game_id="PORESP")
    assert nodes == []                                       # NOT paired — the trap is stopped
    assert unmatched and {u["reason"] for u in unmatched} == {"settlement_period_mismatch"}
    assert any("settlement_period_mismatch" in w for w in rec.warnings)


def test_corners_same_period_is_paired():
    """When both venues settle the SAME period, the corners pair IS eligible (nodes carry the period)."""
    k, p = _corners_opts("full_game", "full_game")
    nodes, unmatched = tb.join_game(k, p)
    assert {n["side"] for n in nodes} == {"over", "under"}
    assert all(n["kalshi_period"] == n["poly_period"] == "full_game" for n in nodes)
    assert not any(u.get("reason") == "settlement_period_mismatch" for u in unmatched)


def test_unknown_period_still_pairs_backward_compatible():
    """If a description is missing (period unknown on one side) we can't PROVE a mismatch, so we still
    pair — the guard only fires on a KNOWN disagreement (keeps period-less fixtures working)."""
    k, p = _corners_opts(None, "regulation")
    nodes, _ = tb.join_game(k, p)
    assert len(nodes) == 2


def test_settle_period_parsed_and_attached_end_to_end():
    """kalshi_options / poly_options attach the parsed period; a full-vs-90min corners pair drops out
    of build_tree (period_mismatch_dropped recorded)."""
    kfull = "Corners over the entire game including any extra time periods."
    preg = "Only corners in the first 90 minutes plus stoppage; extra time does not count."

    def _kc():
        et = f"KXWCTCORNERS-{_POR_SUF}"
        return {"event_ticker": et, "ticker": f"{et}-c", "yes_sub_title": "9+ corners", "title": "9+ corners",
                "status": "active", "rules_primary": kfull}
    kalshi_by_event = {f"KXWCGAME-{_POR_SUF}": _POR_GAME, f"KXWCTCORNERS-{_POR_SUF}": [_kc()]}

    class _K:
        def iter_markets(self, *, series_ticker=None, **k):
            return list(_POR_GAME) if series_ticker == "KXWCGAME" else []

        def markets(self, *, event_ticker=None, **k):
            return {"markets": list(kalshi_by_event.get(event_ticker, []))}

    base = _ev(_PRT_BASE, [_single("Portugal", "p"), _single("Spain", "s"),
                           {"groupItemTitle": "Draw (Portugal vs. Spain)", "question": "draw?",
                            "outcomes": ["Yes", "No"], "clobTokenIds": ["d", "dn"]}])
    corners = {"id": f"{_PRT_BASE}-total-corners", "slug": f"{_PRT_BASE}-total-corners", "closed": False,
               "markets": [{"groupItemTitle": "O/U 8.5", "outcomes": ["Over", "Under"],
                            "clobTokenIds": ["co", "cu"], "description": preg}]}

    class _P:
        def events_by_series(self, *a, **k):
            return [base, corners]

    tree = tb.build_tree(_K(), _P(), GenzConfig(), now=_POR_NOW)
    g = tree["games"][_POR_SUF]
    assert not any(n["market_type"] == "corners" for n in g["nodes"])        # period mismatch -> not paired
    assert g["coverage"]["period_mismatch_dropped"] >= 1
    assert tb.build_meta(tree, now=_POR_NOW)["period_mismatch_dropped_total"] >= 1


# --------------------------------------------------------------------------- #
# SYSTEMIC PAIRING ALARM — the 39/39 silent-zeros guard                          #
# --------------------------------------------------------------------------- #
def _tree(paired, total, sport="tennis", competition=None, poly_absent=False):
    games = {}
    for i in range(total):
        nodes = [{"twin_key": "match_winner|x"}] if i < paired else []
        g = {"away": f"Player {i}", "home": "Torres: Round Of", "nodes": nodes, "sport": sport}
        if competition:
            g["competition"] = competition
        if poly_absent and not nodes:
            g["coverage"] = {"poly_absent": True}
        games[f"G{i}"] = g
    return {"games": games}


def test_systemic_alert_fires_below_20pct_and_meta_carries_it():
    tree = _tree(paired=0, total=35, sport="tennis")           # the real 0/35 case (no competition -> 'all')
    alert = pairing_alert(tree, "tennis")
    assert alert and not alert["one_sided"] and len(alert["broken"]) == 1
    b = alert["broken"][0]
    assert b["competition"] == "all" and b["paired"] == 0 and b["total"] == 35 and b["share"] == 0.0
    assert b["sample_unmatched_tokens"] and "round" in b["sample_unmatched_tokens"][0]["tokens"]
    meta = tb.build_meta(tree, now=NOW, sport="tennis")
    assert meta["systemic_alert"]["broken"][0]["total"] == 35


def test_systemic_alert_silent_when_healthy_or_too_few_games():
    assert pairing_alert(_tree(paired=10, total=35), "tennis") is None      # 28% paired -> healthy
    assert pairing_alert(_tree(paired=0, total=4), "tennis") is None        # < 5 games -> no alarm (small slate)
    assert "systemic_alert" not in tb.build_meta(_tree(paired=30, total=35), now=NOW, sport="tennis")


def test_pairing_broken_requires_both_share_and_paired_count():
    # 18/92 = 19.6% share (< 20%) BUT 18 paired (>= 5) -> NOT broken (the AND gate; the 92/18 false alarm).
    assert pairing_alert(_tree(paired=18, total=92), "soccer") is None
    # 3/40 = 7.5% share AND only 3 paired -> genuinely broken (both gates fail).
    a = pairing_alert(_tree(paired=3, total=40), "soccer")
    assert a and len(a["broken"]) == 1 and a["broken"][0]["paired"] == 3 and not a["one_sided"]


def test_soccer_one_sided_competition_is_grey_note_not_broken():
    # Friendlies: 0/55 paired but ALL unpaired are poly_absent (Kalshi-only) -> grey one-sided, NOT red.
    tree = _tree(paired=0, total=55, sport="soccer", competition="club_friendlies", poly_absent=True)
    a = pairing_alert(tree, "soccer")
    assert a and not a["broken"] and len(a["one_sided"]) == 1
    assert a["one_sided"][0] == {"competition": "club_friendlies", "count": 55, "venue": "Kalshi"}


def test_soccer_per_competition_isolates_broken_from_healthy():
    # A mixed soccer tree: MLS healthy (13/15), friendlies one-sided (0/55 poly_absent), a real break (2/20).
    games = {}
    games.update(_tree(13, 15, "soccer", competition="mls")["games"])
    fr = _tree(0, 55, "soccer", competition="club_friendlies", poly_absent=True)["games"]
    games.update({f"F{k}": v for k, v in fr.items()})
    br = _tree(2, 20, "soccer", competition="ucl")["games"]            # genuine failure (not poly_absent)
    games.update({f"U{k}": v for k, v in br.items()})
    a = pairing_alert({"games": games}, "soccer")
    comps_broken = {b["competition"] for b in a["broken"]}
    comps_one = {o["competition"] for o in a["one_sided"]}
    assert comps_broken == {"ucl"} and comps_one == {"club_friendlies"}    # mls healthy -> neither
