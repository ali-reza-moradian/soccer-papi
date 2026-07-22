"""The SPORT ADAPTER seam for the GenZ tree builder.

`build_tree` (tree_builder.py) is sport-agnostic: it asks a :class:`SportSpec` to (1) discover the
games in the window plus whatever per-build Polymarket context it needs, and (2) turn each game into
one tree entry (its paired nodes + unmatched list + coverage/meta). Soccer's spec (SoccerSpec, in
tree_builder.py) wraps the EXISTING functions verbatim so its output is byte-for-byte unchanged;
MLBSpec (sports_mlb.py) implements the same three methods against the MLB market shapes.

A tree entry (the value stored at tree['games'][game_id]) is the dict the engine's collect_markets
consumes: it MUST carry at least {home, away, kickoff_utc, nodes, unmatched}. Each node is the
existing tree-node shape (market_type, market_key, side, line, kind, confidence, kalshi_ticker,
kalshi_side, poly_token_id, poly_side, + optional poly_fee_*/settlement fields).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from . import config as gz_config

# SYSTEMIC PAIRING ALARM (all sports). A silent 39/39-games-with-zero-nodes build is almost always a
# venue FORMAT DRIFT (a title/slug shape changed), not a real "nothing paired today". Below these
# thresholds the build writes a systemic_alert into <sport>_tree_meta.json + logs a loud WARNING so it
# can never pass silently again.
SYSTEMIC_MIN_GAMES = 5
SYSTEMIC_MIN_PAIRED_SHARE = 0.20
SYSTEMIC_MIN_PAIRED_GAMES = 5      # BROKEN also requires paired < this, so a big slate with >= 5 paired
                                   # games is NEVER "broken" (e.g. 18/92 = 19.6% share but 18 pairs is fine).


def _unmatched_samples(group: list, limit: int = 6) -> list:
    samples: list = []
    for gid, g in group:
        if g.get("nodes"):
            continue
        label = f"{g.get('away', '?')} vs {g.get('home', '?')}"
        samples.append({"game": gid, "label": label,
                        "tokens": sorted(set(re.findall(r"[a-z0-9]+", label.lower())))})
        if len(samples) >= limit:
            break
    return samples


def pairing_alert(tree: dict, sport: str = "?") -> Optional[dict]:
    """PER-COMPETITION systemic pairing alarm. A competition is BROKEN (red) only when BOTH < 20% of its
    games paired AND fewer than SYSTEMIC_MIN_PAIRED_GAMES (5) paired — i.e. a genuine format-drift failure,
    not a large slate that just runs many one-sided games. A competition whose unpaired games are simply
    ABSENT on one venue (coverage.poly_absent -> Kalshi-only, e.g. friendlies) is reported as a grey
    'N one-sided on <venue>' note, NEVER red. Sports without competitions (mlb/tennis/ufc) evaluate as one
    'all' group. Returns {sport, broken:[...], one_sided:[...]} or None when every competition is healthy."""
    games = tree.get("games", {}) or {}
    if len(games) < SYSTEMIC_MIN_GAMES:
        return None
    groups: dict = {}
    for gid, g in games.items():
        groups.setdefault(g.get("competition") or "all", []).append((gid, g))
    broken: list = []
    one_sided: list = []
    for comp, group in sorted(groups.items()):
        total_c = len(group)
        if total_c < SYSTEMIC_MIN_GAMES:                     # too small a competition to judge
            continue
        paired_c = sum(1 for _, g in group if g.get("nodes"))
        share_c = paired_c / total_c
        if share_c >= SYSTEMIC_MIN_PAIRED_SHARE or paired_c >= SYSTEMIC_MIN_PAIRED_GAMES:
            continue                                         # healthy on EITHER gate -> not broken
        # Below BOTH thresholds. One-sided (a venue simply doesn't carry it) is normal, not a break.
        poly_absent = sum(1 for _, g in group
                          if not g.get("nodes") and (g.get("coverage") or {}).get("poly_absent"))
        genuine = sum(1 for _, g in group
                      if not g.get("nodes") and not (g.get("coverage") or {}).get("poly_absent"))
        if genuine == 0 and poly_absent > 0:
            one_sided.append({"competition": comp, "count": poly_absent, "venue": "Kalshi"})
        else:
            broken.append({"competition": comp, "share": round(share_c, 4), "paired": paired_c,
                           "total": total_c, "sample_unmatched_tokens": _unmatched_samples(group)})
    if not broken and not one_sided:
        return None
    return {"sport": sport, "broken": broken, "one_sided": one_sided}


# --------------------------------------------------------------------------- #
# FAMILY REGISTRY — evidence-first, per-sport market-family pairing               #
# --------------------------------------------------------------------------- #
# A market FAMILY (moneyline, total_runs, go_the_distance, method-by-KO, total_sets, ...) is claimed by
# a FamilySpec whose selectors recognize its markets on each venue from the REAL texts. The builder
# emits paired tree nodes, per-family REFUSALS (a definition mismatch that would lose both legs — the
# corners-class trap, e.g. Kalshi 'KO/TKO/DQ' vs Poly 'KO or TKO'), or INVENTORY (one venue only /
# incompatible convention). A market no FamilySpec claims -> inventory reason='no_family'. Existing
# ml2/match_winner/fight_winner/total_runs pairing is unchanged (its adapters ARE its registry entry);
# NEW families are added as FamilySpecs so every pairing rule is written against a committed raw dump.


@dataclass
class FamilyResult:
    """One family's contribution to a game's tree: paired nodes, refusals (definition mismatch),
    inventory (unpaired-for-a-reason), and which venue markets it CLAIMED (so the residual can be
    inventoried as 'no_family')."""
    nodes: list = field(default_factory=list)
    refusals: list = field(default_factory=list)      # [{family, reason, kalshi, poly}]
    inventory: list = field(default_factory=list)      # [{venue, family, title, reason}]
    claimed_k: set = field(default_factory=set)        # kalshi tickers this family consumed
    claimed_p: set = field(default_factory=set)        # poly market slugs/ids this family consumed


@dataclass
class FamilySpec:
    """One market family's pairing rule, written against the dumped texts. ``build`` receives the raw
    markets this family CLAIMS on each venue (via the selectors) and returns a FamilyResult. Selectors
    return True for a market that belongs to this family (title/question/rules text), so the registry
    can compute the residual (claimed-by-none -> 'no_family')."""
    family: str
    mece_shape: str                                    # "2way"
    kalshi_selector: Callable[[dict], bool]
    poly_selector: Callable[[dict], bool]
    build: Callable[[list, list, dict], FamilyResult]
    settlement_axes: tuple = ()                        # named facets that MUST match to pair (doc/tests)


def run_registry(families: list, kalshi_markets: list, poly_markets: list, *,
                 ctx: Optional[dict] = None, log: Any = None, game_id: str = ""
                 ) -> tuple[list, list, list]:
    """Registry-driven pairing: each FamilySpec claims its markets on both venues and builds its nodes/
    refusals/inventory. Markets no family claims -> inventory reason='no_family'. Returns
    (nodes, unmatched, refusals) where ``unmatched`` merges every family's inventory + the residual."""
    ctx = ctx or {}
    nodes: list = []
    unmatched: list = []
    refusals: list = []
    claimed_k: set = set()
    claimed_p: set = set()
    for fam in families:
        k_claim = [m for m in kalshi_markets if _safe(fam.kalshi_selector, m)]
        p_claim = [m for m in poly_markets if _safe(fam.poly_selector, m)]
        if not k_claim and not p_claim:
            continue
        res = fam.build(k_claim, p_claim, ctx)
        nodes.extend(res.nodes)
        unmatched.extend(res.inventory)
        refusals.extend(res.refusals)
        claimed_k |= res.claimed_k
        claimed_p |= res.claimed_p
        if log:
            for r in res.refusals:
                log.warning("[%s] REFUSED %s: %s (kalshi=%r poly=%r)", game_id, fam.family,
                            r.get("reason"), r.get("kalshi"), r.get("poly"))
    # Residual: any market no family claimed at all -> inventory 'no_family'.
    for m in kalshi_markets:
        if str(m.get("ticker")) not in claimed_k and not any(_safe(f.kalshi_selector, m) for f in families):
            unmatched.append({"venue": "kalshi", "family": None, "market_type": "other",
                              "title": m.get("title"), "identifier": m.get("ticker"), "reason": "no_family"})
    for m in poly_markets:
        pid = str(m.get("slug") or m.get("id") or "")
        if pid not in claimed_p and not any(_safe(f.poly_selector, m) for f in families):
            unmatched.append({"venue": "polymarket", "family": None, "market_type": "other",
                              "title": m.get("question") or m.get("groupItemTitle"),
                              "identifier": pid, "reason": "no_family"})
    return nodes, unmatched, refusals


