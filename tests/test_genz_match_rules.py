"""Tests for the GenZ deterministic Kalshi<->Polymarket outcome matcher (src/genz/match_rules.py).

The contract: a Kalshi outcome and a Polymarket outcome are the SAME bet iff their twin_key strings
are equal. These tests pin the threshold<->line conversions and the cross-venue twin equality for
corners, totals, team totals, spreads, halves, exact score (home/away ordering), BTTS, advance,
first-to-score, moneyline (3-way), and player props (low-confidence, accent-normalized)."""
from __future__ import annotations

from src.genz import match_rules as mr

CTX = mr.GameCtx(home="Norway", away="Ivory Coast")    # Kalshi ticker suffix CIVNOR -> away=CIV, home=NOR


def _k(series, sub, title=None):
    return {"event_ticker": f"{series}-26JUN30CIVNOR", "ticker": f"{series}-26JUN30CIVNOR-X",
            "yes_sub_title": sub, "title": title if title is not None else sub}


def _yes(market):
    """The YES (first/over/cover) outcome of a Kalshi market."""
    outs = mr.kalshi_outcomes(market, CTX)
    return outs[0][0]


# --------------------------------------------------------------------------- #
# Low-level threshold<->line converters                                         #
# --------------------------------------------------------------------------- #
def test_goals_threshold_to_line():
    assert mr.goals_over_to_line("Over 2.5 goals") == 2.5         # half-line direct
    assert mr.goals_over_to_line("3+ goals") == 2.5              # >=3 -> over 2.5
    assert mr.goals_over_to_line("nope") is None


def test_corners_threshold_to_line():
    assert mr.corners_plus_to_line("9+ corners") == 8.5          # K+ (>=9) -> over (K-0.5)
    assert mr.corners_plus_to_line("Over 8.5 corners") == 8.5
    assert mr.poly_corners_line("O/U 8.5 Total Corners") == 8.5


def test_spread_phrase_to_line():
    assert mr.spread_cover_to_line("wins by more than 1.5") == 1.5
    assert mr.spread_cover_to_line("by 2+ goals") == 1.5         # >=2 margin == covers -1.5


def test_half_result_label_maps_both_venues():
    assert mr.half_result_side("Tie 1st Half", home="Norway", away="Ivory Coast") == ("1h", "draw")
    assert mr.half_result_side("Draw at halftime", home="Norway", away="Ivory Coast") == ("1h", "draw")
    assert mr.half_result_side("Norway wins 1st Half", home="Norway", away="Ivory Coast") == ("1h", "home")
    assert mr.half_result_side("Ivory Coast leading at halftime", home="Norway", away="Ivory Coast") == ("1h", "away")
    assert mr.half_result_side("Norway 2nd Half", home="Norway", away="Ivory Coast") == ("2h", "home")


def test_player_name_accent_normalization():
    assert mr.normalize_player("Kylian Mbappé") == "kylian mbappe"
    assert mr.strip_accents("Mbappé") == "Mbappe" and mr.strip_accents("Müller") == "Muller"
    assert mr.player_surname("Erling Håland") == "haland"
    # Kalshi full name and Poly surname collapse to the SAME group via the surname key.
    assert mr.mk_player_goals("Erling Haaland", 2).group == mr.mk_player_goals("Haaland", 2).group


# --------------------------------------------------------------------------- #
# Cross-venue twin equality (the deterministic join)                            #
# --------------------------------------------------------------------------- #
def test_corners_twin_kalshi_to_poly():
    """Kalshi '9+ corners' OVER twins Poly 'O/U 8.5 Total Corners' OVER."""
    k_over = _yes(_k("KXWCTCORNERS", "9+ corners"))
    p_over = mr.mk_corners(mr.poly_corners_line("O/U 8.5 Total Corners"), "over")
    assert k_over.twin_key() == p_over.twin_key() == "corners|8.5##over"
    assert k_over.kind == "2way"


def _kalshi_over_under(market):
    """The (over=YES, under=NO) canonical outcomes of a binary Kalshi market."""
    outs = mr.kalshi_outcomes(market, CTX)
    return (next(o for o, s in outs if s == "YES"), next(o for o, s in outs if s == "NO"))


