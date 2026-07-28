"""DETERMINISTIC Kalshi<->Polymarket outcome matcher (NO LLM).

The tree builder feeds each venue's raw markets through here to get a list of canonical ``Outcome``
records. Two outcomes — one from Kalshi, one from Polymarket — are the SAME bet iff their
``twin_key`` strings are equal. That is the only join the builder needs.

A ``twin_key`` is ``"<group>##<side>"`` where:
  * ``group`` identifies the market (and, for line markets, the LINE; for team markets, the team(s))
    in a venue-agnostic, normalized form — so the two sides of a 2-outcome market share a ``group``
    and the engine can take best-of-both per side;
  * ``side`` identifies the outcome within that market (over/under, yes/no, home/draw/away, a team,
    a score string, or a player threshold).

THRESHOLD<->LINE CONVERTER (the core rule). Kalshi quotes integer/colloquial thresholds; Polymarket
quotes half-lines. We canonicalize BOTH to the Poly half-line:
  * total goals:  Kalshi "Over 2.5 goals"      -> line 2.5   == Poly "O/U 2.5" OVER          (direct)
  * corners:      Kalshi "9+ corners" (>=9)     -> line 8.5   == Poly "O/U 8.5 Total Corners"  (K-0.5)
  * team total:   Kalshi "Norway over 1.5"      -> line 1.5   == Poly "Norway O/U 1.5" OVER    (direct)
  * spread:       Kalshi "wins by more than 1.5"-> line 1.5   == Poly "Spread: Team (-1.5)"     (cover)
  * moneyline:    Kalshi "Reg Time: Tie/Home/Away" == Poly draw / home win / away win
  * halves:       Kalshi "Tie 1st Half"         == Poly "Draw at halftime"; "<Team> wins 1st Half"
                  == Poly "<Team> leading at halftime"  (same for 2H)
  * exact score:  Kalshi "<Team> wins A-B" / "Draw A-A" == Poly "Exact Score: <Home> A - B <Away>"
                  (Kalshi ticker is <AWAY><HOME>; we re-order to canonical HOME-AWAY)
  * BTTS / 1H / 2H BTTS:  direct yes/no
  * first to score:  Kalshi "No Goal" == Poly "Neither team"
  * to advance:      Kalshi "<Team> advances" == Poly "<Team> to Advance"
  * player goals:    Kalshi KXWCGOAL "<Player> N+" == Poly "<Player>: N+ goals" (name-normalized)

Player-prop and shot/SOA nodes are inherently fuzzier (different structures, name matching), so they
are always tagged ``confidence="low"`` — the engine treats low-confidence nodes as ALERT-ONLY and
never auto-trades them, even with the execution flags on. When a clean equivalence can't be derived
for some other market we still emit the outcome at ``confidence="low"`` rather than dropping it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from ..theoddsapi import normalize_team  # same team normalization the rest of the project uses
from . import soccer_names             # soccer club-name equivalence (diacritics, legal forms, exonyms)

# Market families and their arity. The engine arbs only clean 2-outcome markets; 3-way and
# many-outcome families are recorded in the tree but skipped by the v1 engine.
TWO_WAY = frozenset({"total_goals", "1h_total", "2h_total", "team_total",
                     "1h_team_total", "2h_team_total", "corners", "team_corners",
                     "btts", "1h_btts", "2h_btts", "spread", "advance"})
THREE_WAY = frozenset({"moneyline", "1h_result", "2h_result", "first_to_score"})
MULTI = frozenset({"exact_score", "player_goals", "player_shots", "soa"})
# Player-prop families are always alert-only (low confidence) regardless of how clean the match is.
ALERT_ONLY = frozenset({"player_goals", "player_shots", "soa"})

# FULL-MATCH COUNT markets: they tally goals/corners over the whole match, so the SETTLEMENT PERIOD
# matters. Kalshi settles these on the FULL game (incl. extra time); Polymarket on 90'+stoppage ONLY.
# In a knockout game (extra time possible) a Kalshi full-game count and a Poly 90-min count are NOT the
# same bet and can BOTH lose — so a period mismatch must never be paired/traded. (Half markets already
# carry the period in their type; result/spread/btts are handled by the moneyline-style regulation
# rules and are out of scope here.)
COUNT_MARKETS = frozenset({"corners", "team_corners", "total_goals", "team_total"})

# Resolution-text fragments that pin a market's settlement period. EXCLUDE => 90'+stoppage only
# ('regulation'); INCLUDE => extra time counts ('full_game'). ET EXCLUSION takes PRIORITY (a phrase
# like 'does not include extra time' contains the substring 'include extra time', so exclusion must win).
_ET_EXCLUDE = ("do not count", "does not count", "does not include extra", "not include extra time",
               "excludes extra time", "excluding extra time", "first 90 minutes", "90 minutes of regular",
               "within the first 90", "regulation time only", "not include overtime",
               "does not include overtime", "90 minutes plus stoppage", "90 minutes of regular play")
_ET_INCLUDE = ("any extra time", "including extra time", "includes extra time", "and extra time",
               "plus extra time", "entire game", "extra time periods", "including overtime",
               "extra time counts", "regulation, stoppage and")


def parse_settlement_period(text: str) -> Optional[str]:
    """Classify a market's resolution description into 'regulation' (90'+stoppage, EXCLUDES extra time)
    or 'full_game' (INCLUDES extra time), else None when undeterminable. Both Kalshi's rules and
    Polymarket's description state this explicitly for count markets. An explicit ET EXCLUSION wins."""
    t = str(text or "").lower()
    if not t:
        return None
    if any(p in t for p in _ET_EXCLUDE):         # explicit exclusion wins ('does not include extra time')
        return "regulation"
    if any(p in t for p in _ET_INCLUDE):
        return "full_game"
    if "extra time" in t or "overtime" in t:     # ET mentioned, no explicit exclusion -> counts
        return "full_game"
    if "90 minutes" in t or "regular time" in t or "regulation" in t:
        return "regulation"
    return None


