"""SMOKE MODE — `python -m src.genz.maker_rt --smoke`. One real, tiny, near-zero-fill order lifecycle.

Runs ONLY when the pre-game gate would arm (enabled + arm file + self-check). It:
  1. sweeps + cancels any stray resting orders from a previous run (both venues),
  2. picks the most liquid PRE-GAME MLB moneyline (ml2) Polymarket token,
  3. rests ONE GTC bid of ~5 shares at max(tick, best_bid - 5 ticks) -- deep enough that a fill is
     ~impossible (rest-leg notional is capped by maker_rt.live.quote_usd_max),
  4. confirms the resting order BOTH via the Poly USER websocket AND an authoritative REST read,
  5. holds ~30s, then cancels and confirms the cancellation (REST + socket),
  6. prints the full lifecycle and exits 0.

Safety: a SIGINT/SIGTERM/SIGBREAK during the hold fires a synchronous cancel-all BEFORE exit (the
shutdown-safety proof). Any UNEXPECTED state -- no order id, or an unexpected real fill -- triggers a
cancel-all and a non-zero exit. This never quotes continuously; it is a single-shot plumbing test.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any, Optional

from .caps import LiveCaps
from .clients import build_pregame_order_clients
from .feeds import PolyUserFeed
from .live import LiveGate
from .orders import PolyOrderClient
from .quotes import round_down_tick
from .store import BookStore
from .universe import build_universe, load_trees

# Process-global handle the signal handler reads (a handler cannot take args). Set once an order rests.
_SHUTDOWN: dict = {"poly": None, "oids": [], "log": None, "tele": None}


# --------------------------------------------------------------------------- #
# small output helpers                                                          #
# --------------------------------------------------------------------------- #
def _p(msg: str) -> None:
    print(msg, flush=True)


def _emit(msg: str, log: Any = None, tele: Any = None) -> None:
    print(msg, flush=True)
    if log:
        try:
            log.warning(msg)
        except Exception:  # noqa: BLE001
            pass
    if tele:
        try:
            tele(msg)
        except Exception:  # noqa: BLE001
            pass


def _telegram_sender(log: Any):
    key, chat = os.environ.get("TELEGRAM_BOT_KEY"), os.environ.get("TELEGRAM_GROUP_ID")
    if not (key and chat):
        return None
    try:
        from ...telegram import send_message
    except Exception:  # noqa: BLE001
        return None
    return lambda text: send_message(key, chat, text, log)


# --------------------------------------------------------------------------- #
# shutdown safety (cancel-all on signal)                                        #
# --------------------------------------------------------------------------- #
def _install_shutdown_handlers(poly: Any, oids: list, log: Any, tele: Any) -> None:
    _SHUTDOWN.update(poly=poly, oids=list(oids), log=log, tele=tele)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _shutdown_handler)
        except (ValueError, OSError, RuntimeError):
            pass


def _cancel_tracked(poly: Any, oids: list, log: Any = None, tele: Any = None) -> int:
    """Cancel every tracked order id then cancel-all (belt-and-suspenders). Returns how many tracked
    ids were cancelled. Pure w.r.t. process exit so it is unit-testable."""
    n = 0
    for oid in oids or []:
        try:
            poly.cancel_order(oid)
            n += 1
        except Exception as exc:  # noqa: BLE001
            _emit(f"[MAKER_RT][SMOKE] cancel {oid} on shutdown FAILED: {exc}", log, None)
    try:
        poly.cancel_all()
    except Exception as exc:  # noqa: BLE001
        _emit(f"[MAKER_RT][SMOKE] cancel_all on shutdown FAILED: {exc}", log, None)
    return n


def _shutdown_handler(signum, frame) -> None:  # noqa: ANN001
    poly, log, tele = _SHUTDOWN.get("poly"), _SHUTDOWN.get("log"), _SHUTDOWN.get("tele")
    _emit(f"[MAKER_RT][SMOKE] SHUTDOWN signal {signum} -> cancel-all firing BEFORE exit.", log, tele)
    n = _cancel_tracked(poly, _SHUTDOWN.get("oids") or [], log, tele)
    _emit(f"[MAKER_RT][SMOKE] cancel-all fired on shutdown (cancelled {n} tracked order(s)); exiting.",
          log, tele)
    os._exit(0)


def _cancel_all(poly: Any, log: Any) -> None:
    try:
        poly.cancel_all()
        _p("[SMOKE] cancel_all() issued.")
    except Exception as exc:  # noqa: BLE001
        _p(f"[SMOKE] cancel_all() FAILED: {exc}")


# --------------------------------------------------------------------------- #
# token selection + status reads                                                #
# --------------------------------------------------------------------------- #
def _phase(kickoff_ts: float, now_ts: float, cutoff: float) -> str:
    if now_ts < kickoff_ts - cutoff:
        return "pre"
    return "gap" if now_ts < kickoff_ts else "inplay"


def _select_mlb_token(cfg: Any, poly: Any, log: Any) -> Optional[dict]:
    """Most liquid PRE-GAME MLB moneyline (ml2) Poly token, with its deep-bid price + size inputs.
    Prefers a token whose deep price >= $0.20 (so ~5 shares clears the venue $1 minimum)."""
    now_ts = time.time()
    cutoff = float(getattr(cfg, "expire_before_kickoff_s", 120))
    universe = build_universe(load_trees(), now_ts, max_games=cfg.max_games,
                              expire_before_kickoff_s=cfg.expire_before_kickoff_s,
                              horizon_hours=cfg.inplay.horizon_hours)
    raw: list = []
    for qm in universe:
        if getattr(qm, "sport", "") != "mlb" or str(getattr(qm, "market_key", "")) != "ml2":
            continue
        if _phase(qm.kickoff_ts, now_ts, cutoff) != "pre":
            continue
        for side, node in qm.sides.items():
            tok = getattr(node, "poly_token_id", None)
            if tok:
                raw.append((qm, side, tok))
    if not raw:
        return None
    scored: list = []
    for qm, side, tok in raw:
        try:
            tick_s, neg = poly._tick_and_negrisk(tok)
            tick = float(tick_s)
            bids = (poly.get_orderbook(tok) or {}).get("bids") or []      # descending (price, size)
            if not bids:
                continue
            best_bid = float(bids[0][0])
            depth = sum(float(s) for _p2, s in bids)
            deep = round_down_tick(max(tick, best_bid - 5 * tick), tick)
            if deep < tick - 1e-9:
                continue
            scored.append({"game": qm.game, "side": side, "token": tok, "tick": tick,
                           "neg": bool(neg), "best_bid": best_bid, "deep": deep, "depth": depth})
        except Exception as exc:  # noqa: BLE001 - a token whose book/tick we can't read is skipped
            if log:
                log.warning("[SMOKE] book/tick fetch failed for %s...: %s", str(tok)[:10], exc)
    if not scored:
        return None
    clean = [s for s in scored if s["deep"] >= 0.20 - 1e-9]       # 5 shares clears the $1 venue minimum
    pool = clean or scored
    pool.sort(key=lambda s: s["depth"], reverse=True)
    return pool[0]


def _rest_status(poly: Any, oid: str) -> dict:
    """Authoritative REST read of one order, normalized (never raises)."""
    try:
        o = poly.get_order(oid)
    except Exception as exc:  # noqa: BLE001 - a gone order may 404 -> treat as not-found
        return {"status": "NOT-FOUND/ERROR", "error": str(exc), "raw": None}
    if not isinstance(o, dict):
        return {"status": str(o), "raw": o}
    return {"status": str(o.get("status") or o.get("state") or "?"),
            "size_matched": o.get("size_matched"), "market": o.get("market"), "raw": o}


def _matched(st: dict) -> float:
    try:
        return float(st.get("size_matched") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_cancelled(st: dict) -> bool:
    s = str(st.get("status") or "").upper()
    return s in ("CANCELED", "CANCELLED") or "NOT-FOUND" in s or "ERROR" in s


# --------------------------------------------------------------------------- #
# user-socket watcher                                                           #
# --------------------------------------------------------------------------- #
class _UserWatch:
    def __init__(self) -> None:
        self.orders: dict = {}
        self.trades: list = []

    def on_order(self, e: dict) -> None:
        oid = e.get("order_id")
        if oid:
            self.orders[oid] = e

    def on_trade(self, e: dict) -> None:
        self.trades.append(e)


async def _await_order_seen(watch: _UserWatch, oid: str, timeout: float) -> Optional[dict]:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if oid in watch.orders:
            return watch.orders[oid]
        await asyncio.sleep(0.25)
    return None


# --------------------------------------------------------------------------- #
# runner                                                                        #
# --------------------------------------------------------------------------- #
async def run_smoke(cfg: Any, *, log: Any = None, hold_s: float = 30.0,
                    shutdown_proof: bool = False) -> int:
    _p("=" * 78)
    _p("maker_rt --smoke  (ONE real tiny near-zero-fill order: place -> confirm -> hold -> cancel)")
    _p("=" * 78)
    tele = _telegram_sender(log)

    # 1) real clients + gate check (runs ONLY when the gate would arm) --------
    kalshi, poly = build_pregame_order_clients(cfg, log=log)
    gate = LiveGate(cfg, kalshi_client=kalshi, poly_client=poly, log=log)
    g = gate.evaluate()
    if not g.armed:
        _p(f"SMOKE REFUSED: pre-game gate would NOT arm -> {g.reason} | checks={g.checks}")
        return 2
    ipg = gate.evaluate_inplay()
    _p(f"gate: pre-game ARMED ({g.checks}); in-play {ipg.reason!r} (must stay refused).")

    caps = LiveCaps(cfg.live, telegram=tele, log=log)

    # 2) startup stray-order sweep (both venues) -----------------------------
    swept = _startup_stray_cancel(poly, kalshi, log)
    _p(f"startup stray-cancel: {swept} order(s) cancelled.")

    # 3) pick the most-liquid pre-game MLB ml2 token -------------------------
    sel = _select_mlb_token(cfg, poly, log)
    if sel is None:
        _p("SMOKE REFUSED: no liquid PRE-GAME MLB moneyline (ml2) token available right now.")
        return 3
    price, tick, neg = sel["deep"], sel["tick"], sel["neg"]
    size = caps.size_shares(price)
    notional = round(size * price, 4)
    proj = caps.projected_pair_stake(price, size, 1.0 - price, size)     # worst-case pair stake
    ok, reason = caps.can_place(proj)
    if not ok:
        _p(f"SMOKE REFUSED by caps: {reason} (projected pair ${proj:.2f}, "
           f"stake_today ${caps.stake_today:.2f}, cap ${caps.max_daily_stake_usd:.0f}).")
        return 5
    _p(f"selected: {sel['game']} [{sel['side']}] token {sel['token'][:12]}... "
       f"best_bid={sel['best_bid']:.4f} deep_bid={price:.4f} (best_bid-5t) tick={tick} "
       f"neg_risk={neg} depth={sel['depth']:.0f}")
    _p(f"order: BUY {size} shares @ {price:.4f} (~${notional:.2f}; rest-leg cap ${caps.quote_usd_max:.0f})")

    # 4) place ONE GTC bid ---------------------------------------------------
    order_client = PolyOrderClient(poly, log=log)
    t_place = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    res = order_client.rest(sel["token"], price, size, tick_size=tick, neg_risk=neg)
    oid = res.get("order_id")
    _p(f"[{t_place}] PLACE -> status={res.get('status')} order_id={oid}")
    if not oid:
        _p(f"SMOKE FAIL: no order_id in place result: {res}. Running cancel-all.")
        _cancel_all(poly, log)
        return 4
    caps.on_open()
    _install_shutdown_handlers(poly, [oid], log, tele)
    _emit(f"[MAKER_RT][SMOKE] rested {size}@{price:.4f} on {sel['game']} [{sel['side']}] "
          f"order_id={oid} -- confirming + holding {hold_s:.0f}s.", log, tele)

    # 5) confirm placement: REST (authoritative) + user socket ----------------
    st = _rest_status(poly, oid)
    _p(f"REST confirm: status={st.get('status')} size_matched={st.get('size_matched')} "
       f"market={st.get('market')}")
    if _matched(st) > 0:
        _p("UNEXPECTED: order shows a fill at placement -> cancel-all + abort.")
        _cancel_all(poly, log)
        return 6
    cond = st.get("market")

    watch = _UserWatch()
    feed = feed_task = None
    try:
        creds = poly.derive_l2_creds()
    except Exception as exc:  # noqa: BLE001
        creds = None
        _p(f"WARN: L2 creds derive failed (user socket skipped): {exc}")
    if creds and creds.get("apiKey"):
        feed = PolyUserFeed(BookStore(), [cond] if cond else [], creds,
                            on_user_trade=watch.on_trade, on_user_order=watch.on_order, log=log)
        feed_task = asyncio.create_task(feed.run())
        seen = await _await_order_seen(watch, oid, timeout=15.0)
        if seen:
            _p(f"USER-SOCKET confirm: order {oid} observed (status={seen.get('status')}, "
               f"price={seen.get('price')}).")
        else:
            _p("USER-SOCKET confirm: order not observed within 15s (REST read is authoritative).")
    else:
        _p("USER-SOCKET confirm: SKIPPED (no L2 creds).")

    # 6) hold (a signal here fires cancel-all before exit) -------------------
    if shutdown_proof:
        _p(f"AWAITING_SHUTDOWN_SIGNAL order_id={oid} (holding up to {hold_s:.0f}s; send CTRL_BREAK now)")
    else:
        _p(f"HOLDING {hold_s:.0f}s with the order resting...")
    await asyncio.sleep(hold_s)

    # re-check for an unexpected fill during the hold
    st_hold = _rest_status(poly, oid)
    if _matched(st_hold) > 0:
        _p(f"UNEXPECTED: order filled during hold (size_matched={st_hold.get('size_matched')}) "
           "-> cancel-all + abort.")
        _cancel_all(poly, log)
        _stop_feed(feed, feed_task)
        return 6

    # 7) cancel + confirm cancellation (REST + socket) -----------------------
    t_cancel = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        order_client.cancel(oid)
    except Exception as exc:  # noqa: BLE001
        _p(f"CANCEL error: {exc} -> cancel-all fallback.")
        _cancel_all(poly, log)
    caps.on_close()
    st2 = _rest_status(poly, oid)
    cancelled = _is_cancelled(st2)
    _p(f"[{t_cancel}] CANCEL -> REST status={st2.get('status')} (cancelled={cancelled})")
    ws_cancel = watch.orders.get(oid)
    _p(f"USER-SOCKET cancel event: {ws_cancel.get('status') if ws_cancel else 'not observed'}")
    await _drain_feed(feed, feed_task)

    # 8) lifecycle summary ---------------------------------------------------
    _p("-" * 78)
    _p("LIFECYCLE:")
    _p(f"  order_id     : {oid}")
    _p(f"  token        : {sel['token']}")
    _p(f"  placed       : {t_place}  BUY {size} @ {price:.4f}  (~${notional:.2f})")
    _p(f"  rest confirm : {st.get('status')}  (size_matched={st.get('size_matched')})")
    _p(f"  socket seen  : {'yes' if oid in watch.orders else 'no'}")
    _p(f"  cancelled    : {t_cancel}  REST={st2.get('status')}  cancelled={cancelled}")
    _p("-" * 78)
    if not cancelled:
        _emit("[MAKER_RT][SMOKE] order NOT confirmed cancelled -> cancel-all + non-zero exit.", log, tele)
        _cancel_all(poly, log)
        return 7
    _emit(f"[MAKER_RT][SMOKE] PASS -- placed + confirmed + cancelled cleanly (order_id={oid}).", log, tele)
    _p("=" * 78)
    _p("SMOKE PASS")
    _p("=" * 78)
    return 0


async def run_smoke_sell(cfg: Any, *, log: Any = None) -> int:
    """SELL-SIDE proof: BUY ~5 shares of a liquid pre-game market at market, then exercise the REAL unwind
    path (place_market_sell) to flat and REST-VERIFY flat. Prints both venue transactions + the realized
    round-trip cost. Runs when live.enabled (clients present) -- does NOT need the arm file (this is the
    gate for RE-arming after the -$2.35 orphan). Any non-flat result -> non-zero exit."""
    _p("=" * 78)
    _p("maker_rt --smoke-sell  (buy 5 @ market -> REAL unwind to flat -> REST-verify flat)")
    _p("=" * 78)
    kalshi, poly = build_pregame_order_clients(cfg, log=log)
    if poly is None:
        _p("SMOKE-SELL REFUSED: maker_rt.live.enabled is false (no order clients).")
        return 2
    try:
        _p(f"poly balance readable: {bool(poly.get_balance())}")
    except Exception as exc:  # noqa: BLE001
        _p(f"SMOKE-SELL REFUSED: poly balance read failed: {exc}")
        return 2
    sel = _select_mlb_token(cfg, poly, log)
    if sel is None:
        _p("SMOKE-SELL REFUSED: no liquid pre-game MLB (ml2) token available right now.")
        return 3
    token, tick, neg = sel["token"], sel["tick"], sel["neg"]
    try:
        ob = poly.get_orderbook(token)
        asks = ob.get("asks") or []
        best_ask = float(asks[0][0]) if asks else None
    except Exception as exc:  # noqa: BLE001
        _p(f"SMOKE-SELL REFUSED: orderbook read failed: {exc}")
        return 3
    if best_ask is None:
        _p("SMOKE-SELL REFUSED: no ask liquidity to buy into.")
        return 3
    # CTF approval (a missing approval is exactly what silently rejected the unwind sell).
    try:
        appr = poly.ctf_allowance_ok(token)
        _p(f"CTF sell approval present: {appr}")
        if not appr:
            _p("setting one-time CTF approval ...")
            poly.set_ctf_approval(token)
    except Exception as exc:  # noqa: BLE001
        _p(f"CTF approval check/set failed (continuing): {exc}")

    shares = 5
    buy_px = round(best_ask + 2 * tick, 6)                        # marketable buy (cross up 2 ticks)
    _p(f"selected {sel['game']} token {token[:12]}... best_ask={best_ask} buy@{buy_px} x{shares}")
    _install_shutdown_handlers(poly, [], log, _telegram_sender(log))   # a signal cancels+cleans
    buy = poly.place_order(token, buy_px, shares, "BUY", order_type="FAK", tick_size=tick, neg_risk=neg)
    _p(f"BUY : status={buy.get('status')} shares={buy.get('shares')} avg={buy.get('avg_price')} "
       f"id={buy.get('order_id')}")
    # Settlement is NOT instant: the CLOB balance endpoint reports the PRE-buy amount for a few seconds
    # after a fill (this exact race made the first smoke read 0 shares after a 5-share buy). Poll (forcing
    # a re-sync) until the held balance reflects the fill, then trust it as the amount to unwind.
    pos = await asyncio.to_thread(poly.settle_conditional_balance, token, (lambda b: b >= 1.0),
                                  timeout_s=12.0)
    _p(f"position after BUY (REST, settled): {pos} shares")
    if pos is None or pos < 1.0:
        _p("SMOKE-SELL: buy filled < 1 share (thin book) — nothing to unwind; treating as INCONCLUSIVE.")
        return 4
    _p("--- exercising the REAL unwind path (place_market_sell, FAK marketable) ---")
    sell = poly.place_market_sell(token, pos)
    _p(f"SELL: status={sell.get('status')} shares={sell.get('shares')} avg={sell.get('avg_price')} "
       f"id={sell.get('order_id')}")
    # Same settlement race on the way back to flat — poll for the balance to DROP instead of a fixed sleep.
    pos2 = await asyncio.to_thread(poly.settle_conditional_balance, token, (lambda b: b <= 0.5),
                                   timeout_s=8.0)
    flat = pos2 is not None and pos2 <= 0.5
    _p(f"position after UNWIND (REST, settled): {pos2} shares -> {'FLAT' if flat else 'NOT FLAT'}")
    buy_avg = float(buy.get("avg_price") or buy_px)
    sell_avg = float(sell.get("avg_price") or 0.0)
    realized = round((buy_avg - sell_avg) * float(pos), 4)
    _p("-" * 78)
    _p("VENUE-CONFIRMED ROUND TRIP:")
    _p(f"  BUY  order {buy.get('order_id')}  {buy.get('shares')} @ {buy_avg}")
    _p(f"  SELL order {sell.get('order_id')}  {sell.get('shares')} @ {sell_avg}")
    _p(f"  realized cost (spread+fees): ${realized}")
    _p(f"  final position (REST-verified): {pos2} shares")
    _p("-" * 78)
    if not flat:
        _p("SMOKE-SELL FAIL: position NOT flat after unwind — the SELL path is still broken. Do NOT re-arm.")
        _cancel_all(poly, log)
        return 5
    _p("SMOKE-SELL PASS: the REAL unwind flattened the position and it is REST-verified flat.")
    _p("=" * 78)
    return 0


def _kalshi_pos(kalshi: Any, ticker: str) -> Optional[float]:
    """Net |contracts| on ``ticker`` from the Kalshi portfolio positions endpoint (0 if flat/absent)."""
    try:
        resp = kalshi.get_positions()
    except Exception:  # noqa: BLE001
        return None
    rows = resp.get("market_positions") if isinstance(resp, dict) else (resp or [])
    if not rows and isinstance(resp, dict):
        rows = resp.get("positions") or resp.get("data") or []
    for p in rows or []:
        if isinstance(p, dict) and (p.get("ticker") == ticker or p.get("market_ticker") == ticker):
            for k in ("position", "net_position", "count"):
                if p.get(k) is not None:
                    return abs(float(p[k]))
            return 0.0
    return 0.0


def _kalshi_ask_from_raw(kalshi: Any, ticker: str, side: str) -> tuple:
    """Best ask (price to BUY ``side``) + total depth from the RAW Kalshi orderbook (orderbook_fp /
    yes_dollars / no_dollars). A NO bid at price p IS a YES ask at 1-p (and vice-versa), so the asks for
    ``side`` are the OPPOSITE side's bids converted. Returns (best_ask, depth) or (None, 0.0). (The exec
    adapter's own ask-ladder parser predates the orderbook_fp format; the maker itself reads the WS book.)"""
    try:
        raw = (kalshi.get_orderbook(ticker, side=side) or {}).get("raw") or {}
    except Exception:  # noqa: BLE001
        return None, 0.0
    ob = raw.get("orderbook_fp") or raw.get("orderbook") or raw
    opp = "no_dollars" if str(side).upper() == "YES" else "yes_dollars"
    levels = ob.get(opp) if isinstance(ob, dict) else None
    best, depth = None, 0.0
    for lvl in levels or []:
        try:
            price, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        ask = round(1.0 - price, 4)                  # opposite-side bid @ price -> our ask @ 1-price
        depth += size
        if best is None or ask < best:
            best = ask
    return best, depth