def test_corners_kalshi_K_pairs_only_with_poly_K_minus_half():
    """LIVE-CONFIRMED half-line lock: Kalshi 'K+ corners' (>=K) == Poly 'O/U (K-0.5) Over'
    (K=9 -> 8.5 ; K=10 -> 9.5 — Kalshi 10+ Yes ~47c == Poly O/U 9.5 Over, the SAME outcome).
    Over<->Yes / Under<->No. A refactor that shifts the half-line breaks this."""
    for k_int, line in ((9, 8.5), (10, 9.5)):
        k_over, k_under = _kalshi_over_under(_k("KXWCTCORNERS", f"{k_int}+ corners"))
        assert mr.poly_corners_line(f"O/U {line} Total Corners") == line
        # Over <-> Yes (the YES side) twins Poly Over at (K-0.5); Under <-> No twins Poly Under.
        assert k_over.twin_key() == mr.mk_corners(line, "over").twin_key() == f"corners|{line}##over"
        assert k_under.twin_key() == mr.mk_corners(line, "under").twin_key() == f"corners|{line}##under"


def test_corners_kalshi_K_does_not_pair_with_K_plus_half():
    """GUARD: Kalshi 'K+ corners' must NOT pair with Poly 'O/U (K+0.5)' — that is a DIFFERENT outcome
    (9+ means >8.5, i.e. line 8.5, never 9.5). Lock the rejection so the half-line can't drift up."""
    k_over, k_under = _kalshi_over_under(_k("KXWCTCORNERS", "9+ corners"))
    # The WRONG line (K+0.5 = 9.5) must NOT collide on either side.
    assert k_over.twin_key() != mr.mk_corners(9.5, "over").twin_key()
    assert k_under.twin_key() != mr.mk_corners(9.5, "under").twin_key()
    # ...and the CORRECT line (K-0.5 = 8.5) does collide.
    assert k_over.twin_key() == mr.mk_corners(8.5, "over").twin_key()
    assert k_under.twin_key() == mr.mk_corners(8.5, "under").twin_key()


def test_team_corners_kalshi_K_pairs_with_poly_K_minus_half():
    """team_corners follows the SAME K -> (K-0.5) rule: Kalshi 'Norway 5+ corners' == Poly
    'Norway O/U 4.5 Over' (team-keyed). The wrong half-line (5.5) is rejected."""
    k_over, k_under = _kalshi_over_under(_k("KXWCCORNERS", "Norway 5+ corners"))
    assert k_over.twin_key() == mr.mk_corners(4.5, "over", team="Norway").twin_key() \
        == "team_corners|norway|4.5##over"
    assert k_under.twin_key() == mr.mk_corners(4.5, "under", team="Norway").twin_key()
    assert k_over.twin_key() != mr.mk_corners(5.5, "over", team="Norway").twin_key()   # K+0.5 rejected


def test_totals_twin_kalshi_to_poly():
    k_over = _yes(_k("KXWCTOTAL", "Over 2.5 goals"))
    p_over = mr.mk_total(mr.poly_total_line("O/U 2.5"), "over")
    assert k_over.twin_key() == p_over.twin_key() == "total_goals|2.5##over"
    # the binary NO side is the Under twin, same line/group.
    k_under = mr.kalshi_outcomes(_k("KXWCTOTAL", "Over 2.5 goals"), CTX)[1][0]
    assert k_under.twin_key() == "total_goals|2.5##under" and k_under.group == k_over.group


def test_half_total_twins():
    k1 = _yes(_k("KXWC1HTOTAL", "Over 1.5 goals"))
    assert k1.twin_key() == mr.mk_total(1.5, "over", "1h").twin_key() == "1h_total|1.5##over"
    k2 = _yes(_k("KXWC2HTOTAL", "Over 0.5 goals"))
    assert k2.twin_key() == "2h_total|0.5##over" and k2.kind == "2way"


def test_team_total_twin():
    k_over = _yes(_k("KXWCTEAMTOTAL", "Norway over 1.5 goals"))
    t, line = mr.poly_team_total("Norway O/U 1.5", CTX)
    assert k_over.twin_key() == mr.mk_team_total(t, line, "over").twin_key()


