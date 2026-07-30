"""maker_rt entry point: a single asyncio process — `python -m src.genz.maker_rt`.

SHADOW by default (real sockets, paper quotes, zero orders). The live path is built but gated by
LiveGate (config + arm file + self-check); this build never opens it. Graceful shutdown cancels ALL
live orders FIRST (armed only), then stops the feeds. A HEAD-change guard polls git every
maker_rt.head_poll_s and exits 0 so the .ps1 wrapper restarts on fresh bytecode.
"""
from __future__ import annotations

import asyncio
import html
import os
import signal
import sys
import time
from typing import Any, Optional

from ...logsetup import get_logger, setup_logging
from . import alerts
from . import config as mrt_config
from . import singleton
from .clients import build_pregame_order_clients
from .driver import QuoteDriver
from .feeds import KalshiFeed, PolyMarketFeed, PolyUserFeed
from .gitguard import head_changed, read_head_sha
from .hedge import LiveHedger
from .inplay_exec import InFlightGuard
from .live import LiveGate
from .loopstats import STATS
from .state import MakerState, bump_restart, utcnow
from .store import BookStore
from .universe import build_universe, kalshi_tickers, load_trees, poly_tokens, tree_mtimes

HEARTBEAT_EVERY_S = 2.5
# (fill-poll cadence now lives in cfg.live.fill_poll_s — the REST poll is the fill authority of record,
#  not a "backup", so it is a tunable config value rather than a module constant.)
RECONCILE_EVERY_S = 300.0                     # position reconciliation cadence while armed (5 min)
SETTLE_EVERY_S = 900.0                         # settled-pnl reconciliation cadence (15 min; markets settle slowly)
# STOP_ALL is read-only, but it resolves through the one resolver like every other runtime path.
# It MUST stay a function: a module-level constant would resolve at IMPORT time, which both defeats
# monkeypatching of OPS_DIR and (under pytest) would trip the live-write guard during collection.
def stop_all_path() -> str:
    return mrt_config.runtime_path("stop_all")
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


def _telegram_sender(log: Any, *, queued: bool = False):
    """A ``callable(text)`` that alerts Telegram, or None when creds are absent. Only ever called by the
    in-play executor when the (locked) in-play gate is armed.

    ``queued`` puts a FIFO + one sender thread in front of the POST (see tgqueue) so a Telegram stall
    cannot freeze the event loop at a fill/halt moment. It is OFF by default because the one caller that
    must NOT queue is the singleton refusal: that process exits immediately afterwards, and an alert
    handed to a thread it will not wait for is an alert nobody receives.

    HTML-ESCAPES the text. ``send_message`` posts with parse_mode=HTML (other senders in this repo use
    real markup), so a bare ``<`` in OUR plain-text alerts was parsed as a start tag and Telegram
    REJECTED the whole message with 400 "Unsupported start tag". That silently killed every alert
    containing a ``<=``: the ARM banner ("quote<=$5"), the in-play AUTO-HALT ("pnl_today <= -$X") and
    the pre-game DAY-HALT ("locked_net <= -2%") — i.e. exactly the messages that matter most."""
    key, chat = os.environ.get("TELEGRAM_BOT_KEY"), os.environ.get("TELEGRAM_GROUP_ID")
    if not (key and chat):
        return None
    try:
        from ...telegram import send_message
    except Exception:  # noqa: BLE001
        return None

    def _post(text: str) -> Any:
        return send_message(key, chat, html.escape(text, quote=False), log)

    if not queued:
        return _post
    from .tgqueue import TelegramQueue
    q = TelegramQueue(_post, log=log)

    def _enqueue(text: str) -> Any:
        # TIMED at the one place every consumer (caps, executor, driver, this module) goes through, so
        # what the LOOP pays for alerting is a number rather than unexplained lag. Post-queue this is
        # microseconds; before the queue it was up to 64s at exactly the worst moment.
        with STATS.timer("telegram"):
            return q(text)
    _enqueue.queue = q                       # so shutdown can flush it  # type: ignore[attr-defined]
    return _enqueue


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


