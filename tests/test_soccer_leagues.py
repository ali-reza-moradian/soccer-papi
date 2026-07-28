"""SOCCER LEAGUE EXPANSION (2026-07-28) — evidence-first, so every claim here is pinned to a payload
captured live that day, never to an assumption.

The sweep enumerated the whole Kalshi series catalog (1,466 soccer-tagged series, 95 of them *GAME),
found 26 with games inside the window, and asked Polymarket for each one's real fixtures. Nineteen
leagues came back live on BOTH venues; four are genuinely Kalshi-only. What is pinned below:

  1. KEYWORD -> SERIES is UNAMBIGUOUS. Every _COMP_SERIES_KEYWORDS entry must resolve to EXACTLY ONE
     *GAME ticker across the whole committed catalog. A Kalshi title change, or a NEW series that
     makes an existing keyword ambiguous, now fails the suite instead of silently re-pointing a
     competition at the wrong league (the 'champions league' -> AFC/Women's variants trap).
  2. TOTALS come from the GAME ticker STEM, confirmed against the catalog — never from the keyword
     scan, because Kalshi does not repeat the competition name in a totals title ('Champions League
     Total Goals' has no 'uefa'). And a stem-derived TOTAL must be the FULL-GAME one.
  3. THE PER-TEAM SEARCH FALLBACK. Poly's /public-search is not an AND over the words we send: for
     many real fixtures the combined '<away> <home>' query returns nothing while either team name
     alone returns the exact event. Measured live, the retry recovered 6 of 8 such games — including
     leagues that looked absent entirely (Czech: 'Sparta Prague Zlin' -> 0 hits, 'Zlin' ->
     cze1-asp-fcz-2026-07-31). The widened search must NOT widen ACCEPTANCE.
  4. PREFIX DISJOINTNESS. The Conference League is 'col' and Colombia's DIMAYOR is 'col1'; they must
     never claim each other's events.
"""
from __future__ import annotations

import json
import os

import pytest
import yaml

from src.genz import tree_builder as tb
from src.genz.config import Competition, load_genz_config

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


@pytest.fixture(scope="module")
def catalog():
    with open(os.path.join(FIX, "kalshi_series_catalog_soccer.json"), encoding="utf-8") as fh:
        return json.load(fh)["series"]


