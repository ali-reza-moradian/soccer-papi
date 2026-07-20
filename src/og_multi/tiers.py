"""Tier A + Tier B arb assembly for the OG multi-sport scanner (spec sections 3-4).

Tier A — enrich the settlement-clean WINNER/TOTAL tree families with book legs. Per outcome the
candidates are {kalshi, polymarket (live exchange asks, walked), pinnacle, 1xbet (the-odds-api, flat)}
and ``compute_arb`` picks best-of-book across every present book (>=2 required). Settlement chips
INHERIT from the tree node (rain / walkover / draw-NC carry through to the OG row). Only families with
a real the-odds-api book counterpart are enriched — ``ml2``/``total_runs`` (MLB), ``match_winner``
(tennis), ``fight_winner`` (UFC); families with no book twin (go_the_distance, method, …) stay
exchange-only (GenZ territory) and are counted in inventory, per the spec.

Tier B — book-only families paired pinnacle<->1xbet with NO exchange twin: MLB run_line (complementary
+-1.5 spreads) + off-tree totals; tennis game_spread / game_total (same line on both books); UFC
round_total. Every Tier B row carries ``tier:'B'`` + ``rules_unverified:true`` (the panel's 'BOOK
RULES UNVERIFIED' chip) and is NEVER merged with an exchange leg.

Rows are byte-shaped like the soccer ``og_current.json`` row (so the panel renders them identically),
plus the additive ``tier`` / ``family`` / ``settlement`` / ``rules_unverified`` fields. Pure — no
network: the exchange ask ladders are fetched once by scan.py and passed in via ``ladders``.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .. import bookmath
from ..arbitrage import Candidate, compute_arb, select_legs
from ..theoddsapi import canonical_book_slug
from . import match, sizing

# Families Tier A enriches with book legs (they have a the-odds-api counterpart), per sport.
ENRICH_FAMILIES: dict[str, set[str]] = {
    "mlb": {"ml2", "total_runs"}, "tennis": {"match_winner"}, "ufc": {"fight_winner"},
}
# Which the-odds-api bulk market backs each enrich family.
_TOA_MARKET: dict[str, str] = {"ml2": "h2h", "match_winner": "h2h", "fight_winner": "h2h",
                               "total_runs": "totals"}
ACTIONABLE_BOOKS = ("pinnacle", "1xbet")           # the funded books whose legs may form the arb
_UNCONSTRAINED = 1e7                                # 1xbet effective cap (user directive: unconstrained)

# Settlement-flag -> panel chip text (mirrors the soccer rain-chip carry-through).
_RISK_CHIP = {"mlb_rain_rule": "RAIN RULE", "unparsed_settlement": "SETTLEMENT?",
              "tennis_unparsed_settlement": "SETTLEMENT?", "forfeit_rule": "FORFEIT?"}
_NOTE_CHIP = {"walkover_50_50": "W/O 50-50", "dnc_50_50": "DRAW/NC 50-50"}


@dataclass
class TierCfg:
    pinnacle_limit: float = 5000.0
    age_limit_min: float = 30.0
    poly_fee_rate: float = 0.05
    bankroll_cap: float = 30000.0
    min_total_stake: float = 20.0                  # honest-boundary floor below which a row is below_floor


# --------------------------------------------------------------------------- #
# the-odds-api event -> per-market book index                                   #
# --------------------------------------------------------------------------- #
def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_stale(last_update: Any, now: datetime, age_limit_min: float) -> bool:
    """A fixed-book leg is stale when its last_update is missing/unparseable or older than the limit
    (exchange legs are live and never gated here). Conservative: unknown age == stale (drop)."""
    lu = _parse_iso(last_update)
    if lu is None:
        return True
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    return (now - lu).total_seconds() > float(age_limit_min) * 60.0


def _index_books(ev: dict, *, now: datetime, age_limit_min: float) -> dict[str, dict[str, dict]]:
    """{'h2h'|'totals'|'spreads': {book_slug: {'last_update','stale','outcomes':[{name,price,point}]}}}.
    Every book in the response is indexed; the tier logic then selects only pinnacle/1xbet as legs."""
    idx: dict[str, dict[str, dict]] = {"h2h": {}, "totals": {}, "spreads": {}}
    for bm in ev.get("bookmakers") or []:
        if not isinstance(bm, dict):
            continue
        slug = canonical_book_slug(str(bm.get("key") or ""))
        for mk in bm.get("markets") or []:
            key = mk.get("key")
            if key not in idx or slug in idx[key]:
                continue
            lu = mk.get("last_update") or bm.get("last_update")
            outs = [{"name": o.get("name"), "price": _f(o.get("price")), "point": _f(o.get("point"))}
                    for o in (mk.get("outcomes") or []) if isinstance(o, dict)]
            idx[key][slug] = {"last_update": lu, "stale": _is_stale(lu, now, age_limit_min),
                              "outcomes": outs}
    return idx


def _eq_line(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and round(float(a), 2) == round(float(b), 2)


def _book_price(sport: str, market_type: str, idx: dict, book: str, node: dict) -> tuple[Optional[float], Any]:
    """The (price, last_update) a fixed book posts for one tree node's outcome, or (None, None). h2h
    routes by OUTCOME NAME (team/player identity); totals route by side (over/under) + EXACT line."""
    market = _TOA_MARKET.get(market_type)
    entry = idx.get(market, {}).get(book)
    if not entry or entry["stale"]:
        return None, None
    if market == "h2h":
        for o in entry["outcomes"]:
            if o["price"] and match.name_eq(sport, o["name"], node.get("outcome_label")):
                return o["price"], entry["last_update"]
    elif market == "totals":
        side, line = node.get("side"), node.get("line")
        for o in entry["outcomes"]:
            if o["price"] and str(o["name"]).strip().lower() == side and _eq_line(o["point"], line):
                return o["price"], entry["last_update"]
    return None, None


def _has_total_line(entry: dict, line: Optional[float]) -> bool:
    over = any(str(o["name"]).strip().lower() == "over" and _eq_line(o["point"], line) for o in entry["outcomes"])
    under = any(str(o["name"]).strip().lower() == "under" and _eq_line(o["point"], line) for o in entry["outcomes"])
    return over and under


def _has_book_coverage(sport: str, nodes: list[dict], idx: dict) -> bool:
    """True when >=1 actionable book (pinnacle/1xbet) posts the family's the-odds-api market fresh."""
    mt = nodes[0].get("market_type")
    market = _TOA_MARKET.get(mt)
    if market == "h2h":
        return any((e := idx["h2h"].get(b)) and not e["stale"] and e["outcomes"] for b in ACTIONABLE_BOOKS)
    if market == "totals":
        line = nodes[0].get("line")
        return any((e := idx["totals"].get(b)) and not e["stale"] and _has_total_line(e, line)
                   for b in ACTIONABLE_BOOKS)
    return False