# --------------------------------------------------------------------------- #
# TWO-LEG TIE scope — a knockout tie is NOT the same bet as one leg of it        #
# --------------------------------------------------------------------------- #
# UEFA qualifying rounds are two-legged. Both venues list markets on the SAME fixture that settle on
# COMPLETELY different events, and their team-named outcomes look identical to a pairing engine:
#
#   Kalshi KXUCLGAME     'Reg Time: Crvena Zvezda'  -> wins THIS game in 90'+stoppage
#   Kalshi KXUCLADVANCE  'Crvena Zvezda advances'   -> wins the TIE on aggregate over both legs
#   Poly   'Team to Advance' -> "...officially advances from this two-legged tie, based on aggregate
#                               score across both legs... includes advancement after regulation,
#                               extra time, a penalty shoot-out..."
#
# Pairing a single-game leg against an aggregate market is a guaranteed mis-settlement: a team can
# lose the leg and still advance. (Verified live 2026-07-28 — tests/fixtures/raw_soccer_ucl_*.json.)
#
# The discrimination is SUBTLE, and a naive keyword scan gets it backwards: ordinary regulation-time
# markets carry the boilerplate "extra time, and penalty shoot-outs are excluded", so matching bare
# 'penalt' or 'extra time' would refuse the very markets we want. Only TIE-SCOPE phrasing counts.
_TIE_SCOPE = (
    "two-legged", "two legged", "on aggregate", "aggregate score", "aggregate winner",
    "advances from this", "advance past", "to advance", " advances", "advancement after",
    "wins the tie", "qualifies for the next round", "progress to the next round",
    "over both legs", "across both legs", "both legs",
)
# Phrases that pin a market to ONE fixture. 'excluded' boilerplate lives here, not in _TIE_SCOPE.
_SINGLE_SCOPE = (
    "90 minutes plus stoppage", "90 minutes of regular", "within the first 90",
    "first 90 minutes", "this market will resolve to \"yes\"", "wins on ", "win on ",
    "end in a draw", "regulation time only",
)


