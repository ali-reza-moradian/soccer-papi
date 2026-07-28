"""Regression suite for the 2026-07-28 EUROPEAN SOCCER PAIRING failure (0/92 paired).

Every expectation below comes from payloads captured live that day (tests/fixtures/raw_soccer_*.json
and the build log), not from invented spellings. The failure had TWO independent causes:

  A. CONFIG — Polymarket's club-friendly slug prefix is 'clf' (clf-avl-rso-2026-07-28), not
     'club-friendly'. 94 of 95 Kalshi friendlies were rejected on the slug before any name was read.
  B. NAMES  — the matcher tokenised with an ASCII-only regex and demanded every token hit, so a
     diacritic SPLIT the token ('Víkingur' -> ['v','kingur']), a transliteration was a hard miss
     ('Kairat'/'Qairat'), and a sponsor or city present on one side only vetoed the pair.

A third trap is guarded here too: UEFA qualifying is TWO-LEGGED, and Polymarket carries a
'Team to Advance' market that settles on the aggregate across both legs. Pairing it against Kalshi's
single-leg winner is a guaranteed mis-settlement (a team can lose the leg and still advance).
"""
from __future__ import annotations

import json
import os

import pytest

from src.genz import match_rules as mr
from src.genz import soccer_names as sn

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# 1. The exact live examples from the report — every one must match             #
# --------------------------------------------------------------------------- #
LIVE_PAIRS = [
    # (kalshi spelling, polymarket spelling, what differs)
    ("Dinamo Zagreb", "GNK Dinamo Zagreb", "legal-form prefix"),
    ("Sturm Graz", "SK Puntigamer Sturm Graz", "legal form + SPONSOR token"),
    ("Kairat", "Qairat FK", "transliteration K/Q"),
    ("Omonia Nicosia", "AS Omónoia Leukosías", "diacritics + exonym city"),
    ("Lech Poznan", "KKS Lech Poznań", "legal form + diacritic"),
    ("Egnatia Rrogozhine", "KF Egnatia Rrogozhinë", "legal form + diacritic"),
    ("Real Sociedad", "Real Sociedad San Sebastian", "city suffix"),
    ("Vikingur Reykjavik", "KF Víkingur", "diacritic + dropped city"),
    ("Be`er Sheva", "MH Hapoel Be'er Sheva", "apostrophe + prefix"),
    ("Gornik Zabrze", "Górnik Zabrze", "diacritic"),
    ("Fenerbahce", "Fenerbahçe SK", "diacritic + legal form"),
    ("Crvena Zvezda", "FK Crvena zvezda", "legal form + case"),
    ("Heart of Midlothian", "Heart of Midlothian FC", "legal form"),
    ("NK Celje", "NK Celje", "identical"),
    ("Shamrock", "Shamrock Rovers FC", "dropped nickname + legal form"),
    ("Ararat-Armenia", "Ararat-Armenia FA", "legal form"),
    ("Aarhus", "Aarhus GF", "legal form"),
    ("Hoffenheim", "TSG Hoffenheim", "legal form"),
    ("Leverkusen", "Bayer Leverkusen", "sponsor prefix"),
    ("Genk", "KRC Genk", "legal form"),
    ("Al Ahli Saudi", "Al Ahli Saudi FC", "legal form"),
    ("Leeds United", "Leeds United", "identical"),
    ("Sunderland", "Sunderland AFC", "legal form"),
    ("Malaga", "Malaga CF", "legal form"),
    ("Al-Ittihad", "Al-Ittihad Club", "legal form"),
    ("Monza", "AC Monza", "legal form"),
    ("Bayern Munich", "Bayern Munich", "identical"),
    ("Stoke", "Stoke City", "city suffix"),
    ("Everton", "Everton FC", "legal form"),
    ("Thun", "FC Thun", "legal form"),
    ("Larne", "Larne FC", "legal form"),
    # MLS truncations (must keep working — these were passing before the fix)
    ("Los Angeles G", "Los Angeles Galaxy", "Kalshi truncation letter"),
    ("Los Angeles F", "Los Angeles FC", "truncation letter -> legal-form token"),
    ("New York RB", "New York Red Bulls", "initialism"),
    ("DC United", "D.C. United SC", "punctuation-split initialism"),
    ("Orlando", "Orlando City SC", "city + legal form"),
    ("Vancouver", "Vancouver Whitecaps FC", "nickname + legal form"),
]


