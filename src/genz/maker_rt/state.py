"""Runtime state I/O: the heartbeat, the dated per-event CSV, and the panel summary.

All three live under data/genz/. The heartbeat is written every loop so the panel can flag a stale
process; the summary aggregates the day's shadow (or live) activity for the MAKER strip; the CSV is
the append-only event log (quote/reprice/expire/behind/fill/hedge/drift/achievable_sample).

Schema 2 adds PER-SPORT and PER-PHASE (pre / inplay) breakdowns plus an ACHIEVABLE-NET ladder — the
net edge the book actually supports if we simply JOINED the best bid, aggregated so we can learn what
target the market bears (vs the fixed target_net). The achievable ladder uses a bounded reservoir for
percentiles + exact threshold counts, and is NEVER written per-evaluation (thousands/day) — only a
throttled 1-row/min/market sample lands in the CSV.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import config as mrt_config

SCHEMA = 3                       # 3: rails-gated achievable ladder + paired fill_drift + restarts_today
_RESERVOIR_CAP = 5000            # bounded achievable-net sample reservoir per (sport, phase)
RUNSTATE_NAME = "maker_rt_runstate.json"   # persistent process-start counter (crash-loop signal)
# Cross-restart tuning counters. These deliberately do NOT roll at midnight AND must outlive the
# process: fills are rare (single digits per day) and the maker restarts on every deploy — ~10x on a
# working day — so an in-memory "last 20 fills" window would reset long before it ever held 20 fills
# and the target_net question could never be answered. Kept in their own file because runstate.json
# rolls daily by design.
TUNING_NAME = "maker_rt_tuning.json"
_TUNING_WINDOW = 20                        # fills in the rolling unwind / realized-locked-net windows
_TUNING_SAVE_EVERY_S = 30.0                # quote counters are hot; persist on a timer + on every fill

CSV_COLUMNS = [
    "ts", "day", "event", "mode", "sport", "phase", "game", "market_key", "side", "direction",
    "rest_venue", "hedge_venue", "quote_price", "size", "floor", "at_best", "hedge_ask",
    "net_at_quote", "achievable_net", "rails_ok", "queue_ahead", "trigger", "quote_age_s",
    "hedge_avg", "hedge_fee", "locked_net", "realized_pnl_usd", "hedge_order_id", "fill_ts",
    "drift_1", "drift_5", "drift_30",
    # N28: the hedge's WALKED locked net (%) at the moment this row was written. Paired with
    # ``quote_age_s`` it is what makes "were we picked off?" answerable — the stale-quote question is a
    # correlation between how long a quote rested and how far the HEDGE had moved by the time it was
    # taken, and until now neither column was populated on a live row.
    "hedge_locked_now",
    "reason",
]


def hedged_lifetime(life: Any, untracked: Any, exits: Any) -> float:
    """THE hedged-pnl formula, in ONE place:  hedged = lifetime - untracked - exits.

    ``settled_pnl_lifetime`` is every dollar the venues actually realized for us. Two of its parts are
    not maker edge and must be removed before anyone reads it as "how well is the hedged strategy
    doing": ``untracked`` (naked luck — the UFC ghost-stack +$42) and ``exits`` (the unwind toll, a
    negative number, so subtracting it makes hedged LARGER than lifetime). Both call sites of this
    subtraction used to be spelled out by hand in five files; a formula with five copies is a formula
    with five chances to disagree, and this one decides what the balance audit compares to venue cash."""
    def f(v: Any) -> float:
        try:
            return float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return round(f(life) - f(untracked) - f(exits), 4)


def _median(xs: list) -> Optional[float]:
    vs = sorted(v for v in xs if v is not None)
    if not vs:
        return None
    n = len(vs)
    return round((vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2.0), 4)


def _pctl(vs: list, p: float) -> Optional[float]:
    """The p-th percentile (0..1) of a sorted-able list (nearest-rank), or None if empty."""
    if not vs:
        return None
    s = sorted(vs)
    i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return round(s[i], 4)


# --------------------------------------------------------------------------- #
# Per-(sport, phase) aggregate bucket                                           #
# --------------------------------------------------------------------------- #
@dataclass
class _Bucket:
    quotes: int = 0
    reprices: int = 0
    fills: int = 0
    behind: int = 0
    expired: int = 0
    at_best_hits: int = 0
    fill_nets: list = field(default_factory=list)
    drift1: list = field(default_factory=list)
    drift5: list = field(default_factory=list)
    drift30: list = field(default_factory=list)
    # ACHIEVABLE-NET accumulator (reservoir for percentiles + exact threshold counts). RAILS-GATED: only
    # rails_ok samples (both-books-fresh AND not-frozen) feed the ladder — that kills the ghost inflation
    # from stale/frozen phantom edges. Rails-failed evaluations are counted in achv_gated, not the ladder.
    achv_n: int = 0
    achv_gated: int = 0          # evaluations dropped from the ladder because rails_ok was false
    achv_reservoir: list = field(default_factory=list)
    achv_ge0: int = 0
    achv_ge25: int = 0            # >= 0.0025 (0.25%)
    achv_ge50: int = 0           # >= 0.0050 (0.50%)
    achv_ge100: int = 0          # >= 0.0100 (1.00%)

    def add_achievable(self, value: float, rng: random.Random) -> None:
        """Reservoir-sample the achievable-net value + bump the exact threshold counts."""
        self.achv_n += 1
        if value >= 0:
            self.achv_ge0 += 1
        if value >= 0.0025:
            self.achv_ge25 += 1
        if value >= 0.0050:
            self.achv_ge50 += 1
        if value >= 0.0100:
            self.achv_ge100 += 1
        r = self.achv_reservoir
        if len(r) < _RESERVOIR_CAP:
            r.append(value)
        else:                                            # Algorithm R: replace with prob cap/n
            j = rng.randint(0, self.achv_n - 1)
            if j < _RESERVOIR_CAP:
                r[j] = value

    def stats(self) -> dict:
        n = self.quotes
        return {
            "quotes": self.quotes, "fills": self.fills, "behind_best": self.behind,
            "fill_rate": round(self.fills / n, 4) if n else 0.0,
            "at_best_share": round(self.at_best_hits / n, 4) if n else 0.0,
            "median_net_at_fill": _median(self.fill_nets),
            "drift_median_1": _median(self.drift1), "drift_median_5": _median(self.drift5),
            "drift_median_30": _median(self.drift30),
            "achievable": self.achievable(),
        }

    def achievable(self) -> dict:
        """The achievable-net ladder for this bucket.

        UNITS, stated because they were mixed (N27): ``p50``/``p90`` are FRACTIONS of a share (0.0105 =
        1.05%) while ``median_net_at_fill`` and ``locked_net_p50`` in the same summary are PERCENTS —
        two edge numbers side by side, differing by 100x, with nothing in the payload saying so. The
        ``*_pct`` fields are the canonical ones; the bare ``p50``/``p90`` stay for the existing panel
        build and are deprecated. ``share_ge_*`` are shares of samples (0..1), never percents."""
        pct = lambda c: round(c / self.achv_n, 4) if self.achv_n else 0.0   # noqa: E731
        p50, p90 = _pctl(self.achv_reservoir, 0.5), _pctl(self.achv_reservoir, 0.9)
        return {
            "n": self.achv_n, "gated": self.achv_gated,           # n = rails_ok samples in the ladder
            "p50": p50, "p90": p90,                               # DEPRECATED (fractions) — use *_pct
            "p50_pct": round(p50 * 100.0, 4) if p50 is not None else None,
            "p90_pct": round(p90 * 100.0, 4) if p90 is not None else None,
            "units": {"p50_pct": "percent", "p90_pct": "percent", "share_ge_*": "share_of_samples"},
            "share_ge_0": pct(self.achv_ge0), "share_ge_25bp": pct(self.achv_ge25),
            "share_ge_50bp": pct(self.achv_ge50), "share_ge_100bp": pct(self.achv_ge100),
        }

    def achievable_state(self) -> dict:
        """The ladder's RAW counters + reservoir, for persistence across restarts (N27)."""
        return {"n": self.achv_n, "gated": self.achv_gated, "ge0": self.achv_ge0,
                "ge25": self.achv_ge25, "ge50": self.achv_ge50, "ge100": self.achv_ge100,
                "reservoir": list(self.achv_reservoir)}

    def load_achievable_state(self, d: dict) -> None:
        """Restore a persisted ladder. Counts and the reservoir travel together, so the percentiles
        stay consistent with the n they are drawn from."""
        if not isinstance(d, dict):
            return
        self.achv_n = int(d.get("n") or 0)
        self.achv_gated = int(d.get("gated") or 0)
        self.achv_ge0 = int(d.get("ge0") or 0)
        self.achv_ge25 = int(d.get("ge25") or 0)
        self.achv_ge50 = int(d.get("ge50") or 0)
        self.achv_ge100 = int(d.get("ge100") or 0)
        r = d.get("reservoir")
        self.achv_reservoir = [float(x) for x in r][:_RESERVOIR_CAP] if isinstance(r, list) else []


