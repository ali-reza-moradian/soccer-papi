"""Multi-sport adapter tests.

The FIRST job here is the byte-identical LOCK on the SoccerSpec extraction: a soccer tree built
through the new sport-adapter seam (``build_tree(..., spec=SoccerSpec())`` and the default path) must
match a committed golden EXACTLY, so the refactor provably did not change soccer output. The fixture
uses deterministic tickers (no hash()) so the golden is reproducible in CI.
"""
from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timezone

from src.genz import tree_builder as tb
from src.genz.config import GenzConfig

NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
SUF = "26JUN30CIVNOR"
BASE = "fifwc-civ-nor-2026-06-30"
GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "soccer_tree_golden.json")


# -- deterministic soccer fixture (stable tickers -> reproducible golden) ------------------------- #
def _kalshi_markets():
    c = itertools.count(1)

    def km(series, sub, title=None):
        et = f"{series}-{SUF}"
        return {"event_ticker": et, "ticker": f"{et}-T{next(c)}",
                "yes_sub_title": sub, "title": title if title is not None else sub, "status": "active"}

    game = [km("KXWCGAME", "Norway"), km("KXWCGAME", "Tie"), km("KXWCGAME", "Ivory Coast")]
    return game, {
        f"KXWCGAME-{SUF}": game,
        f"KXWCTOTAL-{SUF}": [km("KXWCTOTAL", "Over 1.5 goals"), km("KXWCTOTAL", "Over 2.5 goals"),
                             km("KXWCTOTAL", "Over 3.5 goals")],
    }


def _single(git, tok):
    return {"groupItemTitle": git, "question": f"Will {git}?", "outcomes": ["Yes", "No"],
            "clobTokenIds": [tok, f"{tok}_no"]}


def _ou(git, over, under):
    return {"groupItemTitle": git, "outcomes": ["Over", "Under"], "clobTokenIds": [over, under]}


def _ev(slug, markets):
    return {"id": slug, "slug": slug, "closed": False, "eventDate": "2026-06-30", "title": slug,
            "markets": markets}


def _series():
    return [
        _ev(BASE, [_single("Norway", "nor_y"), _single("Côte d'Ivoire", "civ_y"),
                   {"groupItemTitle": "Draw (Côte d'Ivoire vs. Norway)", "question": "draw?",
                    "outcomes": ["Yes", "No"], "clobTokenIds": ["drw_y", "drw_n"]}]),
        _ev(f"{BASE}-more-markets", [_ou("O/U 1.5", "ov15", "un15"), _ou("O/U 2.5", "ov25", "un25")]),
    ]


class _Kalshi:
    def __init__(self):
        self._game, self._by_event = _kalshi_markets()

    def iter_markets(self, *, series_ticker=None, status="open", limit=100, max_pages=50):
        return list(self._game) if series_ticker == "KXWCGAME" else []

    def markets(self, *, series_ticker=None, event_ticker=None, status="open", limit=100, cursor=None):
        return {"markets": list(self._by_event.get(event_ticker, []))}


class _Poly:
    def events_by_series(self, series_slug, *, closed=False, page_limit=100, max_pages=50):
        return _series()


def _build(spec=None):
    return tb.build_tree(_Kalshi(), _Poly(), GenzConfig(), now=NOW, spec=spec)


# --------------------------------------------------------------------------- #
# Byte-identical SoccerSpec extraction lock                                     #
# --------------------------------------------------------------------------- #
def test_soccer_tree_byte_identical_to_golden():
    """The default build_tree (soccer) must serialize byte-for-byte to the committed golden."""
    out = json.dumps(_build(), ensure_ascii=False, indent=2)
    with open(GOLDEN, encoding="utf-8") as fh:
        assert out == fh.read()


def test_soccer_spec_equals_default_path():
    """Passing SoccerSpec() explicitly is identical to the default — the seam adds no behavior."""
    assert _build(spec=tb.SoccerSpec()) == _build()


def test_soccer_spec_advertises_soccer_paths():
    sp = tb.SoccerSpec().paths()
    assert sp.sport == "soccer"
    assert sp.tree_path.endswith("match_tree.json") and sp.snapshot_path.endswith("genz_snapshot.json")


