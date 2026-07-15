"""Execution engine — N-leg (Phase A) dry-run + live with generalized auto-unwind.

``execute_arb(arb, *, live=False, ...)`` is the single entry point for BOTH the pre-game and the
in-play feeds. It re-derives LIVE order params (never the stale detection prices), walks every
leg's live book, models fees + slippage, and:

  * DRY-RUN (live=False, the default): measures edge-survival and logs to dryrun_log.csv. Places
    NOTHING. This is the safe default and the point of the whole module.
  * LIVE (live=True): only reachable when cfg.live_allowed (enabled AND not dry_run) AND, if
    require_human_confirm, a confirm() y/N passes AND no STOP file AND all guardrails pass.

N-leg model: an arb is an ordered list of N legs (N=2 or 3) where EVERY leg trades on kalshi or
polymarket (a non-tradable venue -> skip "untradable_venue"). For a 3-way 1x2 winner arb the
Home/Draw/Away outcomes are MECE, split any way across the two venues. Legs fire HARDEST/THINNEST
first (least depth at target; kalshi before poly), each IOC (kalshi) / FOK (poly), sized to the
same target count; if an earlier leg partial-fills, every later leg is sized down to that realized
minimum. AUTO-UNWIND generalized: if any leg FAILS (0 fill) we immediately close all already-
filled legs back to flat; if legs merely under-fill we close the excess on the over-filled legs so
all legs end at the matched size. Never leaves a partial multi-leg position; verifies flat-or-
hedged afterward and HALTS (STOP file) on a stuck naked leg.
"""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config as exec_config
from . import fees_sizing as fs
from .guardrails import Guardrails
from .ledger import Ledger
from .resolve import MarketData, NormalizedArb, ResolveError, VenueLeg, normalize_arb

DRYRUN_COLUMNS = [
    "ts_utc", "status", "source", "detected_at", "fixture", "market", "fingerprint", "n_legs",
    "intended_size",
    "kalshi_top_price", "kalshi_top_size", "poly_top_price", "poly_top_size",
    "kalshi_fill_price", "poly_fill_price", "kalshi_slippage", "poly_slippage",
    "kalshi_fee", "poly_fee", "total_cost", "net_profit", "net_edge_pct", "arb_survived",
    "legs_json", "skip_reason",
]


def _arb_source(narb) -> str:
    """Where this arb came from, for the dry-run log: an explicit ``source`` on the arb dict
    (e.g. 'live', 'selfcheck') else inferred 'live-feed'/'pre-game' from the fingerprint."""
    src = (narb.raw or {}).get("source") if narb.raw else None
    if src:
        return str(src)
    return "live-feed" if str(narb.fingerprint).startswith("live|") else "pre-game"


def _skip_row(arb: dict[str, Any], narb, reason: str) -> dict[str, Any]:
    """Build a VISIBLE status='skipped' dry-run row (no survival number) carrying fixture/market/
    legs/venues/source + the reason — so the panel shows the attempt and WHY it produced no number.
    Works whether or not normalize_arb succeeded (``narb`` may be None on a resolve error)."""
    if narb is not None:
        source = _arb_source(narb)
        legs = [{"venue": l.venue, "outcome": l.outcome, "id": l.identifier, "side": l.side}
                for l in narb.legs]
        fixture, market, fingerprint = narb.fixture, narb.market, narb.fingerprint
        n_legs, detected = narb.n_legs, (narb.detected_at or "")
    else:
        raw_legs = [l for l in (arb.get("legs") or []) if isinstance(l, dict)]
        fp = str(arb.get("signature", "") or "")
        source = str(arb.get("source") or ("live-feed" if fp.startswith("live|") else "pre-game"))
        legs = [{"venue": l.get("venue") or l.get("book"), "outcome": l.get("outcome"),
                 "id": l.get("venue_id"), "side": l.get("venue_side")} for l in raw_legs]
        fixture, market, fingerprint = arb.get("match", ""), arb.get("market", ""), fp
        n_legs, detected = len(raw_legs), str(arb.get("detected_at", "") or "")
    return {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "skipped", "skip_reason": reason,
        "source": source, "detected_at": detected,
        "fixture": fixture, "market": market, "fingerprint": fingerprint,
        "n_legs": n_legs, "intended_size": "", "arb_survived": "",
        "legs_json": json.dumps(legs),
    }