def parse_tie_scope(text: str) -> Optional[str]:
    """Classify a market's settlement SCOPE: 'tie' (resolves on a two-legged aggregate / who advances)
    or 'single_game' (resolves on one fixture), else None when undeterminable.

    TIE phrasing WINS over single-game phrasing, because an aggregate market's description also
    describes the deciding leg ("with the deciding leg scheduled for July 28")."""
    t = str(text or "").lower()
    if not t:
        return None
    if any(p in t for p in _TIE_SCOPE):
        return "tie"
    if any(p in t for p in _SINGLE_SCOPE):
        return "single_game"
    return None


def kind_for(market_type: str) -> str:
    """The arity flag stored on the tree node: '2way' (engine-tradeable), '3way', or 'multi'."""
    if market_type in TWO_WAY:
        return "2way"
    if market_type in THREE_WAY:
        return "3way"
    return "multi"


# --------------------------------------------------------------------------- #
# Name / number normalization                                                   #
# --------------------------------------------------------------------------- #
def strip_accents(text: str) -> str:
    """Fold accents/diacritics to ASCII (Mbappé -> Mbappe, Müller -> Muller)."""
    nfkd = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_player(name: str) -> str:
    """Canonical player key: accents stripped, lowercased, punctuation dropped, spaces collapsed.
    'Kylian Mbappé' -> 'kylian mbappe'; 'B. Gunnarsson' -> 'b gunnarsson'."""
    s = strip_accents(name).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _team(name: str) -> str:
    """Normalized team key (reuses the project's cross-provider team normalizer)."""
    return normalize_team(name)


def _pair(a: str, b: str) -> str:
    """Order-independent normalized team-pair tag, so both venues build the same group string."""
    return "_".join(sorted((_team(a), _team(b))))


# --------------------------------------------------------------------------- #
# Low-level threshold<->line converters (unit-tested directly)                   #
# --------------------------------------------------------------------------- #
_NUM = r"(\d+(?:\.\d+)?)"


def goals_over_to_line(text: str) -> Optional[float]:
    """Kalshi total-goals threshold -> canonical Poly half-line. 'Over 2.5 goals' -> 2.5;
    'Over 3 goals'/'3+ goals' (>=3) -> 2.5. None if no number is present."""
    t = str(text or "").lower()
    m = re.search(rf"over\s+{_NUM}", t) or re.search(rf"{_NUM}\s*\+", t)
    if not m:
        return None
    v = float(m.group(1))
    return v if (v * 2) % 2 != 0 else v - 0.5     # X.5 direct; whole N (>=N) -> N-0.5


def corners_plus_to_line(text: str) -> Optional[float]:
    """Kalshi corners threshold -> canonical Poly half-line. '9+ corners' (>=9) -> 8.5;
    'Over 8.5 corners' -> 8.5. None if no number."""
    t = str(text or "").lower()
    m = re.search(rf"{_NUM}\s*\+", t)
    if m:
        return float(m.group(1)) - 0.5            # K+ means >=K  ==  over (K-0.5)
    m = re.search(rf"over\s+{_NUM}", t)
    if m:
        v = float(m.group(1))
        return v if (v * 2) % 2 != 0 else v - 0.5
    return None


def team_total_over_to_line(text: str) -> Optional[float]:
    """Kalshi team total 'Team over 1.5' / 'over 1.5 goals' -> 1.5 (direct half-line)."""
    m = re.search(rf"over\s+{_NUM}", str(text or "").lower())
    if not m:
        return None
    v = float(m.group(1))
    return v if (v * 2) % 2 != 0 else v - 0.5


