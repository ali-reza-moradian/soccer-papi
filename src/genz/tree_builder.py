"""JOB 1 — the TREE BUILDER (slow, hourly).

Discovers every World Cup game kicking off within the lookahead window, enumerates EVERY market on
BOTH venues for each game, pairs each Kalshi outcome to its Polymarket twin with the deterministic
rules in match_rules.py (NO LLM), and writes a STATIC match tree the fast price loop consumes:

    data/genz/match_tree.json  — per game: matched nodes (each carries both venues' identifiers +
                                 side) and an `unmatched` list (markets on only one venue)
    data/genz/tree_meta.json   — {built_utc, games:[...], next_kickoffs}

The builder does NETWORK reads via the public no-auth clients in src/kalshi.py and src/polymarket.py
(reused verbatim) — it NEVER touches the OG scanner. Games already kicked off / finished are pruned.

Tree node shape (one outcome, carrying BOTH venues so the engine can take best-of-both per side):
    {market_type, market_key, side, outcome_label, line, kind("2way"|"3way"|"multi"), confidence,
     kalshi_ticker, kalshi_side("YES"/"NO"), poly_token_id, poly_side}
Two nodes that share `market_key` are the two sides of one 2-outcome market.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .. import kalshi as ks
from .. import polymarket as pm
from . import config as gz_config
from . import match_rules as mr
from . import soccer_names

# code (lowercase FIFA) -> canonical team name, inverted from the Polymarket source's curated map.
_CODE_TO_NAME: dict[str, str] = {code: name for name, code in pm._FIFA_CODES_RAW.items()}

# Kalshi uses FIFA-style 3-letter codes; Polymarket uses ISO 3166-1 alpha-3. Where they DIFFER, the
# constructed fifwc-<away>-<home>-<date> slug is wrong and finds NO Poly event, silently dropping the
# game (confirmed live: Portugal is POR on Kalshi / PRT on Poly; Switzerland SUI / CHE). This maps the
# Kalshi (FIFA) code -> the Poly (ISO alpha-3) code for the mismatches; the primary slug uses it and
# the series-scan fallback (resolve_poly_slug) catches anything not listed here.
_KALSHI_TO_POLY_CODE: dict[str, str] = {
    "por": "prt", "sui": "che", "cro": "hrv", "ned": "nld", "ger": "deu", "den": "dnk", "uru": "ury",
    "rsa": "zaf", "par": "pry", "chi": "chl", "uae": "are", "alg": "dza", "bul": "bgr", "gre": "grc",
    "crc": "cri", "hai": "hti", "hon": "hnd", "gui": "gin", "mtn": "mrt", "tog": "tgo", "zam": "zmb",
    "vie": "vnm", "oma": "omn", "kuw": "kwt", "sud": "sdn", "mad": "mdg", "gam": "gmb", "tri": "tto",
    "sin": "sgp", "tan": "tza", "nig": "ner", "nca": "nic", "gua": "gtm", "phi": "phl", "mas": "mys",
    "sri": "lka", "lib": "lbn", "bur": "bfa", "ang": "ago", "mri": "mus", "esa": "slv",
}
_POLY_TO_KALSHI_CODE: dict[str, str] = {v: k for k, v in _KALSHI_TO_POLY_CODE.items()}


def _poly_code(kalshi_code: str) -> str:
    """Kalshi/FIFA 3-letter code -> the Polymarket (ISO alpha-3) code, translated where they differ."""
    k = str(kalshi_code or "").lower()
    return _KALSHI_TO_POLY_CODE.get(k, k)


def _poly_codes_for(kalshi_code: str) -> set:
    """The Poly codes a Kalshi code may appear as: its ISO translation AND the raw code itself."""
    k = str(kalshi_code or "").lower()
    return {k, _poly_code(k)}


def _poly_code_name(code: str) -> str:
    """Normalized team NAME for a Polymarket code (ISO alpha-3, else raw), via the reverse table + the
    FIFA code->name map. '' if the code isn't a known WC nation."""
    c = str(code or "").lower()
    name = _CODE_TO_NAME.get(_POLY_TO_KALSHI_CODE.get(c, c))
    return mr._team(name) if name else ""

# Recognized Polymarket sibling-slug suffixes -> the market KIND we parse them as. The builder
# discovers siblings dynamically by game-slug PREFIX; this only classifies what it finds.
_POLY_SIBLINGS: list[tuple[str, str]] = [
    ("-halftime-result", "1h_result"),
    ("-first-half-result", "1h_result"),
    ("-second-half-result", "2h_result"),
    ("-exact-score", "exact_score"),
    ("-first-to-score", "first_to_score"),
    ("-total-corners", "corners"),
    ("-corners", "corners"),
    ("-to-advance", "advance"),
    ("-advance", "advance"),
    ("-player-props", "player_goals"),
    ("-btts", "btts"),
    ("-more-markets", "more"),
]


@dataclass
class Game:
    game_id: str                 # the Kalshi event suffix, e.g. "26JUN30CIVNOR"
    kalshi_suffix: str
    date: str                    # YYYY-MM-DD
    home: str
    away: str
    kickoff_iso: str
    poly_base_slug: str
    poly_slug_alts: list = field(default_factory=list)
    competition: str = "world_cup"        # POST-WC: which competition discovered this game (legacy = world_cup)
    kalshi_series: list = field(default_factory=list)   # non-WC: the competition's Kalshi series (moneyline)
    poly_prefix: str = ""                 # non-WC: the Poly slug prefix for the token-scan resolver

    @property
    def ctx(self) -> mr.GameCtx:
        return mr.GameCtx(home=self.home, away=self.away)


# --------------------------------------------------------------------------- #
# Game discovery                                                               #
# --------------------------------------------------------------------------- #
def _decode_suffix(suffix: str) -> Optional[tuple[str, str, str]]:
    """('YYYY-MM-DD', away_code, home_code) from a Kalshi event suffix <YYMMMDD><AWAY3><HOME3>."""
    m = re.match(r"^(\d{2}[A-Z]{3}\d{2})([A-Z]{3})([A-Z]{3})$", suffix or "")
    if not m:
        return None
    iso = ks._event_commence_iso(f"X-{suffix}")            # -> 'YYYY-MM-DDT12:00:00Z'
    if not iso:
        return None
    return iso[:10], m.group(2).lower(), m.group(3).lower()


def discover_games(kalshi_client: Any, *, now: datetime, lookahead_hours: float,
                   log: Any = None) -> list[Game]:
    """Every WC game with an open KXWCGAME event, kicking off within [now, now+lookahead], not
    already started. Each game's teams + date come from the event suffix; the Polymarket slug is
    fifwc-<away>-<home>-<date> (both team orderings kept as alternates)."""
    games: dict[str, Game] = {}
    try:
        markets = kalshi_client.iter_markets(series_ticker="KXWCGAME", status="open")
    except Exception as exc:  # noqa: BLE001 - a failed discovery fetch must not crash the build
        if log:
            log.warning("[GENZ] KXWCGAME discovery fetch failed: %s — no games this build.", exc)
        return []
    horizon = now.timestamp() + lookahead_hours * 3600.0
    for m in markets:
        if not isinstance(m, dict):
            continue
        suffix = ks._game_key(str(m.get("event_ticker") or ""))
        if suffix in games:
            continue
        decoded = _decode_suffix(suffix)
        if not decoded:
            continue
        date, away_c, home_c = decoded
        kickoff = _kickoff_iso(m, date)                    # provisional (noon); precise time from Poly later
        ts = _parse_iso(kickoff)
        # Keep games within the lookahead window and not ancient (>24h before the provisional noon).
        # We do NOT prune "already started" here — the precise Poly kickoff isn't known yet, so the
        # ENGINE's started-game gate is the authority on skipping live games (markets_skipped).
        if ts is None or ts > horizon or ts < now.timestamp() - 24 * 3600.0:
            continue
        home = _CODE_TO_NAME.get(home_c, home_c.upper())
        away = _CODE_TO_NAME.get(away_c, away_c.upper())
        # Build the Poly slug from the TRANSLATED (ISO) codes; keep the raw codes + both orderings as
        # alternates. A leftover code mismatch is caught by resolve_poly_slug's series scan.
        pa, ph = _poly_code(away_c), _poly_code(home_c)
        slugs: list[str] = []
        for a, b in ((pa, ph), (ph, pa), (away_c, home_c), (home_c, away_c)):
            s = f"fifwc-{a}-{b}-{date}"
            if s not in slugs:
                slugs.append(s)
        games[suffix] = Game(game_id=suffix, kalshi_suffix=suffix, date=date, home=home, away=away,
                             kickoff_iso=kickoff, poly_base_slug=slugs[0], poly_slug_alts=slugs)
    return list(games.values())


def _kickoff_iso(market: dict[str, Any], date: str) -> str:
    """PROVISIONAL kickoff for discovery only. The precise kickoff (with time-of-day) is enriched from
    Polymarket in build_tree. We DELIBERATELY do not use the Kalshi expiration/settlement fields
    (expected_expiration_time / latest_expiration_time / close_time) — those are AFTER the game, so
    using them as 'kickoff' made a LIVE game look future and the started-game gate never fired. Anchor
    at noon UTC of the match date as the safe fallback."""
    return f"{date}T12:00:00Z"


