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
        self.connected = False
        self._stop = False

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
                if self.log:
                    self.log.warning("[MAKER_RT] %s socket error: %s — reconnect in %.0fs", self.name, exc, backoff)
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
            self.on_prints(prints)
        self.on_update()


class KalshiFeed(_BaseFeed):
    name = "kalshi"

    def __init__(self, store, tickers: list, *, api_key_id: str = None, signer: Callable = None, **kw) -> None:
        super().__init__(store, **kw)
        self.tickers = list(tickers)
        self.api_key_id = api_key_id
        self.signer = signer
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
        try:
            await self._subscribe(ws)
            while not self._stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    if self.store.need_resync():
                        await self._subscribe(ws)
                    continue
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
        prints = self.store.apply_kalshi(parsing.parse_kalshi(data), time.time())
        if prints:
            self.on_prints(prints)
        self.on_update()


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
                        self.on_user_trade(e)
                    elif e.get("kind") == "poly_user_order":
                        self.on_user_order(e)
        finally:
            self.connected = False
            await ws.close()