def test_spread_twin_negative_line_cover():
    """Kalshi 'Norway wins by more than 1.5' twins Poly 'Spread: Norway (-1.5)'. The market is keyed
    by the FAVORED team + margin; its two sides are the complements cover / plus."""
    k_cover = _yes(_k("KXWCSPREAD", "Norway wins by more than 1.5"))
    p_cover = mr.mk_spread("Norway", 1.5, "cover")
    assert k_cover.twin_key() == p_cover.twin_key() == "spread|1.5|norway##cover"
    # The NO side is the SAME line's other half (Norway fails to cover = Côte d'Ivoire +1.5).
    k_plus = mr.kalshi_outcomes(_k("KXWCSPREAD", "Norway wins by more than 1.5"), CTX)[1][0]
    assert k_plus.group == k_cover.group and k_plus.side == "plus"
    assert k_plus.twin_key() == mr.mk_spread("Norway", 1.5, "plus").twin_key()
    # A DIFFERENT team's favorite handicap at the same margin is a DIFFERENT group (never conflated).
    assert mr.mk_spread("Ivory Coast", 1.5, "cover").group != k_cover.group


def test_half_result_twin_tie_first_half():
    k_draw = _yes(_k("KXWC1H", "Tie 1st Half"))
    p_draw = mr.mk_half_result(*mr.half_result_side("Draw at halftime", home="Norway", away="Ivory Coast"))
    assert k_draw.twin_key() == p_draw.twin_key() == "1h_result##draw"
    assert k_draw.kind == "3way"


def test_exact_score_canonical_away_home_key():
    # Canonical key = AWAYgoals-HOMEgoals (CIVgoals-NORgoals). Winner + score parsed from the TITLE.
    assert mr.kalshi_exact_score("Reg Time: Ivory Coast wins 2-1", CTX) == "2-1"   # CIV 2, NOR 1
    assert mr.kalshi_exact_score("Reg Time: Norway wins 2-1", CTX) == "1-2"        # NOR 2, CIV 1
    assert mr.kalshi_exact_score("Reg Time: Norway wins 1-0", CTX) == "0-1"        # NOR 1, CIV 0
    assert mr.kalshi_exact_score("Reg Time: Draw 1-1", CTX) == "1-1"
    # Poly 'Côte d'Ivoire A - B Norway' = CIV scored A, Norway scored B (team-order robust).
    assert mr.poly_exact_score("Côte d'Ivoire 2 - 1 Norway", CTX) == "2-1"
    assert mr.poly_exact_score("Côte d'Ivoire 1 - 2 Norway", CTX) == "1-2"
    assert mr.poly_exact_score("1 - 1", CTX) == "1-1"


def test_exact_score_pairs_correct_scoreline_not_inverted():
    """The live bug: CIV-wins-2-1 was paired to the NOR-wins scoreline. Each Kalshi score must twin
    its TRUE Poly scoreline, and CIV-wins-2-1 must NOT collide with NOR-wins-2-1."""
    k_civ = _yes(_k("KXWCSCORE", "x", title="Reg Time: Ivory Coast wins 2-1"))
    p_civ = mr.mk_exact_score(mr.poly_exact_score("Côte d'Ivoire 2 - 1 Norway", CTX))
    p_nor = mr.mk_exact_score(mr.poly_exact_score("Côte d'Ivoire 1 - 2 Norway", CTX))
    assert k_civ.twin_key() == p_civ.twin_key() == "exact_score##2-1"   # CIV wins 2-1 <-> CIV 2 - 1 NOR
    assert k_civ.twin_key() != p_nor.twin_key()                        # NOT the Norway-wins scoreline
    k_nor = _yes(_k("KXWCSCORE", "x", title="Reg Time: Norway wins 2-1"))
    assert k_nor.twin_key() == p_nor.twin_key() == "exact_score##1-2"
    # Confirmed-live reversal case: CIV wins 1-0 -> Poly 'Côte d'Ivoire 1 - 0 Norway' (not 0 - 1).
    k_10 = _yes(_k("KXWCSCORE", "x", title="Reg Time: Côte d'Ivoire wins 1-0"))
    assert k_10.twin_key() == mr.mk_exact_score(mr.poly_exact_score("Côte d'Ivoire 1 - 0 Norway", CTX)).twin_key()
    assert k_10.twin_key() != mr.mk_exact_score(mr.poly_exact_score("Côte d'Ivoire 0 - 1 Norway", CTX)).twin_key()
    assert k_civ.kind == "multi"


