"""the-odds-api.com supplemental odds source.

This is a SECONDARY feed that merges into the OddsPapi pipeline. Its only job is to fill
gaps OddsPapi leaves for the World Cup — recover books OddsPapi returns suspended (e.g. 1xbet)
and add soft books (bet365, unibet, williamhill, sbobet, …) — by emitting `bookmakerOdds`
fragments keyed by the SAME canonical OddsPapi fixtureId / marketId / outcomeId the engine
already uses. Once merged, the arb math, clone-dedup, staleness, and mapping-guard run unchanged
(see normalize.parse_odds_payload). Nothing here touches arbitrage.py or the OddsPapi path.

Safety posture (a mis-mapped leg = a phantom arb):
  * Fixture match requires BOTH a team-identity match AND commence_time within tolerance.
    Ambiguous or unmatched events are dropped and logged — never guessed.
  * Home/away come from the canonical fixture's p1/p2 identity, NOT the-odds-api's home_team tag.
  * Spreads: the-odds-api home-team `point` maps directly to OddsPapi's signed `handicap` (which
    is the line on outcome "1"); the away outcome must carry the exact negation or the event is
    skipped. Whether an unvalidated spread may turn actionable is gated in run.py.
  * Every the-odds-api leg has limit=None (→ low_confidence + $100 fallback) and
    changedAt = the-odds-api last_update (so the existing staleness rules apply).

Gap-fill merge: a (fixture, book) is injected ONLY when OddsPapi did not already supply that book
active for that fixture — so the two providers never collide on one slug, and provenance is
unambiguous (every injected slug on a fixture came from here).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

BASE_URL = "https://api.the-odds-api.com/v4"


# --------------------------------------------------------------------------- #
# HTTP client                                                                   #
# --------------------------------------------------------------------------- #
class TheOddsApiError(Exception):
    """Any failure talking to the-odds-api. Caught in run.py so it never breaks the OddsPapi run."""


class TheOddsApiClient:
    def __init__(self, api_key: str, timeout: float = 30.0, session: requests.Session | None = None) -> None:
        if not api_key:
            raise TheOddsApiError("ODDS_API_KEY is empty — cannot call the-odds-api.")
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self.requests_remaining: Optional[str] = None
        self.requests_used: Optional[str] = None

    def sports(self) -> Any:
        return self._get("/sports", {})

    def wc_odds(self, wc_key: str, regions: str, markets: str, odds_format: str = "decimal") -> Any:
        """Featured odds for every in-window event in `wc_key`. One HTTP call; the-odds-api bills
        it as (#markets x #regions) credits. Returns a list of event dicts."""
        return self._get(
            f"/sports/{wc_key}/odds",
            {"regions": regions, "markets": markets, "oddsFormat": odds_format, "dateFormat": "iso"},
        )

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        full = {**params, "apiKey": self.api_key}
        try:
            resp = self.session.get(BASE_URL + path, params=full, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TheOddsApiError(f"network error on {path}: {exc}") from exc
        # the-odds-api reports quota on every response via headers.
        self.requests_remaining = resp.headers.get("x-requests-remaining", self.requests_remaining)
        self.requests_used = resp.headers.get("x-requests-used", self.requests_used)
        if resp.status_code >= 400:
            raise TheOddsApiError(f"{resp.status_code} on {path}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise TheOddsApiError(f"non-JSON response on {path}: {resp.text[:200]}") from exc


# --------------------------------------------------------------------------- #
# Team-name normalization + cross-provider equivalence                          #
# --------------------------------------------------------------------------- #
# There is NO pre-existing name-normalization in this repo (OddsPapi hands back aligned IDs, so it
# never needed one). This map is built from scratch. Keys/values are POST-normalization (accent-
# stripped, lowercased, punctuation removed, spaces collapsed); both providers' spellings are
# collapsed to one canonical token so a match is identity-based, not order- or spelling-based.
_TEAM_EQUIV: dict[str, str] = {
    "turkey": "turkiye", "turkiye": "turkiye",
    "usa": "usa", "united states": "usa", "united states of america": "usa", "us": "usa",
    "ivory coast": "ivory coast", "cote divoire": "ivory coast", "cote d ivoire": "ivory coast",
    "curacao": "curacao",
    "south korea": "korea republic", "korea republic": "korea republic",
    "republic of korea": "korea republic", "korea south": "korea republic",
    "north korea": "korea dpr", "korea dpr": "korea dpr", "dpr korea": "korea dpr",
    "czechia": "czechia", "czech republic": "czechia",
    "bosnia": "bosnia", "bosnia and herzegovina": "bosnia", "bosnia herzegovina": "bosnia",
}


def normalize_team(name: Any) -> str:
    """Accent-strip, lowercase, drop punctuation, collapse whitespace, then apply the equivalence
    map. Apostrophes are deleted (so ``Côte d'Ivoire`` -> ``cote divoire``); other punctuation
    becomes a space."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))  # strip accents
    s = s.replace("'", "").replace("’", "")               # delete apostrophes
    s = "".join(c if c.isalnum() else " " for c in s)          # other punctuation -> space
    s = " ".join(s.lower().split())                            # collapse whitespace
    return _TEAM_EQUIV.get(s, s)


# --------------------------------------------------------------------------- #
# Bookmaker-key alias (the-odds-api key -> canonical OddsPapi slug)              #
# --------------------------------------------------------------------------- #
# Only books whose resulting slug is present in the OddsPapi bookmakers catalog are ingested, so
# clone-dedup and commission lookups work by slug. Unknown the-odds-api keys fall through as
# identity and are then filtered (and logged) by the catalog-membership check — extend this map
# once the first live payload shows the exact spellings.
_BOOK_ALIAS: dict[str, str] = {
    # --- confirmed present in live eu/uk World Cup payloads (keys -> OddsPapi catalog slugs) ---
    "onexbet": "1xbet", "1xbet": "1xbet",
    "pinnacle": "pinnacle",
    "bet365": "bet365",
    "williamhill": "williamhill", "william_hill": "williamhill",
    "betfair_ex_uk": "betfair-ex", "betfair_ex_eu": "betfair-ex", "betfair": "betfair-ex",
    "betfair_sb_uk": "betfair-spb",
    "unibet": "unibet", "unibet_uk": "unibet", "unibet_eu": "unibet",
    "unibet_nl": "unibet", "unibet_se": "unibet", "unibet_fr": "unibet",
    "leovegas": "leovegas", "leovegas_uk": "leovegas.uk", "leovegas_se": "leovegas",
    "betway": "betway",
    "coral": "coral",
    "ladbrokes": "ladbrokes", "ladbrokes_uk": "ladbrokes",
    "paddypower": "paddypower", "paddy_power": "paddypower",
    "boylesports": "boylesports",
    "grosvenor": "grosvenor",
    "matchbook": "matchbook",
    "betfred": "betfred", "betfred_uk": "betfred",
    # NOTE: betvictor & smarkets are GRANTED by the plan but absent from the cached /v4/bookmakers
    # catalog (Jun 11) — they stay filtered/logged until the bookmaker catalog is refreshed.
    "betvictor": "betvictor",
    "smarkets": "smarkets",
    "888sport": "888sport", "sport888": "888sport",
    "casumo": "casumo",
    "skybet": "skybet", "sky_bet": "skybet",
    "winamax_fr": "winamax.fr", "winamax_de": "winamax.de",
    # --- other common eu/uk keys (harmless if absent from a given payload) ---
    "sbobet": "sbobet",
    "betano": "betano",
    "betsson": "betsson",
    "nordicbet": "nordicbet",
    "coolbet": "coolbet",
    "betvictor": "betvictor",
    "tipico_de": "tipico",
}


def canonical_book_slug(odds_api_key: str) -> str:
    return _BOOK_ALIAS.get(odds_api_key.lower(), odds_api_key.lower())


# --------------------------------------------------------------------------- #
# Market reverse-index (family/line -> canonical marketId + outcomeIds)          #
# --------------------------------------------------------------------------- #
def _line_key(v: Any) -> Optional[float]:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


@dataclass
class MarketIndex:
    h2h: Optional[dict[str, Any]] = None                  # {marketId, home_oid, draw_oid, away_oid}
    totals: dict[float, dict[str, Any]] = field(default_factory=dict)   # line -> {marketId, over_oid, under_oid}
    spreads: dict[float, dict[str, Any]] = field(default_factory=dict)  # line -> {marketId, home_oid, away_oid}
    ambiguous: list[str] = field(default_factory=list)    # collisions we refused to index (logged)


def build_market_index(markets_json: list[dict[str, Any]], sport_id: int) -> MarketIndex:
    """Reverse-index the global markets catalog for full-time h2h / totals / spreads.

    Each (family, line) resolves to exactly one marketId with named outcomeIds. A line that maps to
    more than one marketId is recorded as ambiguous and NOT indexed (we refuse to guess)."""
    idx = MarketIndex()

    def _outs(m: dict[str, Any]) -> dict[str, int]:
        return {str(o.get("outcomeName")): int(o["outcomeId"])
                for o in (m.get("outcomes") or []) if o.get("outcomeId") is not None}

    for m in markets_json or []:
        m_sport = m.get("sportId")
        if m_sport is not None and int(m_sport) != int(sport_id):
            continue
        if (m.get("period") or "fulltime") != "fulltime":
            continue
        mtype = str(m.get("marketType") or "").lower()
        mid = m.get("marketId")
        if mid is None:
            continue
        mid = int(mid)
        outs = _outs(m)

        if mtype == "1x2" and {"1", "X", "2"} <= set(outs):
            cand = {"marketId": mid, "home_oid": outs["1"], "draw_oid": outs["X"], "away_oid": outs["2"]}
            if idx.h2h and idx.h2h["marketId"] != mid:
                idx.ambiguous.append(f"h2h: {idx.h2h['marketId']} vs {mid}")
            else:
                idx.h2h = cand

        elif mtype == "totals" and "team" not in str(m.get("marketName") or "").lower() \
                and {"Over", "Under"} <= set(outs):
            line = _line_key(m.get("handicap"))
            if line is None:
                continue
            cand = {"marketId": mid, "over_oid": outs["Over"], "under_oid": outs["Under"]}
            if line in idx.totals and idx.totals[line]["marketId"] != mid:
                idx.ambiguous.append(f"totals {line}: {idx.totals[line]['marketId']} vs {mid}")
            else:
                idx.totals[line] = cand

        elif mtype == "spreads" and {"1", "2"} <= set(outs):
            line = _line_key(m.get("handicap"))
            if line is None:
                continue
            cand = {"marketId": mid, "home_oid": outs["1"], "away_oid": outs["2"]}
            if line in idx.spreads and idx.spreads[line]["marketId"] != mid:
                idx.ambiguous.append(f"spreads {line}: {idx.spreads[line]['marketId']} vs {mid}")
            else:
                idx.spreads[line] = cand

    return idx


# --------------------------------------------------------------------------- #
# Fixture matching (team identity + commence_time)                              #
# --------------------------------------------------------------------------- #
def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class FixtureMatch:
    fixture_id: str
    home_is_p1: bool          # True: event home_team == canonical p1; False: home_team == p2
    p1_norm: str
    p2_norm: str


def match_event_to_fixture(
    home_team: str,
    away_team: str,
    commence_time: Any,
    by_fixture: dict[str, dict[str, Any]],
    tolerance_minutes: float,
) -> tuple[Optional[FixtureMatch], str]:
    """Return (match, reason). `match` is None unless EXACTLY one fixture matches on both team
    identity (unordered) and commence_time within tolerance. reason is one of:
    'ok' | 'unmatched_name' | 'time_mismatch' | 'ambiguous'."""
    h, a = normalize_team(home_team), normalize_team(away_team)
    if not h or not a:
        return None, "unmatched_name"
    ev_dt = _parse_iso(commence_time)

    name_hits: list[tuple[str, dict[str, Any], str, str]] = []
    for fid, info in by_fixture.items():
        p1, p2 = normalize_team(info.get("p1")), normalize_team(info.get("p2"))
        if {h, a} == {p1, p2} and p1 != p2:
            name_hits.append((fid, info, p1, p2))

    if not name_hits:
        return None, "unmatched_name"

    # Of the name matches, keep only those whose kickoff is within tolerance.
    time_hits = []
    for fid, info, p1, p2 in name_hits:
        f_dt = _parse_iso(info.get("start_time"))
        if ev_dt is None or f_dt is None:
            continue
        if abs((f_dt - ev_dt).total_seconds()) <= tolerance_minutes * 60.0:
            time_hits.append((fid, info, p1, p2))

    if not time_hits:
        return None, "time_mismatch"
    if len(time_hits) > 1:
        return None, "ambiguous"

    fid, info, p1, p2 = time_hits[0]
    return FixtureMatch(fixture_id=fid, home_is_p1=(h == p1), p1_norm=p1, p2_norm=p2), "ok"


# --------------------------------------------------------------------------- #
# Leg construction                                                              #
# --------------------------------------------------------------------------- #
def _player_line(price: float, last_update: Any) -> dict[str, Any]:
    """One canonical priced outcome. limit=None -> the engine marks it low_confidence and falls
    back to the $100 T_max; changedAt drives the existing kickoff-aware staleness rules."""
    return {
        "price": price,
        "priceAmerican": None,
        "limit": None,
        "changedAt": last_update,
        "mainLine": True,
        "active": True,
    }


def _empty_book_entry() -> dict[str, Any]:
    return {"bookmakerIsActive": True, "suspended": False, "markets": {}}


def _add_leg(entry: dict[str, Any], mid: int, oid: int, price: float, last_update: Any) -> None:
    mkt = entry["markets"].setdefault(str(mid), {"marketActive": True, "outcomes": {}})
    mkt["outcomes"][str(oid)] = {"players": {"0": _player_line(price, last_update)}}


def _oddspapi_has_active(book_entry: Any) -> bool:
    """True only if OddsPapi already supplied this book with a GENUINELY active price for the
    fixture (so we DEFER, not inject). A non-empty markets dict is NOT enough: OddsPapi often sends
    a suspended book (e.g. kalshi) carrying a markets structure whose outcomes are all unpriced /
    inactive. We therefore require >=1 market with >=1 active, priced (decimal > 1) outcome — the
    SAME definition normalize.parse_odds_payload uses to decide books_present (COVERAGE OK vs
    SUSPENDED). When OddsPapi's book is merely suspended, the supplemental source overrides it."""
    if not isinstance(book_entry, dict):
        return False
    if book_entry.get("bookmakerIsActive") is False or book_entry.get("suspended") is True:
        return False
    for mdata in (book_entry.get("markets") or {}).values():
        if not isinstance(mdata, dict) or mdata.get("marketActive") is False:
            continue
        for odata in (mdata.get("outcomes") or {}).values():
            if not isinstance(odata, dict):
                continue
            p = (odata.get("players") or {}).get("0")
            if not isinstance(p, dict) or p.get("active") is False:
                continue
            try:
                price = float(p.get("price"))
            except (TypeError, ValueError):
                continue
            if price > 1.0:
                return True
    return False


# --------------------------------------------------------------------------- #
# Coverage report                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Coverage:
    events_total: int = 0
    matched: int = 0
    unmatched_name: list[str] = field(default_factory=list)
    time_mismatch: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    recovered: dict[str, int] = field(default_factory=dict)      # slug -> #fixtures injected
    deferred: dict[str, int] = field(default_factory=dict)       # slug -> #fixtures OddsPapi already had
    unknown_books: dict[str, int] = field(default_factory=dict)  # the-odds-api key -> count (not in catalog)
    skipped_books: dict[str, int] = field(default_factory=dict)  # slug -> count (not in allow-list)
    cost_credits: int = 0

    def lines(self) -> list[str]:
        def topn(d: dict[str, int]) -> str:
            return ", ".join(f"{k}({v})" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "none"
        out = [
            f"THE-ODDS-API: {self.matched}/{self.events_total} events mapped "
            f"(unmatched-name {len(self.unmatched_name)}, time-mismatch {len(self.time_mismatch)}, "
            f"ambiguous {len(self.ambiguous)}) | cost {self.cost_credits} credits this poll",
            f"  recovered (book -> #fixtures): {topn(self.recovered)}",
            f"  deferred to OddsPapi (already active): {topn(self.deferred)}",
        ]
        if self.unknown_books:
            out.append(f"  books NOT in OddsPapi catalog (skipped — extend alias map): {topn(self.unknown_books)}")
        if self.skipped_books:
            out.append(f"  books not in allow-list (skipped): {topn(self.skipped_books)}")
        for label, names in (("unmatched-name", self.unmatched_name),
                             ("time-mismatch", self.time_mismatch), ("ambiguous", self.ambiguous)):
            if names:
                out.append(f"  {label}: {', '.join(names)}")
        return out


# --------------------------------------------------------------------------- #
# Merge entry point                                                             #
# --------------------------------------------------------------------------- #
def merge_into(
    raw_by_fixture: dict[str, dict[str, Any]],
    by_fixture: dict[str, dict[str, Any]],
    market_index: MarketIndex,
    catalog_slugs: set[str],
    payload: Any,
    *,
    tolerance_minutes: float,
    allow_books: Optional[set[str]],
    cost_credits: int,
    log,
) -> tuple[Coverage, dict[str, set[str]]]:
    """Merge the-odds-api events into raw_by_fixture (gap-fill) and return (coverage, toa_books).

    toa_books maps canonical fixtureId -> the set of slugs THIS source injected, so run.py can keep
    those legs out of the actionable universe (and gate unvalidated spreads) without touching the
    OddsPapi path."""
    cov = Coverage(cost_credits=cost_credits)
    toa_books: dict[str, set[str]] = {}

    events = payload if isinstance(payload, list) else []
    cov.events_total = len(events)

    for ev in events:
        if not isinstance(ev, dict):
            continue
        home, away = ev.get("home_team"), ev.get("away_team")
        label = f"{home} v {away}"
        fm, reason = match_event_to_fixture(home, away, ev.get("commence_time"), by_fixture, tolerance_minutes)
        if fm is None:
            bucket = getattr(cov, reason, None)
            if isinstance(bucket, list):
                bucket.append(label)
            continue
        cov.matched += 1
        fid = fm.fixture_id

        # Ensure a fixture envelope exists (create a minimal one if OddsPapi returned nothing for it).
        fx_raw = raw_by_fixture.get(fid)
        if fx_raw is None:
            info = by_fixture.get(fid, {})
            fx_raw = {
                "fixtureId": fid,
                "startTime": info.get("start_time"),
                "statusId": info.get("status_id"),
                "hasOdds": True,
                "bookmakerOdds": {},
            }
            raw_by_fixture[fid] = fx_raw
        book_odds = fx_raw.setdefault("bookmakerOdds", {})

        for bm in ev.get("bookmakers") or []:
            if not isinstance(bm, dict):
                continue
            slug = canonical_book_slug(str(bm.get("key") or ""))
            if slug not in catalog_slugs:
                cov.unknown_books[str(bm.get("key"))] = cov.unknown_books.get(str(bm.get("key")), 0) + 1
                continue
            if allow_books is not None and slug not in allow_books:
                cov.skipped_books[slug] = cov.skipped_books.get(slug, 0) + 1
                continue
            # Gap-fill: never override an active OddsPapi book — defer to it.
            if _oddspapi_has_active(book_odds.get(slug)):
                cov.deferred[slug] = cov.deferred.get(slug, 0) + 1
                continue

            entry = _empty_book_entry()
            legs_added = _emit_book_markets(entry, bm, fm, market_index, log, label, slug)
            if legs_added == 0:
                continue
            book_odds[slug] = entry  # overwrite any suspended OddsPapi stub with our active entry
            toa_books.setdefault(fid, set()).add(slug)
            cov.recovered[slug] = cov.recovered.get(slug, 0) + 1

    return cov, toa_books


def _emit_book_markets(
    entry: dict[str, Any], bm: dict[str, Any], fm: FixtureMatch,
    idx: MarketIndex, log, label: str, slug: str,
) -> int:
    """Translate one the-odds-api bookmaker's markets into canonical legs on `entry`. Returns the
    number of legs added. Home/away are assigned by p1/p2 identity, never by the provider's tag."""
    last_update = bm.get("last_update")
    added = 0
    for mk in bm.get("markets") or []:
        if not isinstance(mk, dict):
            continue
        key = mk.get("key")
        mk_update = mk.get("last_update", last_update)
        outcomes = mk.get("outcomes") or []

        if key == "h2h" and idx.h2h:
            added += _emit_h2h(entry, outcomes, fm, idx.h2h, mk_update)
        elif key == "totals":
            added += _emit_totals(entry, outcomes, idx, mk_update)
        elif key == "spreads":
            added += _emit_spreads(entry, outcomes, fm, idx, mk_update, log, label, slug)
    return added


def _price(o: dict[str, Any]) -> Optional[float]:
    try:
        p = float(o.get("price"))
        return p if p > 1.0 else None
    except (TypeError, ValueError):
        return None


def _emit_h2h(entry, outcomes, fm: FixtureMatch, h2h, last_update) -> int:
    added = 0
    for o in outcomes:
        name = normalize_team(o.get("name"))
        price = _price(o)
        if price is None:
            continue
        low = str(o.get("name") or "").strip().lower()
        if low == "draw":
            oid = h2h["draw_oid"]
        elif name == fm.p1_norm:
            oid = h2h["home_oid"]
        elif name == fm.p2_norm:
            oid = h2h["away_oid"]
        else:
            continue  # unrecognised outcome -> drop, never guess
        _add_leg(entry, h2h["marketId"], oid, price, last_update)
        added += 1
    return added


def _emit_totals(entry, outcomes, idx: MarketIndex, last_update) -> int:
    added = 0
    for o in outcomes:
        price = _price(o)
        line = _line_key(o.get("point"))
        if price is None or line is None:
            continue
        spec = idx.totals.get(line)
        if not spec:
            continue
        nm = str(o.get("name") or "").strip().lower()
        if nm == "over":
            oid = spec["over_oid"]
        elif nm == "under":
            oid = spec["under_oid"]
        else:
            continue
        _add_leg(entry, spec["marketId"], oid, price, last_update)
        added += 1
    return added


def _emit_spreads(entry, outcomes, fm: FixtureMatch, idx: MarketIndex, last_update, log, label, slug) -> int:
    """Map a signed spread. OddsPapi's `handicap` is the line on outcome '1' (home/p1), so the
    canonical marketId is keyed by the p1 team's point. We require the away point to be the exact
    negation; anything else is a data anomaly and the whole market is skipped (no guessing)."""
    by_team: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        n = normalize_team(o.get("name"))
        if n:
            by_team[n] = o
    p1o, p2o = by_team.get(fm.p1_norm), by_team.get(fm.p2_norm)
    if not p1o or not p2o:
        return 0
    p1_pt, p2_pt = _line_key(p1o.get("point")), _line_key(p2o.get("point"))
    p1_price, p2_price = _price(p1o), _price(p2o)
    if p1_pt is None or p2_pt is None or p1_price is None or p2_price is None:
        return 0
    if round(p1_pt + p2_pt, 2) != 0.0:
        log.warning("[THE-ODDS-API] %s %s: spread points not symmetric (%s / %s) — skipping market.",
                    label, slug, p1_pt, p2_pt)
        return 0
    spec = idx.spreads.get(p1_pt)
    if not spec:
        return 0
    _add_leg(entry, spec["marketId"], spec["home_oid"], p1_price, last_update)
    _add_leg(entry, spec["marketId"], spec["away_oid"], p2_price, last_update)
    return 2