def _skipped(arb: dict[str, Any], narb, reason: str, *, live: bool, write_log: bool,
             path: str, log: Any) -> "ExecResult":
    """Never drop an arb silently: log WHY, and on the DRY-RUN path write a visible status='skipped'
    row to dryrun_log.csv (carrying the row in ``detail`` so the loop's dedupe can persist it). Live
    skips keep the old behavior (no dry-run row) but still log + return the reason."""
    row = _skip_row(arb, narb, reason)
    if log:
        log.warning("[EXEC] skipped — %s | %s | %s", reason, row["fixture"], row["market"])
    if not live and write_log:
        _append_dryrun(path, row)
    return ExecResult("skipped", reason=reason, live=live, detail=row)


@dataclass
class ExecResult:
    status: str
    reason: str = ""
    live: bool = False
    intended_size: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    arb_survived: Optional[bool] = None
    trade_id: Optional[str] = None


# --------------------------------------------------------------------------- #
# Live-book access + sizing                                                      #
# --------------------------------------------------------------------------- #
def fetch_ladder(market_data: Any, leg: VenueLeg) -> list[tuple[float, float]]:
    """LIVE ascending ask ladder to BUY this leg, dispatched by venue. Works with the real
    MarketData and any fake exposing kalshi_ask_ladder / poly_ask_ladder."""
    if leg.venue == "kalshi":
        return market_data.kalshi_ask_ladder(leg.identifier, leg.side)
    return market_data.poly_ask_ladder(leg.identifier)


def plan_size(narb: NormalizedArb, ladders: list[list[tuple[float, float]]],
              cfg: exec_config.ExecConfig) -> int:
    """Intended INTEGER unit count for EVERY leg (sized to the integer Kalshi leg).

    Bounded by (a) the per-trade notional cap across ALL legs at detection prices and (b)
    ~volume_haircut of the THINNEST leg's total depth, so a FOK/IOC is likely to fully fill."""
    px_sum = sum(l.detected_price for l in narb.legs)
    n_cap = (cfg.max_per_trade_usd / px_sum) if px_sum > 0 else 0.0
    min_depth = min((sum(s for _, s in ladder) for ladder in ladders), default=0.0)
    n_depth = fs.volume_haircut(min_depth, cfg.volume_haircut)
    return max(0, int(min(n_cap, n_depth)))


def _append_dryrun(path: str, row: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Schema-safe append: if an existing file's header differs from the current columns (e.g. an
    # older log written before a column was added), migrate it in place by re-mapping every row by
    # NAME, then append. Never let a header drift silently shift values into the wrong columns.
    existing: list[dict[str, Any]] = []
    needs_rewrite = not os.path.exists(path)
    if not needs_rewrite:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if list(reader.fieldnames or []) != DRYRUN_COLUMNS:
                existing = list(reader)          # DictReader maps by name -> safe remap
                needs_rewrite = True
    if needs_rewrite:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=DRYRUN_COLUMNS)
            w.writeheader()
            for r in existing:
                w.writerow({k: r.get(k, "") for k in DRYRUN_COLUMNS})
            w.writerow({k: row.get(k, "") for k in DRYRUN_COLUMNS})
        return
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=DRYRUN_COLUMNS)
        w.writerow({k: row.get(k, "") for k in DRYRUN_COLUMNS})


def append_dryrun(row: dict[str, Any], path: Optional[str] = None) -> None:
    """Public: append one dry-run row (schema-safe). Used by the loop's dedupe to write only the
    rows it decides to keep (execute_arb itself can be called with write_log=False)."""
    _append_dryrun(path or exec_config.DRYRUN_LOG_PATH, row)


