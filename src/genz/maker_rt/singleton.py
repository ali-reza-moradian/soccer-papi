"""SINGLETON GUARD — exactly one maker_rt process, enforced by the operating system.

Two concurrent makers is not hypothetical. The supervisor identifies this component by the WRAPPER's
command line only (``scripts/ops.py``), so a dead wrapper whose python child survived reads as
"missing" and a SECOND wrapper + maker is started against the same account. What that costs:

  * every daily cap doubles, because each process keeps its own in-memory ``LiveCaps`` — the $800 stake
    rail becomes $1,600 and the $50 loss rail becomes $100, with neither process able to see it;
  * the newcomer's startup stray-order sweep cancels the INCUMBENT's resting orders (they are
    indistinguishable from a dead run's), destroying live queue position;
  * each reads the other's perfectly legitimate hedge as an unexplained venue holding, so both
    false-orphan and halt.

The lock is an OS-held exclusive byte-range lock, NOT a pid file. That distinction is the whole design:
the kernel releases a byte-range lock when the holding process dies, however it dies, so there is no
stale lock to clean up after a crash and no "is pid 4212 still this bot, or something that reused the
number?" guessing. A pid file would need exactly that guess, and getting it wrong in either direction
is worse than having no guard — refuse forever after a crash, or admit a second maker.
"""
from __future__ import annotations

import os
from typing import Any, Optional

#: The locked fd, kept on a module global FOR THE LIFETIME OF THE PROCESS. This is load-bearing: if the
#: fd were a local it would be garbage-collected, and closing the descriptor releases the lock.
_HELD_FD: Optional[int] = None
_HELD_PATH: Optional[str] = None

REFUSED_ALERT_EVERY_S = 900.0     # a refused start alerts at most this often (the wrapper retries /5s)


def _try_lock(fd: int) -> bool:
    """Take a NON-BLOCKING exclusive lock on the first byte of ``fd``. False when someone else holds it."""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:               # Windows raises OSError(EDEADLOCK/EACCES); POSIX BlockingIOError
        return False


def acquire(path: str) -> bool:
    """True when THIS process now holds the maker lock; False when another live process already does.

    Idempotent: a second call from the same process returns True without re-locking. The pid is written
    into the file purely as a human breadcrumb — nothing reads it back to make a decision, precisely so
    the guard never depends on interpreting a pid."""
    global _HELD_FD, _HELD_PATH
    if _HELD_FD is not None:
        return True
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    if not _try_lock(fd):
        os.close(fd)
        return False
    try:
        # Breadcrumb only — nothing reads this back to make a decision. FIXED WIDTH so a shorter pid
        # overwrites a longer one cleanly (the file is never truncated: on Windows that can collide with
        # the byte-range lock we are holding on byte 0).
        os.write(fd, f"{os.getpid():<20}\n".encode())
    except OSError:
        pass
    _HELD_FD, _HELD_PATH = fd, path
    return True


def release() -> None:
    """Drop the lock (tests + an orderly shutdown). The OS does this for us on exit either way."""
    global _HELD_FD, _HELD_PATH
    fd, _HELD_FD, _HELD_PATH = _HELD_FD, None, None
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def holder_pid(path: str) -> Optional[int]:
    """The pid recorded in the lock file, for the refusal message. None when unreadable/absent."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return int((fh.read() or "0").strip() or 0) or None
    except (OSError, ValueError):
        return None


def should_alert_refusal(path: str, now_ts: float) -> bool:
    """Throttle the refusal alert to one per ``REFUSED_ALERT_EVERY_S``.

    The wrapper relaunches every 5 seconds, and a refused process has no memory, so without a stamp on
    disk a stuck double-supervision would Telegram ~180 times an hour. The stamp lives beside the lock
    (a separate file — the lock itself is held by the OTHER process and must not be written here)."""
    stamp = f"{path}.refused"
    prev = 0.0
    try:
        with open(stamp, "r", encoding="utf-8-sig") as fh:
            prev = float((fh.read() or "0").strip() or 0.0)
    except (OSError, ValueError):
        prev = 0.0
    if now_ts - prev < REFUSED_ALERT_EVERY_S:
        return False
    try:
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(f"{now_ts:.0f}\n")
    except OSError:
        pass
    return True


def refusal_message(path: str) -> str:
    """The log line for a refused start (technical: pid + path)."""
    pid = holder_pid(path)
    return ("[MAKER_RT][CRITICAL] ANOTHER maker_rt PROCESS IS ALREADY RUNNING (lock %s held%s) — "
            "REFUSING to start a second one. Two makers double every daily cap, cancel each other's "
            "resting orders on startup, and false-orphan each other. Kill the stale process (or its "
            "wrapper) and this instance will start on the next supervisor sweep."
            % (path, f" by pid {pid}" if pid else ""))


def human_refusal() -> str:
    """The plain-language operator alert (no paths, no pids — those stay in the log)."""
    return ("I did not start: another copy of me is already running and trading. Two copies would each "
            "think they had the full daily budget, so I stopped instead. Nothing was placed. Once the "
            "old copy is gone I start again on my own.")


def guard(path: str, log: Any = None, telegram: Any = None, now_ts: float = 0.0) -> bool:
    """Acquire-or-refuse, with the screaming attached. True = safe to continue starting up."""
    if acquire(path):
        if log:
            log.info("[MAKER_RT] singleton lock acquired (%s) — this is the only maker on this host.", path)
        return True
    if log:
        crit = getattr(log, "critical", None) or log.error
        crit(refusal_message(path))
    if telegram is not None and should_alert_refusal(path, now_ts):
        try:
            telegram(human_refusal())
        except Exception:  # noqa: BLE001 — an alert failure must not change the refusal
            pass
    return False
