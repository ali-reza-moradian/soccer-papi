"""maker_rt — the real-time Kalshi/Polymarket maker/hedger (single asyncio process).

Ships in SHADOW mode: real websockets, paper quotes, ZERO orders. The live-order path (orders.py +
hedge.LiveHedger) is fully built and unit-tested but HARD-LOCKED behind LiveGate (config enabled AND
an on-disk arm file AND a startup self-check). Run: ``python -m src.genz.maker_rt``.
"""
