"""Cached catalogs (sports/bookmakers/markets/tournaments/names) and the logic that
turns them into the structures the engine needs:

  * MarketSpec      — which markets are MECE back-arb markets, with line/period/outcomes.
  * clone groups    — union-find over bookmaker `cloneOf` so clones never form two legs.
  * tournament IDs  — resolve friendlies by name regex (or use pinned IDs).
  * name map        — fixtureId -> human names (+ status/kickoff) and participantId -> name.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---- file names under data/cache/ ------------------------------------------ #
SPORTS_FILE = "sports.json"
BOOKMAKERS_FILE = "bookmakers.json"
MARKETS_FILE = "markets.json"
TOURNAMENTS_FILE = "tournaments.json"
NAMES_FILE = "names.json"


# --------------------------------------------------------------------------- #
# Cache I/O                                                                     #
# --------------------------------------------------------------------------- #
def _path(cache_dir: str, name: str) -> str:
    return os.path.join(cache_dir, name)


def load_json(cache_dir: str, name: str) -> Optional[Any]:
    p = _path(cache_dir, name)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(cache_dir: str, name: str, payload: Any) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    p = _path(cache_dir, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)


def file_age_hours(cache_dir: str, name: str, now_epoch: float) -> Optional[float]:
    p = _path(cache_dir, name)
    if not os.path.exists(p):
        return None
    return (now_epoch - os.path.getmtime(p)) / 3600.0


# --------------------------------------------------------------------------- #
# Market classification (MECE back-arb markets only)                            #
# --------------------------------------------------------------------------- #
@dataclass
class MarketSpec:
    market_id: int
    label: str               # human label (we trust the API marketName)
    family: str              # 1x2 | totals | btts | asian_handicap | euro_handicap | dnb | odd_even | team_totals
    period: str              # fulltime | 1st_half | 2nd_half | ...
    line: Optional[float]    # handicap / total line; None for 1x2/btts/dnb/odd_even
    n_way: int
    outcome_ids: list[int]
    outcome_names: dict[int, str]
    push_on_draw: bool = False  # Draw No Bet refunds on draw

    @property
    def has_quarter_line(self) -> bool:
        return self.line is not None and abs(self.line * 2 - round(self.line * 2)) > 1e-9


# Families where the `line` (handicap) is meaningful.
_LINE_FAMILIES = {"totals", "team_totals", "asian_handicap", "euro_handicap"}


def classify_family(name: str, mtype: str, n_outcomes: int, handicap: float) -> Optional[str]:
    """Return the MECE family for a market, or None if it is not a clean back-arb market.

    Heuristic over marketName + marketType + outcome count + handicap. Anything we do
    not recognise is returned as None and logged by the caller (so coverage gaps surface).
    """
    nm = name.lower()
    mt = (mtype or "").lower()
    h = handicap or 0.0

    if "double chance" in nm or mt == "double_chance":
        return None  # explicitly excluded (overlaps 1x2)

    if "both teams to score" in nm or "btts" in nm or mt == "btts":
        return "btts" if n_outcomes == 2 else None
    if "draw no bet" in nm or mt in ("draw_no_bet", "dnb"):
        return "dnb" if n_outcomes == 2 else None
    if ("odd" in nm and "even" in nm) or mt in ("odd_even", "oddeven"):
        return "odd_even" if n_outcomes == 2 else None

    is_total = ("over/under" in nm or "over / under" in nm or "total" in nm or mt == "totals"
                or "over" in nm and "under" in nm)
    if is_total:
        if ("team" in nm) or (("home" in nm or "away" in nm) and "total" in nm):
            return "team_totals" if n_outcomes == 2 else None
        return "totals" if n_outcomes == 2 else None

    if "handicap" in nm or "asian" in nm or mt in ("handicap", "asian_handicap", "ah", "3way_handicap", "european_handicap"):
        if n_outcomes == 3:
            return "euro_handicap"
        if n_outcomes == 2:
            return "asian_handicap"
        return None

    # Full Time / Half result (1 / X / 2). A non-zero handicap makes it a 3-way handicap.
    looks_1x2 = (mt == "1x2" or "result" in nm or "1x2" in nm or "winner" in nm
                 or "match odds" in nm or "moneyline" in nm)
    if looks_1x2 and n_outcomes == 3:
        return "euro_handicap" if abs(h) > 1e-9 else "1x2"

    return None


def _period_of(raw: dict[str, Any]) -> str:
    p = (raw.get("period") or "fulltime")
    return str(p).lower()


def build_market_specs(
    markets_json: list[dict[str, Any]],
    sport_id: int,
    exclude_names: list[str],
) -> tuple[dict[int, MarketSpec], list[dict[str, Any]]]:
    """Return (accepted specs by marketId, list of skipped/unclassified markets for logging)."""
    specs: dict[int, MarketSpec] = {}
    skipped: list[dict[str, Any]] = []

    for m in markets_json or []:
        mid = m.get("marketId")
        if mid is None:
            continue
        mid = int(mid)
        m_sport = m.get("sportId")
        if m_sport is not None and int(m_sport) != int(sport_id):
            continue
        name = (m.get("marketName") or "").strip()
        lname = name.lower()
        if m.get("playerProp"):
            skipped.append({"marketId": mid, "name": name, "reason": "player_prop"})
            continue
        if any(x in lname for x in exclude_names):
            skipped.append({"marketId": mid, "name": name, "reason": "excluded_name"})
            continue

        outcomes = m.get("outcomes") or []
        n = len(outcomes)
        handicap = m.get("handicap")
        handicap_f = float(handicap) if handicap is not None else 0.0

        family = classify_family(lname, m.get("marketType") or "", n, handicap_f)
        if family is None:
            skipped.append({"marketId": mid, "name": name, "marketType": m.get("marketType"), "reason": "not_mece"})
            continue

        line: Optional[float] = handicap_f if family in _LINE_FAMILIES else None
        outcome_ids = [int(o["outcomeId"]) for o in outcomes if o.get("outcomeId") is not None]
        outcome_names = {int(o["outcomeId"]): str(o.get("outcomeName", o["outcomeId"]))
                         for o in outcomes if o.get("outcomeId") is not None}

        specs[mid] = MarketSpec(
            market_id=mid,
            label=name or f"market {mid}",
            family=family,
            period=_period_of(m),
            line=line,
            n_way=n,
            outcome_ids=outcome_ids,
            outcome_names=outcome_names,
            push_on_draw=(family == "dnb"),
        )

    return specs, skipped


# --------------------------------------------------------------------------- #
# Clone groups (union-find over cloneOf)                                        #
# --------------------------------------------------------------------------- #
def build_clone_group_fn(bookmakers_json: list[dict[str, Any]]) -> Callable[[str], str]:
    """Return a function slug -> clone-group-root. Books not in the catalog map to themselves."""
    name_to_slug: dict[str, str] = {}
    clone_of: dict[str, Any] = {}
    slugs: set[str] = set()

    for b in bookmakers_json or []:
        slug = b.get("slug")
        if not slug:
            continue
        slugs.add(slug)
        bn = (b.get("bookmakerName") or "").strip().lower()
        if bn:
            name_to_slug[bn] = slug
        clone_of[slug] = b.get("cloneOf")

    parent = {s: s for s in slugs}

    def find(x: str) -> str:
        if x not in parent:
            return x
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for slug, co in clone_of.items():
        if not co:
            continue
        target = co if co in slugs else name_to_slug.get(str(co).strip().lower())
        if target and target in parent:
            union(slug, target)

    def group_of(book: str) -> str:
        return find(book) if book in parent else book

    return group_of


# --------------------------------------------------------------------------- #
# Tournament resolution                                                         #
# --------------------------------------------------------------------------- #
def resolve_tournament_ids(
    tournaments_json: list[dict[str, Any]],
    name_regex: str,
    national_teams_only: bool,
) -> list[dict[str, Any]]:
    """Find friendlies tournaments. Returns matched tournament dicts (with counts) for logging."""
    pat = re.compile(name_regex, re.IGNORECASE)
    matches: list[dict[str, Any]] = []
    for t in tournaments_json or []:
        name = t.get("tournamentName") or ""
        slug = t.get("tournamentSlug") or ""
        if not (pat.search(name) or pat.search(slug)):
            continue

        cat = (t.get("categoryName") or "").lower()
        blob = f"{name} {slug} {cat}".lower()

        # Simulated / virtual leagues (SRL = Simulated Reality League, esoccer, etc.) are
        # AI-generated matches, never real arb targets — always exclude them.
        if any(tag in blob for tag in ("srl", "simulated", "esoccer", "e-soccer", "virtual")):
            continue

        if national_teams_only:
            # Drop club-level friendlies; keep international/national-team ones (incl. youth/women).
            if "club" in blob:
                continue
        matches.append(t)
    return matches


# --------------------------------------------------------------------------- #
# Name map                                                                      #
# --------------------------------------------------------------------------- #
def build_name_map(fixtures_json: list[dict[str, Any]]) -> dict[str, Any]:
    """Build {fixture_id -> info} and {participant_id -> name} from /v4/fixtures output."""
    by_fixture: dict[str, dict[str, Any]] = {}
    by_participant: dict[str, str] = {}
    for f in fixtures_json or []:
        fid = f.get("fixtureId")
        p1 = f.get("participant1Name") or f.get("participant1ShortName")
        p2 = f.get("participant2Name") or f.get("participant2ShortName")
        if f.get("participant1Id") is not None and p1:
            by_participant[str(f["participant1Id"])] = p1
        if f.get("participant2Id") is not None and p2:
            by_participant[str(f["participant2Id"])] = p2
        if fid:
            by_fixture[str(fid)] = {
                "p1": p1,
                "p2": p2,
                "tournament": f.get("tournamentName"),
                "category": f.get("categoryName"),
                "status_id": f.get("statusId"),
                "status_name": f.get("statusName"),
                "start_time": f.get("startTime"),
            }
    return {"by_fixture": by_fixture, "by_participant": by_participant,
            "saved_at": None}


def refresh_names(client, cache_dir: str, sport_id: int, tournament_ids: list[int],
                  from_utc: str, to_utc: str, now_epoch: float) -> dict[str, Any]:
    """Fetch /v4/fixtures for the friendlies window and cache the name map. 1 billable request."""
    all_fixtures: list[dict[str, Any]] = []
    for tid in tournament_ids:
        fx = client.fixtures(sport_id=sport_id, tournament_id=tid, from_utc=from_utc,
                             to_utc=to_utc, has_odds=True)
        if isinstance(fx, list):
            all_fixtures.extend(fx)
    name_map = build_name_map(all_fixtures)
    name_map["saved_at"] = now_epoch
    save_json(cache_dir, NAMES_FILE, name_map)
    return name_map


# --------------------------------------------------------------------------- #
# Catalog refresh (run by refresh-catalog workflow)                            #
# --------------------------------------------------------------------------- #
def refresh_catalogs(client, cache_dir: str, sport_id: int, log) -> dict[str, int]:
    """Fetch and cache sports/bookmakers/markets/tournaments. ~4 billable requests."""
    counts: dict[str, int] = {}

    sports = client.sports()
    save_json(cache_dir, SPORTS_FILE, sports)
    counts["sports"] = len(sports) if isinstance(sports, list) else 0
    log.info("Cached %s sports", counts["sports"])

    books = client.bookmakers()
    save_json(cache_dir, BOOKMAKERS_FILE, books)
    counts["bookmakers"] = len(books) if isinstance(books, list) else 0
    log.info("Cached %s bookmakers", counts["bookmakers"])

    markets = client.markets()
    save_json(cache_dir, MARKETS_FILE, markets)
    counts["markets"] = len(markets) if isinstance(markets, list) else 0
    log.info("Cached %s markets", counts["markets"])

    tournaments = client.tournaments(sport_id=sport_id)
    save_json(cache_dir, TOURNAMENTS_FILE, tournaments)
    counts["tournaments"] = len(tournaments) if isinstance(tournaments, list) else 0
    log.info("Cached %s tournaments for sportId=%s", counts["tournaments"], sport_id)

    return counts
