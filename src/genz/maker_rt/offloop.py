"""ONE background thread for the synchronous venue I/O the event loop must not block on.

Deliberately not a thread pool. One thread means at most ONE off-loop venue request in flight, which
keeps three things true at once:

  * the shared ``requests`` sessions inside PolyExec/KalshiExec see a single concurrent caller besides
    the loop, so no venue client has to become thread-safe for this to be correct;
  * the venue read budget stays predictable on a shared IP (a pool would multiply it by its width);
  * results arrive in submission order, so a later cancel can never be resolved before an earlier one.

Jobs carry a KEY and are DE-DUPLICATED on it: submitting a key that is already queued or running is a
no-op. That is not an optimisation — it is the property the cancel-retry storm needs, because the loop
asks about the same order several times a second and must not be able to stack DELETEs on it.

Nothing here decides anything. The worker calls the function, captures ``(result, exception)``, and
puts the pair in a mailbox; every decision — freeing a slot, routing a fill, mutating caps — is made by
the loop when it drains. That split is the whole safety argument: the loop stays the only thread that
touches trading state.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

#: Bound on undrained results. A caller that stops draining has a bug, and an unbounded mailbox would
#: turn that bug into a memory leak; dropping the OLDEST is right because a stale venue read is the
#: least useful one (the loop re-submits and gets a fresh answer).
MAX_PENDING_RESULTS = 512


class Worker:
    """Submit ``(key, callable)``; drain ``(key, result, exception)`` on the loop thread."""

    def __init__(self, *, name: str = "maker-rt-offloop", log: Any = None) -> None:
        self.log = log
        self._q: "queue.Queue" = queue.Queue()
        self._results: list = []
        self._lock = threading.Lock()
        self._inflight: set = set()          # keys queued OR running (guarded by _lock)
        self._stop = threading.Event()
        self._name = name
        self._thread: Optional[threading.Thread] = None
        self.submitted = 0                   # lifetime counters (panel/diagnostics)
        self.completed = 0
        self.dropped = 0

    # -- lifecycle -----------------------------------------------------------
    def _ensure_thread(self) -> None:
        """Start the thread on FIRST submit, not at construction: a shadow run must not grow a thread
        it never uses, and neither must a test that only builds the executor."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:                              # shutdown sentinel
                break
            key, fn = item
            res, exc = None, None
            try:
                res = fn()
            except BaseException as e:  # noqa: BLE001 — a raising job is a RESULT, never a dead thread
                exc = e
            with self._lock:
                self._inflight.discard(key)
                self._results.append((key, res, exc))
                if len(self._results) > MAX_PENDING_RESULTS:
                    drop = len(self._results) - MAX_PENDING_RESULTS
                    del self._results[:drop]
                    self.dropped += drop
                self.completed += 1

    def close(self, timeout: float = 2.0) -> None:
        """Stop the thread (best-effort). Safe to call more than once and on a never-started worker."""
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=max(0.0, timeout))

    # -- submit / drain ------------------------------------------------------
    def in_flight(self, key: Any) -> bool:
        with self._lock:
            return key in self._inflight

    def pending(self) -> int:
        with self._lock:
            return len(self._inflight)

    def submit(self, key: Any, fn: Callable[[], Any]) -> bool:
        """Queue ``fn`` under ``key``. False when that key is already in flight (nothing was queued)."""
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            self.submitted += 1
        self._ensure_thread()
        self._q.put((key, fn))
        return True

    def drain(self) -> list:
        """Take every completed ``(key, result, exception)``. Call ONLY from the loop thread."""
        with self._lock:
            out, self._results = self._results, []
        return out