class _Catalog:
    """A Kalshi client that only knows how to list the captured series catalog."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_series(self, category=None):
        self.calls += 1
        return self.rows


# Every league confirmed live on BOTH venues, with the Kalshi series the keywords must resolve to and
# the Polymarket slug prefix READ OFF a real event that day (the example slug is in config.yaml).
BOTH_VENUES = {
    "mls":                    ("KXMLSGAME", "mls"),
    "club_friendlies":        ("KXCLUBFGAME", "clf"),
    "ucl_qualifying":         ("KXUCLGAME", "ucl"),
    "uefa_europa_league":     ("KXUELGAME", "uel"),
    "uefa_conference_league": ("KXUECLGAME", "col"),
    "argentina_primera":      ("KXARGPREMDIVGAME", "arg"),
    "brasileirao":            ("KXBRASILEIROGAME", "bra"),
    "brasileirao_b":          ("KXBRASILEIROBGAME", "bra2"),
    "conmebol_sudamericana":  ("KXCONMEBOLSUDGAME", "sud"),
    "liga_mx":                ("KXLIGAMXGAME", "mex"),
    "nwsl":                   ("KXNWSLGAME", "nwsl"),
    "ekstraklasa":            ("KXEKSTRAKLASAGAME", "pol"),
    "eliteserien":            ("KXELITESERIENGAME", "nor"),
    "usl_championship":       ("KXUSLGAME", "uslc"),
    "chinese_super_league":   ("KXCHNSLGAME", "chi"),
    "liga_dimayor":           ("KXDIMAYORGAME", "col1"),
    "croatia_hnl":            ("KXHNLGAME", "hr1"),
    "peru_liga_1":            ("KXPERLIGA1GAME", "per1"),
    "scottish_premiership":   ("KXSCOTTISHPREMGAME", "scop"),
    "uruguay_primera":        ("KXURYPDGAME", "uru1"),
    "czech_first_league":     ("KXCZEFLGAME", "cze1"),
    "bolivia_primera":        ("KXBOLPDIVGAME", "bol1"),
}

# Live on Kalshi, checked and genuinely absent on Polymarket -> deliberately NOT configured.
KALSHI_ONLY = ["KXAPFDDHGAME", "KXASEANGAME", "KXLIGAEXPGAME", "KXCANPLGAME"]


# --------------------------------------------------------------------------- #
# 1. Keyword -> exactly one series, against the whole captured catalog          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,expect", [(n, s) for n, (s, _) in BOTH_VENUES.items()])
def test_keyword_resolves_to_exactly_one_game_series(catalog, name, expect):
    comp = Competition(name=name, kalshi_series="AUTO", poly_slug_prefix="x", enabled=True)
    assert tb._scan_soccer_series(_Catalog(catalog), comp, None, ends="GAME") == [expect]


def test_no_keyword_is_ambiguous_across_the_whole_catalog(catalog):
    """One assertion for the property that matters: no keyword set may claim two GAME series."""
    ambiguous = {}
    for name in tb._COMP_SERIES_KEYWORDS:
        comp = Competition(name=name, kalshi_series="AUTO", poly_slug_prefix="x", enabled=True)
        hits = tb._scan_soccer_series(_Catalog(catalog), comp, None, ends="GAME")
        if len(hits) != 1:
            ambiguous[name] = hits
    assert ambiguous == {}


def test_every_enabled_competition_can_resolve_its_series(catalog):
    """Config and code must not drift apart: an enabled AUTO competition with no keyword entry would
    fall back to its own name as the keyword and quietly find nothing."""
    for comp in load_genz_config().competitions:
        if not comp.enabled or comp.kalshi_series != "AUTO":
            continue
        assert comp.name in tb._COMP_SERIES_KEYWORDS, f"{comp.name} has no keyword entry"
        assert len(tb._scan_soccer_series(_Catalog(catalog), comp, None, ends="GAME")) == 1


def test_kalshi_only_leagues_are_not_configured():
    """They were checked and Polymarket does not carry them; listing them would only burn requests."""
    configured = {c.name for c in load_genz_config().competitions}
    for name in ("paraguay_apf", "asean", "liga_expansion", "canadian_premier"):
        assert name not in configured


def test_kalshi_only_leagues_are_real_series_in_the_catalog(catalog):
    """The honest-zero note names four series — they must actually exist, so the note stays checkable."""
    tickers = {r["ticker"] for r in catalog}
    for t in KALSHI_ONLY:
        assert t in tickers


# --------------------------------------------------------------------------- #
# 2. Totals: stem-derived, full-game only                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,game", [(n, s) for n, (s, _) in BOTH_VENUES.items()
                                       if n != "bolivia_primera"])
def test_full_game_total_series_is_derived_from_the_game_stem(catalog, name, game):
    comp = Competition(name=name, kalshi_series="AUTO", poly_slug_prefix="x", enabled=True)
    client = _Catalog(catalog)
    scanned = tb._scan_soccer_series(client, comp, None, ends="TOTAL")
    total = tb._total_series_for(client, [game], scanned, None)
    assert game[:-4] + "TOTAL" in total


def test_bolivia_has_no_total_series_and_reports_an_honest_empty(catalog):
    """Kalshi lists KXBOLPDIVGAME but no KXBOLPDIVTOTAL. That must be [] — never an invented ticker."""
    comp = Competition(name="bolivia_primera", kalshi_series="AUTO", poly_slug_prefix="bol1", enabled=True)
    client = _Catalog(catalog)
    scanned = tb._scan_soccer_series(client, comp, None, ends="TOTAL")
    assert tb._total_series_for(client, ["KXBOLPDIVGAME"], scanned, None) == []
    assert "KXBOLPDIVTOTAL" not in {r["ticker"] for r in catalog}


def test_team_and_half_totals_are_a_different_family_not_a_game_total():
    """KXMLSTEAMTOTAL / KXMLS1HTOTAL do come back for the short 'mls' keyword. That is SAFE only
    because they route to their own market types — if they ever collapsed onto 'total_goals' they
    would pair a half/team total against Poly's full-game Over/Under (the corners-class trap)."""
    from src.genz import match_rules as mr
    ctx = mr.GameCtx(home="Toronto", away="New York City")
    game = {"event_ticker": "KXMLSTOTAL-26JUL31NYCTOR", "yes_sub_title": "Over 2.5 goals scored"}
    team = {"event_ticker": "KXMLSTEAMTOTAL-26JUL31NYCTOR", "yes_sub_title": "Toronto over 2.5 goals"}
    half = {"event_ticker": "KXMLS1HTOTAL-26JUL31NYCTOR", "yes_sub_title": "Over 2.5 1H goals scored"}
    keys = {n: {o.twin_key() for o, _ in mr.kalshi_outcomes(m, ctx)}
            for n, m in (("game", game), ("team", team), ("half", half))}
    assert keys["game"] & keys["team"] == set()
    assert keys["game"] & keys["half"] == set()
    assert any(k.startswith("total_goals|2.5") for k in keys["game"])