def test_spread_covering_team_from_title_not_sub():
    """The live bug: Norway -1.5 was emitted with side='ivory coast'. The covering team must come from
    the TITLE ('Norway wins by more than 1.5'), so a misleading yes_sub_title can't flip it."""
    outs = mr.kalshi_outcomes(_k("KXWCSPREAD", "Côte d'Ivoire",          # misleading sub
                                 title="Goal Diff Reg Time: Norway wins by more than 1.5 goals"), CTX)
    yes = next(o for o, s in outs if s == "YES")
    no = next(o for o, s in outs if s == "NO")
    # The market is keyed by the FAVORED team (Norway) from the title, with cover/plus sides — the
    # 'ivory coast' sub must NOT make this a Côte d'Ivoire-favored market.
    assert yes.group == "spread|1.5|norway" and yes.side == "cover" and yes.line == 1.5
    assert no.group == "spread|1.5|norway" and no.side == "plus"
    assert yes.twin_key() == mr.mk_spread("Norway", 1.5, "cover").twin_key()


def test_btts_and_advance_twins():
    k_yes = _yes(_k("KXWCBTTS", "Both teams to score"))
    assert k_yes.twin_key() == mr.mk_btts("", "yes").twin_key() == "btts##yes"
    k_adv = _yes(_k("KXWCADVANCE", "Norway advances"))
    assert k_adv.twin_key() == mr.mk_advance(CTX.pair, mr._team("Norway"), label="Norway").twin_key()
    assert k_adv.kind == "2way"


def test_first_to_score_no_goal_twin():
    k_none = _yes(_k("KXWCFTTS", "No Goal"))
    assert k_none.twin_key() == mr.mk_first_to_score("none").twin_key() == "first_to_score##none"
    assert k_none.kind == "3way"


def test_moneyline_is_three_way_and_sides():
    assert _yes(_k("KXWCGAME", "Tie")).twin_key() == "moneyline##draw"
    assert _yes(_k("KXWCGAME", "Norway")).twin_key() == "moneyline##home"
    assert _yes(_k("KXWCGAME", "Ivory Coast")).twin_key() == "moneyline##away"
    assert _yes(_k("KXWCGAME", "Norway")).kind == "3way"


def test_player_props_are_low_confidence_alert_only():
    k = _yes(_k("KXWCGOAL", "Erling Haaland 2+"))
    p_parsed = mr.poly_player_goals("Haaland: 2+ goals")
    p = mr.mk_player_goals(*p_parsed)
    assert k.twin_key() == p.twin_key()
    assert k.confidence == "low" and k.kind == "multi"   # player props never auto-trade


# --------------------------------------------------------------------------- #
# SETTLEMENT PERIOD parsing (regulation 90'+stoppage vs full game incl. ET).     #
# The two venues settle full-match COUNT markets on different periods.           #
# --------------------------------------------------------------------------- #
def test_parse_settlement_period():
    full = "Corners are counted over the entire game (regulation, stoppage AND any extra time periods)."
    reg = ("Refers only to corners within the first 90 minutes of regular play plus stoppage time. "
           "Corners awarded during extra time or penalty shootouts do not count.")
    assert mr.parse_settlement_period(full) == "full_game"
    assert mr.parse_settlement_period(reg) == "regulation"
    # 'does not include extra time' must resolve regulation (exclusion wins over the substring match).
    assert mr.parse_settlement_period("Settled at 90 minutes plus stoppage; does not include extra time.") == "regulation"
    assert mr.parse_settlement_period("This market includes extra time.") == "full_game"
    assert mr.parse_settlement_period("") is None
    assert mr.parse_settlement_period("some unrelated resolution text") is None


def test_count_markets_set():
    assert {"corners", "team_corners", "total_goals", "team_total"} <= mr.COUNT_MARKETS
    assert "moneyline" not in mr.COUNT_MARKETS and "1h_total" not in mr.COUNT_MARKETS