def dryrun_dedupe_key(row: dict[str, Any]) -> tuple:
    """Identity for loop dedupe: the structural fingerprint plus the price/edge that actually
    moves between cycles. Two consecutive rows with the same key are 'identical' and not re-logged.
    ``skip_reason`` is part of the key so a skipped arb whose reason CHANGES (e.g. empty book ->
    size 0) is re-logged, while the same skip every cycle is not re-spammed."""
    return (row.get("fingerprint", ""), row.get("net_edge_pct", ""),
            row.get("kalshi_fill_price", ""), row.get("poly_fill_price", ""),
            row.get("skip_reason", ""))


# --------------------------------------------------------------------------- #
# Entry point                                                                    #
# --------------------------------------------------------------------------- #
def execute_arb(arb: dict[str, Any], *, live: bool = False,
                cfg: Optional[exec_config.ExecConfig] = None,
                market_data: Optional[MarketData] = None,
                kalshi: Any = None, poly: Any = None,
                ledger: Optional[Ledger] = None, guard: Optional[Guardrails] = None,
                confirm: Optional[Callable[[NormalizedArb, int], bool]] = None,
                dryrun_log_path: Optional[str] = None, write_log: bool = True,
                poly_fee_rate: float = fs.DEFAULT_POLY_FEE_RATE,
                log: Any = None) -> ExecResult:
    """Dry-run by default. See module docstring for the live preconditions.

    ``write_log=False`` computes the dry-run row and returns it in ``result.detail`` WITHOUT
    appending to dryrun_log.csv — the caller (e.g. the loop's dedupe) decides whether to persist."""
    cfg = cfg or exec_config.load_exec_config()
    market_data = market_data or MarketData()
    dryrun_log_path = dryrun_log_path or exec_config.DRYRUN_LOG_PATH

    try:
        narb = normalize_arb(arb)
    except ResolveError as exc:
        return _skipped(arb, None, str(exc), live=live, write_log=write_log,
                        path=dryrun_log_path, log=log)

    if any(not leg.identifier for leg in narb.legs):
        return _skipped(arb, narb, "missing venue identifier on a leg", live=live,
                        write_log=write_log, path=dryrun_log_path, log=log)

    # Re-pull LIVE books for every leg (never trust detection prices).
    try:
        ladders = [fetch_ladder(market_data, leg) for leg in narb.legs]
    except Exception as exc:  # noqa: BLE001 - any data error -> skip safely
        if guard:
            guard.record_error()
        return _skipped(arb, narb, f"book fetch failed: {exc}", live=live,
                        write_log=write_log, path=dryrun_log_path, log=log)

    if any(not ladder for ladder in ladders):
        return _skipped(arb, narb, "empty live book on a leg", live=live,
                        write_log=write_log, path=dryrun_log_path, log=log)

    size = plan_size(narb, ladders, cfg)
    if size <= 0:
        return _skipped(arb, narb, "planned size is 0 (cap/depth too small)", live=live,
                        write_log=write_log, path=dryrun_log_path, log=log)

    # Walk every book at the intended size -> realized fill prices + edge after costs (all N legs).
    walks = [fs.walk_book(ladder, size) for ladder in ladders]
    edge = fs.edge_after_costs_n(size, [(leg.venue, w.avg_price) for leg, w in zip(narb.legs, walks)],
                                 poly_fee_rate)

    if not live:
        return _do_dryrun(narb, ladders, walks, size, edge, dryrun_log_path, log, write_log)

    return _do_live(narb, ladders, size, edge, cfg, kalshi, poly, ledger, guard, confirm, log)


