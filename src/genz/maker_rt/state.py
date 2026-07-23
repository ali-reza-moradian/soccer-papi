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
    "drift_1", "drift_5", "drift_30", "reason",
]


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
        pct = lambda c: round(c / self.achv_n, 4) if self.achv_n else 0.0   # noqa: E731
        return {
            "n": self.achv_n, "gated": self.achv_gated,           # n = rails_ok samples in the ladder
            "p50": _pctl(self.achv_reservoir, 0.5), "p90": _pctl(self.achv_reservoir, 0.9),
            "share_ge_0": pct(self.achv_ge0), "share_ge_25bp": pct(self.achv_ge25),
            "share_ge_50bp": pct(self.achv_ge50), "share_ge_100bp": pct(self.achv_ge100),
        }


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
    _tuning_saved_ts: float = 0.0                      # last persist (see maybe_persist_tuning)
    gates: dict = field(default_factory=dict)          # {"pre": bool, "inplay": bool} — armed states, set at startup
    live: dict = field(default_factory=dict)           # PRE-GAME live snapshot (open_quotes/stake/fills/pnl/halt/feed_ok)
    buckets: dict = field(default_factory=dict)        # (sport, phase) -> _Bucket
    log: Any = None                                    # optional logger (artifact-guard WARNINGs)
    _rng: Any = field(default_factory=lambda: random.Random(1234))

    def _roll(self, day: str) -> None:
        if day != self.day:
            keep = (self.log, self.restarts_today, self.gates, self.live,   # survive the daily reset
                    self.lifetime_fills, self.lifetime_unwinds, self.recent_outcomes,
                    self.lifetime_quotes, self.recent_locked_nets)
            self.__init__(day=day)  # type: ignore[misc]
            (self.log, self.restarts_today, self.gates, self.live,
             self.lifetime_fills, self.lifetime_unwinds, self.recent_outcomes,
             self.lifetime_quotes, self.recent_locked_nets) = keep

    def _bucket(self, sport: str, phase: str) -> _Bucket:
        return self.buckets.setdefault((str(sport or "?"), str(phase or "pre")), _Bucket())

    # -- cross-restart tuning counters ---------------------------------------
    def load_tuning(self) -> None:
        """Restore the cross-restart counters at startup. Without this the 'last 20 fills' windows
        would reset on every deploy (~10x/day) and never accumulate enough fills to judge target_net."""
        obj = load_tuning()
        if not obj:
            return
        self.lifetime_quotes = int(obj.get("lifetime_quotes", 0) or 0)
        self.lifetime_fills = int(obj.get("lifetime_fills", 0) or 0)
        self.lifetime_unwinds = int(obj.get("lifetime_unwinds", 0) or 0)
        self.recent_outcomes = deque((int(v) for v in (obj.get("recent_outcomes") or [])),
                                     maxlen=_TUNING_WINDOW)
        self.recent_locked_nets = deque((float(v) for v in (obj.get("recent_locked_nets") or [])),
                                        maxlen=_TUNING_WINDOW)

    def persist_tuning(self) -> None:
        """Write the cross-restart counters (atomic, best-effort — never blocks trading)."""
        _atomic_json(_tuning_path(), {
            "lifetime_quotes": self.lifetime_quotes,
            "lifetime_fills": self.lifetime_fills,
            "lifetime_unwinds": self.lifetime_unwinds,
            "recent_outcomes": list(self.recent_outcomes),
            "recent_locked_nets": list(self.recent_locked_nets),
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
        elif ev in ("hedge_declined", "hedge_unwound", "unwind_FAILED"):
            # the fill required an EXIT (we paid the unwind toll). unwind_cost is on the row here, BEFORE
            # _append_csv drops it (it's not a CSV column — realized_pnl_usd carries -cost to the CSV).
            self.n_unwinds += 1; self.lifetime_unwinds += 1
            self.unwind_cost_today += float(row.get("unwind_cost") or 0.0)
            self.recent_outcomes.append(1)
            self.persist_tuning()                           # ditto — a paid exit toll must survive a deploy
        elif ev == "fill_drift":
            for src, dst_flat, dst_b in ((row.get("drift_1"), self.drift1, b.drift1),
                                         (row.get("drift_5"), self.drift5, b.drift5),
                                         (row.get("drift_30"), self.drift30, b.drift30)):
                if src is not None and src != "":
                    dst_flat.append(float(src)); dst_b.append(float(src))
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

    def _append_csv(self, row: dict, now: datetime) -> None:
        path = mrt_config.events_path_for(now.strftime("%Y%m%d"))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new = not os.path.exists(path)
        full = {c: "" for c in CSV_COLUMNS}
        full.update({k: v for k, v in row.items() if k in CSV_COLUMNS})
        full["ts"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        full["day"] = now.strftime("%Y%m%d")
        with open(path, "a", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(full)

    # -- summary + heartbeat -------------------------------------------------
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
            "pnl_today": round(self.pnl_today, 4), "restarts_today": self.restarts_today,
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
            "by_sport": self._by_sport(), "by_phase": self._by_phase(),
        }

    def write_summary(self, mode: str, sockets: dict, now: datetime,
                      path: Optional[str] = None) -> None:
        _atomic_json(path or mrt_config.SUMMARY_PATH, self.summary(mode, sockets, now))

    def heartbeat(self, mode: str, sockets: dict, open_quotes: int, now: datetime) -> dict:
        hb = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "schema": SCHEMA, "mode": mode,
              "sockets": dict(sockets), "open_quotes": open_quotes,
              "fills_today": self.n_fills, "pnl_today": round(self.pnl_today, 4),
              "restarts_today": self.restarts_today, "gates": dict(self.gates),
              "live": dict(self.live)}
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
        _atomic_json(path or mrt_config.HEARTBEAT_PATH,
                     self.heartbeat(mode, sockets, open_quotes, now))


def _atomic_json(path: str, obj: dict, *, retries: int = 5, backoff_s: float = 0.04) -> None:
    """Atomically write ``obj`` as JSON. ROBUST on Windows: ``os.replace`` raises PermissionError when a
    reader (the panel HTTP server / antivirus) momentarily holds the target open — the #1 crash of the
    weekend (50/51 tracebacks) took the whole maker process down here. Retry with a short backoff, and
    if still blocked, WARN and skip this write rather than crash. A per-pid tmp avoids old/new-process
    collisions during a restart."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    last: Optional[Exception] = None
    for i in range(max(1, retries)):
        try:
            os.replace(tmp, path)
            return
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


def _runstate_path() -> str:
    return os.path.join(mrt_config.OPS_DIR, RUNSTATE_NAME)


def bump_restart(now: datetime) -> int:
    """Increment (and return) today's maker_rt process-start count — persisted across the fresh
    interpreter each restart spawns, so a spike is a visible CRASH-LOOP signal on the panel. Rolls at
    UTC midnight. Best-effort; never raises."""
    day = now.strftime("%Y%m%d")
    obj: Any = {}
    try:
        with open(_runstate_path(), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        obj = {}
    if not isinstance(obj, dict) or obj.get("day") != day:
        obj = {"day": day, "restarts": 0}
    obj["restarts"] = int(obj.get("restarts", 0)) + 1
    _atomic_json(_runstate_path(), obj)
    return int(obj["restarts"])


def _tuning_path() -> str:
    return os.path.join(mrt_config.OPS_DIR, TUNING_NAME)


def load_tuning() -> dict:
    """Read the persisted cross-restart tuning counters. Never raises; missing/corrupt -> empty."""
    try:
        with open(_tuning_path(), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (FileNotFoundError, ValueError, OSError, TypeError):
        return {}


def read_restarts(now: datetime) -> int:
    """Today's persisted restart count (0 when absent / a prior day). Never raises."""
    day = now.strftime("%Y%m%d")
    try:
        with open(_runstate_path(), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return int(obj.get("restarts", 0)) if isinstance(obj, dict) and obj.get("day") == day else 0
    except (FileNotFoundError, ValueError, OSError, TypeError):
        return 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
