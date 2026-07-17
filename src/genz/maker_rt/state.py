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
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import config as mrt_config

SCHEMA = 2
_RESERVOIR_CAP = 5000            # bounded achievable-net sample reservoir per (sport, phase)

CSV_COLUMNS = [
    "ts", "day", "event", "mode", "sport", "phase", "game", "market_key", "side", "direction",
    "rest_venue", "hedge_venue", "quote_price", "size", "floor", "at_best", "hedge_ask",
    "net_at_quote", "achievable_net", "queue_ahead", "trigger", "quote_age_s", "hedge_avg", "hedge_fee",
    "locked_net", "drift_1", "drift_5", "drift_30", "reason",
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
    # ACHIEVABLE-NET accumulator (reservoir for percentiles + exact threshold counts).
    achv_n: int = 0
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
            "n": self.achv_n,
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
        agg.achv_n += b.achv_n; agg.achv_ge0 += b.achv_ge0; agg.achv_ge25 += b.achv_ge25
        agg.achv_ge50 += b.achv_ge50; agg.achv_ge100 += b.achv_ge100
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
    buckets: dict = field(default_factory=dict)        # (sport, phase) -> _Bucket
    _rng: Any = field(default_factory=lambda: random.Random(1234))

    def _roll(self, day: str) -> None:
        if day != self.day:
            self.__init__(day=day)  # type: ignore[misc]

    def _bucket(self, sport: str, phase: str) -> _Bucket:
        return self.buckets.setdefault((str(sport or "?"), str(phase or "pre")), _Bucket())

    # -- events -------------------------------------------------------------
    def record(self, row: dict, now: datetime) -> None:
        """Update aggregates from an event row and append it to the dated CSV."""
        day = now.strftime("%Y%m%d")
        self._roll(day)
        ev = row.get("event")
        b = self._bucket(row.get("sport"), row.get("phase"))
        if ev == "quote":
            self.n_quotes += 1; b.quotes += 1
            if row.get("at_best"):
                self.at_best_hits += 1; b.at_best_hits += 1
        elif ev == "reprice":
            self.n_reprices += 1; b.reprices += 1
        elif ev == "behind":
            self.n_behind += 1; b.behind += 1
        elif ev == "expire":
            self.n_expired += 1; b.expired += 1
        elif ev == "fill":
            self.n_fills += 1; b.fills += 1
            if row.get("locked_net") is not None:
                self.fill_nets.append(float(row["locked_net"]))
                b.fill_nets.append(float(row["locked_net"]))
                self.pnl_today += float(row.get("locked_pnl") or 0.0)
        elif ev == "drift":
            for src, dst_flat, dst_b in ((row.get("drift_1"), self.drift1, b.drift1),
                                         (row.get("drift_5"), self.drift5, b.drift5),
                                         (row.get("drift_30"), self.drift30, b.drift30)):
                if src is not None:
                    dst_flat.append(float(src)); dst_b.append(float(src))
        self._append_csv(row, now)

    def record_achievable(self, sport: str, phase: str, value: Optional[float], now: datetime) -> None:
        """Aggregate ONE achievable-net evaluation into the (sport, phase) bucket. NO CSV row (this
        fires thousands of times/day — the throttled sample is a separate event via record())."""
        if value is None:
            return
        self._roll(now.strftime("%Y%m%d"))
        self._bucket(sport, phase).add_achievable(float(value), self._rng)

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
            "pnl_today": round(self.pnl_today, 4),
            "by_sport": self._by_sport(), "by_phase": self._by_phase(),
        }

    def write_summary(self, mode: str, sockets: dict, now: datetime,
                      path: Optional[str] = None) -> None:
        _atomic_json(path or mrt_config.SUMMARY_PATH, self.summary(mode, sockets, now))

    def heartbeat(self, mode: str, sockets: dict, open_quotes: int, now: datetime) -> dict:
        return {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "schema": SCHEMA, "mode": mode,
                "sockets": dict(sockets), "open_quotes": open_quotes,
                "fills_today": self.n_fills, "pnl_today": round(self.pnl_today, 4)}

    def write_heartbeat(self, mode: str, sockets: dict, open_quotes: int, now: datetime,
                        path: Optional[str] = None) -> None:
        _atomic_json(path or mrt_config.HEARTBEAT_PATH,
                     self.heartbeat(mode, sockets, open_quotes, now))


def _atomic_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