# --------------------------------------------------------------------------- #
# 3. Config carries the VERIFIED prefixes                                       #
# --------------------------------------------------------------------------- #
def test_config_prefixes_match_the_captured_slugs():
    got = {c.name: c.poly_slug_prefix for c in load_genz_config().competitions if c.enabled}
    assert got == {n: p for n, (_, p) in BOTH_VENUES.items()}


def test_conference_league_and_colombia_prefixes_stay_disjoint():
    """'col' (Conference League) vs 'col1' (Colombia DIMAYOR) — the startswith(prefix + '-') rule is
    what keeps them apart, so pin it against the two real slugs."""
    colombia, conference = "col1-cab-llf-2026-07-30", "col-al-dil-2026-07-28"
    assert not colombia.startswith("col" + "-")
    assert not conference.startswith("col1" + "-")
    assert conference.startswith("col" + "-") and colombia.startswith("col1" + "-")


def test_world_cup_entry_stays_disabled_so_the_legacy_golden_is_untouched():
    wc = [c for c in load_genz_config().competitions if c.name == "world_cup"]
    assert wc and wc[0].enabled is False and wc[0].kalshi_series == ["KXWCGAME"]


def test_config_yaml_documents_a_real_example_slug_for_every_new_league():
    """Each added line carries the live slug that fixed its prefix — evidence, in the file itself."""
    with open(CONFIG, encoding="utf-8") as fh:
        text = fh.read()
    for slug in ("uel-csk1-qar-2026-07-30", "col-al-dil-2026-07-28", "arg-ros-rac-2026-07-28",
                 "bra-flu-bah-2026-07-29", "bra2-fec-bot-2026-07-28", "sud-tig-nac-2026-07-28",
                 "mex-jua-pum-2026-07-31", "nwsl-bay-got-2026-07-29", "pol-mot-jag-2026-07-31",
                 "nor-bog-lil-2026-07-31", "uslc-lc-bir-2026-07-29", "chi-hen-ygb-2026-07-31",
                 "col1-cab-llf-2026-07-30", "hr1-din-sla-2026-08-01", "per1-clc-ccu-2026-07-31",
                 "scop-dun-ran-2026-07-31", "uru1-tor-wan-2026-07-31", "cze1-asp-fcz-2026-07-31",
                 "bol1-tom-abb-2026-06-20"):
        assert slug in text


