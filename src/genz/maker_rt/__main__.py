"""maker_rt entry point: a single asyncio process — `python -m src.genz.maker_rt`.

SHADOW by default (real sockets, paper quotes, zero orders). The live path is built but gated by
LiveGate (config + arm file + self-check); this build never opens it. Graceful shutdown cancels ALL
live orders FIRST (armed only), then stops the feeds. A HEAD-change guard polls git every
maker_rt.head_poll_s and exits 0 so the .ps1 wrapper restarts on fresh bytecode.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Optional

from ...logsetup import get_logger, setup_logging
from . import config as mrt_config
from .driver import QuoteDriver
from .feeds import KalshiFeed, PolyMarketFeed
from .gitguard import head_changed, read_head_sha
from .hedge import LiveHedger
from .inplay_exec import InFlightGuard, InplayLiveExecutor
from .live import LiveGate
from .state import MakerState, bump_restart, utcnow
from .store import BookStore
from .universe import build_universe, kalshi_tickers, load_trees, poly_tokens, tree_mtimes

HEARTBEAT_EVERY_S = 2.5


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


async def _run(cfg: Any, log: Any) -> int:
    mrt_config.ensure_dirs()
    now0 = utcnow()
    restarts = bump_restart(now0)                     # crash-loop signal (persisted, per UTC day)
    live_gate = LiveGate(cfg, log=log)                # no order clients in shadow -> both gates stay closed
    gate = live_gate.evaluate()                       # SHADOW unless enabled+armed+self-check pass (pre-game)
    inplay_gate = live_gate.evaluate_inplay()
    mode = "live" if gate.armed else "shadow"
    log.info("[MAKER_RT] starting in %s mode (pre-game: %s | in-play: %s) — restart #%d today",
             mode.upper(), gate.reason, inplay_gate.reason, restarts)

    store = BookStore()
    state = MakerState(log=log)
    state.restarts_today = restarts
    state.gates = {"pre": bool(gate.armed), "inplay": bool(inplay_gate.armed)}
    # IN-PLAY LIVE executor — built + wired, but LOCKED (its gate never opens in this build; every driver
    # hook is a no-op). No order clients are injected in shadow, so it can never place an order.
    executor = InplayLiveExecutor(cfg, live_gate, LiveHedger(poly_rate=cfg.poly_fee_rate, log=log),
                                  in_flight=InFlightGuard(), telegram=_telegram_sender(log),
                                  state=state, log=log)
    driver = QuoteDriver(cfg, state, log=log, inplay_exec=executor)
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

    head0 = read_head_sha(mrt_config.REPO_ROOT)
    mtimes = tree_mtimes()
    last_hb = 0.0
    last_achv_log = 0.0
    try:
        while True:
            now, now_ts = utcnow(), time.time()
            driver.refresh_quotes(store, now, now_ts)
            driver.process_drift(store, now, now_ts)
            driver.expire_kickoff(now, now_ts)
            sockets = {"poly_market": pm.connected, "poly_user": False, "kalshi": ks.connected}
            if now_ts - last_hb >= HEARTBEAT_EVERY_S:
                state.write_heartbeat(mode, sockets, driver.open_quote_count(), now)
                summ = state.summary(mode, sockets, now)
                state.write_summary(mode, sockets, now)
                if now_ts - last_achv_log >= 60.0:            # a compact per-sport achievable heartbeat
                    last_achv_log = now_ts
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
        # SHUTDOWN: cancel all LIVE orders FIRST (armed only), then stop the feeds.
        # (In shadow there are no live orders; the hook is here for the armed path.)
        # FLUSH pending drift so a HEAD-change/restart never silently drops a fill's drift numbers.
        try:
            driver.flush_drift(utcnow())
        except Exception as exc:  # noqa: BLE001 — a flush failure must not mask the real shutdown
            log.warning("[MAKER_RT] drift flush on shutdown failed: %s", exc)
        pm.stop(); ks.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        state.write_heartbeat(mode, {"poly_market": False, "poly_user": False, "kalshi": False},
                              0, utcnow())


def _load_env() -> None:
    """Load the repo .env so Kalshi/Polymarket creds are present (as src.executor.config does)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=os.path.join(mrt_config.REPO_ROOT, ".env"), override=False)
    except ImportError:  # pragma: no cover - dotenv optional; rely on the ambient environment
        pass


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
    try:
        return asyncio.run(_run(cfg, log))
    except KeyboardInterrupt:
        log.warning("[MAKER_RT] interrupted — shutting down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
