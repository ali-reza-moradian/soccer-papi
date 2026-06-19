"""STEP 5 — player-goals tier: Polymarket per-game player props <-> Kalshi KXWCGOAL.

Default OFF (config player_props.enabled). A per-player "anytime goalscorer" is a 2-way market:
  * "scores" (1+ goals)  — back it on whichever venue is dearer, and
  * "no goal" (0 goals)  — back it on the other,
and an arb exists when 1/scores_eff + 1/no_eff < 1, exactly like any other 2-way market.

SETTLEMENT — the reason this needs a stage gate. Kalshi KXWCGOAL settles INCLUDING extra time
("regulation, stoppage and any extra time periods"); Polymarket per-game player goals settle on
REG-90 ("the first 90 minutes of regular play plus stoppage time"). Those bases only coincide when
NO extra time is possible — i.e. GROUP-STAGE fixtures. For knockout fixtures (ET possible) a goal in
ET would win the Kalshi leg but lose the Poly leg, turning a "riskless" arb into real exposure, so
knockouts are SKIPPED (fail-closed). PER-GAME SCOPE ONLY: data is pulled solely from a game's Kalshi
KXWCGOAL event and the Polymarket '<game>-player-props' sibling — never tournament "to score in the
World Cup" markets.

Only the "1+ goals" line is paired (Kalshi "<player>: 1+" with Poly "<player>: 1+ goals" Yes/No);
multi-goal lines (2+, 3+) are ignored. Names are matched diacritics-folded, restricted to the two
games' rosters, with a surname fallback and ambiguous matches dropped (fail-closed). Nothing here
touches arbitrage.py.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Any, Optional

from . import catalog, kalshi as kalshi_mod, polymarket as poly_mod
from .arbitrage import Candidate, cap_total_investment, compute_arb, make_signature, select_legs
from .theoddsapi import match_event_to_fixture, normalize_team

# Default boundary: 2026 World Cup group stage runs through ~Jun 27; the Round of 32 (first knockout,
# extra time possible) begins after. Fixtures kicking off on/after this are treated as knockout and
# SKIPPED. Override with config player_props.knockout_start_utc.
DEFAULT_KNOCKOUT_START_UTC = "2026-06-28T00:00:00Z"

_PLAYER_THRESHOLD_RE = re.compile(r"^(?P<player>.+?):\s*(?P<n>\d+)\+", re.IGNORECASE)
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


# --------------------------------------------------------------------------- #
# Name normalization + roster-restricted matching                              #
# --------------------------------------------------------------------------- #
def normalize_player(name: Any) -> str:
    """Diacritics-folded, lowercased, punctuation-stripped name (Hložek -> hlozek, Éderson ->
    ederson). NFKD decomposition drops combining marks; non-alphanumerics collapse to single spaces."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^0-9a-zA-Z ]+", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()


def _surname(name_norm: str) -> str:
    """Last name token, ignoring trailing suffixes (jr/sr/ii…). '' if none."""
    toks = [t for t in name_norm.split() if t not in _SUFFIXES]
    return toks[-1] if toks else ""


def _tokens(name_norm: str) -> frozenset[str]:
    return frozenset(t for t in name_norm.split() if t not in _SUFFIXES)


def match_players(kalshi_norms: set[str], poly_norms: set[str]) -> dict[str, str]:
    """Map kalshi player-norm -> poly player-norm, restricted to these two rosters. Exact full-name
    matches first; then a conservative fallback for the SAME player written slightly differently
    (e.g. 'Neymar' vs 'Neymar Jr', 'Tomas Soucek' vs 'Tomas Soucek Jr'): the two names' token sets
    (suffixes dropped) must be nested AND share a surname. The match is taken ONLY when exactly one
    Poly candidate qualifies — any ambiguity (e.g. two 'Silva's) is dropped (fail-closed, never
    guess). Different first names with a shared surname ('Thiago Silva' vs 'Bruno Silva') never match."""
    out: dict[str, str] = {}
    remaining = set(poly_norms)
    for kn in kalshi_norms:
        if kn in remaining:
            out[kn] = kn
            remaining.discard(kn)
    for kn in kalshi_norms:
        if kn in out:
            continue
        kt = _tokens(kn)
        if not kt:
            continue
        cands = [pn for pn in remaining
                 if _surname(pn) == _surname(kn) and (kt <= _tokens(pn) or _tokens(pn) <= kt)]
        if len(cands) == 1:
            out[kn] = cands[0]
    return out


