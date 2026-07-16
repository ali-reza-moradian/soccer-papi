"""MLB sport adapter for the GenZ tree builder (a SECOND sport; the soccer pipeline is untouched).

Discovers MLB games on Kalshi (series KXMLBGAME moneyline + KXMLBTOTAL total-runs ladder), pairs each
to its Polymarket game event (one event per game, all markets nested), and emits the same tree-node
shape the engine already prices. Two market families per game:

    ml2         moneyline, 2-way MECE (no ties — extra innings decide; aligned on both venues)
    total_runs  Over/Under runs, one node pair per line PRESENT ON BOTH venues

THE RAIN GUARD (the corners lesson, transplanted): a rain-shortened game can settle DIFFERENTLY across
venues on TOTALS (Kalshi may void to a fair price; Poly settles on the official — possibly shortened —
final). We fetch BOTH venues' resolution texts on every node, parse them (parse_mlb_shortened_rule),
and flag total_runs nodes with settlement_risk so they are excluded from would_trade AND from the paper
maker, and rendered with a RAIN RULE chip. Moneyline is aligned (who wins is who wins) and not flagged.

Ticker facts (verified live 2026-07): KXMLBGAME-<YY><MON><DD><HHMM><AWAY><HOME>[G1|G2] (time ET; a
doubleheader carries a G1/G2 SUFFIX). Each GAME event has one market per team (ticker '<event>-<CODE>',
yes_sub_title = team name). Poly event slug mlb-<away>-<home>-<YYYY-MM-DD>; startTime is the precise UTC
first pitch. Codes mostly match across venues; the uncertain ones are LEARNED at build time.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - zoneinfo should exist on 3.9+, but never crash the import
    _ET = None

from .. import polymarket as pm
from . import config as gz_config
from .tree_builder import _poly_market_fee   # the generic gamma feeSchedule extraction (reused)

MLB_TEAM_MAP_PATH = os.path.join(gz_config.GENZ_DIR, "mlb_team_map.json")


# --------------------------------------------------------------------------- #
# 30-team crosswalk: KALSHI code -> (best-guess POLY code, full name).           #
# The uncertain poly codes are DISCOVERED/OVERRIDDEN at build time (learned map). #
# --------------------------------------------------------------------------- #
_TEAMS: list[tuple[str, str, str]] = [
    # kalshi_code, poly_code(guess), full_name
    ("NYY", "nyy", "New York Yankees"), ("BOS", "bos", "Boston Red Sox"),
    ("TB", "tb", "Tampa Bay Rays"), ("TOR", "tor", "Toronto Blue Jays"),
    ("BAL", "bal", "Baltimore Orioles"), ("CLE", "cle", "Cleveland Guardians"),
    ("MIN", "min", "Minnesota Twins"), ("DET", "det", "Detroit Tigers"),
    ("KC", "kc", "Kansas City Royals"), ("CWS", "cws", "Chicago White Sox"),
    ("HOU", "hou", "Houston Astros"), ("SEA", "sea", "Seattle Mariners"),
    ("TEX", "tex", "Texas Rangers"), ("LAA", "laa", "Los Angeles Angels"),
    ("ATH", "ath", "Athletics"), ("ATL", "atl", "Atlanta Braves"),
    ("PHI", "phi", "Philadelphia Phillies"), ("NYM", "nym", "New York Mets"),
    ("MIA", "mia", "Miami Marlins"), ("WSH", "wsh", "Washington Nationals"),
    ("MIL", "mil", "Milwaukee Brewers"), ("CHC", "chc", "Chicago Cubs"),
    ("STL", "stl", "St. Louis Cardinals"), ("CIN", "cin", "Cincinnati Reds"),
    ("PIT", "pit", "Pittsburgh Pirates"), ("LAD", "lad", "Los Angeles Dodgers"),
    ("SD", "sd", "San Diego Padres"), ("SF", "sf", "San Francisco Giants"),
    ("ARI", "ari", "Arizona Diamondbacks"), ("COL", "col", "Colorado Rockies"),
]
# Kalshi sometimes writes the White Sox / Cubs codes differently; alias both to the same team.
_KALSHI_ALIASES = {"CHW": "CWS", "SDP": "SD", "SFG": "SF", "TBR": "TB", "KCR": "KC", "WAS": "WSH",
                   "AZ": "ARI", "OAK": "ATH"}

def _norm(s: str) -> str:
    """Lowercase alphanumerics only — for tolerant team-name matching across venues."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