def spread_cover_to_line(text: str) -> Optional[float]:
    """Kalshi spread phrasing -> the (positive) cover margin. 'wins by more than 1.5' -> 1.5;
    'by 2+' -> 1.5 (>=2 goal margin == covers -1.5). The team then covers the NEGATIVE of this."""
    t = str(text or "").lower()
    m = re.search(rf"more than\s+{_NUM}", t) or re.search(rf"by\s+(?:more than\s+)?{_NUM}", t)
    if not m:
        return None
    v = float(m.group(1))
    return v if (v * 2) % 2 != 0 else v - 0.5


# Halves: map either venue's phrasing to a canonical ('1h'|'2h', side) pair.
def half_result_side(text: str, *, home: str, away: str) -> Optional[tuple[str, str]]:
    """('1h'|'2h', 'home'|'draw'|'away') from a half-result label on EITHER venue, else None.
    Recognizes Kalshi 'Tie 1st Half' / '<Team> wins 1st Half' AND Poly 'Draw at halftime' /
    '<Team> leading at halftime' (and the 2H 'second half' equivalents)."""
    t = str(text or "").lower()
    half = "1h" if ("1st half" in t or "first half" in t or "halftime" in t or "half time" in t) else \
           ("2h" if ("2nd half" in t or "second half" in t) else None)
    if half is None:
        return None
    if "tie" in t or "draw" in t:
        return half, "draw"
    if _team(home) and _team(home) in _team(t):
        return half, "home"
    if _team(away) and _team(away) in _team(t):
        return half, "away"
    return None


def _after_colon(text: str) -> str:
    """Drop a leading label prefix ('Reg Time: ', 'Goal Diff Reg Time: ') so a CLEAN team fragment
    is left for equivalence-aware matching."""
    return str(text or "").rsplit(":", 1)[-1].strip()


def _team_role_in(fragment: str, ctx: "GameCtx") -> Optional[str]:
    """'home' / 'away' / None for a team-name fragment, matched EQUIVALENCE-aware: normalize_team folds
    'Ivory Coast' and 'Côte d'Ivoire' to the same key, so a title that spells the away team either way
    still resolves. A clean fragment is matched by equality; a longer one by normalized-substring."""
    cand = normalize_team(_after_colon(fragment))
    h, a = _team(ctx.home), _team(ctx.away)
    if cand == h:
        return "home"
    if cand == a:
        return "away"
    if h and h in cand:
        return "home"
    if a and a in cand:
        return "away"
    frag = _after_colon(fragment)                      # club-name equivalence (diacritics/exonyms)
    if ctx.home and soccer_names.same_club_with_alias(frag, ctx.home):
        return "home"
    if ctx.away and soccer_names.same_club_with_alias(frag, ctx.away):
        return "away"
    return None


# EXACT SCORE — canonical key is 'AWAYgoals-HOMEgoals' (for CIV vs NOR: 'CIVgoals-NORgoals'), derived
# CONSISTENTLY on both venues from the WINNER + score in the TITLE TEXT (never the ticker letters), so
# the keys collide only when the real scoreline matches (CIV-wins-2-1 != NOR-wins-2-1).
_SCORE_WIN_RE = re.compile(r"(.+?)\s+win(?:s)?\s+(\d+)\s*[-–]\s*(\d+)", re.IGNORECASE)
# Spread title: the covering team is named before 'wins by (more than) X' ('Norway wins by more than 1.5').
_SPREAD_WIN_RE = re.compile(r"(.+?)\s+win(?:s)?\s+by\b", re.IGNORECASE)


def kalshi_exact_score(title: str, ctx: "GameCtx") -> Optional[str]:
    """'AWAY-HOME' goal key from a Kalshi exact-score TITLE — 'Reg Time: Ivory Coast wins 2-1' ->
    '2-1' (CIV scored 2, NOR 1); 'Reg Time: Norway wins 2-1' -> '1-2'; 'Reg Time: Draw 1-1' -> '1-1'.
    None if the title can't be parsed or the winning team isn't this game's home/away."""
    core = _after_colon(title)
    low = core.lower()
    nums = re.findall(r"\d+", core)
    if "draw" in low or "tie" in low:
        if len(nums) >= 2:
            return f"{nums[0]}-{nums[1]}"
        return f"{nums[0]}-{nums[0]}" if nums else None
    m = _SCORE_WIN_RE.search(core)
    if not m:
        return None
    role = _team_role_in(m.group(1), ctx)
    a, b = m.group(2), m.group(3)                          # winner scored a, loser b
    if role == "away":
        return f"{a}-{b}"                                  # away won -> away=a, home=b
    if role == "home":
        return f"{b}-{a}"                                  # home won -> home=a, away=b
    return None


