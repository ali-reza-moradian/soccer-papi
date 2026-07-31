"""The 2026-07-30 20:42Z -> 2026-07-31 13:58Z off-loop stall, and the hardening it forced.

THE FAILURE, from the logs: the watchdog fired 60 times over 17 hours, every firing reporting the same
two things — "not landed for 40s" (59 of 60; one said 43s) and "3 job(s) in flight" (55 of 60). Zero
batch failures, zero worker exceptions, zero restarts in the whole window.

Those numbers rule out the obvious stories and point at exactly one. A DEAD thread or a HUNG venue call
would make the stale figure GROW without bound; it never moved off 40s, so batches were landing. What
was landing them late was the queue: the measurement-gate report shared the single venue worker and
took **25.3s** per run scanning **543 MB** of event CSVs, with the 10s fill poll and the 5-minute
reconciliation stacked behind it — 4 firings an hour against a 5-minute alert throttle, which is the
15-minute gates cadence exactly.

So the safety net was not offline for 17 hours; it was blacked out for ~30s every 15 minutes. Both
statements matter: the second is much less alarming than the first, and still unacceptable for the
detector of record.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from src.genz.maker_rt import gates
from src.genz.maker_rt.offloop import Worker

from .test_maker_rt_pregame import _Log, _Store, _cand, _dec, _exec

NOW = datetime(2026, 7, 31, 14, 0, 0, tzinfo=timezone.utc)
_STORE = _Store(kalshi_ask=0.60, poly_best_ask=0.55)
_PDEC = lambda: _dec(0.38, hedge_ask=0.60)          # noqa: E731


def _ex(tmp_path):
    ex, _ = _exec(tmp_path)
    ex.roll_day(NOW)
    ex.log = _Log()
    ex.telegram = lambda t: None
    ex.caps.max_open_quotes = 12
    ex.caps.max_daily_stake_usd = 10_000.0
    ex.caps.max_fills_per_day = 20
    return ex


def _settle(w, tries=300):
    for _ in range(tries):
        if w.pending() == 0:
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- #
# THE ROOT CAUSE — report work sharing the venue thread                         #
# --------------------------------------------------------------------------- #
def test_the_gates_report_does_not_share_the_venue_worker(tmp_path):
    """25.3s of CSV scanning on the thread that owes a 10s fill poll is what produced the blackouts.
    The report does NO venue I/O, so it needs none of the single-thread guarantees that worker exists
    to provide — different work, different thread."""
    ex = _ex(tmp_path)
    assert ex._report_worker is not ex._worker
    ex.submit_gates()
    assert ex._report_worker.pending() == 1
    assert ex._worker.pending() == 0, "the venue worker is free to serve the fill poll"
    ex._report_worker.close()


def test_the_gates_report_only_reads_files_that_can_qualify(tmp_path):
    """The corpus reached 543 MB across 16 files and every run re-scanned all of it. A daily file older
    than the cut cannot hold a qualifying row, so opening it is pure cost."""
    for day in ("20260722", "20260729", "20260730", "20260731"):
        (tmp_path / f"maker_rt_{day}.csv").write_text("ts,mode,event\n", encoding="utf-8")
    import src.genz.config as gz
    old = gz.GENZ_DIR
    try:
        gz.GENZ_DIR = str(tmp_path)
        got = sorted(f.rsplit("_", 1)[-1] for f in gates._relevant_files())
    finally:
        gz.GENZ_DIR = old
    assert got == ["20260730.csv", "20260731.csv"], "pre-cut days are not opened at all"


def test_an_unparseable_filename_is_still_read(tmp_path):
    """A rename must cost time, never rows."""
    (tmp_path / "maker_rt_2_backup.csv").write_text("ts,mode,event\n", encoding="utf-8")
    import src.genz.config as gz
    old = gz.GENZ_DIR
    try:
        gz.GENZ_DIR = str(tmp_path)
        assert any("backup" in f for f in gates._relevant_files())
    finally:
        gz.GENZ_DIR = old


# --------------------------------------------------------------------------- #
# THE WORKER — a stuck or dead thread must not end all off-loop work            #
# --------------------------------------------------------------------------- #
def test_a_hung_job_does_not_block_the_queue_forever():
    """THE FAILURE MODE THIS EXISTS FOR: a key stays in ``_inflight`` while its job runs, so a hung
    venue call means every later submit is refused as a duplicate and the fill-poll backstop simply
    stops — silently, with nothing appearing to fail. Python cannot kill the thread; it can stop it
    owning the queue."""
    w = Worker(job_timeout_s=0.2, log=_Log())
    gate = threading.Event()
    assert w.submit("stuck", lambda: gate.wait(30)) is True
    assert w.submit("stuck", lambda: None) is False, "in flight -> deduped, as designed"
    time.sleep(0.35)
    assert w.submit("stuck", lambda: "fresh") is True, "past the deadline the key is submittable again"
    assert w.abandoned >= 1
    got = []
    for _ in range(300):
        got += w.drain()
        if got:
            break
        time.sleep(0.01)
    assert any(r[1] == "fresh" for r in got), "the replacement thread served the new job"
    gate.set()
    w.close()


def test_the_abandoned_thread_cannot_publish_a_late_result():
    """Its result would race the thread that replaced it, and the loop would act on a stale read."""
    w = Worker(job_timeout_s=0.2, log=_Log())
    release = threading.Event()
    w.submit("slow", lambda: release.wait(5) or "LATE")
    time.sleep(0.35)
    w.submit("slow", lambda: "CURRENT")
    _settle(w)
    release.set()
    time.sleep(0.3)
    vals = [r[1] for r in w.drain()]
    assert "LATE" not in vals
    w.close()


def test_a_dead_thread_is_restarted_and_screams():
    """Never silently inert: the thread is checked on drain as well as submit."""
    log = _Log()
    w = Worker(log=log)
    w.submit("x", lambda: 1)
    _settle(w)
    w._thread.join(timeout=0)
    w._stop.set()                                    # simulate the thread having stopped
    w._thread.join(timeout=2)
    assert not w._thread.is_alive()
    w.drain()                                        # the liveness check lives here too
    assert w.restarts >= 1
    assert any("NOT RUNNING" in m for m in log.warns)
    w.close()


def test_a_raising_job_screams_and_the_worker_survives():
    log = _Log()
    w = Worker(log=log)
    w.submit("boom", lambda: (_ for _ in ()).throw(ValueError("venue 500")))
    got = []
    for _ in range(300):
        got += w.drain()
        if got:
            break
        time.sleep(0.01)
    assert got and isinstance(got[0][2], ValueError)
    assert any("RAISED" in m for m in log.warns)
    assert w.submit("next", lambda: "alive") is True
    w.close()


# --------------------------------------------------------------------------- #
# HELD-FOR-BOOK RESULTS EXPIRE                                                  #
# --------------------------------------------------------------------------- #
def test_a_held_result_expires_instead_of_blocking(tmp_path):
    """Phase 3 holds a result that needs a book. It must not hold it FOREVER: after enough drains with
    no store the venue read behind it is stale, and re-reading is both cheaper and more correct."""
    ex = _ex(tmp_path)
    ex._worker._results.append((("fill_poll",), {"index": {}, "per_order": {}, "venue_ok": {},
                                                 "covered": set()}, None))
    ex._drain_cancel_results(None, NOW, 0.0)
    assert len(ex._deferred_offloop) == 1
    for _ in range(ex.EXPIRE_HELD_DRAINS + 2):
        ex._drain_cancel_results(None, NOW, 0.0)
    assert ex._deferred_offloop == [], "it aged out rather than blocking"
    assert any("dropping a held" in w for w in ex.log.warns)


def test_a_held_result_is_still_applied_when_a_book_arrives(tmp_path):
    """Expiry must not break the thing the holding was for."""
    ex = _ex(tmp_path)
    ex._worker._results.append((("fill_poll",), {"index": {}, "per_order": {}, "venue_ok": {},
                                                 "covered": set()}, None))
    applied = []
    ex._apply_fill_poll_batch = lambda *a, **k: applied.append(a)
    ex._drain_cancel_results(None, NOW, 0.0)
    ex._drain_cancel_results(_STORE, NOW, 100.0)
    assert len(applied) == 1 and ex._deferred_offloop == []


# --------------------------------------------------------------------------- #
# THE WATCHDOG ESCALATES                                                        #
# --------------------------------------------------------------------------- #
def test_a_short_stall_alerts_but_keeps_trading(tmp_path):
    """Under the halt threshold the sockets are still routing fills and a brief gap is survivable."""
    ex = _ex(tmp_path)
    ex._fill_poll_applied_ts = 1000.0
    ex._watch_offloop_stall(1000.0 + 4 * ex.fill_poll_s + 1)
    assert any("STALLED" in w for w in ex.log.warns)
    assert ex.caps.halted is False


def test_a_long_stall_HALTS_rather_than_trading_blind(tmp_path):
    """Past the threshold we are trading with no independent confirmation of our own fills, and "the
    websocket looks fine" is exactly the assumption the backstop exists to stop us making — the
    2026-07-23 invisible fills were a healthy-looking socket reporting nothing for 6,126 polls."""
    ex = _ex(tmp_path)
    ex._fill_poll_applied_ts = 1000.0
    ex._watch_offloop_stall(1000.0 + ex.OFFLOOP_HALT_AFTER_S + 1)
    assert ex.caps.halted is True and ex.caps.halt_reason == "offloop_stalled"
    assert any("HALTING live quoting" in w for w in ex.log.warns)


def test_a_landed_batch_clears_the_stall_halt(tmp_path):
    """It has to un-halt itself: a halt that needs a human to clear turns a transient into an outage."""
    ex = _ex(tmp_path)
    ex._fill_poll_applied_ts = 1000.0
    ex._watch_offloop_stall(1000.0 + ex.OFFLOOP_HALT_AFTER_S + 1)
    assert ex.caps.halted is True
    ex._apply_fill_poll_batch({"index": {}, "per_order": {}, "venue_ok": {}, "covered": set()},
                              None, _STORE, NOW, 5000.0)
    assert ex.caps.halted is False and ex.caps.halt_reason is None


def test_the_stall_halt_never_washes_away_another_halt(tmp_path):
    """A landing batch must clear ONLY its own reason — never an orphan, a quarantine or a daily cap."""
    ex = _ex(tmp_path)
    ex.caps.halted, ex.caps.halt_reason = True, "orphan_position"
    ex._clear_offloop_halt("a batch landed")
    assert ex.caps.halted is True and ex.caps.halt_reason == "orphan_position"


# --------------------------------------------------------------------------- #
# THE NOISE ITEMS                                                               #
# --------------------------------------------------------------------------- #
def test_the_digest_names_the_finished_matches():
    from src.genz.maker_rt import alerts
    line = alerts.digest_line(15, placed=3, cancelled=1, fills=0, open_now=2, max_open=12,
                              closed_markets=["Ilves vs Stjarnan Total O3.5",
                                              "Pafos vs Hajduk Total U2.5"])
    assert "2 market(s) finished" in line
    assert "Ilves vs Stjarnan" in line, "the per-event alert never said WHICH game"


def test_the_digest_summarises_a_large_batch():
    from src.genz.maker_rt import alerts
    line = alerts.digest_line(15, placed=0, cancelled=0, fills=0, open_now=0, max_open=12,
                              closed_markets=[f"Match {i}" for i in range(9)])
    assert "9 market(s) finished" in line and "+5 more" in line