# --------------------------------------------------------------------------- #
# Tree family enumeration + labels                                              #
# --------------------------------------------------------------------------- #
def _enrich_families(sport: str, game: dict) -> dict[str, list[dict]]:
    """The game's ENRICH families as {market_key: [2 side-nodes]} (2-way only)."""
    allow = ENRICH_FAMILIES.get(sport, set())
    fams: dict[str, list[dict]] = defaultdict(list)
    for n in game.get("nodes") or []:
        if n.get("market_type") in allow and n.get("kind") == "2way" and n.get("market_key"):
            fams[n["market_key"]].append(n)
    return {k: v for k, v in fams.items() if len(v) == 2}


def _tree_total_lines(game: dict) -> set[float]:
    return {round(float(n["line"]), 2) for n in (game.get("nodes") or [])
            if n.get("market_type") == "total_runs" and n.get("line") is not None}


def _game_display(sport: str, game: dict) -> str:
    """A clean 'A vs B' label — prefer the winner nodes' outcome_labels (the tree home/away strings can
    be imperfectly parsed for tennis/UFC), else fall back to the home/away fields."""
    winners = [n.get("outcome_label") for n in (game.get("nodes") or [])
               if n.get("market_type") in ("ml2", "match_winner", "fight_winner") and n.get("outcome_label")]
    if len(set(winners)) >= 2:
        uniq = list(dict.fromkeys(winners))
        return f"{uniq[0]} vs {uniq[1]}"
    away, home = game.get("away"), game.get("home")
    return f"{away} vs {home}" if away and home else str(home or away or "?")