def poly_exact_score(title: str, ctx: "GameCtx") -> Optional[str]:
    """'AWAY-HOME' goal key from a Poly exact-score title — 'Côte d'Ivoire 2 - 1 Norway' -> '2-1'.
    Teams are identified BY NAME (equivalence-aware), so the title's team ORDER doesn't matter."""
    m = re.search(r"(.+?)\s+(\d+)\s*[-–]\s*(\d+)\s+(.+)", str(title or ""))
    if m:
        r1, r2 = _team_role_in(m.group(1), ctx), _team_role_in(m.group(4), ctx)
        goals: dict[str, str] = {}
        if r1:
            goals[r1] = m.group(2)
        if r2:
            goals[r2] = m.group(3)
        if "away" in goals and "home" in goals:
            return f"{goals['away']}-{goals['home']}"
    nums = re.findall(r"\d+", str(title or ""))             # bare symmetric draw ('1 - 1')
    if len(nums) >= 2 and nums[0] == nums[1]:
        return f"{nums[0]}-{nums[1]}"
    return None


# --------------------------------------------------------------------------- #
# Canonical outcome                                                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Outcome:
    """One canonical outcome on one venue. ``group`` is the venue-agnostic market id (shared by the
    sides of a 2-outcome market); ``side`` is the outcome within it. Equal ``twin_key`` across the
    two venues == the same bet."""
    market_type: str
    group: str
    side: str
    line: Optional[float]
    label: str = ""                  # team/player/score for display; identity already in group/side
    confidence: str = "high"

    @property
    def kind(self) -> str:
        return kind_for(self.market_type)

    def twin_key(self) -> str:
        return f"{self.group}##{self.side}"


def _o(market_type: str, group: str, side: str, line: Optional[float] = None, label: str = "",
       confidence: str = "high") -> Outcome:
    conf = "low" if market_type in ALERT_ONLY else confidence
    return Outcome(market_type, group, side, line, label, conf)


# --------------------------------------------------------------------------- #
# Context                                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class GameCtx:
    """The two teams of a game, with home/away fixed (Kalshi ticker is <AWAY><HOME>)."""
    home: str
    away: str

    def side_for_team(self, name: str) -> Optional[str]:
        """Which side of THIS game a market's team name refers to.

        The cheap normalized-substring test is tried first (it settles the common venue truncations),
        then the SOCCER CLUB-NAME matcher, which is what resolves the European spellings the substring
        test cannot: 'Omonia Nicosia' is 'AS Omónoia Leukosías' and 'Vikingur Reykjavik' is
        'KF Víkingur'. Without this the EVENT pairs but its team-named outcomes silently drop, leaving
        a game with only its draw node (live: Kairat/Omonia and Be'er Sheva/Vikingur got 1 node of 3)."""
        nt = _team(name)
        if _team(self.home) and (nt == _team(self.home) or _team(self.home) in nt):
            return "home"
        if _team(self.away) and (nt == _team(self.away) or _team(self.away) in nt):
            return "away"
        if self.home and soccer_names.same_club_with_alias(name, self.home):
            return "home"
        if self.away and soccer_names.same_club_with_alias(name, self.away):
            return "away"
        return None

    @property
    def pair(self) -> str:
        return _pair(self.home, self.away)


# --------------------------------------------------------------------------- #
# Canonical outcome BUILDERS — called by BOTH venues so twins share a group     #
# --------------------------------------------------------------------------- #
def mk_moneyline(side: str, label: str = "") -> Outcome:                       # 3-way
    return _o("moneyline", "moneyline", side, label=label)


