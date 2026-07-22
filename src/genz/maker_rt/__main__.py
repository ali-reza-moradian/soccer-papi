"""maker_rt entry point: a single asyncio process — `python -m src.genz.maker_rt`.

SHADOW by default (real sockets, paper quotes, zero orders). The live path is built but gated by
LiveGate (config + arm file + self-check); this build never opens it. Graceful shutdown cancels ALL
live orders FIRST (armed only), then stops the feeds. A HEAD-change guard polls git every
maker_rt.head_poll_s and exits 0 so the .ps1 wrapper restarts on fresh bytecode.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Any, Optional

from ...logsetup import get_logger, setup_logging
from . import config as mrt_config
from .clients import build_pregame_order_clients
from .driver import QuoteDriver
from .feeds import KalshiFeed, PolyMarketFeed, PolyUserFeed
from .gitguard import head_changed, read_head_sha
from .hedge import LiveHedger
from .inplay_exec import InFlightGuard
from .live import LiveGate
from .state import MakerState, bump_restart, utcnow
from .store import BookStore
from .universe import build_universe, kalshi_tickers, load_trees, poly_tokens, tree_mtimes

HEARTBEAT_EVERY_S = 2.5
LIVE_FILL_POLL_S = 1.5                        # REST backup fill-poll cadence (socket is the primary signal)
STOP_ALL_PATH = os.path.join(mrt_config.OPS_DIR, "STOP_ALL")
_STOP = {"flag": False}                       # set by a SIGTERM/SIGBREAK handler -> graceful cancel-all + exit


def _install_signal_handlers(log: Any) -> None:
    """SIGTERM/SIGBREAK -> request a graceful stop (the loop then cancels all live orders + exits). SIGINT
    already unwinds via KeyboardInterrupt -> the _run finally. Best-effort; unsupported signals are skipped."""
    def _handler(signum, frame):  # noqa: ANN001
        _STOP["flag"] = True
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError, RuntimeError):  # pragma: no cover - platform dependent
                pass


def _telegram_sender(log: Any):
    """A ``callable(text)`` that alerts Telegram, or None when creds are absent. Only ever called by the
    in-play executor when the (locked) in-play gate is armed."""
    key, chat = os.environ.get("TELEGRAM_BOT_KEY"), os.environ.get("TELEGRAM_GROUP_ID")
    if not (key and chat):
        return None
    try:
        from ...telegram import send_message
    except Exception:  # noqa: BLE001
        return None
    return lambda text: send_message(key, chat, text, log)


def _kalshi_ws_auth() -> tuple:
    """(api_key_id, signer) for the Kalshi WS handshake, or (None, None) if creds are absent."""
    api_key_id = os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_ACCESS_KEY")
    try:
        from ...executor.kalshi_exec import make_rsa_signer
        signer = make_rsa_signer()
    except Exception:  # noqa: BLE001 - no key / no crypto -> Kalshi socket simply stays down
        signer = None
    return api_key_id, signer


def _sport_breakdown(universe: list) -> str:
    """'soccer=2 mlb=14 tennis=6' — the per-sport market counts, for the startup log."""
    from collections import Counter
    c = Counter(getattr(m, "sport", "?") for m in universe)
    return " ".join(f"{k}={c[k]}" for k in sorted(c)) or "empty"


def _spawn_feeds(store: BookStore, universe: list, cfg: Any, log: Any) -> tuple:
    """Create + start the shadow feeds (poly market + kalshi) for the current universe."""
    api_key_id, signer = _kalshi_ws_auth()
    pm = PolyMarketFeed(store, poly_tokens(universe), ping_s=cfg.ping_s, log=log,
                        on_prints=None, on_update=None)
    ks = KalshiFeed(store, kalshi_tickers(universe), api_key_id=api_key_id, signer=signer, log=log,
                    on_prints=None, on_update=None)
    return pm, ks


def _build_pregame_exec(cfg: Any, live_gate: Any, armed: bool, kalshi_oc: Any, poly_oc: Any,
                        in_flight: Any, telegram: Any, state: Any, log: Any) -> Any:
    """The PRE-GAME continuous live executor, or None when not armed / no clients. rest-poly only."""
    if not (armed and poly_oc is not None):
        return None
    from .caps import LiveCaps
    from .orders import PolyOrderClient
    from .pregame_exec import PregameLiveExecutor
    caps = LiveCaps(cfg.live, telegram=telegram, log=log)
    order_client = PolyOrderClient(poly_oc, log=log)
    hedger = LiveHedger(kalshi_client=kalshi_oc, poly_client=poly_oc, poly_rate=cfg.poly_fee_rate, log=log)
    return PregameLiveExecutor(cfg, live_gate, order_client, hedger, caps, poly_oc,
                               in_flight=in_flight, telegram=telegram, state=state, log=log)


def _startup_stray_cancel_armed(poly_oc: Any, kalshi_oc: Any, log: Any) -> int:
    """Startup net: cancel any order a previous run left resting (the graceful-stop gap's backstop)."""
    try:
        from .smoke import _startup_stray_cancel
        return _startup_stray_cancel(poly_oc, kalshi_oc, log)
    except Exception as exc:  # noqa: BLE001
        log.warning("[MAKER_RT][LIVE] startup stray-cancel failed: %s", exc)
        return 0


def _armed_startup_alert(cfg: Any, pre_armed: bool, inplay_armed: bool, telegram: Any, swept: int,
                         log: Any) -> None:
    phases = "+".join(p for p, on in (("pre", pre_armed), ("inplay", inplay_armed)) if on) or "none"
    msg = ("[MAKER_RT][LIVE] LIVE ARMED (rest-poly; phases: %s). stray-cancel=%d. SHARED caps: "
           "quote<=$%.0f, daily_stake<=$%.0f, open<=%d, fills/day<=%d, loss<=$%.0f. in-play circuit: "
           "first-fill pause %.0fs, day-halt at locked_net %.1f%%."
           % (phases, swept, cfg.live.quote_usd_max, cfg.live.max_daily_stake_usd, cfg.live.max_open_quotes,
              cfg.live.max_fills_per_day, cfg.live.max_daily_loss_usd,
              cfg.live_inplay.first_fill_pause_s, cfg.live_inplay.halt_locked_net * 100))
    log.warning(msg)
    if telegram is None:
        log.warning("[MAKER_RT][LIVE] ARMED but TELEGRAM_* env is ABSENT — no live-action alerts will be "
                    "sent. Set TELEGRAM_BOT_KEY + TELEGRAM_GROUP_ID (the wrapper sources secrets.local.ps1).")
    else:
        try:
            telegram(msg)
        except Exception:  # noqa: BLE001
            pass


def _spawn_user_feed(store: BookStore, pregame_exec: Any, poly_oc: Any, log: Any) -> tuple:
    """Start the Poly USER socket when armed, routing order updates to the executor. (None, None) if not
    armed or L2 creds can't be derived — in that case the REST poll is the only fill signal, so the
    executor's feed_ok stays False and placement halts."""
    if pregame_exec is None:
        return None, None
    try:
        creds = poly_oc.derive_l2_creds()
    except Exception as exc:  # noqa: BLE001
        log.error("[MAKER_RT][LIVE] L2 creds derive FAILED (%s) — user socket not started; placement will "
                  "halt (feed down). REST fill-poll still runs as a safety net.", exc)
        return None, None
    if not (creds and creds.get("apiKey")):
        log.error("[MAKER_RT][LIVE] no L2 creds — user socket not started; placement halts (feed down).")
        return None, None

    def _on_user_order(e):
        pregame_exec.on_order_update(e, store, utcnow(), time.time())

    pm_user = PolyUserFeed(store, [], creds, on_user_order=_on_user_order,
                           on_user_trade=lambda e: None, log=log)
    return pm_user, asyncio.create_task(pm_user.run())


async def _run(cfg: Any, log: Any) -> int:
    mrt_config.ensure_dirs()
    now0 = utcnow()
    restarts = bump_restart(now0)                     # crash-loop signal (persisted, per UTC day)
    # PRE-GAME live: construct order clients ONLY when live.enabled (a shadow/default config NEVER
    # builds a client), and inject them so the pre-game gate can arm. NOTE: the CONTINUOUS pre-game
    # placement executor is a SEPARATE, later enable — this build injects clients + shares one-in-flight
    # but does NOT place pre-game orders (refresh_quotes stays shadow). Use `--smoke` for live placement.
    kalshi_oc, poly_oc = build_pregame_order_clients(cfg, log=log)
    in_flight = InFlightGuard()                       # ONE fill in flight, shared pre-game + in-play
    live_gate = LiveGate(cfg, kalshi_client=kalshi_oc, poly_client=poly_oc, log=log)
    gate = live_gate.evaluate()                       # armed only when enabled+arm file+self-check pass
    inplay_gate = live_gate.evaluate_inplay()
    mode = "live" if (gate.armed or inplay_gate.armed) else "shadow"
    log.info("[MAKER_RT] starting in %s mode (pre-game: %s | in-play: %s) — restart #%d today",
             mode.upper(), gate.reason, inplay_gate.reason, restarts)

    store = BookStore()
    state = MakerState(log=log)
    state.restarts_today = restarts
    state.gates = {"pre": bool(gate.armed), "inplay": bool(inplay_gate.armed)}
    telegram = _telegram_sender(log)
    # UNIFIED LIVE executor (rest-poly, BOTH phases) — built when EITHER gate is armed AND the order
    # clients exist. ONE shared LiveCaps budget + ONE global in-flight guard govern pre+inplay together.
    pregame_exec = _build_pregame_exec(cfg, live_gate, gate.armed or inplay_gate.armed, kalshi_oc,
                                       poly_oc, in_flight, telegram, state, log)
    if pregame_exec is not None:
        swept = _startup_stray_cancel_armed(poly_oc, kalshi_oc, log)
        _armed_startup_alert(cfg, gate.armed, inplay_gate.armed, telegram, swept, log)
    driver = QuoteDriver(cfg, state, log=log, inplay_exec=None, pregame_exec=pregame_exec)
    horizon = cfg.inplay.horizon_hours
    universe = build_universe(load_trees(), time.time(), max_games=cfg.max_games,
                              expire_before_kickoff_s=cfg.expire_before_kickoff_s, horizon_hours=horizon)
    driver.set_universe(universe)
    log.info("[MAKER_RT] universe: %d markets (%s), %d poly tokens, %d kalshi tickers.",
             len(universe), _sport_breakdown(universe), len(poly_tokens(universe)),
             len(kalshi_tickers(universe)))

    def on_prints(prints):
        driver.consume_prints(prints, store, utcnow(), time.time())

    pm, ks = _spawn_feeds(store, universe, cfg, log)
    pm.on_prints = on_prints
    ks.on_prints = on_prints
    tasks = [asyncio.create_task(pm.run()), asyncio.create_task(ks.run())]
    # Poly USER socket (our real fills/order updates) — started ONLY when armed. Route order updates to
    # the executor's fill detector; the REST poll below is the reliable backup.
    pm_user, user_task = _spawn_user_feed(store, pregame_exec, poly_oc, log)

    head0 = read_head_sha(mrt_config.REPO_ROOT)
    mtimes = tree_mtimes()
    last_hb = 0.0
    last_achv_log = 0.0
    last_fill_poll = 0.0
    try:
        while True:
            now, now_ts = utcnow(), time.time()
            # GRACEFUL STOP: the supervisor drops data/ops/STOP_ALL (or a SIGTERM/SIGBREAK arrives) —
            # return 0 so the finally cancels every live order BEFORE the supervisor can force-kill us.
            if _STOP["flag"] or os.path.exists(STOP_ALL_PATH):
                log.warning("[MAKER_RT] graceful stop (%s) — cancelling live orders + exiting.",
                            "STOP_ALL" if os.path.exists(STOP_ALL_PATH) else "signal")
                return 0
            driver.refresh_quotes(store, now, now_ts)
            driver.process_drift(store, now, now_ts)
            driver.expire_kickoff(now, now_ts)
            if pregame_exec is not None:
                pregame_exec.roll_day(now)                        # reset in-play circuit + caps at UTC midnight
                pregame_exec.enforce_arm_state(now)               # a phase disarmed mid-run -> cancel its opens
                pregame_exec.set_feed_ok(bool(pm_user is not None and pm_user.connected), now)
                pregame_exec.sample_metrics(store, now_ts)        # at-best sampler for the lifetime metrics
                pregame_exec.maybe_flush_digest(now_ts)           # routine-event Telegram digest (15 min)
                if now_ts - last_fill_poll >= LIVE_FILL_POLL_S:      # REST backup fill detector
                    last_fill_poll = now_ts
                    pregame_exec.poll_open_orders(store, now, now_ts)
                state.live = pregame_exec.snapshot(now_ts)
            poly_user_up = bool(pm_user is not None and pm_user.connected)
            sockets = {"poly_market": pm.connected, "poly_user": poly_user_up, "kalshi": ks.connected}
            if now_ts - last_hb >= HEARTBEAT_EVERY_S:
                state.write_heartbeat(mode, sockets, driver.open_quote_count(), now)
                summ = state.summary(mode, sockets, now)
                state.write_summary(mode, sockets, now)
                if now_ts - last_achv_log >= 60.0:            # a compact per-sport achievable heartbeat
                    last_achv_log = now_ts
                    if pregame_exec is not None:
                        lv = pregame_exec.snapshot()
                        log.info("[MAKER_RT][LIVE] feed_ok=%s open=%d stake=$%.2f/%.0f fills=%d pnl=$%.2f "
                                 "halted=%s", lv["feed_ok"], lv["open_quotes"], lv["stake_today"],
                                 lv["stake_cap"], lv["fills_today"], lv["pnl_today"], lv["halted"])
                    for sp, sd in (summ.get("by_sport") or {}).items():
                        a = sd.get("achievable") or {}
                        if a.get("n"):
                            log.info("[MAKER_RT][ACHV] %s p50=%s ge0.25pct=%s ge1pct=%s (n=%d) | q=%d f=%d",
                                     sp, a.get("p50"), a.get("share_ge_25bp"), a.get("share_ge_100bp"),
                                     a.get("n"), sd.get("quotes", 0), sd.get("fills", 0))
                last_hb = now_ts
                if head_changed(head0, read_head_sha(mrt_config.REPO_ROOT)):
                    log.warning("[MAKER_RT] git HEAD changed — exiting 0 for a fresh restart.")
                    return 0
                nm = tree_mtimes()
                if nm != mtimes:                          # trees rebuilt -> rebuild universe + feeds
                    mtimes = nm
                    universe = build_universe(load_trees(), now_ts, max_games=cfg.max_games,
                                              expire_before_kickoff_s=cfg.expire_before_kickoff_s,
                                              horizon_hours=horizon)
                    driver.set_universe(universe)
                    for t in tasks:
                        t.cancel()
                    pm.stop(); ks.stop()
                    pm, ks = _spawn_feeds(store, universe, cfg, log)
                    pm.on_prints = on_prints; ks.on_prints = on_prints
                    tasks = [asyncio.create_task(pm.run()), asyncio.create_task(ks.run())]
                    log.info("[MAKER_RT] trees changed — universe now %d markets.", len(universe))
            await asyncio.sleep(cfg.debounce_ms / 1000.0)
    except asyncio.CancelledError:
        raise
    finally:
        # SHUTDOWN: cancel ALL live orders FIRST (every exit path — STOP_ALL, HEAD change, signal,
        # exception — runs this), then stop the feeds. An unhedged resting order must never be stranded.
        if pregame_exec is not None:
            try:
                n = pregame_exec.cancel_all("shutdown", utcnow())
                log.warning("[MAKER_RT][LIVE] shutdown cancel-all: %d open order(s) cancelled.", n)
            except Exception as exc:  # noqa: BLE001 — a cancel failure must not mask the shutdown
                log.error("[MAKER_RT][LIVE] shutdown cancel-all FAILED: %s", exc)
        # FLUSH pending drift so a HEAD-change/restart never silently drops a fill's drift numbers.
        try:
            driver.flush_drift(utcnow())
        except Exception as exc:  # noqa: BLE001 — a flush failure must not mask the real shutdown
            log.warning("[MAKER_RT] drift flush on shutdown failed: %s", exc)
        pm.stop(); ks.stop()
        if pm_user is not None:
            pm_user.stop()
        for t in tasks:
            t.cancel()
        if user_task is not None:
            user_task.cancel()
        await asyncio.gather(*tasks, *( [user_task] if user_task is not None else [] ),
                             return_exceptions=True)
        state.write_heartbeat(mode, {"poly_market": False, "poly_user": False, "kalshi": False},
                              0, utcnow())


def _load_env() -> None:
    """Load the repo .env so Kalshi/Polymarket creds are present (as src.executor.config does)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=os.path.join(mrt_config.REPO_ROOT, ".env"), override=False)
    except ImportError:  # pragma: no cover - dotenv optional; rely on the ambient environment
        pass


def _arg_value(args: list, name: str, *, default: Any, cast: Any) -> Any:
    """Value following ``name`` in argv (e.g. ``--hold 90``), cast, else ``default``."""
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            try:
                return cast(args[i + 1])
            except (ValueError, TypeError):
                return default
    return default


def main(argv: Optional[list] = None) -> int:
    _load_env()
    setup_logging()
    log = get_logger("maker_rt")
    cfg = mrt_config.load_maker_rt_config()
    args = sys.argv[1:] if argv is None else argv
    if "--selfcheck" in args:
        # READ-ONLY credential/readiness diagnostic — constructs the real order clients and probes
        # each self-check item individually. Places NOTHING; never starts the feeds.
        from .selfcheck import run_selfcheck
        return run_selfcheck(cfg, log=log)
    if "--smoke" in args:
        # ONE real, tiny, near-zero-fill order lifecycle (place -> confirm -> hold -> cancel). Runs only
        # when the pre-game gate would arm. --hold <s> sets the rest duration; --shutdown-proof holds
        # for the cancel-on-signal test.
        from .smoke import run_smoke
        hold = _arg_value(args, "--hold", default=30.0, cast=float)
        return asyncio.run(run_smoke(cfg, log=log, hold_s=hold,
                                     shutdown_proof=("--shutdown-proof" in args)))
    _install_signal_handlers(log)             # SIGTERM/SIGBREAK -> graceful cancel-all + exit
    try:
        return asyncio.run(_run(cfg, log))
    except KeyboardInterrupt:
        log.warning("[MAKER_RT] interrupted — shutting down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