def _full_iso(v: Any) -> Optional[str]:
    """A UTC ISO instant from a value that carries a TIME-OF-DAY (not a date-only string), else None."""
    s = str(v or "")
    if not re.search(r"\d{1,2}:\d{2}", s):     # no time component -> not a precise kickoff
        return None
    ts = _parse_iso(s)
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _poly_kickoff_iso(event: dict[str, Any]) -> Optional[str]:
    """The PRECISE kickoff (time-of-day) for a game from its Polymarket event: startTime, else a
    market's gameStartTime. None when only date-anchored values exist (caller keeps the noon fallback).
    NEVER startDate (a creation artifact)."""
    iso = _full_iso(event.get("startTime"))
    if iso:
        return iso
    for m in event.get("markets") or []:
        if isinstance(m, dict):
            iso = _full_iso(m.get("gameStartTime"))
            if iso:
                return iso
    return None


def _game_kickoff(game: "Game", series_events: list[dict]) -> str:
    """The game's kickoff: the PRECISE Polymarket startTime if available, else the provisional noon."""
    bases = (game.poly_base_slug, *game.poly_slug_alts)
    for ev in series_events:
        if isinstance(ev, dict) and str(ev.get("slug") or "") in bases:
            precise = _poly_kickoff_iso(ev)
            if precise:
                return precise
    return game.kickoff_iso


def _parse_iso(v: Any) -> Optional[float]:
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Settlement PERIOD — parse each market's resolution text (regulation vs full game)#
# --------------------------------------------------------------------------- #
# Kalshi and Polymarket settle full-match COUNT markets on DIFFERENT periods (Kalshi = full game incl.
# extra time; Poly = 90'+stoppage only). We read each venue's resolution description so a period
# mismatch is never paired (see join_game) or traded (see the engine guard).
_KALSHI_DESC_KEYS = ("rules_primary", "rules_secondary", "settlement_sources", "subtitle", "description")
_POLY_DESC_KEYS = ("description", "resolutionSource", "rules", "resolution")


def _kalshi_desc(m: dict) -> str:
    return " ".join(str(m.get(k) or "") for k in _KALSHI_DESC_KEYS)


def _poly_desc(m: dict) -> str:
    return " ".join(str(m.get(k) or "") for k in _POLY_DESC_KEYS)


def _poly_token_periods(events: list[dict]) -> dict[str, Optional[str]]:
    """clob_token_id -> settlement period (parsed from each market's description). Both tokens of a
    binary market share the market's period."""
    out: dict[str, Optional[str]] = {}
    for ev in events:
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            period = mr.parse_settlement_period(_poly_desc(m))
            for tok in pm._as_list(m.get("clobTokenIds")):
                if tok:
                    out[str(tok)] = period
    return out


def _poly_token_scopes(events: list[dict]) -> dict[str, Optional[str]]:
    """clob_token_id -> settlement SCOPE ('tie' | 'single_game'). Read from the question AND the
    groupItemTitle as well as the description: Polymarket's two-legged market is titled
    'X vs. Y: Team to Advance' with groupItemTitle 'Team to Advance'."""
    out: dict[str, Optional[str]] = {}
    for ev in events:
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            blob = f"{m.get('question')} {m.get('groupItemTitle')} {_poly_desc(m)}"
            scope = mr.parse_tie_scope(blob)
            for tok in pm._as_list(m.get("clobTokenIds")):
                if tok:
                    out[str(tok)] = scope
    return out


_POLY_FEE_DEFAULT_RATE = 0.05         # gamma feeSchedule.rate for sports markets (verified live)


def _poly_market_fee(m: dict) -> dict[str, Any]:
    """Per-market Polymarket taker-fee schedule from the gamma payload: {enabled, rate, taker_only}.
    Older / non-sports markets carry feesEnabled false -> enabled False, rate 0. When enabled but the
    rate is absent, default to 0.05 (the confirmed sports rate)."""
    enabled = bool(m.get("feesEnabled"))
    sched = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
    rate = pm._num(sched.get("rate"))
    if enabled and rate is None:
        rate = _POLY_FEE_DEFAULT_RATE
    taker_only = bool(sched.get("takerOnly") if "takerOnly" in sched else m.get("takerOnly"))
    return {"enabled": enabled, "rate": float(rate or 0.0) if enabled else 0.0, "taker_only": taker_only}


def _poly_token_fees(events: list[dict]) -> dict[str, dict[str, Any]]:
    """clob_token_id -> fee schedule {enabled, rate, taker_only}. Both tokens of a binary market share
    the market's fee schedule."""
    out: dict[str, dict[str, Any]] = {}
    for ev in events:
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            fee = _poly_market_fee(m)
            for tok in pm._as_list(m.get("clobTokenIds")):
                if tok:
                    out[str(tok)] = fee
    return out


# --------------------------------------------------------------------------- #
# Kalshi side: enumerate all per-type series for the game                        #
# --------------------------------------------------------------------------- #
def kalshi_options(kalshi_client: Any, game: Game, series_list: list[str],
                   log: Any = None) -> tuple[dict[str, dict], dict[str, Any]]:
    """Pull every configured KXWC* series for this game's suffix and convert each market to canonical
    outcomes. EACH series fetch is independent: a timeout / connection / non-200 on one series is
    logged and SKIPPED (its market type is simply absent this build) — it never aborts the game or the
    build. Returns ({twin_key: option}, coverage) where coverage = {ok, failed:[series, …]}."""
    options: dict[str, dict] = {}
    ok = 0
    failed: list[str] = []
    for series in series_list:
        et = f"{series}-{game.kalshi_suffix}"
        try:
            page = kalshi_client.markets(event_ticker=et, status="open")
        except Exception as exc:  # noqa: BLE001 - one slow/failed series must not abort the build
            failed.append(series)
            if log:
                log.warning("[GENZ] %s %s fetch failed: %s — skipping this market type this build.",
                            game.game_id, series, exc)
            continue
        ok += 1
        for m in (page or {}).get("markets") or []:
            if not isinstance(m, dict):
                continue
            period = mr.parse_settlement_period(_kalshi_desc(m))   # regulation vs full game (incl. ET)
            scope = mr.parse_tie_scope(f"{m.get('title')} {m.get('yes_sub_title')} {_kalshi_desc(m)}")
            for outcome, side in mr.kalshi_outcomes(m, game.ctx):
                options.setdefault(outcome.twin_key(), {
                    "market_type": outcome.market_type, "market_key": outcome.group,
                    "side": outcome.side, "line": outcome.line, "kind": outcome.kind,
                    "confidence": outcome.confidence, "outcome_label": outcome.label or outcome.side,
                    "kalshi_ticker": m.get("ticker"), "kalshi_side": side, "settle_period": period,
                    "settle_scope": scope,
                })
    return options, {"ok": ok, "failed": failed}


# --------------------------------------------------------------------------- #
# Polymarket side: enumerate the WHOLE soccer-fifwc series once, then filter      #
# each game's events by slug prefix (the proven sibling-discovery path).          #
# --------------------------------------------------------------------------- #
def fetch_series_events(poly_client: Any, series_slug: str, log: Any = None) -> list[dict]:
    """Pull EVERY event in the Polymarket WC series (paginated, closed=false). Each game is one 1x2
    event PLUS many siblings (-more-markets, -total-corners, -exact-score, -halftime-result,
    -player-props, …); /public-search does NOT surface those by slug, so we enumerate the series and
    filter by slug prefix per game (game_sibling_events). Fetched ONCE per build, reused for all games."""
    try:
        return poly_client.events_by_series(series_slug, closed=False)
    except Exception as exc:  # noqa: BLE001 - timeout/connection/non-200 must not crash the build
        if log:
            log.warning("[GENZ] poly series %s fetch failed: %s — no Poly markets this build.",
                        series_slug, exc)
        return []


def game_sibling_events(series_events: list[dict], game: Game) -> list[dict]:
    """Every series event whose slug STARTS WITH this game's slug (either team ordering): the 1x2 +
    all siblings. De-duplicated by slug."""
    prefixes = tuple(dict.fromkeys((game.poly_base_slug, *game.poly_slug_alts)))
    out: dict[str, dict] = {}
    for ev in series_events:
        if not isinstance(ev, dict) or ev.get("closed"):
            continue
        slug = str(ev.get("slug") or "")
        if slug and any(slug.startswith(p) for p in prefixes):
            out.setdefault(slug, ev)
    return list(out.values())


_GAME_SLUG_RE = re.compile(r"^(fifwc-([a-z]{3})-([a-z]{3})-(\d{4}-\d{2}-\d{2}))")


