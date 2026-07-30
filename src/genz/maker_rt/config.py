"""maker_rt configuration — reads the ``maker_rt:`` block of config.yaml (safe defaults when absent).

SHADOW is the default posture: real websockets, paper quotes, ZERO orders. The live-order path is
fully built but HARD-LOCKED behind ``maker_rt.live.enabled`` AND an on-disk arm file AND a startup
self-check (see ``LiveGate``). Nothing here places an order.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from .. import config as gz_config

REPO_ROOT = gz_config.REPO_ROOT
GENZ_DIR = gz_config.GENZ_DIR
OPS_DIR = os.path.join(REPO_ROOT, "data", "ops")


# --------------------------------------------------------------------------- #
# THE runtime-path resolver — the ONE place a runtime write path is built        #
# --------------------------------------------------------------------------- #
# Every file the maker writes at runtime is named here and resolved through
# ``runtime_path()``. Nothing else may join a directory to a runtime filename: a second derivation is
# a second thing to remember to isolate, and forgetting cost us a corrupted trading ledger (a suite
# run injected 2,904 rows including 177 fake fills into the live events CSV, and the running maker
# then reported a fabricated 0.7% locked_net). An autouse fixture patched that, but a convention is
# not a guarantee — so the resolver also enforces the guard below.
#
# ``base`` is resolved from the MODULE ATTRIBUTES on every call (never captured at import), so
# monkeypatching GENZ_DIR / OPS_DIR actually redirects everything.
RUNTIME_FILES: dict[str, tuple[str, str]] = {
    "events":        ("genz", "maker_rt_{day}.csv"),
    "heartbeat":     ("genz", "maker_rt_heartbeat.json"),
    "summary":       ("genz", "maker_rt_summary.json"),
    "runstate":      ("ops",  "maker_rt_runstate.json"),
    "tuning":        ("ops",  "maker_rt_tuning.json"),
    "traded_tokens": ("ops",  "maker_rt_traded_tokens.json"),
    "orphan":        ("ops",  "maker_rt_ORPHAN.json"),
    "settled_ledger": ("ops", "maker_rt_settled_ledger.json"),
    "expected_positions": ("ops", "maker_rt_expected_positions.json"),
    "provisional_marks": ("ops", "maker_rt_provisional_marks.json"),
    "daily_caps":    ("ops",  "maker_rt_daily_caps.json"),
    # Bookings REFUSED by a booking-time invariant (impossible pair sum / edge above the sanity
    # ceiling). Latched like ORPHAN: halts live trading until a human clears the file.
    "quarantine":    ("ops",  "maker_rt_QUARANTINE.json"),
    # Settlements REFUSED by the sanity rails — real money whose numbers we don't trust. Queued for
    # manual reconciliation instead of being dropped (they used to just vanish).
    "refused_settlements": ("ops", "maker_rt_REFUSED_SETTLEMENTS.json"),
    "stop_all":      ("ops",  "STOP_ALL"),
}


class LiveStateWriteUnderTest(RuntimeError):
    """Raised when code running under pytest resolves or writes a LIVE runtime path."""


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _tmp_roots() -> list[str]:
    """Every directory we accept as 'a temp dir' (pytest's tmp_path lives under one of these)."""
    out: list[str] = []
    for r in (tempfile.gettempdir(), os.environ.get("PYTEST_DEBUG_TEMPROOT"),
              os.environ.get("TMPDIR"), os.environ.get("TEMP"), os.environ.get("TMP")):
        if not r:
            continue
        try:
            out.append(_norm(r))
        except (OSError, ValueError):     # pragma: no cover - unresolvable env value
            continue
    return out


def under_tmp(path: str) -> bool:
    """True iff ``path`` lives under a temp root (case/short-name normalised, drive-safe)."""
    p = _norm(path)
    for root in _tmp_roots():
        try:
            if os.path.commonpath([p, root]) == root:
                return True
        except ValueError:                # different drives on Windows -> not under it
            continue
    return False


def assert_writable(path: str) -> str:
    """THE GUARD. Under pytest, refuse any path that is not under a temp dir.

    Returns ``path`` unchanged in production (the env lookup short-circuits before any filesystem
    work). Under pytest it RAISES rather than writing, so a test can never touch live trading state —
    not by forgetting a fixture, not by passing an explicit path, not via a new call site."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return path
    if under_tmp(path):
        return path
    raise LiveStateWriteUnderTest(
        f"REFUSED: test tried to write LIVE maker_rt runtime state at {path!r}.\n"
        f"  Tests must never touch data/genz or data/ops — a suite run once injected 2,904 rows "
        f"(incl. 177 fake fills) into the live events ledger.\n"
        f"  tests/conftest.py points GENZ_DIR/OPS_DIR at tmp_path for every test; if you see this, "
        f"something resolved a path WITHOUT the module attributes (e.g. a value captured at import, "
        f"or a directory joined by hand instead of via config.runtime_path())."
    )


def runtime_path(kind: str, **fmt: Any) -> str:
    """Resolve a named runtime artifact. The ONLY sanctioned way to build a runtime write path."""
    try:
        base, name = RUNTIME_FILES[kind]
    except KeyError:
        raise KeyError(f"unknown runtime file {kind!r}; known: {sorted(RUNTIME_FILES)}") from None
    directory = GENZ_DIR if base == "genz" else OPS_DIR
    return assert_writable(os.path.join(directory, name.format(**fmt) if fmt else name))


def events_path_for(day: str) -> str:
    """The dated per-event log — data/genz/maker_rt_YYYYMMDD.csv."""
    return runtime_path("events", day=day)


def heartbeat_path() -> str:
    return runtime_path("heartbeat")


def summary_path() -> str:
    return runtime_path("summary")


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
    quote_usd_max: float = 5.0            # PILOT: rest-leg notional cap (hedged pair ~2x -> the per-bet limit)
    max_open_quotes: int = 2
    max_fills_per_day: int = 10
    max_daily_loss_usd: float = 25.0
    # SUM of ALL legs committed today (rest fills + hedges + unwinds). A new quote whose projected pair
    # stake would push the running total past this is REFUSED and quoting HALTS for the day (+Telegram).
    max_daily_stake_usd: float = 100.0
    # PER-PAIR cap ($): the rest leg + its WORST-CASE hedge (shares x hedge best-ask) for ONE bet.
    # quote_usd_max bounds the rest leg only; a cheap rest leg's hedge can be many multiples of it
    # (TBTOR rest $1.02, hedge $16.21). A quote whose projected pair exceeds this is REFUSED (no day-halt).
    max_pair_stake_usd: float = 25.0
    hedge_timeout_ms: int = 3000
    unwind_on_hedge_fail: bool = True
    # When position reconciliation finds an ORPHAN, market-sell it to flat before halting (default False =
    # halt + scream only; a human flattens). Even when true, live stays HALTED for manual review.
    auto_flatten: bool = False
    # BOUNDED AUTO-FLATTEN ($). An orphan whose WORST CASE (its full notional — what it costs if the
    # flatten sells for nothing) is at or under this is swept out at a capped price, proved flat against
    # the venue, booked, and quoting RESUMES; a larger one halts for a human exactly as before. Halting
    # the whole bot is the right answer to unbounded risk and the wrong answer to eleven cents: the
    # 2026-07-28 CSKA orphan was a $25 position that stopped trading for three hours. A flatten that
    # cannot prove flat still halts (fails closed). 0 disables it (always halt).
    auto_flatten_max_usd: float = 120.0
    # WS-INDEPENDENT FILL AUTHORITY: while ANY live order is open, poll its REST order status (and sweep
    # the account fill history) every this many seconds. The private websocket fill channel is only an
    # accelerator — this poll is the detector of record and needs no socket to be connected.
    fill_poll_s: float = 10.0
    # SLOT AGE-OUT: a resting order older than this (seconds) is repriced-or-cancelled so no order holds
    # one of the (scarce) max_open_quotes slots forever — a stuck order behind best fills nothing and
    # starves every other candidate. 0 disables the age-out.
    max_quote_age_s: float = 900.0
    # KALSHI WS FLAP GRACE (seconds): the Kalshi socket briefly drops `connected` on EVERY reconnect
    # (incl. the quiet-market ping probe). Because REST is the WS-INDEPENDENT fill authority, a brief WS
    # blip is not a fill-signal outage — so we only treat the Kalshi feed as DOWN (cancel resting
    # rest-kalshi quotes) after it has been continuously down this long. A reconnect inside the grace
    # preserves queue position. 0 disables the debounce (cancel on the first observed drop).
    kalshi_feed_grace_s: float = 20.0


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
    # FIRST-FILL soft circuit (replaces the calendar guard): after the day's first IN-PLAY fill, pause
    # in-play placement this long (pre-game unaffected) + Telegram the fill->hedge->locked_net chain.
    first_fill_pause_s: float = 120.0
    # Any in-play fill whose locked_net is <= this HALTS in-play for the rest of the day (pre-game continues).
    halt_locked_net: float = -0.020


@dataclass
class InplayConfig:
    """The ``maker_rt.inplay:`` sub-block — admission horizon + the anti-phantom rails for in-play
    shadow quoting. Live is HARD-refused in-play regardless (see LiveGate)."""
    horizon_hours: dict = field(default_factory=lambda: {"soccer": 3.0, "mlb": 4.5, "tennis": 4.5, "ufc": 1.5})
    fresh_s: float = 10.0          # (legacy) book-change freshness — superseded by conn_fresh_s/node_quiet_max_s
    shock_move: float = 0.05       # (b) a mid move >= this within shock_window_s freezes the node
    shock_window_s: float = 10.0
    freeze_s: float = 30.0         #     freeze duration after a shock (disarm + no quotes)
    persist_ms: int = 1500         # (c) a direction must be continuously viable this long before arming
    # CONNECTION-BASED freshness (fixes quote-churn: a QUIET book on a HEALTHY socket is FRESH).
    conn_fresh_s: float = 30.0     # a venue connection is fresh if it had ANY protocol activity within this
    node_quiet_max_s: float = 180.0  # a node goes suspect only if its book hasn't ticked this long (live game)
    stale_grace_s: float = 5.0     # a live order is cancelled for staleness only after it holds this long

    def horizon_for(self, sport: str) -> float:
        return float(self.horizon_hours.get(sport, 3.0))


@dataclass
class MakerRtConfig:
    """Typed view of the ``maker_rt:`` block (missing keys -> safe defaults)."""
    max_games: int = 20                    # nearest-by-kickoff games to quote across both sports
    quote_usd: float = 100.0               # shadow quote notional per node/direction
    target_net: float = 0.010              # min net edge the maker combo must clear at the quote price
    # SANITY CEILING: a computed edge above this (%) is almost certainly a PRICING/PAIRING bug (wrong
    # markets paired, a stale/one-sided book), NOT a real opportunity — the quote is REJECTED and logged
    # loudly, never placed. Same doctrine as the detector's max_plausible_roi_pct (8%); the maker's own
    # edges are ~1%, so 5% is a wide bug-catcher, not a live constraint.
    max_plausible_edge_pct: float = 5.0
    # HEDGE EXECUTION FLOOR: the fee-inclusive locked net the hedge order's PRICE CAP is solved at, so a
    # hedge cannot execute worse than this (it fills at/under the cap or misses -> verified unwind).
    # -0.010 matches the decline floor: a marginally negative pair is cheaper to LOCK than to unwind
    # (an unwind pays spread + taker fee, ~0.5-2%, plus brief naked exposure). Set 0.0 for break-even,
    # which also makes any pair over $1.00/share unfillable — at -0.010 the ceiling is $1.01/share.
    hedge_execution_floor: float = -0.010
    debounce_ms: int = 250                 # quote-engine debounce
    expire_before_kickoff_s: int = 120     # cancel/expire all quotes at kickoff - this
    poly_fee_rate: float = 0.05            # Polymarket sports taker rate (hedge-fee model)
    # LIVE-eligible rest directions. rest_poly is proven+armed; rest_kalshi stays OFF (shadow) until its
    # own SMOKE-KALSHI passes, then add "rest_kalshi" here. Shared caps/one-in-flight span both.
    directions: tuple = ("rest_poly",)
    # PER-DIRECTION SLOT RESERVATION: of maker_rt.live.max_open_quotes total resting slots, GUARANTEE
    # this many to EACH enabled direction (the remainder float freely). Stops one direction (e.g.
    # rest-kalshi) monopolizing every open slot and starving the other. 0 = off; single-direction
    # configs are unaffected either way (there is no other direction to protect a slot for).
    reserve_per_direction: int = 0
    head_poll_s: int = 60                  # stale-code guard: exit 0 on git HEAD change
    ping_s: int = 10                       # ws keepalive PING cadence (poly)
    # REPRICE HYSTERESIS (stop shredding queue position): a VOLUNTARY upward reprice needs >= this many
    # ticks of improvement (or no-longer-at-best) AND the order to have rested >= min_rest_s. A MANDATORY
    # reprice (floor/never-crossable violation) is always immediate.
    reprice_min_ticks: int = 2
    min_rest_s: float = 20.0
    # HEDGE-THIN pre-filter: a node that recently refused hedge_too_thin must show CONTINUOUS hedge depth
    # for >= this many seconds before it RE-arms (a flickering-thin hedge otherwise arms-then-cancels,
    # shredding quote lifetime). Healthy nodes (no recent thinness) arm immediately. Pairs with the 15-min
    # cooldown after 3 refusals / 10 min.
    hedge_persist_s: float = 10.0
    # TELEGRAM digest: routine quote/reprice/cancel events collapse into one line every this many minutes
    # (0 = old behavior, instant per-event). FILL/HEDGE/UNWIND/PAUSE/HALT/feed-down/errors stay INSTANT.
    telegram_digest_min: float = 15.0
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
                 "poly_fee_rate", "head_poll_s", "ping_s", "reprice_min_ticks", "min_rest_s",
                 "hedge_persist_s", "telegram_digest_min", "reserve_per_direction",
                 "max_plausible_edge_pct", "hedge_execution_floor"):
        if blk.get(name) is not None:
            setattr(cfg, name, blk[name])
    cfg.max_plausible_edge_pct = float(cfg.max_plausible_edge_pct)
    cfg.hedge_execution_floor = float(cfg.hedge_execution_floor)
    cfg.reserve_per_direction = int(cfg.reserve_per_direction)
    cfg.max_games = int(cfg.max_games)
    cfg.quote_usd = float(cfg.quote_usd)
    cfg.target_net = float(cfg.target_net)
    cfg.debounce_ms = int(cfg.debounce_ms)
    cfg.expire_before_kickoff_s = int(cfg.expire_before_kickoff_s)
    cfg.poly_fee_rate = float(cfg.poly_fee_rate)
    cfg.head_poll_s = int(cfg.head_poll_s)
    cfg.ping_s = int(cfg.ping_s)
    cfg.reprice_min_ticks = int(cfg.reprice_min_ticks)
    cfg.min_rest_s = float(cfg.min_rest_s)
    cfg.hedge_persist_s = float(cfg.hedge_persist_s)
    cfg.telegram_digest_min = float(cfg.telegram_digest_min)
    if blk.get("directions") is not None:                # LIVE-eligible rest directions (list in YAML)
        d = blk["directions"]
        cfg.directions = tuple(str(x).strip().lower().replace("-", "_") for x in d) if isinstance(d, (list, tuple)) \
            else (str(d).strip().lower().replace("-", "_"),)
    else:
        cfg.directions = tuple(cfg.directions)
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
    for name in ("fresh_s", "shock_move", "shock_window_s", "freeze_s", "conn_fresh_s",
                 "node_quiet_max_s", "stale_grace_s"):
        if ip_blk.get(name) is not None:
            setattr(ic, name, float(ip_blk[name]))
    if ip_blk.get("persist_ms") is not None:
        ic.persist_ms = int(ip_blk["persist_ms"])
    cfg.inplay = ic
    live_blk = blk.get("live")
    live_blk = dict(live_blk) if isinstance(live_blk, dict) else {}
    lc = LiveConfig()
    for name in ("enabled", "arm_file", "quote_usd_max", "max_open_quotes", "max_fills_per_day",
                 "max_daily_loss_usd", "max_daily_stake_usd", "max_pair_stake_usd", "hedge_timeout_ms",
                 "unwind_on_hedge_fail", "auto_flatten", "auto_flatten_max_usd", "fill_poll_s",
                 "max_quote_age_s", "kalshi_feed_grace_s"):
        if live_blk.get(name) is not None:
            setattr(lc, name, live_blk[name])
    lc.enabled = bool(lc.enabled)
    lc.auto_flatten = bool(lc.auto_flatten)
    lc.auto_flatten_max_usd = float(lc.auto_flatten_max_usd)
    lc.max_quote_age_s = float(lc.max_quote_age_s)
    lc.fill_poll_s = float(lc.fill_poll_s)
    lc.quote_usd_max = float(lc.quote_usd_max)
    lc.max_open_quotes = int(lc.max_open_quotes)
    lc.max_fills_per_day = int(lc.max_fills_per_day)
    lc.max_daily_loss_usd = float(lc.max_daily_loss_usd)
    lc.max_daily_stake_usd = float(lc.max_daily_stake_usd)
    lc.max_pair_stake_usd = float(lc.max_pair_stake_usd)
    lc.kalshi_feed_grace_s = float(lc.kalshi_feed_grace_s)
    lc.hedge_timeout_ms = int(lc.hedge_timeout_ms)
    lc.unwind_on_hedge_fail = bool(lc.unwind_on_hedge_fail)
    cfg.live = lc
    # LIVE-INPLAY — the second, independent gate (locked by default; enabled False).
    li_blk = blk.get("live_inplay")
    li_blk = dict(li_blk) if isinstance(li_blk, dict) else {}
    li = InplayLiveConfig()
    for name in ("enabled", "arm_file", "quote_usd_max", "max_open_quotes", "max_fills_per_day",
                 "max_daily_loss_usd", "hedge_timeout_ms", "freeze_cooloff_s", "hedge_decline_floor",
                 "first_fill_pause_s", "halt_locked_net"):
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
    li.first_fill_pause_s = float(li.first_fill_pause_s)
    li.halt_locked_net = float(li.halt_locked_net)
    cfg.live_inplay = li
    return cfg


def ensure_dirs() -> None:
    os.makedirs(GENZ_DIR, exist_ok=True)
    os.makedirs(OPS_DIR, exist_ok=True)
