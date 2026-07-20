"""Tennis sport adapter for the GenZ tree builder (a THIRD sport; soccer + MLB pipelines untouched).

Discovers ATP/WTA matches on Kalshi (series KXATPMATCH + KXWTAMATCH, one event per match holding two
per-player YES/NO markets), pairs each to its Polymarket match event, and emits the engine's tree-node
shape. ONE market family in v1 (deliberate):

    match_winner   2-way MECE (no draw). twin_key match_winner|<surname>.

WHY ONLY THE WINNER (the corners lesson): Poly's sets/games markets resolve "No" on ANY forfeit while
Kalshi's exact-score/games handle a retirement differently, so a cross-venue sets/games pair is NOT
the same bet (it can lose both legs on a retirement). The match WINNER is the only proven-same bet:
both venues pay the player who ADVANCES once a ball is played. Everything else on either venue is left
UNPAIRED and logged as a per-build INVENTORY line, so family #2 can be added later on evidence.

THE WALKOVER GUARD (the tennis rain-rule): a match can settle DIFFERENTLY across venues on a PRE-BALL
walkover. On the STARTED rule (a ball was played, then a retirement) both venues pay the advancing
player — aligned. On a WALKOVER (withdrawal before a ball) Poly resolves 50-50 while Kalshi refunds to
a fair/last price. That divergence is EXPECTED and INFORMATIONAL (settlement_note="walkover_50_50",
shown as an amber W/O chip) — NOT excluded. A started-rule divergence (a venue that settles a
retirement on price, not advancement) is a REAL divergence -> the pair is refused. An UNPARSEABLE
resolution text on either side is conservatively flagged settlement_risk (excluded), exactly like MLB.

Ticker facts (verified live 2026-07): KX(ATP|WTA)MATCH-<YY><MON><DD><FRAG1><FRAG2>, FRAGs = 3-letter
surname fragments (AMBIGUOUS -> the TITLE "<Player A> vs <Player B>" is the source of truth). Each
event = 2 markets (ticker '<event>-<CODE>', yes_sub_title = full player name, title carries both
names). Poly event slug <tour>-<s1>-<s2>-<YYYY-MM-DD>, s* = last-name lowercased/accent-stripped and
TRUNCATED to 7 chars; the slug DATE can differ from startTime by a day (UTC/ET rollover). Tennis start
times slide hours ("not before"), so pairing NEVER refuses on a start-time delta — it matches on
surname TOKEN SETS from the titles + date ±1.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .. import polymarket as pm
from . import config as gz_config
from .tree_builder import _poly_market_fee   # the generic gamma feeSchedule extraction (reused)

_SERIES_TOUR = {"KXATPMATCH": "atp", "KXWTAMATCH": "wta"}


# --------------------------------------------------------------------------- #
# Name normalization (accents, surnames, slug fragments, token sets)             #
# --------------------------------------------------------------------------- #
def _strip_accents(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(ch))


def _norm(s: str) -> str:
    """Lowercase, accent-stripped, single-spaced alphanumerics — for tolerant name matching."""
    t = re.sub(r"[^a-z0-9 ]", " ", _strip_accents(s).lower())
    return re.sub(r"\s+", " ", t).strip()


# Stage/round + title-boilerplate words that must NEVER be matching tokens: a title-drift leak
# ('Torres: Round Of 16' -> tokens {torres, round, of}) would otherwise defeat token-set matching and
# mint a phantom no-match. Guarded here so even a name that slips the sanitizer still matches on the
# real surname. Derived from the committed raw dump (tests/fixtures/raw/tennis_titledrift_kalshi.json).
_STAGE_STOPWORDS = frozenset({
    "round", "of", "final", "finals", "quarterfinal", "quarterfinals", "semifinal", "semifinals",
    "quarter", "semi", "qualifier", "qualifying", "qualification", "robin", "match", "vs", "win",
    "wins", "will", "the",
})


def name_tokens(full: str) -> frozenset:
    """The normalized word tokens of a player name — for token-set (containment) matching across
    venues, which resolves both the surname-truncation slug quirk AND a dropped middle name
    ('Daniel Merida' ⊆ 'Daniel Merida Aguilar'). Stage/boilerplate words are dropped (see
    ``_STAGE_STOPWORDS``) so a title-drift leak can never poison the token set."""
    return frozenset(t for t in _norm(full).split() if t and t not in _STAGE_STOPWORDS)


# Surname particles that belong WITH the final token (so 'Alex de Minaur' -> 'de minaur').
_PARTICLES = frozenset({"de", "del", "della", "di", "da", "van", "von", "der", "den", "la", "le",
                        "el", "al", "bin", "ibn", "dos", "das", "mc", "mac"})


def _surname_raw(full: str) -> str:
    """The raw surname substring from the ORIGINAL name — the last WHITESPACE token, extended backwards
    over known particles ('de Minaur', 'van der ...'). Keeps internal hyphens/apostrophes intact so a
    compound ('Auger-Aliassime') or apostrophe name ("O'Connell") survives to the slug step."""
    toks = str(full or "").split()
    if not toks:
        return ""
    i = len(toks) - 1
    while i > 0 and _norm(toks[i - 1]) in _PARTICLES:
        i -= 1
    return " ".join(toks[i:])


def surname(full: str) -> str:
    """The surname of a full name, NORMALIZED (accent-stripped, lowercased, single-spaced) — the
    twin_key side + the token-match key, so both venues produce the same key for the same player."""
    return _norm(_surname_raw(full))


def surname_slug(full: str) -> str:
    """The Polymarket slug fragment for a surname: accent-stripped, lowercased, apostrophes/spaces ->
    hyphens, compound hyphens kept ('auger-aliassime', 'de-minaur', "o'connell" -> 'o-connell'). This
    is the UN-truncated form; construct_slugs also tries Poly's 7-char truncation."""
    s = _strip_accents(_surname_raw(full)).lower()
    s = re.sub(r"[’'`]", "-", s)                       # apostrophes -> hyphen
    s = re.sub(r"[^a-z0-9-]+", "-", s)                      # spaces/other -> hyphen (keeps hyphens)
    return re.sub(r"-+", "-", s).strip("-")