def _parse_game_slug(slug: str) -> Optional[tuple[str, str, str, str]]:
    """(game_key, code_a, code_b, date) from a fifwc event slug (base OR sibling), or None. game_key is
    the 'fifwc-<a>-<b>-<date>' prefix shared by a game's 1x2 event and all its siblings."""
    m = _GAME_SLUG_RE.match(str(slug or "").lower())
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def _event_team_names(events: list[dict]) -> Optional[set]:
    """The normalized team-name pair from a group's base 1x2 event (parse_event_legs), or None. This
    is TABLE-INDEPENDENT — it reads Polymarket's own team names, so it resolves even a code mismatch
    we haven't listed."""
    for ev in events:
        parsed = pm.parse_event_legs(ev)                   # only the base 1x2 event parses to legs
        if parsed:
            names = {mr._team(l.team) for l in parsed.legs if l.role == "team" and l.team}
            if len(names) == 2:
                return names
    return None


def resolve_poly_slug(game: Game, series_events: list[dict], log: Any = None) -> bool:
    """Guarantee the game's Poly slug resolves to real events. If the primary slug already matches
    (via the translation table), keep it. OTHERWISE fall back to SCANNING the soccer-fifwc series for
    an event whose DATE and both TEAMS match this game — matched by the code table OR by Polymarket's
    OWN team names — so a 3-letter-code mismatch can NEVER silently drop a game. Updates
    game.poly_base_slug/alts and logs a WARNING when the fallback fires. Returns True if resolved."""
    if game_sibling_events(series_events, game):
        return True                                        # primary slug already works
    decoded = _decode_suffix(game.kalshi_suffix)
    if not decoded:
        return False
    _, away_c, home_c = decoded
    acc_away, acc_home = _poly_codes_for(away_c), _poly_codes_for(home_c)
    game_teams = {mr._team(game.home), mr._team(game.away)}

    groups: dict[str, list[dict]] = {}                     # date-matching series events grouped by game_key
    for ev in series_events:
        if not isinstance(ev, dict) or ev.get("closed"):
            continue
        parsed = _parse_game_slug(ev.get("slug") or "")
        if parsed and parsed[3] == game.date:
            groups.setdefault(parsed[0], []).append(ev)

    for game_key, evs in groups.items():
        _, ca, cb, _d = _parse_game_slug(game_key)
        code_ok = (ca in acc_away and cb in acc_home) or (ca in acc_home and cb in acc_away)
        table_names = {_poly_code_name(ca), _poly_code_name(cb)}
        name_ok = "" not in table_names and table_names == game_teams
        event_names = _event_team_names(evs)               # table-independent (Poly's own names)
        own_ok = event_names is not None and event_names == game_teams
        if code_ok or name_ok or own_ok:
            how = "code table" if code_ok else ("code->name" if name_ok else "poly event names")
            game.poly_base_slug = game_key
            game.poly_slug_alts = [game_key]
            if log:
                log.warning("[GENZ] %s %s vs %s: primary Poly slug missed (code mismatch) — resolved "
                            "via %s to %s.", game.game_id, game.away, game.home, how, game_key)
            return True

    if log:
        log.warning("[GENZ] %s %s vs %s: NO Poly event matched (tried slug %s) — one-sided this build.",
                    game.game_id, game.away, game.home, game.poly_base_slug)
    return False


def _poly_kind(slug: str, game: Game) -> str:
    """Classify a Polymarket event by its slug suffix relative to the game base slug(s)."""
    bases = (game.poly_base_slug, *game.poly_slug_alts)
    if slug in bases:
        return "moneyline"
    suffix = slug
    for base in bases:
        if slug.startswith(base):
            suffix = slug[len(base):]
            break
    for frag, kind in _POLY_SIBLINGS:
        if frag in suffix:
            return kind
    return "unknown"


def poly_options(series_events: list[dict], game: Game, log: Any = None) -> tuple[dict[str, dict], dict[str, Any]]:
    """Convert every Polymarket event/market for the game (its slug-prefix siblings) into canonical
    outcomes keyed by twin_key, each carrying its CLOB token + a human poly_side. EACH sibling event
    is parsed independently: a malformed event is logged and SKIPPED, never aborting the game. Returns
    ({twin_key: option}, coverage) where coverage = {ok, failed:[slug, …]}."""
    options: dict[str, dict] = {}
    ok = 0
    failed: list[str] = []
    sibs = game_sibling_events(series_events, game)
    token_periods = _poly_token_periods(sibs)              # clob token -> settlement period (reg / full game)
    token_scopes = _poly_token_scopes(sibs)                # clob token -> tie vs single-game scope
    token_fees = _poly_token_fees(sibs)                    # clob token -> taker-fee schedule
    for ev in sibs:
        slug = str(ev.get("slug") or "")
        try:
            outs = _poly_event_outcomes(ev, _poly_kind(slug, game), game)
        except Exception as exc:  # noqa: BLE001 - a malformed sibling must not abort the game's build
            failed.append(slug)
            if log:
                log.warning("[GENZ] %s poly %s parse failed: %s — skipping this sibling this build.",
                            game.game_id, slug, exc)
            continue
        ok += 1
        for outcome, token, poly_side in outs:
            if not token:
                continue
            fee = token_fees.get(str(token)) or {"enabled": False, "rate": 0.0, "taker_only": False}
            options.setdefault(outcome.twin_key(), {
                "market_type": outcome.market_type, "market_key": outcome.group,
                "side": outcome.side, "line": outcome.line, "kind": outcome.kind,
                "confidence": outcome.confidence, "outcome_label": outcome.label or outcome.side,
                "poly_token_id": str(token), "poly_side": poly_side,
                "settle_period": token_periods.get(str(token)),
                "settle_scope": token_scopes.get(str(token)),
                "poly_fee_enabled": fee["enabled"], "poly_fee_rate": fee["rate"],
                "poly_fee_taker_only": fee["taker_only"],
            })
    return options, {"ok": ok, "failed": failed}


def _git(m: dict) -> str:
    return str(m.get("groupItemTitle") or m.get("question") or m.get("title") or "")


def _num(text: str) -> Optional[float]:
    """The O/U LINE from a Polymarket title — the LAST number, so a leading '1st'/'2nd Half'
    qualifier ('1st Half O/U 1.5') doesn't shadow the real line (1.5)."""
    nums = re.findall(r"\d+(?:\.\d+)?", str(text or ""))
    return float(nums[-1]) if nums else None


def _period_text(m: dict) -> str:
    """ALL of a Poly market's text (groupItemTitle + question + title), lowercased — so the half-period
    qualifier is detected wherever Polymarket states it (sometimes only in the question), never missed
    just because groupItemTitle is generic. A missed period would mis-pair a 1st-half O/U to a full-game
    total."""
    return f"{m.get('groupItemTitle') or ''} {m.get('question') or ''} {m.get('title') or ''}".lower()


def _half_of(low: str) -> str:
    if "1st half" in low or "first half" in low:
        return "1h"
    if "2nd half" in low or "second half" in low:
        return "2h"
    return ""


# Generic (non-team) words that can precede an O/U qualifier in a GAME-WIDE total title.
_GENERIC_TOTAL_TOKENS = frozenset({"total", "totals", "goals", "goal", "match", "game", "fulltime",
                                   "full", "time", "regular", "scored", "the", "in", "of", "by"})


def _poly_total_team(title: str, ctx: mr.GameCtx) -> Optional[str]:
    """The TEAM named BEFORE the period / O-U qualifier in a Poly total title ('Senegal 1st Half O/U
    0.5' -> 'Senegal'; 'United States 1st Half' -> 'USA'), or None for a GAME-WIDE total ('1st Half
    O/U 0.5' / 'O/U 2.5'). A title is game-wide ONLY when nothing team-like precedes the qualifier —
    so Kalshi's game-wide KXWC1H/2HTOTAL can never pair with a Poly single-team half-total.

    Robust: the prefix is normalized via normalize_team (folds 'United States'->'usa' etc.); an
    UNRECOGNIZED team prefix still returns non-None, so it is always excluded from the game-wide node."""
    low = str(title or "").lower()
    cut = len(title)
    for q in ("1st half", "first half", "2nd half", "second half", "o/u", "over/under"):
        i = low.find(q)
        if i != -1:
            cut = min(cut, i)
    norm = mr._team(title[:cut])                       # normalize the prefix (equivalence-aware)
    tokens = [t for t in norm.split() if t]
    if not tokens or all(t in _GENERIC_TOTAL_TOKENS for t in tokens):
        return None                                    # empty / all-generic -> GAME-WIDE
    for team in (ctx.home, ctx.away):                  # prefer the matching canonical game team
        tn = mr._team(team)
        if tn and (norm == tn or tn in norm or norm in tn):
            return team
    return title[:cut].strip(" -:") or None            # unrecognized team -> still NOT game-wide


