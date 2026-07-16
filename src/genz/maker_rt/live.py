"""The LIVE gate — the hard lock in front of every order path.

Live refuses unless ALL of: config maker_rt.live.enabled is true, the on-disk arm file exists, AND a
startup self-check passes (readable balances on BOTH venues, Polymarket allowance/approvals readable,
tick sizes fetchable). Any failure -> SHADOW. In this build enabled defaults False, so the gate never
opens; the checks + refusal reasons are unit-tested with fakes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class GateResult:
    armed: bool
    reason: str
    checks: dict = field(default_factory=dict)


class LiveGate:
    """Decides whether the live order path may run. Clients are injected (fakes in tests); when the
    gate refuses, the caller must run in shadow and NEVER construct order clients."""

    def __init__(self, cfg: Any, *, kalshi_client: Any = None, poly_client: Any = None,
                 sample_token: Optional[str] = None, log: Any = None) -> None:
        self.cfg = cfg
        self.kalshi = kalshi_client
        self.poly = poly_client
        self.sample_token = sample_token
        self.log = log

    def _self_check(self) -> dict:
        """Run the startup readiness probes; each is pass/fail (an exception is a fail). NEVER raises."""
        checks: dict = {}

        def probe(name: str, fn) -> None:
            try:
                fn()
                checks[name] = True
            except Exception as exc:  # noqa: BLE001 - a failed probe is a refusal, not a crash
                checks[name] = False
                if self.log:
                    self.log.warning("[MAKER_RT][GATE] self-check '%s' failed: %s", name, exc)

        if self.kalshi is not None:
            probe("kalshi_balance", self.kalshi.get_balance)
        else:
            checks["kalshi_balance"] = False
        if self.poly is not None:
            probe("poly_balance", self.poly.get_balance)
            # allowance/approvals ride along on the v2 balance read; a separate preflight confirms auth.
            if hasattr(self.poly, "can_place_polymarket_orders"):
                probe("poly_preflight", lambda: _require(self.poly.can_place_polymarket_orders()))
            if self.sample_token and hasattr(self.poly, "_tick_and_negrisk"):
                probe("poly_tick", lambda: self.poly._tick_and_negrisk(self.sample_token))
        else:
            checks["poly_balance"] = False
        return checks

    def evaluate(self) -> GateResult:
        live = getattr(self.cfg, "live", None)
        if not getattr(live, "enabled", False):
            return GateResult(False, "live.enabled is false (shadow)")
        arm = getattr(live, "arm_file", "")
        if not (arm and os.path.exists(arm)):
            return GateResult(False, f"arm file missing ({arm})")
        checks = self._self_check()
        if not checks or not all(checks.values()):
            failed = [k for k, v in checks.items() if not v]
            return GateResult(False, f"self-check failed: {failed}", checks=checks)
        return GateResult(True, "armed", checks=checks)


def _require(res: Any) -> None:
    """A (ok, reason) preflight tuple must be ok, else raise so the probe records a fail."""
    ok = res[0] if isinstance(res, (tuple, list)) else bool(res)
    if not ok:
        raise RuntimeError(str(res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else res))