def _select_kalshi_market(cfg: Any, kalshi: Any, log: Any) -> Optional[dict]:
    """Most liquid PRE-GAME Kalshi market (any sport) with a readable ask — for the buy+unwind leg.
    Prefers a mid-priced market (0.15-0.85) so ~$3 buys and unwinds cleanly."""
    now_ts = time.time()
    cutoff = float(getattr(cfg, "expire_before_kickoff_s", 120))
    universe = build_universe(load_trees(), now_ts, max_games=cfg.max_games,
                              expire_before_kickoff_s=cfg.expire_before_kickoff_s,
                              horizon_hours=cfg.inplay.horizon_hours)
    seen: set = set()
    scored: list = []
    for qm in universe:
        if _phase(qm.kickoff_ts, now_ts, cutoff) != "pre":
            continue
        for _side, node in qm.sides.items():
            tk = getattr(node, "kalshi_ticker", None)
            k_side = getattr(node, "kalshi_side", None)
            if not tk or not k_side or (tk, k_side) in seen:
                continue
            seen.add((tk, k_side))
            best_ask, depth = _kalshi_ask_from_raw(kalshi, tk, k_side)
            if best_ask is None or depth <= 0:
                continue
            scored.append({"game": qm.game, "ticker": tk, "side": k_side,
                           "best_ask": best_ask, "depth": depth})
    if not scored:
        return None
    clean = [s for s in scored if 0.15 - 1e-9 <= s["best_ask"] <= 0.85 + 1e-9]
    pool = clean or scored
    pool.sort(key=lambda s: s["depth"], reverse=True)
    return pool[0]


