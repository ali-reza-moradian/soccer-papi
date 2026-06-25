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
import sys

from . import arb_source
from . import config as exec_config
from .engine import execute_arb
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


def cmd_dryrun(args) -> int:
    log = _logger()
    exec_config.ensure_dirs()
    cfg = exec_config.load_exec_config()
    arbs = arb_source.load_clean_arbs(args.csv)
    if not arbs:
        log.info("No clean kalshi<->poly arbs found in %s.", args.csv or arb_source.DEFAULT_CSV)
        return 0
    md = MarketData()
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
    p_dry.add_argument("--limit", type=int, default=50, help="max arbs to evaluate this run")
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

    p_self = sub.add_parser("selfcheck",
                            help="inject synthetic dry-run + ledger rows so the panel lights up")
    p_self.add_argument("--clear", action="store_true", help="remove only the selfcheck rows")
    p_self.set_defaults(func=cmd_selfcheck)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