_POLY_GUESS: dict[str, str] = {k: p for k, p, _ in _TEAMS}          # kalshi code -> poly code (guess)
_FULL_NAME: dict[str, str] = {k: n for k, _, n in _TEAMS}          # kalshi code -> full name
# normalized full name / city / nickname -> kalshi code (titles are truth; this resolves them).
_NAME_TO_KCODE: dict[str, str] = {}
for _k, _p, _n in _TEAMS:
    _NAME_TO_KCODE[_norm(_n)] = _k
    _parts = _n.split()
    if len(_parts) >= 2:
        _NAME_TO_KCODE.setdefault(_norm(_parts[-1]), _k)            # nickname (Yankees, Mets, ...)
        _NAME_TO_KCODE.setdefault(_norm(" ".join(_parts[:-1])), _k)  # city (New York, Los Angeles, ...)


def _kalshi_code(code: str) -> str:
    c = str(code or "").upper()
    return _KALSHI_ALIASES.get(c, c)


def load_team_map() -> dict[str, str]:
    """The LEARNED kalshi_code -> poly_code overrides persisted at build time (empty if none yet)."""
    try:
        with open(MLB_TEAM_MAP_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return {str(k).upper(): str(v).lower() for k, v in d.items()} if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def poly_code(kalshi_code: str, learned: Optional[dict[str, str]] = None) -> str:
    """The Polymarket slug code for a Kalshi team code: the LEARNED map wins, else the hardcoded guess,
    else the lowercased code itself."""
    k = _kalshi_code(kalshi_code)
    if learned and k in learned:
        return learned[k]
    return _POLY_GUESS.get(k, k.lower())


def learn_team_map(series_events: list[dict], log: Any = None) -> dict[str, str]:
    """Reconcile the hardcoded poly-code guesses against ACTUAL Polymarket MLB slugs: for each event
    slug 'mlb-<a>-<b>-<date>', match each code to a team via the event's title names and record the
    real poly code. Persist to mlb_team_map.json and return it. Never raises."""
    learned: dict[str, str] = dict(load_team_map())
    try:
        for ev in series_events or []:
            if not isinstance(ev, dict):
                continue
            m = re.match(r"^mlb-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})", str(ev.get("slug") or ""))
            title = str(ev.get("title") or "")
            if not m or " vs" not in title.lower():
                continue
            names = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.IGNORECASE)
            if len(names) != 2:
                continue
            for slug_code, name in ((m.group(1), names[0]), (m.group(2), names[1])):
                kcode = _NAME_TO_KCODE.get(_norm(name))
                if kcode and _POLY_GUESS.get(kcode) != slug_code:
                    learned[kcode] = slug_code                     # a real, verified poly code
        if learned:
            os.makedirs(os.path.dirname(MLB_TEAM_MAP_PATH), exist_ok=True)
            tmp = MLB_TEAM_MAP_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(learned, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, MLB_TEAM_MAP_PATH)
    except Exception as exc:  # noqa: BLE001 - learning is best-effort, never fatal
        if log:
            log.warning("[MLB] team-map learning failed (%s) — using hardcoded guesses.", exc)
    return learned


# --------------------------------------------------------------------------- #
# Rain guard — parse each venue's resolution text                                #
# --------------------------------------------------------------------------- #
_VOID_SHORT_PATTERNS = (
    "abandoned before", "last fair market price", "innings if the home team",
    "called before", "shortened game will", "does not become an official",
)
_OFFICIAL_PATTERNS = (
    "official result", "official final statistics", "regardless of how many innings",
    "mlb declares", "official game", "final statistics of the event",
)


def parse_mlb_shortened_rule(text: str) -> Optional[str]:
    """Classify a resolution text for how it settles a RAIN-SHORTENED game:
        'void_short'      — voids / uses a fair price when the game is called short
        'official_result' — settles on the official (possibly shortened) final
        None              — neither pattern present (caller treats totals conservatively as risk)
    Cancellation/postponement language ('resolve to a fair price' if CANCELLED) is NOT void_short — only
    SHORTENED-game handling counts, so aligned moneyline texts don't get mis-flagged."""
    low = str(text or "").lower()
    if any(p in low for p in _VOID_SHORT_PATTERNS):
        return "void_short"
    if any(p in low for p in _OFFICIAL_PATTERNS):
        return "official_result"
    return None