# =========================================================================== #
# MLB adapter (src/genz/sports_mlb.py)                                          #
# =========================================================================== #
from src.genz import sports_mlb as M   # noqa: E402
from src.genz import engine as E       # noqa: E402
from src.genz import papermaker as PM  # noqa: E402
from src.genz.config import load_genz_config   # noqa: E402

MLB_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)   # NYM@PHI first pitch 23:10Z (within 24h)


# -- crosswalk + rule parser ---------------------------------------------------------------------- #
def test_all_30_teams_crosswalk_round_trips():
    assert len(M._TEAMS) == 30
    for kcode, pcode, name in M._TEAMS:
        assert M.poly_code(kcode) == pcode                     # kalshi code -> poly code
        assert M._FULL_NAME[kcode] == name
        assert M._NAME_TO_KCODE[M._norm(name)] == kcode        # full name -> kalshi code (titles are truth)


def test_learned_map_overrides_guess():
    learned = {"ATH": "oak"}
    assert M.poly_code("ATH", learned) == "oak"                # learned wins
    assert M.poly_code("ATH") == "ath"                         # hardcoded guess otherwise
    assert M.poly_code("CHW", learned) == "cws"                # alias CHW -> CWS -> cws


def test_parse_mlb_shortened_rule_fixtures():
    assert M.parse_mlb_shortened_rule("game abandoned before 9 innings; last fair market price") == "void_short"
    assert M.parse_mlb_shortened_rule("resolves on the official final statistics of the event") == "official_result"
    # cancellation-only 'fair price' must NOT be read as void_short (so moneyline stays aligned)
    assert M.parse_mlb_shortened_rule("if cancelled the market resolves to a fair price per rules") is None
    assert M.parse_mlb_shortened_rule("collectively score more than 8.5 runs") is None


def test_ticker_decode_and_away_home_from_market_tickers():
    assert M._decode_event_suffix("26JUL161910NYMPHI") == ("2026-07-16", "1910", "")
    assert M._decode_event_suffix("26JUL171335TBBOSG1") == ("2026-07-17", "1335", "G1")
    gm = [{"ticker": "KXMLBGAME-26JUL161910NYMPHI-PHI"}, {"ticker": "KXMLBGAME-26JUL161910NYMPHI-NYM"}]
    assert M._codes_from_markets(gm, "NYMPHI") == ("NYM", "PHI")   # away prefixes the concat, home suffixes
    assert M._et_to_utc("2026-07-16", "1910") == "2026-07-16T23:10:00Z"   # EDT -> UTC


def test_unresolvable_codes_returns_none():
    # tickers whose codes don't reconstruct the concat -> None -> discovery logs + skips
    gm = [{"ticker": "KXMLBGAME-X-AAA"}, {"ticker": "KXMLBGAME-X-BBB"}]
    assert M._codes_from_markets(gm, "NYMPHI") is None


# -- doubleheader: +-90min startTime gate --------------------------------------------------------- #
def test_doubleheader_pairs_by_start_time_window():
    """Two Kalshi games (G1 13:35 ET / G2 19:10 ET) vs ONE poly event at 23:10Z: only the game within
    90 min pairs; the other is refused (one-sided) — never mispaired to the wrong game."""
    poly_ev = {"slug": "mlb-tb-bos-2026-07-17", "startTime": "2026-07-17T23:10:00Z",
               "title": "Tampa Bay Rays vs. Boston Red Sox", "markets": []}

    class _P:
        def events_by_slug(self, slug):
            return [poly_ev]

    g1 = M.MLBGame("26JUL171335TBBOSG1", "26JUL171335TBBOSG1", "2026-07-17", "TB", "BOS",
                   "Tampa Bay Rays", "Boston Red Sox", "2026-07-17T17:35:00Z", dh="G1")
    g2 = M.MLBGame("26JUL171910TBBOSG2", "26JUL171910TBBOSG2", "2026-07-17", "TB", "BOS",
                   "Tampa Bay Rays", "Boston Red Sox", "2026-07-17T23:10:00Z", dh="G2")
    assert M.resolve_poly_event(g1, [poly_ev], {}, _P()) is None          # 17:35 vs 23:10 -> refused
    assert M.resolve_poly_event(g2, [poly_ev], {}, _P()) is poly_ev       # 23:10 vs 23:10 -> paired


