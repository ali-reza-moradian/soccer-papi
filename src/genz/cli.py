"""GenZ command line — the two jobs.

    python -m src.genz.cli build-tree                 # JOB 1 (hourly): rebuild the static match tree
    python -m src.genz.cli run --loop --interval 20   # JOB 2 (always-on): fast price + arb loop
    python -m src.genz.cli run --once                 # one cycle (useful for cron / a smoke test)
    python -m src.genz.cli status                     # print config + last heartbeat

Execution always flows through the EXECUTOR, so the executor flags govern it: with the defaults
(enabled:false / dry_run:true) the loop measures only. Live trading clients are constructed ONLY when
``live_allowed`` (enabled AND not dry_run) — never otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from .. import kalshi as ks
from .. import polymarket as pm
from ..executor import config as exec_config
from ..logsetup import get_logger, setup_logging
from . import config as gz_config
from . import engine as gz_engine
from . import tree_builder


def _logger():
    return get_logger("genz")


def _kalshi_client(gz_cfg: gz_config.GenzConfig):
    # A per-sport process gets its own Kalshi throttle (genz.mlb.kalshi_min_interval) so MLB's separate
    # process doesn't contend with soccer's on the tight public read limit. None -> the client default.
    kw = {} if gz_cfg.kalshi_min_interval is None else {"min_interval": float(gz_cfg.kalshi_min_interval)}
    return ks.KalshiClient(base_url=gz_cfg.kalshi_base_url, timeout=gz_cfg.http_timeout_seconds, **kw)


def _spec_for(sport: str):
    """The tree-builder sport adapter for ``sport`` (soccer default). MLB/tennis are imported lazily so
    the soccer path never pulls their code in."""
    if sport == "mlb":
        from . import sports_mlb
        return sports_mlb.MLB_SPEC
    if sport == "tennis":
        from . import sports_tennis
        return sports_tennis.TENNIS_SPEC
    return tree_builder.SOCCER_SPEC


def _poly_client(gz_cfg: gz_config.GenzConfig):
    return pm.PolymarketClient(gamma_base=gz_cfg.gamma_base, clob_base=gz_cfg.clob_base,
                               timeout=gz_cfg.http_timeout_seconds)


# --------------------------------------------------------------------------- #
# JOB 1 — build-tree                                                            #
# --------------------------------------------------------------------------- #
def cmd_build_tree(args) -> int:
    log = _logger()
    gz_config.ensure_dirs()
    sport = getattr(args, "sport", "soccer")
    gz_cfg = gz_config.load_genz_config(overrides={"lookahead_hours": args.lookahead}, sport=sport)
    paths = gz_config.paths_for_sport(sport)
    spec = _spec_for(sport)
    now = datetime.now(timezone.utc)
    log.info("[GENZ] build-tree (%s): discovering games within %.0fh ...", sport, gz_cfg.lookahead_hours)
    tree = tree_builder.build_tree(_kalshi_client(gz_cfg), _poly_client(gz_cfg), gz_cfg,
                                   now=now, log=log, spec=spec)
    tp, mp = tree_builder.write_tree(tree, now=now, tree_path=paths.tree_path, meta_path=paths.meta_path)
    games = tree.get("games", {})
    nodes = sum(len(g.get("nodes") or []) for g in games.values())
    log.info("[GENZ] wrote %d game(s), %d node(s) -> %s (meta: %s)", len(games), nodes, tp, mp)
    return 0


# --------------------------------------------------------------------------- #
# JOB 2 — run                                                                   #
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    log = _logger()
    gz_config.ensure_dirs()
    sport = getattr(args, "sport", "soccer")
    gz_cfg = gz_config.load_genz_config(
        overrides={k: v for k, v in (("interval_seconds", args.interval),) if v is not None}, sport=sport)
    paths = gz_config.paths_for_sport(sport)
    exec_cfg = exec_config.load_exec_config()

    # --debug-gate: print, per game, kickoff_utc / now_utc / started, plus each totals node's exact
    # legs (Kalshi ticker+side+price, Poly token+side+price, line/period) and the Over+Under sum.
    if getattr(args, "debug_gate", False):
        from datetime import datetime, timezone
        from ..executor.resolve import MarketData
        from .. import kalshi as ks2
        from .. import polymarket as pm2
        tree = tree_builder.load_tree(paths.tree_path)
        md = MarketData(
            kalshi_client=_kalshi_client(gz_cfg),
            poly_client=pm2.PolymarketClient(gamma_base=gz_cfg.gamma_base, clob_base=gz_cfg.clob_base,
                                             timeout=gz_cfg.http_timeout_seconds))
        gz_engine.debug_gate(tree, now=datetime.now(timezone.utc), md=md, gz_cfg=gz_cfg)
        return 0

    kalshi = poly = None
    if exec_cfg.live_allowed:
        from ..executor.kalshi_exec import KalshiExec
        from ..executor.poly_exec import PolyExec
        kalshi = KalshiExec(api_base=exec_cfg.kalshi_api_base, log=log)
        poly = PolyExec(log=log)
        log.warning("[GENZ] LIVE execution path active (executor enabled=true, dry_run=false).")
    else:
        log.info("[GENZ] dry-run (executor enabled=%s dry_run=%s) — measure only, nothing placed.",
                 exec_cfg.enabled, exec_cfg.dry_run)

    once = not args.loop                       # default = a single cycle; --loop runs continuously
    log.info("[GENZ] run (%s): interval=%.0fs loop=%s", sport, gz_cfg.interval_seconds, args.loop)
    gz_engine.run_loop(gz_cfg, exec_cfg, interval=args.interval, once=once,
                       log=log, kalshi=kalshi, poly=poly, paths=paths)
    return 0


# --------------------------------------------------------------------------- #
# status                                                                        #
# --------------------------------------------------------------------------- #
def cmd_report(args) -> int:
    """Read the genz_arbs feed and print UNIQUE arbs ranked by persistence (seen_count) — so the
    dry-run evidence is readable without drowning in duplicate per-cycle rows."""
    from . import report
    rows = report.read_rows()
    if not rows:
        print("No genz_arbs rows yet (data/genz/genz_arbs*.csv). Let the loop run first.")
        return 0
    uniques = report.aggregate(rows)
    print(f"{len(rows)} raw row(s) -> {len(uniques)} unique arb(s).")
    print(report.format_report(uniques, limit=args.limit))
    return 0


def cmd_status(args) -> int:
    gz_config.ensure_dirs()
    exec_cfg = exec_config.load_exec_config()
    print(f"executor: enabled={exec_cfg.enabled} dry_run={exec_cfg.dry_run} "
          f"live_allowed={exec_cfg.live_allowed}  (GenZ measures only unless live_allowed)")
    for label, path in (("match_tree", gz_config.MATCH_TREE_PATH), ("tree_meta", gz_config.TREE_META_PATH),
                        ("heartbeat", gz_config.HEARTBEAT_PATH)):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if label == "match_tree":
                print(f"  match_tree: {len(data.get('games', {}))} game(s)")
            else:
                print(f"  {label}: {json.dumps(data)[:200]}")
        else:
            print(f"  {label}: (none yet at {path})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="genz", description="GenZ Kalshi<->Polymarket soccer arbitrage.")
    sub = p.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("build-tree", help="JOB 1: rebuild the static match tree (hourly).")
    bt.add_argument("--lookahead", type=float, default=None, help="hours ahead to discover games (default 48).")
    bt.add_argument("--sport", choices=("soccer", "mlb", "tennis"), default="soccer",
                    help="which sport to build (default soccer — the existing WC pipeline).")
    bt.set_defaults(func=cmd_build_tree)

    rn = sub.add_parser("run", help="JOB 2: fast price + arb loop.")
    rn.add_argument("--loop", action="store_true", help="run continuously (default one cycle).")
    rn.add_argument("--once", action="store_true", help="run exactly one cycle and exit.")
    rn.add_argument("--interval", type=float, default=None, help="seconds between cycles (default 20).")
    rn.add_argument("--sport", choices=("soccer", "mlb", "tennis"), default="soccer",
                    help="which sport to price (default soccer — the existing WC pipeline).")
    rn.add_argument("--debug-gate", dest="debug_gate", action="store_true",
                    help="print per-game kickoff/now/started + each totals node's exact legs; then exit.")
    rn.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="print UNIQUE arbs from the feed, ranked by persistence.")
    rp.add_argument("--limit", type=int, default=50, help="max unique arbs to show (default 50).")
    rp.set_defaults(func=cmd_report)

    st = sub.add_parser("status", help="print config + last heartbeat.")
    st.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    # Configure the root logger -> stdout so every cycle's one-line summary is EMITTED (without this
    # the "genz" logger propagates to a handler-less root and Python's lastResort drops all INFO; the
    # supervisor then captures nothing and data/ops/genz.log stays 0 bytes).
    setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