async def run_smoke_kalshi(cfg: Any, *, log: Any = None) -> int:
    """SMOKE-KALSHI (the gate for enabling the rest_kalshi live direction). Real money, tiny:
    (i) rest ONE deep unfillable bid -> REST-confirm resting -> cancel -> confirm gone;
    (ii) buy ~$3 at market (IOC) -> IOC-unwind (place_market_sell) -> REST-verify FLAT via positions.
    Pastes both venue-confirmed receipts. Runs when live.enabled; does NOT need the arm file."""
    _p("=" * 78)
    _p("maker_rt --smoke-kalshi  (rest unfillable bid -> cancel; buy $3 -> IOC-unwind -> REST-verify flat)")
    _p("=" * 78)
    kalshi, poly = build_pregame_order_clients(cfg, log=log)
    if kalshi is None:
        _p("SMOKE-KALSHI REFUSED: maker_rt.live.enabled is false (no order clients).")
        return 2
    try:
        _p(f"kalshi balance readable: {bool(kalshi.get_balance())}")
    except Exception as exc:  # noqa: BLE001
        _p(f"SMOKE-KALSHI REFUSED: kalshi balance read failed: {exc}")
        return 2
    from .orders import KalshiOrderClient
    koc = KalshiOrderClient(kalshi, log=log)
    sel = _select_kalshi_market(cfg, kalshi, log)
    if sel is None:
        _p("SMOKE-KALSHI REFUSED: no liquid pre-game Kalshi market available right now.")
        return 3
    ticker, side, best_ask = sel["ticker"], sel["side"], sel["best_ask"]
    _p(f"selected {sel['game']} {ticker} {side} best_ask={best_ask}")
    _install_shutdown_handlers(poly, [], log, _telegram_sender(log))

    # (i) RESTING proof: a deep unfillable bid (2c) rests, is confirmed, then cancelled + confirmed gone.
    rest_px = 0.02
    _p("--- (i) resting proof: rest a deep unfillable bid, confirm resting, cancel, confirm gone ---")
    r = koc.rest(ticker, side, rest_px, 1)
    oid = r.get("order_id")
    _p(f"REST : status={r.get('status')} id={oid} @ {rest_px} x1")
    if not oid:
        _p(f"SMOKE-KALSHI FAIL: rest returned no order_id: {r}")
        return 4
    await asyncio.sleep(1.5)
    st = koc.order_status(oid)
    _p(f"REST-confirm: present={bool(st)} status={st.get('status')}")
    cx = koc.cancel(oid)
    await asyncio.sleep(1.5)
    st2 = koc.order_status(oid)
    gone = not st2 or str(st2.get("status") or "").lower() in ("canceled", "cancelled")
    _p(f"CANCEL: resp={cx}; post-cancel present={bool(st2)} -> {'GONE' if gone else 'STILL PRESENT'}")
    if not gone:
        _p("SMOKE-KALSHI FAIL: resting order not confirmed cancelled.")
        return 5

    # (ii) UNWIND proof: buy ~$3 through THE EXACT HEDGER path (LiveHedger.hedge -> kalshi IOC), then
    # IOC-unwind (place_market_sell — the exact executor unwind path) -> REST-verify flat via positions.
    import math
    from .hedge import LiveHedger
    n = max(1, math.ceil(3.0 / max(best_ask, 0.05)))
    _p(f"--- (ii) unwind proof: buy ~$3 ({n}) via LiveHedger.hedge (kalshi IOC), IOC-unwind, verify flat ---")
    hedger = LiveHedger(kalshi_client=kalshi, poly_client=poly, poly_rate=cfg.poly_fee_rate, log=log)
    hres = hedger.hedge({"token_id": "smoke", "side": "BUY", "price": best_ask, "size": n},
                        {"ticker": ticker, "side": side, "best_ask": best_ask})
    buy_detail = (getattr(hres, "detail", None) or {}).get("kalshi") or {}
    buy_id = buy_detail.get("order_id")
    buy_avg = getattr(hres, "hedge_avg_price", None)
    filled = int(round(float(getattr(hres, "hedged_shares", 0) or 0)))
    _p(f"HEDGE-BUY (LiveHedger.hedge -> kalshi IOC): status={getattr(hres,'status',None)} filled={filled} "
       f"avg={buy_avg} id={buy_id}")
    if filled < 1:
        _p("SMOKE-KALSHI: hedge buy filled 0 (thin book) — nothing to unwind; INCONCLUSIVE.")
        return 4
    _p(f"position after BUY (REST): {_kalshi_pos(kalshi, ticker)} contracts")
    sell = kalshi.place_market_sell(ticker, side, filled, client_order_id="mrt-smoke-unwind")
    _p(f"SELL: status={sell.get('status')} fill_count={sell.get('fill_count')} avg={sell.get('avg_price')} "
       f"id={sell.get('order_id')}")
    flat_pos = None
    for _ in range(6):                                   # settle poll (mirrors the executor verify)
        await asyncio.sleep(0.6)
        flat_pos = _kalshi_pos(kalshi, ticker)
        if flat_pos is not None and abs(flat_pos) <= 0.5:
            break
    flat = flat_pos is not None and abs(flat_pos) <= 0.5
    _p(f"position after UNWIND (REST-verified): {flat_pos} -> {'FLAT' if flat else 'NOT FLAT'}")
    _p("-" * 78)
    _p("VENUE-CONFIRMED RECEIPTS:")
    _p(f"  REST order {oid}  @ {rest_px}  -> cancelled (confirmed gone)")
    _p(f"  BUY  order {buy_id}  {filled} @ {buy_avg}  (via LiveHedger.hedge)")
    _p(f"  SELL order {sell.get('order_id')}  {sell.get('fill_count')} @ {sell.get('avg_price')}")
    _p(f"  final position (REST-verified): {flat_pos} contracts")
    _p("-" * 78)
    if not flat:
        _p("SMOKE-KALSHI FAIL: position NOT flat after unwind — do NOT enable rest_kalshi.")
        _cancel_all(poly, log)
        return 5
    _p("SMOKE-KALSHI PASS: resting confirmed+cancelled AND buy IOC-unwound to REST-verified flat.")
    _p("=" * 78)
    return 0