# --------------------------------------------------------------------------- #
# 4. The per-team search fallback                                               #
# --------------------------------------------------------------------------- #
def test_query_order_is_combined_then_each_team():
    assert tb.poly_search_queries("Sparta Prague", "Zlin") == ["Sparta Prague Zlin", "Sparta Prague", "Zlin"]


def test_query_list_is_deduped_and_skips_blanks():
    assert tb.poly_search_queries("Zlin", "") == ["Zlin"]
    assert tb.poly_search_queries("", "") == []


class _Poly:
    """A /public-search stub: exact query -> the events it returns. Records the call order."""

    def __init__(self, by_query):
        self.by_query = by_query
        self.queries = []

    def search(self, q):
        self.queries.append(q)
        return {"events": self.by_query.get(q, [])}


def _game(away, home, date, prefix, gid="G"):
    return tb.Game(game_id=gid, kalshi_suffix=gid, date=date, home=home, away=away,
                   kickoff_iso=f"{date}T12:00:00Z", poly_base_slug="", competition="c",
                   poly_prefix=prefix)


# The real 2026-07-28 payload: the combined query returned nothing, 'Zlin' alone returned the event.
CZECH_EVENT = {"slug": "cze1-asp-fcz-2026-07-31", "title": "AC Sparta Praha vs. FC Zlín"}


def test_per_team_fallback_recovers_the_czech_game(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "_load_team_aliases", lambda: {})
    monkeypatch.setattr(tb, "_save_team_aliases", lambda a: None)
    poly = _Poly({"Sparta Prague Zlin": [], "Sparta Prague": [], "Zlin": [CZECH_EVENT]})
    game = _game("Sparta Prague", "Zlin", "2026-07-31", "cze1")
    ev = tb._resolve_competition_poly(game, poly, None)
    assert ev is CZECH_EVENT
    assert game.poly_base_slug == "cze1-asp-fcz-2026-07-31"
    assert poly.queries == ["Sparta Prague Zlin", "Sparta Prague", "Zlin"]


def test_combined_hit_short_circuits_to_exactly_one_request(monkeypatch):
    """A game the combined query already resolves must behave EXACTLY as before — one search, no
    extra load. This is what keeps the 94-game friendly slate from tripling its request count."""
    monkeypatch.setattr(tb, "_load_team_aliases", lambda: {})
    monkeypatch.setattr(tb, "_save_team_aliases", lambda a: None)
    ev = {"slug": "clf-avl-rso-2026-07-28", "title": "Aston Villa vs. Real Sociedad San Sebastian"}
    poly = _Poly({"Aston Villa Real Sociedad": [ev]})
    game = _game("Aston Villa", "Real Sociedad", "2026-07-28", "clf")
    assert tb._resolve_competition_poly(game, poly, None) is ev
    assert poly.queries == ["Aston Villa Real Sociedad"]


def test_widened_search_does_not_widen_acceptance_wrong_league(monkeypatch):
    """The Canadian Premier League game's ONLY search hit was a WTA tennis doubles event on the same
    date. A wider search must still reject it on the slug prefix."""
    monkeypatch.setattr(tb, "_load_team_aliases", lambda: {})
    monkeypatch.setattr(tb, "_save_team_aliases", lambda a: None)
    noise = {"slug": "wta-doubles-ayukbar-cavasal-2026-07-28",
             "title": "Targu Mures (Doubles): Ayukawa/Barry vs Cavalle-Reimers/Salden"}
    poly = _Poly({q: [noise] for q in ("Cavalry FC Supra Du Quebec", "Cavalry", "FC Supra Du Quebec")})
    game = _game("Cavalry", "FC Supra Du Quebec", "2026-07-28", "cpl")
    assert tb._resolve_competition_poly(game, poly, None) is None