def _slug_frag_variants(full: str) -> list[str]:
    """Candidate Poly slug fragments for a player: the full surname slug AND its 7-char truncation
    (Poly's observed convention). De-duped, order preserved."""
    base = surname_slug(full)
    out: list[str] = []
    for cand in (base, base[:7]):
        if cand and cand not in out:
            out.append(cand)
    return out


# --------------------------------------------------------------------------- #
# Walkover rule parsing (the tennis rain-guard) — from the REAL captured texts    #
# --------------------------------------------------------------------------- #
# STARTED rule = how an INCOMPLETE started match (a ball was played, then retirement) settles.
_ADVANCING = ("advances against", "player who advances", "player that advances", "after a ball has been played",
              "advances due to", "retirement, default", "who advances")
# a venue that settles a STARTED-but-incomplete match on PRICE (not advancement) is a real divergence
_STARTED_LAST_PRICE = ("suspended", "not resumed")
# WALKOVER rule = how a PRE-BALL withdrawal settles.
_WALK_5050 = ("50-50", "50/50", "fifty-fifty", "fifty fifty")
_WALK_PRICE = ("fair price", "last traded price", "last fair market price", "last price")


def parse_tennis_rules(text: str) -> dict:
    """Classify a resolution text into its two facets:
        started_rule  in {'advancing_player', 'last_price_started', None}
        walkover_rule in {'fifty_fifty_walkover', 'last_price_walkover', None}
    A single text can carry BOTH (Poly says a retirement pays the advancing player AND a walkover pays
    50-50). None on a facet means the text did not state it."""
    low = str(text or "").lower()
    started = None
    if any(p in low for p in _ADVANCING):
        started = "advancing_player"
    elif any(p in low for p in _STARTED_LAST_PRICE) and any(p in low for p in _WALK_PRICE):
        started = "last_price_started"
    walkover = None
    walk_ctx = ("walkover" in low or "withdraw" in low or "does not occur" in low
                or "before the match starts" in low or "before the start" in low or "cancel" in low)
    if "walkover" in low and any(p in low for p in _WALK_5050):
        walkover = "fifty_fifty_walkover"
    elif walk_ctx and any(p in low for p in _WALK_5050):
        walkover = "fifty_fifty_walkover"
    elif walk_ctx and any(p in low for p in _WALK_PRICE):
        walkover = "last_price_walkover"
    return {"started_rule": started, "walkover_rule": walkover}


