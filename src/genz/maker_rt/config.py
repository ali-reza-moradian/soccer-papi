"""maker_rt configuration — reads the ``maker_rt:`` block of config.yaml (safe defaults when absent).

SHADOW is the default posture: real websockets, paper quotes, ZERO orders. The live-order path is
fully built but HARD-LOCKED behind ``maker_rt.live.enabled`` AND an on-disk arm file AND a startup
self-check (see ``LiveGate``). Nothing here places an order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from .. import config as gz_config

REPO_ROOT = gz_config.REPO_ROOT
GENZ_DIR = gz_config.GENZ_DIR
OPS_DIR = os.path.join(REPO_ROOT, "data", "ops")

# Runtime artifacts (all under data/genz/, isolated per the maker_rt namespace).
HEARTBEAT_PATH = os.path.join(GENZ_DIR, "maker_rt_heartbeat.json")
SUMMARY_PATH = os.path.join(GENZ_DIR, "maker_rt_summary.json")


def events_path_for(day: str) -> str:
    """The dated per-event log — data/genz/maker_rt_YYYYMMDD.csv."""
    return os.path.join(GENZ_DIR, f"maker_rt_{day}.csv")


# --------------------------------------------------------------------------- #
# WebSocket endpoints (verified July 2026)                                       #
# --------------------------------------------------------------------------- #
POLY_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLY_USER_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
KALSHI_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"           # the path signed in the RSA handshake header


@dataclass
class LiveConfig:
    """The LIVE sub-block — every value is a HARD cap enforced before any order. Defaults are the
    locked, refuse-everything posture: enabled False, so the live path never arms in this build."""
    enabled: bool = False
    arm_file: str = os.path.join(OPS_DIR, "ARM_MAKER")
    quote_usd_max: float = 25.0
    max_open_quotes: int = 2
    max_fills_per_day: int = 6
    max_daily_loss_usd: float = 25.0
    hedge_timeout_ms: int = 3000
    unwind_on_hedge_fail: bool = True


@dataclass
class InplayLiveConfig:
    """The ``maker_rt.live_inplay:`` sub-block — the SECOND, INDEPENDENT live gate for IN-PLAY markets
    (the pre-game ``live`` gate is separate and unchanged). Defaults are the locked posture: enabled
    False, so the in-play live path never arms in this build. Rails here are STRICTER than pre-game."""
    enabled: bool = False
    arm_file: str = os.path.join(OPS_DIR, "ARM_MAKER_INPLAY")
    quote_usd_max: float = 25.0
    max_open_quotes: int = 1
    max_fills_per_day: int = 4
    max_daily_loss_usd: float = 20.0
    hedge_timeout_ms: int = 1500
    freeze_cooloff_s: float = 10.0   # place only after the node has been unfrozen AND both-books-fresh this long
    hedge_decline_floor: float = -0.010   # re-verify at fill: if walked hedge nets below this, decline+unwind


@dataclass
class InplayConfig:
    """The ``maker_rt.inplay:`` sub-block — admission horizon + the anti-phantom rails for in-play
    shadow quoting. Live is HARD-refused in-play regardless (see LiveGate)."""
    horizon_hours: dict = field(default_factory=lambda: {"soccer": 3.0, "mlb": 4.5, "tennis": 4.5, "ufc": 1.5})
    fresh_s: float = 10.0          # (a) both venues' books must have updated within this to quote/fill
    shock_move: float = 0.05       # (b) a mid move >= this within shock_window_s freezes the node
    shock_window_s: float = 10.0
    freeze_s: float = 30.0         #     freeze duration after a shock (disarm + no quotes)
    persist_ms: int = 1500         # (c) a direction must be continuously viable this long before arming

    def horizon_for(self, sport: str) -> float:
        return float(self.horizon_hours.get(sport, 3.0))


@dataclass
class MakerRtConfig:
    """Typed view of the ``maker_rt:`` block (missing keys -> safe defaults)."""
    max_games: int = 20                    # nearest-by-kickoff games to quote across both sports
    quote_usd: float = 100.0               # shadow quote notional per node/direction
    target_net: float = 0.010              # min net edge the maker combo must clear at the quote price
    debounce_ms: int = 250                 # quote-engine debounce
    expire_before_kickoff_s: int = 120     # cancel/expire all quotes at kickoff - this
    poly_fee_rate: float = 0.05            # Polymarket sports taker rate (hedge-fee model)
    head_poll_s: int = 60                  # stale-code guard: exit 0 on git HEAD change
    ping_s: int = 10                       # ws keepalive PING cadence (poly)
    drift_marks_s: tuple = (1, 5, 30)      # adverse-selection hedge-drift marks after a shadow fill
    # Kalshi series that charge a MAKER fee — never rest on the Kalshi side of these (verified list).
    kalshi_maker_fee_series: tuple = ()
    # PER-SPORT POLY-LEG CAP: skip a direction whenever the Polymarket leg's price (rest price when
    # resting Poly; hedge best ask when hedging Poly) exceeds the sport's cap. On a pre-event walkover
    # (tennis) or a cancel/draw/NC (ufc) the Poly leg settles 50c while the Kalshi leg refunds ~last
    # price, so the hedged pair's tail loss ~= max(0, poly-0.50) on that rare event. Capping at 0.65
    # bounds the tail to ~15c on a <0.5c-expected event vs the ~1c target edge. UFC's pre-event
    # cancellation frequency is HIGHER than tennis walkovers, so the cap is NOT optional for ufc. Only
    # the match_winner/fight_winner sports are in the map; other sports are uncapped.
    poly_leg_cap: dict = field(default_factory=lambda: {"tennis": 0.65, "ufc": 0.65})
    # DEPRECATED alias for poly_leg_cap['tennis'] — read at load for back-compat, logged once.
    tennis_max_poly_leg: Optional[float] = None
    inplay: InplayConfig = field(default_factory=InplayConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    live_inplay: InplayLiveConfig = field(default_factory=InplayLiveConfig)


def _raw(config_path: Optional[str] = None) -> dict[str, Any]:
    path = config_path or os.environ.get("CONFIG_PATH") or gz_config.DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}


def load_maker_rt_config(config_path: Optional[str] = None,
                         overrides: Optional[dict[str, Any]] = None) -> MakerRtConfig:
    """Load ``maker_rt:`` from config.yaml into a :class:`MakerRtConfig`. ``overrides`` win."""
    blk = _raw(config_path).get("maker_rt")
    blk = dict(blk) if isinstance(blk, dict) else {}
    if overrides:
        blk.update({k: v for k, v in overrides.items() if v is not None})
    cfg = MakerRtConfig()
    for name in ("max_games", "quote_usd", "target_net", "debounce_ms", "expire_before_kickoff_s",
                 "poly_fee_rate", "head_poll_s", "ping_s"):
        if blk.get(name) is not None:
            setattr(cfg, name, blk[name])
    cfg.max_games = int(cfg.max_games)
    cfg.quote_usd = float(cfg.quote_usd)
    cfg.target_net = float(cfg.target_net)
    cfg.debounce_ms = int(cfg.debounce_ms)
    cfg.expire_before_kickoff_s = int(cfg.expire_before_kickoff_s)
    cfg.poly_fee_rate = float(cfg.poly_fee_rate)
    cfg.head_poll_s = int(cfg.head_poll_s)
    cfg.ping_s = int(cfg.ping_s)
    # PER-SPORT poly-leg cap map, with the DEPRECATED tennis_max_poly_leg scalar as a back-compat alias.
    cap = dict(cfg.poly_leg_cap)
    if isinstance(blk.get("poly_leg_cap"), dict):
        cap.update({str(k): float(v) for k, v in blk["poly_leg_cap"].items()})
    if blk.get("tennis_max_poly_leg") is not None:
        cap["tennis"] = float(blk["tennis_max_poly_leg"])
        import logging
        logging.getLogger("maker_rt").warning(
            "[MAKER_RT] config maker_rt.tennis_max_poly_leg is DEPRECATED — use "
            "maker_rt.poly_leg_cap: {tennis: %.2f, ...}; honoring it for tennis.", cap["tennis"])
    cfg.poly_leg_cap = cap
    if blk.get("drift_marks_s"):
        cfg.drift_marks_s = tuple(int(x) for x in blk["drift_marks_s"])
    if blk.get("kalshi_maker_fee_series"):
        cfg.kalshi_maker_fee_series = tuple(str(x) for x in blk["kalshi_maker_fee_series"])
    # IN-PLAY rails sub-block.
    ip_blk = blk.get("inplay")
    ip_blk = dict(ip_blk) if isinstance(ip_blk, dict) else {}
    ic = InplayConfig()
    if isinstance(ip_blk.get("horizon_hours"), dict):
        ic.horizon_hours = {str(k): float(v) for k, v in ip_blk["horizon_hours"].items()}
    for name in ("fresh_s", "shock_move", "shock_window_s", "freeze_s"):
        if ip_blk.get(name) is not None:
            setattr(ic, name, float(ip_blk[name]))
    if ip_blk.get("persist_ms") is not None:
        ic.persist_ms = int(ip_blk["persist_ms"])
    cfg.inplay = ic
    live_blk = blk.get("live")
    live_blk = dict(live_blk) if isinstance(live_blk, dict) else {}
    lc = LiveConfig()
    for name in ("enabled", "arm_file", "quote_usd_max", "max_open_quotes", "max_fills_per_day",
                 "max_daily_loss_usd", "hedge_timeout_ms", "unwind_on_hedge_fail"):
        if live_blk.get(name) is not None:
            setattr(lc, name, live_blk[name])
    lc.enabled = bool(lc.enabled)
    lc.quote_usd_max = float(lc.quote_usd_max)
    lc.max_open_quotes = int(lc.max_open_quotes)
    lc.max_fills_per_day = int(lc.max_fills_per_day)
    lc.max_daily_loss_usd = float(lc.max_daily_loss_usd)
    lc.hedge_timeout_ms = int(lc.hedge_timeout_ms)
    lc.unwind_on_hedge_fail = bool(lc.unwind_on_hedge_fail)
    cfg.live = lc
    # LIVE-INPLAY — the second, independent gate (locked by default; enabled False).
    li_blk = blk.get("live_inplay")
    li_blk = dict(li_blk) if isinstance(li_blk, dict) else {}
    li = InplayLiveConfig()
    for name in ("enabled", "arm_file", "quote_usd_max", "max_open_quotes", "max_fills_per_day",
                 "max_daily_loss_usd", "hedge_timeout_ms", "freeze_cooloff_s", "hedge_decline_floor"):
        if li_blk.get(name) is not None:
            setattr(li, name, li_blk[name])
    li.enabled = bool(li.enabled)
    li.quote_usd_max = float(li.quote_usd_max)
    li.max_open_quotes = int(li.max_open_quotes)
    li.max_fills_per_day = int(li.max_fills_per_day)
    li.max_daily_loss_usd = float(li.max_daily_loss_usd)
    li.hedge_timeout_ms = int(li.hedge_timeout_ms)
    li.freeze_cooloff_s = float(li.freeze_cooloff_s)
    li.hedge_decline_floor = float(li.hedge_decline_floor)
    cfg.live_inplay = li
    return cfg


def ensure_dirs() -> None:
    os.makedirs(GENZ_DIR, exist_ok=True)
    os.makedirs(OPS_DIR, exist_ok=True)