# --------------------------------------------------------------------------- #
# Stage tag + settlement gate                                                   #
# --------------------------------------------------------------------------- #
def fixture_stage(kickoff_iso: Any, knockout_start_iso: str = DEFAULT_KNOCKOUT_START_UTC) -> str:
    """'group_stage' if the fixture kicks off before knockout_start_utc, else 'knockout'. An
    unparseable kickoff is treated as 'knockout' (fail-closed -> skipped)."""
    from .theoddsapi import _parse_iso
    ko, start = _parse_iso(kickoff_iso), _parse_iso(knockout_start_iso)
    if ko is None or start is None:
        return "knockout"
    return "group_stage" if ko < start else "knockout"


def settlement_ok(stage: str) -> bool:
    """Kalshi (incl-ET) and Poly (reg-90) player-goal bases match ONLY when no ET is possible."""
    return stage == "group_stage"


# --------------------------------------------------------------------------- #
# Parsing the two venues' 1+ goal markets                                       #
# --------------------------------------------------------------------------- #
def _player_and_threshold(label: str) -> Optional[tuple[str, int]]:
    m = _PLAYER_THRESHOLD_RE.match(str(label or "").strip())
    if not m:
        return None
    try:
        return m.group("player").strip(), int(m.group("n"))
    except ValueError:
        return None