def test_widened_search_does_not_widen_acceptance_wrong_date(monkeypatch):
    """Bolivia's clubs ARE on Poly, but only on dates outside the window. Right league, wrong date
    must stay unresolved rather than pair a different fixture."""
    monkeypatch.setattr(tb, "_load_team_aliases", lambda: {})
    monkeypatch.setattr(tb, "_save_team_aliases", lambda a: None)
    stale = {"slug": "bol1-tom-abb-2026-06-20", "title": "CD Real Tomayapo vs. ABB"}
    poly = _Poly({q: [stale] for q in ("CD Real Tomayapo Academia del Balompie Boliviano",
                                       "CD Real Tomayapo", "Academia del Balompie Boliviano")})
    game = _game("CD Real Tomayapo", "Academia del Balompie Boliviano", "2026-07-31", "bol1")
    assert tb._resolve_competition_poly(game, poly, None) is None


def test_widened_search_does_not_widen_acceptance_wrong_teams(monkeypatch):
    """Same league, same date, DIFFERENT fixture -> rejected on names, not silently paired."""
    monkeypatch.setattr(tb, "_load_team_aliases", lambda: {})
    monkeypatch.setattr(tb, "_save_team_aliases", lambda a: None)
    other = {"slug": "bra-int-fla-2026-07-29", "title": "SC Internacional vs. CR Flamengo"}
    poly = _Poly({q: [other] for q in ("Fluminense Bahia", "Fluminense", "Bahia")})
    game = _game("Fluminense", "Bahia", "2026-07-29", "bra")
    assert tb._resolve_competition_poly(game, poly, None) is None


def test_a_failing_query_does_not_abort_the_remaining_ones(monkeypatch):
    """One search raising must not cost us the fallback that would have matched."""
    monkeypatch.setattr(tb, "_load_team_aliases", lambda: {})
    monkeypatch.setattr(tb, "_save_team_aliases", lambda a: None)

    class _Flaky(_Poly):
        def search(self, q):
            self.queries.append(q)
            if q == "Sparta Prague Zlin":
                raise RuntimeError("gamma 502")
            return {"events": self.by_query.get(q, [])}

    poly = _Flaky({"Zlin": [CZECH_EVENT]})
    game = _game("Sparta Prague", "Zlin", "2026-07-31", "cze1")
    assert tb._resolve_competition_poly(game, poly, None) is CZECH_EVENT


def test_reverse_orientation_is_accepted(monkeypatch):
    """Venues disagree on home/away order; the fallback must keep trying both orientations."""
    monkeypatch.setattr(tb, "_load_team_aliases", lambda: {})
    monkeypatch.setattr(tb, "_save_team_aliases", lambda a: None)
    ev = {"slug": "uel-ben-stg-2026-07-30", "title": "FC St. Gallen vs. Sport Lisboa e Benfica"}
    poly = _Poly({"St. Gallen": [ev]})
    game = _game("SL Benfica", "St. Gallen", "2026-07-30", "uel")
    assert tb._resolve_competition_poly(game, poly, None) is ev


# --------------------------------------------------------------------------- #
# 5. Kalshi's non-WC title tail                                                 #
# --------------------------------------------------------------------------- #
def test_winner_tail_is_stripped_from_the_non_wc_title():
    """Kalshi titles non-WC game-winners '<A> vs <B> Winner?'. That trailing question belongs to the
    TITLE, not to team B — left on, it rides along and poisons the name match."""
    assert tb._soccer_names_from_title("Montevideo City vs Wanderers Winner?") == \
        ("Montevideo City", "Wanderers")
    assert tb._soccer_names_from_title(
        "CD Real Tomayapo vs Academia del Balompie Boliviano Winner?") == \
        ("CD Real Tomayapo", "Academia del Balompie Boliviano")


def test_world_cup_and_poly_title_forms_are_unaffected():
    """The tail strip must not disturb the two title shapes that already parsed."""
    assert tb._soccer_names_from_title("Will Thun win the Dinamo Zagreb vs Thun match?") == \
        ("Dinamo Zagreb", "Thun")
    assert tb._soccer_names_from_title("GNK Dinamo Zagreb vs. FC Thun") == \
        ("GNK Dinamo Zagreb", "FC Thun")


