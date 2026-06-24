"""Local monitoring panel for the executor (Streamlit) — READ-MOSTLY.

It OBSERVES the executor's existing files/state and exposes exactly ONE write action: the STOP
kill-switch (create/remove data/executor/STOP). It never places, sizes, or modifies a trade, and
never imports or affects run.py / the scanner. Balance reads go through the existing read-only
adapter methods behind a short TTL cache so the panel cannot rate-limit the venues.

All logic lives in pure, unit-tested helpers; Streamlit is a thin rendering layer imported lazily
inside ``run_panel`` so importing this module (and testing it) needs no Streamlit and no network.

Launch:  python -m src.executor.cli panel      (or: streamlit run src/executor/dashboard.py)
"""
from __future__ import annotations

import csv
import json
import os
import time
from typing import Any, Callable, Optional

from . import config as exec_config
from .engine import DRYRUN_COLUMNS  # noqa: F401  (column reference / parity)
from .ledger import Ledger


# --------------------------------------------------------------------------- #
# File tailing (safe when missing/empty)                                        #
# --------------------------------------------------------------------------- #
def tail_csv(path: str, n: int = 50, *, newest_first: bool = True) -> list[dict[str, Any]]:
    """Last ``n`` rows of a CSV as dicts. [] when the file is missing or has only a header."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return []
    rows = rows[-n:]
    return list(reversed(rows)) if newest_first else rows


def tail_lines(path: str, n: int = 200) -> list[str]:
    """Last ``n`` non-empty-trimmed lines of a text/log file. [] when missing."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    return lines[-n:]


# --------------------------------------------------------------------------- #
# STATUS BAR helpers                                                             #
# --------------------------------------------------------------------------- #
def flag_badges(cfg: exec_config.ExecConfig) -> list[dict[str, Any]]:
    """The three master flags as badges with a safety LEVEL (safe/warn/danger). The safe state
    (the shipped default) is always 'safe'."""
    return [
        {"label": "enabled", "value": cfg.enabled, "level": "danger" if cfg.enabled else "safe"},
        {"label": "dry_run", "value": cfg.dry_run, "level": "safe" if cfg.dry_run else "danger"},
        {"label": "live_enabled", "value": cfg.live_enabled,
         "level": "warn" if cfg.live_enabled else "safe"},
    ]


def counter_summary(cfg: exec_config.ExecConfig, ledger: Optional[Ledger] = None) -> dict[str, Any]:
    """Today's LIVE counters (trades / spend / realized loss) vs the configured caps."""
    led = ledger or Ledger()
    c = led.today_live_counters()
    loss, cap = float(c["loss_usd"]), float(cfg.max_daily_loss_usd)
    loss_pct = (loss / cap * 100.0) if cap > 0 else 0.0
    return {
        "trades": c["trades"], "max_trades": cfg.max_trades_per_day,
        "spend_usd": round(c["spend_usd"], 2), "max_spend_usd": cfg.max_daily_spend_usd,
        "loss_usd": round(loss, 2), "max_loss_usd": cap,
        "loss_pct_of_cap": round(loss_pct, 1),
        "loss_near_cap": loss_pct >= 80.0,
        "loss_cap_hit": loss >= cap > 0,
    }


def stop_state() -> bool:
    """True if the STOP kill-switch file exists (executor halted)."""
    return exec_config.stop_file_present()


def set_stop(reason: str = "manual via dashboard") -> None:
    """THE ONLY WRITE the panel performs: create the STOP kill-switch file."""
    exec_config.trip_stop(reason)


def clear_stop() -> bool:
    """Remove the STOP file (RESUME). Returns True if a file was removed."""
    return exec_config.clear_stop()


# --------------------------------------------------------------------------- #
# Read-only balance cache (so the panel can't rate-limit the venues)            #
# --------------------------------------------------------------------------- #
class ReadCache:
    """Tiny TTL cache for read-only adapter calls. A producer exception is captured as
    {"error": ...} (and itself cached) so a broken/credential-less venue never crashes the panel
    and never hammers the API."""

    def __init__(self, ttl: float = 10.0, clock: Callable[[], float] = time.time) -> None:
        self.ttl = ttl
        self.clock = clock
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str, producer: Callable[[], Any]) -> Any:
        now = self.clock()
        hit = self._store.get(key)
        if hit is not None and (now - hit[1]) < self.ttl:
            return hit[0]
        try:
            val = producer()
        except Exception as exc:  # noqa: BLE001 - surface as a value, never raise into the panel
            val = {"error": str(exc)}
        self._store[key] = (val, now)
        return val


