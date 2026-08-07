"""Plain stdout logging so the full arbitrage math is readable in the Actions logs."""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False

#: Longest line any of our own logging emits is the LIVE ARMED banner at 510 chars, so 800 keeps every
#: real line intact while capping a pasted response body. This exists because ``py_clob_client_v2``
#: logs ``resp.text`` UNBOUNDED on any non-200: each Cloudflare "error 1015 — rate limited" page arrived
#: as 137 lines / 8,485 bytes, and 6,466 of them landed in one day. One bad response should cost one
#: line you can read, not a screenful that buries the errors around it. (The offending logger is a
#: third-party package — the cap belongs here, in OUR config, never in site-packages.)
_MAX_LINE = int(os.environ.get("LOG_MAX_LINE") or 800)

#: Third-party HTTP loggers, quieted to WARNING. httpx logs one INFO line PER REQUEST, and reconciliation
#: alone made ~54,000 of them in 14.6h — the single biggest contributor to a 3.3 GB maker_rt.log. Set
#: HTTP_LOG_LEVEL=INFO to get the per-request trace back when actually debugging a venue conversation.
_HTTP_LOGGERS = ("httpx", "httpcore", "urllib3", "requests", "websockets", "web3", "asyncio")


class _TruncatingFormatter(logging.Formatter):
    """Format normally, then cap the result, saying how much was dropped rather than hiding it."""

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        if _MAX_LINE > 0 and len(s) > _MAX_LINE:
            return f"{s[:_MAX_LINE]}… [+{len(s) - _MAX_LINE} chars truncated]"
        return s


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
        handler.setFormatter(_TruncatingFormatter("%(asctime)s %(levelname)-5s %(message)s",
                                                  datefmt="%H:%M:%S"))
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(lvl)
        # Quiet noisy third-party loggers (see _HTTP_LOGGERS: httpx alone was ~54k INFO lines / 14.6h).
        http_lvl_name = (os.environ.get("HTTP_LOG_LEVEL") or "WARNING").upper()
        http_lvl = getattr(logging, http_lvl_name, logging.WARNING)
        for name in _HTTP_LOGGERS:
            logging.getLogger(name).setLevel(http_lvl)
        _CONFIGURED = True
    else:
        logging.getLogger().setLevel(lvl)

    return logging.getLogger("arb")


def get_logger(name: str = "arb") -> logging.Logger:
    return logging.getLogger(name)