def test_generic_second_team_keeps_its_clean_yes_sub_title():
    """The real Uruguay payload: yes_sub_title is the clean 'Wanderers', but 'wanderers' is a
    club-GENERIC token, so it could not be matched back to the raw title fragment and the fragment
    won. Team B must come out clean."""
    markets = [{"title": "Montevideo City vs Wanderers Winner?", "yes_sub_title": "Wanderers"},
               {"title": "Montevideo City vs Wanderers Winner?", "yes_sub_title": "Montevideo City"},
               {"title": "Montevideo City vs Wanderers Winner?", "yes_sub_title": "Tie"}]
    assert tb._competition_teams(markets) == ("Montevideo City", "Wanderers")


# --------------------------------------------------------------------------- #
# 6. The committed per-league raw dumps — one real game per league, captured    #
#    through the SHIPPED resolver, so these assertions run against payloads     #
#    rather than against restated expectations.                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def dumps():
    def _load(side):
        with open(os.path.join(FIX, f"raw_soccer_leagues_{side}.json"), encoding="utf-8") as fh:
            return json.load(fh)["leagues"]
    return _load("kalshi"), _load("poly")


def test_every_dumped_league_is_a_configured_competition(dumps):
    k, p = dumps
    configured = {c.name for c in load_genz_config().competitions if c.enabled}
    assert set(k) == set(p)
    assert set(k) <= configured
    assert len(k) >= 15, "the expansion evidence should cover most of the added leagues"


def test_dumped_poly_slug_carries_the_configured_prefix(dumps):
    """The whole point of the sweep: each league's configured prefix is the one its REAL events use."""
    _, p = dumps
    cfg = {c.name: c.poly_slug_prefix for c in load_genz_config().competitions}
    for league, d in p.items():
        assert d["poly_slug_prefix"] == cfg[league]
        for ev in d["events"]:
            assert str(ev["slug"]).startswith(cfg[league] + "-"), (league, ev["slug"])


def test_dumped_kalshi_event_belongs_to_the_resolved_series(dumps, catalog):
    k, _ = dumps
    tickers = {r["ticker"] for r in catalog}
    for league, d in k.items():
        series = str(d["event_ticker"]).split("-")[0]
        assert series in tickers, (league, series)
        assert series == BOTH_VENUES[league][0]
        for s in d["series_scanned"]:
            assert s in tickers, (league, s)


def test_dumped_team_names_match_across_the_two_venues(dumps):
    """The normalizer is re-run against every captured league: the Kalshi yes_sub_title names and the
    Polymarket event title must resolve to the same two clubs, in one orientation or the other."""
    from src.genz import soccer_names as sn
    k, p = dumps
    for league, kd in k.items():
        away, home = kd["kalshi_teams"]
        title = p[league]["events"][0]["title"]
        names = tb._soccer_names_from_title(title)
        assert names, (league, title)
        pa, pb = names
        ok = ((sn.same_club_with_alias(away, pa) and sn.same_club_with_alias(home, pb))
              or (sn.same_club_with_alias(away, pb) and sn.same_club_with_alias(home, pa)))
        assert ok, f"{league}: {away!r}/{home!r} vs {title!r} — {sn.explain(away, pa)}"


def test_dumped_kalshi_markets_carry_a_three_way_winner(dumps):
    """Every league's game-winner event must be the SAME 3-way shape (home / away / Tie) the pairing
    rules assume — a league whose winner market were 2-way would need its own rule, not this one."""
    k, _ = dumps
    for league, d in k.items():
        winner = [m for m in d["markets"]
                  if str(m.get("event_ticker", "")).startswith(d["event_ticker"].split("-")[0] + "-")
                  and str(m.get("event_ticker")) == d["event_ticker"]]
        subs = {tb._clean_sub(m.get("yes_sub_title")).lower() for m in winner}
        assert len(winner) == 3, (league, len(winner))
        assert subs & {"tie", "draw"}, (league, subs)