def _market_label(family: str, line: Optional[float]) -> str:
    names = {"ml2": "Moneyline", "match_winner": "Match Winner", "fight_winner": "Fight Winner",
             "total_runs": "Total Runs", "run_line": "Run Line", "total": "Total",
             "game_spread": "Game Spread", "game_total": "Total Games", "round_total": "Total Rounds"}
    base = names.get(family, family.replace("_", " ").title())
    return f"{base} {line:g}" if line is not None else base


def _family_settlement(nodes: list[dict]) -> list[str]:
    chips: set[str] = set()
    for n in nodes:
        risk, note = n.get("settlement_risk"), n.get("settlement_note")
        if risk:
            chips.add(_RISK_CHIP.get(risk, str(risk)))
        if note:
            chips.add(_NOTE_CHIP.get(note, str(note)))
    return sorted(chips)


# --------------------------------------------------------------------------- #
# Row construction (soccer og_current shape + additive tier fields)             #
# --------------------------------------------------------------------------- #
def _leg_dict(lg: Any) -> dict[str, Any]:
    return {"outcome": lg.outcome, "book": lg.book, "top_odds": lg.top_odds,
            "avg_fill_odds": lg.avg_fill_odds, "stake": lg.stake, "payout": lg.payout}


def _build_row(*, match_label: str, market: str, res: Any, h: Any, tier: str, family: str,
               settlement: list[str], rules_unverified: bool, actionable: bool, floor: float,
               fixture_id: str, kickoff_utc: str) -> dict[str, Any]:
    """One OG row. Emits the soccer 16 keys (so the panel renders it identically) plus the additive
    tier/family/settlement/rules_unverified fields. Three branches match run._write_og_current:
    fee-trap (net<=0), below-floor (no honest size / below the stake floor), full (sized)."""
    base: dict[str, Any] = {
        "match": match_label, "market": market, "fixture_id": fixture_id, "kickoff_et": kickoff_utc,
        "roi_pct": round(res.roi_pct, 2), "arb_sum_S": round(res.arb_sum_S, 6),
        "net_roi_pct": round(res.net_roi_pct, 4), "fee_pct": round(res.net_fee_rate * 100.0, 4),
        "actionable": actionable, "shadow_books": [],
        "tier": tier, "family": family, "settlement": settlement, "rules_unverified": rules_unverified,
    }
    if res.net_roi_pct <= 0:                                  # gross arb eaten by fees -> FEE > EDGE
        base.update({"fee_trap": True, "below_floor": False, "total_stake": 0.0,
                     "t_max_honest": 0.0, "profit": None, "legs": []})
    elif h is None or h.t_max_honest < floor:                # positive net but too thin to place
        base.update({"fee_trap": False, "below_floor": True,
                     "total_stake": (h.total_stake if h else 0.0),
                     "t_max_honest": (h.t_max_honest if h else 0.0), "profit": None, "legs": []})
    else:
        base.update({"fee_trap": False, "below_floor": False, "total_stake": h.total_stake,
                     "t_max_honest": h.t_max_honest, "profit": h.profit,
                     "legs": [_leg_dict(lg) for lg in h.legs]})
    return base


