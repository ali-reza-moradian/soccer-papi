# GenZ engine — standalone Kalshi ⟷ Polymarket soccer arbitrage

GenZ is a **fully separate** system from the OG scanner (`src/run.py`). It has its own data path
(`data/genz/`), its own static match tree, its own fast price loop, and its own auto-execution. It
does **not** modify or import-affect `run.py` / `arbitrage.py`'s behavior; it **reuses** the proven
readers (`src/kalshi.py`, `src/polymarket.py`), the arb math (`src/arbitrage.py`), walk-to-stake
(`src/bookmath.py`), and the executor's safety machinery (`src/executor/`).

All execution flows through the executor, so the executor's three master switches govern GenZ too.
With the defaults (`enabled:false` / `dry_run:true` / `require_human_confirm:true`) GenZ **measures
only** — it computes would-be arbs and writes `data/genz/genz_arbs.csv`, and places nothing.

## Two jobs

### JOB 1 — Tree builder (slow, hourly)
```
python -m src.genz.cli build-tree            # --lookahead 48 (hours), default from config.yaml
```
Discovers every World Cup game kicking off within the lookahead window (Kalshi `KXWCGAME` event
tickers `KXWCGAME-<YYMMMDD><AWAY3><HOME3>`, e.g. `KXWCGAME-26JUN30CIVNOR`), enumerates **every**
market on **both** venues for each game (all `KXWC*` per-type series; the Polymarket 1x2 event +
every sibling discovered by the `fifwc-<away>-<home>-<date>` slug prefix), and pairs each Kalshi
outcome to its Polymarket twin with the deterministic rules in `match_rules.py` (no LLM). Writes:

* `data/genz/match_tree.json` — per game: matched nodes (each carrying both venues' identifiers +
  side + the line/threshold + `kind` `2way`/`3way`/`multi` + `confidence`) and an `unmatched` list.
* `data/genz/tree_meta.json` — `{built_utc, games, next_kickoffs}`.

### JOB 2 — Price engine (fast, ~20s loop)
```
python -m src.genz.cli run --loop --interval 20    # always-on
python -m src.genz.cli run --once                  # one cycle (cron / smoke test)
```
Loads `match_tree.json` and, each cycle, reads live prices **only** for the tickers/tokens already
in the tree (concurrent REST; Kalshi `*_dollars` + `orderbook_fp` ladder, Polymarket CLOB book). For
each matched **2-outcome** market it takes the cheapest ask per side across the two venues
(best-of-both) using the **walk-to-stake** fill, and flags an arb when `price_A + price_B < 1` after
costs. 3-way moneyline and many-outcome markets are recorded but **skipped** by v1. Each cycle writes
`data/genz/genz_arbs.csv` and `data/genz/genz_heartbeat.json`.

Guardrails/sizing/dedupe come from the executor; GenZ adds **at most one live trade attempt per
cycle**. Low-confidence (player-prop) nodes are **alert-only** and never auto-traded, even with the
flags on.

## Dashboard
```
streamlit run src/genz/dashboard.py
```
Per game: the match tree with live best-of-both prices, the implied-cost (sum%) colored green `<1` /
red `>=1`, the unmatched markets, and the `genz_arbs` feed + heartbeat. Kept separate from the OG
panel — two projects, two views.

## Scheduling (Windows Task Scheduler)

Run two jobs. The hourly tree build hands the fresh `match_tree.json` to the always-running price
loop via the file (the loop reloads the tree every cycle, so no restart is needed).

**(1) Hourly tree build** — `GenZ-BuildTree`:
```
schtasks /Create /TN "GenZ-BuildTree" /SC HOURLY ^
  /TR "cmd /c cd /d C:\bots\soccer-papi && python -m src.genz.cli build-tree >> data\genz\build.log 2>&1"
```

**(2) Always-on price loop** — `GenZ-Run` (start at logon, auto-restart if it ever exits):
```
schtasks /Create /TN "GenZ-Run" /SC ONLOGON /RL HIGHEST ^
  /TR "cmd /c cd /d C:\bots\soccer-papi && python -m src.genz.cli run --loop --interval 20 >> data\genz\run.log 2>&1"
```
In the task's **Settings**, enable *"If the task fails, restart every 1 minute"* and *"If the running
task does not end when requested, force it to stop"*; under **Conditions** untick *"Stop if the
computer switches to battery power"*. (Equivalently, register the XML with `Restart` settings.)

> Live trading stays OFF until an operator flips `executor.enabled: true` **and**
> `executor.dry_run: false` in `config.yaml` (and clears any `data/executor/STOP`). Until then both
> jobs are safe to schedule — they only read prices and write `data/genz/`.

## Config (`genz:` block in `config.yaml`)
`lookahead_hours` (48), `interval_seconds` (20), `max_workers` (12), `walk_stake_usd` (200),
`min_edge_pct` (1.0), `poly_series_slug` (`soccer-fifwc`). Trade safety lives in the `executor:`
block and is never duplicated here.
