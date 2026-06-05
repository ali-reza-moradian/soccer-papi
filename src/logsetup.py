"""Plain stdout logging so the full arbitrage math is readable in the Actions logs."""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging(level: str | None = None) -> logging.Logger:
    """Configure root logging to stdout. Idempotent.

    Level can be overridden with the LOG_LEVEL env var (default INFO; DEBUG shows
    near-misses and per-leg detail).
    """
    global _CONFIGURED
    lvl_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, lvl_name, logging.INFO)

    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s",
                                                datefmt="%H:%M:%S"))
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(lvl)
        # Quiet noisy third-party loggers.
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        _CONFIGURED = True
    else:
        logging.getLogger().setLevel(lvl)

    return logging.getLogger("arb")


def get_logger(name: str = "arb") -> logging.Logger:
    return logging.getLogger(name)
