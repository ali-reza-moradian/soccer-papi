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
from datetime import datetime, timezone
from typing import Any, Optional

from .. import kalshi as ks
from .. import polymarket as pm
from . import config as gz_config
from . import match_rules as mr

# code (lowercase FIFA) -> canonical team name, inverted from the Polymarket source's curated map.
_CODE_TO_NAME: dict[str, str] = {code: name for name, code in pm._FIFA_CODES_RAW.items()}

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
        base = f"fifwc-{away_c}-{home_c}-{date}"
        alt = f"fifwc-{home_c}-{away_c}-{date}"
        games[suffix] = Game(game_id=suffix, kalshi_suffix=suffix, date=date, home=home, away=away,
                             kickoff_iso=kickoff, poly_base_slug=base, poly_slug_alts=[base, alt])
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
            for outcome, side in mr.kalshi_outcomes(m, game.ctx):
                options.setdefault(outcome.twin_key(), {
                    "market_type": outcome.market_type, "market_key": outcome.group,
                    "side": outcome.side, "line": outcome.line, "kind": outcome.kind,
                    "confidence": outcome.confidence, "outcome_label": outcome.label or outcome.side,
                    "kalshi_ticker": m.get("ticker"), "kalshi_side": side,
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
    for ev in game_sibling_events(series_events, game):
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
            options.setdefault(outcome.twin_key(), {
                "market_type": outcome.market_type, "market_key": outcome.group,
                "side": outcome.side, "line": outcome.line, "kind": outcome.kind,
                "confidence": outcome.confidence, "outcome_label": outcome.label or outcome.side,
                "poly_token_id": str(token), "poly_side": poly_side,
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
def join_game(k_opts: dict[str, dict], p_opts: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Join Kalshi and Polymarket options by twin_key. A key in BOTH -> a matched node carrying both
    identifiers; a key in only one -> an unmatched entry (for visibility)."""
    nodes: list[dict] = []
    for key in sorted(set(k_opts) | set(p_opts)):
        k, p = k_opts.get(key), p_opts.get(key)
        if k and p:
            nodes.append({
                "twin_key": key,
                "market_type": k["market_type"], "market_key": k["market_key"], "side": k["side"],
                "outcome_label": k.get("outcome_label") or p.get("outcome_label"),
                "line": k["line"], "kind": k["kind"], "confidence": k["confidence"],
                "kalshi_ticker": k["kalshi_ticker"], "kalshi_side": k["kalshi_side"],
                "poly_token_id": p["poly_token_id"], "poly_side": p["poly_side"],
            })
    unmatched: list[dict] = []
    for key in sorted(set(k_opts) - set(p_opts)):
        o = k_opts[key]
        unmatched.append({"venue": "kalshi", "market_type": o["market_type"],
                          "outcome_label": o.get("outcome_label"), "identifier": o.get("kalshi_ticker")})
    for key in sorted(set(p_opts) - set(k_opts)):
        o = p_opts[key]
        unmatched.append({"venue": "polymarket", "market_type": o["market_type"],
                          "outcome_label": o.get("outcome_label"), "identifier": o.get("poly_token_id")})
    return nodes, unmatched


def build_tree(kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
               now: Optional[datetime] = None, log: Any = None) -> dict[str, Any]:
    """Discover games and build the full match tree dict (does not write). Pure-ish: all network goes
    through the injected clients, so tests pass fakes."""
    now = now or datetime.now(timezone.utc)
    games = discover_games(kalshi_client, now=now, lookahead_hours=cfg.lookahead_hours, log=log)
    # Enumerate the whole soccer-fifwc series ONCE; each game's events are filtered out by slug prefix.
    series_events = fetch_series_events(poly_client, cfg.poly_series_slug, log)
    tree: dict[str, Any] = {"games": {}}
    for game in games:
        # A PARTIAL tree is fine: an unexpected error on one game must not kill the rest of the build.
        try:
            kickoff = _game_kickoff(game, series_events)   # PRECISE Poly kickoff, else provisional noon
            k_opts, k_cov = kalshi_options(kalshi_client, game, cfg.kalshi_series, log)
            p_opts, p_cov = poly_options(series_events, game, log)
            nodes, unmatched = join_game(k_opts, p_opts)
        except Exception as exc:  # noqa: BLE001 - never abort the whole build for one game
            if log:
                log.warning("[GENZ] %s build failed: %s — skipping this game.", game.game_id, exc)
            continue
        tree["games"][game.game_id] = {
            "kalshi_suffix": game.kalshi_suffix, "poly_base_slug": game.poly_base_slug,
            "home": game.home, "away": game.away, "date": game.date, "kickoff_utc": kickoff,
            "nodes": nodes, "unmatched": unmatched,
            # coverage: how many series/siblings succeeded vs failed this build (dashboard shows gaps).
            "coverage": {"kalshi_ok": k_cov["ok"], "kalshi_failed": k_cov["failed"],
                         "poly_ok": p_cov["ok"], "poly_failed": p_cov["failed"]},
        }
        if log:
            two_way = sum(1 for n in nodes if n["kind"] == "2way")
            log.info("[GENZ] %s %s vs %s: %d matched node(s) (%d 2-way), %d unmatched | "
                     "kalshi %d ok/%d failed, poly %d ok/%d failed.",
                     game.game_id, game.away, game.home, len(nodes), two_way, len(unmatched),
                     k_cov["ok"], len(k_cov["failed"]), p_cov["ok"], len(p_cov["failed"]))
    return tree


def build_meta(tree: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    games = tree.get("games", {})
    kickoffs = sorted((g.get("kickoff_utc", "") for g in games.values()) if games else [])
    # Per-game coverage so the dashboard can show which market types failed to fetch this build.
    coverage = {gid: g.get("coverage", {}) for gid, g in games.items()}
    any_failed = sorted({s for g in games.values()
                         for s in (g.get("coverage", {}).get("kalshi_failed", []))})
    return {"built_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "games": sorted(games.keys()), "next_kickoffs": kickoffs[:10],
            "coverage": coverage, "kalshi_series_failed_any_game": any_failed}


def write_tree(tree: dict[str, Any], *, now: Optional[datetime] = None,
               tree_path: Optional[str] = None, meta_path: Optional[str] = None) -> tuple[str, str]:
    """Persist match_tree.json + tree_meta.json under data/genz/. Returns the two paths written."""
    now = now or datetime.now(timezone.utc)
    gz_config.ensure_dirs()
    tp = tree_path or gz_config.MATCH_TREE_PATH
    mp = meta_path or gz_config.TREE_META_PATH
    with open(tp, "w", encoding="utf-8") as fh:
        json.dump(tree, fh, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump(build_meta(tree, now=now), fh, ensure_ascii=False, indent=2)
    return tp, mp


def load_tree(tree_path: Optional[str] = None) -> dict[str, Any]:
    """Load match_tree.json (the engine reads this every cycle). {} if absent."""
    tp = tree_path or gz_config.MATCH_TREE_PATH
    if not os.path.exists(tp):
        return {"games": {}}
    with open(tp, "r", encoding="utf-8") as fh:
        return json.load(fh)