def _poly_event_outcomes(ev: dict, kind: str, game: Game) -> list[tuple[mr.Outcome, str, str]]:
    """(Outcome, clob_token, poly_side_label) list for one Polymarket event of a given KIND. The
    big-fan-out siblings (-more-markets, -total-corners) classify EACH market by its title into the
    same market_type vocabulary the Kalshi side uses, so twin_keys collide."""
    ctx = game.ctx
    markets = [m for m in (ev.get("markets") or []) if isinstance(m, dict)]
    out: list[tuple[mr.Outcome, str, str]] = []

    if kind == "moneyline":
        parsed = pm.parse_event_legs(ev)
        if parsed:
            for leg in parsed.legs:
                if leg.role == "draw":
                    out.append((mr.mk_moneyline("draw", "Draw"), leg.yes_token or "", "Draw"))
                elif leg.team:
                    side = ctx.side_for_team(leg.team)
                    if side:
                        out.append((mr.mk_moneyline(side, leg.team), leg.yes_token or "", leg.team))
        return out

    if kind == "more":                  # totals + team totals + half totals + BTTS(+halves) + spreads + advance
        for sp in pm.parse_spread_markets(ev):
            out.extend(_poly_spread_outcomes(sp, game))
        for m in markets:
            git = _git(m)
            low = git.lower()
            ptext = _period_text(m)                     # detect 1st/2nd-half from ALL the market's text
            over, under = pm._outcome_token(m, "over"), pm._outcome_token(m, "under")
            yes, no = pm._outcome_token(m, "yes"), pm._outcome_token(m, "no")
            if over and under:                         # an Over/Under market
                num = _num(git)
                if num is None:
                    continue
                if "corner" in low:
                    ct = mr._team_in(git, ctx)
                    out.append((mr.mk_corners(num, "over", team=ct), over, f"{git} Over"))
                    out.append((mr.mk_corners(num, "under", team=ct), under, f"{git} Under"))
                    continue
                half = _half_of(ptext)
                team = _poly_total_team(git, ctx)      # team named before the period/O-U qualifier, or None
                if half and team:
                    # TEAM half-total ('<Team> 1st/2nd Half O/U') — a DIFFERENT market than the
                    # GAME-WIDE half-total. Kalshi has no team-half-total series, so it stays one-sided
                    # (unmatched) and NEVER pollutes the game-wide 1h_total/2h_total node.
                    out.append((mr.mk_team_total(team, num, "over", half), over, f"{git} Over"))
                    out.append((mr.mk_team_total(team, num, "under", half), under, f"{git} Under"))
                elif half:
                    # GAME-WIDE half-total (NO team) — the only Poly market Kalshi's KXWC1H/2HTOTAL pairs with.
                    out.append((mr.mk_total(num, "over", half), over, f"{git} Over"))
                    out.append((mr.mk_total(num, "under", half), under, f"{git} Under"))
                elif team:
                    out.append((mr.mk_team_total(team, num, "over"), over, f"{git} Over"))   # full-game team total
                    out.append((mr.mk_team_total(team, num, "under"), under, f"{git} Under"))
                else:
                    out.append((mr.mk_total(num, "over"), over, "Over"))                     # full-game total
                    out.append((mr.mk_total(num, "under"), under, "Under"))
            elif yes and no:                           # a Yes/No market
                if "both teams to score" in low or "btts" in low:
                    half = _half_of(ptext)
                    out.append((mr.mk_btts(half, "yes"), yes, f"{git} Yes"))
                    out.append((mr.mk_btts(half, "no"), no, f"{git} No"))
                elif "advance" in low:
                    team = mr._team_in(git, ctx)
                    if team:
                        opp = game.away if mr._team(team) == mr._team(game.home) else game.home
                        out.append((mr.mk_advance(ctx.pair, mr._team(team), label=team), yes, f"{team} advance"))
                        out.append((mr.mk_advance(ctx.pair, mr._team(opp), label=opp), no, f"{opp} advance"))
        return out

    if kind == "corners":
        for m in markets:
            git = _git(m)
            num = _num(git)
            over, under = pm._outcome_token(m, "over"), pm._outcome_token(m, "under")
            if num is None or not (over and under) or _half_of(_period_text(m)):
                continue                                # full-game corners only (half corners -> unmatched)
            team = mr._team_in(git, ctx)
            out.append((mr.mk_corners(num, "over", team=team), over, f"{git} Over"))
            out.append((mr.mk_corners(num, "under", team=team), under, f"{git} Under"))
        return out

    if kind in ("1h_result", "2h_result"):
        half = "1h" if kind == "1h_result" else "2h"
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            label = str(m.get("groupItemTitle") or m.get("question") or "")
            tok = pm.yes_token(m)
            if not tok:
                continue
            side = "draw" if ("draw" in label.lower() or "tie" in label.lower()) else ctx.side_for_team(label)
            if side:
                out.append((mr.mk_half_result(half, side, label), tok, label))
        return out

    if kind == "exact_score":
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            label = str(m.get("groupItemTitle") or m.get("question") or "")
            score = mr.poly_exact_score(label, ctx)              # 'Côte d'Ivoire A - B Norway' -> away-home key
            tok = pm.yes_token(m)
            if score and tok:
                out.append((mr.mk_exact_score(score), tok, label))
        return out

    if kind == "first_to_score":
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            label = str(m.get("groupItemTitle") or m.get("question") or "")
            tok = pm.yes_token(m)
            if not tok:
                continue
            low = label.lower()
            side = "none" if ("neither" in low or "no goal" in low) else ctx.side_for_team(label)
            if side:
                out.append((mr.mk_first_to_score(side, label), tok, label))
        return out

    if kind == "advance":
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            label = str(m.get("groupItemTitle") or m.get("question") or "")
            tok = pm.yes_token(m)
            team = ctx.side_for_team(label)
            if tok and team:
                tnorm = mr._team(game.home if team == "home" else game.away)
                out.append((mr.mk_advance(ctx.pair, tnorm, label=label), tok, label))
        return out

    if kind == "player_goals":
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            parsed = mr.poly_player_goals(m.get("groupItemTitle") or m.get("question") or "")
            tok = pm.yes_token(m)
            if parsed and tok:
                out.append((mr.mk_player_goals(*parsed), tok, m.get("groupItemTitle") or ""))
        return out

    return out


def _poly_spread_outcomes(sp: dict, game: Game) -> list[tuple[mr.Outcome, str, str]]:
    """The two COMPLEMENTARY sides of one handicap line from a parse_spread_markets entry, keyed by
    the FAVORITE (negative line): 'cover' = the favored team -margin (its own token), 'plus' = the
    underdog at +margin (the other token) — the genuine opposite side of the SAME line. Both share the
    favored-team group, so this never produces a cross-team (two-favorites) pairing."""
    line = float(sp["line"])
    margin = abs(line)
    if line < 0:                                           # named is the favorite (covers -margin)
        fav, fav_tok, dog, dog_tok = sp["named"], sp["named_token"], sp["opp"], sp["opp_token"]
    else:                                                  # named has +line -> the OTHER team is favored
        fav, fav_tok, dog, dog_tok = sp["opp"], sp["opp_token"], sp["named"], sp["named_token"]
    return [
        (mr.mk_spread(fav, margin, "cover", label=f"{fav} -{margin}"), fav_tok, f"{fav} -{margin}"),
        (mr.mk_spread(fav, margin, "plus", label=f"{dog} +{margin}"), dog_tok, f"{dog} +{margin}"),
    ]


# --------------------------------------------------------------------------- #
# Join + persist                                                               #
# --------------------------------------------------------------------------- #
def join_game(k_opts: dict[str, dict], p_opts: dict[str, dict],
              log: Any = None, game_id: str = "") -> tuple[list[dict], list[dict]]:
    """Join Kalshi and Polymarket options by twin_key. A key in BOTH -> a matched node carrying both
    identifiers; a key in only one -> an unmatched entry (for visibility).

    SETTLEMENT-PERIOD SAFETY: for full-match COUNT markets (corners/goals/team totals) the two venues
    can settle on DIFFERENT periods (Kalshi full game incl. extra time; Poly 90'+stoppage only). If
    their parsed periods are BOTH KNOWN and DISAGREE, the two legs are NOT the same bet (in a knockout
    they can both lose) — so they are NOT paired: both go to `unmatched` with reason
    'settlement_period_mismatch' and a WARNING is logged. Every count node carries kalshi_period /
    poly_period so the engine can re-check (belt-and-suspenders)."""
    nodes: list[dict] = []
    period_mismatch: list[str] = []                        # twin_keys dropped for a period disagreement
    two_leg_mismatch: list[str] = []                       # twin_keys dropped for a tie-vs-leg disagreement
    for key in sorted(set(k_opts) | set(p_opts)):
        k, p = k_opts.get(key), p_opts.get(key)
        if not (k and p):
            continue
        # TWO-LEG TIE GUARD (all market types). One side settling on the AGGREGATE of a two-legged tie
        # while the other settles on a single leg is not the same bet — a team can lose the leg and
        # still advance. Refuse whenever the two scopes are known and DISAGREE, and equally when only
        # ONE side declares tie scope (an undeclared counterpart is not evidence that it is a tie).
        ks_, ps_ = k.get("settle_scope"), p.get("settle_scope")
        if (ks_ == "tie") != (ps_ == "tie"):
            two_leg_mismatch.append(key)
            if log:
                log.warning("[GENZ] %s %s: two_leg_mismatch — kalshi=%s vs poly=%s; NOT paired (a "
                            "two-legged tie/aggregate market is a different bet from one leg).",
                            game_id, key, ks_, ps_)
            continue
        kp, pp = k.get("settle_period"), p.get("settle_period")
        if k["market_type"] in mr.COUNT_MARKETS and kp and pp and kp != pp:
            period_mismatch.append(key)                    # known disagreement on extra-time inclusion
            if log:
                log.warning("[GENZ] %s %s: settlement_period_mismatch — kalshi=%s vs poly=%s; NOT "
                            "paired (both legs can lose in extra time).", game_id, key, kp, pp)
            continue
        nodes.append({
            "twin_key": key,
            "market_type": k["market_type"], "market_key": k["market_key"], "side": k["side"],
            "outcome_label": k.get("outcome_label") or p.get("outcome_label"),
            "line": k["line"], "kind": k["kind"], "confidence": k["confidence"],
            "kalshi_ticker": k["kalshi_ticker"], "kalshi_side": k["kalshi_side"],
            "poly_token_id": p["poly_token_id"], "poly_side": p["poly_side"],
            "kalshi_period": kp, "poly_period": pp,
            "poly_fee_enabled": p.get("poly_fee_enabled", False),
            "poly_fee_rate": p.get("poly_fee_rate", 0.0),
            "poly_fee_taker_only": p.get("poly_fee_taker_only", False),
        })
    unmatched: list[dict] = []
    mismatch_set = set(period_mismatch)
    two_leg_set = set(two_leg_mismatch)

    def _reason(key: str) -> str:
        if key in two_leg_set:
            return "two_leg_mismatch"
        return "settlement_period_mismatch" if key in mismatch_set else "one_venue_only"

    for key in sorted((set(k_opts) - set(p_opts)) | mismatch_set | two_leg_set):
        o = k_opts.get(key)
        if o:
            unmatched.append({"venue": "kalshi", "market_type": o["market_type"],
                              "outcome_label": o.get("outcome_label"), "identifier": o.get("kalshi_ticker"),
                              "reason": _reason(key)})
    for key in sorted((set(p_opts) - set(k_opts)) | mismatch_set | two_leg_set):
        o = p_opts.get(key)
        if o:
            unmatched.append({"venue": "polymarket", "market_type": o["market_type"],
                              "outcome_label": o.get("outcome_label"), "identifier": o.get("poly_token_id"),
                              "reason": _reason(key)})
    return nodes, unmatched


