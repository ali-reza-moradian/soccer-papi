"""Earliest-possible-resolution estimate for GROUP-stage outcome markets (winner / bottom).

A group market's LATEST resolution is its group's final scheduled match; but a team's winner/bottom
outcome can be mathematically CLINCHED sooner from current standings. We estimate both, purely from
Kalshi KXWCGAME data (match dates from the event ticker + results from settled markets) intersected
with each group's team set — no extra OddsPapi cost, no 3-letter-code mapping (team names come from
yes_sub_title). The estimate is a heads-up for the human ("verify before betting"), NOT a guarantee:
the clinch math ignores FIFA tiebreakers (uses strict inequalities to stay conservative).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .kalshi import _event_commence_iso
from .theoddsapi import _parse_iso, normalize_team

GROUP_GAMES_PER_TEAM = 3        # 4-team group, round-robin -> each team plays 3


@dataclass
class Game:
    date_iso: Optional[str]     # match calendar day at noon UTC (from the event ticker)
    teams: frozenset            # {team_norm, team_norm}
    winner: Optional[str]       # team_norm | "draw" | None (not yet played / unsettled)


def parse_game_schedule(markets: list[dict[str, Any]]) -> list[Game]:
    """KXWCGAME markets (open AND settled) -> [Game]. A game is 'played' (winner set) once any of its
    markets carries a result; the winning team's market resolves 'yes' (or the Tie market does)."""
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        if isinstance(m, dict):
            by_event.setdefault(str(m.get("event_ticker") or ""), []).append(m)
    games: list[Game] = []
    for et, ms in by_event.items():
        date_iso = _event_commence_iso(et)
        teams: set[str] = set()
        winner: Optional[str] = None
        settled = False
        for m in ms:
            sub = str(m.get("yes_sub_title") or "").strip()
            res = str(m.get("result") or "").lower()
            if res in ("yes", "no"):
                settled = True
            if sub.lower() == "tie":
                if res == "yes":
                    winner = "draw"
            elif sub:
                teams.add(normalize_team(sub))
                if res == "yes":
                    winner = normalize_team(sub)
        if len(teams) != 2:
            continue
        games.append(Game(date_iso, frozenset(teams), winner if settled else None))
    return games


def standings(group_games: list[Game], team_set: frozenset) -> dict[str, dict[str, int]]:
    """Points + games played for each team in the group from its PLAYED games (win 3 / draw 1)."""
    st = {t: {"pts": 0, "played": 0} for t in team_set}
    for g in group_games:
        if g.winner is None or not (g.teams <= team_set):
            continue
        a, b = tuple(g.teams)
        st[a]["played"] += 1
        st[b]["played"] += 1
        if g.winner == "draw":
            st[a]["pts"] += 1
            st[b]["pts"] += 1
        elif g.winner in st:
            st[g.winner]["pts"] += 3
    return st


def is_clinched(team: str, kind: str, st: dict[str, dict[str, int]]) -> bool:
    """Is `team`'s `kind` ('winner'|'bottom') ALREADY mathematically guaranteed? Strict inequalities
    (tiebreaker-agnostic, conservative). Winner: team's points exceed every rival's MAX achievable.
    Bottom: team's MAX achievable is below every rival's current points (they can only climb)."""
    if team not in st or kind not in ("winner", "bottom"):
        return False
    others = [o for o in st if o != team]
    if not others:
        return False
    pts = st[team]["pts"]
    my_max = pts + 3 * (GROUP_GAMES_PER_TEAM - st[team]["played"])
    if kind == "winner":
        return all(pts > st[o]["pts"] + 3 * (GROUP_GAMES_PER_TEAM - st[o]["played"]) for o in others)
    return all(my_max < st[o]["pts"] for o in others)        # bottom


def _days_out(d: datetime, now: datetime) -> int:
    return max(0, math.ceil((d - now).total_seconds() / 86400.0))


def resolution_estimate(team_set: frozenset, kind: str, team_norm: str, games: list[Game],
                        now: datetime, within_days: float) -> Optional[dict[str, Any]]:
    """Per (group, team, outcome): {resolves_by_utc, resolves_by_days, earliest_days, early,
    clinched, within_window}. None if the group's schedule is unknown.

    resolves_by = the group's FINAL match date (latest possible resolution). `early` is True when the
    outcome could resolve before then — already clinched, or there is an earlier remaining group match
    at which it could clinch. within_window flips the ⚡ flag when that earliest date is <= within_days.
    """
    group_games = [g for g in games if g.teams <= team_set and g.date_iso]
    if not group_games:
        return None
    dts = sorted(d for d in (_parse_iso(g.date_iso) for g in group_games) if d is not None)
    if not dts:
        return None
    resolves_by = dts[-1]
    st = standings(group_games, team_set)
    clinched = is_clinched(team_norm, kind, st)
    remaining = sorted(d for d in (_parse_iso(g.date_iso) for g in group_games if g.winner is None)
                       if d is not None and d >= now)
    if clinched:
        earliest = now
    elif remaining:
        earliest = remaining[0]
    else:
        earliest = resolves_by
    early = clinched or earliest < resolves_by
    return {
        "resolves_by_utc": resolves_by.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolves_by_days": _days_out(resolves_by, now),
        "earliest_days": _days_out(earliest, now),
        "early": early,
        "clinched": clinched,
        "within_window": _days_out(earliest, now) <= within_days,
    }
