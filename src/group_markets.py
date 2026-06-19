"""Group-stage OUTCOME markets, cross-exchange (Kalshi <-> Polymarket). SCOPE_GROUP, NOT per-game.

A group market asks a group-stage-OUTCOME question about ONE team ("does <team> win / finish bottom
of / qualify from Group X"), resolved when the whole group finishes (~10 days out). Each is binary
Yes/No on both venues, so per team it is a 2-way market: "<team> X" (Yes) and "<team> not X" (No);
an arb backs the dearer Yes on one venue and the dearer No on the other.

SETTLEMENT CLASS (audited live — see the classification report). A pair is built ONLY when both
venues' resolution text is provably identical:
  * SAFE       — Group Winner ("finishes 1st, FIFA tiebreak") and Group Bottom ("finishes last/4th,
                 FIFA tiebreak"): both venues define them identically -> may be actionable.
  * DANGEROUS  — anything "qualify / advance / reach": Kalshi GROUPQUAL ("qualify ... for the Round
                 of 32") vs Poly "advance to Knockout Stage" vs Poly "reach Round of 16" do NOT share
                 one definition (advance-to-R32 != reach-R16, and the 8-best-thirds rule differs).
                 Detected and logged, but FORCED non-actionable always (fail-closed) until approved.

Everything is gated behind config group_markets.enabled (default OFF). Every group arb is tagged with
capital_lockup_days (days until the group stage ends) and that is surfaced in the alert + log, since
these freeze capital for ~10 days. Nothing here touches arbitrage.py; SAFE arbs flow through the
normal CSV/xlsx/alert path as ordinary Opportunities with a synthetic per-(group,team) market.
"""
from __future__ import annotations

import collections
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from . import catalog, group_resolution, kalshi as kalshi_mod, polymarket as poly_mod
from .arbitrage import Candidate, cap_total_investment, compute_arb, make_signature, select_legs
from .theoddsapi import SCOPE_GROUP, normalize_team

# group market name -> the outcome kind the clinch math understands ('winner'/'bottom'); other types
# (qualify) get schedule-based resolution only (no clinch).
_KIND = {"group_winner": "winner", "group_bottom": "bottom"}

DEFAULT_GROUP_STAGE_END_UTC = "2026-06-27T23:59:59Z"

SAFE = "SAFE"
DANGEROUS = "DANGEROUS"


@dataclass(frozen=True)
class GroupMarketType:
    name: str                     # "group_winner" | "group_bottom" | "group_qualify"
    kalshi_series: str            # KXWCGROUPWIN / KXWCGROUPBOTTOM / KXWCGROUPQUAL
    poly_slug_re: str             # regex over a Poly event slug to identify (group letter in group 1)
    settlement_class: str         # SAFE -> may be actionable; DANGEROUS -> never actionable
    label: str


# Only definition-identical pairs are SAFE. The DANGEROUS qualify pair is registered so it is
# detected + logged but NEVER actionable. (Second Place / Group Exact Order / Reach-R16 are NOT
# registered: each exists on only one venue, so there is nothing to pair -> skipped entirely.)
MARKET_TYPES = (
    GroupMarketType("group_winner", "KXWCGROUPWIN",
                    r"^world-cup-group-([a-l])-winner$", SAFE, "Group Winner"),
    GroupMarketType("group_bottom", "KXWCGROUPBOTTOM",
                    r"^world-cup-group-([a-l])-last-place\b", SAFE, "Group Bottom"),
    GroupMarketType("group_qualify", "KXWCGROUPQUAL",
                    r"^world-cup-team-to-advance-to-knockout-stages$", DANGEROUS, "Group Qualify (advance)"),
)


def actionable_for(mtype: GroupMarketType, group_actionable_cfg: bool) -> bool:
    """A group arb may be actionable ONLY if its settlement class is SAFE *and* the operator opted in
    (group_markets.actionable). DANGEROUS (qualify/advance/reach) is ALWAYS non-actionable — the
    config flag can never turn it on (fail-closed; settlement definitions differ across venues)."""
    return mtype.settlement_class == SAFE and bool(group_actionable_cfg)