# --------------------------------------------------------------------------- #
# SOCCER POST-WC — competition-driven discovery (the WC is over). ADDITIVE: an empty competitions list #
# (the default) keeps the legacy World Cup path byte-identical; the world_cup entry reproduces it.       #
# --------------------------------------------------------------------------- #
SOCCER_SERIES_MAP_PATH = os.path.join(gz_config.GENZ_DIR, "soccer_series_map.json")


def load_series_map() -> dict:
    """The learned {competition: [kalshi_series,...]} map (AUTO discovery persists here). {} if absent."""
    try:
        with open(SOCCER_SERIES_MAP_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_series_map(m: dict) -> None:
    try:
        gz_config.ensure_dirs()
        tmp = SOCCER_SERIES_MAP_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(m, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SOCCER_SERIES_MAP_PATH)
    except OSError:
        pass


# Per-competition TITLE keywords that uniquely name the game-winner series in the live Kalshi catalog
# (verified 2026-07: 'Major League Soccer Game'->KXMLSGAME, 'UEFA Champions League Game'->KXUCLGAME,
# 'Club Friendlies'->KXCLUBFGAME). Kept tight so 'champions league' can't grab AFC/Women's variants.
#
# EVERY entry below was proved against the LIVE catalog on 2026-07-28 (1,466 soccer-tagged series,
# 95 of them *GAME): each keyword set resolves to EXACTLY ONE ticker — see
# tests/test_soccer_leagues.py, which re-runs that proof against the committed catalog fixture so a
# Kalshi title change (or a new series that would make a keyword ambiguous) fails the suite instead
# of silently re-pointing a competition. The full-game TOTAL series is NOT keyword-matched — Kalshi
# does not repeat the competition name in totals titles — it is derived from the GAME ticker stem and
# confirmed against the catalog by _total_series_for.
_COMP_SERIES_KEYWORDS: dict[str, tuple] = {
    "mls": ("major league soccer", "mls"),
    "ucl_qualifying": ("uefa champions league",),
    "club_friendlies": ("club friendlies", "club friendly"),
    # --- added 2026-07-28: every remaining league live on BOTH venues ---
    "uefa_europa_league": ("uefa europa league",),
    "uefa_conference_league": ("uefa conference league",),
    "argentina_primera": ("argentina primera division",),
    "brasileirao": ("brasileiro serie a",),
    "brasileirao_b": ("brasileiro serie b",),
    "conmebol_sudamericana": ("conmebol sudamericana",),
    "liga_mx": ("liga mx",),
    "nwsl": ("nwsl",),
    "ekstraklasa": ("ekstraklasa",),
    "eliteserien": ("eliteserien",),
    "usl_championship": ("usl championship",),
    "chinese_super_league": ("chinese super league",),
    "liga_dimayor": ("liga dimayor",),
    "croatia_hnl": ("croatia hnl",),
    "peru_liga_1": ("peru liga 1",),
    "scottish_premiership": ("scottish premiership",),
    "uruguay_primera": ("uruguay primera division",),
    "czech_first_league": ("czech first league",),
    "bolivia_primera": ("bolivia premier division",),
}


def _is_soccer_series(s: dict) -> bool:
    tags = [str(t).lower() for t in (s.get("tags") or [])]
    return "soccer" in tags or "football" in tags


def _scan_soccer_series(kalshi_client: Any, comp: Any, log: Any = None, *, ends: str = "GAME") -> list[str]:
    """Scan the Kalshi series CATALOG (list_series, category='Sports') for a competition's series ending
    ``ends`` ('GAME' = the per-match 3-way winner; 'TOTAL' = the 2-way goal totals), soccer-tagged and
    matched by the competition's TITLE keywords. REPORTS candidates. [] (honest zero) when the client
    can't list the catalog or nothing matches — never a hardcoded guess."""
    lister = getattr(kalshi_client, "list_series", None)
    if not callable(lister):
        if log and ends == "GAME":
            log.info("[GENZ][SOCCER] AUTO %s: Kalshi client has no series catalog listing — reporting "
                     "zero.", comp.name)
        return []
    try:
        rows = lister(category="Sports")
    except Exception as exc:  # noqa: BLE001
        if log:
            log.warning("[GENZ][SOCCER] AUTO %s catalog scan failed: %s — reporting zero.", comp.name, exc)
        return []
    kws = _COMP_SERIES_KEYWORDS.get(comp.name) or (str(comp.name).replace("_", " "),)
    hits: list[str] = []
    for s in rows or []:
        if not isinstance(s, dict) or not _is_soccer_series(s):
            continue
        ticker = str(s.get("ticker") or "")
        if not ticker.endswith(ends):
            continue
        blob = f"{ticker} {s.get('title') or ''}".lower()
        if any(k in blob for k in kws):
            hits.append(ticker)
    if log and ends == "GAME":
        log.info("[GENZ][SOCCER] AUTO %s: catalog candidates %s.", comp.name, hits or "none")
    return sorted(dict.fromkeys(hits))


def _total_series_for(kalshi_client: Any, game_series: list[str], scanned: list[str],
                      log: Any = None) -> list[str]:
    """The competition's 2-way TOTAL series, derived from its GAME series ticker STEM and confirmed
    against the live catalog.

    The title-keyword scan alone MISSES these, because Kalshi does not repeat the competition name in
    a totals series title: KXUCLGAME is 'UEFA Champions League Game' (matches the 'uefa champions
    league' keyword) but KXUCLTOTAL is 'Champions League Total Goals' (no 'uefa'), and KXCLUBFTOTAL is
    just 'Point Total'. Both were therefore invisible, so soccer produced ONLY 3-way moneyline nodes —
    and the engine/maker quote 2-way markets, which is why soccer vanished from the quotable universe.

    Deriving GAME -> TOTAL on the ticker stem is exact, not a guess, and the result is only used when
    the catalog actually lists it."""
    derived = [s[:-4] + "TOTAL" for s in game_series if s.endswith("GAME")]
    if not derived:
        return list(scanned)
    known: set = set()
    try:
        lister = getattr(kalshi_client, "list_series", None)
        for s in (lister(category="Sports") if callable(lister) else []) or []:
            if isinstance(s, dict) and _is_soccer_series(s):
                known.add(str(s.get("ticker") or ""))
    except Exception:  # noqa: BLE001 — catalog unavailable -> fall back to the keyword scan alone
        return list(scanned)
    confirmed = [t for t in derived if t in known and t not in scanned]
    if log and confirmed:
        log.info("[GENZ][SOCCER] TOTAL series derived from the GAME stem and confirmed in the catalog: "
                 "%s (title-keyword scan found %s).", confirmed, scanned or "none")
    return sorted(dict.fromkeys(list(scanned) + confirmed))


# Kalshi decorates a per-player/team yes_sub_title with a period prefix on some competitions
# (UCL: 'Reg Time: Levski Sofia' / 'Reg Time: Tie'). Strip it before the name is read.
_REG_PREFIX_RE = re.compile(r"^\s*(reg(?:ulation)?\s*time|full\s*time|ft)\s*[:\-]\s*", re.IGNORECASE)
_DRAW_WORDS = frozenset({"tie", "draw"})


def _clean_sub(sub: str) -> str:
    return _REG_PREFIX_RE.sub("", str(sub or "")).strip()


def resolve_kalshi_series(comp: Any, kalshi_client: Any, series_map: dict, log: Any = None) -> list[str]:
    """A competition's Kalshi game-winner series. Explicit list -> as-is. 'AUTO' -> the learned mapping,
    else a catalog scan (reported + persisted to soccer_series_map.json). Never a hardcoded guess."""
    ks_cfg = comp.kalshi_series
    if isinstance(ks_cfg, (list, tuple)):
        return [str(s) for s in ks_cfg]
    if str(ks_cfg).upper() != "AUTO":
        return [str(ks_cfg)]
    if series_map.get(comp.name):
        return list(series_map[comp.name])
    cands = _scan_soccer_series(kalshi_client, comp, log)
    if cands:
        series_map[comp.name] = cands
    return list(cands)


_SOCCER_VS_RE = re.compile(r"\bvs\.?\b", re.IGNORECASE)
# Kalshi's NON-WC game-winner titles are '<A> vs <B> Winner?' — the trailing question is part of the
# TITLE, not of team B's name. Left in place it rides along on the second name and poisons the match:
# live 2026-07-28 'Montevideo City vs Wanderers Winner?' yielded team B = 'Wanderers Winner?', and
# because 'wanderers' is a club-GENERIC token it could not be matched back to the clean
# yes_sub_title 'Wanderers', so the raw fragment won — and Uruguay's only fixture went unpaired
# against a Polymarket event ('uru1-tor-wan-2026-07-31') that was right there.
_SOCCER_TITLE_TAIL_RE = re.compile(r"\s*\bwinner\s*\??\s*$", re.IGNORECASE)


def _soccer_names_from_title(title: str) -> Optional[tuple[str, str]]:
    """Generic '(A, B)' from a soccer market title ('Will A win the A vs B match?', the non-WC
    '<A> vs <B> Winner?', or a bare 'A vs B')."""
    t = _SOCCER_TITLE_TAIL_RE.sub("", str(title or ""))
    m = re.search(r"win the (.+?)\s+match\b", t, re.IGNORECASE)
    core = m.group(1) if m else t
    parts = _SOCCER_VS_RE.split(core, maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(" .:-"), parts[1].strip(" .:-")
    return (a, b) if a and b else None


def _competition_slug(prefix: str, away: str, home: str, date: str) -> str:
    def toks(s: str) -> str:
        return "-".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))
    return f"{prefix}-{toks(away)}-{toks(home)}-{date}" if prefix else ""