# -- end-to-end MLB build (fake clients mirroring the live shapes) --------------------------------- #
_MLB_SUF = "26JUL161910NYMPHI"


def _kg(code, sub, text):
    return {"event_ticker": f"KXMLBGAME-{_MLB_SUF}", "ticker": f"KXMLBGAME-{_MLB_SUF}-{code}",
            "yes_sub_title": sub, "title": "New York M vs Philadelphia Winner?", "status": "open",
            "rules_primary": text}


def _kt(idx, line, text):
    return {"event_ticker": f"KXMLBTOTAL-{_MLB_SUF}", "ticker": f"KXMLBTOTAL-{_MLB_SUF}-{idx}",
            "yes_sub_title": f"Over {line} runs scored", "title": "New York M vs Philadelphia Total Runs?",
            "status": "open", "rules_primary": text}


_MLB_GAME = [_kg("NYM", "New York M", "If New York M wins the game resolves Yes."),
             _kg("PHI", "Philadelphia", "If Philadelphia wins the game resolves Yes.")]
_MLB_TOTAL = [_kt(8, 8.5, "game abandoned before 9 innings uses last fair market price"),   # void_short
              _kt(9, 9.5, "collectively score more than 9.5 runs")]                          # None -> risk


def _fee(rate=0.05):
    return {"feesEnabled": True, "feeSchedule": {"rate": rate}}


# REAL-shaped Poly MLB markets (verified live 2026-07): moneyline groupItemTitle is null + the
# question is '<away> vs. <home>'; a full-game total is groupItemTitle 'O/U 8.5' + question
# '<away> vs. <home>: O/U 8.5'. The identity gate keys on exactly these fields.
_MLB_TEAMS_TITLE = "New York Mets vs. Philadelphia Phillies"


def _slug_line(line):
    return str(line).replace(".", "pt")


def _pm_ml(tokens=("tok_nym", "tok_phi")):
    d = {"groupItemTitle": None, "question": _MLB_TEAMS_TITLE,
         "outcomes": ["New York Mets", "Philadelphia Phillies"], "clobTokenIds": list(tokens),
         "slug": "mlb-nym-phi-2026-07-16", "description": "moneyline"}
    d.update(_fee())
    return d


def _pm_total(line, over_tok, under_tok, desc, *, f5=False):
    frag = f"1st 5 Innings O/U {line}" if f5 else f"O/U {line}"
    slugpart = ("f5-total-" if f5 else "total-") + _slug_line(line)
    d = {"groupItemTitle": frag, "question": f"{_MLB_TEAMS_TITLE}: {frag}",
         "outcomes": ["Over", "Under"], "clobTokenIds": [over_tok, under_tok],
         "slug": f"mlb-nym-phi-2026-07-16-{slugpart}", "description": desc}
    d.update(_fee())
    return d


_POLY_MLB_EVENT = {
    "slug": "mlb-nym-phi-2026-07-16", "startTime": "2026-07-16T23:10:00Z",
    "title": "New York Mets vs. Philadelphia Phillies",
    "markets": [
        _pm_ml(),
        _pm_total(8.5, "o85", "u85", "resolves on the official final statistics of the event"),  # official
        _pm_total(10.5, "o105", "u105", "official final statistics"),   # full-game, poly-only line
    ],
}


class _MlbKalshi:
    def iter_markets(self, *, series_ticker=None, status="open", limit=100, max_pages=50):
        if series_ticker == "KXMLBGAME":
            return list(_MLB_GAME)
        if series_ticker == "KXMLBTOTAL":
            return list(_MLB_TOTAL)
        return []


class _MlbPoly:
    def events_by_series(self, series_slug, *, closed=False, page_limit=100, max_pages=50):
        return [_POLY_MLB_EVENT]

    def events_by_slug(self, slug):
        return [_POLY_MLB_EVENT] if slug == "mlb-nym-phi-2026-07-16" else []


def _build_mlb():
    cfg = load_genz_config(sport="mlb")
    return tb.build_tree(_MlbKalshi(), _MlbPoly(), cfg, now=MLB_NOW, spec=M.MLB_SPEC)


