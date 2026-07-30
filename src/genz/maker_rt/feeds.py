"""The three websocket feeds (asyncio) — thin socket shells over the pure BookStore + parsers.

Each feed reconnects with backoff, resubscribes on reconnect, and keeps a ``connected`` flag for the
panel's socket dots. All state application + seq-gap recovery lives in BookStore/parsers (unit-tested
without a socket); here we only do I/O and wire callbacks:

  on_prints(prints) — public trade prints for the shadow fill model
  on_update()       — "books changed" nudge -> the loop debounces a quote refresh

Poly market + Kalshi run in every mode; the Poly USER feed (our real fills) is started ONLY when the
live gate is open.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Optional

from . import config as mrt_config
from . import parsing


def _ssl_ctx():
    """A TLS context that trusts certifi's CA bundle — the system store on Windows/minimal hosts often
    lacks the venues' root CAs (a plain wss connect then fails CERTIFICATE_VERIFY_FAILED)."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - certifi is a transitive dep of requests
        return ssl.create_default_context()


async def _connect(uri: str, headers: Optional[dict] = None):
    """websockets.connect across minor-version header-kwarg differences (additional_headers vs extra)."""
    import websockets
    ctx = _ssl_ctx() if uri.startswith("wss://") else None
    try:
        return await websockets.connect(uri, additional_headers=headers, ping_interval=None,
                                        open_timeout=15, close_timeout=5, ssl=ctx)
    except TypeError:                    # older websockets used extra_headers
        return await websockets.connect(uri, extra_headers=headers, ping_interval=None, ssl=ctx)


