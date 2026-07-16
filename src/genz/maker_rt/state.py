"""Runtime state I/O: the heartbeat, the dated per-event CSV, and the panel summary.

All three live under data/genz/. The heartbeat is written every loop so the panel can flag a stale
process; the summary aggregates the day's shadow (or live) activity for the MAKER strip; the CSV is
the append-only event log (quote/reprice/expire/behind/fill/hedge/drift).
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import config as mrt_config

SCHEMA = 1

CSV_COLUMNS = [
    "ts", "day", "event", "mode", "sport", "game", "market_key", "side", "direction",
    "rest_venue", "hedge_venue", "quote_price", "size", "floor", "at_best", "hedge_ask",
    "net_at_quote", "queue_ahead", "trigger", "quote_age_s", "hedge_avg", "hedge_fee",
    "locked_net", "drift_1", "drift_5", "drift_30", "reason",
]


def _median(xs: list) -> Optional[float]:
    vs = sorted(v for v in xs if v is not None)
    if not vs:
        return None
    n = len(vs)
    return round((vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2.0), 4)


@dataclass
class MakerState:
    """Day-scoped aggregates + the file writers (heartbeat / CSV / summary)."""
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

    def _roll(self, day: str) -> None:
        if day != self.day:
            self.__init__(day=day)  # type: ignore[misc]

    # -- events -------------------------------------------------------------
    def record(self, row: dict, now: datetime) -> None:
        """Update aggregates from an event row and append it to the dated CSV."""
        day = now.strftime("%Y%m%d")
        self._roll(day)
        ev = row.get("event")
        if ev == "quote":
            self.n_quotes += 1
            if row.get("at_best"):
                self.at_best_hits += 1
        elif ev == "reprice":
            self.n_reprices += 1
        elif ev == "behind":
            self.n_behind += 1
        elif ev == "expire":
            self.n_expired += 1
        elif ev == "fill":
            self.n_fills += 1
            if row.get("locked_net") is not None:
                self.fill_nets.append(float(row["locked_net"]))
                self.pnl_today += float(row.get("locked_pnl") or 0.0)
        elif ev == "drift":
            for src, dst in ((row.get("drift_1"), self.drift1), (row.get("drift_5"), self.drift5),
                             (row.get("drift_30"), self.drift30)):
                if src is not None:
                    dst.append(float(src))
        self._append_csv(row, now)

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