def test_mlb_build_moneyline_and_totals_pairing():
    tree = _build_mlb()
    g = tree["games"][_MLB_SUF]
    assert g["away"] == "New York Mets" and g["home"] == "Philadelphia Phillies"
    assert g["kickoff_utc"] == "2026-07-16T23:10:00Z" and g["sport"] == "mlb"
    by = {}
    for n in g["nodes"]:
        by.setdefault(n["market_type"], []).append(n)
    # moneyline: 2 nodes (away/home), aligned tokens, NOT settlement_risk
    ml = {n["side"]: n for n in by["ml2"]}
    assert set(ml) == {"away", "home"}
    assert ml["away"]["poly_token_id"] == "tok_nym" and ml["away"]["kalshi_side"] == "YES"
    assert not any(n.get("settlement_risk") for n in by["ml2"])
    # totals: only the SHARED line 8.5 pairs (kalshi 9.5 + poly 10.5 are one-venue-only -> unmatched)
    lines = {n["line"] for n in by["total_runs"]}
    assert lines == {8.5}
    over = next(n for n in by["total_runs"] if n["side"] == "over")
    assert over["poly_token_id"] == "o85" and over["kalshi_side"] == "YES"
    assert {u["market_type"] for u in g["unmatched"]} == {"total_runs"}


def test_mlb_totals_carry_poly_fee_and_rain_guard():
    g = _build_mlb()["games"][_MLB_SUF]
    tr = [n for n in g["nodes"] if n["market_type"] == "total_runs"]
    assert all(n["poly_fee_rate"] == 0.05 and n["poly_fee_enabled"] for n in tr)   # gamma fee payload
    # kalshi void_short + poly official_result -> the rain-rule flag, with BOTH raw texts stored
    over = next(n for n in tr if n["side"] == "over")
    assert over["settlement_risk"] == "mlb_rain_rule"
    assert over["kalshi_rule"] == "void_short" and over["poly_rule"] == "official_result"
    assert "abandoned" in over["settlement_texts"]["kalshi"]
    assert "official" in over["settlement_texts"]["poly"]


def test_mlb_fee_math_parity_on_a_node():
    """A poly leg with fees enabled charges rate*min(p,1-p) per share — same formula the paper maker and
    executor use — so an MLB total node's fee matches an independent computation."""
    g = _build_mlb()["games"][_MLB_SUF]
    over = next(n for n in g["nodes"] if n["market_type"] == "total_runs" and n["side"] == "over")
    p = 0.45
    expected = over["poly_fee_rate"] * min(p, 1 - p)
    assert abs(PM.hedge_fee_rate("polymarket", p, over["poly_fee_rate"]) - expected) < 1e-9


# -- engine integration: settlement_risk excluded from would_trade AND the paper maker ------------- #
class _CheapMD:
    """Every leg prices at 0.48 -> a 0.96 over+under arb, enough to reach the guards."""
    def kalshi_ask_ladder(self, ticker, side="YES"):
        return [(0.48, 1000.0)]

    def poly_ask_ladder(self, token_id):
        return [(0.48, 1000.0)]


def test_mlb_settlement_risk_excluded_from_would_trade_and_papermaker(tmp_path):
    tree = _build_mlb()
    markets = E.collect_markets(tree)
    total_mkt = next(m for m in markets if m.market_type == "total_runs")
    ml_mkt = next(m for m in markets if m.market_type == "ml2")
    # the paper maker must skip the rain-risk totals market, but not the moneyline
    assert PM._market_settlement_risk(total_mkt) is True
    assert PM._market_settlement_risk(ml_mkt) is False
    assert E._market_settlement_risk(total_mkt) == "mlb_rain_rule"
    # run a cycle: the total_runs arb is rejected as settlement_risk (never would_trade)
    exec_cfg = __import__("src.executor.config", fromlist=["load_exec_config"]).load_exec_config()
    res = E.run_cycle(tree, _CheapMD(), load_genz_config(sport="mlb"), exec_cfg, now=MLB_NOW,
                      write=False, log=None)
    trow = next(r for r in res.rows if r.get("market_key", "").startswith("total_runs"))
    assert trow["exec_status"] == "rejected_settlement_risk" and not trow["would_trade"]