def mk_half_result(half: str, side: str, label: str = "") -> Outcome:          # 3-way
    return _o(f"{half}_result", f"{half}_result", side, label=label)


def mk_total(line: float, side: str, half: str = "") -> Outcome:             # 2-way
    """Total GOALS O/U (full game, or a half via ``half`` in {'1h','2h'}). Kalshi 'Over X.5 goals'
    and Poly 'O/U X.5' / '1st Half O/U X.5' collapse to the same group."""
    mt = "total_goals" if not half else f"{half}_total"
    return _o(mt, f"{mt}|{line}", side, line=line)


def mk_team_total(team: str, line: float, side: str, half: str = "") -> Outcome:   # 2-way
    """One team's total goals O/U — full game ('team_total') or a half ('1h_team_total' /
    '2h_team_total'). A TEAM half-total is a DIFFERENT market than the GAME-WIDE half-total
    (mk_total(..., half)) — Kalshi's KXWC1HTOTAL is game-wide (any first-half goal, both teams), so a
    Poly '<Team> 1st Half O/U' must NEVER land in the game-wide 1h_total/2h_total node."""
    mt = "team_total" if not half else f"{half}_team_total"
    return _o(mt, f"{mt}|{_team(team)}|{line}", side, line=line, label=team)


def mk_corners(line: float, side: str, *, team: Optional[str] = None) -> Outcome:   # 2-way
    if team:
        return _o("team_corners", f"team_corners|{_team(team)}|{line}", side, line=line, label=team)
    return _o("corners", f"corners|{line}", side, line=line)


def mk_btts(half: str, side: str) -> Outcome:                                 # 2-way
    mt = "btts" if not half else f"{half}_btts"
    grp = "btts" if not half else f"{half}_btts"
    return _o(mt, grp, side)


def mk_spread(favored_team: str, margin: float, side: str, *, label: str = "") -> Outcome:   # 2-way
    """One side of ONE handicap line, keyed by the FAVORED team + margin so the two sides are genuine
    complements (and 'Norway -2.5' is NEVER conflated with 'Côte d'Ivoire -2.5', a DIFFERENT team's
    favorite handicap that can also lose). ``side`` is 'cover' (favored team wins by > margin) or
    'plus' (the underdog at +margin = the favored team fails to cover) — the two MECE outcomes."""
    return _o("spread", f"spread|{margin}|{_team(favored_team)}", side, line=margin, label=label)


def mk_advance(pair: str, team_side: str, *, label: str = "") -> Outcome:     # 2-way
    return _o("advance", f"advance|{pair}", team_side, label=label)


def mk_exact_score(score: str) -> Outcome:                                    # multi
    return _o("exact_score", "exact_score", score)


def mk_first_to_score(side: str, label: str = "") -> Outcome:                 # 3-way
    return _o("first_to_score", "first_to_score", side, label=label)


def player_surname(name: str) -> str:
    """Surname key for a player (last token of the normalized name) — Kalshi uses full names, Poly
    often only the surname, so we join on surname (low-confidence by construction)."""
    parts = normalize_player(name).split()
    return parts[-1] if parts else ""


def mk_player_goals(name: str, threshold: int) -> Outcome:                    # multi, alert-only
    return _o("player_goals", f"player_goals|{player_surname(name)}", f"{threshold}+",
              label=name)


# --------------------------------------------------------------------------- #
# Polymarket label parsers (the builder routes by sibling slug suffix)           #
# --------------------------------------------------------------------------- #
def poly_total_line(label: str) -> Optional[float]:
    """'O/U 2.5' -> 2.5 (full-game total goals)."""
    m = re.search(rf"o/?u\s+{_NUM}", str(label or "").lower())
    return float(m.group(1)) if m else None


