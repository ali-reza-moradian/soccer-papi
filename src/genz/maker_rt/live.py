"""The LIVE gate — the hard lock in front of every order path.

TWO INDEPENDENT GATES, one per phase (a fill's phase decides which applies):
  * PRE-GAME  (``maker_rt.live``)        — armed iff enabled AND ``ARM_MAKER`` exists AND self-check.
  * IN-PLAY   (``maker_rt.live_inplay``) — armed iff enabled AND ``ARM_MAKER_INPLAY`` exists AND
    self-check. STRICTER downstream rails (freeze cool-off + hedge re-verify) live in the executor.

The two gates share nothing but the startup self-check (readable balances on BOTH venues, Polymarket
allowance/approvals readable, tick sizes fetchable); their enabled flags + arm files are separate, so
arming pre-game never arms in-play and vice-versa. In this build BOTH default enabled False, so neither
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


def is_inplay(phase: Any) -> bool:
    return str(phase) == "inplay"


def assert_live_allowed(phase: Any, armed: bool) -> None:
    """HARD LOCK re-checked immediately before a live order fires: refuse unless ``armed`` is the result
    of the phase's OWN gate (pre-game or in-play). Raises AssertionError otherwise. The two-gate design
    replaced the old blanket in-play refusal — in-play may now fire, but ONLY through its own armed gate."""
    assert armed, f"LIVE not armed for phase={phase!r} — refusing to place an order."


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

    def _evaluate_block(self, block: Any, label: str) -> GateResult:
        """Evaluate ONE gate block (enabled + arm_file + self-check). Shared by both gates."""
        if not getattr(block, "enabled", False):
            return GateResult(False, f"{label}.enabled is false (shadow)")
        arm = getattr(block, "arm_file", "")
        if not (arm and os.path.exists(arm)):
            return GateResult(False, f"arm file missing ({arm})")
        checks = self._self_check()
        if not checks or not all(checks.values()):
            failed = [k for k, v in checks.items() if not v]
            return GateResult(False, f"self-check failed: {failed}", checks=checks)
        return GateResult(True, "armed", checks=checks)

    def evaluate(self) -> GateResult:
        """The PRE-GAME gate (``maker_rt.live`` — enabled + ARM_MAKER + self-check)."""
        return self._evaluate_block(getattr(self.cfg, "live", None), "live")

    def evaluate_inplay(self) -> GateResult:
        """The IN-PLAY gate (``maker_rt.live_inplay`` — enabled + ARM_MAKER_INPLAY + self-check).
        INDEPENDENT of the pre-game gate: separate enabled flag AND separate arm file."""
        return self._evaluate_block(getattr(self.cfg, "live_inplay", None), "live_inplay")

    def may_place(self, phase: Any, *, gate: Optional[GateResult] = None,
                  inplay_gate: Optional[GateResult] = None) -> bool:
        """TWO-GATE rule: an in-play fill needs the IN-PLAY gate armed; any other phase needs the
        PRE-GAME gate armed. The two are evaluated independently (own enabled flag + own arm file)."""
        if is_inplay(phase):
            g = inplay_gate or self.evaluate_inplay()
        else:
            g = gate or self.evaluate()
        return bool(g.armed)


def _require(res: Any) -> None:
    """A (ok, reason) preflight tuple must be ok, else raise so the probe records a fail."""
    ok = res[0] if isinstance(res, (tuple, list)) else bool(res)
    if not ok:
        raise RuntimeError(str(res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else res))