# --------------------------------------------------------------------------- #
# Panel 2 — dry-run arbs view                                                    #
# --------------------------------------------------------------------------- #
def infer_source(row: dict[str, Any]) -> str:
    """Pre-game vs live-feed, inferred read-only from the fingerprint (live-feed arbs are
    fingerprinted 'live|...'). No engine change needed."""
    return "live-feed" if str(row.get("fingerprint", "")).startswith("live|") else "pre-game"


def summarize_legs(legs_json: Any) -> str:
    """Compact 'venue:outcome' summary from a legs_json string/list (for the arbs/ledger tables)."""
    if not legs_json:
        return ""
    legs = legs_json
    if isinstance(legs_json, str):
        try:
            legs = json.loads(legs_json)
        except ValueError:
            return ""
    if not isinstance(legs, list):
        return ""
    parts = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        venue = str(leg.get("venue", "") or leg.get("book", ""))
        short = "poly" if venue == "polymarket" else (venue or "?")
        parts.append(f"{short}:{leg.get('outcome', '?')}")
    return ", ".join(parts)


def _as_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


_DRYRUN_VIEW_COLS = ["ts_utc", "source", "fixture", "market", "legs", "n_legs", "intended_size",
                     "kalshi_fill_price", "poly_fill_price", "kalshi_slippage", "poly_slippage",
                     "kalshi_fee", "poly_fee", "net_edge_pct", "arb_survived"]


def dryrun_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape dry-run rows for the table: add source + a compact legs summary, keep display columns."""
    out = []
    for r in rows:
        view = {k: r.get(k, "") for k in _DRYRUN_VIEW_COLS if k not in ("source", "legs")}
        view["source"] = infer_source(r)
        view["legs"] = summarize_legs(r.get("legs_json"))
        view["arb_survived"] = _as_bool(r.get("arb_survived"))
        out.append({k: view.get(k, "") for k in _DRYRUN_VIEW_COLS})
    return out


# --------------------------------------------------------------------------- #
# Panel 3 — trade ledger view                                                    #
# --------------------------------------------------------------------------- #
def ledger_row_alert(row: dict[str, Any]) -> bool:
    """True for rows that ended in an unwind or a HALT (highlight in the table)."""
    status = str(row.get("status", "")).lower()
    if status in ("leg_failure_unwind", "unhedged_halt"):
        return True
    return bool(str(row.get("unwind_event", "")).strip())


def classify_ledger_row(row: dict[str, Any]) -> str:
    """Coarse category for coloring: halt | unwind | no_fill | filled | submitted | other."""
    status = str(row.get("status", "")).lower()
    if status == "unhedged_halt":
        return "halt"
    if status == "leg_failure_unwind" or str(row.get("unwind_event", "")).strip():
        return "unwind"
    if status.endswith("_no_fill"):
        return "no_fill"
    if status == "filled":
        return "filled"
    if status == "submitted":
        return "submitted"
    return "other"


_LEDGER_VIEW_COLS = ["submit_utc", "status", "alert", "fixture", "market", "legs", "intended_size",
                     "kalshi_fill_count", "poly_fill_shares", "unhedged_kalshi", "unhedged_poly",
                     "unwind_event", "unwind_cost", "realized_pnl", "modeled_edge_pct"]


def ledger_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape ledger rows for the table: add an alert flag + compact legs summary."""
    out = []
    for r in rows:
        view = {k: r.get(k, "") for k in _LEDGER_VIEW_COLS if k not in ("alert", "legs")}
        view["alert"] = ledger_row_alert(r)
        view["legs"] = summarize_legs(r.get("legs_json"))
        out.append({k: view.get(k, "") for k in _LEDGER_VIEW_COLS})
    return out


# --------------------------------------------------------------------------- #
# Panel 4 — guardrail / event log                                               #
# --------------------------------------------------------------------------- #
_EVENT_KEYWORDS = ("guardrail", "cooldown", "dedupe", "cap", "untradable_venue", "HALT",
                   "no_fill", "AUTO-UNWIND", "under-fill", "consecutive", "STOP", "skip")


def filter_event_lines(lines: list[str], *, only_events: bool = True) -> list[str]:
    """Newest-first log lines; when ``only_events`` keep just the guardrail/decision lines so you
    can see WHY something didn't fire."""
    if only_events:
        lines = [ln for ln in lines if any(k.lower() in ln.lower() for k in _EVENT_KEYWORDS)]
    return list(reversed(lines))


