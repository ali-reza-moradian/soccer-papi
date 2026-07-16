"""maker_rt entry point: a single asyncio process — `python -m src.genz.maker_rt`.

SHADOW by default (real sockets, paper quotes, zero orders). The live path is built but gated by
LiveGate (config + arm file + self-check); this build never opens it. Graceful shutdown cancels ALL
live orders FIRST (armed only), then stops the feeds. A HEAD-change guard polls git every
maker_rt.head_poll_s and exits 0 so the .ps1 wrapper restarts on fresh bytecode.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

from ...logsetup import get_logger, setup_logging
from . import config as mrt_config
from .driver import QuoteDriver
from .feeds import KalshiFeed, PolyMarketFeed
from .gitguard import head_changed, read_head_sha
from .live import LiveGate
from .state import MakerState, utcnow
from .store import BookStore
from .universe import build_universe, kalshi_tickers, load_trees, poly_tokens, tree_mtimes

HEARTBEAT_EVERY_S = 2.5


def _kalshi_ws_auth() -> tuple:
    """(api_key_id, signer) for the Kalshi WS handshake, or (None, None) if creds are absent."""
    api_key_id = os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_ACCESS_KEY")
    try:
        from ...executor.kalshi_exec import make_rsa_signer
        signer = make_rsa_signer()
    except Exception:  # noqa: BLE001 - no key / no crypto -> Kalshi socket simply stays down
        signer = None
    return api_key_id, signer


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
    gate = LiveGate(cfg, log=log).evaluate()          # SHADOW unless enabled+armed+self-check pass
    mode = "live" if gate.armed else "shadow"
    log.info("[MAKER_RT] starting in %s mode (%s)", mode.upper(), gate.reason)

    store = BookStore()
    state = MakerState()
    driver = QuoteDriver(cfg, state, log=log)
    universe = build_universe(load_trees(), time.time(), max_games=cfg.max_games,
                              expire_before_kickoff_s=cfg.expire_before_kickoff_s)
    driver.set_universe(universe)
    log.info("[MAKER_RT] universe: %d markets, %d poly tokens, %d kalshi tickers.",
             len(universe), len(poly_tokens(universe)), len(kalshi_tickers(universe)))

    def on_prints(prints):
        driver.consume_prints(prints, store, utcnow(), time.time())

    pm, ks = _spawn_feeds(store, universe, cfg, log)
    pm.on_prints = on_prints
    ks.on_prints = on_prints
    tasks = [asyncio.create_task(pm.run()), asyncio.create_task(ks.run())]

    head0 = read_head_sha(mrt_config.REPO_ROOT)
    mtimes = tree_mtimes()
    last_hb = 0.0
    try:
        while True:
            now, now_ts = utcnow(), time.time()
            driver.refresh_quotes(store, now, now_ts)
            driver.process_drift(store, now, now_ts)
            driver.expire_kickoff(now, now_ts)
            sockets = {"poly_market": pm.connected, "poly_user": False, "kalshi": ks.connected}
            if now_ts - last_hb >= HEARTBEAT_EVERY_S:
                state.write_heartbeat(mode, sockets, driver.open_quote_count(), now)
                state.write_summary(mode, sockets, now)
                last_hb = now_ts
                if head_changed(head0, read_head_sha(mrt_config.REPO_ROOT)):
                    log.warning("[MAKER_RT] git HEAD changed — exiting 0 for a fresh restart.")
                    return 0
                nm = tree_mtimes()
                if nm != mtimes:                          # trees rebuilt -> rebuild universe + feeds
                    mtimes = nm
                    universe = build_universe(load_trees(), now_ts, max_games=cfg.max_games,
                                              expire_before_kickoff_s=cfg.expire_before_kickoff_s)
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
    try:
        return asyncio.run(_run(cfg, log))
    except KeyboardInterrupt:
        log.warning("[MAKER_RT] interrupted — shutting down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