def _safe(fn: Callable, m: dict) -> bool:
    try:
        return bool(fn(m))
    except Exception:  # noqa: BLE001 - a selector must never crash the build
        return False


@runtime_checkable
class SportSpec(Protocol):
    """One sport's discovery + pairing rules. Duck-typed; the Protocol is for documentation/checking."""

    name: str

    def paths(self) -> gz_config.SportPaths:
        """The sport's isolated runtime-file set (tree/snapshot/heartbeat/arbs/papermaker)."""
        ...

    def game_id(self, game: Any) -> str:
        """Stable per-game key used for tree['games'][...] (soccer: the Kalshi event suffix)."""
        ...

    def discover_games(self, kalshi_client: Any, poly_client: Any, cfg: gz_config.GenzConfig, *,
                       now: Any, log: Any = None) -> tuple[list[Any], Any]:
        """Return (games, poly_context). ``poly_context`` is handed back to :meth:`pair_markets`
        unchanged (soccer: the whole series' events fetched once; mlb: same, for the fallback scan)."""
        ...

    def pair_markets(self, kalshi_client: Any, poly_client: Any, game: Any, poly_ctx: Any,
                     cfg: gz_config.GenzConfig, *, log: Any = None) -> Optional[dict[str, Any]]:
        """Build ONE game's tree entry (paired nodes + unmatched + coverage/meta), or None to skip it."""
        ...