def _stop_feed(feed: Any, feed_task: Any) -> None:
    if feed:
        feed.stop()
    if feed_task:
        feed_task.cancel()


async def _drain_feed(feed: Any, feed_task: Any) -> None:
    _stop_feed(feed, feed_task)
    if feed_task:
        await asyncio.gather(feed_task, return_exceptions=True)


def _startup_stray_cancel(poly: Any, kalshi: Any, log: Any, prefix: str = "mrt-") -> int:
    """Cancel any resting order left by a previous run. On Poly every resting order on the account is
    ours -> cancel it. On Kalshi cancel only those whose client_order_id starts with our ``prefix``."""
    n = 0
    try:
        oo = poly.open_orders()
        orders = oo if isinstance(oo, list) else ((oo or {}).get("data") or (oo or {}).get("orders") or [])
        for o in orders or []:
            oid = (o.get("id") or o.get("orderID") or o.get("order_id")) if isinstance(o, dict) else None
            if oid:
                try:
                    poly.cancel_order(oid)
                    n += 1
                    if log:
                        log.info("[SMOKE] stray-cancel poly %s", oid)
                except Exception as exc:  # noqa: BLE001
                    if log:
                        log.warning("[SMOKE] stray-cancel poly %s failed: %s", oid, exc)
    except Exception as exc:  # noqa: BLE001
        if log:
            log.warning("[SMOKE] poly open-orders list failed: %s", exc)
    try:
        resp = kalshi.get_orders(status="resting")
        korders = resp.get("orders") if isinstance(resp, dict) else (resp or [])
        for o in korders or []:
            coid = str((o or {}).get("client_order_id") or "")
            oid = (o or {}).get("order_id") or (o or {}).get("id")
            if oid and coid.startswith(prefix):
                try:
                    kalshi.cancel_order(oid)
                    n += 1
                    if log:
                        log.info("[SMOKE] stray-cancel kalshi %s", oid)
                except Exception as exc:  # noqa: BLE001
                    if log:
                        log.warning("[SMOKE] stray-cancel kalshi %s failed: %s", oid, exc)
    except Exception as exc:  # noqa: BLE001
        if log:
            log.warning("[SMOKE] kalshi orders list failed: %s", exc)
    return n
