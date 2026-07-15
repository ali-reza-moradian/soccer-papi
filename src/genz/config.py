"""GenZ configuration + isolated data paths (data/genz/).

Reads ONLY the ``genz:`` block of config.yaml (never the scanner's window/secret logic). Execution
itself is delegated to the EXECUTOR, so the trade safety switches live in the ``executor:`` block and
are loaded via src/executor/config.py — GenZ never duplicates or overrides them. With the executor
defaults (enabled:false / dry_run:true) GenZ measures only.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")

# All GenZ state is isolated here — no shared mutable state with the scanner or the executor.
GENZ_DIR = os.path.join(REPO_ROOT, "data", "genz")
MATCH_TREE_PATH = os.path.join(GENZ_DIR, "match_tree.json")
TREE_META_PATH = os.path.join(GENZ_DIR, "tree_meta.json")
ARBS_CSV_PATH = os.path.join(GENZ_DIR, "genz_arbs.csv")           # legacy single file (still read)
HEARTBEAT_PATH = os.path.join(GENZ_DIR, "genz_heartbeat.json")
SNAPSHOT_PATH = os.path.join(GENZ_DIR, "genz_snapshot.json")      # EVERY priced market each cycle (dashboard)
PAPERMAKER_SUMMARY_PATH = os.path.join(GENZ_DIR, "papermaker_summary.json")   # paper-maker dry-run summary
PAPERMAKER_STATE_PATH = os.path.join(GENZ_DIR, "papermaker_state.json")       # open quotes/drift across cycles


def papermaker_path_for(now: Optional[datetime] = None) -> str:
    """The DATED paper-maker event log — data/genz/papermaker_YYYYMMDD.csv (quote/fill/expiry events)."""
    now = now or datetime.now(timezone.utc)
    return os.path.join(GENZ_DIR, f"papermaker_{now.strftime('%Y%m%d')}.csv")


def arbs_path_for(now: Optional[datetime] = None) -> str:
    """The DATED genz_arbs file for the day (rotation) — data/genz/genz_arbs_YYYYMMDD.csv — so a
    multi-day run doesn't grow one giant file. The report/dashboard read across all of them."""
    now = now or datetime.now(timezone.utc)
    return os.path.join(GENZ_DIR, f"genz_arbs_{now.strftime('%Y%m%d')}.csv")


def arbs_csv_paths() -> list[str]:
    """Every genz_arbs csv (the legacy base + all dated files), sorted — the full evidence base."""
    return sorted(glob.glob(os.path.join(GENZ_DIR, "genz_arbs*.csv")))

# Every per-type Kalshi WC series we enumerate per game (KXWCGAME first = the 1x2 spine). Any other
# KXWC* series discovered for a game's event suffix is pulled too (tree_builder is not limited to
# this list); this is the seed set.
DEFAULT_KALSHI_SERIES = [
    "KXWCGAME", "KXWCTOTAL", "KXWCTEAMTOTAL", "KXWCSPREAD", "KXWC1HSPREAD", "KXWC2HSPREAD",
    "KXWCBTTS", "KXWC1HBTTS", "KXWC2HBTTS", "KXWCCORNERS", "KXWCTCORNERS", "KXWCSCORE",
    "KXWCGOAL", "KXWCSOA", "KXWC1H", "KXWC2H", "KXWCFTTS", "KXWC1HTOTAL", "KXWC2HTOTAL",
    "KXWCADVANCE",
]


def ensure_dirs() -> None:
    """Create data/genz/ if absent. Safe to call repeatedly."""
    os.makedirs(GENZ_DIR, exist_ok=True)


@dataclass
class GenzConfig:
    """Typed view of the ``genz:`` config block (missing keys -> safe defaults)."""
    lookahead_hours: float = 48.0          # how far ahead the tree builder discovers games
    interval_seconds: float = 20.0         # price-loop cycle target
    max_workers: int = 12                  # concurrent REST price fetches per cycle
    walk_stake_usd: float = 200.0          # stake to walk each book to (walk-to-stake fill)
    min_edge_pct: float = 1.0              # NET edge floor (%) to flag/attempt an arb — now gates the
                                           # executor's net_edge_pct (after Kalshi's ceil-to-cent fee)
    max_plausible_roi_pct: float = 8.0     # arbs above this ROI (or implied_cost<0.5) are pairing/staleness bugs -> rejected
    min_total_implied: float = 0.95        # totals O/U over+under must sum >= this (same line/period == ~1.0); below -> mismatch
    http_timeout_seconds: float = 15.0     # per-call HTTP timeout (fail fast; a hung call must not stall a 20s cycle)
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    gamma_base: str = "https://gamma-api.polymarket.com"
    clob_base: str = "https://clob.polymarket.com"
    poly_series_slug: str = "soccer-fifwc"
    kalshi_series: list = field(default_factory=lambda: list(DEFAULT_KALSHI_SERIES))
    poly_fee_rate: float = 0.05            # Polymarket sports taker rate (from bookmakers.poly_fee_rate)
    # PAPER MAKER (dry-run maker-edge measurement — never places an order):
    papermaker_enabled: bool = True
    papermaker_target_net_pct: float = 1.0   # min net edge (%) a hedged maker combo must clear
    papermaker_ref_shares: float = 100.0     # shares to walk the hedge book to when marking a fill


def _raw(config_path: str | None) -> dict[str, Any]:
    path = config_path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}


def _block(config_path: str | None) -> dict[str, Any]:
    blk = _raw(config_path).get("genz")
    return blk if isinstance(blk, dict) else {}


def load_genz_config(config_path: str | None = None, *, overrides: dict[str, Any] | None = None) -> GenzConfig:
    """Load ``genz:`` from config.yaml into a :class:`GenzConfig`. ``overrides`` (CLI flags) win."""
    data = dict(_block(config_path))
    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})
    cfg = GenzConfig()
    for name in GenzConfig.__dataclass_fields__:
        if name in data and data[name] is not None:
            setattr(cfg, name, data[name])
    # light coercion for the scalars (yaml may hand ints where floats are expected)
    cfg.lookahead_hours = float(cfg.lookahead_hours)
    cfg.interval_seconds = float(cfg.interval_seconds)
    cfg.max_workers = int(cfg.max_workers)
    cfg.walk_stake_usd = float(cfg.walk_stake_usd)
    cfg.min_edge_pct = float(cfg.min_edge_pct)
    cfg.max_plausible_roi_pct = float(cfg.max_plausible_roi_pct)
    cfg.min_total_implied = float(cfg.min_total_implied)
    cfg.http_timeout_seconds = float(cfg.http_timeout_seconds)
    # Cross-block reads: the shared Polymarket fee rate and the papermaker: block.
    raw = _raw(config_path)
    bk = raw.get("bookmakers") if isinstance(raw.get("bookmakers"), dict) else {}
    if bk.get("poly_fee_rate") is not None:
        cfg.poly_fee_rate = float(bk["poly_fee_rate"])
    pm = raw.get("papermaker") if isinstance(raw.get("papermaker"), dict) else {}
    if pm.get("enabled") is not None:
        cfg.papermaker_enabled = bool(pm["enabled"])
    if pm.get("target_net_pct") is not None:
        cfg.papermaker_target_net_pct = float(pm["target_net_pct"])
    if pm.get("ref_shares") is not None:
        cfg.papermaker_ref_shares = float(pm["ref_shares"])
    return cfg