# --------------------------------------------------------------------------- #
# Default read-only balance providers (lazy adapter construction)               #
# --------------------------------------------------------------------------- #
def default_balance_providers(cfg: exec_config.ExecConfig) -> dict[str, Callable[[], Any]]:
    """Zero-arg producers for Kalshi/Poly balances via the existing READ-ONLY adapter methods.
    Construction + the call are deferred so a missing credential surfaces as a cached error string,
    never an import-time or panel crash."""
    def kalshi_balance() -> Any:
        from .kalshi_exec import KalshiExec
        return KalshiExec(api_base=cfg.kalshi_api_base).get_balance()

    def poly_balance() -> Any:
        from .poly_exec import PolyExec
        return PolyExec().get_balance()

    return {"kalshi": kalshi_balance, "polymarket": poly_balance}


# --------------------------------------------------------------------------- #
# Streamlit rendering (lazy import; not unit-tested)                             #
# --------------------------------------------------------------------------- #
_LEVEL_COLOR = {"safe": "#1a7f37", "warn": "#bf8700", "danger": "#cf222e"}


def run_panel() -> None:  # pragma: no cover - Streamlit UI
    import streamlit as st

    st.set_page_config(page_title="Executor Monitor", layout="wide")
    cfg = exec_config.load_exec_config()

    # short-lived caches stored on the session so reruns reuse them
    if "_balcache" not in st.session_state:
        st.session_state["_balcache"] = ReadCache(ttl=10.0)
    balcache: ReadCache = st.session_state["_balcache"]
    providers = default_balance_providers(cfg)

    # ---- auto-refresh (best-effort; falls back to a manual button) ----
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=5000, key="auto5s")
    except Exception:  # noqa: BLE001
        st.caption("Install streamlit-autorefresh for live auto-refresh; using manual refresh.")
        st.button("Refresh")

    # ===================== STATUS BAR =====================
    st.subheader("Status")
    cols = st.columns(3)
    for col, b in zip(cols, flag_badges(cfg)):
        color = _LEVEL_COLOR[b["level"]]
        col.markdown(
            f"<div style='padding:8px;border-radius:8px;background:{color};color:white;"
            f"text-align:center;font-weight:700'>{b['label']} = {b['value']}</div>",
            unsafe_allow_html=True)

    bcols = st.columns(4)
    bcols[0].metric("Kalshi balance", str(balcache.get("kalshi", providers["kalshi"])))
    bcols[1].metric("Poly balance", str(balcache.get("polymarket", providers["polymarket"])))
    cs = counter_summary(cfg)
    bcols[2].metric("Today trades", f"{cs['trades']} / {cs['max_trades']}")
    bcols[3].metric("Today spend", f"${cs['spend_usd']} / ${cs['max_spend_usd']:.0f}")

    pnl_col, stop_col = st.columns([3, 1])
    pnl_col.metric("Realized loss vs cap",
                   f"${cs['loss_usd']} / ${cs['max_loss_usd']:.0f}  ({cs['loss_pct_of_cap']}%)",
                   delta="CAP HIT" if cs["loss_cap_hit"] else ("near cap" if cs["loss_near_cap"] else None),
                   delta_color="inverse")

    halted = stop_state()
    stop_col.markdown(f"**STOP file:** {'🛑 PRESENT (halted)' if halted else '✅ absent'}")
    if halted:
        if stop_col.checkbox("Confirm RESUME (remove STOP)"):
            if stop_col.button("RESUME ▶", type="primary"):
                clear_stop()
                st.rerun()
    else:
        if stop_col.button("STOP 🛑", type="primary"):
            set_stop("manual via dashboard")
            st.rerun()

    st.divider()

    # ===================== LIVE / RECENT ARBS =====================
    st.subheader("Live / recent arbs (dry-run)")
    arbs = dryrun_view(tail_csv(exec_config.DRYRUN_LOG_PATH, 100))
    if arbs:
        st.dataframe(arbs, use_container_width=True, hide_index=True)
    else:
        st.info("no data yet — dryrun_log.csv is empty/missing.")

    # ===================== TRADE LEDGER =====================
    st.subheader("Trade ledger")
    led = ledger_view(tail_csv(exec_config.LEDGER_PATH, 100))
    if led:
        st.dataframe(led, use_container_width=True, hide_index=True)
    else:
        st.info("no data yet — trade_ledger.csv is empty/missing (live trades only).")

    # ===================== GUARDRAIL / EVENT LOG =====================
    st.subheader("Guardrail / event log")
    only_events = st.checkbox("Only guardrail/decision lines", value=True)
    events = filter_event_lines(tail_lines(exec_config.LOG_PATH, 400), only_events=only_events)
    if events:
        st.code("\n".join(events[:200]))
    else:
        st.info("no data yet — executor.log is empty/missing.")


if __name__ == "__main__":  # `streamlit run src/executor/dashboard.py`
    run_panel()
