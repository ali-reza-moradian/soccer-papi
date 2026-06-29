"""Executor CLI (Phase 5): dryrun | place | status.

    python -m src.executor.cli dryrun            # measure edge-survival on live detected arbs
    python -m src.executor.cli place             # execute ONE best arb (respects ALL flags)
    python -m src.executor.cli status            # balances, positions, today's counters, STOP

With shipped defaults (enabled=false, dry_run=true, require_human_confirm=true) `place` is a
dry-run with a human-confirm prompt; a REAL order requires enabled=true AND dry_run=false in
config (and then the y/N prompt, unless require_human_confirm=false).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import arb_source
from . import config as exec_config
from .engine import append_dryrun, dryrun_dedupe_key, execute_arb
from .guardrails import Guardrails
from .ledger import Ledger
from .resolve import MarketData, normalize_arb


def _logger() -> logging.Logger:
    log = logging.getLogger("executor")
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        log.addHandler(h)
        # Also persist events (guardrail skips, cooldowns, halts, untradable rejects) to the
        # executor log so the dashboard's event panel can tail them. Additive; failure is non-fatal.
        try:
            exec_config.ensure_dirs()
            fh = logging.FileHandler(exec_config.LOG_PATH, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass
        log.setLevel(logging.INFO)
    return log


def _confirm(narb, size) -> bool:
    """Interactive y/N gate shown immediately before a LIVE placement."""
    print("\n*** LIVE PLACEMENT CONFIRMATION ***")
    print(f"  fixture : {narb.fixture}")
    print(f"  market  : {narb.market}")
    print(f"  kalshi  : BUY {narb.kalshi.side} {size} @ ~{narb.kalshi.detected_price:.4f} "
          f"({narb.kalshi.identifier})")
    print(f"  poly    : BUY {size} @ ~{narb.poly.detected_price:.4f} ({narb.poly.identifier})")
    try:
        ans = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans == "y"


def run_dryrun_cycle(cfg, market_data, arbs, limit, last_seen: dict, *,
                     dryrun_log_path=None, log=None) -> dict:
    """One dry-run pass over ``arbs`` with cross-cycle DEDUPE. For each arb we COMPUTE the row
    (write_log=False) and only append it when it is new OR its price/edge/skip-reason changed vs the
    previous cycle (``last_seen`` maps fingerprint -> last dedupe key, mutated in place). SKIPPED
    arbs (empty book, size 0, …) now produce a visible status='skipped' row too — never dropped
    silently. Returns {total, survived, failed, skipped, written, skip_reason}. No sleeping, no STOP
    check — that's the driver's job (testable)."""
    total = survived = failed = skipped = written = 0
    skip_reason = ""
    for arb in arbs[:limit]:
        res = execute_arb(arb, live=False, cfg=cfg, market_data=market_data,
                          write_log=False, dryrun_log_path=dryrun_log_path, log=log)
        if res.status not in ("dryrun", "skipped"):     # nothing computed -> nothing to record
            continue
        row = res.detail
        if res.status == "skipped":
            skipped += 1
            skip_reason = row.get("skip_reason", "") or skip_reason
        else:
            total += 1
            if res.arb_survived:
                survived += 1
            else:
                failed += 1
        fp = row.get("fingerprint", "")
        key = dryrun_dedupe_key(row)
        if last_seen.get(fp) != key:    # new arb OR price/edge/skip-reason moved -> log it
            append_dryrun(row, dryrun_log_path)
            last_seen[fp] = key
            written += 1
    return {"total": total, "survived": survived, "failed": failed,
            "skipped": skipped, "written": written, "skip_reason": skip_reason}


def cmd_dryrun(args) -> int:
    log = _logger()
    exec_config.ensure_dirs()
    cfg = exec_config.load_exec_config()
    md = MarketData()

    if not args.loop:
        # One-shot: unchanged behavior (writes every computed row), honoring --recent-minutes only
        # when the user actually passed it.
        arbs = arb_source.load_clean_arbs(args.csv, recent_minutes=args.recent_minutes)
        if not arbs:
            log.info("No clean kalshi<->poly arbs found in %s.", args.csv or arb_source.DEFAULT_CSV)
            return 0
        survived = total = 0
        for arb in arbs[: args.limit]:
            res = execute_arb(arb, live=False, cfg=cfg, market_data=md, log=log)
            total += 1
            if res.arb_survived:
                survived += 1
            log.info("  %-28s | %-22s | %s%s", arb.get("match", "")[:28], arb.get("market", "")[:22],
                     res.status, "" if res.status != "dryrun" else f" survived={res.arb_survived}")
        log.info("Dry-run complete: %d/%d arbs survived after fills+fees. Log: %s",
                 survived, total, exec_config.DRYRUN_LOG_PATH)
        return 0

    # Loop mode: continuous, recent-only, deduped. recent_minutes defaults to 30 here.
    recent = args.recent_minutes if args.recent_minutes is not None else 30
    interval = max(1, args.interval)
    last_seen: dict = {}
    log.info("Dry-run LOOP started: every %ds, recent<=%dmin. STOP file or Ctrl-C to exit.",
             interval, recent)
    try:
        while True:
            if exec_config.stop_file_present():
                log.info("STOP present — halting loop.")
                return 0
            arbs = arb_source.load_clean_arbs(args.csv, recent_minutes=recent)
            # Pass the real logger so execute_arb's per-arb "[EXEC] skipped — ..." / "[EXEC dry-run]"
            # lines land in executor.log (and the panel's Guardrail/event log) — never a silent drop.
            stats = run_dryrun_cycle(cfg, md, arbs, args.limit, last_seen, log=log)
            attempted = stats["total"] + stats["skipped"]
            skip_note = (f" ({stats['skip_reason']})"
                         if stats["skipped"] and stats["skip_reason"] else "")
            log.info("cycle %s — %d recent clean arb%s: %d survived, %d failed, %d skipped%s "
                     "(%d new rows)", time.strftime("%H:%M"), attempted,
                     "" if attempted == 1 else "s", stats["survived"], stats["failed"],
                     stats["skipped"], skip_note, stats["written"])
            # HEARTBEAT: prove the loop is alive even when there's nothing to trade. Numbers match the
            # panel's scanner-feed line (clean kalshi<->poly vs total incl 1xbet, last 60 min). Never
            # let heartbeat IO break the loop.
            try:
                from . import dashboard as _dash
                rows = _dash.tail_csv(args.csv or _dash.ARBS_CSV, 2000, newest_first=False)
                _dash.write_heartbeat(_dash.count_clean_recent_arbs(rows), _dash.count_recent_arbs(rows))
            except Exception:  # noqa: BLE001 - heartbeat is best-effort, never fatal
                pass
            # Responsive sleep: wake every second to notice a STOP file promptly.
            for _ in range(interval):
                if exec_config.stop_file_present():
                    log.info("STOP present — halting loop.")
                    return 0
                time.sleep(1)
    except KeyboardInterrupt:
        log.info("Ctrl-C — exiting dry-run loop cleanly.")
        return 0


def cmd_place(args) -> int:
    log = _logger()
    exec_config.ensure_dirs()
    cfg = exec_config.load_exec_config()
    arbs = arb_source.load_clean_arbs(args.csv)
    if not arbs:
        log.info("No clean kalshi<->poly arbs available to place.")
        return 0
    best = arbs[0]
    log.info("Best clean arb: %s | %s | roi=%.2f%%", best.get("match"), best.get("market"),
             best.get("roi_pct", 0.0))

    live = cfg.live_allowed and not args.dry_run
    md = MarketData()
    kalshi = poly = None
    ledger = Ledger()
    guard = Guardrails(cfg, ledger=ledger)
    if live:
        from .kalshi_exec import KalshiExec
        from .poly_exec import PolyExec
        kalshi = KalshiExec(api_base=cfg.kalshi_api_base, log=log)
        poly = PolyExec(log=log)
        log.warning("LIVE placement path active (enabled=true, dry_run=false).")
    else:
        log.info("Dry-run placement (enabled=%s dry_run=%s) — no real order will be sent.",
                 cfg.enabled, cfg.dry_run)

    res = execute_arb(best, live=live, cfg=cfg, market_data=md, kalshi=kalshi, poly=poly,
                      ledger=ledger, guard=guard, confirm=_confirm, log=log)
    log.info("Result: status=%s reason=%s size=%s trade_id=%s",
             res.status, res.reason, res.intended_size, res.trade_id)
    return 0


def cmd_status(args) -> int:
    log = _logger()
    exec_config.ensure_dirs()
    cfg = exec_config.load_exec_config()
    print(f"executor.enabled={cfg.enabled}  dry_run={cfg.dry_run}  "
          f"require_human_confirm={cfg.require_human_confirm}  live_allowed={cfg.live_allowed}")
    print(f"STOP file present: {exec_config.stop_file_present()}  ({exec_config.STOP_FILE})")

    counters = Ledger().today_live_counters()
    print(f"Today (LIVE): trades={counters['trades']} spend=${counters['spend_usd']:.2f} "
          f"loss=${counters['loss_usd']:.2f}")
    print(f"  caps: trades<={cfg.max_trades_per_day} spend<=${cfg.max_daily_spend_usd:.0f} "
          f"loss<=${cfg.max_daily_loss_usd:.0f} per-trade<=${cfg.max_per_trade_usd:.0f}")

    if args.balances:
        try:
            from .kalshi_exec import KalshiExec
            print(f"Kalshi balance : {KalshiExec(api_base=cfg.kalshi_api_base).get_balance()}")
            print(f"Kalshi positions: {KalshiExec(api_base=cfg.kalshi_api_base).get_positions()}")
        except Exception as exc:  # noqa: BLE001
            print(f"Kalshi read failed: {exc}")
        try:
            from .poly_exec import PolyExec
            p = PolyExec()
            ok, reason = p.can_place_polymarket_orders()
            print(f"Poly preflight : {ok} — {reason}")
            print(f"Poly balance   : {p.get_balance()}")
        except Exception as exc:  # noqa: BLE001
            print(f"Poly read failed: {exc}")
    else:
        print("(pass --balances to query live venue balances/positions)")
    return 0


def cmd_selfcheck(args) -> int:
    """Light up the executor + panel end-to-end with synthetic data (no live markets/orders)."""
    from . import selfcheck
    log = _logger()
    cfg = exec_config.load_exec_config()

    if args.clear:
        counts = selfcheck.clear_selfcheck()
        print(f"Cleared selfcheck rows: dryrun_log -{counts['dryrun_removed']}, "
              f"trade_ledger -{counts['ledger_removed']}.")
        return 0

    s = selfcheck.run_selfcheck(cfg=cfg, log=log)
    flags = s["flags"]
    safe = (flags["enabled"] is False and flags["dry_run"] is True and flags["live_enabled"] is False)

    def tick(ok: bool) -> str:
        return "[OK]" if ok else "[!!]"

    print("\n=== executor selfcheck ===")
    print(f"  {tick(s['dryrun_selfcheck_rows'] >= 2)} dryrun_log written "
          f"({s['dryrun_selfcheck_rows']} selfcheck rows; all_survived={s['all_survived']}) "
          f"-> {s['dryrun_path']}")
    print(f"  {tick(s['ledger_selfcheck_rows'] >= 2)} trade_ledger written "
          f"({s['ledger_selfcheck_rows']} selfcheck rows: 1 filled + 1 leg-failure-unwind) "
          f"-> {s['ledger_path']}")
    print(f"  {tick(s['dryrun_exists'] and s['ledger_exists'])} panel files present "
          f"(dryrun_log, trade_ledger; log -> {s['log_path']})")
    print(f"  {tick(safe)} master flags safe: enabled={flags['enabled']} "
          f"dry_run={flags['dry_run']} live_enabled={flags['live_enabled']}")
    print("\nView it:  python -m src.executor.cli panel")
    print("Clean up: python -m src.executor.cli selfcheck --clear\n")
    return 0


# --------------------------------------------------------------------------- #
# diagnose — read-only end-to-end health check (places nothing)                  #
# --------------------------------------------------------------------------- #
def _diag_line(ok: bool, name: str, detail: str) -> str:
    """A single 'PASS/FAIL  name: detail' diagnose line."""
    return f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}"


def _env_key_status() -> list[tuple[str, bool]]:
    """Presence (NOT value) of the 5 expected trading-secret keys, for the .env section."""
    return [
        ("KALSHI_API_KEY_ID",
         bool(os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_ACCESS_KEY"))),
        ("KALSHI_PRIVATE_KEY",
         bool(os.environ.get("KALSHI_PRIVATE_KEY") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
              or os.environ.get("KALSHI_PRIVATE_KEY_FILE"))),
        ("POLYGON_PRIVATE_KEY", bool(os.environ.get("POLYGON_PRIVATE_KEY"))),
        ("POLY_FUNDER_ADDRESS", bool(os.environ.get("POLY_FUNDER_ADDRESS"))),
        ("POLY_SIGNATURE_TYPE", bool(os.environ.get("POLY_SIGNATURE_TYPE"))),
    ]


def _kalshi_adapter(cfg):
    """Construct the read-only Kalshi adapter (monkeypatched in tests)."""
    from .kalshi_exec import KalshiExec
    return KalshiExec(api_base=cfg.kalshi_api_base)


def _poly_adapter(cfg):
    """Construct the read-only Polymarket adapter (monkeypatched in tests)."""
    from .poly_exec import PolyExec
    return PolyExec()


def _poly_wallet_facts() -> tuple[Any, Any]:
    """(funder_address, signature_type) the bot WILL query — resolved from the wallet, falling back
    to env so it still prints something useful without a private key (monkeypatched in tests)."""
    try:
        from .poly_exec import resolve_wallet
        w = resolve_wallet()
        return w.get("funder"), w.get("signature_type")
    except Exception:  # noqa: BLE001 - no key / SDK absent -> show whatever env has
        return os.environ.get("POLY_FUNDER_ADDRESS"), os.environ.get("POLY_SIGNATURE_TYPE")


def _poly_pusd_usd(poly) -> "float | None":
    """Best-effort explicit pUSD probe (fallback only). Under py-clob-client-v2, get_balance()'s
    COLLATERAL read already returns pUSD, so this rarely matters; kept as a defensive fallback for
    the $0 branch. None when the SDK has no distinct pUSD asset type — so it never errors diagnose."""
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    except Exception:  # noqa: BLE001
        return None
    asset = getattr(AssetType, "PUSD", None) or getattr(AssetType, "pUSD", None)
    if asset is None:
        return None
    try:
        from . import dashboard as _dash
        _, sig = _poly_wallet_facts()
        params = BalanceAllowanceParams(asset_type=asset, signature_type=int(sig or 3))
        return _dash.poly_usd(poly.client.get_balance_allowance(params))
    except Exception:  # noqa: BLE001
        return None


def cmd_diagnose(args) -> int:
    """READ-ONLY end-to-end health check: one PASS/FAIL line per check + the raw value. Places
    nothing, sends no orders. Mirrors the panel's reads so the terminal and panel agree."""
    import tempfile
    from . import dashboard as _dash
    from . import selfcheck as _selfcheck

    exec_config.ensure_dirs()
    cfg = exec_config.load_exec_config()
    print("\n=== executor diagnose (read-only; places nothing) ===\n")
    overall_ok = True

    # 1) .env keys present (never print the values).
    env = _env_key_status()
    present = [k for k, ok in env if ok]
    missing = [k for k, ok in env if not ok]
    ok_env = not missing
    detail = f"{len(present)}/5 present: {', '.join(present) or 'none'}"
    if missing:
        detail += f"  | MISSING: {', '.join(missing)}"
    print(_diag_line(ok_env, ".env loaded", detail))
    overall_ok &= ok_env

    # 2) Kalshi auth + balance.
    try:
        bal = _kalshi_adapter(cfg).get_balance()
        usd = _dash.kalshi_usd(bal)
        if usd is not None:
            print(_diag_line(True, "Kalshi auth+balance", f"${usd:,.2f}"))
        else:
            print(_diag_line(False, "Kalshi auth+balance", f"unrecognized balance: {bal}"))
            overall_ok = False
    except Exception as exc:  # noqa: BLE001
        print(_diag_line(False, "Kalshi auth+balance", f"error: {exc}"))
        overall_ok = False

    # 3) Polymarket — print the EXACT funder so it can be compared to the wallet popup char-by-char.
    funder, sig_type = _poly_wallet_facts()
    print(f"[..] Polymarket funder queried: {funder}")
    print(f"     signature_type: {sig_type}")
    print("     If this address != your Polymarket wallet popup, POLY_FUNDER_ADDRESS is wrong.")
    try:
        poly = _poly_adapter(cfg)
        ok_pf, reason = poly.can_place_polymarket_orders()
        print(_diag_line(bool(ok_pf), "Polymarket can-place-orders", reason))
        overall_ok &= bool(ok_pf)
        pbal = poly.get_balance()
        pusd = _dash.poly_usd(pbal)
        if pusd is None:
            print(_diag_line(False, "Polymarket balance", f"unrecognized: {pbal}"))
            overall_ok = False
        elif pusd > 0:
            print(_diag_line(True, "Polymarket balance", f"${pusd:,.2f}"))
        else:
            probe = _poly_pusd_usd(poly)
            if probe:
                extra = f"  | funder pUSD: ${probe:,.2f} (non-zero - funds are in pUSD, not USDC COLLATERAL)"
            else:
                extra = ("  | pUSD not readable via installed SDK (AssetType has no pUSD) - "
                         "verify the funder above matches your wallet popup")
            print(_diag_line(False, "Polymarket balance", f"$0 - check wallet/sig_type{extra}"))
            overall_ok = False
    except Exception as exc:  # noqa: BLE001
        print(_diag_line(False, "Polymarket", f"error: {exc}"))
        overall_ok = False

    # 4) Dry-run pipeline: run the selfcheck synthetic arb into TEMP files (real executor data
    #    untouched) and confirm a dryrun row is produced.
    try:
        tmp = tempfile.mkdtemp(prefix="diagnose_")
        s = _selfcheck.run_selfcheck(cfg=cfg, dryrun_log_path=os.path.join(tmp, "dry.csv"),
                                     ledger_path=os.path.join(tmp, "led.csv"))
        ok_dry = s["dryrun_selfcheck_rows"] >= 1
        print(_diag_line(ok_dry, "Dry-run pipeline",
                         f"{s['dryrun_selfcheck_rows']} synthetic row(s); all_survived={s['all_survived']}"))
        overall_ok &= ok_dry
    except Exception as exc:  # noqa: BLE001
        print(_diag_line(False, "Dry-run pipeline", f"error: {exc}"))
        overall_ok = False

    # 5) Scanner feed: tradable (clean kalshi<->poly) vs total incl 1xbet, last 60 min.
    rows = _dash.tail_csv(args.csv or _dash.ARBS_CSV, 2000, newest_first=False)
    clean = _dash.count_clean_recent_arbs(rows)
    total = _dash.count_recent_arbs(rows)
    print(_diag_line(True, "Scanner feed (last 60 min)",
                     f"{total} total arb(s) incl 1xbet; {clean} tradable by THIS bot (clean kalshi<->poly)"))

    # 6) Verdict + the exact next command.
    print()
    if overall_ok:
        print("VERDICT: PASS - all checks passed.")
    else:
        print("VERDICT: WARN - one or more checks FAILED; fix the FAIL line(s) above.")
    print("Next: python -m src.executor.cli dryrun --loop\n")
    return 0