# --------------------------------------------------------------------------- #
# DRY-RUN                                                                        #
# --------------------------------------------------------------------------- #
def _do_dryrun(narb, ladders, walks, size, edge, path, log, write_log=True) -> ExecResult:
    legs_detail = []
    for leg, ladder, w in zip(narb.legs, ladders, walks):
        legs_detail.append({
            "venue": leg.venue, "outcome": leg.outcome, "id": leg.identifier, "side": leg.side,
            "top_price": round(ladder[0][0], 4), "top_size": ladder[0][1],
            "fill_price": round(w.avg_price, 4), "slippage": round(w.avg_price - leg.detected_price, 4),
            "fully_filled": w.fully_filled,
        })
    first_k = next((d for d in legs_detail if d["venue"] == "kalshi"), None)
    first_p = next((d for d in legs_detail if d["venue"] == "polymarket"), None)
    row = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "dryrun", "skip_reason": "",
        "source": _arb_source(narb),
        "detected_at": narb.detected_at or "",
        "fixture": narb.fixture, "market": narb.market, "fingerprint": narb.fingerprint,
        "n_legs": narb.n_legs, "intended_size": size,
        "kalshi_top_price": (first_k or {}).get("top_price", ""),
        "kalshi_top_size": (first_k or {}).get("top_size", ""),
        "poly_top_price": (first_p or {}).get("top_price", ""),
        "poly_top_size": (first_p or {}).get("top_size", ""),
        "kalshi_fill_price": (first_k or {}).get("fill_price", ""),
        "poly_fill_price": (first_p or {}).get("fill_price", ""),
        "kalshi_slippage": (first_k or {}).get("slippage", ""),
        "poly_slippage": (first_p or {}).get("slippage", ""),
        "kalshi_fee": round(edge.kalshi_fee, 4), "poly_fee": round(edge.poly_fee, 4),
        "total_cost": round(edge.total_cost, 4), "net_profit": round(edge.net_profit, 4),
        "net_edge_pct": round(edge.net_edge_pct, 4), "arb_survived": edge.arb_survived,
        "legs_json": json.dumps(legs_detail),
    }
    if write_log:
        _append_dryrun(path, row)
    if log:
        log.info("[EXEC dry-run] %s | %s | %d legs | size %d | net edge %.2f%% | survived=%s",
                 narb.fixture, narb.market, narb.n_legs, size, edge.net_edge_pct, edge.arb_survived)
    return ExecResult("dryrun", reason="logged" if write_log else "computed", live=False,
                      intended_size=size, arb_survived=edge.arb_survived, detail=row)


# --------------------------------------------------------------------------- #
# LIVE + GENERALIZED AUTO-UNWIND                                                 #
# --------------------------------------------------------------------------- #
def _short(venue: str) -> str:
    return "poly" if venue == "polymarket" else "kalshi"


def _place_leg(leg: VenueLeg, leg_size: int, limit: float, kalshi, poly, trade_id: str,
               i: int) -> dict[str, Any]:
    """Fire one leg (kalshi IOC / poly FOK). Returns {fill, avg, requested, raw, error}."""
    try:
        if leg.venue == "kalshi":
            res = kalshi.place_order(leg.identifier, leg.side, leg_size, limit,
                                     time_in_force="immediate_or_cancel",
                                     client_order_id=f"{trade_id}-{i}")
            return {"fill": int(res.get("fill_count", 0)), "avg": float(res.get("avg_price") or limit),
                    "requested": leg_size, "raw": res, "error": None}
        res = poly.place_order(leg.identifier, limit, leg_size, "BUY", order_type="FOK")
        return {"fill": float(res.get("shares", 0.0)), "avg": float(res.get("avg_price") or limit),
                "requested": leg_size, "raw": res, "error": None}
    except Exception as exc:  # noqa: BLE001 - any placement error -> failed leg (0 fill)
        return {"fill": 0, "avg": limit, "requested": leg_size, "raw": str(exc), "error": str(exc)}


def _unwind_leg(leg: VenueLeg, count: float, buy_price: float, kalshi, poly, log) -> dict[str, Any]:
    """Market-sell/close ``count`` units of ``leg`` back toward flat. Returns
    {venue, sold, sell_price, cost, note}. ``cost`` is the realized unwind LOSS."""
    if count <= 0:
        return {"venue": leg.venue, "sold": 0, "sell_price": 0.0, "cost": 0.0, "note": "nothing to unwind"}
    try:
        if leg.venue == "kalshi":
            sell = kalshi.place_market_sell(leg.identifier, leg.side, int(count), client_order_id="unwind")
            sold = int(sell.get("fill_count", 0))
        else:
            sell = poly.place_market_sell(leg.identifier, count)
            sold = float(sell.get("shares", sell.get("fill_count", 0)))
    except Exception as exc:  # noqa: BLE001 - unwind failed -> caller halts on residual naked leg
        return {"venue": leg.venue, "sold": 0, "sell_price": 0.0, "cost": float(count) * buy_price,
                "note": f"UNWIND FAILED: {exc}"}
    sell_price = float(sell.get("avg_price") or 0.0)
    fee = (fs.kalshi_fee_usd(int(count), buy_price) + fs.kalshi_fee_usd(int(sold), sell_price)
           if leg.venue == "kalshi" else 0.0)
    cost = (count * buy_price) - (sold * sell_price) + fee
    return {"venue": leg.venue, "sold": sold, "sell_price": sell_price, "cost": max(0.0, cost),
            "raw": sell, "note": f"unwound {sold}/{count} {leg.venue} @ {sell_price:.4f}"}


