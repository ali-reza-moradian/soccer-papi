"""[LOOP] attribution: where the event loop's time actually goes — as sums and counts, not just a max.

A max answers "how bad did it get once". It cannot answer "how much of that minute did this subsystem
consume", which is the only number that can prove a de-stall: a bucket that is expensive because ONE
call blocked for five seconds needs a different fix from one that is expensive because it was called
4,909 times an hour. So every bucket carries **sum + count + max** for the reporting window.

Two blocks that were previously unattributed are buckets here too:

  * ``ws_cb``     — the websocket callbacks. They run on the SAME event loop as the trading tick (the
                    feeds are asyncio tasks, not threads), so a slow ``consume_prints`` is loop lag
                    that the old ``blockers`` map never saw.
  * ``heartbeat`` — the heartbeat/summary/persist block: JSON builds plus several file writes, every
                    2.5s, all synchronous.

NESTING: ``cancel`` and ``telegram`` are measured where the work happens, which means they are SUBSETS
of whichever outer bucket contained them (a reprice cancel is inside ``quotes``; an instant alert can
be inside ``quotes``, ``fill_poll`` or ``ws_cb``). They are reported anyway because the storm they
measure is the point — just never add the buckets together and expect the tick.

Measurement must never be able to break trading, so every method here is total: an unknown bucket is
created on the fly and a timer whose body raises still records its elapsed time.
"""
from __future__ import annotations

import time
from typing import Any, Optional

#: Declared up front so the log line keeps a stable shape even on a window where a bucket never fired.
BUCKETS = ("quotes", "cancel", "fill_poll", "reconcile", "settle", "ws_cb", "heartbeat", "telegram",
           # The 8-hourly balance audit's LOOP-SIDE cost — a clock comparison and a queue put, three
           # times a day plus a cheap drain every heartbeat. It has its own bucket rather than hiding
           # inside ``heartbeat`` precisely because "this reporting job is cheap" is the assumption
           # that cost us a 17-hour blackout of the fill-poll backstop. Now it is a number.
           "balance")


class _Timer:
    """``with stats.timer('fill_poll'):`` — records elapsed ms even when the body raises."""

    __slots__ = ("_stats", "_bucket", "_t0")

    def __init__(self, stats: "LoopStats", bucket: str) -> None:
        self._stats, self._bucket, self._t0 = stats, bucket, 0.0

    def __enter__(self) -> "_Timer":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        self._stats.add(self._bucket, (time.monotonic() - self._t0) * 1000.0)
        return False                                  # never swallow an exception


class LoopStats:
    """Per-bucket (sum_ms, count, max_ms) for one reporting window, plus the lag reservoir."""

    def __init__(self) -> None:
        self.lags: list = []                          # per-tick lag over target, ms
        self.ticks = 0
        self.reset()

    # -- buckets -------------------------------------------------------------
    def reset(self) -> None:
        """Start a new reporting window (buckets AND the lag reservoir)."""
        self._sum: dict = {b: 0.0 for b in BUCKETS}
        self._n: dict = {b: 0 for b in BUCKETS}
        self._max: dict = {b: 0.0 for b in BUCKETS}
        self.lags = []
        self.ticks = 0

    def add(self, bucket: str, ms: float) -> None:
        """Record one call of ``bucket`` costing ``ms``. Total: an unknown bucket is created."""
        try:
            ms = float(ms)
        except (TypeError, ValueError):
            return
        if bucket not in self._sum:
            self._sum[bucket], self._n[bucket], self._max[bucket] = 0.0, 0, 0.0
        self._sum[bucket] += ms
        self._n[bucket] += 1
        if ms > self._max[bucket]:
            self._max[bucket] = ms

    def timer(self, bucket: str) -> _Timer:
        return _Timer(self, bucket)

    def sum_ms(self, bucket: str) -> float:
        return float(self._sum.get(bucket, 0.0))

    def count(self, bucket: str) -> int:
        return int(self._n.get(bucket, 0))

    def max_ms(self, bucket: str) -> float:
        return float(self._max.get(bucket, 0.0))

    # -- lag ------------------------------------------------------------------
    def note_tick(self, lag_ms: float) -> None:
        self.ticks += 1
        self.lags.append(max(0.0, float(lag_ms)))

    def lag_percentiles(self) -> tuple:
        """(p50, p99, max) of this window's lag in ms — (0,0,0) on an empty window."""
        if not self.lags:
            return 0.0, 0.0, 0.0
        o = sorted(self.lags)
        return o[len(o) // 2], o[min(len(o) - 1, int(0.99 * (len(o) - 1) + 0.5))], o[-1]

    # -- rendering ------------------------------------------------------------
    def worst_bucket(self) -> Optional[str]:
        """The bucket that consumed the most WALL TIME this window (sum, not max) — the thing to fix."""
        live = [(b, s) for b, s in self._sum.items() if self._n.get(b, 0) > 0]
        return max(live, key=lambda kv: kv[1])[0] if live else None

    def render(self) -> str:
        """'fill_poll 4820ms/61x/312max · quotes 1180ms/9800x/978max' — busiest-by-sum first."""
        rows = [(b, self._sum[b], self._n[b], self._max[b]) for b in self._sum if self._n.get(b, 0)]
        rows.sort(key=lambda r: r[1], reverse=True)
        return " · ".join(f"{b} {s:.0f}ms/{n}x/{mx:.0f}max" for b, s, n, mx in rows) or "idle"


#: The process-wide instance. The maker is ONE asyncio process, and the alternative — threading a
#: stats handle through the driver, the executor, the caps and the alert senders — would put a
#: measurement parameter on twenty signatures that have nothing to do with measurement.
STATS = LoopStats()


def timed(bucket: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` inside ``bucket``'s timer and return its result (for one-line call sites)."""
    with STATS.timer(bucket):
        return fn(*args, **kwargs)