def cmd_panel(args) -> int:
    """Launch the read-only Streamlit monitoring panel (`streamlit run dashboard.py`)."""
    import subprocess
    from . import dashboard
    path = dashboard.__file__
    cmd = [sys.executable, "-m", "streamlit", "run", path]
    print("Launching monitoring panel:", " ".join(cmd))
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("streamlit not installed. Run: pip install streamlit")
        print(f"Then: streamlit run {path}")
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="executor", description="kalshi<->poly arb executor")
    ap.add_argument("--csv", default=None, help="detected-arbs CSV (default: data/arbitrage_opportunities.csv)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dry = sub.add_parser("dryrun", help="measure edge-survival on live detected arbs")
    p_dry.add_argument("--limit", type=int, default=50, help="max arbs to evaluate per cycle")
    p_dry.add_argument("--loop", action="store_true",
                       help="run continuously (one cycle per --interval) until STOP/Ctrl-C")
    p_dry.add_argument("--interval", type=int, default=60, help="seconds between loop cycles")
    p_dry.add_argument("--recent-minutes", type=int, default=None, dest="recent_minutes",
                       help="only evaluate arbs detected within the last N minutes "
                            "(default: 30 in --loop mode; no filter for one-shot unless given)")
    p_dry.set_defaults(func=cmd_dryrun)

    p_place = sub.add_parser("place", help="execute ONE best arb (respects all flags)")
    p_place.add_argument("--dry-run", action="store_true",
                         help="force dry-run even if config would allow live")
    p_place.set_defaults(func=cmd_place)

    p_status = sub.add_parser("status", help="balances, positions, counters, STOP state")
    p_status.add_argument("--balances", action="store_true", help="query live venue balances")
    p_status.set_defaults(func=cmd_status)

    p_panel = sub.add_parser("panel", help="launch the read-only Streamlit monitoring panel")
    p_panel.set_defaults(func=cmd_panel)

    p_diag = sub.add_parser("diagnose",
                            help="read-only end-to-end health check (PASS/FAIL per check; places nothing)")
    p_diag.set_defaults(func=cmd_diagnose)

    p_self = sub.add_parser("selfcheck",
                            help="inject synthetic dry-run + ledger rows so the panel lights up")
    p_self.add_argument("--clear", action="store_true", help="remove only the selfcheck rows")
    p_self.set_defaults(func=cmd_selfcheck)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