@pytest.mark.parametrize("kalshi,poly,why", LIVE_PAIRS)
def test_live_club_spellings_match(kalshi, poly, why):
    assert sn.same_club_with_alias(kalshi, poly), f"{why}: {sn.explain(kalshi, poly)}"


# --------------------------------------------------------------------------- #
# 2. DECOYS — near-misses that must NEVER pair                                  #
# --------------------------------------------------------------------------- #
DECOYS = [
    ("Dinamo Zagreb", "Dinamo Bucuresti", "same club word, DIFFERENT city"),
    ("Sparta Prague", "Sparta Rotterdam", "same club word, different city"),
    ("Sporting CP", "Sporting Gijon", "generic club word only"),
    ("Inter Milan", "Inter Club d Escaldes", "generic club word only"),
    ("Lincoln Red Imps", "Lincoln City", "shared place name, different club"),
    ("Kairat", "Qarabag", "different club, similar-looking"),
    ("Los Angeles G", "Los Angeles FC", "Galaxy vs LAFC — the truncation letter DISAMBIGUATES"),
    ("Los Angeles F", "Los Angeles Galaxy", "LAFC vs Galaxy, reversed"),
    ("Dinamo Zagreb", "GNK Dinamo Zagreb II", "first team vs RESERVES"),
    ("Real Madrid", "Real Madrid Castilla", "first team vs reserves (named)"),
    ("Thun", "Thun II", "first team vs reserves"),
    ("PSV", "PSG", "3-letter clubs one edit apart"),
    ("Atletico", "Athletic Bilbao", "different clubs, similar generic word"),
]


@pytest.mark.parametrize("a,b,why", DECOYS)
def test_decoys_do_not_match(a, b, why):
    assert not sn.same_club_with_alias(a, b), f"{why}: {sn.explain(a, b)}"


# --------------------------------------------------------------------------- #
# 3. The normalizer's individual rules                                          #
# --------------------------------------------------------------------------- #
def test_diacritics_fold_without_splitting_tokens():
    """The ORIGINAL BUG: an ASCII-only regex split an accented word into fragments."""
    assert sn.significant("KF Víkingur") == ["vikingur"]
    assert sn.significant("AS Omónoia Leukosías") == ["omonoia", "leukosias"]
    assert sn.significant("Górnik Zabrze") == ["gornik", "zabrze"]
    assert sn.significant("KKS Lech Poznań") == ["lech", "poznan"]
    assert sn.significant("KF Egnatia Rrogozhinë") == ["egnatia", "rrogozhine"]


def test_legal_form_tokens_are_dropped_but_club_words_are_not():
    assert sn.significant("GNK Dinamo Zagreb") == ["dinamo", "zagreb"]
    assert sn.significant("SK Puntigamer Sturm Graz") == ["puntigamer", "sturm", "graz"]
    # 'atletico'/'real' are CLUB words, not legal forms — stripping them wiped the name to nothing.
    assert sn.significant("Atletico Madrid") == ["atletico", "madrid"]
    assert sn.significant("Real Sociedad") == ["real", "sociedad"]


def test_bare_numbers_dropped_and_never_empty():
    assert sn.significant("Dos Hermanas CF 1971") == ["dos", "hermanas"]
    assert sn.significant("KÍ") == ["ki"]          # all-legal name still yields a token


def test_single_letters_are_kept_as_truncation_markers():
    """Dropping them collapses 'Los Angeles G' and 'Los Angeles F' to the same key."""
    assert sn.significant("Los Angeles G") == ["los", "angeles", "g"]
    assert sn.tokens("D.C. United") == ["dc", "united"]      # punctuation-split initials glued back


