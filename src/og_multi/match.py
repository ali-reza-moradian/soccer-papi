"""Match a the-odds-api event to a GenZ tree game (spec section 2).

A mis-matched event mints a phantom arb, so matching is identity + date/time gated and never guessed:
  * MLB    — full team-name token match, commence within +/-90 min of the game's kickoff.
  * tennis — surname token sets, containment both ways, date +/-1 (time is DISPLAY-only).
  * UFC    — fighter full-name token sets, date +/-1.

The per-sport surname/token functions are IMPORTED from the GenZ sport adapters (``sports_tennis`` /
``sports_ufc``) so this matcher's keys are byte-identical to the ``node["side"]`` surnames already in
the tree — a toa outcome "Andrey Rublev" reduces to ``"rublev"`` exactly as the tree node did. A
decoy (same player, different date) fails the date gate; an event that matches two games is AMBIGUOUS
and refused (never guessed). Every match carries a method + score for the logs; unmatched events are
summarised once per cycle by :func:`match_events`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ..genz.sports_tennis import name_tokens as _tennis_tokens, surname as _tennis_surname
from ..genz.sports_ufc import name_tokens as _ufc_tokens, surname as _ufc_surname

MLB_COMMENCE_TOLERANCE_MIN = 90.0        # MLB: exact-ish kickoff; distinguishes doubleheader G1/G2
COMBAT_DATE_TOLERANCE_DAYS = 1           # tennis/UFC: date +/-1 (venues post on adjacent UTC days)


@dataclass
class Matched:
    game_id: str
    game: dict
    method: str                          # 'team_tokens' | 'surname_tokens' | 'fighter_tokens'
    score: float                         # 0..1 (1.0 = exact token-set / identity)


@dataclass
class MatchResult:
    by_game: dict[str, tuple[dict, Matched]] = field(default_factory=dict)  # game_id -> (toa_event, Matched)
    unmatched: list[str] = field(default_factory=list)                      # "home v away" labels
    ambiguous: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Normalization                                                                 #
# --------------------------------------------------------------------------- #
def _norm(s: Any) -> str:
    """Accent-strip, lowercase, punctuation/hyphens -> space, collapse whitespace."""
    if not s:
        return ""
    t = unicodedata.normalize("NFKD", str(s))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _team_tokens(s: Any) -> frozenset:
    return frozenset(t for t in _norm(s).split() if t)


def _tokens_contained(a: frozenset, b: frozenset) -> bool:
    """True when either token set is a non-empty subset of the other (containment both ways)."""
    return bool(a) and bool(b) and (a <= b or b <= a)


def _team_eq(a: Any, b: Any) -> bool:
    """MLB team identity: equal norms, substring either way (Athletics vs Oakland Athletics), OR
    token containment either way (Athletics vs Oakland Athletics as token sets)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return _tokens_contained(_team_tokens(a), _team_tokens(b))


# Surname / token helpers per sport, sourced from the GenZ adapters so keys match node["side"].
def _sport_fns(sport: str) -> tuple[Callable[[Any], str], Callable[[Any], frozenset]]:
    if sport == "tennis":
        return _tennis_surname, _tennis_tokens
    if sport == "ufc":
        return _ufc_surname, _ufc_tokens
    raise ValueError(f"no surname fns for sport {sport!r}")


def name_eq(sport: str, a: Any, b: Any) -> bool:
    """Compare an OUTCOME label to a tree node's ``outcome_label`` for the given sport (used by
    tiers.py to route a book's h2h outcome onto the right node). MLB = team identity; tennis/UFC =
    surname equality with a full-name token-containment fallback."""
    if sport == "mlb":
        return _team_eq(a, b)
    surname, tokens = _sport_fns(sport)
    sa, sb = surname(a), surname(b)
    if sa and sb and sa == sb:
        return True
    return _tokens_contained(tokens(a), tokens(b))


# --------------------------------------------------------------------------- #
# Time / date gates                                                             #
# --------------------------------------------------------------------------- #
def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _event_date(ev: dict) -> Optional[date]:
    dt = _parse_iso(ev.get("commence_time"))
    return dt.astimezone(timezone.utc).date() if dt else None


def _game_date(game: dict) -> Optional[date]:
    d = game.get("date")
    if d:
        try:
            return date.fromisoformat(str(d)[:10])
        except ValueError:
            pass
    dt = _parse_iso(game.get("kickoff_utc"))
    return dt.astimezone(timezone.utc).date() if dt else None


