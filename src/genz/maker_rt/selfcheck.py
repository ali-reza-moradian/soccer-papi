"""Verbose per-item self-check for maker_rt — ``python -m src.genz.maker_rt --selfcheck``.

READ-ONLY diagnostic. It constructs the REAL order clients (KalshiExec + PolyExec) exactly as the
armed live path would (mirrors src/genz/cli.py), then runs each LiveGate readiness probe
INDIVIDUALLY and prints PASS/FAIL with the full underlying exception (type + message). For every
credential the clients look for it reports whether the env var is SET / EMPTY / MISSING (NAMES
ONLY — a value is NEVER printed; wallet addresses are masked), and it prints the gate arming state
(enabled + arm file) so the operator can tell "credentials missing" apart from "gate can't arm in
this build".

Places NOTHING (read-only balance/allowance/tick reads only — the same reads exec_preflight does).

WHY THIS EXISTS: the running maker_rt process (``__main__._run``) builds LiveGate WITHOUT order
clients (the shadow lock), so its startup self-check logs ``kalshi_balance``/``poly_balance`` as
FAILED whenever a gate is armed (enabled + arm file) — REGARDLESS of whether the credentials are
valid. This flag is how you probe the credentials for real and separate the two causes.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

from . import config as mrt_config
from .live import LiveGate
from .universe import build_universe, load_trees, poly_tokens


# --------------------------------------------------------------------------- #
# Helpers — never emit a secret value                                            #
# --------------------------------------------------------------------------- #
def _env_status(name: str) -> str:
    """SET / EMPTY / MISSING for env var ``name`` — never returns or prints the value."""
    if name not in os.environ:
        return "MISSING"
    return "SET" if os.environ[name].strip() else "EMPTY"


def _mask(v: Any) -> Any:
    """Mask a wallet address to first6...last4 so a diagnostic paste never leaks the full address."""
    if not v:
        return v
    s = str(v)
    return s if len(s) <= 12 else f"{s[:6]}...{s[-4:]}"


_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def _mask_addrs(text: Any) -> str:
    """Mask every 0x…-address inside a free-text string (e.g. the preflight OK message)."""
    return _ADDR_RE.sub(lambda m: str(_mask(m.group(0))), str(text))


def _probe(fn) -> tuple:
    """Run ``fn``; return (ok, err_type, err_msg). Never raises."""
    try:
        fn()
        return True, None, None
    except Exception as exc:  # noqa: BLE001 - a failed probe is a fail, not a crash
        return False, type(exc).__name__, str(exc)


def _mark(ok: Optional[bool]) -> str:
    return "SKIP" if ok is None else ("PASS" if ok else "FAIL")


# --------------------------------------------------------------------------- #
# Report sections                                                               #
# --------------------------------------------------------------------------- #
def _print_gate_state(cfg: Any) -> None:
    print("\n-- gate arming state (this is what makes the startup self-check RUN) --")
    for label, blk in (("pre-game (maker_rt.live)       ", getattr(cfg, "live", None)),
                       ("in-play  (maker_rt.live_inplay)", getattr(cfg, "live_inplay", None))):
        enabled = bool(getattr(blk, "enabled", False))
        arm = getattr(blk, "arm_file", "")
        exists = bool(arm and os.path.exists(arm))
        print(f"  {label}: enabled={enabled} arm_file={arm!r} arm_exists={exists}")


def _print_env_block() -> None:
    print("\n-- credentials (names/presence only; a VALUE is NEVER printed) --")
    print(f"  KALSHI_API_KEY_ID          : {_env_status('KALSHI_API_KEY_ID')}"
          f"   (fallback KALSHI_ACCESS_KEY: {_env_status('KALSHI_ACCESS_KEY')})")
    print(f"  KALSHI_PRIVATE_KEY (PEM)   : {_env_status('KALSHI_PRIVATE_KEY')}")
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY_FILE")
    tail = (f"path={path!r} exists={os.path.exists(path if os.path.isabs(path) else os.path.join(mrt_config.REPO_ROOT, path))}"
            if path else "no path set")
    print(f"  KALSHI_PRIVATE_KEY_PATH    : {_env_status('KALSHI_PRIVATE_KEY_PATH')}"
          f"   (FILE: {_env_status('KALSHI_PRIVATE_KEY_FILE')}) -> {tail}")
    print(f"  POLYGON_PRIVATE_KEY        : {_env_status('POLYGON_PRIVATE_KEY')}")
    st = os.environ.get("POLY_SIGNATURE_TYPE")
    print(f"  POLY_SIGNATURE_TYPE        : {_env_status('POLY_SIGNATURE_TYPE')}"
          f"   (value={st if st is not None else '<unset->default 1>'})")
    fsrc = "POLY_FUNDER_ADDRESS" if os.environ.get("POLY_FUNDER_ADDRESS", "").strip() else "derived-from-signer"
    print(f"  POLY_FUNDER_ADDRESS        : {_env_status('POLY_FUNDER_ADDRESS')}   (funder source: {fsrc})")


def _sample_poly_token(cfg: Any) -> Optional[str]:
    """A real poly token from the CURRENT universe, so the tick probe hits a live token (or None)."""
    try:
        universe = build_universe(load_trees(), time.time(), max_games=cfg.max_games,
                                  expire_before_kickoff_s=cfg.expire_before_kickoff_s,
                                  horizon_hours=cfg.inplay.horizon_hours)
        toks = poly_tokens(universe)
        return toks[0] if toks else None
    except Exception:  # noqa: BLE001 - no universe -> just skip the tick probe
        return None


# --------------------------------------------------------------------------- #
# Runner                                                                         #
# --------------------------------------------------------------------------- #
def run_selfcheck(cfg: Any, *, log: Any = None) -> int:
    """Run every readiness probe individually with verbose diagnostics. Returns 0 iff the required
    probes (kalshi_balance, poly_balance, poly_preflight) all PASS. Places NOTHING."""
    print("=" * 78)
    print("maker_rt --selfcheck  (READ-ONLY; constructs real order clients; places NOTHING)")
    print("=" * 78)

    _print_gate_state(cfg)
    _print_env_block()

    from ...executor import config as exec_config
    exec_cfg = exec_config.load_exec_config()

    print("\n-- probes (each run individually; full exception shown on FAIL) --")
    results: dict[str, Optional[bool]] = {}

    # 1) kalshi_balance -------------------------------------------------------
    from ...executor.kalshi_exec import KalshiExec
    kalshi = KalshiExec(api_base=exec_cfg.kalshi_api_base, log=log)
    ok, et, em = _probe(kalshi.get_balance)
    results["kalshi_balance"] = ok
    print(f"[{_mark(ok)}] kalshi_balance")
    print(f"        endpoint : GET {exec_cfg.kalshi_api_base}/portfolio/balance")
    print("        auth hdrs: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, "
          "KALSHI-ACCESS-SIGNATURE (RSA-PSS/SHA-256)")
    print(f"        key id   : KALSHI_API_KEY_ID={_env_status('KALSHI_API_KEY_ID')} "
          f"(or KALSHI_ACCESS_KEY={_env_status('KALSHI_ACCESS_KEY')})")
    print(f"        rsa key  : PEM={_env_status('KALSHI_PRIVATE_KEY')} "
          f"PATH={_env_status('KALSHI_PRIVATE_KEY_PATH')} FILE={_env_status('KALSHI_PRIVATE_KEY_FILE')}")
    if not ok:
        print(f"        ERROR    : {et}: {em}")

    # 2) poly_balance ---------------------------------------------------------
    from ...executor.poly_exec import PolyExec, PolyExecError, resolve_wallet
    poly = PolyExec(log=log)
    try:
        w = resolve_wallet()
        sig_type, funder, signer, wallet_err = w.get("signature_type"), w.get("funder"), w.get("signer_address"), None
    except PolyExecError as exc:
        sig_type = funder = signer = None
        wallet_err = str(exc)
    ok, et, em = _probe(poly.get_balance)
    results["poly_balance"] = ok
    print(f"[{_mark(ok)}] poly_balance")
    print("        client   : py_clob_client_v2.client.ClobClient "
          "(host=https://clob.polymarket.com, chain=137)")
    print(f"        sig_type : {sig_type if sig_type is not None else '?'} "
          f"(POLY_SIGNATURE_TYPE={_env_status('POLY_SIGNATURE_TYPE')}; default 1=POLY_PROXY)")
    fsrc = "POLY_FUNDER_ADDRESS" if os.environ.get("POLY_FUNDER_ADDRESS", "").strip() else "derived-from-signer"
    print(f"        funder   : {fsrc} -> {_mask(funder)}")
    print(f"        signer   : derived from POLYGON_PRIVATE_KEY={_env_status('POLYGON_PRIVATE_KEY')} "
          f"-> {_mask(signer)}")
    if wallet_err:
        print(f"        wallet   : resolve_wallet FAILED: {wallet_err}")
    if not ok:
        print(f"        ERROR    : {et}: {em}")

    # 3) poly_preflight (allowance/approvals via a signed get_api_keys read) ---
    pf = {"reason": ""}

    def _preflight() -> None:
        res = poly.can_place_polymarket_orders()
        ok2 = res[0] if isinstance(res, (tuple, list)) else bool(res)
        pf["reason"] = str(res[1]) if isinstance(res, (tuple, list)) and len(res) > 1 else ""
        if not ok2:
            raise RuntimeError(pf["reason"] or "preflight returned not-ok")

    ok, et, em = _probe(_preflight)
    results["poly_preflight"] = ok
    print(f"[{_mark(ok)}] poly_preflight  (allowance/approvals; signed get_api_keys read)")
    if ok:
        print(f"        detail   : {_mask_addrs(pf['reason']) or 'OK'}")
    else:
        print(f"        ERROR    : {_mask_addrs(em)}")

    # 4) poly_tick (tick_size + neg_risk fetch for a real universe token) ------
    sample = _sample_poly_token(cfg)
    if sample is None:
        results["poly_tick"] = None
        print("[SKIP] poly_tick  (no poly token in current universe; nothing to fetch a tick for)")
    else:
        ok, et, em = _probe(lambda: poly._tick_and_negrisk(sample))
        results["poly_tick"] = ok
        print(f"[{_mark(ok)}] poly_tick  (get_tick_size + get_neg_risk for a sample token)")
        print(f"        token    : {str(sample)[:10]}...")
        if not ok:
            print(f"        ERROR    : {et}: {em}")

    # Definitive gate decision WITH the real clients injected. -----------------
    gate = LiveGate(cfg, kalshi_client=kalshi, poly_client=poly, sample_token=sample, log=None)
    pre, ip = gate.evaluate(), gate.evaluate_inplay()
    print("\n-- gate decision WITH real clients injected (the definitive would-it-arm answer) --")
    print(f"  pre-game : armed={pre.armed} reason={pre.reason!r} checks={pre.checks}")
    print(f"  in-play  : armed={ip.armed} reason={ip.reason!r} checks={ip.checks}")

    print("\n-- NOTE --")
    print("  The RUNNING maker_rt process now injects order clients into LiveGate when")
    print("  maker_rt.live.enabled is true, so its startup gate ARMS (gates.pre=true) exactly as the")
    print("  probes above show. But the CONTINUOUS pre-game placement executor is NOT wired yet, so")
    print("  normal operation still quotes SHADOW. Use `--smoke` to place one real order end-to-end.")

    required = ("kalshi_balance", "poly_balance", "poly_preflight")
    all_ok = all(bool(results.get(k)) for k in required) and (results.get("poly_tick") in (True, None))
    print("\n" + "=" * 78)
    verdict = "PASS" if all_ok else "FAIL"
    tail = ("credentials resolve and both venues are readable"
            if all_ok else "at least one probe FAILED (see ERROR lines above)")
    print(f"SELFCHECK {verdict} -- {tail}")
    print("=" * 78)
    return 0 if all_ok else 1