def _spawn_feeds(store: BookStore, universe: list, cfg: Any, log: Any, *,
                 on_prints: Any = None, on_fill: Any = None) -> tuple:
    """Create the feeds (poly market + kalshi) for the current universe WITH their callbacks attached.

    The callbacks are parameters, not something the caller patches on afterwards, because forgetting to
    re-attach one on respawn is exactly the 2026-07-23 invisible-fill bug: the universe-rebuild path
    re-created both feeds and reattached only ``on_prints``, so the private Kalshi ``on_fill`` channel
    silently reverted to its no-op default for the rest of the run."""
    api_key_id, signer = _kalshi_ws_auth()
    pm = PolyMarketFeed(store, poly_tokens(universe), ping_s=cfg.ping_s, log=log,
                        on_prints=on_prints, on_update=None)
    ks = KalshiFeed(store, kalshi_tickers(universe), api_key_id=api_key_id, signer=signer, log=log,
                    on_prints=on_prints, on_update=None, on_fill=on_fill)
    return pm, ks


def _build_pregame_exec(cfg: Any, live_gate: Any, armed: bool, kalshi_oc: Any, poly_oc: Any,
                        in_flight: Any, telegram: Any, state: Any, log: Any) -> Any:
    """The continuous live executor, or None when not armed / no clients. Owns BOTH rest directions
    (rest_poly on Poly, rest_kalshi on Kalshi) under one shared LiveCaps + one in-flight guard."""
    if not (armed and poly_oc is not None):
        return None
    from .caps import LiveCaps
    from .orders import KalshiOrderClient, PolyOrderClient
    from .pregame_exec import PregameLiveExecutor
    caps = LiveCaps(cfg.live, telegram=telegram, log=log)
    order_client = PolyOrderClient(poly_oc, log=log)
    kalshi_order_client = KalshiOrderClient(kalshi_oc, log=log) if kalshi_oc is not None else None
    hedger = LiveHedger(kalshi_client=kalshi_oc, poly_client=poly_oc, poly_rate=cfg.poly_fee_rate, log=log)
    return PregameLiveExecutor(cfg, live_gate, order_client, hedger, caps, poly_oc,
                               in_flight=in_flight, telegram=telegram, state=state, log=log,
                               kalshi_order_client=kalshi_order_client, kalshi=kalshi_oc)


def _startup_stray_cancel_armed(poly_oc: Any, kalshi_oc: Any, log: Any) -> int:
    """Startup net: cancel any order a previous run left resting (the graceful-stop gap's backstop)."""
    try:
        from .smoke import _startup_stray_cancel
        return _startup_stray_cancel(poly_oc, kalshi_oc, log)
    except Exception as exc:  # noqa: BLE001
        log.warning("[MAKER_RT][LIVE] startup stray-cancel failed: %s", exc)
        return 0


