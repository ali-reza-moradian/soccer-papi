"""Tests for the UFC sport adapter (src/genz/sports_ufc.py) — the FOURTH GenZ sport.

Covers name matching (multi-word surnames Du Plessis / Della Maddalena, accents, token containment,
a decoy card), the scan-only discovery (no slug construction; digit-suffixed slugs matched purely by
tokens), the fight-winner identity gate (winner + Goes-the-Distance + KO/TKO + round markets -> only
the winner paired, exclusions logged, inventory line), parse_ufc_rules against the REAL captured texts
(both facets), and the draw/NC guard (50-50 note vs winner-divergence refuse vs unparseable flag).
All four goldens stay byte-identical (locked in test_genz_sports.py / test_genz_tennis.py)."""
from __future__ import annotations

from datetime import datetime, timezone

from src.genz import sports_ufc as U
from src.genz.config import load_genz_config


class _Log:
    def __init__(self):
        self.infos, self.warnings = [], []

    def info(self, fmt, *a):
        self.infos.append(fmt % a if a else fmt)

    def warning(self, fmt, *a):
        self.warnings.append(fmt % a if a else fmt)


# --------------------------------------------------------------------------- #
# Name matching — multi-word surnames, accents, token containment                #
# --------------------------------------------------------------------------- #
def test_surname_multiword_and_accents():
    assert U.surname("Dricus Du Plessis") == "du plessis"
    assert U.surname("Jack Della Maddalena") == "della maddalena"
    assert U.surname("Kamaru Usman") == "usman"
    assert U.surname("José Aldo") == "aldo"                              # accent-stripped


def test_token_containment_both_directions_and_nickname():
    assert U._same_fighter("Christian Duncan", "Christian Leroy Duncan") is True   # dropped middle name
    assert U._same_fighter("Kamaru Usman", "Usman Kamaru") is True                 # order-independent
    assert U._same_fighter("Dricus Du Plessis", "Kamaru Usman") is False
    assert U._same_fighter("Jose Aldo", "José Aldo Junior") is True                # accent + suffix


def test_names_from_title_multiword_surname_and_enrich():
    # the 'win the X vs Y professional MMA fight' clause fixes the A-vs-B order; multi-word surname kept
    assert U._names_from_title(
        "Will Kamaru Usman win the Du Plessis vs Usman professional MMA fight scheduled for Jul 18, 2026?") \
        == ("Du Plessis", "Usman")
    assert U._decode_date("26JUL18DUUSM") == "2026-07-18"
    # discovery upgrades the title surnames to full names via the per-fighter yes_sub_titles.
    mkts = [{"yes_sub_title": "Dricus Du Plessis"}, {"yes_sub_title": "Kamaru Usman"}]
    assert U._enrich_full_names(("Du Plessis", "Usman"), mkts) == ("Dricus Du Plessis", "Kamaru Usman")


# --------------------------------------------------------------------------- #
# Scan-only discovery: NO slug construction; digit-suffixed slugs matched by tokens #
# --------------------------------------------------------------------------- #
def _fight(a="Kamaru Usman", b="Dricus Du Plessis", date="2026-07-18"):
    return U.UFCFight("KXUFCFIGHT-26JUL18DUUSM", date, a, b, f"{date}T12:00:00Z",
                      markets=[{"event_ticker": "KXUFCFIGHT-26JUL18DUUSM"}])


def test_scan_matches_digit_suffixed_slug_by_tokens_and_ignores_decoy():
    """UFC slugs are underivable (ufc-kam-dri, digit suffixes) — the scan matches purely by FULL-NAME
    token sets + date ±1, and IGNORES a decoy same-fighters event on a different date. There is NO
    events_by_slug call (the adapter never constructs a slug)."""
    fight = _fight()
    series = [
        {"slug": "ufc-kam-dri-2026-01-01", "startTime": "2026-01-01T21:00:00Z",   # DECOY: wrong date
         "title": "UFC 300: Kamaru Usman vs. Dricus Du Plessis (Middleweight)", "markets": []},
        {"slug": "ufc-kam-dri-2026-07-18", "startTime": "2026-07-18T21:00:00Z",   # the real one (digit-suffix)
         "title": "UFC Fight Night: Kamaru Usman vs. Dricus Du Plessis (Middleweight, Main Card)",
         "markets": [{"slug": "ufc-kam-dri-2026-07-18", "groupItemTitle": "Kamaru Usman vs. Dricus Du Plessis",
                      "question": "UFC Fight Night: Kamaru Usman vs. Dricus Du Plessis (Middleweight, Main Card)",
                      "outcomes": ["Kamaru Usman", "Dricus Du Plessis"], "clobTokenIds": ["t_u", "t_d"],
                      "description": "officially declared the winner. draw ... canceled ... resolve 50-50",
                      "feesEnabled": True, "feeSchedule": {"rate": 0.05}}]},
    ]
    ev, method = U.resolve_poly_event(fight, series, log=_Log())
    assert method == "scan" and ev["slug"] == "ufc-kam-dri-2026-07-18"      # decoy ignored, real matched
    assert fight.card == "UFC Fight Night"