def poly_corners_line(label: str) -> Optional[float]:
    """'O/U 8.5 Total Corners' / 'Over 8.5 corners' -> 8.5."""
    t = str(label or "").lower()
    if "corner" not in t:
        return None
    m = re.search(rf"{_NUM}", t)
    return float(m.group(1)) if m else None


def poly_team_total(label: str, ctx: GameCtx) -> Optional[tuple[str, float]]:
    """'Norway O/U 1.5' -> ('Norway', 1.5)."""
    m = re.search(rf"o/?u\s+{_NUM}", str(label or "").lower())
    if not m:
        return None
    for team in (ctx.home, ctx.away):
        if _team(team) and _team(team) in _team(label):
            return team, float(m.group(1))
    return None


def poly_player_goals(label: str) -> Optional[tuple[str, int]]:
    """'Haaland: 2+ goals' -> ('Haaland', 2); 'Haaland to score' -> ('Haaland', 1)."""
    t = str(label or "")
    if "goal" not in t.lower() and "score" not in t.lower():
        return None
    m = re.search(r"(\d+)\s*\+", t)
    thr = int(m.group(1)) if m else 1
    name = re.split(r":|\bto\b|\b\d", t)[0].strip(" -:")
    return (name, thr) if name else None


# --------------------------------------------------------------------------- #
# Kalshi router — one market -> its canonical outcome(s) + the Kalshi side       #
# --------------------------------------------------------------------------- #
def _series_prefix(event_ticker: str) -> str:
    return str(event_ticker or "").split("-", 1)[0]


def _team_in(text: str, ctx: GameCtx) -> Optional[str]:
    """Whichever of the game's two teams is named in ``text`` (home preferred), else None."""
    nt = _team(text)
    for team in (ctx.home, ctx.away):
        if _team(team) and _team(team) in nt:
            return team
    return None


def _player_threshold(sub: str) -> tuple[str, int]:
    """('Erling Haaland', 2) from 'Erling Haaland 2+'; threshold 1 when none stated."""
    m = re.search(r"(\d+)\s*\+", sub)
    thr = int(m.group(1)) if m else 1
    name = re.split(r"\b\d", sub)[0].strip(" -:")
    return name, thr


