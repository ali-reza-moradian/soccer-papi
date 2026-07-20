"""OG MULTI-SPORT — the 4-book layer (Kalshi, Polymarket, Pinnacle, 1xbet) for MLB, tennis, UFC.

A STANDALONE scanner, separate from the soccer OG (``src/run.py``, untouched) and reusing the proven
primitives: the GenZ sport trees (``data/genz/<sport>_tree.json``), the live exchange readers
(``src.executor.resolve.MarketData``), the arb math (``src.arbitrage.compute_arb`` — exact per-share
Kalshi/Polymarket taker fees), and the honest walk-to-stake sizer (``src.og_sizing.honest_size``).

``python -m src.og_multi`` runs ONE multi-sport scan cycle: per enabled sport it loads the GenZ tree,
fetches the-odds-api odds (self-learning, 422-proof adapter), matches events to tree games, assembles
best-of-4-book candidates, computes the arb, walks the exchange legs to stake, and writes
``data/og_current_<sport>.json`` (same panel shape as ``og_current.json`` plus tier/family/settlement
fields). Per-sport cadence is enforced inside the cycle via last-run stamps, so one process
(``scripts/run_og_multi_loop.ps1``, supervised component ``og_multi``) serves every sport.

ALERT-ONLY: this package holds NO executor and NO order-placement code.

Modules:
  * ``toa``   — the-odds-api adapter (sport-key discovery, capability learning, quota discipline).
  * ``match`` — the-odds-api event -> GenZ tree game matcher (shared per-sport name normalizer).
  * ``tiers`` — Tier A (enrich tree families with book legs) + Tier B (book-only families).
  * ``sizing``— thin wrapper over ``src.og_sizing`` for the walked exchange legs.
  * ``state`` — persisted caches (tennis keys, learned capabilities, quota) + per-sport run stamps.
  * ``scan``  — the one-cycle orchestrator that ties it together and writes the panel files.
"""
