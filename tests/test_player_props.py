"""STEP 5 player-props tests: diacritics normalizer, roster-restricted matching (surname fallback +
ambiguous drop), the group-stage settlement gate (knockout REJECTED / group-stage ALLOWED), the
1+-only parsers, and the 2-way arb build."""
from __future__ import annotations

from src import player_props as pp
from src.config import Config, Secrets

KNOCK = "2026-06-28T00:00:00Z"


def _cfg():
    return Config(raw={"bankroll_total": 30000,
                       "thresholds": {"min_roi_pct": 0.5, "min_total_stake": 20,
                                      "roi_suspicious_pct": 8.0, "assumed_unknown_limit": 1000,
                                      "low_confidence_limit_floor": 10}},
                  secrets=Secrets(None, None, None))


# --------------------------------------------------------------------------- #
# Diacritics normalizer + roster-restricted matching                           #
# --------------------------------------------------------------------------- #
def test_normalize_player_folds_diacritics():
    assert pp.normalize_player("Hložek") == "hlozek"
    assert pp.normalize_player("Tomáš Souček") == "tomas soucek"
    assert pp.normalize_player("Éderson") == "ederson"
    assert pp.normalize_player("Neymar Jr.") == "neymar jr"


def test_match_players_exact_surname_and_ambiguous():
    kalshi = {"patrik schick", "tomas soucek", "neymar"}
    poly = {"patrik schick", "tomas soucek jr", "neymar jr"}
    m = pp.match_players(kalshi, poly)
    assert m["patrik schick"] == "patrik schick"      # exact
    assert m["tomas soucek"] == "tomas soucek jr"     # surname fallback (soucek unique)
    assert m["neymar"] == "neymar jr"                 # surname fallback (neymar unique)


def test_match_players_drops_ambiguous_and_wrong_firstname():
    # Two Poly 'silva's both nest the bare surname 'silva' -> ambiguous -> dropped.
    assert pp.match_players({"silva"}, {"bruno silva", "thiago silva"}) == {}
    # Same surname but different FIRST names never match (token sets not nested) -> fail-closed.
    assert pp.match_players({"thiago silva"}, {"bruno silva"}) == {}


# --------------------------------------------------------------------------- #
# Settlement gate — the group-stage fix (knockout rejected / group allowed)      #
# --------------------------------------------------------------------------- #
def test_fixture_stage_and_settlement_gate():
    assert pp.fixture_stage("2026-06-18T16:00:00Z", KNOCK) == "group_stage"
    assert pp.fixture_stage("2026-06-28T16:00:00Z", KNOCK) == "knockout"
    assert pp.fixture_stage("2026-06-30T16:00:00Z", KNOCK) == "knockout"
    assert pp.settlement_ok("group_stage") is True
    assert pp.settlement_ok("knockout") is False
    assert pp.fixture_stage("garbage", KNOCK) == "knockout"   # fail-closed


def test_knockout_pair_rejected_group_stage_allowed():
    """Identical priced legs DO form an arb; the ONLY thing that changes the outcome is the stage gate
    — group-stage is allowed, knockout is rejected (Kalshi incl-ET vs Poly reg-90 settlement)."""
    cfg = _cfg()
    # scores best 2.2 (kalshi) + no-goal best 2.0 (poly): S = 1/2.2 + 1/2.0 = 0.954 -> arb.
    built = pp.build_player_arb("idG", "Patrik Schick", kalshi_scores=(2.20, 1000),
                                kalshi_no=(1.95, 1000), poly_scores=(2.10, 1000), poly_no=(2.00, 1000),
                                cfg=cfg, commission={})
    assert built is not None and built[1].is_arb              # a real arb exists on the prices

    group_ko, knock_ko = "2026-06-18T16:00:00Z", "2026-06-29T16:00:00Z"
    # The gate detect() applies before pairing:
    assert pp.settlement_ok(pp.fixture_stage(group_ko, KNOCK)) is True    # ALLOWED
    assert pp.settlement_ok(pp.fixture_stage(knock_ko, KNOCK)) is False   # REJECTED


# --------------------------------------------------------------------------- #
# 1+-only parsers                                                               #
# --------------------------------------------------------------------------- #
def _kmkt(sub, yes_ask, no_ask, yes_sz="100", no_sz="100", status="active"):
    return {"yes_sub_title": sub, "status": status, "yes_ask_dollars": yes_ask,
            "no_ask_dollars": no_ask, "yes_ask_size_fp": yes_sz, "no_ask_size_fp": no_sz}


def test_parse_kalshi_goals_keeps_only_1plus():
    markets = [_kmkt("Patrik Schick: 1+", "0.40", "0.62"),
               _kmkt("Patrik Schick: 2+", "0.10", "0.91"),     # multi-goal -> ignored
               _kmkt("Tomas Soucek: 1+", "0.30", "0.72")]
    g = pp.parse_kalshi_goals(markets)
    assert set(g) == {"patrik schick", "tomas soucek"}
    assert round(g["patrik schick"]["scores"][0], 3) == 2.5    # 1/0.40
    assert round(g["patrik schick"]["no_score"][0], 4) == round(1 / 0.62, 4)


def _pmkt(git, yes_tok, no_tok):
    return {"groupItemTitle": git, "question": git, "outcomes": '["Yes", "No"]',
            "clobTokenIds": f'["{yes_tok}", "{no_tok}"]'}


def test_parse_poly_goal_tokens_only_1plus_goals():
    ev = {"markets": [
        _pmkt("Neymar Jr: 1+ goals", "ny_y", "ny_n"),
        _pmkt("Neymar Jr: 2+ goals", "x", "y"),                # multi-goal -> ignored
        _pmkt("Neymar Jr: 1+ assists", "a", "b"),              # not goals -> ignored
    ]}
    t = pp.parse_poly_goal_tokens(ev)
    assert set(t) == {"neymar jr"}
    assert t["neymar jr"]["yes_token"] == "ny_y" and t["neymar jr"]["no_token"] == "ny_n"


def test_build_player_arb_returns_none_when_no_arb():
    cfg = _cfg()
    # Both sides priced poorly -> S > 1 -> not an arb -> None.
    assert pp.build_player_arb("idG", "X", (1.5, 1000), (1.5, 1000), (1.5, 1000), (1.5, 1000),
                               cfg=cfg, commission={}) is None