def test_token_similarity_rules():
    assert sn.token_similar("kairat", "qairat")              # one substitution
    assert sn.token_similar("omonia", "omonoia")             # one insertion
    assert sn.token_similar("pozna", "poznan")               # truncation
    assert sn.token_similar("g", "galaxy")                   # single-letter truncation
    assert not sn.token_similar("psv", "psg")                # too short for a one-edit fuse
    assert not sn.token_similar("zagreb", "bucuresti")


def test_sponsor_or_city_on_one_side_does_not_veto():
    s = sn.score("Sturm Graz", "SK Puntigamer Sturm Graz")
    assert s["score"] == 1.0 and "puntigamer" in s["unmatched_b"]
    assert sn.same_club("Sturm Graz", "SK Puntigamer Sturm Graz")


def test_explain_is_self_diagnosing():
    """A near-miss must print BOTH token sets so the cause is readable in the build log."""
    text = sn.explain("Omonia Nicosia", "AS Omónoia Leukosías")
    assert "omonia" in text and "omonoia" in text and "leukosias" in text
    assert "score=" in text and "unmatched=" in text


def test_alias_layer_resolves_the_underivable_and_is_learned():
    """Exonyms cannot be derived from the strings; they are recorded, not guessed."""
    assert not sn.same_club("Omonia Nicosia", "AS Omónoia Leukosías")     # rules alone: no
    assert sn.same_club_with_alias("Omonia Nicosia", "AS Omónoia Leukosías")
    learned: dict = {}
    sn.learn(learned, "Panathinaikos", "PAO Athens")
    assert learned and sn.same_club_with_alias("Panathinaikos", "PAO Athens", learned)
    # a DERIVABLE pair is not stored — the file must not bloat with what the rules already handle
    already: dict = {}
    sn.learn(already, "Thun", "FC Thun")
    assert already == {}


# --------------------------------------------------------------------------- #
# 4. TWO-LEG TIE GUARD — real UCL texts from the captured payloads              #
# --------------------------------------------------------------------------- #
def test_poly_team_to_advance_is_tie_scope():
    """The real Polymarket aggregate market must classify as 'tie'."""
    ev = _fixture("raw_soccer_ucl_dinamo_thun_poly.json")
    advance = [m for e in ev["events"] for m in (e.get("markets") or [])
               if str(m.get("groupItemTitle")) == "Team to Advance"]
    assert advance, "fixture must contain the Team to Advance market"
    for m in advance:
        blob = f"{m.get('question')} {m.get('groupItemTitle')} {m.get('description')}"
        assert mr.parse_tie_scope(blob) == "tie"
        assert "two-legged tie" in str(m.get("description"))


def test_poly_single_game_winner_is_not_tie_scope():
    """The 1x2 markets on the SAME event must stay pairable."""
    ev = _fixture("raw_soccer_ucl_dinamo_thun_poly.json")
    singles = [m for e in ev["events"] for m in (e.get("markets") or [])
               if str(m.get("question", "")).startswith("Will ") and " win on " in str(m.get("question"))]
    assert singles
    for m in singles:
        blob = f"{m.get('question')} {m.get('groupItemTitle')} {m.get('description')}"
        assert mr.parse_tie_scope(blob) == "single_game"


def test_kalshi_regulation_winner_is_single_game():
    """Kalshi's game-winner rules say '90 minutes plus stoppage (does not include extra time or
    penalties)'. That boilerplate must NOT be mistaken for tie scope."""
    ev = _fixture("raw_soccer_ucl_dinamo_thun_kalshi.json")
    assert ev["markets"]
    for m in ev["markets"]:
        blob = f"{m.get('title')} {m.get('yes_sub_title')} {m.get('rules_primary')}"
        assert mr.parse_tie_scope(blob) == "single_game", m.get("ticker")