def _within_days(a: Optional[date], b: Optional[date], days: int) -> bool:
    return a is not None and b is not None and abs((a - b).days) <= days


# --------------------------------------------------------------------------- #
# Per-sport game matchers                                                        #
# --------------------------------------------------------------------------- #
def _tree_surnames(game: dict, surname: Callable[[Any], str]) -> frozenset:
    """A game's participant surnames — the winner nodes' ``side`` (already normalized) unioned with
    the surnames of the (possibly imperfectly parsed) home/away labels."""
    sides = {n.get("side") for n in (game.get("nodes") or [])
             if n.get("market_type") in ("match_winner", "fight_winner") and n.get("side")}
    for key in ("home", "away"):
        sn = surname(game.get(key))
        if sn:
            sides.add(sn)
    return frozenset(s for s in sides if s)


def _mlb_match(ev: dict, game: dict) -> Optional[tuple[str, float]]:
    toa = [ev.get("home_team"), ev.get("away_team")]
    tree = [game.get("home"), game.get("away")]
    if not all(toa) or not all(tree):
        return None
    ok = ((_team_eq(toa[0], tree[0]) and _team_eq(toa[1], tree[1]))
          or (_team_eq(toa[0], tree[1]) and _team_eq(toa[1], tree[0])))
    if not ok:
        return None
    ct, kt = _parse_iso(ev.get("commence_time")), _parse_iso(game.get("kickoff_utc"))
    if ct is None or kt is None or abs((ct - kt).total_seconds()) > MLB_COMMENCE_TOLERANCE_MIN * 60.0:
        return None
    return "team_tokens", 1.0


def _combat_match(sport: str, ev: dict, game: dict) -> Optional[tuple[str, float]]:
    surname, _tokens = _sport_fns(sport)
    toa_sn = {surname(ev.get("home_team")), surname(ev.get("away_team"))}
    toa_sn.discard("")
    if len(toa_sn) < 2:                                  # both participants must reduce to distinct surnames
        return None
    tree_sn = _tree_surnames(game, surname)
    if not toa_sn <= tree_sn:
        return None
    if not _within_days(_event_date(ev), _game_date(game), COMBAT_DATE_TOLERANCE_DAYS):
        return None
    method = "surname_tokens" if sport == "tennis" else "fighter_tokens"
    return method, 1.0


def _match_one(sport: str, ev: dict, games: dict) -> tuple[Optional[Matched], list[str]]:
    """Return (Matched|None, candidate_game_ids). >1 candidate => ambiguous (caller refuses)."""
    hits: list[Matched] = []
    for gid, game in games.items():
        res = _mlb_match(ev, game) if sport == "mlb" else _combat_match(sport, ev, game)
        if res is not None:
            hits.append(Matched(game_id=gid, game=game, method=res[0], score=res[1]))
    if len(hits) == 1:
        return hits[0], [hits[0].game_id]
    return None, [h.game_id for h in hits]


# --------------------------------------------------------------------------- #
# Public entry                                                                  #
# --------------------------------------------------------------------------- #
def match_events(sport: str, events: list, games: dict, *, now: datetime, log) -> MatchResult:
    """Match every the-odds-api event to a tree game. Logs each match (method+score) at debug and a
    single unmatched/ambiguous summary per cycle (name drift is self-diagnosing)."""
    result = MatchResult()
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        label = f"{ev.get('home_team')} v {ev.get('away_team')}"
        matched, cands = _match_one(sport, ev, games)
        if matched is None:
            (result.ambiguous if len(cands) > 1 else result.unmatched).append(label)
            continue
        if matched.game_id in result.by_game:            # two events, one game — keep the first
            result.unmatched.append(label)
            continue
        result.by_game[matched.game_id] = (ev, matched)
        log.debug("[MATCH] %s: '%s' -> %s via %s (score %.2f)",
                  sport, label, matched.game_id, matched.method, matched.score)
    if result.unmatched:
        log.info("[MATCH] %s: %d/%d events matched; unmatched: %s", sport,
                 len(result.by_game), len(result.by_game) + len(result.unmatched) + len(result.ambiguous),
                 ", ".join(result.unmatched))
    if result.ambiguous:
        log.info("[MATCH] %s: %d ambiguous (matched >1 game, refused): %s",
                 sport, len(result.ambiguous), ", ".join(result.ambiguous))
    return result