# --------------------------------------------------------------------------- #
# Discovery                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class TennisMatch:
    event_ticker: str            # KX(ATP|WTA)MATCH-<suffix> (unique per match)
    tour: str                    # 'atp' | 'wta'
    date: str                    # YYYY-MM-DD (Kalshi ticker date; DISPLAY schedule, may slide)
    player_a: str                # full names (title order)
    player_b: str
    kickoff_iso: str             # provisional; refined from Poly startTime when paired
    markets: list = field(default_factory=list)   # the 1-or-2 Kalshi per-player markets
    poly_base_slug: str = ""

    @property
    def game_id(self) -> str:
        return self.event_ticker


_SUFFIX_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})([A-Z]{3})([A-Z]{3})$")
_TITLE_VS_RE = re.compile(r"\bvs\.?\b", re.IGNORECASE)

# Trailing round/stage decoration on a Kalshi (or Poly) tennis title. DERIVED FROM THE REAL TEXT
# (tests/fixtures/raw/tennis_titledrift_kalshi.json: "... Tabilo vs Torres: Round Of 16 match?"), not
# assumed — the ': Round Of 16' bled into player_b before this. Optional leading ':'/'-'/en-dash, the
# stage phrase, then everything after it. Case-insensitive.
_STAGE_SUFFIX_RE = re.compile(
    r"[:\-–]?\s*(round\s+of\s+\d+|quarterfinals?|semifinals?|finals?|qualifying|qualifier)\b.*$",
    re.IGNORECASE)


def strip_stage_suffix(title: str) -> str:
    """Remove a trailing round/stage decoration ('...: Round Of 16', ' - Semifinal', ' Final') from a
    tennis title BEFORE any name extraction. Idempotent; leaves a clean '<A> vs <B>' (or bare name)."""
    return _STAGE_SUFFIX_RE.sub("", str(title or "")).strip(" .:-")