def test_kalshi_advance_rules_are_tie_scope():
    """Verbatim KXUCLADVANCE rules text captured live 2026-07-28."""
    rules = ("If Crvena Zvezda advance past Larne in the Crvena Zvezda vs Larne soccer match in the "
             "Qualification Round 2 of the Champions League, then the market resolves to Yes.")
    assert mr.parse_tie_scope(rules) == "tie"
    assert mr.parse_tie_scope("Crvena Zvezda advances") == "tie"


def test_penalties_boilerplate_alone_is_not_tie_scope():
    """THE false-positive trap: ordinary regulation markets mention penalties in their EXCLUSIONS.
    A naive 'penalt' keyword scan would refuse every market we actually want to pair."""
    boiler = ("This market will resolve to \"Yes\" if both teams each score at least one goal during "
              "the first half. Second-half goals, extra time, and penalty shoot-outs are excluded.")
    assert mr.parse_tie_scope(boiler) != "tie"
    # Kalshi's own regulation wording must stay clear of tie scope too.
    kalshi_boiler = ("If Thun wins the Dinamo Zagreb vs Thun professional Champions League soccer game "
                     "originally scheduled for Jul 28, 2026 after 90 minutes plus stoppage time (does "
                     "not include extra time or penalties), then the market resolves to Yes.")
    assert mr.parse_tie_scope(kalshi_boiler) == "single_game"
    assert mr.parse_settlement_period(kalshi_boiler) == "regulation"


def test_tie_scope_is_none_when_undeterminable():
    assert mr.parse_tie_scope("") is None
    assert mr.parse_tie_scope("Some unrelated market about the weather") is None


# --------------------------------------------------------------------------- #
# 5. join_game REFUSES a tie-vs-leg pairing                                     #
# --------------------------------------------------------------------------- #
def _opt(scope, venue):
    o = {"market_type": "ml3", "market_key": "ml3", "side": "home", "line": None, "kind": "3way",
         "confidence": "high", "outcome_label": "Home", "settle_period": "regulation",
         "settle_scope": scope}
    o.update({"kalshi_ticker": "KX-1", "kalshi_side": "yes"} if venue == "kalshi"
             else {"poly_token_id": "TOK", "poly_side": "Yes"})
    return o


def test_join_game_refuses_tie_against_single_game():
    from src.genz.tree_builder import join_game
    nodes, unmatched = join_game({"k": _opt("single_game", "kalshi")},
                                 {"k": _opt("tie", "poly")}, game_id="G")
    assert nodes == []
    assert {u["reason"] for u in unmatched} == {"two_leg_mismatch"}
    assert {u["venue"] for u in unmatched} == {"kalshi", "polymarket"}


def test_join_game_refuses_when_only_one_side_declares_tie():
    """An UNDECLARED counterpart is not evidence that it is also a tie."""
    from src.genz.tree_builder import join_game
    nodes, unmatched = join_game({"k": _opt(None, "kalshi")}, {"k": _opt("tie", "poly")}, game_id="G")
    assert nodes == [] and unmatched[0]["reason"] == "two_leg_mismatch"


def test_join_game_pairs_two_single_game_legs():
    from src.genz.tree_builder import join_game
    nodes, _ = join_game({"k": _opt("single_game", "kalshi")},
                         {"k": _opt("single_game", "poly")}, game_id="G")
    assert len(nodes) == 1 and nodes[0]["twin_key"] == "k"


def test_join_game_pairs_two_tie_markets():
    """Tie-vs-tie is a legitimate pair (Kalshi ADVANCE against Poly Team to Advance)."""
    from src.genz.tree_builder import join_game
    nodes, _ = join_game({"k": _opt("tie", "kalshi")}, {"k": _opt("tie", "poly")}, game_id="G")
    assert len(nodes) == 1


def test_join_game_unaffected_when_neither_declares_scope():
    from src.genz.tree_builder import join_game
    nodes, _ = join_game({"k": _opt(None, "kalshi")}, {"k": _opt(None, "poly")}, game_id="G")
    assert len(nodes) == 1