def _finish(outcomes: dict[int, list[Candidate]], extras: dict, *, cfg: TierCfg, match_label: str,
            market: str, family: str, tier: str, settlement: list[str], rules_unverified: bool,
            fixture_id: str, kickoff_utc: str) -> Optional[dict[str, Any]]:
    """select best book per outcome -> compute_arb -> honest size -> row. None unless a gross arb
    across >=2 distinct books exists."""
    if len(outcomes) < 2 or any(not c for c in outcomes.values()):
        return None
    chosen = select_legs(outcomes)
    if chosen is None or len({c.book for c in chosen}) < 2:
        return None
    res = compute_arb(chosen, assumed_unknown_limit=cfg.pinnacle_limit,
                      assumed_unknown_limit_by_book={"1xbet": _UNCONSTRAINED},
                      poly_fee_rate=cfg.poly_fee_rate)
    if not res.is_arb:
        return None
    h = sizing.honest_size_arb(res, extras, bankroll_cap=cfg.bankroll_cap, poly_fee_rate=cfg.poly_fee_rate)
    return _build_row(match_label=match_label, market=market, res=res, h=h, tier=tier, family=family,
                      settlement=settlement, rules_unverified=rules_unverified, actionable=True,
                      floor=cfg.min_total_stake, fixture_id=fixture_id, kickoff_utc=kickoff_utc)


# --------------------------------------------------------------------------- #
# Exchange candidate assembly (from pre-fetched ladders)                         #
# --------------------------------------------------------------------------- #
def _exchange_candidates(node: dict, oid: int, label: str, ladders: dict) -> tuple[list[Candidate], dict]:
    cands: list[Candidate] = []
    extras: dict[tuple[str, int], dict] = {}
    for venue, book, ident_key, side in (
        ("kalshi", "kalshi", "kalshi_ticker", node.get("kalshi_side") or "YES"),
        ("poly", "polymarket", "poly_token_id", "BUY"),
    ):
        ident = node.get(ident_key)
        if not ident:
            continue
        asks = bookmath.valid_asks(ladders.get((venue, ident, side)) or [])
        if not asks:
            continue
        best = asks[0][0]
        depth = sum(p * s for p, s in asks)
        cands.append(Candidate(outcome_id=oid, outcome_name=label, book=book, clone_group=book,
                               decimal_odds=1.0 / best, limit=depth, is_exchange=True, commission=0.0))
        extras[(book, oid)] = {"ladder": asks, "fee_book": book, "size_limit": None}
    return cands, extras


def _book_candidate(sport: str, node: dict, oid: int, label: str, idx: dict, cfg: TierCfg) -> tuple[list, dict]:
    cands: list[Candidate] = []
    extras: dict[tuple[str, int], dict] = {}
    for book in ACTIONABLE_BOOKS:
        price, lu = _book_price(sport, node.get("market_type"), idx, book, node)
        if price is None:
            continue
        lim = cfg.pinnacle_limit if book == "pinnacle" else None
        cands.append(Candidate(outcome_id=oid, outcome_name=label, book=book, clone_group=book,
                               decimal_odds=price, limit=lim, is_exchange=False, commission=0.0,
                               changed_at=lu))
        extras[(book, oid)] = {"ladder": None, "fee_book": None, "size_limit": lim}
    return cands, extras


# --------------------------------------------------------------------------- #
# Tier A                                                                        #
# --------------------------------------------------------------------------- #
def _tier_a_family(sport: str, game: dict, fam_key: str, nodes: list[dict], idx: dict, ladders: dict,
                   *, cfg: TierCfg, game_label: str, fixture_id: str) -> Optional[dict]:
    outcomes: dict[int, list[Candidate]] = {}
    extras: dict[tuple[str, int], dict] = {}
    for oid, node in enumerate(sorted(nodes, key=lambda n: str(n.get("side")))):
        label = node.get("outcome_label") or str(node.get("side"))
        ex_c, ex_x = _exchange_candidates(node, oid, label, ladders)
        bk_c, bk_x = _book_candidate(sport, node, oid, label, idx, cfg)
        cands = ex_c + bk_c
        if not cands:
            return None
        outcomes[oid] = cands
        extras.update(ex_x)
        extras.update(bk_x)
    return _finish(outcomes, extras, cfg=cfg, match_label=game_label,
                   market=_market_label(nodes[0].get("market_type"), nodes[0].get("line")),
                   family=nodes[0].get("market_type"), tier="A", settlement=_family_settlement(nodes),
                   rules_unverified=False, fixture_id=fixture_id, kickoff_utc=str(game.get("kickoff_utc") or ""))


