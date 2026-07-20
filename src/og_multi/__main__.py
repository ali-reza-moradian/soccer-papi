"""``python -m src.og_multi`` — run ONE multi-sport OG scan cycle (see :func:`scan.run_cycle`).

The wrapper ``scripts/run_og_multi_loop.ps1`` re-invokes this every 300s (a fresh interpreter per
cycle); per-sport cadence is enforced inside via last-run stamps, so one process serves every sport.
"""
from .scan import main

if __name__ == "__main__":
    main()