def _do_live(narb, ladders, size, edge, cfg, kalshi, poly, ledger, guard, confirm, log) -> ExecResult:
    # --- hard preconditions ------------------------------------------------
    if not cfg.live_allowed:
        return ExecResult("blocked", reason="live not allowed (need enabled=true AND dry_run=false)",
                          live=True, intended_size=size)
    if exec_config.stop_file_present():
        return ExecResult("stopped", reason="STOP file present", live=True, intended_size=size)

    ledger = ledger or Ledger()
    guard = guard or Guardrails(cfg, ledger=ledger)

    leg_ladders = list(zip(narb.legs, ladders))
    decision = guard.pre_trade_check_n(narb, edge, leg_ladders, size)
    if not decision.allowed:
        if decision.halt:
            exec_config.trip_stop(decision.reason)
            if log:
                log.error("[EXEC] HALT — %s (STOP file set).", decision.reason)
            return ExecResult("halted", reason=decision.reason, live=True, intended_size=size)
        if log:
            log.info("[EXEC] guardrail skip: %s", decision.reason)
        return ExecResult("blocked", reason=decision.reason, live=True, intended_size=size)

    # --- human confirm gate ------------------------------------------------
    if cfg.require_human_confirm:
        ok = bool(confirm(narb, size)) if confirm else False
        if not ok:
            return ExecResult("aborted", reason="human confirm declined/absent", live=True, intended_size=size)

    # --- submit record -----------------------------------------------------
    limits = [fs.marketable_limit(leg.detected_price, cfg.marketable_buffer, side="buy")
              for leg in narb.legs]
    first_k = narb.kalshi
    first_p = narb.poly
    trade_id = ledger.append_submit({
        "fingerprint": narb.fingerprint, "fixture": narb.fixture, "market": narb.market,
        "n_legs": narb.n_legs,
        "kalshi_ticker": first_k.identifier if first_k else "",
        "kalshi_side": first_k.side if first_k else "",
        "poly_token": first_p.identifier if first_p else "",
        "poly_side": first_p.side if first_p else "",
        "intended_size": size, "modeled_edge_pct": round(edge.net_edge_pct, 4),
        "legs_json": json.dumps([{"venue": l.venue, "id": l.identifier, "side": l.side,
                                  "outcome": l.outcome} for l in narb.legs]),
        "status": "submitted",
    })
    guard.mark_fired(narb.fingerprint)

    # --- fire legs hardest/thinnest-first (least depth at target; kalshi before poly) ------
    order = sorted(range(len(narb.legs)),
                   key=lambda i: (fs.depth_at_or_below(ladders[i], narb.legs[i].detected_price),
                                  0 if narb.legs[i].venue == "kalshi" else 1))

    fired: list[dict[str, Any]] = []     # in fire order: {idx, leg, fill, avg, requested}
    realized_min = float(size)
    for i in order:
        leg = narb.legs[i]
        leg_size = int(realized_min)
        if leg.venue == "polymarket":
            leg_size = max(leg_size, fs.min_poly_shares(leg.detected_price))   # clear Poly min
        res = _place_leg(leg, leg_size, limits[i], kalshi, poly, trade_id, i)
        if res["error"]:
            guard.record_error()
        fired.append({"idx": i, "leg": leg, "fill": res["fill"], "avg": res["avg"],
                      "requested": leg_size, "raw": res["raw"]})

        if res["fill"] <= 0:
            # FULL FAILURE of this leg -> stop firing; unwind everything already filled -> flat.
            return _fail_unwind(narb, fired, size, kalshi, poly, ledger, guard, trade_id, log)

        realized_min = min(realized_min, float(res["fill"]))

    # All legs filled > 0. Reconcile every leg DOWN to the matched (minimum) size.
    matched = int(realized_min)
    return _finalize_filled(narb, fired, size, matched, edge, kalshi, poly, ledger, guard, trade_id, log)