# --------------------------------------------------------------------------- #
# Tier B — book-only families (pinnacle <-> 1xbet)                               #
# --------------------------------------------------------------------------- #
def _norm(s: Any) -> str:
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t.lower())).strip()


def _both_books(idx: dict, market: str) -> Optional[tuple[dict, dict]]:
    pinn, onex = idx[market].get("pinnacle"), idx[market].get("1xbet")
    if not pinn or not onex or pinn["stale"] or onex["stale"]:
        return None
    return pinn, onex


def _total_price(entry: dict, side: str, line: float) -> Optional[float]:
    for o in entry["outcomes"]:
        if o["price"] and str(o["name"]).strip().lower() == side and _eq_line(o["point"], line):
            return o["price"]
    return None


def _tb_totals(sport: str, game: dict, idx: dict, *, family: str, exclude: set[float], cfg: TierCfg,
               game_label: str, fixture_id: str) -> list[dict]:
    both = _both_books(idx, "totals")
    if both is None:
        return []
    pinn, onex = both

    def lines(entry: dict) -> set[float]:
        return {round(o["point"], 2) for o in entry["outcomes"]
                if o["point"] is not None and _total_price(entry, str(o["name"]).strip().lower(), o["point"]) is not None}

    shared = {ln for ln in (lines(pinn) & lines(onex)) if ln not in exclude}
    rows: list[dict] = []
    for line in sorted(shared):
        outcomes: dict[int, list[Candidate]] = {}
        extras: dict[tuple[str, int], dict] = {}
        for oid, side in ((0, "over"), (1, "under")):
            cands: list[Candidate] = []
            for book, entry in (("pinnacle", pinn), ("1xbet", onex)):
                price = _total_price(entry, side, line)
                if price is None:
                    continue
                lim = cfg.pinnacle_limit if book == "pinnacle" else None
                cands.append(Candidate(outcome_id=oid, outcome_name=f"{side.title()} {line:g}", book=book,
                                       clone_group=book, decimal_odds=price, limit=lim, commission=0.0))
                extras[(book, oid)] = {"ladder": None, "fee_book": None, "size_limit": lim}
            if not cands:
                break
            outcomes[oid] = cands
        row = _finish(outcomes, extras, cfg=cfg, match_label=game_label, market=_market_label(family, line),
                      family=family, tier="B", settlement=[], rules_unverified=True,
                      fixture_id=fixture_id, kickoff_utc=str(game.get("kickoff_utc") or ""))
        if row:
            rows.append(row)
    return rows


def _tb_spreads(sport: str, game: dict, idx: dict, *, family: str, require_abs_line: Optional[float],
                cfg: TierCfg, game_label: str, fixture_id: str) -> list[dict]:
    both = _both_books(idx, "spreads")
    if both is None:
        return []
    pinn, onex = both

    def by_team(entry: dict) -> dict[str, tuple[str, float, float]]:
        out: dict[str, tuple[str, float, float]] = {}
        for o in entry["outcomes"]:
            if o["price"] is not None and o["point"] is not None and o["name"]:
                out[_norm(o["name"])] = (o["name"], round(o["point"], 2), o["price"])
        return out

    p, x = by_team(pinn), by_team(onex)
    teams = sorted(set(p) & set(x))
    if len(teams) != 2:
        return []
    t1, t2 = teams
    if p[t1][1] != x[t1][1] or p[t2][1] != x[t2][1]:         # same line on BOTH books
        return []
    if round(p[t1][1] + p[t2][1], 2) != 0.0:                 # exact-negative (complementary) points
        return []
    if require_abs_line is not None and abs(p[t1][1]) != require_abs_line:
        return []
    outcomes: dict[int, list[Candidate]] = {}
    extras: dict[tuple[str, int], dict] = {}
    for oid, team in ((0, t1), (1, t2)):
        cands: list[Candidate] = []
        for book, side in (("pinnacle", p), ("1xbet", x)):
            name, point, price = side[team]
            lim = cfg.pinnacle_limit if book == "pinnacle" else None
            cands.append(Candidate(outcome_id=oid, outcome_name=f"{name} {point:+g}", book=book,
                                   clone_group=book, decimal_odds=price, limit=lim, commission=0.0))
            extras[(book, oid)] = {"ladder": None, "fee_book": None, "size_limit": lim}
        outcomes[oid] = cands
    row = _finish(outcomes, extras, cfg=cfg, match_label=game_label,
                  market=_market_label(family, abs(p[t1][1])), family=family, tier="B", settlement=[],
                  rules_unverified=True, fixture_id=fixture_id, kickoff_utc=str(game.get("kickoff_utc") or ""))
    return [row] if row else []