class _BaseFeed:
    name = "base"

    def __init__(self, store: Any, *, on_prints: Callable = None, on_update: Callable = None,
                 log: Any = None) -> None:
        self.store = store
        self.on_prints = on_prints or (lambda p: None)
        self.on_update = on_update or (lambda: None)
        self.log = log
        self._connected = False
        self._stop = False
        # RECONNECT HEALTH (surfaced in the digest): ``attempts`` counts retries scheduled after a socket
        # failure; ``success`` counts the re-establishments that actually followed one. attempts > success
        # for any sustained period is the signature of a feed that is down and NOT coming back — the thing
        # we could not see on 2026-07-28 when poly_user dropped 3x overnight.
        self.reconnect_attempts = 0
        self.reconnect_success = 0
        self._awaiting_reconnect = False
        self.callback_errors = 0     # wired-callback raises (application bugs, NOT socket errors)
        self._cb_error_logged_at = -1e18

    def _dispatch(self, what: str, fn: Callable, *args: Any) -> None:
        """Run a wired callback so an APPLICATION error can never masquerade as a socket error.

        These callbacks reach deep into the executor — a Kalshi ``fill`` frame runs the entire
        fill -> hedge -> book chain from inside this coroutine — while ``run()`` treats ANY exception out
        of ``_session`` as "socket error: reconnect". So a raising hedge chain was reported as a network
        blip at WARNING, and the connection was torn down as the remedy: the failure that actually
        mattered was invisible, and the socket paid for it (N10).

        Two decisions here, both deliberate. Log CRITICAL with the traceback, because a callback that
        raises mid-hedge is the most serious thing this process can do quietly. And KEEP the connection
        open: REST is the fill authority of record, so a live socket plus a screaming log is strictly
        better than a reconnect that loses queue position and fixes nothing."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 — an application raise must not kill the feed
            self.callback_errors += 1
            # THROTTLED, because the Poly market channel delivers thousands of frames a minute and a
            # persistently-raising callback would otherwise bury the log it is trying to be visible in.
            # The first one is always logged; after that, once per minute with the running count.
            now = time.monotonic()
            if self.log and (self.callback_errors == 1 or now - self._cb_error_logged_at >= 60.0):
                self._cb_error_logged_at = now
                import traceback
                crit = getattr(self.log, "critical", None) or self.log.error
                crit("[MAKER_RT][CRITICAL] %s callback %r RAISED (%d this run): %s — the socket is kept "
                     "OPEN (REST is the fill authority); this is an application bug, not a network "
                     "error.\n%s", self.name, what, self.callback_errors, exc,
                     traceback.format_exc(limit=8))

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: Any) -> None:
        value = bool(value)
        if value and not self._connected and self._awaiting_reconnect:
            self.reconnect_success += 1                  # a retry actually came back up
            self._awaiting_reconnect = False
        self._connected = value

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any socket error -> reconnect
                self.connected = False
                self.reconnect_attempts += 1
                self._awaiting_reconnect = True
                if self.log:
                    self.log.warning("[MAKER_RT] %s socket error: %s — reconnect in %.0fs "
                                     "(attempt #%d, %d recovered)", self.name, exc, backoff,
                                     self.reconnect_attempts, self.reconnect_success)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class PolyMarketFeed(_BaseFeed):
    name = "poly_market"

    def __init__(self, store, tokens: list, *, ping_s: int = 10, **kw) -> None:
        super().__init__(store, **kw)
        self.tokens = list(tokens)
        self.ping_s = ping_s

    async def _session(self) -> None:
        if not self.tokens:
            self.connected = False
            await asyncio.sleep(5)
            return
        ws = await _connect(mrt_config.POLY_MARKET_WS)
        self.connected = True
        try:
            await ws.send(json.dumps({"assets_ids": self.tokens, "type": "market"}))
            last_ping = time.monotonic()
            while not self._stop:
                if time.monotonic() - last_ping >= self.ping_s:
                    await ws.send("PING")
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.ping_s)
                except asyncio.TimeoutError:
                    continue
                self.store.mark_activity("poly", time.time())     # ANY frame (incl. PONG) = socket alive
                if raw in ("PONG", "PING"):
                    continue
                self._handle(raw)
        finally:
            self.connected = False
            await ws.close()

    def _handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        prints = self.store.apply_poly(parsing.parse_poly_market(data), time.time())
        if prints:
            self._dispatch("on_prints", self.on_prints, prints)
        self._dispatch("on_update", self.on_update)


class KalshiFeed(_BaseFeed):
    name = "kalshi"

    def __init__(self, store, tickers: list, *, api_key_id: str = None, signer: Callable = None,
                 on_fill: Callable = None, **kw) -> None:
        super().__init__(store, **kw)
        self.tickers = list(tickers)
        self.api_key_id = api_key_id
        self.signer = signer
        self.on_fill = on_fill or (lambda e: None)   # OUR real rest-kalshi fills (live only; set when armed)
        self._id = 0

    def _auth_headers(self) -> dict:
        import time as _t
        ts = str(int(_t.time() * 1000))
        sig = self.signer(f"{ts}GET{mrt_config.KALSHI_WS_PATH}") if self.signer else ""
        return {"KALSHI-ACCESS-KEY": self.api_key_id or "", "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sig}

    async def _session(self) -> None:
        if not self.tickers or not self.api_key_id or not self.signer:
            self.connected = False
            await asyncio.sleep(5)
            return
        ws = await _connect(mrt_config.KALSHI_WS, headers=self._auth_headers())
        self.connected = True
        missed_pongs = 0
        try:
            await self._subscribe(ws)
            while not self._stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    # QUIET market (Kalshi has no app-ping): actively PROBE the socket so a quiet-but-alive
                    # connection stays FRESH. TOLERATE a single slow/missed pong (count it) — one hiccup on
                    # a healthy-but-quiet socket must NOT tear the connection down and shred our queue
                    # position (the rest-kalshi flap); only a SECOND consecutive miss (socket really dead)
                    # escalates to the reconnect via the raised TimeoutError.
                    try:
                        await asyncio.wait_for(await ws.ping(), timeout=8)
                    except asyncio.TimeoutError:
                        missed_pongs += 1
                        if missed_pongs >= 2:
                            raise
                        continue
                    missed_pongs = 0
                    self.store.mark_activity("kalshi", time.time())
                    if self.store.need_resync():
                        await self._subscribe(ws)
                    continue
                missed_pongs = 0
                self.store.mark_activity("kalshi", time.time())   # ANY frame = socket alive
                self._handle(raw)
                if self.store.need_resync():                # a seq gap dropped the books -> full resub
                    await self._subscribe(ws)
        finally:
            self.connected = False
            await ws.close()

    async def _subscribe(self, ws) -> None:
        self._id += 1
        await ws.send(json.dumps({"id": self._id, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta", "trade", "fill"],
                                             "market_tickers": list(self.tickers)}}))
        self.store.clear_resync()

    def _handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        events = parsing.parse_kalshi(data)
        for e in events:                                  # OUR fills (private 'fill' channel) -> executor
            if isinstance(e, dict) and e.get("kind") == "kalshi_fill":
                self._dispatch("on_fill", self.on_fill, e)
        prints = self.store.apply_kalshi(events, time.time())
        if prints:
            self._dispatch("on_prints", self.on_prints, prints)
        self._dispatch("on_update", self.on_update)


class PolyUserFeed(_BaseFeed):
    """LIVE ONLY: our real fills (event_type 'trade') and order lifecycle. Started only when armed."""
    name = "poly_user"

    def __init__(self, store, condition_ids: list, creds: dict, *, on_user_trade: Callable = None,
                 on_user_order: Callable = None, **kw) -> None:
        super().__init__(store, **kw)
        self.condition_ids = list(condition_ids)
        self.creds = creds
        self.on_user_trade = on_user_trade or (lambda e: None)
        self.on_user_order = on_user_order or (lambda e: None)   # order PLACEMENT/UPDATE/CANCELLATION

    async def _session(self) -> None:
        ws = await _connect(mrt_config.POLY_USER_WS)
        self.connected = True
        if self.log:
            self.log.info("[MAKER_RT] poly_user socket connected (markets=%d).", len(self.condition_ids))
        try:
            await ws.send(json.dumps({"auth": self.creds, "markets": self.condition_ids, "type": "user"}))
            last_ping = time.monotonic()
            while not self._stop:
                if time.monotonic() - last_ping >= 10:      # keepalive: the server closes an idle socket
                    await ws.send("PING")
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    continue
                if raw in ("PONG", "PING"):
                    continue
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                for e in parsing.parse_poly_user(data):
                    if e.get("kind") == "poly_user_trade":
                        self._dispatch("on_user_trade", self.on_user_trade, e)
                    elif e.get("kind") == "poly_user_order":
                        self._dispatch("on_user_order", self.on_user_order, e)
        finally:
            self.connected = False
            await ws.close()