def _pool(buckets: list) -> dict:
    """Pool several _Bucket stats into one view (for a by-sport row spanning phases, or vice-versa).
    Counts sum exactly; percentiles come from the concatenated (capped) reservoirs; medians from the
    concatenated lists."""
    agg = _Bucket()
    for b in buckets:
        agg.quotes += b.quotes; agg.fills += b.fills; agg.behind += b.behind
        agg.at_best_hits += b.at_best_hits
        agg.fill_nets += b.fill_nets
        agg.drift1 += b.drift1; agg.drift5 += b.drift5; agg.drift30 += b.drift30
        agg.achv_n += b.achv_n; agg.achv_gated += b.achv_gated; agg.achv_ge0 += b.achv_ge0
        agg.achv_ge25 += b.achv_ge25; agg.achv_ge50 += b.achv_ge50; agg.achv_ge100 += b.achv_ge100
        agg.achv_reservoir += b.achv_reservoir
    if len(agg.achv_reservoir) > _RESERVOIR_CAP:
        agg.achv_reservoir = agg.achv_reservoir[:_RESERVOIR_CAP]
    return agg.stats()


@dataclass
class MakerState:
    """Day-scoped aggregates + the file writers (heartbeat / CSV / summary). Holds flat totals (for the
    heartbeat + top-line summary) AND per-(sport, phase) buckets (for the schema-2 breakdowns)."""
    day: str = ""
    n_quotes: int = 0
    n_reprices: int = 0
    n_fills: int = 0
    n_behind: int = 0
    n_expired: int = 0
    at_best_hits: int = 0
    fill_nets: list = field(default_factory=list)      # locked net per shadow fill (%)
    drift1: list = field(default_factory=list)
    drift5: list = field(default_factory=list)
    drift30: list = field(default_factory=list)
    pnl_today: float = 0.0
    n_unwinds: int = 0                                  # unwind rows today (hedge_declined|hedge_unwound|unwind_FAILED)
    unwind_cost_today: float = 0.0                      # $ paid to EXIT (unwind cost) today
    restarts_today: int = 0                             # process starts today (crash-loop signal; set at startup)
    # UNWIND-ECONOMICS lifetime counters + a 20-fill rolling window (1=fill unwound, 0=cleanly hedged).
    # These SURVIVE the daily roll so unwind_rate + the amber "paying the exit toll too often" signal
    # reflect recent behaviour across midnight, not a counter that resets to 0/0 every day.
    lifetime_fills: int = 0
    lifetime_unwinds: int = 0
    recent_outcomes: Any = field(default_factory=lambda: deque(maxlen=_TUNING_WINDOW))
    # TARGET-NET TUNING SIGNALS (both survive the daily roll — see below). These are the two numbers that
    # say whether lowering target_net to 0.6% was right or too thin:
    #   fills_per_100_quotes — did the thinner floor actually buy us more SHOTS TAKEN? A day-scoped
    #     fill_rate is useless here because fills are rare enough to read 0/750 on most days, so this
    #     counts across days. lifetime_quotes is the denominator (lifetime_fills already exists).
    #   recent_locked_nets — the REALIZED locked net (%) of the last 20 cleanly-hedged fills, so we can
    #     see the p50/p10 of what we actually captured rather than what we hoped to at quote time.
    #     Read it WITH unwind_rate_recent over the same window: this deque only holds fills that LOCKED,
    #     so on its own it cannot show fills that paid the exit toll instead.
    lifetime_quotes: int = 0
    recent_locked_nets: Any = field(default_factory=lambda: deque(maxlen=_TUNING_WINDOW))
    # SETTLED-P&L (VENUE TRUTH). The fill-time locked_pnl is an ESTIMATE; the real number is known only
    # after BOTH legs settle/redeem. A ``trade_settled`` row (written by the settlement reconciler) nets
    # both venues; these lifetime counters are the AUTHORITATIVE realized pnl the panel/summary report
    # (vs the fill-time estimate in pnl_today). They survive the daily roll + persist across restarts.
    settled_pnl_lifetime: float = 0.0                  # sum of true net across ALL settled trades ($)
    # UNTRACKED (naked) settled pnl — e.g. the 2026-07-25 UFC ghost-stack luck (+$42). Tracked SEPARATELY
    # so the HEDGED-only realized number (settled_pnl_lifetime − this) is not flattered by a naked windfall.
    settled_pnl_untracked_lifetime: float = 0.0
    # EXIT COSTS (the unwind toll), lifetime. THE BOOKS-vs-BANKS GAP: a fill we could not hedge is
    # unwound at a real cost in real venue cash, but the market it happened in then settles with us
    # FLAT — so no trade_settled row is ever written for it and, before this counter, the toll lived
    # only in ``unwind_cost_today``, which rolls to zero at UTC midnight. Between the 2026-07-31
    # baseline and 2026-08-04 that silently hid $6.99: the books claimed +$8.53 while the exchanges
    # moved +$3.30. The toll is REAL REALIZED MONEY, so it enters ``settled_pnl_lifetime`` (which is
    # what the balance audit compares against venue cash) AND is kept here so the HEDGED number can
    # still be read pure:  hedged = lifetime - untracked - exits.
    settled_pnl_exits_lifetime: float = 0.0            # signed (always <= 0 in practice): $ paid to EXIT
    settled_exits: int = 0                             # count of VERIFIED exits booked into lifetime
    settled_cost_lifetime: float = 0.0                 # sum of cost basis across settled trades ($; ROI denom)
    settled_trades: int = 0                            # count of settled trades (hedged + untracked)
    # SANITY CEILING on |net| for a settled row (defense-in-depth: the reconciler guards at the source,
    # this guards the AGGREGATOR so a backfill / CSV replay / future call site can't inject a corrupt
    # number either). Set at startup from cfg.live.max_pair_stake_usd; the ROI ceiling is fixed at 50%.
    settled_max_net_usd: float = 100.0
    # Keys of the one-time restatements already applied to the counters above. Persisted WITH them, so
    # "has this correction been applied?" is answered by the same file the correction changed — there is
    # no window in which one landed and the other did not.
    restatements_applied: list = field(default_factory=list)
    # The same corrections, DATED. A restatement moves the books at the moment it is applied, for money
    # the venues moved at some earlier time — so to the 8-hourly audit it looks exactly like a book that
    # changed while no cash did, which is the one thing that audit exists to scream about. Each entry
    # carries ``applied_ts`` (when the books moved) and ``effective_ts`` (when the cash moved) so a
    # window can tell a correction apart from a leak instead of raising a false alarm on our own fix.
    restatement_log: list = field(default_factory=list)
    _tuning_saved_ts: float = 0.0                      # last persist (see maybe_persist_tuning)
    measurement_gates: dict = field(default_factory=dict)   # gates.report(), refreshed off the hot path
    # SAFETY SYSTEMS: last-landed age per background pass (fill-poll / reconcile / settle / gates /
    # balance), with the cadence each is supposed to keep. Set by the loop from the executor's
    # ``safety_snapshot`` + the balance reconciler. It is on the HEARTBEAT as well as the summary
    # because the heartbeat is the surface that survives when everything else is unavailable — and the
    # whole point is that a quiet safety net must be visibly quiet, not invisibly absent.
    safety: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)          # {"pre": bool, "inplay": bool} — armed states, set at startup
    live: dict = field(default_factory=dict)           # PRE-GAME live snapshot (open_quotes/stake/fills/pnl/halt/feed_ok)
    buckets: dict = field(default_factory=dict)        # (sport, phase) -> _Bucket
    log: Any = None                                    # optional logger (artifact-guard WARNINGs)
    _rng: Any = field(default_factory=lambda: random.Random(1234))

    def _roll(self, day: str) -> None:
        if day != self.day:
            keep = (self.log, self.restarts_today, self.gates, self.live,   # survive the daily reset
                    self.lifetime_fills, self.lifetime_unwinds, self.recent_outcomes,
                    self.lifetime_quotes, self.recent_locked_nets,
                    self.settled_pnl_lifetime, self.settled_cost_lifetime, self.settled_trades,
                    self.settled_max_net_usd, self.settled_pnl_untracked_lifetime, self.safety,
                    self.settled_pnl_exits_lifetime, self.settled_exits, self.restatements_applied,
                    self.restatement_log)
            self.__init__(day=day)  # type: ignore[misc]
            (self.log, self.restarts_today, self.gates, self.live,
             self.lifetime_fills, self.lifetime_unwinds, self.recent_outcomes,
             self.lifetime_quotes, self.recent_locked_nets,
             self.settled_pnl_lifetime, self.settled_cost_lifetime, self.settled_trades,
             self.settled_max_net_usd, self.settled_pnl_untracked_lifetime, self.safety,
             self.settled_pnl_exits_lifetime, self.settled_exits, self.restatements_applied,
             self.restatement_log) = keep

    def _bucket(self, sport: str, phase: str) -> _Bucket:
        return self.buckets.setdefault((str(sport or "?"), str(phase or "pre")), _Bucket())

    # -- cross-restart tuning counters ---------------------------------------
    def load_tuning(self) -> None:
        """Restore the cross-restart counters at startup. Without this the 'last 20 fills' windows
        would reset on every deploy (~10x/day) and never accumulate enough fills to judge target_net."""
        obj = load_tuning(self.log)
        if not obj:
            return
        self.lifetime_quotes = int(obj.get("lifetime_quotes", 0) or 0)
        self.lifetime_fills = int(obj.get("lifetime_fills", 0) or 0)
        self.lifetime_unwinds = int(obj.get("lifetime_unwinds", 0) or 0)
        self.recent_outcomes = deque((int(v) for v in (obj.get("recent_outcomes") or [])),
                                     maxlen=_TUNING_WINDOW)
        self.recent_locked_nets = deque((float(v) for v in (obj.get("recent_locked_nets") or [])),
                                        maxlen=_TUNING_WINDOW)
        self.settled_pnl_lifetime = float(obj.get("settled_pnl_lifetime", 0.0) or 0.0)
        self.settled_pnl_untracked_lifetime = float(obj.get("settled_pnl_untracked_lifetime", 0.0) or 0.0)
        self.settled_pnl_exits_lifetime = float(obj.get("settled_pnl_exits_lifetime", 0.0) or 0.0)
        self.settled_exits = int(obj.get("settled_exits", 0) or 0)
        self.restatements_applied = [str(k) for k in (obj.get("restatements_applied") or []) if k]
        self.restatement_log = [r for r in (obj.get("restatement_log") or []) if isinstance(r, dict)]
        self.settled_cost_lifetime = float(obj.get("settled_cost_lifetime", 0.0) or 0.0)
        self.settled_trades = int(obj.get("settled_trades", 0) or 0)
        # ACHIEVABLE LADDERS (N27). Restarts run ~10-21x/day, and the ladder is the ONLY evidence that
        # says whether a sport's market bears the target at all — resetting it every deploy meant it
        # could never reach an n worth reading. Restored per (sport, phase) with its counts and its
        # reservoir together, so the percentiles stay consistent with the n they are drawn from.
        for key, d in (obj.get("achievable") or {}).items():
            try:
                sport, _, phase = str(key).partition("|")
                if sport and phase:
                    self._bucket(sport, phase).load_achievable_state(d)
            except Exception:  # noqa: BLE001 — a corrupt ladder entry must never block startup
                continue

    def apply_restatements(self, path: Optional[str] = None) -> list:
        """Apply any one-time KEYED corrections to the lifetime counters. Returns the keys applied now.

        Call ONCE at startup, immediately after :meth:`load_tuning`. A restatement corrects history that
        the live path can no longer produce — money the venues moved that the books never booked — and it
        must land EXACTLY ONCE across the 10-21 restarts a working day sees. So the key is written into
        the SAME file the counters live in, by the SAME atomic write: there is no ordering in which the
        correction is applied but its key is not, and an already-listed key is refused forever.

        The file is a list of ``{key, note, exits_usd?, untracked_usd?, hedged_usd?}``. ``exits_usd`` is
        SIGNED the way the cash moved (a paid exit toll is NEGATIVE) and moves BOTH
        ``settled_pnl_lifetime`` and the exits bucket, exactly as a live exit does — so the restated
        number is indistinguishable from one the running bot would have produced."""
        p = path or mrt_config.runtime_path("restatements")
        if not os.path.exists(p):
            return []
        try:
            with open(p, "r", encoding="utf-8-sig") as fh:
                entries = json.load(fh)
        except (ValueError, OSError, TypeError):
            preserve_unreadable(p, self.log, what="the lifetime restatements")
            return []
        if isinstance(entries, dict):
            entries = entries.get("restatements") or []
        applied: list = []
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            key = str(e.get("key") or "")
            if not key:
                continue

            def _f(name: str) -> float:
                try:
                    return float(e.get(name) or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            if key in self.restatements_applied:
                # ALREADY BOOKED — the money must never move again. But an entry applied by a build that
                # predates ``restatement_log`` has no dated record, and without one the balance audit
                # cannot tell our own correction from a leak and will alarm on it forever. So rebuild the
                # LOG ENTRY ONLY, touching no counter.
                if not any(str(r.get("key")) == key for r in self.restatement_log):
                    self.restatement_log.append({
                        "key": key, "usd": round(_f("exits_usd") + _f("untracked_usd")
                                                 + _f("hedged_usd"), 4),
                        "applied_ts": str(e.get("applied_ts") or utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
                        "effective_ts": str(e.get("effective_ts")
                                            or e.get("applied_ts")
                                            or utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
                        "note": str(e.get("note") or "")[:200], "reconstructed": True})
                    self.persist_tuning()
                continue                                  # REFUSE the money, silently and forever
            exits, untracked, hedged = _f("exits_usd"), _f("untracked_usd"), _f("hedged_usd")
            total = exits + untracked + hedged
            self.settled_pnl_lifetime += total
            self.settled_pnl_exits_lifetime += exits
            self.settled_pnl_untracked_lifetime += untracked
            if abs(exits) > 1e-9:
                self.settled_exits += int(e.get("exits_n") or 0)
            self.restatements_applied.append(key)
            # DATED, so the balance audit can tell this apart from a leak. ``effective_ts`` is when the
            # CASH moved (it defaults to now, which makes the entry inert for windows either way);
            # ``applied_ts`` is when the BOOKS moved, i.e. right now.
            self.restatement_log.append({
                "key": key, "usd": round(total, 4),
                "applied_ts": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "effective_ts": str(e.get("effective_ts") or utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
                "note": str(e.get("note") or "")[:200]})
            applied.append(key)
            if self.log:
                self.log.warning(
                    "[MAKER_RT][RESTATEMENT] applied %s: exits %+.4f, untracked %+.4f, hedged %+.4f -> "
                    "lifetime %+.4f (hedged %+.4f). %s", key, exits, untracked, hedged,
                    self.settled_pnl_lifetime,
                    hedged_lifetime(self.settled_pnl_lifetime, self.settled_pnl_untracked_lifetime,
                                    self.settled_pnl_exits_lifetime), e.get("note") or "")
        if applied:
            self.persist_tuning()          # key + counters land TOGETHER, in one atomic write
        return applied

    def persist_tuning(self) -> None:
        """Write the cross-restart counters (atomic, best-effort — never blocks trading)."""
        _atomic_json(_tuning_path(), {
            "lifetime_quotes": self.lifetime_quotes,
            "lifetime_fills": self.lifetime_fills,
            "lifetime_unwinds": self.lifetime_unwinds,
            "recent_outcomes": list(self.recent_outcomes),
            "recent_locked_nets": list(self.recent_locked_nets),
            "settled_pnl_lifetime": round(self.settled_pnl_lifetime, 4),
            "settled_pnl_untracked_lifetime": round(self.settled_pnl_untracked_lifetime, 4),
            "settled_pnl_exits_lifetime": round(self.settled_pnl_exits_lifetime, 4),
            "settled_exits": self.settled_exits,
            "restatements_applied": list(self.restatements_applied),
            "restatement_log": list(self.restatement_log),
            "settled_cost_lifetime": round(self.settled_cost_lifetime, 4),
            "settled_trades": self.settled_trades,
            "achievable": {f"{sp}|{ph}": b.achievable_state()
                           for (sp, ph), b in self.buckets.items() if b.achv_n},
        })

    def maybe_persist_tuning(self, now_ts: float) -> None:
        """Throttled persist for the hot quote counter (fills persist immediately — see record())."""
        if now_ts - self._tuning_saved_ts < _TUNING_SAVE_EVERY_S:
            return
        self._tuning_saved_ts = now_ts
        self.persist_tuning()

    # -- events -------------------------------------------------------------
    def record(self, row: dict, now: datetime) -> None:
        """Update aggregates from an event row and append it to the dated CSV."""
        ev = row.get("event")
        # ARTIFACT GUARD: a schema-transition parse once emitted 8 blank fills (no game/market, junk
        # locked_net) into a real day. A fill with no sport/game/market is not real — WARN and DROP it
        # (never aggregated, never written) so a corrupt row can't inflate fills / pnl / the ladders.
        if ev == "fill" and not (row.get("sport") and row.get("game") and row.get("market_key")):
            if self.log:
                self.log.warning("[MAKER_RT] dropped malformed fill (no sport/game/market): %s",
                                 {k: row.get(k) for k in ("sport", "game", "market_key", "phase",
                                                          "locked_net", "trigger")})
            return
        day = now.strftime("%Y%m%d")
        self._roll(day)
        b = self._bucket(row.get("sport"), row.get("phase"))
        if ev == "quote":
            self.n_quotes += 1; b.quotes += 1; self.lifetime_quotes += 1
            if row.get("at_best"):
                self.at_best_hits += 1; b.at_best_hits += 1
        elif ev == "reprice":
            self.n_reprices += 1; b.reprices += 1
        elif ev == "behind":
            self.n_behind += 1; b.behind += 1
        elif ev == "expire":
            self.n_expired += 1; b.expired += 1
        elif ev == "fill":
            self.n_fills += 1; b.fills += 1; self.lifetime_fills += 1
            if row.get("locked_net") is not None:
                self.fill_nets.append(float(row["locked_net"]))
                b.fill_nets.append(float(row["locked_net"]))
                self.pnl_today += float(row.get("locked_pnl") or 0.0)
        elif ev == "hedge_locked":
            self.recent_outcomes.append(0)                  # a CLEAN hedge — the fill did NOT need an exit
            if row.get("locked_net") is not None:           # REALIZED locked net (%) — the target_net check
                self.recent_locked_nets.append(float(row["locked_net"]))
            self.persist_tuning()                           # fills are rare + precious: persist NOW
        elif ev in ("hedge_declined", "hedge_unwound", "unwind_FAILED", "auto_flattened"):
            # the fill required an EXIT (we paid the unwind toll). unwind_cost is on the row here, BEFORE
            # _append_csv drops it (it's not a CSV column — realized_pnl_usd carries -cost to the CSV).
            self.n_unwinds += 1; self.lifetime_unwinds += 1
            cost = float(row.get("unwind_cost") or 0.0)
            self.unwind_cost_today += cost
            self.recent_outcomes.append(1)
            # THE EXIT TOLL ENTERS LIFETIME. This money left the account for good: we bought shares we
            # could not hedge and sold them back cheaper, and the market then settled with us FLAT, so
            # no trade_settled row will ever be written to account for it. Until now the only record was
            # ``unwind_cost_today``, which resets at UTC midnight — which is precisely how the books came
            # to claim +$8.53 while the exchanges had moved +$3.30.
            #
            # WHICH EXITS COUNT, and why the third one does not:
            #   hedge_unwound  — a VERIFIED unwind (``_verified_unwind`` confirmed flat). Always booked;
            #                    a fully-hedged or dust row simply carries cost 0.0 and adds nothing.
            #   auto_flattened — the bounded sweep that closes a position the first unwind could not,
            #                    and it is TERMINAL on exactly the same terms: it books only after the
            #                    position reads flat. Same treatment, for the same reason. It has never
            #                    fired (0 rows in every CSV ever written), which is precisely why it was
            #                    worth closing now — its cost would have vanished from lifetime exactly
            #                    as the three unwinds did, and nobody would have been looking.
            #   hedge_declined — same verified-flat path, taken because the hedge was too dear. Booked
            #                    ONLY when it carries a real paid cost: the zero-cost variants are a
            #                    fill that needed no exit and un-closable dust, neither of which moved
            #                    cash, and booking them would put a 0.00 exit in the count.
            #   unwind_FAILED  — NOT booked. That position STILL EXISTS. It is marked provisionally and
            #                    its real outcome arrives later as mark_corrected / trade_settled, which
            #                    already add to lifetime. Booking it here too would double-count the
            #                    same shares — once at a worst-case guess and once at venue truth.
            if ev in ("hedge_unwound", "auto_flattened") or (ev == "hedge_declined" and abs(cost) > 1e-9):
                self.settled_pnl_lifetime -= cost           # cost is POSITIVE $ paid; lifetime falls
                self.settled_pnl_exits_lifetime -= cost     # ... and the exits bucket keeps it separable
                self.settled_exits += 1
            self.persist_tuning()                           # ditto — a paid exit toll must survive a deploy
        elif ev == "fill_drift":
            for src, dst_flat, dst_b in ((row.get("drift_1"), self.drift1, b.drift1),
                                         (row.get("drift_5"), self.drift5, b.drift5),
                                         (row.get("drift_30"), self.drift30, b.drift30)):
                if src is not None and src != "":
                    dst_flat.append(float(src)); dst_b.append(float(src))
        elif ev == "trade_settled":
            # VENUE-TRUTH realized pnl (both legs netted, incl. settlement/redemption). This — NOT the
            # fill-time locked_pnl estimate — is the authoritative lifetime realized number the panel
            # reports. ``settled_cost_usd`` rides on the row for the ROI denominator but is NOT a CSV
            # column (like unwind_cost/locked_pnl — read here, then dropped by _append_csv); the human
            # cost/ROI is carried in ``reason``. Idempotency is the reconciler's job (keyed by market).
            if row.get("realized_pnl_usd") not in (None, ""):
                # SANITY GUARD (defense-in-depth): a settled net larger than one pair's stake or with a
                # >50% ROI is a unit/pairing bug (the $500-for-$5 incident). REFUSE it here so it can
                # NEVER enter lifetime pnl, log CRITICAL, and rename the CSV event so no path re-reads it
                # as a real settled trade. The reconciler already refuses at the source; this catches a
                # backfill / CSV replay / any other caller.
                from .settle import sane_settled
                ok, why = sane_settled(row["realized_pnl_usd"], row.get("settled_cost_usd", 0.0) or 0.0,
                                       max_net_usd=self.settled_max_net_usd,
                                       untracked=bool(row.get("untracked")))
                if not ok:
                    if self.log:
                        crit = getattr(self.log, "critical", None) or self.log.error
                        crit("[MAKER_RT][SETTLE][CRITICAL] REFUSED trade_settled %s %s (%s): net $%s "
                             "cost $%s — NOT counted in lifetime pnl. %s", row.get("game"),
                             row.get("market_key"), why, row.get("realized_pnl_usd"),
                             row.get("settled_cost_usd"), row.get("reason"))
                    self._append_csv({**row, "event": "trade_settled_refused"}, now)
                    return                                       # never aggregate; audit row is inert
                self.settled_pnl_lifetime += float(row["realized_pnl_usd"])
                if row.get("untracked"):                        # naked windfall/loss — keep it OUT of hedged pnl
                    self.settled_pnl_untracked_lifetime += float(row["realized_pnl_usd"])
                self.settled_trades += 1
                if row.get("settled_cost_usd") not in (None, ""):
                    self.settled_cost_lifetime += float(row["settled_cost_usd"])
                self.persist_tuning()                           # settled truth is precious — persist NOW
        elif ev == "mark_corrected":
            # A position we had booked at a WORST-CASE mark has closed/settled and the venue told us what
            # it really came to. ``realized_pnl_usd`` here is the ACTUAL outcome (the executor applies the
            # delta to the daily counter separately), so it enters lifetime realized on exactly the same
            # terms as a trade_settled row. It rides the UNTRACKED bucket because a leg that reached this
            # path was NAKED — its outcome is luck, not maker edge, and must not flatter the hedged number.
            if row.get("realized_pnl_usd") not in (None, ""):
                from .settle import sane_settled
                ok, why = sane_settled(row["realized_pnl_usd"], row.get("settled_cost_usd", 0.0) or 0.0,
                                       max_net_usd=self.settled_max_net_usd, untracked=True)
                if not ok:
                    if self.log:
                        crit = getattr(self.log, "critical", None) or self.log.error
                        crit("[MAKER_RT][SETTLE][CRITICAL] REFUSED mark_corrected %s (%s): net $%s — NOT "
                             "counted. %s", row.get("game"), why, row.get("realized_pnl_usd"),
                             row.get("reason"))
                    self._append_csv({**row, "event": "mark_corrected_refused"}, now)
                    return
                self.settled_pnl_lifetime += float(row["realized_pnl_usd"])
                self.settled_pnl_untracked_lifetime += float(row["realized_pnl_usd"])
                self.settled_trades += 1
                if row.get("settled_cost_usd") not in (None, ""):
                    self.settled_cost_lifetime += float(row["settled_cost_usd"])
                self.persist_tuning()
        self._append_csv(row, now)

    def record_achievable(self, sport: str, phase: str, value: Optional[float], now: datetime,
                          rails_ok: bool = True) -> None:
        """Aggregate ONE achievable-net evaluation into the (sport, phase) bucket. NO CSV row (this
        fires thousands of times/day — the throttled sample is a separate event via record()).
        RAILS-GATED: only rails_ok evaluations (both-books-fresh AND not-frozen) feed the ladder; a
        rails-failed one is counted in ``gated`` and kept out of the percentiles/thresholds — that is
        what kills the ghost inflation from stale/frozen phantom edges."""
        if value is None:
            return
        self._roll(now.strftime("%Y%m%d"))
        b = self._bucket(sport, phase)
        if not rails_ok:
            b.achv_gated += 1
            return
        b.add_achievable(float(value), self._rng)

    def _append_csv(self, row: dict, now: datetime, *, retries: int = 4,
                    backoff_s: float = 0.05) -> bool:
        """Append one event row to the dated CSV. RETRIES a transient lock and NEVER raises.

        This open used to be unguarded, and it is called from the middle of the fill -> hedge chain
        (``record`` runs before the hedge is booked). On Windows a reader holding the file — the panel
        HTTP server, antivirus — makes the open fail with PermissionError, and that exception,
        propagating out of ``_record_fill``, would abandon the chain between advancing the fill and
        hedging it (N10). The ledger is important; it is not more important than the hedge. So: retry
        briefly, then WARN and drop this one row rather than take the trading path down with it."""
        path = mrt_config.events_path_for(now.strftime("%Y%m%d"))   # resolver-guarded
        mrt_config.assert_writable(path)          # guard at the WRITE site too, not just the resolver
        full = {c: "" for c in CSV_COLUMNS}
        full.update({k: v for k, v in row.items() if k in CSV_COLUMNS})
        full["ts"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        full["day"] = now.strftime("%Y%m%d")
        last: Optional[Exception] = None
        for i in range(max(1, retries)):
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                new = not os.path.exists(path)
                with open(path, "a", encoding="utf-8", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                    if new:
                        w.writeheader()
                    w.writerow(full)
                return True
            except OSError as exc:                 # PermissionError is an OSError on Windows
                last = exc
                time.sleep(backoff_s * (i + 1))
        (self.log or logging.getLogger("maker_rt")).warning(
            "[MAKER_RT] events CSV append blocked after %d retries (%s) — DROPPED one %r row rather "
            "than raising into the trading path.", retries, last, row.get("event"))
        return False

    # -- summary + heartbeat -------------------------------------------------
    def _caps_num(self, name: str, fallback: float) -> float:
        """A daily counter from the LIVE caps snapshot, falling back to the since-restart number.

        ``self.live`` is the executor's own snapshot of ``LiveCaps``, so this reads the same counters
        the daily rails enforce rather than a second set that happens to share their names."""
        v = (self.live or {}).get(name)
        try:
            return float(v) if v is not None else float(fallback)
        except (TypeError, ValueError):
            return float(fallback)

    def _by_sport(self) -> dict:
        sports: dict = {}
        for (sport, _phase), b in self.buckets.items():
            sports.setdefault(sport, []).append(b)
        return {s: _pool(bs) for s, bs in sports.items()}

    def _by_phase(self) -> dict:
        phases: dict = {}
        for (_sport, phase), b in self.buckets.items():
            phases.setdefault(phase, []).append(b)
        return {p: _pool(bs) for p, bs in phases.items()}

    def summary(self, mode: str, sockets: dict, now: datetime) -> dict:
        return {
            "schema": SCHEMA, "mode": mode, "updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "day": self.day, "sockets": dict(sockets),
            "quotes": self.n_quotes, "reprices": self.n_reprices, "fills": self.n_fills,
            "behind_best": self.n_behind, "expired": self.n_expired,
            "fill_rate": round(self.n_fills / self.n_quotes, 4) if self.n_quotes else 0.0,
            "at_best_share": round(self.at_best_hits / self.n_quotes, 4) if self.n_quotes else 0.0,
            "median_net_at_fill": _median(self.fill_nets),
            "drift_median_1": _median(self.drift1), "drift_median_5": _median(self.drift5),
            "drift_median_30": _median(self.drift30),
            "restarts_today": self.restarts_today,
            # F2 — THE DAILY TRIO, FROM THE RAIL THAT ENFORCES IT. ``n_fills``/``pnl_today`` on this
            # object reset with the PROCESS (~10-21 restarts/day), so a panel reading them saw a day
            # that started at the last deploy while the caps that actually halt trading were counting
            # from midnight. The alerts were always truthful (they read caps); only the panel was not.
            # ``*_since_restart`` keeps the old numbers under a name that says what they are.
            "fills_today": self._caps_num("fills_today", self.n_fills),
            "stake_today": self._caps_num("stake_today", 0.0),
            "pnl_today": round(self._caps_num("pnl_today", self.pnl_today), 4),
            "fills_since_restart": self.n_fills,
            "pnl_since_restart": round(self.pnl_today, 4),
            "windows": {"fills_today": "utc_day (LiveCaps)", "stake_today": "utc_day (LiveCaps)",
                        "pnl_today": "utc_day (LiveCaps)", "quotes": "since_restart",
                        "fills": "since_restart", "median_net_at_fill": "since_restart (percent)",
                        "locked_net_p50": "last<=20 locked fills (percent)",
                        "settled_pnl_lifetime": "lifetime (venue-truth)"},
            # SETTLED (VENUE-TRUTH) realized pnl — the authoritative lifetime number (both legs netted),
            # distinct from pnl_today (the fill-time locked estimate).
            "settled_pnl_lifetime": round(self.settled_pnl_lifetime, 4),
            # HEDGED-ONLY realized pnl: lifetime MINUS untracked naked windfalls (the UFC +$42) MINUS the
            # exit toll, so the maker's true hedged edge is flattered by neither luck nor a hidden loss.
            "settled_pnl_hedged_lifetime": hedged_lifetime(self.settled_pnl_lifetime,
                                                           self.settled_pnl_untracked_lifetime,
                                                           self.settled_pnl_exits_lifetime),
            "settled_pnl_untracked_lifetime": round(self.settled_pnl_untracked_lifetime, 4),
            "settled_pnl_exits_lifetime": round(self.settled_pnl_exits_lifetime, 4),
            "settled_exits": self.settled_exits,
            "settled_trades": self.settled_trades,
            "settled_roi": (round(self.settled_pnl_lifetime / self.settled_cost_lifetime, 4)
                            if self.settled_cost_lifetime else 0.0),
            "unwind_count_today": self.n_unwinds,
            "unwind_cost_today_usd": round(self.unwind_cost_today, 4),
            "unwind_rate": round(self.lifetime_unwinds / self.lifetime_fills, 4) if self.lifetime_fills else 0.0,
            "unwind_rate_recent": (round(sum(self.recent_outcomes) / len(self.recent_outcomes), 4)
                                   if self.recent_outcomes else 0.0),   # over the last <=20 fills
            "unwind_window": len(self.recent_outcomes),
            # -- target_net tuning: did 0.6% buy more SHOTS, and what did we actually CAPTURE? --
            "fills_per_100_quotes": (round(100.0 * self.lifetime_fills / self.lifetime_quotes, 3)
                                     if self.lifetime_quotes else 0.0),
            "fills_per_100_quotes_n": self.lifetime_quotes,   # denominator — a tiny sample must be visible
            "locked_net_p50": _pctl(list(self.recent_locked_nets), 0.5),   # % , last <=20 LOCKED fills
            "locked_net_p10": _pctl(list(self.recent_locked_nets), 0.1),   # the thin tail that kills us
            "locked_net_window": len(self.recent_locked_nets),
            "gates": dict(self.gates), "live": dict(self.live),
            # SAFETY SYSTEMS — the last time each background safety pass actually LANDED. See the field.
            "safety": dict(self.safety),
            "by_sport": self._by_sport(), "by_phase": self._by_phase(),
            # THE MEASUREMENT GATE for the panel — served from a CACHE that a SLOW cadence refreshes.
            #
            # It was computed inline here first, and that was a serious mistake: the summary is written
            # every 2.5s and the report parses every maker_rt_*.csv on disk (~59 MB today). The loop went
            # to 62s per tick, the heartbeat block alone to 56s, and both websockets timed out — the
            # maker stopped quoting entirely for seven minutes. A reporting helper must never be on a
            # hot path, and "cheap" is not a property you get to assume about something that reads files.
            "measurement_gates": dict(self.measurement_gates),
        }

    def write_summary(self, mode: str, sockets: dict, now: datetime,
                      path: Optional[str] = None) -> None:
        _atomic_json(path or mrt_config.summary_path(), self.summary(mode, sockets, now))

    def heartbeat(self, mode: str, sockets: dict, open_quotes: int, now: datetime) -> dict:
        hb = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "schema": SCHEMA, "mode": mode,
              "sockets": dict(sockets), "open_quotes": open_quotes,
              # F2: the DAILY trio comes from LiveCaps (what the rails count), and the since-restart
              # numbers keep a name that says which window they are — they were called "today".
              "fills_today": self._caps_num("fills_today", self.n_fills),
              "stake_today": self._caps_num("stake_today", 0.0),
              "pnl_today": round(self._caps_num("pnl_today", self.pnl_today), 4),
              "fills_since_restart": self.n_fills,
              "pnl_since_restart": round(self.pnl_today, 4),
              "settled_pnl_lifetime": round(self.settled_pnl_lifetime, 4),
              "settled_pnl_hedged_lifetime": hedged_lifetime(self.settled_pnl_lifetime,
                                                             self.settled_pnl_untracked_lifetime,
                                                             self.settled_pnl_exits_lifetime),
              "settled_pnl_untracked_lifetime": round(self.settled_pnl_untracked_lifetime, 4),
              "settled_pnl_exits_lifetime": round(self.settled_pnl_exits_lifetime, 4),
              "settled_trades": self.settled_trades,
              "restarts_today": self.restarts_today, "gates": dict(self.gates),
              "safety": dict(self.safety), "live": dict(self.live)}
        # TOP-LEVEL ORPHAN + FLAP banner: a naked position must be visible in the heartbeat itself, not
        # buried under live{}. This is the surface that does NOT depend on Telegram being reachable.
        orph = (self.live or {}).get("orphan")
        if orph:
            hb["ORPHAN"] = orph
            hb["halted"] = True
        flaps = (self.live or {}).get("flaps")
        if flaps:
            hb["flaps"] = flaps
        # target_net tuning signals on the heartbeat too, so the panel shows them even before the
        # (larger, less frequently written) summary lands.
        hb["fills_per_100_quotes"] = (round(100.0 * self.lifetime_fills / self.lifetime_quotes, 3)
                                      if self.lifetime_quotes else 0.0)
        hb["locked_net_p50"] = _pctl(list(self.recent_locked_nets), 0.5)
        hb["locked_net_p10"] = _pctl(list(self.recent_locked_nets), 0.1)
        hb["locked_net_window"] = len(self.recent_locked_nets)
        return hb

    def write_heartbeat(self, mode: str, sockets: dict, open_quotes: int, now: datetime,
                        path: Optional[str] = None) -> None:
        _atomic_json(path or mrt_config.heartbeat_path(),
                     self.heartbeat(mode, sockets, open_quotes, now))


def _atomic_json(path: str, obj: Any, *, retries: int = 5, backoff_s: float = 0.04,
                 default: Any = None) -> bool:
    """Atomically write ``obj`` as JSON; returns True iff the bytes actually landed.

    ROBUST on Windows: ``os.replace`` raises PermissionError when a reader (the panel HTTP server /
    antivirus) momentarily holds the target open — the #1 crash of the weekend (50/51 tracebacks) took
    the whole maker process down here. Retry with a short backoff, and if still blocked, WARN and skip
    this write rather than crash. A per-pid tmp avoids old/new-process collisions during a restart.

    The RETURN VALUE is the point of the bool: a persister that cannot tell whether it wrote is how a
    spent daily budget silently reopens. Callers holding money state (see ``_persist_json``) escalate a
    False to an ERROR + Telegram instead of shrugging."""
    mrt_config.assert_writable(path)     # GUARD at the write site: also covers explicit-path callers
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=default)
    last: Optional[Exception] = None
    for i in range(max(1, retries)):
        try:
            os.replace(tmp, path)
            return True
        except (PermissionError, OSError) as exc:      # a reader holds the target -> transient on Windows
            last = exc
            time.sleep(backoff_s * (i + 1))
    logging.getLogger("maker_rt").warning(
        "[MAKER_RT] atomic write to %s blocked after %d retries (%s) - skipped this write, not crashing.",
        path, retries, last)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


#: Public name for the atomic writer. EVERY runtime persister in the package goes through this one
#: function: per-pid tmp, retry, ``assert_writable``, and a truthful return value. Seven persisters in
#: pregame_exec used to hand-roll ``path + ".tmp"`` instead, which collides between an old and a new
#: process during the ~11-21 restarts a working day sees.
atomic_json = _atomic_json


def preserve_unreadable(path: str, log: Any = None, *, what: str = "state") -> Optional[str]:
    """Rename a file we could not parse to ``<path>.unreadable.bak`` and return the new name.

    An unreadable file is NOT an absent one, and the difference is money. Every loader here answers a
    parse failure with a default — ``{}``, an empty set — and the next persist then writes that default
    back over the only copy. That is exactly how one BOM wiped $329.96 of committed stake on
    2026-07-28: the read failed silently, the day started at zero, and the zeros were persisted. Moving
    the bytes aside keeps them recoverable by hand instead of letting the next write destroy them."""
    try:
        if not os.path.exists(path):
            return None
        bak = f"{path}.unreadable.bak"
        os.replace(path, bak)
        if log:
            log.error("[MAKER_RT] could not parse %s at %s — moved it to %s so its contents survive; "
                      "starting from defaults. RECOVER BY HAND if those numbers mattered.",
                      what, path, bak)
        return bak
    except OSError as exc:
        if log:
            log.error("[MAKER_RT] could not parse %s at %s AND could not move it aside (%s) — the next "
                      "write will overwrite it.", what, path, exc)
        return None


def _runstate_path() -> str:
    return mrt_config.runtime_path("runstate")


def bump_restart(now: datetime) -> int:
    """Increment (and return) today's maker_rt process-start count — persisted across the fresh
    interpreter each restart spawns, so a spike is a visible CRASH-LOOP signal on the panel. Rolls at
    UTC midnight. Best-effort; never raises."""
    day = now.strftime("%Y%m%d")
    obj: Any = {}
    try:
        with open(_runstate_path(), "r", encoding="utf-8-sig") as fh:
            obj = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        obj = {}
    if not isinstance(obj, dict) or obj.get("day") != day:
        obj = {"day": day, "restarts": 0}
    obj["restarts"] = int(obj.get("restarts", 0)) + 1
    _atomic_json(_runstate_path(), obj)
    return int(obj["restarts"])


def _tuning_path() -> str:
    return mrt_config.runtime_path("tuning")


def load_tuning(log: Any = None) -> dict:
    """Read the persisted cross-restart tuning counters. Never raises; missing -> empty.

    UNREADABLE is handled differently from MISSING, because this file carries lifetime settled pnl and
    the realized-locked-net windows. Returning ``{}`` for a parse error zeroes those in memory, and the
    very next ``persist_tuning`` writes the zeros back over the only copy — a silent lifetime-pnl reset
    with no event anywhere. So read ``utf-8-sig`` (a hand-edit on Windows very likely carries a BOM, and
    a BOM has already cost this system a day's committed stake), and if it still will not parse, move
    the bytes aside via ``preserve_unreadable`` and scream."""
    path = _tuning_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            obj = json.load(fh)
        if isinstance(obj, dict):
            return obj
    except (ValueError, OSError, TypeError):
        pass
    preserve_unreadable(path, log, what="the tuning counters (lifetime settled pnl)")
    return {}


def read_restarts(now: datetime) -> int:
    """Today's persisted restart count (0 when absent / a prior day). Never raises."""
    day = now.strftime("%Y%m%d")
    try:
        with open(_runstate_path(), "r", encoding="utf-8-sig") as fh:
            obj = json.load(fh)
        return int(obj.get("restarts", 0)) if isinstance(obj, dict) and obj.get("day") == day else 0
    except (FileNotFoundError, ValueError, OSError, TypeError):
        return 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