# --------------------------------------------------------------------------- #
# Discovery                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class MLBGame:
    game_id: str                 # full Kalshi event suffix incl. any G1/G2 (unique per game)
    event_suffix: str
    date: str                    # YYYY-MM-DD (US-local game date)
    away_code: str               # kalshi codes (already de-aliased)
    home_code: str
    away: str                    # full names
    home: str
    kickoff_iso: str             # provisional ET->UTC (refined from Poly startTime when paired)
    dh: str = ""                 # '', 'G1', 'G2'
    game_markets: list = field(default_factory=list)   # KXMLBGAME markets (one per team)
    total_markets: list = field(default_factory=list)  # KXMLBTOTAL markets (the ladder)
    poly_base_slug: str = ""


_SUFFIX_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})(\d{4})([A-Z]+?)(G\d)?$")


def _decode_event_suffix(suffix: str) -> Optional[tuple[str, str, str]]:
    """('YYYY-MM-DD', 'HHMM', 'G1'|'') from a KXMLB* event suffix, or None. The concatenated team codes
    are NOT split here (ambiguous) — the caller derives away/home from the per-team market tickers."""
    m = _SUFFIX_RE.match(suffix or "")
    if not m:
        return None
    try:
        d = datetime.strptime(m.group(1).title(), "%y%b%d")
    except ValueError:
        return None
    return d.strftime("%Y-%m-%d"), m.group(2), (m.group(4) or "")


def _et_to_utc(date: str, hhmm: str) -> str:
    """A Kalshi ticker's ET wall-clock (date + HHMM) as a UTC ISO instant (DST-correct via zoneinfo)."""
    try:
        naive = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H%M")
        if _ET is not None:
            return naive.replace(tzinfo=_ET).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return naive.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # fallback: treat as UTC
    except ValueError:
        return f"{date}T12:00:00Z"


def _codes_from_markets(markets: list[dict], concat: str) -> Optional[tuple[str, str]]:
    """(away_code, home_code) from a GAME event's per-team market tickers, ordered by which prefixes the
    event's concatenated <AWAY><HOME> codes. Returns None if the two codes don't reconstruct the concat."""
    codes = []
    for m in markets:
        tk = str(m.get("ticker") or "")
        suf = tk.rsplit("-", 1)[-1].upper() if "-" in tk else ""
        if suf and suf not in codes:
            codes.append(suf)
    if len(codes) != 2:
        return None
    a, b = codes
    up = concat.upper()
    if up.startswith(a) and up.endswith(b):
        return _kalshi_code(a), _kalshi_code(b)
    if up.startswith(b) and up.endswith(a):
        return _kalshi_code(b), _kalshi_code(a)
    return None


def _name_from_market(m: dict) -> str:
    return str(m.get("yes_sub_title") or "").strip()


def discover_mlb_games(kalshi_client: Any, *, now: datetime, lookahead_hours: float,
                       series: list[str], log: Any = None) -> list[MLBGame]:
    """Every KXMLBGAME event opening within [now, now+lookahead], with its per-team GAME markets and its
    KXMLBTOTAL ladder attached. Away/home are resolved from the market tickers + TITLES (titles are
    truth); an event that won't parse is logged and skipped."""
    try:
        game_markets = kalshi_client.iter_markets(series_ticker="KXMLBGAME", status="open")
    except Exception as exc:  # noqa: BLE001
        if log:
            log.warning("[MLB] KXMLBGAME discovery failed: %s — no games this build.", exc)
        return []
    total_markets = []
    if "KXMLBTOTAL" in series:
        try:
            total_markets = kalshi_client.iter_markets(series_ticker="KXMLBTOTAL", status="open")
        except Exception as exc:  # noqa: BLE001
            if log:
                log.warning("[MLB] KXMLBTOTAL discovery failed: %s — totals absent this build.", exc)

    by_game: dict[str, list[dict]] = {}
    by_total: dict[str, list[dict]] = {}
    for m in game_markets:
        by_game.setdefault(_suffix_of(m), []).append(m)
    for m in total_markets:
        by_total.setdefault(_suffix_of(m), []).append(m)

    horizon = now.timestamp() + lookahead_hours * 3600.0
    out: list[MLBGame] = []
    for suffix, mkts in by_game.items():
        decoded = _decode_event_suffix(suffix)
        if not decoded:
            if log:
                log.warning("[MLB] unparseable event %s — skipping.", suffix)
            continue
        date, hhmm, dh = decoded
        m = _SUFFIX_RE.match(suffix)
        concat = m.group(3) if m else ""
        codes = _codes_from_markets(mkts, concat)
        if not codes:
            if log:
                log.warning("[MLB] unparseable event %s (codes %s don't match tickers) — skipping.",
                            suffix, concat)
            continue
        away_c, home_c = codes
        away = _title_team(mkts, away_c)
        home = _title_team(mkts, home_c)
        kickoff = _et_to_utc(date, hhmm)
        ts = _parse_iso(kickoff)
        if ts is None or ts > horizon or ts < now.timestamp() - 12 * 3600.0:
            continue
        out.append(MLBGame(game_id=suffix, event_suffix=suffix, date=date, away_code=away_c,
                           home_code=home_c, away=away, home=home, kickoff_iso=kickoff, dh=dh,
                           game_markets=list(mkts), total_markets=list(by_total.get(suffix, []))))
    return out


