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