# --------------------------------------------------------------------------- #
# 6. Outcome-level identity uses the club matcher (the 1-node-of-3 bug)         #
# --------------------------------------------------------------------------- #
def test_game_ctx_resolves_european_spellings_to_a_side():
    """The EVENT paired but its team outcomes dropped, leaving only the draw node."""
    ctx = mr.GameCtx(home="Omonia Nicosia", away="Kairat")
    assert ctx.side_for_team("AS Omónoia Leukosías") == "home"
    assert ctx.side_for_team("Qairat FK") == "away"
    ctx2 = mr.GameCtx(home="Vikingur Reykjavik", away="Be`er Sheva")
    assert ctx2.side_for_team("KF Víkingur") == "home"
    assert ctx2.side_for_team("MH Hapoel Be'er Sheva") == "away"


def test_game_ctx_still_rejects_a_third_team():
    ctx = mr.GameCtx(home="Omonia Nicosia", away="Kairat")
    assert ctx.side_for_team("Qarabag FK") is None
    assert ctx.side_for_team("Dinamo Bucuresti") is None


# --------------------------------------------------------------------------- #
# 7. TOTAL series discovery — why soccer had no 2-way (quotable) nodes          #
# --------------------------------------------------------------------------- #
class _Catalog:
    """Kalshi list_series stand-in carrying the REAL titles captured 2026-07-28."""
    ROWS = [
        {"ticker": "KXUCLGAME", "title": "UEFA Champions League Game", "tags": ["Soccer"]},
        {"ticker": "KXUCLTOTAL", "title": "Champions League Total Goals", "tags": ["Soccer"]},
        {"ticker": "KXCLUBFGAME", "title": "Club Friendlies", "tags": ["Soccer"]},
        {"ticker": "KXCLUBFTOTAL", "title": "Point Total", "tags": ["Soccer"]},
        {"ticker": "KXMLSGAME", "title": "MLS Game", "tags": ["Soccer"]},
        {"ticker": "KXMLSTOTAL", "title": "MLS Total", "tags": ["Soccer"]},
        {"ticker": "KXNBAGAME", "title": "NBA Game", "tags": ["Basketball"]},
    ]

    def list_series(self, category=None):
        return list(self.ROWS)


def test_total_series_derived_from_game_stem():
    """The title-keyword scan finds NEITHER totals series — their titles omit the competition name.
    Deriving GAME -> TOTAL on the ticker stem recovers them, which is what restores 2-way nodes."""
    from src.genz.tree_builder import _total_series_for
    assert _total_series_for(_Catalog(), ["KXUCLGAME"], []) == ["KXUCLTOTAL"]
    assert _total_series_for(_Catalog(), ["KXCLUBFGAME"], []) == ["KXCLUBFTOTAL"]


def test_total_series_derivation_requires_catalog_confirmation():
    """A derived ticker that the catalog does not list is NOT invented."""
    from src.genz.tree_builder import _total_series_for
    assert _total_series_for(_Catalog(), ["KXNOSUCHGAME"], []) == []


def test_total_series_derivation_is_additive_and_deduped():
    from src.genz.tree_builder import _total_series_for
    assert _total_series_for(_Catalog(), ["KXUCLGAME"], ["KXUCLTOTAL"]) == ["KXUCLTOTAL"]
    got = _total_series_for(_Catalog(), ["KXUCLGAME"], ["KXMLSTOTAL"])
    assert got == ["KXMLSTOTAL", "KXUCLTOTAL"]


def test_total_series_survives_a_catalog_failure():
    """An unavailable catalog falls back to the keyword scan instead of crashing the build."""
    from src.genz.tree_builder import _total_series_for

    class _Boom:
        def list_series(self, category=None):
            raise RuntimeError("kalshi 503")

    assert _total_series_for(_Boom(), ["KXUCLGAME"], ["KXUCLTOTAL"]) == ["KXUCLTOTAL"]