def test_mlb_snapshot_carries_sport_and_settlement_risk():
    tree = _build_mlb()
    markets = E.collect_markets(tree)
    priced = E.price_markets(_CheapMD(), markets, load_genz_config(sport="mlb"))
    snap = E.build_snapshot(tree, markets, priced, {}, MLB_NOW, sport="mlb")
    assert snap["sport"] == "mlb" and snap["schema"] == 4          # schema 4: in-play phase
    g = snap["games"][_MLB_SUF]
    assert g["phase"] == "pre"                                     # MLB_NOW is before first pitch
    tr = next(m for m in g["markets"] if m["market_type"] == "total_runs")
    assert tr["settlement_risk"] == "mlb_rain_rule" and tr["settlement_texts"] is not None
    assert tr["phase"] == "pre" and tr["inplay"] is False


# -- IDENTITY BEFORE SHAPE: exclude F5/run-line/NRFI sub-markets; keep ML + full-game totals --------- #
class _CaptureLog:
    def __init__(self):
        self.infos, self.warnings = [], []

    def info(self, fmt, *a):
        self.infos.append(fmt % a if a else fmt)

    def warning(self, fmt, *a):
        self.warnings.append(fmt % a if a else fmt)


def _mlb_game_nym_phi():
    return M.MLBGame("26JUL161910NYMPHI", "26JUL161910NYMPHI", "2026-07-16", "NYM", "PHI",
                     "New York Mets", "Philadelphia Phillies", "2026-07-16T23:10:00Z")


def _mk_market(git, question, outcomes, tokens, slug):
    d = {"groupItemTitle": git, "question": question, "outcomes": outcomes, "clobTokenIds": tokens,
         "slug": slug, "description": ""}
    d.update(_fee())
    return d


def test_poly_identity_keeps_ml_and_full_game_totals_excludes_the_rest():
    """A real-shaped Poly game event (ML + full-game totals + F5 totals + a 3-outcome F5 moneyline +
    run-line spread + NRFI): ONLY the moneyline (ml2 x2) and the full-game totals survive; every
    F5/spread/NRFI sub-market is excluded and logged."""
    game = _mlb_game_nym_phi()
    base = "mlb-nym-phi-2026-07-16"
    event = {"markets": [
        _mk_market(None, _MLB_TEAMS_TITLE, ["New York Mets", "Philadelphia Phillies"],
                   ["ml_a", "ml_h"], base),                                              # moneyline
        _pm_total(8.5, "o85", "u85", "official final statistics"),                        # full-game total
        _pm_total(9.5, "o95", "u95", "official final statistics"),                        # full-game total
        _pm_total(3.5, "f5o", "f5u", "by the conclusion of the 5th inning", f5=True),     # F5 total -> excluded
        _mk_market("1st 5 Innings", f"1st 5 Innings Moneyline: {_MLB_TEAMS_TITLE}",       # F5 ML (3-way) -> excluded
                   ["New York Mets", "Tie", "Philadelphia Phillies"], ["f5a", "f5t", "f5h"],
                   f"{base}-f5-moneyline"),
        _mk_market("Spread -1.5", "Spread: Philadelphia Phillies (-1.5)",                 # run line -> excluded
                   ["Philadelphia Phillies", "New York Mets"], ["sp_h", "sp_a"], f"{base}-spread-home-1pt5"),
        _mk_market("NRFI", f"Will there be a run scored in the first inning?: {_MLB_TEAMS_TITLE}",
                   ["Yes", "No"], ["nr_y", "nr_n"], f"{base}-nrfi"),                       # NRFI -> excluded
    ]}
    log = _CaptureLog()
    opts = M.poly_mlb_options(event, game, log=log, game_id=game.game_id)
    assert set(opts) == {"ml2|away", "ml2|home",
                         "total_runs|8.5|over", "total_runs|8.5|under",
                         "total_runs|9.5|over", "total_runs|9.5|under"}
    assert opts["ml2|away"]["poly_token_id"] == "ml_a" and opts["ml2|home"]["poly_token_id"] == "ml_h"
    assert opts["total_runs|8.5|over"]["poly_token_id"] == "o85"
    # the surviving totals still carry the gamma fee plumbing
    assert opts["total_runs|9.5|over"]["poly_fee_rate"] == 0.05 and opts["total_runs|9.5|over"]["poly_fee_enabled"]
    excluded = "\n".join(log.infos)
    assert "excluded sub-market" in excluded
    assert any("1st 5" in r for r in log.infos)          # F5 total + F5 ML
    assert any("Spread" in r for r in log.infos)          # run line
    assert any("NRFI" in r.upper() for r in log.infos)    # NRFI