def _competition_teams(mkts: list[dict]) -> Optional[tuple[str, str]]:
    """The two team names for a non-WC event. PRIMARY: the per-team markets' yes_sub_title (strip a
    'Reg Time:' prefix, exclude the Tie/Draw market) — verified live for MLS ('San Jose'/'Tie') and UCL
    ('Reg Time: Levski Sofia'/'Reg Time: Tie'). Order fixed by the title ('A vs B Winner?') when it
    parses, else market order. Returns (away, home) or None."""
    teams: list[str] = []
    for m in mkts:
        sub = _clean_sub(m.get("yes_sub_title"))
        if sub and sub.lower() not in _DRAW_WORDS and sub not in teams:
            teams.append(sub)
    if len(teams) < 2:
        return None
    title_names = next((n for n in (_soccer_names_from_title(m.get("title")) for m in mkts) if n), None)
    if title_names:                                        # order the yes_sub teams by the title's order
        ordered = [next((t for t in teams if _soccer_team_match(t, tn)), tn) for tn in title_names]
        if ordered[0] and ordered[1] and ordered[0] != ordered[1]:
            return (ordered[0], ordered[1])
    return (teams[0], teams[1])


def _discover_competition_games(kalshi_client: Any, comp: Any, series_list: list[str], *,
                                now: datetime, lookahead_hours: float, log: Any = None,
                                pair_series: Optional[list[str]] = None) -> list["Game"]:
    """Generic (non-WC) discovery: enumerate a competition's GAME-WINNER series (``series_list``), group
    by event, read the two teams from yes_sub_title (title fixes order). ``pair_series`` (game + total)
    is stored on each Game for the pairing step. A series with no open markets -> honest zero; an event
    whose teams won't resolve is REPORTED (for the next raw dump), never guessed."""
    pair_series = pair_series if pair_series is not None else series_list
    by_event: dict[str, list[dict]] = {}
    for series in series_list:
        try:
            for m in kalshi_client.iter_markets(series_ticker=series, status="open"):
                if isinstance(m, dict):
                    by_event.setdefault(str(m.get("event_ticker") or ""), []).append(m)
        except Exception as exc:  # noqa: BLE001
            if log:
                log.warning("[GENZ][SOCCER] %s series %s fetch failed: %s.", comp.name, series, exc)
    games: list[Game] = []
    horizon = now.timestamp() + lookahead_hours * 3600.0
    for et, mkts in by_event.items():
        teams = _competition_teams(mkts)
        iso = ks._event_commence_iso(et)
        date = iso[:10] if iso else None
        if not teams or not date:
            if log:
                log.info("[GENZ][SOCCER] %s: unresolved event %s (title=%r) — reported for evidence, "
                         "not paired.", comp.name, et, (mkts[0].get("title") if mkts else ""))
            continue
        ts = _parse_iso(f"{date}T12:00:00Z")
        if ts is None or ts > horizon or ts < now.timestamp() - 24 * 3600.0:
            continue
        away, home = teams
        suffix = ks._game_key(et) or et
        games.append(Game(game_id=suffix, kalshi_suffix=suffix, date=date, home=home, away=away,
                          kickoff_iso=f"{date}T12:00:00Z",
                          poly_base_slug=_competition_slug(comp.poly_slug_prefix, away, home, date),
                          poly_slug_alts=[], competition=comp.name,
                          kalshi_series=list(pair_series), poly_prefix=comp.poly_slug_prefix))
    return games


# --------------------------------------------------------------------------- #
# Non-WC team-name token matching (venue truncations: 'Los Angeles G' <-> 'Los Angeles Galaxy';        #
# 'San Jose' <-> 'San Jose Earthquakes'). Prefix-aware so a truncated token still matches, but 'G' vs  #
# 'F' stays DISTINCT (LA Galaxy vs LAFC) — a plain drop-short-token match would collide them.           #
# --------------------------------------------------------------------------- #
def _sig_tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(name or "").lower())


SOCCER_ALIAS_PATH = os.path.join(gz_config.GENZ_DIR, "soccer_team_alias.json")


def _load_team_aliases() -> dict:
    """The learned club-name alias map (folded key -> folded key). Missing/corrupt -> {}."""
    try:
        with open(SOCCER_ALIAS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a bad alias file must never block a build
        return {}


def _save_team_aliases(aliases: dict) -> None:
    """Persist confirmed club-name pairs so future builds resolve them exactly (atomic, best-effort)."""
    try:
        os.makedirs(os.path.dirname(SOCCER_ALIAS_PATH), exist_ok=True)
        tmp = SOCCER_ALIAS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(aliases.items())), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, SOCCER_ALIAS_PATH)
    except Exception:  # noqa: BLE001
        pass


def _soccer_team_match(a: str, b: str, aliases: Optional[dict] = None) -> bool:
    """Do two spellings name the same club? Delegates to the soccer club-name normalizer (diacritic
    folding, legal-form stripping, per-token fuzzy, sponsor/city tolerance, reserve + truncation
    vetoes) plus the learned alias map. See src/genz/soccer_names.py for the evidence this is built
    on — the old ASCII-only all-tokens-must-match rule scored 0/92 on European club fixtures."""
    return soccer_names.same_club_with_alias(a, b, aliases if aliases is not None else _load_team_aliases())


def poly_search_queries(away: str, home: str) -> list[str]:
    """The /public-search queries to try for one game, IN ORDER: the combined '<away> <home>' first,
    then EACH team name ALONE.

    Poly's fuzzy search is not an AND over the words we send: for many real fixtures the combined
    query returns nothing (or unrelated politics/tennis noise) while either team name alone returns
    the exact event. Measured live 2026-07-28 on 8 games whose combined query found nothing, the
    per-team retry recovered 6 — including whole leagues that looked absent:

        'SL Benfica St. Gallen'   -> 0 hits   | 'St. Gallen'   -> uel-ben-stg-2026-07-30
        'Sparta Prague Zlin'      -> 0 hits   | 'Zlin'         -> cze1-asp-fcz-2026-07-31
        'Banfield Junin'          -> 0 hits   | 'Banfield'     -> arg-ban-cas-2026-07-28
        'Valerenga HamKam'        -> 0 hits   | 'Valerenga'    -> nor-vif-ham-2026-07-31

    This ONLY widens the candidate pool. Acceptance is unchanged and still demands slug prefix +
    date-within-1-day + BOTH team names matching, so a wider search cannot admit a wrong game.
    De-duplicated, so a one-team game costs exactly one query."""
    a, h = str(away or "").strip(), str(home or "").strip()
    return list(dict.fromkeys(q for q in (f"{a} {h}".strip(), a, h) if q))