def test_scan_time_is_display_only_never_refuses():
    """A large start-time delta (card estimates slide hours) must NOT refuse the pairing — only tokens +
    date ±1 matter."""
    fight = _fight()
    series = [{"slug": "ufc-kam-dri-2026-07-18", "startTime": "2026-07-18T03:00:00Z",   # 18h off, same date
               "title": "UFC Fight Night: Kamaru Usman vs. Dricus Du Plessis", "markets": []}]
    ev, method = U.resolve_poly_event(fight, series)
    assert method == "scan" and ev is series[0]


# --------------------------------------------------------------------------- #
# parse_ufc_rules — REAL captured texts, both facets                             #
# --------------------------------------------------------------------------- #
_POLY_TEXT = ('This market will resolve to "Kamaru Usman" if Kamaru Usman is officially declared the '
              "winner of the fight against Dricus Du Plessis. It will resolve to \"Dricus Du Plessis\" if "
              "Dricus Du Plessis is officially declared the winner. If the fight is declared a draw or "
              "technical draw, ruled a No Contest, not scored, canceled, or postponed beyond August 1, "
              '2026, this market will resolve "50-50."')
_KALSHI_TEXT = ("If Kamaru Usman wins the Du Plessis vs Usman professional MMA fight, then the market "
                "resolves to Yes. If the fight is declared a tie or no contest, the market will resolve "
                "to 50/50 for both fighters. If the fight is cancelled or rescheduled to over two weeks "
                "away, the market will resolve to a fair price in accordance with the rules.")


def test_parse_ufc_rules_poly_official_and_5050():
    r = U.parse_ufc_rules(_POLY_TEXT)
    assert r["winner_rule"] == "official_winner" and r["dnc_rule"] == "fifty_fifty"


def test_parse_ufc_rules_kalshi_last_price_cancel():
    # Kalshi 50/50's a tie/NC but refunds a CANCEL to a fair price -> the cancel facet is the divergence.
    r = U.parse_ufc_rules(_KALSHI_TEXT)
    assert r["winner_rule"] == "official_winner" and r["dnc_rule"] == "last_price"


def test_parse_ufc_rules_unparseable():
    r = U.parse_ufc_rules("this text says nothing about winner or draw or cancel handling")
    assert r["winner_rule"] is None and r["dnc_rule"] is None


# --------------------------------------------------------------------------- #
# Draw/NC guard: 50-50 note (KEEP) vs winner divergence (REFUSE) vs unparseable    #
# --------------------------------------------------------------------------- #
def _node():
    return {"twin_key": "fight_winner|du plessis", "market_key": "fight_winner"}


def test_dnc_guard_5050_note_is_informational_and_kept():
    node = _node()
    keep = U._apply_dnc_guard(node, _KALSHI_TEXT, _POLY_TEXT, _Log(), "G")
    assert keep is True
    assert node["settlement_note"] == "dnc_50_50" and "settlement_risk" not in node
    assert "officially declared" in node["settlement_texts"]["poly"]


def test_dnc_guard_winner_divergence_refuses():
    # A venue that settles the WINNER on last price (not the official decision) is a real divergence.
    kalshi_winner_price = ("there will be no official winner recorded; the winner determined by the last "
                           "traded price at close. a cancel resolves to a fair price.")
    node = _node()
    log = _Log()
    keep = U._apply_dnc_guard(node, kalshi_winner_price, _POLY_TEXT, log, "G")
    assert keep is False and any("winner_rule divergence" in w for w in log.warnings)


def test_dnc_guard_unparseable_side_is_conservative_flag():
    node = _node()
    keep = U._apply_dnc_guard(node, "no rule language at all here", _POLY_TEXT, _Log(), "G")
    assert keep is True and node["settlement_risk"] == "ufc_unparsed_settlement"


# --------------------------------------------------------------------------- #
# Pairing end-to-end + identity gate (winner vs Goes-the-Distance/KO/round)       #
# --------------------------------------------------------------------------- #
UFC_NOW = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)   # within 168h of the 26JUL18 card
_EVENT = "KXUFCFIGHT-26JUL18DUUSM"


# The real Kalshi title carries a FIXED event matchup clause (multi-word surname preserved), identical
# in both per-fighter markets: 'Will <Fighter> win the Du Plessis vs Usman professional MMA fight ...'.
_MATCHUP = "Du Plessis vs Usman"