def _fail_unwind(narb, fired, size, kalshi, poly, ledger, guard, trade_id, log) -> ExecResult:
    """A leg fully failed. Unwind every previously-filled leg back to flat. Special-case: if NOTHING
    had filled yet (the first fired leg failed) it is a clean no-fill abort with zero exposure."""
    failed = fired[-1]["leg"]
    prior = [f for f in fired[:-1] if f["fill"] > 0]
    if not prior:
        guard.record_success()
        status = f"{_short(failed.venue)}_no_fill"
        ledger.update(trade_id, status=status, realized_pnl=0)
        if log:
            log.info("[EXEC] %s — aborted with no exposure.", status)
        return ExecResult(status, reason=f"{failed.venue} leg filled 0 (no prior fills)", live=True,
                          intended_size=size, trade_id=trade_id, detail={"fired": fired})

    if log:
        log.warning("[EXEC] %s leg failed — AUTO-UNWIND %d already-filled leg(s) back to flat.",
                    failed.venue, len(prior))
    unwinds = [_unwind_leg(f["leg"], f["fill"], f["avg"], kalshi, poly, log) for f in prior]
    total_cost = sum(u["cost"] for u in unwinds)
    _record_unwinds(ledger, trade_id, "leg_failure_unwind", narb, fired, unwinds, total_cost,
                    realized_pnl=-total_cost)
    result = ExecResult("leg_failure_unwind",
                        reason=f"{failed.venue} leg failed; unwound {len(prior)} leg(s) to flat",
                        live=True, intended_size=size, trade_id=trade_id,
                        detail={"fired": fired, "unwinds": unwinds})
    # Expected end state: flat on every leg.
    _verify_positions(narb, {l.identifier: 0 for l in narb.legs if l.venue == "kalshi"},
                      kalshi, poly, ledger, trade_id, result, log)
    return result


def _finalize_filled(narb, fired, size, matched, edge, kalshi, poly, ledger, guard, trade_id, log) -> ExecResult:
    """Every leg filled > 0. Reduce each over-filled leg DOWN to ``matched`` (the realized minimum)
    so all legs end hedged at the same size. Clean (no reduction) -> 'filled'; any reduction (an
    under-fill occurred) -> 'leg_failure_unwind' at the reduced size."""
    unwinds: list[dict[str, Any]] = []
    for f in fired:
        excess = float(f["fill"]) - matched
        if excess > 1e-9:
            unwinds.append(_unwind_leg(f["leg"], excess, f["avg"], kalshi, poly, log))
    guard.record_success()
    expected_k = {l.identifier: matched for l in narb.legs if l.venue == "kalshi"}

    if not unwinds:
        # Clean full hedge at the target size.
        realized = round(matched - edge.total_cost, 4)
        ledger.update(trade_id, status="filled", intended_size=size, residual_shares=0,
                      realized_pnl=realized, unhedged_kalshi=0, unhedged_poly=0,
                      legs_json=json.dumps(_legs_summary(narb, fired, matched, unwinds)))
        result = ExecResult("filled", reason=f"all {narb.n_legs} legs filled at {matched}",
                            live=True, intended_size=size, trade_id=trade_id,
                            detail={"fired": fired, "matched": matched})
        _verify_positions(narb, expected_k, kalshi, poly, ledger, trade_id, result, log)
        return result

    total_cost = sum(u["cost"] for u in unwinds)
    if log:
        log.warning("[EXEC] under-fill — reduced all legs to matched=%d, unwound excess on %d leg(s).",
                    matched, len(unwinds))
    _record_unwinds(ledger, trade_id, "leg_failure_unwind", narb, fired, unwinds, total_cost,
                    realized_pnl=-total_cost, matched=matched)
    result = ExecResult("leg_failure_unwind",
                        reason=f"under-fill; all legs reduced to matched={matched}",
                        live=True, intended_size=size, trade_id=trade_id,
                        detail={"fired": fired, "unwinds": unwinds, "matched": matched})
    _verify_positions(narb, expected_k, kalshi, poly, ledger, trade_id, result, log)
    return result


