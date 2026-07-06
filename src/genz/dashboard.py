"""GenZ Streamlit dashboard — a SEPARATE view from the OG scanner / executor panels.

    streamlit run src/genz/dashboard.py

Per game it shows the match tree with live best-of-both prices per market, the implied-cost (sum%)
for each 2-outcome market colored green (<1, an arb) / red (>=1), the unmatched markets (one venue
only), and the genz_arbs feed + heartbeat. Renders from data/genz/ files by default; tick "price
live" to walk the live books for the current best-of-both (read-only).
"""
from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from ..executor.resolve import MarketData
from . import config as gz_config
from . import engine as gz_engine
from . import report as gz_report
from . import tree_builder


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _impl_color(cost: float) -> str:
    return "background-color:#123d1f;color:#7ef0a8" if cost < 1.0 else "background-color:#3d1212;color:#f08a8a"


def _price_game(md: MarketData, game_nodes: list[dict], stake: float) -> dict[tuple, gz_engine.PricedVenue]:
    markets = gz_engine.collect_markets({"games": {"g": {"away": "", "home": "", "nodes": game_nodes}}})
    cfg = gz_config.load_genz_config()
    cfg.walk_stake_usd = stake
    return gz_engine.price_markets(md, markets, cfg)


def main() -> None:
    st.set_page_config(page_title="GenZ — Kalshi×Polymarket arb", layout="wide")
    st.title("GenZ — Kalshi ⟷ Polymarket soccer arbitrage")
    st.caption("Standalone from the OG scanner. Execution is delegated to the executor; with its "
               "default flags (enabled:false / dry_run:true) this is measure-only.")

    tree = tree_builder.load_tree()
    meta = _load_json(gz_config.TREE_META_PATH)
    hb = _load_json(gz_config.HEARTBEAT_PATH)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Games", len(tree.get("games", {})))
    c2.metric("Last cycle", hb.get("last_cycle_utc", "—"))
    c3.metric("Nodes priced", f"{hb.get('nodes_priced', '—')} / {hb.get('nodes_unpriced', '—')} unpriced")
    c4.metric("Arbs found", hb.get("arbs_found", "—"))
    c5.metric("Would-trade", hb.get("would_trade", "—"))
    st.caption(f"Tree built: {meta.get('built_utc', '—')}  •  next kickoffs: {', '.join(meta.get('next_kickoffs', [])[:5])}")
    failed_any = meta.get("kalshi_series_failed_any_game") or []
    if failed_any:
        st.warning(f"Coverage gaps this build — Kalshi series that failed to fetch for some game(s): "
                   f"{', '.join(failed_any)}")

    price_live = st.checkbox("Price live (walk the live books, read-only)", value=False)
    stake = st.number_input("Walk-to-stake (units)", min_value=10.0, value=200.0, step=10.0)
    md = MarketData() if price_live else None

    for game_id, g in (tree.get("games") or {}).items():
        st.subheader(f"{g.get('away', '?')} @ {g.get('home', '?')}  —  {game_id}  ({g.get('kickoff_utc', '')})")
        cov = g.get("coverage") or {}
        if cov.get("kalshi_failed") or cov.get("poly_failed"):
            st.caption(f"⚠ coverage gaps — kalshi failed: {cov.get('kalshi_failed', [])} • "
                       f"poly failed: {cov.get('poly_failed', [])}")
        nodes = g.get("nodes") or []
        priced = _price_game(md, nodes, stake) if md else {}

        # group 2-outcome markets for best-of-both + implied cost
        markets = gz_engine.collect_markets({"games": {game_id: g}})
        rows = []
        for m in markets:
            row = {"market": m.market_type, "line": m.line, "confidence": m.confidence}
            impl = 0.0
            ok = True
            for side, node in m.sides.items():
                q = gz_engine._best_side(node, priced) if md else None
                if q is None:
                    row[f"{side}"] = "—"
                    ok = False
                else:
                    row[f"{side}"] = f"{q.venue[:1].upper()} {q.price:.3f}"
                    impl += q.price
            row["implied_cost"] = round(impl, 4) if (md and ok) else None
            rows.append(row)
        if rows:
            df = pd.DataFrame(rows)
            if md and "implied_cost" in df:
                df = df.style.applymap(lambda v: _impl_color(v) if isinstance(v, (int, float)) else "",
                                       subset=["implied_cost"])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.write("No 2-outcome markets matched for this game yet.")

        unmatched = g.get("unmatched") or []
        if unmatched:
            with st.expander(f"Unmatched markets ({len(unmatched)} — on one venue only)"):
                st.dataframe(pd.DataFrame(unmatched), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("genz_arbs — unique arbs by persistence")
    st.caption("The feed appends a row every cycle, so this DEDUPES on (game, market_type, line, "
               "side_a, side_b): seen_count = how many cycles the arb recurred (persistence = real; "
               "flash-once = noise), with latest price + first/last-seen.")
    rows = gz_report.read_rows()
    if rows:
        uniques = gz_report.rank(gz_report.aggregate(rows))
        cols = ["seen_count", "first_seen", "last_seen", "game", "market_type", "line",
                "side_a", "venue_a", "price_a", "side_b", "venue_b", "price_b",
                "latest_implied_cost", "median_implied_cost", "latest_roi_pct",
                "would_trade", "latest_status", "confidence"]
        df = pd.DataFrame(uniques)
        st.dataframe(df[[c for c in cols if c in df.columns]], use_container_width=True, hide_index=True)
        st.caption(f"{len(rows)} raw row(s) collapsed to {len(uniques)} unique arb(s).")
        with st.expander("raw feed (latest 200 rows)"):
            st.dataframe(pd.DataFrame(rows).tail(200).iloc[::-1], use_container_width=True, hide_index=True)
    else:
        st.write("No detected arbs logged yet (data/genz/genz_arbs*.csv).")


if __name__ == "__main__":
    main()
