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
_TENNIS_PATHS = SportPaths(
    "tennis",
    os.path.join(GENZ_DIR, "tennis_tree.json"), os.path.join(GENZ_DIR, "tennis_tree_meta.json"),
    os.path.join(GENZ_DIR, "genz_snapshot_tennis.json"), os.path.join(GENZ_DIR, "genz_heartbeat_tennis.json"),
    os.path.join(GENZ_DIR, "papermaker_summary_tennis.json"), os.path.join(GENZ_DIR, "papermaker_state_tennis.json"),
    "genz_arbs_tennis", "papermaker_tennis")
_UFC_PATHS = SportPaths(
    "ufc",
    os.path.join(GENZ_DIR, "ufc_tree.json"), os.path.join(GENZ_DIR, "ufc_tree_meta.json"),
    os.path.join(GENZ_DIR, "genz_snapshot_ufc.json"), os.path.join(GENZ_DIR, "genz_heartbeat_ufc.json"),
    os.path.join(GENZ_DIR, "papermaker_summary_ufc.json"), os.path.join(GENZ_DIR, "papermaker_state_ufc.json"),
    "genz_arbs_ufc", "papermaker_ufc")
SPORT_PATHS: dict[str, SportPaths] = {"soccer": _SOCCER_PATHS, "mlb": _MLB_PATHS,
                                      "tennis": _TENNIS_PATHS, "ufc": _UFC_PATHS}


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
class Competition:
    """One soccer competition the tree builder discovers (POST-WC generalization). ``kalshi_series`` is
    either the sentinel ``"AUTO"`` (discover the game-winner series from the Kalshi catalog, report +
    persist to soccer_series_map.json) or an explicit list of series tickers. ``poly_slug_prefix`` is
    the Polymarket event-slug stem (e.g. 'mls', 'fifwc'); ``poly_tag`` is an optional discovery hint."""
    name: str
    kalshi_series: Any = "AUTO"
    poly_slug_prefix: str = ""
    poly_tag: str = ""
    enabled: bool = True


def _parse_competitions(raw_list: Any) -> list:
    """Parse ``genz.soccer.competitions`` (a list of dicts) into Competition objects. Malformed/nameless
    entries are skipped. An empty/absent list -> the LEGACY World Cup path (byte-identical golden)."""
    out: list = []
    for c in raw_list or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        out.append(Competition(name=str(c["name"]), kalshi_series=c.get("kalshi_series", "AUTO"),
                               poly_slug_prefix=str(c.get("poly_slug_prefix") or ""),
                               poly_tag=str(c.get("poly_tag") or ""), enabled=bool(c.get("enabled", True))))
    return out


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
    # MULTI-SPORT: which sport this config drives + its Kalshi throttle. soccer is unchanged; mlb/tennis
    # read their genz.<sport> sub-block (lookahead/interval/series/slug), inheriting everything else.
    sport: str = "soccer"
    kalshi_min_interval: Optional[float] = None   # per-process Kalshi request spacing (None = client default)
    poly_tours: list = field(default_factory=lambda: ["atp", "wta"])   # tennis: Poly series slugs to scan
    poly_sport: str = "ufc"                                             # ufc: the Poly series slug scanned per build
    # IN-PLAY DATA COLLECTION (shadow only — live TRADING stays locked; in-play is additionally
    # hard-forbidden for live by assertion). A started game within kickoff+horizon is PRICED and flagged
    # phase="inplay" for data collection (would_trade FORCED false, executor SKIPPED); beyond the horizon
    # it is dropped. The horizon is per-sport (games run different lengths).
    inplay_collect: bool = True
    inplay_horizon_hours: float = 3.0
    # SOCCER POST-WC: competition-driven discovery. Empty (the dataclass default, used by the golden) ->
    # the legacy World Cup path, byte-identical. Populated from genz.soccer.competitions in config.yaml.
    competitions: list = field(default_factory=list)


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


# Per-sport in-play horizon defaults (hours after kickoff a started game is still PRICED for shadow
# collection). Games run different lengths: MLB ~3-4h, a tennis match ~1.5-4h, a UFC fight ~a few min
# but the card slot slides, soccer ~2h. These are conservative upper bounds.
_INPLAY_HORIZON_DEFAULTS: dict[str, float] = {"soccer": 3.0, "mlb": 4.5, "tennis": 4.5, "ufc": 1.5}


def _apply_inplay(cfg: GenzConfig, block: dict[str, Any], sport: str) -> None:
    """Overlay a sport's ``inplay:`` sub-block ({collect, horizon_hours}) onto cfg, defaulting the
    horizon to the per-sport default. Missing sub-block -> collect on, default horizon."""
    ip = block.get("inplay") if isinstance(block.get("inplay"), dict) else {}
    default_h = _INPLAY_HORIZON_DEFAULTS.get(sport, 3.0)
    cfg.inplay_collect = bool(ip.get("collect", True))
    cfg.inplay_horizon_hours = float(ip.get("horizon_hours", default_h))


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
    _apply_inplay(cfg, mlb, "mlb")