def _suffix_of(m: dict) -> str:
    et = str(m.get("event_ticker") or "")
    return et.split("-", 1)[1] if "-" in et else et


def _title_team(markets: list[dict], code: str) -> str:
    """Full team name for a code: prefer the crosswalk, else the market's yes_sub_title, else the code."""
    if code in _FULL_NAME:
        return _FULL_NAME[code]
    for m in markets:
        tk = str(m.get("ticker") or "")
        if tk.rsplit("-", 1)[-1].upper() == code:
            nm = _name_from_market(m)
            if nm:
                return nm
    return code


def _parse_iso(v: Any) -> Optional[float]:
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Polymarket event resolution (slug + startTime verification + fallback)          #
# --------------------------------------------------------------------------- #
def _poly_event(poly_client: Any, slug: str) -> Optional[dict]:
    try:
        evs = poly_client.events_by_slug(slug)
    except Exception:  # noqa: BLE001
        return None
    evs = evs if isinstance(evs, list) else []
    return evs[0] if evs else None


def _within_90min(poly_start: Optional[str], kalshi_utc: str) -> bool:
    a, b = _parse_iso(poly_start), _parse_iso(kalshi_utc)
    return a is not None and b is not None and abs(a - b) <= 90 * 60


def _delta_min(poly_start: Optional[str], kalshi_utc: str) -> Any:
    """|poly_start - kalshi first pitch| in whole minutes (for the DH accept/refuse log), or '?'."""
    a, b = _parse_iso(poly_start), _parse_iso(kalshi_utc)
    return "?" if a is None or b is None else int(round(abs(a - b) / 60.0))


def resolve_poly_event(game: MLBGame, series_events: list[dict], learned: dict[str, str],
                       poly_client: Any, log: Any = None) -> Optional[dict]:
    """The Polymarket game event for this Kalshi game. Primary: the constructed slug, ACCEPTED only if
    its startTime is within +-90min of the Kalshi first pitch (guards doubleheaders — the wrong game 2
    is refused). DOUBLEHEADERS use an EXPLICIT slug order: dh=='G2' tries '<base>-dh2' FIRST then base;
    dh=='G1' tries base then '<base>-dh1'; a non-DH game just tries base. Every candidate is still
    ±90min-verified, and every DH game logs an accepted/refused line per candidate. Fallback: scan the
    mlb series for an event on the same DATE whose two team names match (same ±90min check, WARNING when
    used). None -> the game stays one-sided."""
    base = f"mlb-{poly_code(game.away_code, learned)}-{poly_code(game.home_code, learned)}-{game.date}"
    game.poly_base_slug = base
    if game.dh == "G2":
        candidates = [f"{base}-dh2", base]
    elif game.dh == "G1":
        candidates = [base, f"{base}-dh1"]
    else:
        candidates = [base]
    for slug in candidates:
        ev = _poly_event(poly_client, slug)
        if not ev:
            continue
        ok = _within_90min(ev.get("startTime"), game.kickoff_iso)
        if game.dh and log:
            log.info("[MLB][DH] %s -> %s (%s Δmin=%s)", game.game_id, slug,
                     "accepted" if ok else "refused", _delta_min(ev.get("startTime"), game.kickoff_iso))
        if ok:
            game.poly_base_slug = slug
            return ev
    # Fallback: scan the series for a same-date, same-teams event within the time window.
    want = {_norm(game.away), _norm(game.home)}
    for e in series_events or []:
        if not isinstance(e, dict):
            continue
        eslug = str(e.get("slug") or "")
        if game.date not in eslug or not _within_90min(e.get("startTime"), game.kickoff_iso):
            continue
        title = str(e.get("title") or "")
        names = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.IGNORECASE)
        if len(names) == 2 and {_norm(names[0]), _norm(names[1])} == want:
            game.poly_base_slug = eslug
            if log:
                log.warning("[MLB] %s %s @ %s: primary slug %s missed — resolved via series scan to %s.",
                            game.game_id, game.away, game.home, base, eslug)
            return e
    if log:
        log.warning("[MLB] %s %s @ %s: NO Poly event within 90min of first pitch (tried %s) — one-sided.",
                    game.game_id, game.away, game.home, base)
    return None


