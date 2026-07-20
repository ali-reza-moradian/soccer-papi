"""Tests for the tennis sport adapter (src/genz/sports_tennis.py) — the THIRD GenZ sport.

Covers surname normalization + token-set matching, Poly slug construction (both orders, date ±1,
7-char truncation, compound/apostrophe names), the fallback scan with a decoy event, parse_tennis_rules
against the REAL captured texts (both facets), the match-winner pairing (single + two-market Kalshi
shapes, identity-before-shape excluding Set-N-Winner, inventory of unpaired families), and the walkover
guard (50-50 note vs started-divergence refuse vs unparseable conservative flag). Soccer + MLB goldens
stay byte-identical (they are locked in test_genz_sports.py)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from src.genz import sports_tennis as T
from src.genz.config import load_genz_config

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "raw", "tennis_titledrift_kalshi.json")


class _Log:
    def __init__(self):
        self.infos, self.warnings = [], []

    def info(self, fmt, *a):
        self.infos.append(fmt % a if a else fmt)

    def warning(self, fmt, *a):
        self.warnings.append(fmt % a if a else fmt)


# --------------------------------------------------------------------------- #
# Surname normalization + token-set matching                                     #
# --------------------------------------------------------------------------- #
def test_surname_and_slug_normalization():
    assert T.surname("Robin Söderling") == "soderling"
    assert T.surname_slug("Robin Söderling") == "soderling"                 # accents stripped
    assert T.surname_slug("Christopher O'Connell") == "o-connell"           # apostrophe -> hyphen
    assert T.surname_slug("Felix Auger-Aliassime") == "auger-aliassime"     # compound kept whole
    assert T.surname("Alex de Minaur") == "de minaur"                       # particle joined
    assert T.surname_slug("Alex de Minaur") == "de-minaur"
    assert T.surname("Juan Manuel Cerundolo") == "cerundolo"                # last token


def test_slug_fragment_variants_include_7char_truncation():
    # Poly truncates a slug fragment to 7 chars (live: 'Crawford' -> 'crawfor', 'Zheng' -> 'zhen').
    assert T._slug_frag_variants("Oliver Crawford") == ["crawford", "crawfor"]
    assert "auger-a" in T._slug_frag_variants("Felix Auger-Aliassime")      # compound + its truncation


def test_token_set_matching_both_directions():
    # containment BOTH ways: a dropped middle name still matches (the live Merida/Aguilar case).
    assert T._same_player("Daniel Merida", "Daniel Merida Aguilar") is True
    assert T._same_player("Qinwen Zheng", "Zheng Qinwen") is True           # order-independent
    assert T._same_player("Aleksandr Shevchenko", "Andrey Rublev") is False
    assert T._same_player("Söderling", "Robin Soderling") is True           # accent-insensitive


def test_names_from_title_and_ticker_fragment_resolved_by_title():
    # The 'win the X vs Y' clause fixes the A-vs-B ORDER (surnames); the ticker fragment is ignored.
    assert T._names_from_title("Will Panna Udvardy win the Udvardy vs Badosa: Quarterfinal match?") \
        == ("Udvardy", "Badosa")
    assert T._names_from_title("Liudmila Samsonova vs Hailey Baptiste") == ("Liudmila Samsonova", "Hailey Baptiste")
    assert T._decode_date("26FEB05SAMBAP") == "2026-02-05"
    # discovery then UPGRADES the surname pair to full names via the per-player yes_sub_titles.
    mkts = [{"yes_sub_title": "Panna Udvardy"}, {"yes_sub_title": "Paula Badosa"}]
    assert T._enrich_full_names(("Udvardy", "Badosa"), mkts) == ("Panna Udvardy", "Paula Badosa")


# --------------------------------------------------------------------------- #
# TITLE-DRIFT FIX — derived from the committed raw dump, not assumed              #
# --------------------------------------------------------------------------- #
def test_strip_stage_suffix_from_real_title():
    # The exact broken text captured live: ': Round Of 16' must be removed, names untouched.
    assert T.strip_stage_suffix("Tabilo vs Torres: Round Of 16") == "Tabilo vs Torres"
    assert T.strip_stage_suffix("Udvardy vs Badosa: Quarterfinal") == "Udvardy vs Badosa"
    assert T.strip_stage_suffix("A vs B - Semifinal") == "A vs B"
    assert T.strip_stage_suffix("A vs B Final") == "A vs B"
    assert T.strip_stage_suffix("Alejandro Tabilo") == "Alejandro Tabilo"       # no stage -> unchanged


def test_name_tokens_drop_stage_stopwords():
    # A title-drift leak must not poison the token set: 'round'/'of' are dropped, the surname survives.
    assert T.name_tokens("Torres: Round Of 16") == frozenset({"torres", "16"})
    assert T.name_tokens("Tiago Torres") == frozenset({"tiago", "torres"})


def test_player_names_yes_sub_title_primary_from_fixture():
    """The real captured event: yes_sub_title gives clean full names; the title (with ': Round Of 16')
    only fixes order. Before the fix player_b was 'Torres: Round Of'."""
    raw = json.load(open(_FIX, encoding="utf-8"))
    mkts = raw["markets"]
    names = T._player_names(mkts)
    assert names == ("Alejandro Tabilo", "Tiago Torres")                        # title order A=Tabilo, B=Torres
    # and the tokens are clean (no stage words) so Poly token-matching can pair.
    assert "round" not in (T.name_tokens(names[0]) | T.name_tokens(names[1]))


# --------------------------------------------------------------------------- #
# Slug construction: both orders, date rollover ±1, fallback scan w/ a decoy      #
# --------------------------------------------------------------------------- #
def _match(a="John Smith", b="Bob Jones", date="2026-07-17", tour="atp"):
    return T.TennisMatch(f"KX{tour.upper()}MATCH-26JUL17SMIJON", tour, date, a, b,
                         f"{date}T12:00:00Z", markets=[{"event_ticker": f"KX{tour.upper()}MATCH-26JUL17SMIJON"}])


def test_construct_slugs_both_orders_and_date_pm1():
    slugs = T.construct_slugs(_match())
    assert "atp-smith-jones-2026-07-17" in slugs and "atp-jones-smith-2026-07-17" in slugs   # both orders
    assert "atp-smith-jones-2026-07-16" in slugs and "atp-smith-jones-2026-07-18" in slugs   # date ±1


def test_fallback_scan_matches_by_tokens_and_ignores_decoy():
    """The scan matches on surname TOKEN SETS + date ±1, and IGNORES a decoy same-surname event on a
    different date. Start time is DISPLAY ONLY — never a refusal reason."""
    match = _match("Barbora Krejcikova", "Qinwen Zheng", "2026-07-17", "wta")
    series = [
        {"slug": "wta-krejcik-zhen-2026-01-01", "startTime": "2026-01-01T10:00:00Z",   # DECOY: wrong date
         "title": "Australian Open: Barbora Krejcikova vs Qinwen Zheng", "markets": []},
        {"slug": "wta-krejcik-zhen-2026-07-17", "startTime": "2026-07-17T16:30:00Z",   # the real one
         "title": "Athens Open: Barbora Krejcikova vs Qinwen Zheng",
         "markets": [{"slug": "wta-krejcik-zhen-2026-07-17", "groupItemTitle": None,
                      "outcomes": ["Barbora Krejcikova", "Qinwen Zheng"], "clobTokenIds": ["t_k", "t_z"],
                      "description": "advances against. walkover ... resolve to 50-50", "feesEnabled": True,
                      "feeSchedule": {"rate": 0.05}}]},
    ]

    class _P:                                             # no slug hits -> forces the scan
        def events_by_slug(self, slug):
            return []

    ev, method = T.resolve_poly_event(match, series, _P(), log=_Log())
    assert method == "scan" and ev["slug"] == "wta-krejcik-zhen-2026-07-17"   # decoy ignored, real matched


def test_slug_primary_hit_short_circuits_scan():
    match = _match("Damir Dzumhur", "Alex Molcan", "2026-07-17", "atp")
    target = {"slug": "atp-dzumhur-molcan-2026-07-17", "startTime": "2026-07-17T19:00:00Z",
              "title": "Croatia Open: Damir Dzumhur vs Alex Molcan", "markets": []}

    class _P:
        def events_by_slug(self, slug):
            return [target] if slug == "atp-dzumhur-molcan-2026-07-17" else []

    ev, method = T.resolve_poly_event(match, [], _P())
    assert method == "slug" and ev is target


# --------------------------------------------------------------------------- #
# parse_tennis_rules — REAL captured texts, both facets                          #
# --------------------------------------------------------------------------- #
# The live Poly match-winner description (Dzumhur/Molcan, verbatim shape).
_POLY_TEXT = ("This market will resolve to 'Damir Dzumhur' if Damir Dzumhur advances against Alex Molcan. "
              "If the match begins but is not completed, and one player advances due to the opponent's "
              "retirement, default, or disqualification, this market will resolve to the player who advances. "
              "If the match ends in a walkover (player withdraws before the start and the other advances "
              "automatically), this market will resolve to 50-50.")
# The live Kalshi rules (primary + secondary, verbatim shape).
_KALSHI_TEXT = ("If Damir Dzumhur wins the Dzumhur vs Molcan professional tennis match after a ball has "
                "been played, then the market resolves to Yes. If the match does not occur due to a player "
                "injury, walkover, forfeiture, or any other cancellation (all before the match starts), the "
                "market will resolve to a fair price in accordance with the rules.")


def test_parse_tennis_rules_poly_started_and_walkover():
    r = T.parse_tennis_rules(_POLY_TEXT)
    assert r["started_rule"] == "advancing_player" and r["walkover_rule"] == "fifty_fifty_walkover"


def test_parse_tennis_rules_kalshi_last_price_walkover():
    r = T.parse_tennis_rules(_KALSHI_TEXT)
    assert r["started_rule"] == "advancing_player" and r["walkover_rule"] == "last_price_walkover"


def test_parse_tennis_rules_unparseable():
    r = T.parse_tennis_rules("this text has no walkover or advancement language at all")
    assert r["started_rule"] is None and r["walkover_rule"] is None


# --------------------------------------------------------------------------- #
# Walkover guard: 50-50 note (KEEP) vs started divergence (REFUSE) vs unparseable  #
# --------------------------------------------------------------------------- #
def _node():
    return {"twin_key": "match_winner|dzumhur", "market_key": "match_winner"}


def test_walkover_guard_5050_note_is_informational_and_kept():
    node = _node()
    keep = T._apply_walkover_guard(node, _KALSHI_TEXT, _POLY_TEXT, _Log(), "G")
    assert keep is True
    assert node["settlement_note"] == "walkover_50_50" and "settlement_risk" not in node
    assert "advances against" in node["settlement_texts"]["poly"] or "advances" in node["settlement_texts"]["poly"]


def test_walkover_guard_started_divergence_refuses():
    # A venue that settles an INCOMPLETE STARTED match on last-price (not advancement) is a real divergence.
    kalshi_started_price = ("if the match is suspended and not resumed, the market resolves to the last "
                            "traded price. walkover before the match starts resolves to a fair price.")
    node = _node()
    log = _Log()
    keep = T._apply_walkover_guard(node, kalshi_started_price, _POLY_TEXT, log, "G")
    assert keep is False and any("started_rule divergence" in w for w in log.warnings)


def test_walkover_guard_unparseable_side_is_conservative_flag():
    node = _node()
    keep = T._apply_walkover_guard(node, "no rule language here", _POLY_TEXT, _Log(), "G")
    assert keep is True and node["settlement_risk"] == "tennis_unparsed_settlement"


# --------------------------------------------------------------------------- #
# Pairing — end-to-end (two-market Kalshi + single-market Kalshi), inventory      #
# --------------------------------------------------------------------------- #
TENNIS_NOW = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)   # within 72h of 26JUL17 matches
_SUF = "26JUL17DZUMOL"
_EVENT = f"KXATPMATCH-{_SUF}"


def _kalshi_mkt(code, player, other):
    return {"event_ticker": _EVENT, "ticker": f"{_EVENT}-{code}", "yes_sub_title": player,
            "no_sub_title": player, "status": "active",
            "title": f"Will {player} win the {player.split()[-1]} vs {other.split()[-1]}: Semifinal match?",
            "rules_primary": f"If {player} wins the match after a ball has been played, resolves to Yes.",
            "rules_secondary": ("If the match does not occur due to a walkover or any other cancellation "
                                "before the match starts, the market will resolve to a fair price.")}


def _poly_fee():
    return {"feesEnabled": True, "feeSchedule": {"rate": 0.05, "takerOnly": True, "rebateRate": 0.15}}


def _poly_winner_mkt(slug, a, b):
    d = {"slug": slug, "groupItemTitle": None, "question": f"Croatia Open: {a} vs {b}",
         "outcomes": [a, b], "clobTokenIds": ["tok_a", "tok_b"], "description": _POLY_TEXT}
    d.update(_poly_fee())
    return d


def _poly_setwinner_mkt(slug, a, b):
    # Set 1 Winner shares the two-NAME shape but is NOT the match winner (identity before shape).
    d = {"slug": slug + "-first-set-winner", "groupItemTitle": "Set 1 Winner",
         "question": f"Set 1 Winner: {a.split()[-1]} vs {b.split()[-1]}",
         "outcomes": [a.split()[-1], b.split()[-1]], "clobTokenIds": ["s_a", "s_b"], "description": "set winner"}
    d.update(_poly_fee())
    return d


class _TennisKalshi:
    def iter_markets(self, *, series_ticker=None, status="open", **kw):
        if series_ticker == "KXATPMATCH":
            return [_kalshi_mkt("DZU", "Damir Dzumhur", "Alex Molcan"),
                    _kalshi_mkt("MOL", "Alex Molcan", "Damir Dzumhur")]
        return []


_POLY_EVENT = {
    "slug": "atp-dzumhur-molcan-2026-07-17", "startTime": "2026-07-17T19:00:00Z",
    "title": "Croatia Open: Damir Dzumhur vs Alex Molcan",
    "markets": [
        _poly_winner_mkt("atp-dzumhur-molcan-2026-07-17", "Damir Dzumhur", "Alex Molcan"),
        _poly_setwinner_mkt("atp-dzumhur-molcan-2026-07-17", "Damir Dzumhur", "Alex Molcan"),
    ],
}


class _TennisPoly:
    def events_by_series(self, tour, *, closed=False, **kw):
        return [_POLY_EVENT] if tour == "atp" else []

    def events_by_slug(self, slug):
        return [_POLY_EVENT] if slug == "atp-dzumhur-molcan-2026-07-17" else []


def _build_tennis():
    from src.genz import tree_builder as tb
    cfg = load_genz_config(sport="tennis")
    return tb.build_tree(_TennisKalshi(), _TennisPoly(), cfg, now=TENNIS_NOW, spec=T.TENNIS_SPEC)


def test_tennis_build_pairs_match_winner_only():
    tree = _build_tennis()
    g = tree["games"][_EVENT]
    assert g["sport"] == "tennis" and g["tour"] == "atp"
    assert g["kickoff_utc"] == "2026-07-17T19:00:00Z"                    # Poly startTime
    by = {}
    for n in g["nodes"]:
        by.setdefault(n["market_type"], []).append(n)
    assert set(by) == {"match_winner"}                                   # ONLY match_winner paired
    keys = {n["twin_key"] for n in by["match_winner"]}
    assert keys == {"match_winner|dzumhur", "match_winner|molcan"}
    dzu = next(n for n in by["match_winner"] if n["side"] == "dzumhur")
    assert dzu["poly_token_id"] == "tok_a" and dzu["kalshi_side"] == "YES"
    assert dzu["poly_fee_rate"] == 0.05 and dzu["poly_fee_enabled"] is True   # live fee shape
    assert dzu["settlement_note"] == "walkover_50_50" and not dzu.get("settlement_risk")


def test_tennis_inventory_line_lists_unpaired_setwinner(capsys):
    log = _Log()
    from src.genz import tree_builder as tb
    tb.build_tree(_TennisKalshi(), _TennisPoly(), load_genz_config(sport="tennis"),
                  now=TENNIS_NOW, spec=T.TENNIS_SPEC, log=log)
    inv = [line for line in log.infos if "[TENNIS][INVENTORY]" in line]
    assert inv and "Set 1 Winner" in inv[0]                              # set-winner NOT paired, inventoried


def test_tennis_single_market_kalshi_shape_best_of_both():
    """A Kalshi event with a SINGLE market (yes=one player) still pairs both twin_keys against Poly's two
    outcome tokens — the Poly side supplies both surnames."""
    class _OneMarketKalshi:
        def iter_markets(self, *, series_ticker=None, status="open", **kw):
            return [_kalshi_mkt("DZU", "Damir Dzumhur", "Alex Molcan")] if series_ticker == "KXATPMATCH" else []

    from src.genz import tree_builder as tb
    tree = tb.build_tree(_OneMarketKalshi(), _TennisPoly(), load_genz_config(sport="tennis"),
                         now=TENNIS_NOW, spec=T.TENNIS_SPEC)
    g = tree["games"][_EVENT]
    # only the Dzumhur side has BOTH venues -> 1 paired node; Molcan is one-sided (poly-only) -> unmatched
    keys = {n["twin_key"] for n in g["nodes"]}
    assert "match_winner|dzumhur" in keys
    assert any(u["reason"] == "one_venue_only" for u in g["unmatched"])


def test_tennis_live_node_fee_is_005():
    """The Poly fee on a match-winner node is 0.05 (asserted from the built node — the live gamma shape
    carries feeSchedule.rate 0.05, takerOnly, rebateRate 0.15)."""
    g = _build_tennis()["games"][_EVENT]
    assert all(n["poly_fee_rate"] == 0.05 for n in g["nodes"])