def parse_kalshi_goals(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Kalshi KXWCGOAL 1+ markets -> {player_norm: {raw, scores:(dec,lim), no_score:(dec,lim)}}.
    'scores' backs Yes (yes_ask); 'no_score' backs No (no_ask). Only the 1+ line is kept."""
    out: dict[str, dict[str, Any]] = {}
    for m in markets:
        if str(m.get("status") or "").lower() != "active":
            continue
        pt = _player_and_threshold(m.get("yes_sub_title") or m.get("title"))
        if pt is None or pt[1] != 1:
            continue
        player, _ = pt
        scores_dec = kalshi_mod.decimal_from_dollars(m.get("yes_ask_dollars"))
        no_dec = kalshi_mod.decimal_from_dollars(m.get("no_ask_dollars"))
        if scores_dec is None or no_dec is None:
            continue
        out[normalize_player(player)] = {
            "raw": player,
            "scores": (scores_dec, kalshi_mod.leg_limit(m.get("yes_ask_size_fp"), m.get("yes_ask_dollars"))),
            "no_score": (no_dec, kalshi_mod.leg_limit(m.get("no_ask_size_fp"), m.get("no_ask_dollars"))),
        }
    return out


def parse_poly_goal_tokens(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Poly '-player-props' 1+ GOAL markets -> {player_norm: {raw, yes_token, no_token}} (unpriced).
    Only goal markets (question/git mentions 'goal'), only the 1+ line."""
    out: dict[str, dict[str, Any]] = {}
    for m in (event.get("markets") or []):
        if not isinstance(m, dict):
            continue
        git = str(m.get("groupItemTitle") or "")
        q = str(m.get("question") or "")
        if "goal" not in git.lower() and "goal" not in q.lower():
            continue                                  # skip assists / shots
        pt = _player_and_threshold(git) or _player_and_threshold(q)
        if pt is None or pt[1] != 1:
            continue
        player, _ = pt
        yes, no = poly_mod._outcome_token(m, "yes"), poly_mod._outcome_token(m, "no")
        if yes and no:
            out[normalize_player(player)] = {"raw": player, "yes_token": yes, "no_token": no}
    return out


# --------------------------------------------------------------------------- #
# Arb construction for one matched player (2-way: scores / no goal)              #
# --------------------------------------------------------------------------- #
def _synthetic_mid(fixture_id: str, player_norm: str) -> int:
    h = hashlib.sha1(f"{fixture_id}|{player_norm}".encode("utf-8")).hexdigest()[:7]
    return 9_000_000 + (int(h, 16) % 900_000)


def build_player_arb(fixture_id: str, player_raw: str,
                     kalshi_scores: tuple[float, float], kalshi_no: tuple[float, float],
                     poly_scores: tuple[float, float], poly_no: tuple[float, float],
                     cfg, commission: dict[str, float]) -> Optional[tuple]:
    """Return (spec, arb_result) for one player's 2-way anytime-goalscorer arb, or None if it is not
    an arb above the ROI/stake floor. Outcome 1 = scores (1+), outcome 2 = no goal; each priced from
    BOTH venues so the engine picks the dearer side per outcome."""
    def _cand(oid, name, book, dec_lim):
        dec, lim = dec_lim
        return Candidate(outcome_id=oid, outcome_name=name, book=book, clone_group=book,
                         decimal_odds=dec, american_odds=None, limit=lim,
                         is_exchange=True, commission=commission.get(book, 0.0))

    chosen = select_legs({
        1: [_cand(1, "Scores 1+", "kalshi", kalshi_scores), _cand(1, "Scores 1+", "polymarket", poly_scores)],
        2: [_cand(2, "No goal", "kalshi", kalshi_no), _cand(2, "No goal", "polymarket", poly_no)],
    })
    if not chosen:
        return None
    res = compute_arb(chosen, cfg.assumed_unknown_limit, cfg.assumed_unknown_limit_by_book,
                      float(cfg.threshold("low_confidence_limit_floor", 10)))
    res = cap_total_investment(res, cfg.bankroll_total)
    if not res.is_arb or res.roi_pct < float(cfg.threshold("min_roi_pct", 0.5)):
        return None
    if res.t_max < float(cfg.threshold("min_total_stake", 20)) and not res.low_confidence:
        return None
    mid = _synthetic_mid(fixture_id, normalize_player(player_raw))
    spec = catalog.MarketSpec(market_id=mid, label=f"Anytime Goalscorer: {player_raw}",
                              family="player_goals", period="fulltime", line=0.5, n_way=2,
                              outcome_ids=[1, 2], outcome_names={1: "Scores 1+", 2: "No goal"})
    return spec, res


# --------------------------------------------------------------------------- #
# Orchestration (network) — discover both venues, gate by stage, pair          #
# --------------------------------------------------------------------------- #
def _kalshi_game_to_fixture(game_markets: list[dict[str, Any]], by_fixture: dict[str, dict[str, Any]],
                            tol_min: float, log) -> dict[str, str]:
    """KXWCGAME result markets -> {game_key: fixture_id} by matching the two team names (yes_sub_title,
    non-'Tie') + the ticker date to a canonical fixture — the join key the KXWCGOAL events share."""
    import collections
    by_gk: dict[str, list[dict]] = collections.defaultdict(list)
    for m in game_markets:
        by_gk[kalshi_mod._game_key(str(m.get("event_ticker") or ""))].append(m)
    out: dict[str, str] = {}
    for gk, ms in by_gk.items():
        ticker = next((str(m.get("event_ticker")) for m in ms if "-" in str(m.get("event_ticker") or "")), gk)
        commence = kalshi_mod._event_commence_iso(ticker)
        teams = [str(m.get("yes_sub_title")).strip() for m in ms
                 if str(m.get("yes_sub_title") or "").strip().lower() != "tie" and m.get("yes_sub_title")]
        if commence is None or len(teams) != 2:
            continue
        fm, _ = match_event_to_fixture(teams[0], teams[1], commence, by_fixture, tol_min)
        if fm is not None:
            out[gk] = fm.fixture_id
    return out


def _make_opp(fixture_id, info, spec, res, actionable, cfg):
    from .run import Opportunity      # lazy: avoid a circular import at module load
    p1, p2 = info.get("p1") or "", info.get("p2") or ""
    suspicious = res.roi_pct > float(cfg.threshold("roi_suspicious_pct", 8.0))
    sig = make_signature(fixture_id, spec.market_id, spec.line, res.legs)
    return Opportunity(
        fixture_id=fixture_id, match=f"{p1} vs {p2}", home_team=p1, away_team=p2,
        tournament=info.get("tournament") or "", kickoff_utc=info.get("start_time"), spec=spec,
        res=res, actionable=actionable, shadow_books=[] if actionable else ["kalshi", "polymarket"],
        suspicious=suspicious, bet_links={}, signature=sig)


def detect(cfg, by_fixture: dict[str, dict[str, Any]], now: datetime, log) -> list:
    """Discover Kalshi KXWCGOAL + Polymarket '-player-props' per in-window fixture, gate by stage
    (group-stage only), match players, and return player-goal arb Opportunities. Drop-safe and only
    invoked when player_props.enabled — heavier (extra Kalshi/Poly/CLOB calls)."""
    out: list = []
    knockout_start = str(cfg.player_props_opt("knockout_start_utc", DEFAULT_KNOCKOUT_START_UTC))
    actionable = bool(cfg.player_props_opt("actionable", False))   # shadow-first rollout
    tol = kalshi_mod._DAY_MATCH_TOLERANCE_MIN

    kc = kalshi_mod.KalshiClient(base_url=str(cfg.kalshi_opt("base_url", kalshi_mod.DEFAULT_BASE_URL)))
    gk_to_fid = _kalshi_game_to_fixture(
        kc.iter_markets(series_ticker=str(cfg.kalshi_opt("series_ticker", "KXWCGAME") or "KXWCGAME"),
                        status="open"), by_fixture, tol, log)
    import collections
    by_gk: dict[str, list[dict]] = collections.defaultdict(list)
    for m in kc.iter_markets(series_ticker=str(cfg.player_props_opt("kalshi_goal_series", "KXWCGOAL")),
                             status="open"):
        by_gk[kalshi_mod._game_key(str(m.get("event_ticker") or ""))].append(m)
    kalshi_by_fid: dict[str, dict] = {}
    for gk, fid in gk_to_fid.items():
        g = parse_kalshi_goals(by_gk.get(gk, []))
        if g:
            kalshi_by_fid[fid] = g
    if not kalshi_by_fid:
        log.info("[PLAYER-PROPS] no Kalshi KXWCGOAL fixtures mapped — nothing to pair.")
        return out

    pc = poly_mod.PolymarketClient(gamma_base=str(cfg.polymarket_opt("gamma_base", poly_mod.GAMMA_BASE)),
                                   clob_base=str(cfg.polymarket_opt("clob_base", poly_mod.CLOB_BASE)))
    slug_by_fid: dict[str, str] = {}
    for ev in poly_mod._discover_events(pc, by_fixture, log).values():
        parsed = poly_mod.parse_event_legs(ev)
        teams = [l.team for l in parsed.legs if l.role == "team" and l.team] if parsed else []
        if parsed is None or not parsed.slug or parsed.commence_iso is None or len(teams) != 2:
            continue
        fm, _ = match_event_to_fixture(teams[0], teams[1], parsed.commence_iso, by_fixture,
                                       poly_mod._DAY_MATCH_TOLERANCE_MIN)
        if fm is not None:
            slug_by_fid[fm.fixture_id] = parsed.slug

    n_group = n_knock = n_pair = 0
    for fid, kgoals in kalshi_by_fid.items():
        info = by_fixture.get(fid, {})
        stage = fixture_stage(info.get("start_time"), knockout_start)
        if not settlement_ok(stage):
            n_knock += 1
            log.info("[PLAYER-PROPS] %s vs %s: %s -> SKIP (Kalshi incl-ET vs Poly reg-90 settlement "
                     "mismatch).", info.get("p1"), info.get("p2"), stage)
            continue
        n_group += 1
        slug = slug_by_fid.get(fid)
        if not slug:
            continue
        try:
            sib = pc.events_by_slug(f"{slug}-player-props")
        except poly_mod.PolymarketError:
            continue
        sib = (sib[0] if isinstance(sib, list) and sib else sib)
        ptokens = parse_poly_goal_tokens(sib) if isinstance(sib, dict) else {}
        if not ptokens:
            continue
        for kn, pn in match_players(set(kgoals), set(ptokens)).items():
            ps = poly_mod.price_leg(pc, ptokens[pn]["yes_token"])
            pno = poly_mod.price_leg(pc, ptokens[pn]["no_token"])
            if ps is None or pno is None:
                continue
            built = build_player_arb(fid, kgoals[kn]["raw"], kgoals[kn]["scores"], kgoals[kn]["no_score"],
                                     ps, pno, cfg, cfg.commission)
            if built is None:
                continue
            spec, res = built
            out.append(_make_opp(fid, info, spec, res, actionable, cfg))
            n_pair += 1
    log.info("[PLAYER-PROPS] group-stage %s fixture(s), %s knockout skipped, %s player arb(s) "
             "(actionable=%s).", n_group, n_knock, n_pair, actionable)
    return out