def _armed_startup_alert(cfg: Any, pre_armed: bool, inplay_armed: bool, telegram: Any, swept: int,
                         log: Any, caps: Any = None) -> None:
    phases = "+".join(p for p, on in (("pre", pre_armed), ("inplay", inplay_armed)) if on) or "none"
    # Name the ACTUALLY-enabled directions (this used to hardcode "rest-poly" and kept saying so after
    # rest_kalshi went live) and the per-direction slot reserve, so the banner states the real posture.
    dirs = ",".join(sorted(str(d).replace("_", "-") for d in getattr(cfg, "directions", ("rest_poly",))))
    # Print the caps from the LIVE LiveCaps OBJECT (not cfg) so the banner proves what actually loaded +
    # governs — incl. the per-pair cap and the sanity ceiling. Falls back to cfg if no caps passed.
    c = caps or cfg.live
    msg = ("[MAKER_RT][LIVE] LIVE ARMED (%s; phases: %s). stray-cancel=%d. SHARED caps (ONE budget "
           "across pre-game AND in-play — there are no per-phase exposure caps): "
           "quote<=$%.0f, pair<=$%.0f, daily_stake<=$%.0f, open<=%d (reserve/direction %d), "
           "fills/day<=%d, loss<=$%.0f, sanity_ceiling %.1f%%. "
           "in-play circuit: first-fill pause %.0fs, day-halt at locked_net %.1f%%."
           % (dirs or "none", phases, swept, c.quote_usd_max,
              getattr(c, "max_pair_stake_usd", cfg.live.max_pair_stake_usd), c.max_daily_stake_usd,
              c.max_open_quotes, int(getattr(cfg, "reserve_per_direction", 0)),
              c.max_fills_per_day, c.max_daily_loss_usd, getattr(cfg, "max_plausible_edge_pct", 5.0),
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


def _ensure_ctf_approval(poly_oc: Any, log: Any) -> None:
    """SELL/unwind needs the exchange approved to move our CTF tokens (buying with USDC never did). Probe
    a sample token's CONDITIONAL allowance; if missing, set the one-time approval. Best-effort: a failure
    logs loudly (the sell path + reconciliation still catch a real orphan)."""
    if poly_oc is None:
        return
    try:
        from .universe import build_universe, load_trees, poly_tokens
        toks = poly_tokens(build_universe(load_trees(), 0.0, max_games=5, expire_before_kickoff_s=120))
        if not toks:
            return
        if poly_oc.ctf_allowance_ok(toks[0]):
            log.info("[MAKER_RT][LIVE] CTF sell approval present.")
            return
        log.warning("[MAKER_RT][LIVE] CTF sell approval MISSING — setting it (one-time setApprovalForAll).")
        poly_oc.set_ctf_approval(toks[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("[MAKER_RT][LIVE] CTF approval check/set failed: %s", exc)


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
        # WS callbacks run on the SAME event loop as the trading tick, so their cost IS loop lag.
        with STATS.timer("ws_cb"):
            pregame_exec.on_order_update(e, store, utcnow(), time.time())

    pm_user = PolyUserFeed(store, [], creds, on_user_order=_on_user_order,
                           on_user_trade=lambda e: None, log=log)
    return pm_user, asyncio.create_task(pm_user.run())


async def _run(cfg: Any, log: Any) -> int:
    mrt_config.ensure_dirs()
    # SINGLETON FIRST — before the restart counter, before a client, before a socket. A second maker on
    # this host doubles every daily cap, cancels the incumbent's resting orders with its own startup
    # stray-sweep, and false-orphans it; the supervisor can start one because it matches only the
    # wrapper's command line (scripts/ops.py). Refusing here costs nothing: nothing has been placed,
    # nothing has been counted, and the wrapper simply retries once the stale process is gone.
    if not singleton.guard(mrt_config.runtime_path("lock"), log=log,
                           telegram=_telegram_sender(log), now_ts=time.time()):
        return 3
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
    # SETTLED-PNL SANITY CEILING for the aggregator's defense-in-depth guard (a settled |net| above one
    # pair's stake is a unit/pairing bug and is refused before it can touch lifetime pnl).
    state.settled_max_net_usd = float(getattr(cfg.live, "max_pair_stake_usd", 100.0))
    state.load_tuning()          # cross-restart fill-rate + realized-locked-net windows (target_net tuning)
    state.gates = {"pre": bool(gate.armed), "inplay": bool(inplay_gate.armed)}
    telegram = _telegram_sender(log, queued=True)   # FIFO + sender thread: never block the loop on chat
    # UNIFIED LIVE executor (rest-poly, BOTH phases) — built when EITHER gate is armed AND the order
    # clients exist. ONE shared LiveCaps budget + ONE global in-flight guard govern pre+inplay together.
    pregame_exec = _build_pregame_exec(cfg, live_gate, gate.armed or inplay_gate.armed, kalshi_oc,
                                       poly_oc, in_flight, telegram, state, log)
    if pregame_exec is None and poly_oc is not None:
        # DISARMED, BUT THE PREVIOUS RUN'S ORDERS ARE STILL OURS. The stray-order sweep used to run only
        # when ARMED, so a process that came up in SHADOW simply abandoned whatever its predecessor had
        # resting. On 2026-07-29 the 13:30:07Z shadow restart left six Kalshi quotes on the book; they
        # filled between 14:22Z and 17:18Z with no hedger running and settled to a net -$38.08 of
        # completely unintended naked exposure. An unarmed process having ANY order resting is by
        # definition a leak, so sweep FIRST and say so loudly.
        stray = _startup_stray_cancel_armed(poly_oc, kalshi_oc, log)
        if stray:
            log.error("[MAKER_RT] SHADOW startup: cancelled %d order(s) a previous LIVE run left "
                      "resting. An unarmed process must hold no orders — these would have filled "
                      "UNHEDGED.", stray)
            if telegram is not None:
                try:
                    # "problem", not "error": format_event's "error" branch does not render ``detail``, so
                    # this alert used to arrive with no content in it — for the exact leak that cost
                    # -$38.08 of unhedged exposure on 2026-07-29.
                    telegram(alerts.format_event(
                        "problem", detail=(f"Found {stray} live order(s) left over from an earlier "
                                           "session while the bot is switched OFF. They have been "
                                           "cancelled — no action needed.")))
                except Exception:  # noqa: BLE001
                    pass
    if pregame_exec is not None:
        swept = _startup_stray_cancel_armed(poly_oc, kalshi_oc, log)
        _armed_startup_alert(cfg, gate.armed, inplay_gate.armed, telegram, swept, log, pregame_exec.caps)
        _ensure_ctf_approval(poly_oc, log)                # SELL side needs CTF approval; set once if missing
        try:
            orph = pregame_exec.reconcile_positions(now0)  # STARTUP reconciliation: catch a prior-run orphan
            if orph:
                log.error("[MAKER_RT][LIVE] STARTUP reconciliation found an ORPHAN: %s — live halted.", orph)
        except Exception as exc:  # noqa: BLE001
            log.warning("[MAKER_RT][LIVE] startup reconciliation failed: %s", exc)
    driver = QuoteDriver(cfg, state, log=log, inplay_exec=None, pregame_exec=pregame_exec)
    horizon = cfg.inplay.horizon_hours
    # The trees are kept in a variable, not re-read from scratch each time: ``load_trees`` falls back to
    # the tree it already had for any sport whose file will not parse this pass (N13).
    trees = load_trees(log=log)
    try:
        universe = build_universe(trees, time.time(), max_games=cfg.max_games,
                                  expire_before_kickoff_s=cfg.expire_before_kickoff_s,
                                  horizon_hours=horizon)
    except Exception as exc:  # noqa: BLE001 — a malformed tree must not crash-loop the maker at startup
        log.error("[MAKER_RT] STARTUP universe build FAILED (%s) — starting with an EMPTY universe; the "
                  "next tree rebuild retries.", exc)
        universe = []
    driver.set_universe(universe)
    log.info("[MAKER_RT] universe: %d markets (%s), %d poly tokens, %d kalshi tickers.",
             len(universe), _sport_breakdown(universe), len(poly_tokens(universe)),
             len(kalshi_tickers(universe)))

    def on_prints(prints):
        with STATS.timer("ws_cb"):
            driver.consume_prints(prints, store, utcnow(), time.time())

    # rest-kalshi live: route OUR Kalshi fills (private 'fill' channel) to the executor when that
    # direction is enabled + armed. rest-poly-only configs leave this None (the fills are never ours).
    on_kalshi_fill = None
    if pregame_exec is not None and "rest-kalshi" in getattr(pregame_exec, "directions", set()):
        def on_kalshi_fill(e):                                   # noqa: F811 - the wired variant
            with STATS.timer("ws_cb"):
                pregame_exec.on_kalshi_fill(e, store, utcnow(), time.time())

    def _spawn_wired():
        """Every feed creation goes through here, so no respawn can lose a callback."""
        return _spawn_feeds(store, universe, cfg, log, on_prints=on_prints, on_fill=on_kalshi_fill)

    pm, ks = _spawn_wired()
    sub_tokens, sub_tickers = poly_tokens(universe), kalshi_tickers(universe)   # current subscription set
    tasks = [asyncio.create_task(pm.run()), asyncio.create_task(ks.run())]
    # Poly USER socket (our real fills/order updates) — started ONLY when armed. Route order updates to
    # the executor's fill detector; the REST poll below is the reliable backup.
    pm_user, user_task = _spawn_user_feed(store, pregame_exec, poly_oc, log)

    head0 = read_head_sha(mrt_config.REPO_ROOT)
    mtimes = tree_mtimes()
    last_hb = 0.0
    last_achv_log = 0.0
    last_fill_poll = 0.0
    last_reconcile = 0.0
    last_settle = 0.0
    feed_up = {"kalshi": False, "poly_user": False}   # DOWN->UP edge detector for the reconnect poll
    # EVENT-LOOP LAG. Every pass is supposed to take debounce_ms; anything beyond that is time the loop
    # spent NOT repricing, which at the current market count is the difference between quoting the book
    # and quoting a memory of it. Measured, not assumed: a max and a p50 per heartbeat, alongside how
    # many book events each pass absorbed (the conflation ratio). Cheap enough to leave on forever.
    #
    # WHAT blocks it, and for how much of the window in TOTAL — not just the worst single call. The
    # buckets live in loopstats.STATS (sum + count + max each) because a max cannot tell one 5s stall
    # apart from 4,909 fast calls an hour, and those two need opposite fixes. Two blocks that were
    # previously UNATTRIBUTED are buckets now as well: the websocket callbacks (they run on THIS loop,
    # not a thread) and the heartbeat/summary write.
    last_tick_mono = time.monotonic()
    events_at_last_hb = 0
    try:
        while True:
            now, now_ts = utcnow(), time.time()
            _mono = time.monotonic()
            STATS.note_tick((_mono - last_tick_mono - cfg.debounce_ms / 1000.0) * 1000.0)
            last_tick_mono = _mono
            # GRACEFUL STOP: the supervisor drops data/ops/STOP_ALL (or a SIGTERM/SIGBREAK arrives) —
            # return 0 so the finally cancels every live order BEFORE the supervisor can force-kill us.
            stop_all = os.path.exists(stop_all_path())
            if _STOP["flag"] or stop_all:
                log.warning("[MAKER_RT] graceful stop (%s) — cancelling live orders + exiting.",
                            "STOP_ALL" if stop_all else "signal")
                return 0
            # Quote refresh is in-memory book math EXCEPT when it decides to act: a place/reprice/cancel
            # is a synchronous POST to the venue, so an active tick blocks for as long as the exchange
            # takes. That is the unexplained part of the max lag (a 2.7s tick with only 612ms of fill-poll
            # in it), and it is inherent to placing orders — worth seeing, not worth hiding.
            with STATS.timer("quotes"):
                driver.refresh_quotes(store, now, now_ts)
                driver.process_drift(store, now, now_ts)
                driver.expire_kickoff(now, now_ts)
            if pregame_exec is not None:
                pregame_exec.roll_day(now)                        # reset in-play circuit + caps at UTC midnight
                pregame_exec.enforce_arm_state(now)               # a phase disarmed mid-run -> cancel its opens
                ks_up, pu_up = bool(ks.connected), bool(pm_user is not None and pm_user.connected)
                # RECONNECT DISCIPLINE: on every DOWN->UP edge, REST-poll all open orders + sweep the
                # account fill history BEFORE trusting the stream again (a stream never replays what it
                # missed while it was away). Flap count + downtime are recorded for the panel.
                for _venue, _up, _was in (("kalshi", ks_up, feed_up["kalshi"]),
                                          ("poly_user", pu_up, feed_up["poly_user"])):
                    if _up != _was:
                        pregame_exec.note_flap(_venue, _up, now_ts)
                        if _up:
                            pregame_exec.on_feed_reconnect(_venue, store, now, now_ts)
                        feed_up[_venue] = _up
                pregame_exec.set_feed_ok(pu_up, now)
                pregame_exec.set_kalshi_feed_ok(ks_up, now, now_ts)   # rest-kalshi FILL-signal health (debounced)
                pregame_exec.sample_metrics(store, now_ts)        # at-best sampler for the lifetime metrics
                pregame_exec.sample_slot_wait(now_ts)             # slot-wait gauge (starvation early warning)
                pregame_exec.note_feed_health({"poly_market": pm, "poly_user": pm_user, "kalshi": ks})
                pregame_exec.maybe_flush_digest(now_ts)           # routine-event Telegram digest (15 min)
                # WS-INDEPENDENT FILL AUTHORITY (primary detector; the socket is only an accelerator).
                # The venue READS run on a worker thread; every DECISION (routing a fill, hedging it,
                # freeing a slot, mutating caps) is made here, on the loop, when the batch is drained.
                # The 10s cadence and the routing rules are unchanged — only the blocking moved.
                with STATS.timer("fill_poll"):
                    pregame_exec.drain_offloop(store, now, now_ts)
                if (now_ts - last_fill_poll >= pregame_exec.fill_poll_s
                        or pregame_exec.needs_fill_poll()):
                    last_fill_poll = now_ts
                    pregame_exec.submit_fill_poll(now_ts)
                if now_ts - last_reconcile >= RECONCILE_EVERY_S:     # POSITION RECONCILIATION (orphan guard)
                    last_reconcile = now_ts
                    with STATS.timer("reconcile"):
                        try:
                            pregame_exec.reconcile_positions(now)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("[MAKER_RT][LIVE] reconciliation failed: %s", exc)
                if now_ts - last_settle >= SETTLE_EVERY_S:           # SETTLED-P&L (venue-truth realized pnl)
                    last_settle = now_ts
                    with STATS.timer("settle"):
                        try:
                            pregame_exec.reconcile_settlements(now)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("[MAKER_RT][LIVE] settlement reconcile failed: %s", exc)
                state.live = pregame_exec.snapshot(now_ts)
            poly_user_up = bool(pm_user is not None and pm_user.connected)
            sockets = {"poly_market": pm.connected, "poly_user": poly_user_up, "kalshi": ks.connected}
            if now_ts - last_hb >= HEARTBEAT_EVERY_S:
                # HEARTBEAT/SUMMARY WRITE — several synchronous JSON builds plus file writes, every 2.5s.
                # Previously unattributed, so whatever it cost was reported as bare, unexplained loop lag.
                with STATS.timer("heartbeat"):
                    state.write_heartbeat(mode, sockets, driver.open_quote_count(), now)
                    state.maybe_persist_tuning(now_ts)     # throttled; fills persist immediately
                    if pregame_exec is not None:
                        pregame_exec.maybe_persist_daily_caps(now_ts)  # caps survive a mid-day restart
                    summ = state.summary(mode, sockets, now)
                    state.write_summary(mode, sockets, now)
                if now_ts - last_achv_log >= 60.0:            # a compact per-sport achievable heartbeat
                    last_achv_log = now_ts
                    if STATS.ticks:
                        p50, p99, mx = STATS.lag_percentiles()
                        applied = store.events_applied - events_at_last_hb
                        events_at_last_hb = store.events_applied
                        worst = STATS.worst_bucket() or "none"
                        # BUSIEST is by SUM, not by worst call: the bucket that ate the most of the
                        # window is the one to fix. cancel/telegram are NESTED inside whichever bucket
                        # called them (see loopstats), so the parts do not add up to the tick.
                        log.info("[MAKER_RT][LOOP] %d ticks, lag p50 %.1fms p99 %.1fms max %.1fms "
                                 "(target %dms) · %d book event(s) absorbed = %.1f per tick "
                                 "(conflated) · %d markets · busiest %s · %s",
                                 STATS.ticks, p50, p99, mx, cfg.debounce_ms, applied,
                                 applied / max(1, STATS.ticks), len(universe), worst, STATS.render())
                        STATS.reset()
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
                with STATS.timer("trees"):
                    nm = tree_mtimes()
                if nm != mtimes:                          # trees rebuilt -> rebuild the universe
                    # KEEP-OLD-UNIVERSE. A rebuild that yields NOTHING while markets are live is not a
                    # quiet fixture list — it is a failed read, and adopting it cancels every resting
                    # order. The mtimes are NOT committed in that case, so the next heartbeat retries.
                    try:
                        with STATS.timer("trees"):
                            new_trees = load_trees(previous=trees, log=log)
                            new_universe = build_universe(new_trees, now_ts, max_games=cfg.max_games,
                                                          expire_before_kickoff_s=cfg.expire_before_kickoff_s,
                                                          horizon_hours=horizon)
                    except Exception as exc:  # noqa: BLE001 — a bad tree must never kill the live loop
                        log.error("[MAKER_RT] tree reload FAILED (%s) — keeping the current universe of "
                                  "%d market(s); retrying next heartbeat.", exc, len(universe))
                        new_trees, new_universe = None, None
                    if new_universe is None or (not new_universe and universe):
                        if new_universe is not None:
                            log.error("[MAKER_RT] tree rebuild produced an EMPTY universe while %d "
                                      "market(s) were live — KEEPING the old universe (adopting it "
                                      "would cancel every resting order); retrying next heartbeat.",
                                      len(universe))
                    else:
                        mtimes, trees, universe = nm, new_trees, new_universe
                        driver.set_universe(universe)
                        # RESPAWN ONLY IF THE SUBSCRIPTION SET ACTUALLY CHANGED. A tree mtime bump is not
                        # a reason to drop two healthy websockets: the scanners rewrite the trees every
                        # few minutes, and tearing the sockets down on every bump is what produced the
                        # 113 "Kalshi WS DOWN" flaps (only 25 were real socket errors). Each teardown
                        # also cancelled every open rest-kalshi quote and cost queue position.
                        new_tokens, new_tickers = poly_tokens(universe), kalshi_tickers(universe)
                        if set(new_tokens) != set(sub_tokens) or set(new_tickers) != set(sub_tickers):
                            sub_tokens, sub_tickers = new_tokens, new_tickers
                            for t in tasks:
                                t.cancel()
                            pm.stop(); ks.stop()
                            pm, ks = _spawn_wired()   # callbacks attached at construction — never lost
                            tasks = [asyncio.create_task(pm.run()), asyncio.create_task(ks.run())]
                            log.info("[MAKER_RT] trees changed — universe now %d markets; feeds "
                                     "resubscribed (%d tokens, %d tickers).", len(universe),
                                     len(sub_tokens), len(sub_tickers))
                        else:
                            log.info("[MAKER_RT] trees changed — universe now %d markets; subscription "
                                     "set unchanged, feeds kept alive.", len(universe))
            await asyncio.sleep(cfg.debounce_ms / 1000.0)
    except asyncio.CancelledError:
        raise
    finally:
        # SHUTDOWN: cancel ALL live orders FIRST (every exit path — STOP_ALL, HEAD change, signal,
        # exception — runs this), then stop the feeds. An unhedged resting order must never be stranded.
        if pregame_exec is not None:
            try:
                # The shutdown sweep FORCES every cancel inline (see cancel_all), so it does not depend
                # on the off-loop worker still running — which is why the worker is only stopped after.
                n = pregame_exec.cancel_all("shutdown", utcnow())
                log.warning("[MAKER_RT][LIVE] shutdown cancel-all: %d open order(s) cancelled.", n)
            except Exception as exc:  # noqa: BLE001 — a cancel failure must not mask the shutdown
                log.error("[MAKER_RT][LIVE] shutdown cancel-all FAILED: %s", exc)
            try:
                pregame_exec.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("[MAKER_RT][LIVE] off-loop worker shutdown failed: %s", exc)
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
        state.persist_tuning()        # flush the tuning windows on the way out (deploys are frequent)
        # FLUSH the alert backlog LAST — a deploy is exactly when the final message (shutdown cancel-all,
        # a halt) matters, and a queue that is not drained on exit turns "queued" into "lost".
        tgq = getattr(telegram, "queue", None)
        if tgq is not None:
            try:
                tgq.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("[MAKER_RT] telegram flush on shutdown failed: %s", exc)


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
    if "--smoke-sell" in args:
        # SELL-SIDE proof: buy 5 @ market -> REAL unwind to flat -> REST-verify flat. The gate for
        # re-arming after the orphan bug. Runs when live.enabled; does NOT need the arm file.
        from .smoke import run_smoke_sell
        return asyncio.run(run_smoke_sell(cfg, log=log))
    if "--smoke-kalshi" in args:
        # KALSHI-SIDE proof (gate for enabling rest_kalshi): rest an unfillable bid -> confirm -> cancel;
        # buy ~$3 at market -> IOC-unwind -> REST-verify flat via positions. Runs when live.enabled.
        from .smoke import run_smoke_kalshi
        return asyncio.run(run_smoke_kalshi(cfg, log=log))
    _install_signal_handlers(log)             # SIGTERM/SIGBREAK -> graceful cancel-all + exit
    try:
        return asyncio.run(_run(cfg, log))
    except KeyboardInterrupt:
        log.warning("[MAKER_RT] interrupted — shutting down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