# TENNIS defaults when the genz.tennis sub-block is absent/partial. Everything NOT listed here is
# INHERITED from the base genz block unchanged (mirrors the MLB overlay).
_TENNIS_DEFAULTS: dict[str, Any] = {
    "lookahead_hours": 72.0,
    "interval_seconds": 60.0,
    "kalshi_series": ["KXATPMATCH", "KXWTAMATCH"],
    "poly_tours": ["atp", "wta"],
}


def _apply_tennis_block(cfg: GenzConfig, config_path: str | None) -> None:
    """Overlay the genz.tennis sub-block: lookahead/interval/series/tours + optional inherited-override
    knobs and the per-process Kalshi throttle. Missing keys fall back to _TENNIS_DEFAULTS then genz."""
    cfg.sport = "tennis"
    ten = _block(config_path).get("tennis")
    ten = ten if isinstance(ten, dict) else {}
    cfg.lookahead_hours = float(ten.get("lookahead_hours", _TENNIS_DEFAULTS["lookahead_hours"]))
    cfg.interval_seconds = float(ten.get("interval_seconds", _TENNIS_DEFAULTS["interval_seconds"]))
    cfg.kalshi_series = list(ten.get("kalshi_series") or _TENNIS_DEFAULTS["kalshi_series"])
    cfg.poly_tours = list(ten.get("poly_tours") or _TENNIS_DEFAULTS["poly_tours"])
    cfg.poly_series_slug = cfg.poly_tours[0] if cfg.poly_tours else "atp"   # nominal; discovery uses poly_tours
    for key in ("walk_stake_usd", "min_edge_pct", "max_plausible_roi_pct", "min_total_implied",
                "http_timeout_seconds", "max_workers"):
        if ten.get(key) is not None:
            setattr(cfg, key, (int if key == "max_workers" else float)(ten[key]))
    if ten.get("kalshi_min_interval") is not None:
        cfg.kalshi_min_interval = float(ten["kalshi_min_interval"])
    _apply_inplay(cfg, ten, "tennis")


# UFC defaults when the genz.ufc sub-block is absent/partial. Everything NOT listed is INHERITED from
# genz (mirrors the MLB/tennis overlays).
_UFC_DEFAULTS: dict[str, Any] = {
    "lookahead_hours": 168.0,          # cards are weekly — a short window is empty most days
    "interval_seconds": 90.0,
    "kalshi_series": ["KXUFCFIGHT"],
    "poly_sport": "ufc",
}


def _apply_ufc_block(cfg: GenzConfig, config_path: str | None) -> None:
    """Overlay the genz.ufc sub-block: lookahead/interval/series/poly_sport + optional inherited-override
    knobs and the per-process Kalshi throttle. Missing keys fall back to _UFC_DEFAULTS then genz."""
    cfg.sport = "ufc"
    ufc = _block(config_path).get("ufc")
    ufc = ufc if isinstance(ufc, dict) else {}
    cfg.lookahead_hours = float(ufc.get("lookahead_hours", _UFC_DEFAULTS["lookahead_hours"]))
    cfg.interval_seconds = float(ufc.get("interval_seconds", _UFC_DEFAULTS["interval_seconds"]))
    cfg.kalshi_series = list(ufc.get("kalshi_series") or _UFC_DEFAULTS["kalshi_series"])
    cfg.poly_sport = str(ufc.get("poly_sport") or _UFC_DEFAULTS["poly_sport"])
    cfg.poly_series_slug = cfg.poly_sport                                   # nominal; discovery uses poly_sport
    for key in ("walk_stake_usd", "min_edge_pct", "max_plausible_roi_pct", "min_total_implied",
                "http_timeout_seconds", "max_workers"):
        if ufc.get(key) is not None:
            setattr(cfg, key, (int if key == "max_workers" else float)(ufc[key]))
    if ufc.get("kalshi_min_interval") is not None:
        cfg.kalshi_min_interval = float(ufc["kalshi_min_interval"])
    _apply_inplay(cfg, ufc, "ufc")


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
    # MLB / TENNIS: overlay the genz.<sport> sub-block LAST (after the base genz block is fully loaded),
    # so the sport inherits every unspecified knob from genz and only overrides what it lists.
    if sport == "mlb":
        _apply_mlb_block(cfg, config_path)
    elif sport == "tennis":
        _apply_tennis_block(cfg, config_path)
    elif sport == "ufc":
        _apply_ufc_block(cfg, config_path)
    else:
        soc = _block(config_path).get("soccer")
        soc = soc if isinstance(soc, dict) else {}
        cfg.competitions = _parse_competitions(soc.get("competitions"))
        _apply_inplay(cfg, _block(config_path), "soccer")   # soccer reads genz.inplay directly
    return cfg
