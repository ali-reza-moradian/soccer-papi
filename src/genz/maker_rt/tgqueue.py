"""Telegram off the event loop: one FIFO queue, one sender thread.

Every alert this bot sends is a synchronous HTTPS POST with a 20s timeout and up to 3 attempts — so a
Telegram outage can block whatever called it for a minute. The callers are the worst possible ones: a
fill, a hedge, a HALT. The loop that is supposed to be repricing 160 markets spent that minute waiting
for a chat message about the fill it had just hedged, and the audit measured the worst case at 64s.

So: ``enqueue`` returns immediately, a single daemon thread does the POSTing, and

  * ORDER IS PRESERVED — one thread draining one FIFO, never a pool. An operator reading the chat sees
    fill → hedge → halt in the order those things happened, which is most of what makes the chat useful.
  * FAILURES NEVER REACH TRADING — the send is already wrapped by every caller, and this adds a second
    layer: the thread logs and moves on, so a Telegram problem cannot become a maker problem.
  * NOTHING IS SILENTLY DROPPED at shutdown — ``close`` flushes with a deadline, because a deploy is
    exactly when the last alert (the shutdown cancel-all, a halt) matters most.

The queue is BOUNDED. If Telegram is down and the backlog reaches the cap we drop the OLDEST alert and
count it, because the newest state of a live trading process is the one an operator needs; a heartbeat
from four minutes ago is not worth the newest halt.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

MAX_QUEUE = 500
JOIN_TIMEOUT_S = 8.0


class TelegramQueue:
    """Wrap a blocking ``send(text)`` in a FIFO + one daemon sender thread."""

    def __init__(self, send: Callable[[str], Any], *, log: Any = None, max_queue: int = MAX_QUEUE) -> None:
        self._send = send
        self.log = log
        self._q: "queue.Queue" = queue.Queue()
        self._max = int(max_queue)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0
        self.dropped = 0

    # -- the public callable -------------------------------------------------
    def __call__(self, text: str) -> bool:
        """Drop-in replacement for the raw sender: enqueue and return. True iff it was queued."""
        return self.enqueue(text)

    def enqueue(self, text: str) -> bool:
        if self._stop.is_set():
            # Shutting down: send INLINE rather than queue onto a thread that is going away. A dropped
            # shutdown alert is the one class of loss this design must not introduce.
            return self._deliver(text)
        while self._q.qsize() >= self._max:
            try:
                self._q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                break
        self._ensure_thread()
        self._q.put(text)
        return True

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="maker-rt-telegram", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                text = self._q.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if text is None:
                return
            self._deliver(text)

    def _deliver(self, text: str) -> bool:
        try:
            self._send(text)
            self.sent += 1
            return True
        except Exception as exc:  # noqa: BLE001 — a chat message can never be a trading failure
            self.failed += 1
            if self.log:
                self.log.warning("[MAKER_RT] telegram send failed in the sender thread: %s", exc)
            return False

    # -- shutdown ------------------------------------------------------------
    def close(self, timeout: float = JOIN_TIMEOUT_S) -> int:
        """Flush the backlog (bounded by ``timeout``) and stop. Returns how many were still queued."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=max(0.0, timeout))
        left = self._q.qsize()
        if left and self.log:
            self.log.warning("[MAKER_RT] telegram queue closed with %d message(s) unsent.", left)
        return left

    def depth(self) -> int:
        return self._q.qsize()