def test_poly_identity_collision_drops_ambiguous_line():
    """Two surviving full-game markets that map to the SAME line are ambiguous — the whole key is
    dropped (never guessed) with a WARNING; other lines are unaffected."""
    game = _mlb_game_nym_phi()
    event = {"markets": [
        _pm_total(8.5, "a_o", "a_u", "official final statistics"),
        _pm_total(8.5, "b_o", "b_u", "official final statistics"),   # duplicate line -> collision
        _pm_total(9.5, "c_o", "c_u", "official final statistics"),   # unique -> survives
    ]}
    log = _CaptureLog()
    opts = M.poly_mlb_options(event, game, log=log, game_id="26JUL161910NYMPHI")
    assert "total_runs|8.5|over" not in opts and "total_runs|8.5|under" not in opts
    assert "total_runs|9.5|over" in opts and "total_runs|9.5|under" in opts
    assert any("collision" in w.lower() for w in log.warnings)


# -- DOUBLEHEADER explicit slug order (G1 base-first, G2 dh2-first) --------------------------------- #
def test_dh_slug_order_g1_base_first_g2_dh2_first():
    ev_base = {"slug": "mlb-tb-bos-2026-07-17", "startTime": "2026-07-17T17:35:00Z",
               "title": "Tampa Bay Rays vs. Boston Red Sox", "markets": []}
    ev_dh2 = {"slug": "mlb-tb-bos-2026-07-17-dh2", "startTime": "2026-07-17T23:10:00Z",
              "title": "Tampa Bay Rays vs. Boston Red Sox", "markets": []}

    class _P:
        def __init__(self):
            self.tried = []

        def events_by_slug(self, slug):
            self.tried.append(slug)
            if slug == ev_base["slug"]:
                return [ev_base]
            if slug == ev_dh2["slug"]:
                return [ev_dh2]
            return []

    g1 = M.MLBGame("26JUL171335TBBOSG1", "26JUL171335TBBOSG1", "2026-07-17", "TB", "BOS",
                   "Tampa Bay Rays", "Boston Red Sox", "2026-07-17T17:35:00Z", dh="G1")
    p1 = _P()
    assert M.resolve_poly_event(g1, [], {}, p1, log=_CaptureLog()) is ev_base
    assert p1.tried[0] == "mlb-tb-bos-2026-07-17"                     # G1 tries base FIRST

    g2 = M.MLBGame("26JUL171910TBBOSG2", "26JUL171910TBBOSG2", "2026-07-17", "TB", "BOS",
                   "Tampa Bay Rays", "Boston Red Sox", "2026-07-17T23:10:00Z", dh="G2")
    p2 = _P()
    assert M.resolve_poly_event(g2, [], {}, p2, log=_CaptureLog()) is ev_dh2
    assert p2.tried[0] == "mlb-tb-bos-2026-07-17-dh2"                 # G2 tries -dh2 FIRST


def test_dh_logs_accepted_and_refused_per_candidate():
    """Every DH game logs a '[MLB][DH] ... (accepted|refused Δmin=<n>)' line for each candidate slug."""
    ev = {"slug": "mlb-tb-bos-2026-07-17", "startTime": "2026-07-17T23:10:00Z",
          "title": "Tampa Bay Rays vs. Boston Red Sox", "markets": []}

    class _P:
        def events_by_slug(self, slug):
            return [ev]                      # every candidate resolves to the same event

    g1 = M.MLBGame("26JUL171335TBBOSG1", "26JUL171335TBBOSG1", "2026-07-17", "TB", "BOS",
                   "Tampa Bay Rays", "Boston Red Sox", "2026-07-17T17:35:00Z", dh="G1")
    log = _CaptureLog()
    assert M.resolve_poly_event(g1, [], {}, _P(), log=log) is None    # 17:35 vs 23:10 -> refused both
    dh_lines = [r for r in log.infos if "[MLB][DH]" in r]
    assert dh_lines and all("refused" in r and "Δmin=" in r for r in dh_lines)