def capital_lockup_days(now: datetime, group_stage_end_iso: str = DEFAULT_GROUP_STAGE_END_UTC) -> int:
    """Whole days from now until the group stage ends (when these markets resolve). >=0; 0 if past."""
    from .theoddsapi import _parse_iso
    end = _parse_iso(group_stage_end_iso)
    if end is None:
        return 0
    return max(0, int((end - now).total_seconds() // 86400))


# --------------------------------------------------------------------------- #
# Kalshi side: group-letter -> {team_norm: {raw, yes:(dec,lim), no:(dec,lim)}}   #
# --------------------------------------------------------------------------- #
_KGROUP_RE = re.compile(r"-26([A-L])\b")


def _kalshi_group_letter(event_ticker: str) -> Optional[str]:
    m = _KGROUP_RE.search(str(event_ticker or ""))
    return m.group(1).lower() if m else None


def parse_kalshi_group(markets: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """KXWCGROUP* markets -> {group_letter: {team_norm: {raw, yes:(dec,lim), no:(dec,lim)}}}.
    yes = back 'team is X' (yes_ask); no = back 'team is not X' (no_ask)."""
    out: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    for m in markets:
        if str(m.get("status") or "").lower() != "active":
            continue
        letter = _kalshi_group_letter(m.get("event_ticker"))
        team = str(m.get("yes_sub_title") or "").strip()
        if not letter or not team:
            continue
        yes = kalshi_mod.decimal_from_dollars(m.get("yes_ask_dollars"))
        no = kalshi_mod.decimal_from_dollars(m.get("no_ask_dollars"))
        if yes is None or no is None:
            continue
        out[letter][normalize_team(team)] = {
            "raw": team,
            "yes": (yes, kalshi_mod.leg_limit(m.get("yes_ask_size_fp"), m.get("yes_ask_dollars"))),
            "no": (no, kalshi_mod.leg_limit(m.get("no_ask_size_fp"), m.get("no_ask_dollars"))),
        }
    return out


# --------------------------------------------------------------------------- #
# Polymarket side: per-team Yes/No tokens in a group event                       #
# --------------------------------------------------------------------------- #
# Outcomes that are NOT a real team (tail buckets) — never paired.
_NON_TEAM = {"other", "country e", "no winner", "field", "none"}


def parse_poly_group_tokens(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A Poly group event -> {team_norm: {raw, yes_token, no_token}} (unpriced). Each market is a
    binary Yes/No on one team (groupItemTitle = team). Tail buckets ('Other', 'Country E') dropped."""
    out: dict[str, dict[str, Any]] = {}
    for m in (event.get("markets") or []):
        if not isinstance(m, dict):
            continue
        team = str(m.get("groupItemTitle") or "").strip()
        if not team or team.lower() in _NON_TEAM:
            continue
        yes, no = poly_mod._outcome_token(m, "yes"), poly_mod._outcome_token(m, "no")
        if yes and no:
            out[normalize_team(team)] = {"raw": team, "yes_token": yes, "no_token": no}
    return out


# --------------------------------------------------------------------------- #
# One team's 2-way cross-exchange arb (Yes/No)                                   #
# --------------------------------------------------------------------------- #
def _synthetic_mid(group_letter: str, type_name: str, team_norm: str) -> int:
    h = hashlib.sha1(f"{group_letter}|{type_name}|{team_norm}".encode("utf-8")).hexdigest()[:7]
    return 8_000_000 + (int(h, 16) % 900_000)


def build_group_arb(group_letter: str, mtype: GroupMarketType, team_raw: str,
                    kalshi_yes, kalshi_no, poly_yes, poly_no, cfg, commission) -> Optional[tuple]:
    """(spec, arb_result) for one team's group Yes/No arb, or None if not an arb above the floor.
    Outcome 1 = 'is X' (Yes), outcome 2 = 'not X' (No); each priced from BOTH venues."""
    def _cand(oid, name, book, dec_lim):
        dec, lim = dec_lim
        return Candidate(outcome_id=oid, outcome_name=name, book=book, clone_group=book,
                         decimal_odds=dec, american_odds=None, limit=lim,
                         is_exchange=True, commission=commission.get(book, 0.0))

    chosen = select_legs({
        1: [_cand(1, "Yes", "kalshi", kalshi_yes), _cand(1, "Yes", "polymarket", poly_yes)],
        2: [_cand(2, "No", "kalshi", kalshi_no), _cand(2, "No", "polymarket", poly_no)],
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
    mid = _synthetic_mid(group_letter, mtype.name, normalize_team(team_raw))
    spec = catalog.MarketSpec(market_id=mid, label=f"{mtype.label} {group_letter.upper()}: {team_raw}",
                              family=mtype.name, period="group", line=None, n_way=2,
                              outcome_ids=[1, 2], outcome_names={1: "Yes", 2: "No"})
    return spec, res


# --------------------------------------------------------------------------- #
# Orchestration (network) — discover both venues, classify, pair                #
# --------------------------------------------------------------------------- #
def _enumerate_poly_slugs(pc, tag_id: str, log) -> list[str]:
    """All open WC Poly event slugs (paginated). Used to find group events by slug pattern."""
    slugs: list[str] = []
    for off in range(0, 2000, 100):
        try:
            evs = pc._get(poly_mod.GAMMA_BASE + "/events",
                          {"tag_id": tag_id, "closed": "false", "limit": 100, "offset": off})
        except poly_mod.PolymarketError as exc:
            log.warning("[GROUP] Poly event enumeration failed (%s) — stopping.", exc)
            break
        evs = evs if isinstance(evs, list) else (evs.get("events") or [])
        if not evs:
            break
        slugs.extend(str(e.get("slug") or "") for e in evs if isinstance(e, dict))
    return slugs


def _make_opp(spec, res, group_letter, mtype, team_raw, est, fallback_lockup, fallback_end,
              actionable, cfg):
    """est = group_resolution.resolution_estimate(...) or None (schedule unknown -> coarse fallback)."""
    from .run import Opportunity
    suspicious = res.roi_pct > float(cfg.threshold("roi_suspicious_pct", 8.0))
    sig = make_signature(f"group-{group_letter}-{mtype.name}", spec.market_id, None, res.legs)
    resolves_by = est["resolves_by_utc"] if est else fallback_end
    opp = Opportunity(
        fixture_id=f"group-{group_letter}-{mtype.name}-{normalize_team(team_raw)}",
        match=f"Group {group_letter.upper()}: {team_raw} — {mtype.label}",
        home_team=team_raw, away_team="", tournament="World Cup (group stage)",
        kickoff_utc=resolves_by, spec=spec, res=res, actionable=actionable,
        shadow_books=[] if actionable else ["kalshi", "polymarket"], suspicious=suspicious,
        bet_links={}, signature=sig)
    opp.capital_lockup_days = est["resolves_by_days"] if est else fallback_lockup
    opp.resolves_by_utc = resolves_by
    opp.early_resolution = bool(est and est["early"])
    opp.early_within_window = bool(est and est["within_window"])
    return opp


def detect(cfg, now: datetime, log) -> list:
    """Discover Kalshi + Polymarket group-outcome markets, classify by settlement, and return group
    arb Opportunities. SAFE types may be actionable (config); DANGEROUS types are detected + logged
    but FORCED non-actionable. Default OFF; drop-safe."""
    out: list = []
    actionable_safe = bool(cfg.group_markets_opt("actionable", False))
    group_end = str(cfg.group_markets_opt("group_stage_end_utc", DEFAULT_GROUP_STAGE_END_UTC))
    lockup = capital_lockup_days(now, group_end)

    kc = kalshi_mod.KalshiClient(base_url=str(cfg.kalshi_opt("base_url", kalshi_mod.DEFAULT_BASE_URL)))
    pc = poly_mod.PolymarketClient(gamma_base=str(cfg.polymarket_opt("gamma_base", poly_mod.GAMMA_BASE)),
                                   clob_base=str(cfg.polymarket_opt("clob_base", poly_mod.CLOB_BASE)))
    all_slugs = _enumerate_poly_slugs(pc, str(cfg.group_markets_opt("poly_tag_id", "102232")), log)

    # Per-group resolution schedule: KXWCGAME match dates (ticker) + results (settled markets).
    game_series = str(cfg.kalshi_opt("series_ticker", "KXWCGAME") or "KXWCGAME")
    game_markets = (kc.iter_markets(series_ticker=game_series, status="open")
                    + kc.iter_markets(series_ticker=game_series, status="settled"))
    games = group_resolution.parse_game_schedule(game_markets)
    within_days = cfg.alert_max_days_out or 3

    n_safe = n_danger = 0
    for mt in MARKET_TYPES:
        kgroups = parse_kalshi_group(kc.iter_markets(series_ticker=mt.kalshi_series, status="open"))
        if not kgroups:
            continue
        rx = re.compile(mt.poly_slug_re)
        per_group: dict[str, dict] = {}
        global_tokens: dict[str, dict] = {}
        for slug in all_slugs:
            m = rx.match(slug)
            if not m:
                continue
            try:
                ev = pc.events_by_slug(slug)
            except poly_mod.PolymarketError:
                continue
            ev = (ev[0] if isinstance(ev, list) and ev else ev)
            toks = parse_poly_group_tokens(ev) if isinstance(ev, dict) else {}
            if m.groups():
                per_group[m.group(1).lower()] = toks
            else:
                global_tokens.update(toks)               # group-agnostic event (one 48-team list)

        for letter, kteams in kgroups.items():
            ptoks = per_group.get(letter) or global_tokens
            for tnorm, kd in kteams.items():
                if tnorm not in ptoks:                    # exact team match within the SAME group
                    continue
                py = poly_mod.price_leg(pc, ptoks[tnorm]["yes_token"])
                pno = poly_mod.price_leg(pc, ptoks[tnorm]["no_token"])
                if py is None or pno is None:
                    continue
                built = build_group_arb(letter, mt, kd["raw"], kd["yes"], kd["no"], py, pno, cfg, cfg.commission)
                if built is None:
                    continue
                spec, res = built
                actionable = actionable_for(mt, actionable_safe)   # DANGEROUS -> always False
                est = group_resolution.resolution_estimate(
                    frozenset(kteams), _KIND.get(mt.name, mt.name), tnorm, games, now, within_days)
                out.append(_make_opp(spec, res, letter, mt, kd["raw"], est, lockup, group_end,
                                     actionable, cfg))
                if mt.settlement_class == SAFE:
                    n_safe += 1
                else:
                    n_danger += 1
                    log.info("[GROUP] DANGEROUS %s %s/%s arb detected -> LOGGED, NEVER actionable "
                             "(settlement unverified).", mt.label, letter.upper(), kd["raw"])
    log.info("[GROUP] SAFE arbs %s (actionable=%s), DANGEROUS logged-only %s, capital lockup ~%sd.",
             n_safe, actionable_safe, n_danger, lockup)
    return out
