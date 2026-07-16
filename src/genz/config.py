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


# --------------------------------------------------------------------------- #
# Per-SPORT runtime artifact paths (soccer = the current filenames, byte-for-byte;   #
# every other sport gets its OWN files so nothing shared is ever overwritten).   #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SportPaths:
    """The isolated set of runtime files for one sport. Soccer maps to the EXISTING constants so its
    output is unchanged; MLB (and any future sport) gets a distinct file per artifact."""
    sport: str
    tree_path: str
    meta_path: str
    snapshot_path: str
    heartbeat_path: str
    papermaker_summary_path: str
    papermaker_state_path: str
    arbs_prefix: str            # dated arbs file stem, e.g. 'genz_arbs' -> genz_arbs_YYYYMMDD.csv
    papermaker_prefix: str      # dated papermaker file stem, e.g. 'papermaker' -> papermaker_YYYYMMDD.csv

    def arbs_path_for(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(timezone.utc)
        return os.path.join(GENZ_DIR, f"{self.arbs_prefix}_{now.strftime('%Y%m%d')}.csv")

    def papermaker_path_for(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(timezone.utc)
        return os.path.join(GENZ_DIR, f"{self.papermaker_prefix}_{now.strftime('%Y%m%d')}.csv")


_SOCCER_PATHS = SportPaths(
    "soccer", MATCH_TREE_PATH, TREE_META_PATH, SNAPSHOT_PATH, HEARTBEAT_PATH,
    PAPERMAKER_SUMMARY_PATH, PAPERMAKER_STATE_PATH, "genz_arbs", "papermaker")
_MLB_PATHS = SportPaths(
    "mlb",
    os.path.join(GENZ_DIR, "mlb_tree.json"), os.path.join(GENZ_DIR, "mlb_tree_meta.json"),
    os.path.join(GENZ_DIR, "genz_snapshot_mlb.json"), os.path.join(GENZ_DIR, "genz_heartbeat_mlb.json"),
    os.path.join(GENZ_DIR, "papermaker_summary_mlb.json"), os.path.join(GENZ_DIR, "papermaker_state_mlb.json"),
    "genz_arbs_mlb", "papermaker_mlb")
SPORT_PATHS: dict[str, SportPaths] = {"soccer": _SOCCER_PATHS, "mlb": _MLB_PATHS}


def paths_for_sport(sport: str = "soccer") -> SportPaths:
    """The runtime-file set for a sport (defaults to soccer's existing filenames)."""
    return SPORT_PATHS.get(sport, _SOCCER_PATHS)

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
    # MULTI-SPORT: which sport this config drives + its Kalshi throttle. soccer is unchanged; mlb reads
    # the genz.mlb sub-block (lookahead/interval/series/slug), inheriting everything else from genz.
    sport: str = "soccer"
    kalshi_min_interval: Optional[float] = None   # per-process Kalshi request spacing (None = client default)


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


# MLB defaults when the genz.mlb sub-block is absent/partial (spec section 0). Everything NOT listed
# here (walk_stake_usd / min_edge_pct / max_plausible_roi_pct / min_total_implied / http_timeout /
# max_workers / poly_fee_rate / papermaker_*) is INHERITED from the base genz block unchanged.
_MLB_DEFAULTS: dict[str, Any] = {
    "lookahead_hours": 24.0,
    "interval_seconds": 45.0,
    "kalshi_series": ["KXMLBGAME", "KXMLBTOTAL"],
    "poly_series_slug": "mlb",     # from genz.mlb.poly_slug_prefix
}


def _apply_mlb_block(cfg: GenzConfig, config_path: str | None) -> None:
    """Overlay the genz.mlb sub-block onto a base (genz-defaults) config: MLB lookahead/interval/series/
    slug + optional inherited-override keys and the per-process Kalshi throttle. Missing keys fall back
    to _MLB_DEFAULTS, then to whatever the base genz block already set."""
    cfg.sport = "mlb"
    mlb = _block(config_path).get("mlb")
    mlb = mlb if isinstance(mlb, dict) else {}
    cfg.lookahead_hours = float(mlb.get("lookahead_hours", _MLB_DEFAULTS["lookahead_hours"]))
    cfg.interval_seconds = float(mlb.get("interval_seconds", _MLB_DEFAULTS["interval_seconds"]))
    series = mlb.get("kalshi_series") or _MLB_DEFAULTS["kalshi_series"]
    cfg.kalshi_series = list(series)
    cfg.poly_series_slug = str(mlb.get("poly_slug_prefix") or _MLB_DEFAULTS["poly_series_slug"])
    # Inherited-but-overridable knobs: use the mlb value only when present, else keep the genz base.
    for key in ("walk_stake_usd", "min_edge_pct", "max_plausible_roi_pct", "min_total_implied",
                "http_timeout_seconds", "max_workers"):
        if mlb.get(key) is not None:
            setattr(cfg, key, (int if key == "max_workers" else float)(mlb[key]))
    if mlb.get("kalshi_min_interval") is not None:
        cfg.kalshi_min_interval = float(mlb["kalshi_min_interval"])


def load_genz_config(config_path: str | None = None, *, overrides: dict[str, Any] | None = None,
                     sport: str = "soccer") -> GenzConfig:
    """Load ``genz:`` from config.yaml into a :class:`GenzConfig`. ``overrides`` (CLI flags) win.
    ``sport='mlb'`` overlays the genz.mlb sub-block on top of the genz defaults (soccer is unchanged)."""
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
    # MLB: overlay the genz.mlb sub-block LAST (after the base genz block is fully loaded), so MLB
    # inherits every unspecified knob from genz and only overrides what it lists.
    if sport == "mlb":
        _apply_mlb_block(cfg, config_path)
    return cfg