def _tier_b(sport: str, game: dict, idx: dict, *, cfg: TierCfg, game_label: str, fixture_id: str) -> list[dict]:
    kw = dict(cfg=cfg, game_label=game_label, fixture_id=fixture_id)
    if sport == "mlb":
        return (_tb_spreads(sport, game, idx, family="run_line", require_abs_line=1.5, **kw)
                + _tb_totals(sport, game, idx, family="total", exclude=_tree_total_lines(game), **kw))
    if sport == "tennis":
        return (_tb_spreads(sport, game, idx, family="game_spread", require_abs_line=None, **kw)
                + _tb_totals(sport, game, idx, family="game_total", exclude=set(), **kw))
    if sport == "ufc":
        return _tb_totals(sport, game, idx, family="round_total", exclude=set(), **kw)
    return []


# --------------------------------------------------------------------------- #
# Public: exchange fetch jobs + row assembly                                     #
# --------------------------------------------------------------------------- #
def exchange_jobs(sport: str, by_game: dict, *, age_limit_min: float, now: datetime) -> set[tuple[str, str, str]]:
    """The set of (venue, identifier, side) live-book fetches Tier A will actually use — only the
    book-covered enrich families, so no exchange call is wasted. scan.py fetches these concurrently."""
    jobs: set[tuple[str, str, str]] = set()
    for _gid, (ev, m) in by_game.items():
        idx = _index_books(ev, now=now, age_limit_min=age_limit_min)
        for _fam, nodes in _enrich_families(sport, m.game).items():
            if not _has_book_coverage(sport, nodes, idx):
                continue
            for n in nodes:
                if n.get("kalshi_ticker"):
                    jobs.add(("kalshi", n["kalshi_ticker"], n.get("kalshi_side") or "YES"))
                if n.get("poly_token_id"):
                    jobs.add(("poly", n["poly_token_id"], "BUY"))
    return jobs


def build_rows(sport: str, by_game: dict, ladders: dict, *, cfg: TierCfg, now: datetime,
               log) -> tuple[list[dict], dict[str, int]]:
    """All Tier A + Tier B rows for a sport's matched games, plus inventory counts."""
    rows: list[dict] = []
    inv = {"games_matched": len(by_game), "tier_a_families": 0, "tier_a_no_book": 0,
           "tier_a_exchange_only": 0, "tier_b_families": 0, "arbs": 0, "near_misses": 0}
    for gid, (ev, m) in by_game.items():
        game = m.game
        idx = _index_books(ev, now=now, age_limit_min=cfg.age_limit_min)
        game_label = _game_display(sport, game)
        for fam_key, nodes in _enrich_families(sport, game).items():
            if not _has_book_coverage(sport, nodes, idx):
                inv["tier_a_no_book"] += 1
                continue
            row = _tier_a_family(sport, game, fam_key, nodes, idx, ladders, cfg=cfg,
                                 game_label=game_label, fixture_id=gid)
            if row:
                rows.append(row)
                inv["tier_a_families"] += 1
        for row in _tier_b(sport, game, idx, cfg=cfg, game_label=game_label, fixture_id=gid):
            rows.append(row)
            inv["tier_b_families"] += 1
    for r in rows:
        if r["fee_trap"] or r["below_floor"]:
            inv["near_misses"] += 1
        elif not r["fee_trap"] and not r["below_floor"]:
            inv["arbs"] += 1
    return rows, inv
