"""UFC sport adapter for the GenZ tree builder (a FOURTH sport; soccer + MLB + tennis untouched).

Discovers UFC fights on Kalshi (series KXUFCFIGHT, one event per fight holding two per-fighter YES/NO
markets), pairs each to its Polymarket fight event, and emits the engine's tree-node shape. ONE market
family in v1 (deliberate, same rationale as tennis):

    fight_winner   2-way MECE. twin_key fight_winner|<surname>.

WHY ONLY THE WINNER (the corners lesson): Poly's method/round/distance markets (KO/TKO, submission,
Goes-the-Distance, O/U rounds) resolve on a DIFFERENT event than the winner, and a draw/No-Contest
settles them inconsistently across venues, so cross-venue method/round is NOT the same bet. The fight
WINNER is the only proven-same bet: both venues pay the fighter OFFICIALLY declared the winner.
Everything else is left UNPAIRED and logged as a per-build INVENTORY line (evidence for a future
family #2).

THE DRAW/NC GUARD (UFC's walkover): a fight can settle DIFFERENTLY across venues on a draw, No Contest,
or a pre-event CANCEL. On the WINNER rule both venues pay the officially-declared winner — aligned. On
a CANCEL Poly resolves 50-50 while Kalshi refunds to a fair/last price (Kalshi also does 50/50 for a
tie/NC, but the CANCEL facet is the material divergence the cap addresses). That divergence is EXPECTED
and INFORMATIONAL (settlement_note="dnc_50_50", amber DRAW/NC chip) — NOT excluded (draws/NCs are ~1%
and cancels are pre-event, a near-wash under the poly-leg cap). A winner-rule divergence (a venue that
settles the winner on price/points, not the official decision) is a REAL divergence -> refuse the pair.
An UNPARSEABLE resolution text on either side is conservatively flagged settlement_risk (excluded),
exactly like MLB/tennis.

Live facts (verified 2026-07): Kalshi KXUFCFIGHT-<YY><MON><DD><FRAGS> (FRAGS concat surname fragments,
AMBIGUOUS -> the TITLE 'Will <Fighter> win the <Surname> vs <Surname> professional MMA fight ...' is the
source of truth). Each event = 2 markets (yes_sub_title = full fighter name). Poly event slug
ufc-<f1>-<f2>-<YYYY-MM-DD> where f* are FIRST-name fragments with ARBITRARY disambiguation digits
(ufc-kam-dri-…, ufc-jar-chr20-…, ufc-dander-eellio-…) — UNDERIVABLE, so pairing NEVER constructs a slug:
it SCANS the gamma ufc window and matches by FULL-NAME TOKEN SETS from titles (accent/case-insensitive,
containment both ways, multi-word surnames intact) + date ±1. Fight times are card estimates that slide
hours, so pairing NEVER refuses on a start-time delta.
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


# --------------------------------------------------------------------------- #
# Name normalization (accents, surnames, token sets) — self-contained per sport   #
# --------------------------------------------------------------------------- #
def _strip_accents(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(ch))


def _norm(s: str) -> str:
    """Lowercase, accent-stripped, single-spaced alphanumerics — for tolerant name matching."""
    t = re.sub(r"[^a-z0-9 ]", " ", _strip_accents(s).lower())
    return re.sub(r"\s+", " ", t).strip()


def name_tokens(full: str) -> frozenset:
    """The normalized word tokens of a fighter name — for token-set (containment) matching across
    venues (resolves a dropped/added middle name or a nickname, e.g. 'Christian Duncan' vs
    'Christian Leroy Duncan')."""
    return frozenset(t for t in _norm(full).split() if t)


# Surname particles that belong WITH the final token ('Dricus Du Plessis' -> 'du plessis',
# 'Jack Della Maddalena' -> 'della maddalena').
_PARTICLES = frozenset({"du", "de", "del", "della", "di", "da", "van", "von", "der", "den", "la", "le",
                        "el", "al", "bin", "ibn", "dos", "das", "mc", "mac", "dos", "st"})


def surname(full: str) -> str:
    """The surname of a full name, NORMALIZED, extended backwards over known particles — the twin_key
    side + token-match key, so both venues produce the same key for the same fighter."""
    toks = str(full or "").split()
    if not toks:
        return ""
    i = len(toks) - 1
    while i > 0 and _norm(toks[i - 1]) in _PARTICLES:
        i -= 1
    return _norm(" ".join(toks[i:]))


# --------------------------------------------------------------------------- #
# Settlement rule parsing (the draw/NC guard) — from the REAL captured texts      #
# --------------------------------------------------------------------------- #
# WINNER rule = how a normally-decided fight settles.
_WINNER_OFFICIAL = ("officially declared the winner", "declared the winner", "official winner",
                    "wins the")
# a venue that settles the WINNER on price/points (not the official decision) is a REAL divergence
_WINNER_LAST_PRICE = ("no official winner", "winner determined by", "settled at the last",
                      "resolves to the last traded price")
# DRAW / NO-CONTEST / CANCEL rule.
_DNC_5050 = ("50-50", "50/50", "fifty-fifty", "fifty fifty")
_DNC_PRICE = ("fair price", "last traded price", "last fair market price", "last price")
_DNC_VOID = ("market will be voided", "resolve to void", "declared void")
_DNC_CTX = ("draw", "technical draw", "no contest", "not scored", "cancel", "postpon", "rescheduled",
            "tie or no contest", "voided")
_CANCEL_CTX = ("cancel", "postpon", "rescheduled", "not take place", "does not occur")


def parse_ufc_rules(text: str) -> dict:
    """Classify a resolution text into its two facets:
        winner_rule in {'official_winner', 'last_price_winner', None}
        dnc_rule    in {'fifty_fifty', 'last_price', 'void', None}
    The dnc facet prioritizes the CANCEL handling (the material cross-venue divergence): a venue that
    refunds a pre-event cancel to a fair/last price is 'last_price' even if it 50/50's a tie/NC."""
    low = str(text or "").lower()
    # WINNER facet — a price/negation phrase ('no official winner', 'winner determined by the last ...')
    # is checked FIRST so it isn't shadowed by the 'official winner' substring inside it.
    if any(p in low for p in _WINNER_LAST_PRICE):
        winner = "last_price_winner"
    elif any(p in low for p in _WINNER_OFFICIAL):
        winner = "official_winner"
    else:
        winner = None
    # DNC facet.
    has_ctx = any(c in low for c in _DNC_CTX)
    cancel_ctx = any(c in low for c in _CANCEL_CTX)
    if any(v in low for v in _DNC_VOID):
        dnc = "void"
    elif cancel_ctx and any(p in low for p in _DNC_PRICE):
        dnc = "last_price"                                  # cancel refunds to a fair/last price
    elif has_ctx and any(p in low for p in _DNC_5050):
        dnc = "fifty_fifty"
    elif has_ctx and any(p in low for p in _DNC_PRICE):
        dnc = "last_price"
    else:
        dnc = None
    return {"winner_rule": winner, "dnc_rule": dnc}