def _resolve_competition_poly(game: "Game", poly_client: Any, log: Any = None) -> Optional[dict]:
    """Find the Polymarket base event for a non-WC game via TARGETED /public-search on the two team
    names (Poly's offset pagination caps below the soccer tag's size, so a full tag sweep is not an
    option). Accept an event only when its slug carries the competition's prefix, its slug DATE is
    within ±1, and its two title teams TOKEN-MATCH ours. Sets game.poly_base_slug; None -> Poly-absent
    (honest one-sided).

    Queries are tried in the order :func:`poly_search_queries` gives and we STOP at the first
    accepted event, so a game the combined query already resolves costs exactly one request and
    behaves exactly as before; only the games that would otherwise be reported Poly-absent pay for
    the per-team retries."""
    prefix = str(getattr(game, "poly_prefix", "") or "").lower()
    want_dates = {game.date, _shift_date(game.date, -1), _shift_date(game.date, 1)}
    aliases = _load_team_aliases()
    near_misses: list[str] = []
    seen_slugs: set = set()
    for query in poly_search_queries(game.away, game.home):
        try:
            res = poly_client.search(query)
        except Exception as exc:  # noqa: BLE001
            if log:
                log.debug("[GENZ][SOCCER] poly search %r failed for %s: %s", query, game.game_id, exc)
            continue
        events = res.get("events") if isinstance(res, dict) else res
        for e in events or []:
            if not isinstance(e, dict):
                continue
            base = _game_base_slug(str(e.get("slug") or ""))
            if base in seen_slugs:                     # already judged under an earlier query
                continue
            if prefix and not base.startswith(prefix + "-"):
                continue
            if not re.search(r"\d{4}-\d{2}-\d{2}$", base) or base[-10:] not in want_dates:
                continue
            players = _soccer_names_from_title(e.get("title"))
            if not players:
                continue
            seen_slugs.add(base)
            pa, pb = players
            # Try BOTH orientations; the venues do not agree on home/away ordering.
            for x, y in ((pa, pb), (pb, pa)):
                if _soccer_team_match(game.away, x, aliases) and _soccer_team_match(game.home, y, aliases):
                    if log:
                        sa, sh = soccer_names.score(game.away, x), soccer_names.score(game.home, y)
                        log.info("[GENZ][SOCCER] MATCH %s via %r: %r->%r (score %.2f/strong %d) + "
                                 "%r->%r (score %.2f/strong %d) -> %s", game.game_id, query,
                                 game.away, x, sa["score"], sa["strong"], game.home, y,
                                 sh["score"], sh["strong"], base)
                    # LEARN the pair when the rules alone could not derive it, so the next build is exact.
                    before = dict(aliases)
                    soccer_names.learn(aliases, game.away, x)
                    soccer_names.learn(aliases, game.home, y)
                    if aliases != before:
                        _save_team_aliases(aliases)
                    game.poly_base_slug = base
                    game.poly_slug_alts = [base]
                    return e
            # SAME date + competition but the names did not line up: the diagnostic that matters.
            near_misses.append(f"{base}: {soccer_names.explain(game.away, pa)} | "
                               f"{soccer_names.explain(game.home, pb)}")
    if log and near_misses:
        log.warning("[GENZ][SOCCER] NEAR-MISS %s (%s vs %s): %d same-date candidate(s) rejected on "
                    "NAMES — %s", game.game_id, game.away, game.home, len(near_misses),
                    " || ".join(near_misses[:3]))
    return None


def _shift_date(date: str, days: int) -> str:
    try:
        return (datetime.strptime(date, "%Y-%m-%d").date() + timedelta(days=days)).isoformat()
    except ValueError:
        return date


def _game_base_slug(slug: str) -> str:
    """The base game slug from a Poly slug (strip a '-<sibling>' suffix): 'mls-fcc-vwh-2026-07-22' or
    '...-2026-07-22-more-markets' -> 'mls-fcc-vwh-2026-07-22'."""
    m = re.match(r"^([a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})", str(slug or "").lower())
    return m.group(1) if m else str(slug or "")


# --------------------------------------------------------------------------- #
# SOCCER sport adapter — wraps the functions above verbatim (byte-identical output)#
# --------------------------------------------------------------------------- #
class SoccerSpec:
    """The World Cup adapter: delegates to this module's discovery/pairing functions unchanged, so a
    tree built via ``build_tree(..., spec=SoccerSpec())`` is byte-for-byte identical to the original."""

    name = "soccer"

    def paths(self) -> gz_config.SportPaths:
        return gz_config.paths_for_sport("soccer")

    def game_id(self, game: "Game") -> str:
        return game.game_id

    def discover_games(self, kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
                       now: datetime, log: Any = None) -> tuple[list["Game"], Any]:
        comps = getattr(cfg, "competitions", None) or []
        if not comps:
            # LEGACY World Cup path — byte-for-byte identical (the golden's default GenzConfig hits this).
            games = discover_games(kalshi_client, now=now, lookahead_hours=cfg.lookahead_hours, log=log)
            return games, fetch_series_events(poly_client, cfg.poly_series_slug, log)
        # COMPETITION-DRIVEN. Each enabled competition resolves its Kalshi game-winner series (AUTO ->
        # discovered + reported + persisted) and its Poly events; world_cup reuses the WC machinery.
        all_games: list[Game] = []
        ctx: dict[str, Any] = {"world_cup_events": []}
        series_map = load_series_map()
        for comp in comps:
            if not getattr(comp, "enabled", True):
                continue
            series_list = resolve_kalshi_series(comp, kalshi_client, series_map, log)
            if comp.name == "world_cup":
                gs = discover_games(kalshi_client, now=now, lookahead_hours=cfg.lookahead_hours, log=log)
                ctx["world_cup_events"] = fetch_series_events(poly_client, cfg.poly_series_slug, log)
            else:
                # game-winner (3-way, moneyline) + the 2-way TOTAL series (what the engine actually arbs).
                total_series = _scan_soccer_series(kalshi_client, comp, log, ends="TOTAL")
                total_series = _total_series_for(kalshi_client, series_list, total_series, log)
                gs = _discover_competition_games(kalshi_client, comp, series_list, now=now,
                                                 lookahead_hours=cfg.lookahead_hours, log=log,
                                                 pair_series=series_list + total_series)
                # Poly is resolved per-game by targeted search in pair_markets (no tag pre-fetch — the
                # soccer tag exceeds Poly's offset-pagination cap).
            all_games.extend(gs)
            if log:
                log.info("[GENZ][SOCCER] competition %s: kalshi_series=%s -> %d game(s) in window.",
                         comp.name, series_list or ["KXWCGAME"] if comp.name == "world_cup" else series_list,
                         len(gs))
        save_series_map(series_map)
        return all_games, ctx

    def pair_markets(self, kalshi_client: Any, poly_client: Any, game: "Game", poly_ctx: Any,
                     cfg: gz_config.GenzConfig, *, log: Any = None) -> dict[str, Any]:
        comp = getattr(game, "competition", "world_cup")
        if comp == "world_cup":
            se = poly_ctx.get("world_cup_events") if isinstance(poly_ctx, dict) else poly_ctx
            return self._pair_world_cup(kalshi_client, poly_client, game, se or [], cfg, log=log)
        # NON-WC: evidence-based MONEYLINE (3-way home/away/draw) pairing, reusing the WC machinery.
        return self._pair_competition(kalshi_client, poly_client, game, cfg, log=log)

    def _pair_competition(self, kalshi_client: Any, poly_client: Any, game: "Game",
                          cfg: gz_config.GenzConfig, *, log: Any = None) -> dict[str, Any]:
        """Pair one non-WC soccer game's WINNER market (the proven-same 3-way: home/away/draw). Poly side
        is resolved by targeted team-name search (slugs carry undecodable team codes); the Kalshi
        moneyline comes from the competition's game-winner series. Poly-absent -> honest one-sided."""
        comp = getattr(game, "competition", "?")
        ev = _resolve_competition_poly(game, poly_client, log)
        if ev is None:
            if log:
                log.info("[GENZ][SOCCER] %s %s vs %s: Kalshi-only (no Poly event this week) — 0 nodes.",
                         comp, game.away, game.home)
            return {"kalshi_suffix": game.kalshi_suffix, "poly_base_slug": game.poly_base_slug,
                    "home": game.home, "away": game.away, "date": game.date, "kickoff_utc": game.kickoff_iso,
                    "sport": "soccer", "competition": comp, "nodes": [], "unmatched": [],
                    "coverage": {"kalshi_ok": 0, "kalshi_failed": [], "poly_ok": 0,
                                 "poly_failed": [game.poly_base_slug], "period_mismatch_dropped": 0,
                                 "poly_absent": True}}
        # Fetch the Poly '-more-markets' sibling (the 2-way totals/spreads) alongside the 1x2 base event.
        sibs = [ev]
        try:
            more = poly_client.events_by_slug(f"{game.poly_base_slug}-more-markets")
            if isinstance(more, list):
                sibs.extend(e for e in more if isinstance(e, dict))
        except Exception as exc:  # noqa: BLE001 — no siblings -> 1x2 only, never abort the game
            if log:
                log.debug("[GENZ][SOCCER] %s more-markets fetch failed: %s", game.game_id, exc)
        kickoff = _game_kickoff(game, sibs) or game.kickoff_iso
        k_opts, k_cov = kalshi_options(kalshi_client, game, list(getattr(game, "kalshi_series", []) or []), log)
        p_opts, p_cov = poly_options(sibs, game, log)
        nodes, unmatched = join_game(k_opts, p_opts, log=log, game_id=game.game_id)
        entry = {"kalshi_suffix": game.kalshi_suffix, "poly_base_slug": game.poly_base_slug,
                 "home": game.home, "away": game.away, "date": game.date, "kickoff_utc": kickoff,
                 "sport": "soccer", "competition": comp, "nodes": nodes, "unmatched": unmatched,
                 "coverage": {"kalshi_ok": k_cov["ok"], "kalshi_failed": k_cov["failed"],
                              "poly_ok": p_cov["ok"], "poly_failed": p_cov["failed"],
                              "period_mismatch_dropped": 0}}
        if log:
            log.info("[GENZ][SOCCER] %s %s vs %s: %d node(s) | poly=%s (%d markets).", comp, game.away,
                     game.home, len(nodes), game.poly_base_slug, p_cov["ok"])
        return entry

    def _pair_world_cup(self, kalshi_client: Any, poly_client: Any, game: "Game",
                        series_events: list[dict], cfg: gz_config.GenzConfig, *,
                        log: Any = None) -> dict[str, Any]:
        # Correct the Poly slug for Kalshi<->Poly 3-letter-code mismatches (POR/PRT, SUI/CHE, …) BEFORE
        # reading the Poly side, so a code mismatch can never silently drop a game.
        resolve_poly_slug(game, series_events, log)
        kickoff = _game_kickoff(game, series_events)       # PRECISE Poly kickoff, else provisional noon
        k_opts, k_cov = kalshi_options(kalshi_client, game, cfg.kalshi_series, log)
        p_opts, p_cov = poly_options(series_events, game, log)
        nodes, unmatched = join_game(k_opts, p_opts, log=log, game_id=game.game_id)
        period_dropped = sum(1 for u in unmatched if u.get("reason") == "settlement_period_mismatch") // 2
        entry = {
            "kalshi_suffix": game.kalshi_suffix, "poly_base_slug": game.poly_base_slug,
            "home": game.home, "away": game.away, "date": game.date, "kickoff_utc": kickoff,
            "nodes": nodes, "unmatched": unmatched,
            # coverage: how many series/siblings succeeded vs failed this build (dashboard shows gaps),
            # plus how many count-market pairs were dropped for a settlement-period mismatch (the trap).
            "coverage": {"kalshi_ok": k_cov["ok"], "kalshi_failed": k_cov["failed"],
                         "poly_ok": p_cov["ok"], "poly_failed": p_cov["failed"],
                         "period_mismatch_dropped": period_dropped},
        }
        if log:
            two_way = sum(1 for n in nodes if n["kind"] == "2way")
            log.info("[GENZ] %s %s vs %s: %d matched node(s) (%d 2-way), %d unmatched | "
                     "kalshi %d ok/%d failed, poly %d ok/%d failed.",
                     game.game_id, game.away, game.home, len(nodes), two_way, len(unmatched),
                     k_cov["ok"], len(k_cov["failed"]), p_cov["ok"], len(p_cov["failed"]))
        return entry