def _decode_date(suffix: str) -> Optional[str]:
    m = _SUFFIX_RE.match(suffix or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).title(), "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _names_from_title(title: str) -> Optional[tuple[str, str]]:
    """('Player A', 'Player B') from a Kalshi tennis title — used ONLY to fix the A-vs-B ORDER (the full
    names come from yes_sub_title; see _player_names). Handles the market title ('Will X win the A vs B:
    Round Of 16 match?') and a bare 'A vs B'. The stage suffix is stripped from the extracted core, so
    'Round Of 16' can never leak into a name."""
    t = str(title or "")
    m = re.search(r"win the (.+?)\s+match\b", t, re.IGNORECASE)
    core = strip_stage_suffix(m.group(1) if m else t)      # extract the 'A vs B' core, then de-stage it
    parts = _TITLE_VS_RE.split(core, maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(" .:-"), parts[1].strip(" .:-")
    return (a, b) if a and b else None


def _player_names(mkts: list[dict]) -> Optional[tuple[str, str]]:
    """The two player full names for an event. PRIMARY SOURCE = the per-player markets' yes_sub_title
    full names (already fetched, never stage-decorated); the event TITLE is fallback ONLY, used to fix
    the A-vs-B ORDER (and for a single-market event). This is the title-drift fix: a corrupted title
    ('Tabilo vs Torres: Round Of 16') no longer decides names — yes_sub_title does."""
    fulls: list[str] = []
    seen: set[str] = set()
    for m in mkts:
        f = str(m.get("yes_sub_title") or "").strip()
        sn = surname(f)
        if f and sn and sn not in seen:
            seen.add(sn)
            fulls.append(f)
    title_names = None
    for m in mkts:
        title_names = _names_from_title(m.get("title"))
        if title_names:
            break
    if title_names:                                        # order the yes_sub_title fulls by title order
        ordered = [next((f for f in fulls if surname(f) == surname(tn)), tn) for tn in title_names]
        if ordered[0] and ordered[1]:
            return (ordered[0], ordered[1])
    if len(fulls) == 2:                                    # no/ambiguous title -> yes_sub_title order
        return (fulls[0], fulls[1])
    return title_names


def _suffix_of(m: dict) -> str:
    et = str(m.get("event_ticker") or "")
    return et.split("-", 1)[1] if "-" in et else et


def _enrich_full_names(title_names: tuple, mkts: list[dict]) -> tuple:
    """Upgrade the title's surname pair to FULL names using each market's yes_sub_title (matched by
    surname), keeping the title's A-vs-B order. Falls back to the title surname when no market matches
    (e.g. a single-market Kalshi event only names one player)."""
    fulls = [str(m.get("yes_sub_title") or "").strip() for m in mkts if m.get("yes_sub_title")]
    out = []
    for tn in title_names:
        sn = surname(tn)
        full = next((f for f in fulls if surname(f) == sn), None)
        out.append(full or tn)
    return tuple(out)


def discover_tennis_matches(kalshi_client: Any, *, now: datetime, lookahead_hours: float,
                            series: list[str], log: Any = None) -> list[TennisMatch]:
    """Every open KX(ATP|WTA)MATCH event within [now-12h, now+lookahead]. Names come from the market
    TITLES (yes_sub_title gives one player each; the title carries the 'A vs B' order). An event whose
    title won't parse to two players is logged and skipped."""
    by_event: dict[str, list[dict]] = {}
    tour_of: dict[str, str] = {}
    for s in series:
        tour = _SERIES_TOUR.get(s)
        if not tour:
            continue
        try:
            for m in kalshi_client.iter_markets(series_ticker=s, status="open"):
                if isinstance(m, dict):
                    et = str(m.get("event_ticker") or "")
                    by_event.setdefault(et, []).append(m)
                    tour_of[et] = tour
        except Exception as exc:  # noqa: BLE001 - one series failing must not abort the build
            if log:
                log.warning("[TENNIS] %s discovery failed: %s — that tour absent this build.", s, exc)

    horizon = now.timestamp() + lookahead_hours * 3600.0
    out: list[TennisMatch] = []
    for et, mkts in by_event.items():
        suffix = _suffix_of(mkts[0])
        date = _decode_date(suffix)
        names = _player_names(mkts)                        # PRIMARY = yes_sub_title full names; title = order only
        if not date or not names:
            if log:
                log.warning("[TENNIS] unparseable event %s (title=%r) — skipping.",
                            et, (mkts[0].get("title") if mkts else ""))
            continue
        kickoff = f"{date}T12:00:00Z"                       # provisional; refined from Poly startTime
        ts = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp()
        if ts > horizon or ts < now.timestamp() - 12 * 3600.0:
            continue
        out.append(TennisMatch(event_ticker=et, tour=tour_of.get(et, "atp"), date=date,
                               player_a=names[0], player_b=names[1], kickoff_iso=kickoff,
                               markets=list(mkts)))
    return out


# --------------------------------------------------------------------------- #
# Polymarket event resolution (slug construct + token-set scan; time is DISPLAY) #
# --------------------------------------------------------------------------- #
def _parse_iso(v: Any) -> Optional[float]:
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def _dates_pm1(date: str) -> list[str]:
    """[date, date-1, date+1] as YYYY-MM-DD — tennis slug dates slide across the UTC/ET rollover."""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return [date]
    return [date, (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()]


def construct_slugs(match: TennisMatch) -> list[str]:
    """Candidate Poly slugs: <tour>-<s1>-<s2>-<date> for BOTH surname orders, each fragment variant
    (full + 7-char), across date ±1. De-duped, order preserved (best guesses first)."""
    fa, fb = _slug_frag_variants(match.player_a), _slug_frag_variants(match.player_b)
    out: list[str] = []
    for date in _dates_pm1(match.date):
        for s1 in fa:
            for s2 in fb:
                for a, b in ((s1, s2), (s2, s1)):
                    slug = f"{match.tour}-{a}-{b}-{date}"
                    if slug not in out:
                        out.append(slug)
    return out


def _same_player(a: str, b: str) -> bool:
    """Token containment BOTH ways (a's tokens ⊆ b's or vice-versa) — matches 'Daniel Merida' to
    'Daniel Merida Aguilar' and tolerates a dropped middle/first name, accent-insensitive."""
    ta, tb = name_tokens(a), name_tokens(b)
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def _event_players(ev: dict) -> Optional[tuple[str, str]]:
    """The two player full names from a Poly tennis event title ('<Tournament>: A vs B'). The stage
    suffix is stripped defensively (Poly can decorate titles too)."""
    title = str(ev.get("title") or "")
    core = title.split(":", 1)[1] if ":" in title else title
    core = strip_stage_suffix(core)
    parts = _TITLE_VS_RE.split(core, maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(" .:-"), parts[1].strip(" .:-")
    return (a, b) if a and b else None


def resolve_poly_event(match: TennisMatch, series_events: list[dict], poly_client: Any,
                       log: Any = None) -> tuple[Optional[dict], str]:
    """The Polymarket match event for this Kalshi match, and the METHOD used ('slug' | 'scan' | 'none').
    Primary: construct the slug (both orders, 7-char truncation, date ±1). Fallback (primary in
    practice): scan the pre-fetched tour events for one whose two title players TOKEN-MATCH ours within
    date ±1. Start time is DISPLAY ONLY — tennis 'not before' times slide, so pairing NEVER refuses on a
    time delta. Returns (event|None, method)."""
    for slug in construct_slugs(match):
        try:
            evs = poly_client.events_by_slug(slug)
        except Exception:  # noqa: BLE001
            evs = None
        ev = evs[0] if isinstance(evs, list) and evs else None
        if ev:
            match.poly_base_slug = slug
            return ev, "slug"
    # Fallback: token-set scan over the tour's series events within date ±1.
    want_dates = set(_dates_pm1(match.date))
    for e in series_events or []:
        if not isinstance(e, dict):
            continue
        if (str(e.get("startTime") or "")[:10] not in want_dates
                and str(e.get("slug") or "")[-10:] not in want_dates):
            continue
        players = _event_players(e)
        if not players:
            continue
        pa, pb = players
        if ((_same_player(match.player_a, pa) and _same_player(match.player_b, pb))
                or (_same_player(match.player_a, pb) and _same_player(match.player_b, pa))):
            match.poly_base_slug = str(e.get("slug") or "")
            if log:
                log.info("[TENNIS] %s %s vs %s: matched via SCAN -> %s.",
                         match.event_ticker, match.player_a, match.player_b, match.poly_base_slug)
            return e, "scan"
    if log:
        # SELF-DIAGNOSING (the NEXT drift): dump both venues' extracted token sets so a future
        # name/format drift is visible in the log without a code change.
        want = sorted(name_tokens(match.player_a) | name_tokens(match.player_b))
        cand = []
        for e in series_events or []:
            if not isinstance(e, dict):
                continue
            if (str(e.get("startTime") or "")[:10] in want_dates
                    or str(e.get("slug") or "")[-10:] in want_dates):
                ps = _event_players(e)
                if ps:
                    cand.append(sorted(name_tokens(ps[0]) | name_tokens(ps[1])))
        log.info("[TENNIS] %s %s vs %s: NO Poly event (tried %d slugs + scan) — one-sided. "
                 "kalshi_tokens=%s | poly_candidate_tokens(date-window)=%s",
                 match.event_ticker, match.player_a, match.player_b, len(construct_slugs(match)),
                 want, cand[:8])
    return None, "none"


# --------------------------------------------------------------------------- #
# Options (match winner)                                                         #
# --------------------------------------------------------------------------- #
_KALSHI_TEXT_KEYS = ("rules_primary", "rules_secondary", "subtitle", "title")


def _kalshi_text(m: dict) -> str:
    return " ".join(str(m.get(k) or "") for k in _KALSHI_TEXT_KEYS)


def kalshi_tennis_options(match: TennisMatch) -> tuple[dict[str, dict], list[dict]]:
    """({twin_key: option}, inventory) for the Kalshi side. The match-winner is one twin_key per player
    (best-of-both — Kalshi posts a YES/NO market for each). Any market whose player can't be resolved
    from the title goes to the inventory (never paired)."""
    opts: dict[str, dict] = {}
    inv: list[dict] = []
    for m in match.markets:
        player = str(m.get("yes_sub_title") or "").strip()
        sn = surname(player)
        if not sn or not _match_player(player, match):
            inv.append({"venue": "kalshi", "title": m.get("title"), "market_type": "other"})
            continue
        key = f"match_winner|{sn}"
        opts[key] = {
            "market_type": "match_winner", "market_key": "match_winner", "side": sn, "line": None,
            "kind": "2way", "confidence": "high", "outcome_label": player,
            "kalshi_ticker": m.get("ticker"), "kalshi_side": "YES", "kalshi_text": _kalshi_text(m),
        }
    return opts, inv


def _match_player(name: str, match: TennisMatch) -> bool:
    return _same_player(name, match.player_a) or _same_player(name, match.player_b)


def poly_tennis_options(event: dict, match: TennisMatch, *, log: Any = None,
                        game_id: str = "") -> tuple[dict[str, dict], list[dict]]:
    """({twin_key: option}, inventory) for the Poly side. IDENTITY BEFORE SHAPE: the match-winner is the
    single market whose slug == the event slug (groupItemTitle null, two full-player outcomes) — NOT any
    'Set 1 Winner' market, which shares the two-name shape. Its two outcome tokens map to our players by
    token match. Every OTHER tennis market (sets/games/totals/handicap) is inventoried, never paired."""
    opts: dict[str, dict] = {}
    inv: list[dict] = []
    if not isinstance(event, dict):
        return opts, inv
    eslug = str(event.get("slug") or "")
    for m in event.get("markets") or []:
        if not isinstance(m, dict):
            continue
        mslug = str(m.get("slug") or "")
        outs = pm._as_list(m.get("outcomes"))
        toks = pm._as_list(m.get("clobTokenIds"))
        is_winner = (mslug == eslug and not m.get("groupItemTitle") and len(outs) == 2 and len(toks) >= 2
                     and _players_are_ours(outs, match))
        if not is_winner:
            inv.append({"venue": "polymarket", "title": m.get("question") or m.get("groupItemTitle"),
                        "market_type": "other"})
            continue
        fee = _poly_market_fee(m)
        desc = str(m.get("description") or "")
        for tok, out in zip(toks, outs):
            sn = _our_surname(out, match)
            if not sn:
                continue
            opts[f"match_winner|{sn}"] = {
                "market_type": "match_winner", "market_key": "match_winner", "side": sn, "line": None,
                "kind": "2way", "confidence": "high", "outcome_label": str(out),
                "poly_token_id": str(tok), "poly_side": str(out), "poly_fee_enabled": fee["enabled"],
                "poly_fee_rate": fee["rate"], "poly_fee_taker_only": fee["taker_only"], "poly_text": desc}
    return opts, inv


def _players_are_ours(outcomes: list, match: TennisMatch) -> bool:
    got = {_our_surname(o, match) for o in outcomes}
    got.discard("")
    return len(got) == 2


def _our_surname(name: str, match: TennisMatch) -> str:
    """The canonical surname (Kalshi convention) for a Poly outcome name — via token match to our two
    players, so both venues key the same twin_key even when one lists a middle name."""
    if _same_player(name, match.player_a):
        return surname(match.player_a)
    if _same_player(name, match.player_b):
        return surname(match.player_b)
    return ""


# --------------------------------------------------------------------------- #
# Join + walkover guard                                                          #
# --------------------------------------------------------------------------- #
def join_tennis(k_opts: dict[str, dict], p_opts: dict[str, dict], *, log: Any = None,
                game_id: str = "") -> tuple[list[dict], list[dict]]:
    """Pair Kalshi<->Poly match_winner options by twin_key. Each node gets the WALKOVER GUARD (both
    venues' settlement_texts + facet classification). Unpaired keys -> unmatched."""
    nodes: list[dict] = []
    refused: set = set()
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
        keep = _apply_walkover_guard(node, k.get("kalshi_text", ""), p.get("poly_text", ""), log, game_id)
        if keep:
            nodes.append(node)
        else:
            refused.add(key)
    unmatched: list[dict] = []
    for key in sorted((set(k_opts) - set(p_opts)) | refused):
        o = k_opts.get(key)
        if o:
            unmatched.append({"venue": "kalshi", "market_type": o["market_type"],
                              "outcome_label": o.get("outcome_label"), "identifier": o.get("kalshi_ticker"),
                              "reason": "started_rule_divergence" if key in refused else "one_venue_only"})
    for key in sorted((set(p_opts) - set(k_opts)) | refused):
        o = p_opts.get(key)
        if o:
            unmatched.append({"venue": "polymarket", "market_type": o["market_type"],
                              "outcome_label": o.get("outcome_label"), "identifier": o.get("poly_token_id"),
                              "reason": "started_rule_divergence" if key in refused else "one_venue_only"})
    return nodes, unmatched


def _apply_walkover_guard(node: dict, kalshi_text: str, poly_text: str, log: Any, game_id: str) -> bool:
    """Classify a match_winner node. Returns True to KEEP (paired), False to REFUSE.
      - started_rule NOT in {advancing_player, None} on either side -> REFUSE (a real divergence).
      - walkover_rule unparseable on a side -> settlement_risk (conservative, excluded), KEEP-flagged.
      - walkover_rule divergence (50-50 vs last-price) -> settlement_note='walkover_50_50' (INFO), KEEP.
    Both venues' raw texts are always stored for the row expando."""
    kr = parse_tennis_rules(kalshi_text)
    pr = parse_tennis_rules(poly_text)
    node["settlement_texts"] = {"kalshi": kalshi_text[:600], "poly": poly_text[:600]}
    node["kalshi_rule"] = kr
    node["poly_rule"] = pr
    if kr["started_rule"] == "last_price_started" or pr["started_rule"] == "last_price_started":
        if log:
            log.warning("[TENNIS] %s %s: started_rule divergence (kalshi=%s poly=%s) — REFUSED pairing.",
                        game_id, node["market_key"], kr["started_rule"], pr["started_rule"])
        return False
    if kr["walkover_rule"] is None or pr["walkover_rule"] is None:
        node["settlement_risk"] = "tennis_unparsed_settlement"   # conservative: can't tell -> excluded
        if log:
            log.warning("[TENNIS] %s %s: unparseable walkover rule (kalshi=%s poly=%s) — settlement_risk.",
                        game_id, node["twin_key"], kr["walkover_rule"], pr["walkover_rule"])
        return True
    if kr["walkover_rule"] != pr["walkover_rule"]:
        node["settlement_note"] = "walkover_50_50"               # EXPECTED, informational (amber chip)
        if log:
            log.info("[TENNIS] %s %s: walkover 50-50 note (kalshi=%s poly=%s) — informational, tradable.",
                     game_id, node["twin_key"], kr["walkover_rule"], pr["walkover_rule"])
    return True


# --------------------------------------------------------------------------- #
# Inventory (unpaired families -> a per-build evidence line)                      #
# --------------------------------------------------------------------------- #
def _inventory_line(match: TennisMatch, k_inv: list, p_inv: list) -> str:
    def tally(inv):
        titles = [str(x.get("title") or "").strip() for x in inv if x.get("title")]
        head = "; ".join(list(dict.fromkeys(titles))[:6])   # first 6 distinct titles
        return len(inv), head
    kn, kh = tally(k_inv)
    pn, ph = tally(p_inv)
    return (f"[TENNIS][INVENTORY] {match.event_ticker} {match.player_a} vs {match.player_b}: "
            f"kalshi {kn} other market(s) [{kh}] | poly {pn} other market(s) [{ph}]")


# --------------------------------------------------------------------------- #
# The Tennis SportSpec                                                           #
# --------------------------------------------------------------------------- #
class TennisSpec:
    """The tennis adapter consumed by tree_builder.build_tree(spec=TENNIS_SPEC)."""

    name = "tennis"

    def paths(self) -> gz_config.SportPaths:
        return gz_config.paths_for_sport("tennis")

    def game_id(self, match: TennisMatch) -> str:
        return match.game_id

    def discover_games(self, kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
                       now: datetime, log: Any = None) -> tuple[list[TennisMatch], dict[str, Any]]:
        matches = discover_tennis_matches(kalshi_client, now=now, lookahead_hours=cfg.lookahead_hours,
                                          series=list(cfg.kalshi_series), log=log)
        series_events: list[dict] = []
        for tour in getattr(cfg, "poly_tours", ["atp", "wta"]):
            try:
                series_events.extend(poly_client.events_by_series(tour, closed=False))
            except Exception as exc:  # noqa: BLE001
                if log:
                    log.warning("[TENNIS] poly tour %s fetch failed: %s — slug-only for it this build.",
                                tour, exc)
        return matches, {"series_events": series_events}

    def pair_markets(self, kalshi_client: Any, poly_client: Any, match: TennisMatch, poly_ctx: dict,
                     cfg: gz_config.GenzConfig, *, log: Any = None) -> dict[str, Any]:
        series_events = poly_ctx.get("series_events") or []
        ev, method = resolve_poly_event(match, series_events, poly_client, log)
        k_opts, k_inv = kalshi_tennis_options(match)
        if ev:
            p_opts, p_inv = poly_tennis_options(ev, match, log=log, game_id=match.event_ticker)
        else:
            p_opts, p_inv = {}, []
        kickoff = str((ev or {}).get("startTime") or "") or match.kickoff_iso
        if not _parse_iso(kickoff):
            kickoff = match.kickoff_iso
        nodes, unmatched = join_tennis(k_opts, p_opts, log=log, game_id=match.event_ticker)
        # ADDITIVE family registry (total_sets): SCOPED to the total-sets markets on each venue so the
        # winner path stays byte-identical and the many other tennis markets keep their existing inventory
        # logging. Today Kalshi lists no set market -> total_sets is Poly-only inventory; the synthesis +
        # forfeit-risk path activates automatically if/when Kalshi posts one.
        reg_nodes, reg_unmatched, reg_refusals = _run_family_registry(match, ev, log=log)
        nodes = nodes + reg_nodes
        unmatched = unmatched + reg_unmatched
        risk = sum(1 for n in nodes if n.get("settlement_risk"))
        note = sum(1 for n in nodes if n.get("settlement_note"))
        if log:
            log.info(_inventory_line(match, k_inv, p_inv))
        entry = {
            "kalshi_suffix": _suffix_of(match.markets[0]) if match.markets else "",
            "poly_base_slug": match.poly_base_slug, "tour": match.tour,
            "away": match.player_a, "home": match.player_b, "date": match.date,
            "kickoff_utc": kickoff, "sport": "tennis", "poly_match_method": method,
            "nodes": nodes, "unmatched": unmatched, "refusals": reg_refusals,
            "coverage": {"kalshi_ok": 1, "kalshi_failed": [], "poly_ok": 1 if ev else 0,
                         "poly_failed": [] if ev else [match.poly_base_slug or match.event_ticker],
                         "settlement_risk_nodes": risk, "settlement_note_nodes": note,
                         "refused_families": len(reg_refusals), "period_mismatch_dropped": 0},
        }
        if log:
            log.info("[TENNIS] %s %s vs %s: %d node(s) (%d risk, %d W/O-note), %d unmatched | poly=%s (%s).",
                     match.event_ticker, match.player_a, match.player_b, len(nodes), risk, note,
                     len(unmatched), "yes" if ev else "NO", method)
        return entry


def _run_family_registry(match: TennisMatch, ev: Optional[dict], *, log: Any = None
                         ) -> tuple[list, list, list]:
    """Drive the tennis family registry (total_sets), SCOPED to the total-sets markets on each venue —
    the winner path and all other markets are untouched. Returns (nodes, unmatched, refusals)."""
    from .families_tennis import is_registry_kalshi, is_registry_poly, tennis_families
    from .sports_base import run_registry
    k_reg = [m for m in match.markets if is_registry_kalshi(m)]
    p_reg = [m for m in (ev or {}).get("markets") or [] if isinstance(m, dict) and is_registry_poly(m)]
    ctx = {"player_a": match.player_a, "player_b": match.player_b, "surname": surname}
    return run_registry(tennis_families(), k_reg, p_reg, ctx=ctx, log=log, game_id=match.event_ticker)


TENNIS_SPEC = TennisSpec()
