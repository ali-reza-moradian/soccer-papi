"""Order-client construction for the PRE-GAME live path — the single guarded place clients are built.

The one hard invariant: a SHADOW / default process must NEVER even construct an order client. So this
returns ``(None, None)`` unless ``cfg.live.enabled`` is true, and the executor adapters are imported
lazily (construction touches no network — the Kalshi signer and the Poly ClobClient are both lazy).
Both ``__main__._run`` and ``--smoke`` go through here so the "clients only when enabled" rule is
tested in one place.
"""
from __future__ import annotations

from typing import Any, Optional


def live_enabled(cfg: Any) -> bool:
    return bool(getattr(getattr(cfg, "live", None), "enabled", False))


def build_pregame_order_clients(cfg: Any, *, log: Any = None) -> tuple[Optional[Any], Optional[Any]]:
    """Return (kalshi_exec, poly_exec) for the PRE-GAME live path, or (None, None) in shadow.

    Constructs clients ONLY when ``cfg.live.enabled`` — a shadow/default config yields (None, None)
    and imports nothing from the executor package."""
    if not live_enabled(cfg):
        return None, None
    from ...executor import config as exec_config
    from ...executor.kalshi_exec import KalshiExec
    from ...executor.poly_exec import PolyExec
    exec_cfg = exec_config.load_exec_config()
    return KalshiExec(api_base=exec_cfg.kalshi_api_base, log=log), PolyExec(log=log)