def kalshi_outcomes(market: dict, ctx: GameCtx) -> list[tuple[Outcome, str]]:
    """Canonical outcomes for one Kalshi market, each paired with the Kalshi SIDE (YES/NO) that backs
    it. Binary 2-outcome markets emit BOTH sides (YES=over/yes/cover/leftteam, NO=its complement);
    multi-outcome families (moneyline, exact score, …) emit only the YES outcome. Unrecognized
    markets yield []. Routes purely on the series prefix + the yes_sub_title (no network)."""
    series = _series_prefix(market.get("event_ticker"))
    sub = str(market.get("yes_sub_title") or market.get("title") or "").strip()
    title = str(market.get("title") or market.get("yes_sub_title") or "").strip()   # spreads/scores: parse the TITLE
    low = sub.lower()
    out: list[tuple[Outcome, str]] = []

    if series.endswith("GAME"):                                   # regulation moneyline (3-way)
        if "tie" in low or "draw" in low:
            out.append((mk_moneyline("draw", sub), "YES"))
        else:
            side = ctx.side_for_team(sub)
            if side:
                out.append((mk_moneyline(side, sub), "YES"))
        return out

    if series.endswith("TEAMTOTAL"):                             # team total goals (2-way)
        team = _team_in(sub, ctx)
        line = team_total_over_to_line(sub)
        if team and line is not None:
            out.append((mk_team_total(team, line, "over"), "YES"))
            out.append((mk_team_total(team, line, "under"), "NO"))
        return out

    if series.endswith("1HTOTAL") or series.endswith("2HTOTAL"):  # half total goals (2-way)
        half = "1h" if series.endswith("1HTOTAL") else "2h"
        line = goals_over_to_line(sub)
        if line is not None:
            out.append((mk_total(line, "over", half), "YES"))
            out.append((mk_total(line, "under", half), "NO"))
        return out

    if series.endswith("TCORNERS") or series.endswith("CORNERS"):  # corners (team or total) (2-way)
        team = _team_in(sub, ctx)
        line = corners_plus_to_line(sub)
        if line is not None:
            out.append((mk_corners(line, "over", team=team), "YES"))
            out.append((mk_corners(line, "under", team=team), "NO"))
        return out

    if series.endswith("TOTAL"):                                 # full-game total goals O/U (2-way)
        line = goals_over_to_line(sub)
        if line is not None:
            out.append((mk_total(line, "over"), "YES"))
            out.append((mk_total(line, "under"), "NO"))
        return out

    if series.endswith("BTTS"):                                  # both teams to score (+1H/2H) (2-way)
        half = "1h" if "1H" in series else ("2h" if "2H" in series else "")
        out.append((mk_btts(half, "yes"), "YES"))
        out.append((mk_btts(half, "no"), "NO"))
        return out

    if series.endswith("SPREAD"):                                # handicap (2-way; binary cover)
        half = "1h" if "1H" in series else ("2h" if "2H" in series else "")
        if half:
            return out                                           # full-game spreads only (clean twin)
        # Parse the COVERING team + line from the TITLE ('Goal Diff Reg Time: Norway wins by more
        # than 1.5 goals' -> Norway covers -1.5), equivalence-aware, so the side is the team actually
        # laying the handicap (never the ticker/sub's other label).
        m = _SPREAD_WIN_RE.search(_after_colon(title))
        role = _team_role_in(m.group(1), ctx) if m else None
        margin = spread_cover_to_line(title)
        if role and margin is not None:
            team = ctx.home if role == "home" else ctx.away   # the FAVORED team (wins by > margin)
            opp = ctx.away if role == "home" else ctx.home
            # The binary market's two MECE sides: YES = favored team covers -margin; NO = it fails
            # (= the underdog at +margin). Both keyed by the FAVORED team so they're true complements.
            out.append((mk_spread(team, margin, "cover", label=f"{team} -{margin}"), "YES"))
            out.append((mk_spread(team, margin, "plus", label=f"{opp} +{margin}"), "NO"))
        return out

    if series.endswith("ADVANCE"):                               # to advance (2-way)
        team = _team_in(sub, ctx)
        if team:
            opp = ctx.away if _team(team) == _team(ctx.home) else ctx.home
            out.append((mk_advance(ctx.pair, _team(team), label=team), "YES"))
            out.append((mk_advance(ctx.pair, _team(opp), label=opp), "NO"))
        return out

    if series.endswith("SCORE"):                                 # exact score (multi)
        score = kalshi_exact_score(title, ctx)                   # winner+score from the TITLE text
        if score:
            out.append((mk_exact_score(score), "YES"))
        return out

    if series.endswith("FTTS"):                                  # first to score (3-way)
        if "no goal" in low or "neither" in low or "no team" in low:
            out.append((mk_first_to_score("none", sub), "YES"))
        else:
            side = ctx.side_for_team(sub)
            if side:
                out.append((mk_first_to_score(side, sub), "YES"))
        return out

    if series.endswith("1H") or series.endswith("2H"):           # half result (3-way)
        hr = half_result_side(sub + (" 1st half" if series.endswith("1H") else " 2nd half"),
                              home=ctx.home, away=ctx.away)
        if hr:
            half, side = hr
            out.append((mk_half_result(half, side, sub), "YES"))
        return out

    if series.endswith("GOAL"):                                  # anytime/N+ scorer (multi, alert-only)
        name, thr = _player_threshold(sub)
        if name:
            out.append((mk_player_goals(name, thr), "YES"))
        return out

    if series.endswith("SOA"):                                   # shot on goal (alert-only, fuzzy)
        name, _ = _player_threshold(sub)
        if name:
            out.append((_o("soa", f"soa|{player_surname(name)}", "yes", label=name), "YES"))
            out.append((_o("soa", f"soa|{player_surname(name)}", "no", label=name), "NO"))
        return out

    return out