# --------------------------------------------------------------------------- #
# Kalshi options (moneyline + totals)                                            #
# --------------------------------------------------------------------------- #
_KALSHI_TEXT_KEYS = ("rules_primary", "rules_secondary", "subtitle", "title")


def _kalshi_text(m: dict) -> str:
    return " ".join(str(m.get(k) or "") for k in _KALSHI_TEXT_KEYS)


_OVER_LINE_RE = re.compile(r"over\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _total_line_of(m: dict) -> Optional[float]:
    """The O/U line of a KXMLBTOTAL market from its yes_sub_title ('Over 8.5 runs scored' -> 8.5)."""
    mm = _OVER_LINE_RE.search(str(m.get("yes_sub_title") or m.get("title") or ""))
    return float(mm.group(1)) if mm else None


def kalshi_mlb_options(game: MLBGame) -> dict[str, dict]:
    """{twin_key: option} for the game's Kalshi side: moneyline (ml2, one node per team) + total_runs
    (Over=YES, Under=NO per line). Each option carries the Kalshi resolution text for the rain guard."""
    opts: dict[str, dict] = {}
    for m in game.game_markets:
        code = str(m.get("ticker") or "").rsplit("-", 1)[-1].upper()
        role = "away" if _kalshi_code(code) == game.away_code else ("home" if _kalshi_code(code) == game.home_code else "")
        if not role:
            continue
        team = game.away if role == "away" else game.home
        opts[f"ml2|{role}"] = {
            "market_type": "ml2", "market_key": "ml2", "side": role, "line": None, "kind": "2way",
            "confidence": "high", "outcome_label": team, "kalshi_ticker": m.get("ticker"),
            "kalshi_side": "YES", "kalshi_text": _kalshi_text(m),
        }
    for m in game.total_markets:
        line = _total_line_of(m)
        if line is None:
            continue
        key = f"total_runs|{line}"
        text = _kalshi_text(m)
        opts[f"{key}|over"] = {
            "market_type": "total_runs", "market_key": key, "side": "over", "line": line, "kind": "2way",
            "confidence": "high", "outcome_label": f"Over {line}", "kalshi_ticker": m.get("ticker"),
            "kalshi_side": "YES", "kalshi_text": text}
        opts[f"{key}|under"] = {
            "market_type": "total_runs", "market_key": key, "side": "under", "line": line, "kind": "2way",
            "confidence": "high", "outcome_label": f"Under {line}", "kalshi_ticker": m.get("ticker"),
            "kalshi_side": "NO", "kalshi_text": text}
    return opts


# --------------------------------------------------------------------------- #
# Polymarket options (moneyline + totals) from the single game event             #
# --------------------------------------------------------------------------- #
_OU_TITLE_RE = re.compile(r"o/?u\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

# IDENTITY BEFORE SHAPE. The Poly game event nests MANY sub-markets that share the full-game
# moneyline/total SHAPE — 1st-5-innings totals & moneyline, team totals, run lines/spreads (-1.5),
# NRFI, series, player props. Matching by shape alone mispairs them to the Kalshi full-game legs
# (verified live: an F5 Over got paired to a full-game >=7, and a run line got paired to the
# moneyline). We EXCLUDE any sub-market whose identity text names one of those, then require a POSITIVE
# full-game identity before a survivor is kept. The gate reads title/question/groupItemTitle/slug ONLY
# (never the resolution description, which legitimately says "innings").
_MLB_EXCLUDE_RE = re.compile(
    r"(1st|first)\s*(5|five)|inning|team\s+total|nrfi|spread|run\s*line|[+-]\s*1\.5|"
    r"series|to\s+hit|strikeout|home\s+run|lead|score\s+first",
    re.IGNORECASE)


def _mkt_text(m: dict) -> str:
    return str(m.get("groupItemTitle") or m.get("question") or m.get("title") or "")


def _mlb_identity_text(m: dict) -> str:
    """The identity-bearing fields of a Poly market (title/question/groupItemTitle/slug), joined — the
    ONLY text the exclusion gate reads (the resolution description is deliberately left out)."""
    return " ".join(str(m.get(k) or "") for k in ("groupItemTitle", "question", "title", "slug"))


def _is_ml2_market(title: str, game: MLBGame) -> bool:
    """Positive MONEYLINE identity: the title says 'moneyline', OR it is exactly '<A> vs/@ <B>' naming
    THIS game's two teams with NO other qualifier token (a ':' sub-market suffix disqualifies it). The
    exclusion gate has already removed run lines / F5 / spreads before this runs."""
    low = title.lower()
    if "moneyline" in low:
        return True
    if ":" in title:                                       # e.g. '<A> vs <B>: O/U 8.5' -> not bare ML
        return False
    mm = re.match(r"^\s*(.+?)\s+(?:vs\.?|@)\s+(.+?)\s*$", title, re.IGNORECASE)
    if not mm:
        return False
    return {_role_of_team(mm.group(1), game), _role_of_team(mm.group(2), game)} == {"away", "home"}


def _is_total_runs_market(m: dict, game: MLBGame) -> bool:
    """Positive FULL-GAME TOTAL identity: groupItemTitle is a Totals label, OR the market text says
    'total runs', OR it names BOTH of this game's teams (so a full-game 'O/U 8.5' — whose question is
    '<A> vs <B>: O/U 8.5' — pairs, but a bare shape-only Over/Under never does). F5 totals are already
    excluded by the gate, so this only sees full-game candidates."""
    git = str(m.get("groupItemTitle") or "").strip().lower()
    if git in ("totals", "over/under"):
        return True
    text = _norm(" ".join(str(m.get(k) or "") for k in ("groupItemTitle", "question", "title")))
    if "totalruns" in text:
        return True
    a, h = _norm(game.away), _norm(game.home)
    return bool(a) and bool(h) and a in text and h in text


def poly_mlb_options(event: dict, game: MLBGame, *, log: Any = None, game_id: str = "") -> dict[str, dict]:
    """{twin_key: option} for the game's Polymarket side: moneyline (team outcome tokens) + FULL-GAME
    total_runs (Over/Under tokens per line). IDENTITY BEFORE SHAPE: every sub-market is first run through
    the exclusion gate (F5 / run line / spread / NRFI / team total / series / props — logged once each),
    then a survivor must clear the family's POSITIVE identity. If two SURVIVING markets map to the same
    twin_key the key is AMBIGUOUS and DROPPED (never guessed). Carries each market's fee + description."""
    if not isinstance(event, dict):
        return {}
    staged: dict[str, list[tuple[dict, str]]] = {}        # twin_key -> [(option, source_title), ...]
    excluded_seen: set[str] = set()

    def _stage(key: str, opt: dict, src_title: str) -> None:
        staged.setdefault(key, []).append((opt, src_title))

    for m in event.get("markets") or []:
        if not isinstance(m, dict):
            continue
        title = _mkt_text(m)
        # (a) EXCLUSION GATE — skip any sub-market whose identity names a non-full-game family.
        if _MLB_EXCLUDE_RE.search(_mlb_identity_text(m)):
            label = title or (_mlb_identity_text(m).strip()[:60])
            if log and label not in excluded_seen:
                excluded_seen.add(label)
                log.info("[MLB] excluded sub-market: %s", label)
            continue
        outs = pm._as_list(m.get("outcomes"))
        toks = pm._as_list(m.get("clobTokenIds"))
        fee = _poly_market_fee(m)
        desc = str(m.get("description") or "")
        norm_outs = [str(o).strip().lower() for o in outs]
        mm = _OU_TITLE_RE.search(title)
        # (c) total_runs: O/U shape + no exclusion (above) + POSITIVE full-game total identity.
        if (mm and len(toks) >= 2 and {"over", "under"} <= set(norm_outs)
                and _is_total_runs_market(m, game)):
            line = float(mm.group(1))
            over_tok = toks[norm_outs.index("over")]
            under_tok = toks[norm_outs.index("under")]
            key = f"total_runs|{line}"
            _stage(f"{key}|over", _p_opt("total_runs", key, "over", line, f"O/U {line} Over",
                                         over_tok, "Over", fee, desc), title)
            _stage(f"{key}|under", _p_opt("total_runs", key, "under", line, f"O/U {line} Under",
                                          under_tok, "Under", fee, desc), title)
            continue
        # (b) ml2: two TEAM outcomes for THIS game + POSITIVE moneyline identity.
        if (len(outs) == 2 and len(toks) >= 2 and not ({"over", "under", "yes", "no"} & set(norm_outs))
                and _is_ml2_market(title, game)):
            roles = [_role_of_team(o, game) for o in outs]
            if set(roles) == {"away", "home"}:
                for tok, o, role in zip(toks, outs, roles):
                    team = game.away if role == "away" else game.home
                    _stage(f"ml2|{role}", _p_opt("ml2", "ml2", role, None, team, tok, str(o), fee, desc),
                           title)

    # (d) COLLISION RULE — a twin_key produced by TWO surviving markets is ambiguous; drop it entirely.
    opts: dict[str, dict] = {}
    for key, entries in staged.items():
        if len(entries) > 1:
            titles = " | ".join(sorted({t for _, t in entries}))
            if log:
                log.warning("[MLB] %s twin_key collision on %s — DROPPED (markets: %s).",
                            game_id or game.game_id, key, titles)
            continue
        opts[key] = entries[0][0]
    return opts


def _p_opt(market_type: str, key: str, side: str, line: Optional[float], label: str, token: str,
           poly_side: str, fee: dict, desc: str) -> dict:
    return {"market_type": market_type, "market_key": key, "side": side, "line": line, "kind": "2way",
            "confidence": "high", "outcome_label": label, "poly_token_id": str(token),
            "poly_side": poly_side, "poly_fee_enabled": fee["enabled"], "poly_fee_rate": fee["rate"],
            "poly_fee_taker_only": fee["taker_only"], "poly_text": desc}


def _role_of_team(name: str, game: MLBGame) -> str:
    n = _norm(name)
    if n and (n == _norm(game.away) or _norm(game.away) in n or n in _norm(game.away)):
        return "away"
    if n and (n == _norm(game.home) or _norm(game.home) in n or n in _norm(game.home)):
        return "home"
    return ""


# --------------------------------------------------------------------------- #
# Join + rain guard                                                              #
# --------------------------------------------------------------------------- #
def join_mlb(k_opts: dict[str, dict], p_opts: dict[str, dict], *, log: Any = None,
             game_id: str = "") -> tuple[list[dict], list[dict]]:
    """Pair Kalshi<->Poly options by twin_key into tree nodes. total_runs nodes get the RAIN GUARD:
    each carries both venues' settlement_texts and a settlement_risk flag unless BOTH parse clean
    official_result. Moneyline (ml2) is aligned and never flagged. Unpaired keys -> unmatched."""
    nodes: list[dict] = []
    for key in sorted(set(k_opts) & set(p_opts)):
        k, p = k_opts[key], p_opts[key]
        node = {
            "twin_key": key, "market_type": k["market_type"], "market_key": k["market_key"],
            "side": k["side"], "outcome_label": k.get("outcome_label") or p.get("outcome_label"),
            "line": k["line"], "kind": k["kind"], "confidence": k["confidence"],
            "kalshi_ticker": k["kalshi_ticker"], "kalshi_side": k["kalshi_side"],
            "poly_token_id": p["poly_token_id"], "poly_side": p["poly_side"],
            "poly_fee_enabled": p.get("poly_fee_enabled", False),
            "poly_fee_rate": p.get("poly_fee_rate", 0.0),
            "poly_fee_taker_only": p.get("poly_fee_taker_only", False),
        }
        if k["market_type"] == "total_runs":
            _apply_rain_guard(node, k.get("kalshi_text", ""), p.get("poly_text", ""), log, game_id)
        nodes.append(node)
    unmatched: list[dict] = []
    for key in sorted(set(k_opts) - set(p_opts)):
        o = k_opts[key]
        unmatched.append({"venue": "kalshi", "market_type": o["market_type"],
                          "outcome_label": o.get("outcome_label"), "identifier": o.get("kalshi_ticker"),
                          "reason": "one_venue_only"})
    for key in sorted(set(p_opts) - set(k_opts)):
        o = p_opts[key]
        unmatched.append({"venue": "polymarket", "market_type": o["market_type"],
                          "outcome_label": o.get("outcome_label"), "identifier": o.get("poly_token_id"),
                          "reason": "one_venue_only"})
    return nodes, unmatched


def _apply_rain_guard(node: dict, kalshi_text: str, poly_text: str, log: Any, game_id: str) -> None:
    """Set node['settlement_texts'] and (for a rain-rule mismatch) node['settlement_risk'] on a
    total_runs node. Conservative: unless BOTH sides parse official_result, the node is flagged."""
    kr = parse_mlb_shortened_rule(kalshi_text)
    pr = parse_mlb_shortened_rule(poly_text)
    node["settlement_texts"] = {"kalshi": kalshi_text[:600], "poly": poly_text[:600]}
    node["kalshi_rule"] = kr
    node["poly_rule"] = pr
    if kr == "official_result" and pr == "official_result":
        if log:
            log.warning("[MLB] %s %s: BOTH venues parse official_result on totals — Kalshi may have "
                        "changed terms; totals now clean (verify).", game_id, node["market_key"])
        return
    if kr == "void_short" and pr == "official_result":
        node["settlement_risk"] = "mlb_rain_rule"
    else:
        node["settlement_risk"] = "unparsed_settlement"   # conservative: either side unknown -> risk
    if log:
        log.warning("[MLB] %s %s: settlement_risk=%s (kalshi=%s poly=%s) — excluded from would_trade "
                    "+ paper maker (rain rule).", game_id, node["market_key"],
                    node["settlement_risk"], kr, pr)


# --------------------------------------------------------------------------- #
# The MLB SportSpec                                                              #
# --------------------------------------------------------------------------- #
class MLBSpec:
    """The MLB adapter consumed by tree_builder.build_tree(spec=MLB_SPEC)."""

    name = "mlb"

    def paths(self) -> gz_config.SportPaths:
        return gz_config.paths_for_sport("mlb")

    def game_id(self, game: MLBGame) -> str:
        return game.game_id

    def discover_games(self, kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
                       now: datetime, log: Any = None) -> tuple[list[MLBGame], dict[str, Any]]:
        games = discover_mlb_games(kalshi_client, now=now, lookahead_hours=cfg.lookahead_hours,
                                   series=list(cfg.kalshi_series), log=log)
        try:
            series_events = poly_client.events_by_series(cfg.poly_series_slug, closed=False)
        except Exception as exc:  # noqa: BLE001
            if log:
                log.warning("[MLB] poly series %s fetch failed: %s — slug-only resolution this build.",
                            cfg.poly_series_slug, exc)
            series_events = []
        learned = learn_team_map(series_events, log)
        return games, {"series_events": series_events, "learned": learned}

    def pair_markets(self, kalshi_client: Any, poly_client: Any, game: MLBGame, poly_ctx: dict,
                     cfg: gz_config.GenzConfig, *, log: Any = None) -> dict[str, Any]:
        series_events = poly_ctx.get("series_events") or []
        learned = poly_ctx.get("learned") or {}
        ev = resolve_poly_event(game, series_events, learned, poly_client, log)
        k_opts = kalshi_mlb_options(game)
        p_opts = poly_mlb_options(ev, game, log=log, game_id=game.game_id) if ev else {}
        kickoff = str((ev or {}).get("startTime") or "") or game.kickoff_iso
        if not _parse_iso(kickoff):
            kickoff = game.kickoff_iso
        nodes, unmatched = join_mlb(k_opts, p_opts, log=log, game_id=game.game_id)
        risk = sum(1 for n in nodes if n.get("settlement_risk"))
        entry = {
            "kalshi_suffix": game.event_suffix, "poly_base_slug": game.poly_base_slug,
            "home": game.home, "away": game.away, "home_code": game.home_code,
            "away_code": game.away_code, "date": game.date, "kickoff_utc": kickoff,
            "doubleheader": game.dh, "sport": "mlb", "nodes": nodes, "unmatched": unmatched,
            "coverage": {"kalshi_ok": 1, "kalshi_failed": [], "poly_ok": 1 if ev else 0,
                         "poly_failed": [] if ev else [game.poly_base_slug],
                         "settlement_risk_nodes": risk, "period_mismatch_dropped": 0},
        }
        if log:
            two_way = sum(1 for n in nodes if n["kind"] == "2way")
            log.info("[MLB] %s %s @ %s: %d node(s) (%d 2-way, %d rain-risk), %d unmatched | poly=%s.",
                     game.game_id, game.away, game.home, len(nodes), two_way, risk, len(unmatched),
                     "yes" if ev else "NO")
        return entry


MLB_SPEC = MLBSpec()
