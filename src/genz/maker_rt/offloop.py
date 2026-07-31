"""ONE background thread for the synchronous venue I/O the event loop must not block on.

Deliberately not a thread pool. One thread means at most ONE off-loop venue request in flight, which
keeps three things true at once:

  * the shared ``requests`` sessions inside PolyExec/KalshiExec see a single concurrent caller besides
    the loop, so no venue client has to become thread-safe for this to be correct;
  * the venue read budget stays predictable on a shared IP (a pool would multiply it by its width);
  * results arrive in submission order, so a later cancel can never be resolved before an earlier one.

Because there is only one thread, ANY long job blocks every other one — so work that does no venue I/O
belongs on its OWN Worker, not this one. That is not hypothetical: the measurement-gate report shared
this thread and spent 25.3s per run re-scanning 543 MB of event CSVs, which queued the 10s fill poll
behind it and blacked out the REST fill backstop ~4 times an hour for 17 hours.

Jobs carry a KEY and are DE-DUPLICATED on it: submitting a key that is already queued or running is a
no-op. That is not an optimisation — it is the property the cancel-retry storm needs, because the loop
asks about the same order several times a second and must not be able to stack DELETEs on it.

Nothing here decides anything. The worker calls the function, captures ``(result, exception)``, and
puts the pair in a mailbox; every decision — freeing a slot, routing a fill, mutating caps — is made by
the loop when it drains. That split is the whole safety argument: the loop stays the only thread that
touches trading state.

**A SLOW OR STUCK JOB MUST NOT TAKE THE QUEUE WITH IT.** Python cannot kill a thread, so a genuinely
hung socket read is unkillable — but it does not get to hold everything else hostage. Every job carries
a DEADLINE; when one passes it the worker ABANDONS it (logs CRITICAL, releases the key so new work can
be submitted, and starts a fresh thread), leaving the stuck one to finish or die as a daemon. Without
that, one blocked call silently ends all off-loop work: the key stays in ``_inflight`` forever, every
later ``submit`` is refused as a duplicate, and the fill-poll backstop simply stops — which is exactly
how a safety net goes offline without anything appearing to fail.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional

#: Bound on undrained results. A caller that stops draining has a bug, and an unbounded mailbox would
#: turn that bug into a memory leak; dropping the OLDEST is right because a stale venue read is the
#: least useful one (the loop re-submits and gets a fresh answer).
MAX_PENDING_RESULTS = 512

#: Default per-job deadline. Generous relative to a healthy job (a full fill-poll batch measured ~3.5s
#: against both venues) and short enough that a hung call cannot black out the backstop for long.
DEFAULT_JOB_TIMEOUT_S = 45.0


class Worker:
    """Submit ``(key, callable)``; drain ``(key, result, exception)`` on the loop thread."""

    def __init__(self, *, name: str = "maker-rt-offloop", log: Any = None,
                 job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S) -> None:
        self.log = log
        self.job_timeout_s = float(job_timeout_s)
        self._q: "queue.Queue" = queue.Queue()
        self._results: list = []
        self._lock = threading.Lock()
        self._inflight: dict = {}            # key -> deadline (monotonic); queued OR running
        self._stop = threading.Event()
        self._name = name
        self._thread: Optional[threading.Thread] = None
        self._generation = 0                 # bumped when a thread is abandoned; old threads self-retire
        self.submitted = 0                   # lifetime counters (panel/diagnostics)
        self.completed = 0
        self.dropped = 0
        self.abandoned = 0
        self.restarts = 0

    # -- lifecycle -----------------------------------------------------------
    def _ensure_thread(self) -> None:
        """Start the thread on FIRST submit, and RESTART it if it ever stopped.

        Called from ``submit`` AND ``drain`` so a thread that died — for any reason, including one this
        module did not anticipate — is noticed and replaced, rather than leaving the worker silently
        inert for as long as nobody looks at it."""
        died = False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            died = self._thread is not None
            self._generation += 1
            gen = self._generation
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(gen,), name=self._name, daemon=True)
            t = self._thread
            if died:
                self.restarts += 1
        if died and self.log:
            crit = getattr(self.log, "critical", None) or self.log.error
            crit("[MAKER_RT][CRITICAL] the off-loop worker thread was NOT RUNNING — restarting it "
                 "(restart #%d). Off-loop work had stopped.", self.restarts)
        t.start()

    def _run(self, generation: int) -> None:
        while not self._stop.is_set():
            if generation != self._generation:
                return                                    # abandoned; a newer thread owns the queue
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:                              # shutdown sentinel
                return
            key, fn = item
            res, exc = None, None
            try:
                res = fn()
            except BaseException as e:  # noqa: BLE001 — a raising job is a RESULT, never a dead thread
                exc = e
                if self.log:
                    crit = getattr(self.log, "critical", None) or self.log.error
                    crit("[MAKER_RT][CRITICAL] off-loop job %s RAISED %s: %s — the worker keeps "
                         "running and the loop is told.", key, type(e).__name__, e)
            if generation != self._generation:
                # Abandoned mid-job (its deadline passed). The key is already released and a fresh
                # thread serves the queue; publishing this late result would race it.
                return
            with self._lock:
                self._inflight.pop(key, None)
                self._results.append((key, res, exc))
                if len(self._results) > MAX_PENDING_RESULTS:
                    drop = len(self._results) - MAX_PENDING_RESULTS
                    del self._results[:drop]
                    self.dropped += drop
                self.completed += 1

    def _reap_overdue(self) -> list:
        """Release any in-flight key past its deadline and abandon the thread serving it.

        The stuck thread is left running as a daemon — Python cannot kill it — but it no longer owns the
        queue, and the keys it held become submittable again. Returns the keys abandoned."""
        now = time.monotonic()
        with self._lock:
            overdue = [k for k, dl in self._inflight.items() if dl and now > dl]
            if not overdue:
                return []
            for k in overdue:
                self._inflight.pop(k, None)
            self.abandoned += len(overdue)
            self._generation += 1                         # the current thread is now orphaned
            self._thread = None
            self._q = queue.Queue()                       # its backlog goes with it; the loop re-submits
        if self.log:
            crit = getattr(self.log, "critical", None) or self.log.error
            crit("[MAKER_RT][CRITICAL] off-loop job(s) %s exceeded the %.0fs deadline — ABANDONING the "
                 "worker thread and starting a fresh one. Off-loop work resumes; the stuck call is left "
                 "to finish or die on its own.", overdue, self.job_timeout_s)
        self._ensure_thread()
        return overdue

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

    def submit(self, key: Any, fn: Callable[[], Any], *,
               timeout_s: Optional[float] = None) -> bool:
        """Queue ``fn`` under ``key``. False when that key is already in flight (nothing was queued)."""
        self._reap_overdue()
        deadline = time.monotonic() + float(timeout_s if timeout_s is not None else self.job_timeout_s)
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight[key] = deadline
            self.submitted += 1
        self._ensure_thread()
        self._q.put((key, fn))
        return True

    def drain(self) -> list:
        """Take every completed ``(key, result, exception)``. Call ONLY from the loop thread."""
        self._reap_overdue()
        # LIVENESS, on the loop's own heartbeat. ``_reap_overdue`` only restarts the thread when a key is
        # overdue, so a thread that died with an EMPTY queue would sit dead until something happened to
        # submit again — which, for a worker whose whole job is periodic background work, can be a long
        # time and looks exactly like "everything is fine". Only ever revives a worker that HAS run: a
        # process that merely drains must not grow a thread it never asked for.
        if self._thread is not None:
            self._ensure_thread()
        with self._lock:
            out, self._results = self._results, []
        return out

    def stats(self) -> dict:
        """Counters for the panel / diagnostics."""
        with self._lock:
            inflight = len(self._inflight)
        return {"submitted": self.submitted, "completed": self.completed, "dropped": self.dropped,
                "abandoned": self.abandoned, "restarts": self.restarts, "in_flight": inflight,
                "alive": bool(self._thread is not None and self._thread.is_alive())}