def _legs_summary(narb, fired, matched, unwinds) -> list[dict[str, Any]]:
    by_idx = {f["idx"]: f for f in fired}
    out = []
    for i, leg in enumerate(narb.legs):
        f = by_idx.get(i, {})
        out.append({"venue": leg.venue, "id": leg.identifier, "side": leg.side,
                    "requested": f.get("requested"), "filled": f.get("fill"),
                    "avg": f.get("avg"), "final": matched})
    return out


def _record_unwinds(ledger, trade_id, status, narb, fired, unwinds, total_cost, *,
                    realized_pnl, matched=0) -> None:
    venues = sorted({u["venue"] for u in unwinds if u["sold"] or u["cost"]})
    event = ", ".join(f"{_short(v)}_market_sell" for v in venues) or "unwind"
    ledger.update(trade_id, status=status,
                  unwind_event=event, unwind_cost=round(total_cost, 4),
                  realized_pnl=round(realized_pnl, 4),
                  unhedged_kalshi=sum(1 for u in unwinds if u["venue"] == "kalshi"),
                  unhedged_poly=sum(1 for u in unwinds if u["venue"] == "polymarket"),
                  legs_json=json.dumps(_legs_summary(narb, fired, matched, unwinds)),
                  note="; ".join(u["note"] for u in unwinds)[:500])


def _verify_positions(narb, expected_kalshi: dict[str, int], kalshi, poly,
                      ledger, trade_id, result: ExecResult, log) -> None:
    """Post-trade safety check (spec): confirm net position on every KALSHI leg matches expectation
    (the hedged ``matched`` size, or 0 when flat). On any residual naked leg, attempt ONE more
    unwind and then HALT (STOP file) + flag the result. (Poly FOK is atomic; not position-checked.)"""
    try:
        positions = kalshi.get_positions()
    except Exception as exc:  # noqa: BLE001 - cannot verify -> be conservative, HALT
        exec_config.trip_stop(f"post-trade position check failed: {exc}")
        result.detail["post_check"] = f"verify failed -> HALT: {exc}"
        ledger.update(trade_id, note="post-trade verify failed -> HALT")
        if log:
            log.error("[EXEC] post-trade verify failed -> HALT: %s", exc)
        return

    naked_total = 0
    for ticker, expected in expected_kalshi.items():
        held = _held_for_ticker(positions, ticker)
        naked = held - int(expected)
        if naked > 0:
            naked_total += naked
            leg = next((l for l in narb.legs if l.identifier == ticker), None)
            if leg is not None:
                _unwind_leg(leg, naked, leg.detected_price, kalshi, poly, log)

    if naked_total <= 0:
        result.detail["post_check"] = "flat-or-hedged"
        return

    exec_config.trip_stop(f"post-hoc naked leg(s) total {naked_total}; second unwind then halted")
    ledger.update(trade_id, status="unhedged_halt", unwind_event="post_hoc_unwind",
                  note="post-hoc naked leg -> second unwind + HALT")
    result.status = "unhedged_halt"
    result.reason = f"post-hoc naked leg(s) total {naked_total}; second unwind + HALT"
    result.detail["post_check"] = f"naked {naked_total} -> second unwind + HALT"
    if log:
        log.error("[EXEC] post-hoc naked leg(s) total %d -> second unwind + HALT.", naked_total)


def _held_for_ticker(positions: Any, ticker: str) -> int:
    rows = []
    if isinstance(positions, dict):
        rows = positions.get("market_positions") or positions.get("positions") or []
    elif isinstance(positions, list):
        rows = positions
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("ticker") or r.get("market_ticker") or "") == ticker:
            try:
                return abs(int(r.get("position") or r.get("net_position") or 0))
            except (TypeError, ValueError):
                return 0
    return 0