def _kalshi_mkt(code, fighter):
    return {"event_ticker": _EVENT, "ticker": f"{_EVENT}-{code}", "yes_sub_title": fighter,
            "no_sub_title": fighter, "status": "active",
            "title": f"Will {fighter} win the {_MATCHUP} professional MMA fight scheduled for Jul 18, 2026?",
            "rules_primary": f"If {fighter} wins the fight, the market resolves to Yes.",
            "rules_secondary": ("If the fight is a tie or no contest it resolves 50/50. If the fight is "
                                "cancelled or rescheduled over two weeks away, the market will resolve to "
                                "a fair price.")}


def _fee():
    return {"feesEnabled": True, "feeSchedule": {"rate": 0.05, "takerOnly": True, "rebateRate": 0.15}}


def _pm(slug, question, git, outcomes, tokens, desc=""):
    d = {"slug": slug, "question": question, "groupItemTitle": git, "outcomes": outcomes,
         "clobTokenIds": tokens, "description": desc}
    d.update(_fee())
    return d


_ESLUG = "ufc-kam-dri-2026-07-18"
_POLY_EVENT = {
    "slug": _ESLUG, "startTime": "2026-07-18T21:00:00Z",
    "title": "UFC Fight Night: Kamaru Usman vs. Dricus Du Plessis (Middleweight, Main Card)",
    "description": _POLY_TEXT,
    "markets": [
        _pm(_ESLUG, "UFC Fight Night: Kamaru Usman vs. Dricus Du Plessis (Middleweight, Main Card)",
            "Kamaru Usman vs. Dricus Du Plessis", ["Kamaru Usman", "Dricus Du Plessis"],
            ["tok_u", "tok_d"], _POLY_TEXT),                                        # THE fight winner
        _pm(_ESLUG + "-go-the-distance", "Fight to Go the Distance?", "Fight to Go the Distance?",
            ["Yes", "No"], ["gd_y", "gd_n"]),                                       # excluded
        _pm(_ESLUG + "-win-by-ko-tko", "Will the fight be won by KO or TKO?", "Fight won by KO/TKO?",
            ["Yes", "No"], ["ko_y", "ko_n"]),                                       # excluded
        _pm(_ESLUG + "-totals-2pt5", "O/U 2.5 Rounds", "O/U 2.5 Rounds", ["Over", "Under"],
            ["r_o", "r_u"]),                                                        # excluded (round)
    ],
}


class _UFCKalshi:
    def iter_markets(self, *, series_ticker=None, status="open", **kw):
        if series_ticker == "KXUFCFIGHT":
            return [_kalshi_mkt("USM", "Kamaru Usman"), _kalshi_mkt("DU", "Dricus Du Plessis")]
        return []


class _UFCPoly:
    def events_by_series(self, sport, *, closed=False, **kw):
        return [_POLY_EVENT] if sport == "ufc" else []


def _build_ufc():
    from src.genz import tree_builder as tb
    return tb.build_tree(_UFCKalshi(), _UFCPoly(), load_genz_config(sport="ufc"),
                         now=UFC_NOW, spec=U.UFC_SPEC)


def test_ufc_build_pairs_fight_winner_only():
    tree = _build_ufc()
    g = tree["games"][_EVENT]
    assert g["sport"] == "ufc" and g["poly_match_method"] == "scan"
    assert g["kickoff_utc"] == "2026-07-18T21:00:00Z"
    types = {n["market_type"] for n in g["nodes"]}
    assert types == {"fight_winner"}                                       # ONLY the winner paired
    keys = {n["twin_key"] for n in g["nodes"]}
    assert keys == {"fight_winner|usman", "fight_winner|du plessis"}       # multi-word surname key
    du = next(n for n in g["nodes"] if n["side"] == "du plessis")
    assert du["poly_token_id"] == "tok_d" and du["kalshi_side"] == "YES"
    assert du["poly_fee_rate"] == 0.05 and du["poly_fee_enabled"] is True
    assert du["settlement_note"] == "dnc_50_50" and not du.get("settlement_risk")


def test_ufc_identity_gate_excludes_and_inventories(capsys):
    log = _Log()
    from src.genz import tree_builder as tb
    tb.build_tree(_UFCKalshi(), _UFCPoly(), load_genz_config(sport="ufc"),
                  now=UFC_NOW, spec=U.UFC_SPEC, log=log)
    # the Goes-the-Distance / KO-TKO / round markets are excluded + logged, and inventoried.
    assert any("excluded sub-market" in r and "Distance" in r for r in log.infos)
    assert any("excluded sub-market" in r and ("KO" in r or "TKO" in r) for r in log.infos)
    inv = [r for r in log.infos if "[UFC][INVENTORY]" in r]
    assert inv and "Go the Distance" in inv[0]


def test_ufc_live_node_fee_is_005():
    """The Poly fee on a fight-winner node is 0.05 (the live gamma shape: rate 0.05, takerOnly,
    rebateRate 0.15)."""
    g = _build_ufc()["games"][_EVENT]
    assert all(n["poly_fee_rate"] == 0.05 for n in g["nodes"])