# --------------------------------------------------------------------------- #
# Discovery                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class UFCFight:
    event_ticker: str            # KXUFCFIGHT-<suffix> (unique per fight)
    date: str                    # YYYY-MM-DD (Kalshi ticker date; DISPLAY schedule, may slide)
    fighter_a: str               # full names (title order)
    fighter_b: str
    kickoff_iso: str             # provisional; refined from Poly startTime when paired
    card: str = ""               # Poly card name ('UFC Fight Night: ...'), filled when paired
    markets: list = field(default_factory=list)   # the 1-or-2 Kalshi per-fighter markets
    poly_base_slug: str = ""

    @property
    def game_id(self) -> str:
        return self.event_ticker


_SUFFIX_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})([A-Z0-9]+)$")   # date + a variable fragment concat
_TITLE_VS_RE = re.compile(r"\bvs\.?\b", re.IGNORECASE)
_PARENS_RE = re.compile(r"\(.*?\)")


def _decode_date(suffix: str) -> Optional[str]:
    m = _SUFFIX_RE.match(suffix or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).title(), "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _names_from_title(title: str) -> Optional[tuple[str, str]]:
    """('Fighter A', 'Fighter B') from a Kalshi UFC title ('Will X win the A vs B professional MMA
    fight ...') OR a bare 'A vs B'. Multi-word surnames (Du Plessis) are preserved."""
    t = str(title or "")
    m = re.search(r"win the (.+?)\s+professional\s+(?:mma\s+)?fight", t, re.IGNORECASE)
    core = m.group(1) if m else t
    core = _PARENS_RE.sub("", core).strip()
    parts = _TITLE_VS_RE.split(core, maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(" .:-"), parts[1].strip(" .:-")
    return (a, b) if a and b else None


def _suffix_of(m: dict) -> str:
    et = str(m.get("event_ticker") or "")
    return et.split("-", 1)[1] if "-" in et else et


def _enrich_full_names(title_names: tuple, mkts: list[dict]) -> tuple:
    """Upgrade the title's surname pair to FULL names using each market's yes_sub_title (matched by
    surname), keeping the title's A-vs-B order. Falls back to the title surname when no market matches."""
    fulls = [str(m.get("yes_sub_title") or "").strip() for m in mkts if m.get("yes_sub_title")]
    out = []
    for tn in title_names:
        sn = surname(tn)
        full = next((f for f in fulls if surname(f) == sn), None)
        out.append(full or tn)
    return tuple(out)


def discover_ufc_fights(kalshi_client: Any, *, now: datetime, lookahead_hours: float,
                        series: list[str], log: Any = None) -> list[UFCFight]:
    """Every open KXUFCFIGHT event within [now-12h, now+lookahead]. Names come from the market TITLES
    (yes_sub_title gives one fighter each; the title carries the 'A vs B' order). Logs the observed
    Kalshi market shape (single vs two per fighter). An event whose title won't parse is skipped."""
    by_event: dict[str, list[dict]] = {}
    for s in series:
        try:
            for m in kalshi_client.iter_markets(series_ticker=s, status="open"):
                if isinstance(m, dict):
                    by_event.setdefault(str(m.get("event_ticker") or ""), []).append(m)
        except Exception as exc:  # noqa: BLE001 - a failed series fetch must not abort the build
            if log:
                log.warning("[UFC] %s discovery failed: %s — no fights this build.", s, exc)

    horizon = now.timestamp() + lookahead_hours * 3600.0
    out: list[UFCFight] = []
    shapes: dict[int, int] = {}
    for et, mkts in by_event.items():
        suffix = _suffix_of(mkts[0])
        date = _decode_date(suffix)
        names = None
        for m in mkts:                                     # the title fixes the A-vs-B ORDER
            names = _names_from_title(m.get("title"))
            if names:
                break
        if not date or not names:
            if log:
                log.warning("[UFC] unparseable event %s (title=%r) — skipping.",
                            et, (mkts[0].get("title") if mkts else ""))
            continue
        names = _enrich_full_names(names, mkts)
        kickoff = f"{date}T12:00:00Z"                       # provisional; refined from Poly startTime
        ts = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp()
        if ts > horizon or ts < now.timestamp() - 12 * 3600.0:
            continue
        shapes[len(mkts)] = shapes.get(len(mkts), 0) + 1
        out.append(UFCFight(event_ticker=et, date=date, fighter_a=names[0], fighter_b=names[1],
                            kickoff_iso=kickoff, markets=list(mkts)))
    if log and shapes:
        log.info("[UFC] discovered %d fight(s); kalshi market shape (markets/event -> count): %s",
                 len(out), dict(sorted(shapes.items())))
    return out


# --------------------------------------------------------------------------- #
# Polymarket event resolution — SCAN ONLY (slugs are underivable digit-suffixed)  #
# --------------------------------------------------------------------------- #
def _parse_iso(v: Any) -> Optional[float]:
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def _dates_pm1(date: str) -> list[str]:
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return [date]
    return [date, (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()]


def _same_fighter(a: str, b: str) -> bool:
    """Token containment BOTH ways — tolerates a nickname/middle name and is accent-insensitive."""
    ta, tb = name_tokens(a), name_tokens(b)
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def _event_fighters(ev: dict) -> Optional[tuple[str, str]]:
    """The two fighter full names from a Poly UFC event title ('UFC Fight Night: A vs. B (weight,card)')."""
    title = str(ev.get("title") or "")
    core = title.split(":", 1)[1] if ":" in title else title
    core = _PARENS_RE.sub("", core)
    parts = _TITLE_VS_RE.split(core, maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(" .:-"), parts[1].strip(" .:-")
    return (a, b) if a and b else None


def resolve_poly_event(fight: UFCFight, series_events: list[dict], log: Any = None
                       ) -> tuple[Optional[dict], str]:
    """The Polymarket fight event for this Kalshi fight, and the METHOD ('scan' | 'none'). UFC slugs are
    FIRST-name fragments with arbitrary digits (underivable), so this NEVER constructs a slug — it scans
    the pre-fetched ufc window for an event whose two title fighters TOKEN-MATCH ours within date ±1.
    Start time is DISPLAY ONLY (card estimates slide). Returns (event|None, method)."""
    want_dates = set(_dates_pm1(fight.date))
    best = None
    for e in series_events or []:
        if not isinstance(e, dict):
            continue
        edate = str(e.get("startTime") or "")[:10] or str(e.get("slug") or "")[-10:]
        if edate not in want_dates:
            continue
        fighters = _event_fighters(e)
        if not fighters:
            continue
        pa, pb = fighters
        if ((_same_fighter(fight.fighter_a, pa) and _same_fighter(fight.fighter_b, pb))
                or (_same_fighter(fight.fighter_a, pb) and _same_fighter(fight.fighter_b, pa))):
            best = e
            break
    if best is not None:
        fight.poly_base_slug = str(best.get("slug") or "")
        fight.card = str(best.get("title") or "").split(":", 1)[0].strip() if ":" in str(best.get("title") or "") else ""
        if log:
            log.info("[UFC] %s %s vs %s: matched via SCAN -> %s (score=2/2 tokens).",
                     fight.event_ticker, fight.fighter_a, fight.fighter_b, fight.poly_base_slug)
        return best, "scan"
    if log:
        log.info("[UFC] %s %s vs %s: NO Poly event in the ufc window (date ±1) — one-sided.",
                 fight.event_ticker, fight.fighter_a, fight.fighter_b)
    return None, "none"


# --------------------------------------------------------------------------- #
# Options (fight winner) — IDENTITY BEFORE SHAPE                                  #
# --------------------------------------------------------------------------- #
_KALSHI_TEXT_KEYS = ("rules_primary", "rules_secondary", "subtitle", "title")

# EXCLUDE any Poly sub-market whose title/question names a method/round/distance family (the
# Goes-the-Distance / KO-TKO / submission / round-total shape-alikes) — never the fight winner.
_UFC_EXCLUDE_RE = re.compile(
    r"distance|\bko\b|tko|submission|decision|method|round|points|scorecard|finish|first",
    re.IGNORECASE)


def _kalshi_text(m: dict) -> str:
    return " ".join(str(m.get(k) or "") for k in _KALSHI_TEXT_KEYS)


def kalshi_ufc_options(fight: UFCFight) -> tuple[dict[str, dict], list[dict]]:
    """({twin_key: option}, inventory) for the Kalshi side — the fight-winner is one twin_key per fighter
    (best-of-both). A market whose fighter can't be resolved from the title goes to the inventory."""
    opts: dict[str, dict] = {}
    inv: list[dict] = []
    for m in fight.markets:
        fighter = str(m.get("yes_sub_title") or "").strip()
        sn = surname(fighter)
        if not sn or not _match_fighter(fighter, fight):
            inv.append({"venue": "kalshi", "title": m.get("title"), "market_type": "other"})
            continue
        opts[f"fight_winner|{sn}"] = {
            "market_type": "fight_winner", "market_key": "fight_winner", "side": sn, "line": None,
            "kind": "2way", "confidence": "high", "outcome_label": fighter,
            "kalshi_ticker": m.get("ticker"), "kalshi_side": "YES", "kalshi_text": _kalshi_text(m),
        }
    return opts, inv


def _match_fighter(name: str, fight: UFCFight) -> bool:
    return _same_fighter(name, fight.fighter_a) or _same_fighter(name, fight.fighter_b)


def poly_ufc_options(event: dict, fight: UFCFight, *, log: Any = None,
                     game_id: str = "") -> tuple[dict[str, dict], list[dict]]:
    """({twin_key: option}, inventory) for the Poly side. IDENTITY BEFORE SHAPE: the fight-winner is the
    market whose slug == the event slug AND whose two outcomes are our fighters' full names AND whose
    question does NOT match the method/round/distance exclusion regex. Every other market (Goes-the-
    Distance, KO/TKO, submission, round totals) is inventoried, never paired (each exclusion logged once)."""
    opts: dict[str, dict] = {}
    inv: list[dict] = []
    if not isinstance(event, dict):
        return opts, inv
    eslug = str(event.get("slug") or "")
    logged: set = set()
    for m in event.get("markets") or []:
        if not isinstance(m, dict):
            continue
        mslug = str(m.get("slug") or "")
        qtext = f"{m.get('question') or ''} {m.get('groupItemTitle') or ''}"
        outs = pm._as_list(m.get("outcomes"))
        toks = pm._as_list(m.get("clobTokenIds"))
        excluded = bool(_UFC_EXCLUDE_RE.search(qtext))
        is_winner = (mslug == eslug and not excluded and len(outs) == 2 and len(toks) >= 2
                     and _fighters_are_ours(outs, fight))
        if not is_winner:
            if excluded and log and qtext not in logged:
                logged.add(qtext)
                log.info("[UFC] excluded sub-market: %s", (m.get("question") or m.get("groupItemTitle")))
            inv.append({"venue": "polymarket", "title": m.get("question") or m.get("groupItemTitle"),
                        "market_type": "other"})
            continue
        fee = _poly_market_fee(m)
        desc = str(m.get("description") or event.get("description") or "")
        for tok, out in zip(toks, outs):
            sn = _our_surname(out, fight)
            if not sn:
                continue
            opts[f"fight_winner|{sn}"] = {
                "market_type": "fight_winner", "market_key": "fight_winner", "side": sn, "line": None,
                "kind": "2way", "confidence": "high", "outcome_label": str(out),
                "poly_token_id": str(tok), "poly_side": str(out), "poly_fee_enabled": fee["enabled"],
                "poly_fee_rate": fee["rate"], "poly_fee_taker_only": fee["taker_only"], "poly_text": desc}
    return opts, inv


def _fighters_are_ours(outcomes: list, fight: UFCFight) -> bool:
    got = {_our_surname(o, fight) for o in outcomes}
    got.discard("")
    return len(got) == 2


def _our_surname(name: str, fight: UFCFight) -> str:
    """The twin_key surname for a Poly outcome name, IF it is one of our two fighters. Keyed off the
    outcome's OWN surname (both venues carry the full fighter name, so surname('Dricus Du Plessis') is
    'du plessis' on both) — robust to a truncated discovery-side name."""
    return surname(name) if _match_fighter(name, fight) else ""


# --------------------------------------------------------------------------- #
# Join + draw/NC guard                                                           #
# --------------------------------------------------------------------------- #
def join_ufc(k_opts: dict[str, dict], p_opts: dict[str, dict], *, log: Any = None,
             game_id: str = "") -> tuple[list[dict], list[dict]]:
    """Pair Kalshi<->Poly fight_winner options by twin_key. Each node gets the DRAW/NC GUARD (both
    venues' settlement_texts + facet classification). Unpaired/refused keys -> unmatched."""
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
        keep = _apply_dnc_guard(node, k.get("kalshi_text", ""), p.get("poly_text", ""), log, game_id)
        (nodes.append(node) if keep else refused.add(key))
    unmatched: list[dict] = []
    for key in sorted((set(k_opts) - set(p_opts)) | refused):
        o = k_opts.get(key)
        if o:
            unmatched.append({"venue": "kalshi", "market_type": o["market_type"],
                              "outcome_label": o.get("outcome_label"), "identifier": o.get("kalshi_ticker"),
                              "reason": "winner_rule_divergence" if key in refused else "one_venue_only"})
    for key in sorted((set(p_opts) - set(k_opts)) | refused):
        o = p_opts.get(key)
        if o:
            unmatched.append({"venue": "polymarket", "market_type": o["market_type"],
                              "outcome_label": o.get("outcome_label"), "identifier": o.get("poly_token_id"),
                              "reason": "winner_rule_divergence" if key in refused else "one_venue_only"})
    return nodes, unmatched


def _apply_dnc_guard(node: dict, kalshi_text: str, poly_text: str, log: Any, game_id: str) -> bool:
    """Classify a fight_winner node. Returns True to KEEP (paired), False to REFUSE.
      - winner_rule NOT in {official_winner, None} on either side -> REFUSE (a real divergence).
      - dnc_rule unparseable on a side -> settlement_risk (conservative, excluded), KEEP-flagged.
      - dnc_rule divergence (fifty_fifty vs last_price/void) -> settlement_note='dnc_50_50' (INFO), KEEP.
    Both venues' raw texts are always stored for the row expando."""
    kr = parse_ufc_rules(kalshi_text)
    pr = parse_ufc_rules(poly_text)
    node["settlement_texts"] = {"kalshi": kalshi_text[:600], "poly": poly_text[:600]}
    node["kalshi_rule"] = kr
    node["poly_rule"] = pr
    if kr["winner_rule"] == "last_price_winner" or pr["winner_rule"] == "last_price_winner":
        if log:
            log.warning("[UFC] %s %s: winner_rule divergence (kalshi=%s poly=%s) — REFUSED pairing.",
                        game_id, node["market_key"], kr["winner_rule"], pr["winner_rule"])
        return False
    if kr["dnc_rule"] is None or pr["dnc_rule"] is None:
        node["settlement_risk"] = "ufc_unparsed_settlement"   # conservative: can't tell -> excluded
        if log:
            log.warning("[UFC] %s %s: unparseable draw/NC rule (kalshi=%s poly=%s) — settlement_risk.",
                        game_id, node["twin_key"], kr["dnc_rule"], pr["dnc_rule"])
        return True
    if kr["dnc_rule"] != pr["dnc_rule"]:
        node["settlement_note"] = "dnc_50_50"                 # EXPECTED, informational (amber chip)
        if log:
            log.info("[UFC] %s %s: draw/NC 50-50 note (kalshi=%s poly=%s) — informational, tradable.",
                     game_id, node["twin_key"], kr["dnc_rule"], pr["dnc_rule"])
    return True


# --------------------------------------------------------------------------- #
# Inventory (unpaired families -> a per-build evidence line)                      #
# --------------------------------------------------------------------------- #
def _inventory_line(fight: UFCFight, k_inv: list, p_inv: list) -> str:
    def tally(inv):
        titles = [str(x.get("title") or "").strip() for x in inv if x.get("title")]
        head = "; ".join(list(dict.fromkeys(titles))[:6])
        return len(inv), head
    kn, kh = tally(k_inv)
    pn, ph = tally(p_inv)
    return (f"[UFC][INVENTORY] {fight.event_ticker} {fight.fighter_a} vs {fight.fighter_b}: "
            f"kalshi {kn} other market(s) [{kh}] | poly {pn} other market(s) [{ph}]")


# --------------------------------------------------------------------------- #
# The UFC SportSpec                                                              #
# --------------------------------------------------------------------------- #
class UFCSpec:
    """The UFC adapter consumed by tree_builder.build_tree(spec=UFC_SPEC)."""

    name = "ufc"

    def paths(self) -> gz_config.SportPaths:
        return gz_config.paths_for_sport("ufc")

    def game_id(self, fight: UFCFight) -> str:
        return fight.game_id

    def discover_games(self, kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
                       now: datetime, log: Any = None) -> tuple[list[UFCFight], dict[str, Any]]:
        fights = discover_ufc_fights(kalshi_client, now=now, lookahead_hours=cfg.lookahead_hours,
                                     series=list(cfg.kalshi_series), log=log)
        poly_sport = getattr(cfg, "poly_sport", "ufc")
        try:
            series_events = poly_client.events_by_series(poly_sport, closed=False)
        except Exception as exc:  # noqa: BLE001
            if log:
                log.warning("[UFC] poly %s fetch failed: %s — no Poly pairing this build.", poly_sport, exc)
            series_events = []
        return fights, {"series_events": series_events}

    def pair_markets(self, kalshi_client: Any, poly_client: Any, fight: UFCFight, poly_ctx: dict,
                     cfg: gz_config.GenzConfig, *, log: Any = None) -> dict[str, Any]:
        series_events = poly_ctx.get("series_events") or []
        ev, method = resolve_poly_event(fight, series_events, log)
        k_opts, k_inv = kalshi_ufc_options(fight)
        if ev:
            p_opts, p_inv = poly_ufc_options(ev, fight, log=log, game_id=fight.event_ticker)
        else:
            p_opts, p_inv = {}, []
        kickoff = str((ev or {}).get("startTime") or "") or fight.kickoff_iso
        if not _parse_iso(kickoff):
            kickoff = fight.kickoff_iso
        nodes, unmatched = join_ufc(k_opts, p_opts, log=log, game_id=fight.event_ticker)
        risk = sum(1 for n in nodes if n.get("settlement_risk"))
        note = sum(1 for n in nodes if n.get("settlement_note"))
        if log:
            log.info(_inventory_line(fight, k_inv, p_inv))
        entry = {
            "kalshi_suffix": _suffix_of(fight.markets[0]) if fight.markets else "",
            "poly_base_slug": fight.poly_base_slug, "card": fight.card,
            "away": fight.fighter_a, "home": fight.fighter_b, "date": fight.date,
            "kickoff_utc": kickoff, "sport": "ufc", "poly_match_method": method,
            "nodes": nodes, "unmatched": unmatched,
            "coverage": {"kalshi_ok": 1, "kalshi_failed": [], "poly_ok": 1 if ev else 0,
                         "poly_failed": [] if ev else [fight.poly_base_slug or fight.event_ticker],
                         "settlement_risk_nodes": risk, "settlement_note_nodes": note,
                         "period_mismatch_dropped": 0},
        }
        if log:
            log.info("[UFC] %s %s vs %s: %d node(s) (%d risk, %d DNC-note), %d unmatched | poly=%s (%s).",
                     fight.event_ticker, fight.fighter_a, fight.fighter_b, len(nodes), risk, note,
                     len(unmatched), "yes" if ev else "NO", method)
        return entry


UFC_SPEC = UFCSpec()