SOCCER_SPEC = SoccerSpec()


def build_tree(kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
               now: Optional[datetime] = None, log: Any = None, spec: Any = None) -> dict[str, Any]:
    """Discover games and build the full match tree dict (does not write). Pure-ish: all network goes
    through the injected clients, so tests pass fakes. ``spec`` selects the sport adapter (default
    soccer, which is byte-identical to the pre-refactor builder)."""
    spec = spec or SOCCER_SPEC
    now = now or datetime.now(timezone.utc)
    games, poly_ctx = spec.discover_games(kalshi_client, poly_client, cfg, now=now, log=log)
    tree: dict[str, Any] = {"games": {}}
    for game in games:
        # A PARTIAL tree is fine: an unexpected error on one game must not kill the rest of the build.
        try:
            entry = spec.pair_markets(kalshi_client, poly_client, game, poly_ctx, cfg, log=log)
        except Exception as exc:  # noqa: BLE001 - never abort the whole build for one game
            if log:
                log.warning("[GENZ] %s build failed: %s — skipping this game.", spec.game_id(game), exc)
            continue
        if entry is not None:
            tree["games"][spec.game_id(game)] = entry
    # SYSTEMIC PAIRING ALARM: a build where almost no game paired is a venue format drift, not a quiet
    # "nothing today" — log it loudly (the meta carries the machine-readable alert; see build_meta).
    from .sports_base import pairing_alert
    alert = pairing_alert(tree, getattr(spec, "name", "?"))
    if alert and log:
        name = str(getattr(spec, "name", "?")).upper()
        for b in alert.get("broken", []):
            log.warning("[%s] SYSTEMIC PAIRING FAILURE: competition '%s' %d/%d games paired (%.0f%%) — "
                        "probable venue format drift.", name, b.get("competition"), b.get("paired", 0),
                        b.get("total", 0), (b.get("share", 0) or 0) * 100)
        for o in alert.get("one_sided", []):
            log.info("[%s] competition '%s' is %d one-sided on %s (normal — a venue doesn't carry it).",
                     name, o.get("competition"), o.get("count", 0), o.get("venue", "?"))
    return tree


def build_meta(tree: dict[str, Any], *, now: datetime, sport: str = "?") -> dict[str, Any]:
    from .sports_base import pairing_alert
    games = tree.get("games", {})
    kickoffs = sorted((g.get("kickoff_utc", "") for g in games.values()) if games else [])
    # Per-game coverage so the dashboard can show which market types failed to fetch this build.
    coverage = {gid: g.get("coverage", {}) for gid, g in games.items()}
    any_failed = sorted({s for g in games.values()
                         for s in (g.get("coverage", {}).get("kalshi_failed", []))})
    # Per-game count-market pairs dropped for a settlement-period mismatch (the extra-time trap).
    period_dropped = {gid: g.get("coverage", {}).get("period_mismatch_dropped", 0)
                      for gid, g in games.items() if g.get("coverage", {}).get("period_mismatch_dropped")}
    meta = {"built_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "games": sorted(games.keys()), "next_kickoffs": kickoffs[:10],
            "coverage": coverage, "kalshi_series_failed_any_game": any_failed,
            "period_mismatch_dropped_by_game": period_dropped,
            "period_mismatch_dropped_total": sum(period_dropped.values())}
    alert = pairing_alert(tree, sport)                     # None unless a systemic pairing failure
    if alert:
        meta["systemic_alert"] = alert
    return meta


def _atomic_write_json(path: str, obj: Any) -> None:
    """Write ``obj`` to ``path`` via a per-pid tmp + ``os.replace``, so a READER never sees a partial file.

    A plain truncate-write leaves the tree empty and then half-parsed for as long as ``json.dump`` takes
    on a ~1MB document, and maker_rt reloads on mtime change — a poll that lands inside that window gets
    a JSONDecodeError. That raced three times on 2026-07-29 (07:36, 12:07, 12:37), and because the
    exception surfaced inside the maker's event loop it killed the LIVE process each time, cancelling
    every resting order on the way out (nine of them at 12:06:58) and losing all their queue position.
    ``os.replace`` is atomic on both POSIX and Windows, so the reader sees either the old tree or the new
    one and never the seam. (``save_series_map`` a few hundred lines up already did exactly this.)"""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_tree(tree: dict[str, Any], *, now: Optional[datetime] = None,
               tree_path: Optional[str] = None, meta_path: Optional[str] = None,
               sport: str = "?") -> tuple[str, str]:
    """Persist match_tree.json + tree_meta.json under data/genz/ ATOMICALLY. Returns the paths written."""
    now = now or datetime.now(timezone.utc)
    gz_config.ensure_dirs()
    tp = tree_path or gz_config.MATCH_TREE_PATH
    mp = meta_path or gz_config.TREE_META_PATH
    _atomic_write_json(tp, tree)
    _atomic_write_json(mp, build_meta(tree, now=now, sport=sport))
    return tp, mp


def load_tree(tree_path: Optional[str] = None) -> dict[str, Any]:
    """Load match_tree.json (the engine reads this every cycle). {} if absent.

    Reads ``utf-8-sig`` so a hand-edited tree carrying a Windows BOM still parses. A file that is
    present but UNPARSEABLE still raises — that is information, and the maker's own reader
    (``universe.load_trees``) is where it is turned into "keep the previous tree for this sport"."""
    tp = tree_path or gz_config.MATCH_TREE_PATH
    if not os.path.exists(tp):
        return {"games": {}}
    with open(tp, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)
