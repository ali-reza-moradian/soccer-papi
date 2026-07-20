"""Persisted state for the OG multi-sport scanner — isolated under ``data/og_multi/``.

Four small JSON caches + the per-sport run stamps that let ONE process serve every sport at its own
cadence (the spec's "per-sport cadence enforced inside scan.py via last-run stamps"):

  * ``toa_tennis_keys.json``  — the live tennis tournament sport-keys, discovered once per scan-day.
  * ``toa_capabilities.json`` — the learned valid bulk-market set per the-odds-api sport-key (a market
    that 422s is dropped here and re-probed at most once a week; see src/og_multi/toa.py).
  * ``last_run.json``         — {sport: iso_utc} of each sport's last completed scan (cadence gate).
  * ``quota.json``            — the running the-odds-api credit total for the current UTC day.

The per-sport panel feeds ``data/og_current_<sport>.json`` live in ``data/`` (next to the soccer
``og_current.json``), NOT under ``data/og_multi/`` — so the panel fetches them by a sibling path.

Every function takes an overridable ``base``/``data_dir`` so tests can redirect to a tmp dir; the
module-level defaults are the real repo paths. Writes are atomic (tmp + ``os.replace``).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(REPO_ROOT, "data")
OG_MULTI_DIR = os.path.join(DATA_DIR, "og_multi")

TENNIS_KEYS_NAME = "toa_tennis_keys.json"
CAPABILITIES_NAME = "toa_capabilities.json"
LAST_RUN_NAME = "last_run.json"
QUOTA_NAME = "quota.json"


# --------------------------------------------------------------------------- #
# Paths + atomic JSON I/O                                                        #
# --------------------------------------------------------------------------- #
def ensure_dir(base: str = OG_MULTI_DIR) -> None:
    """Create ``data/og_multi/`` (and ``data/``) if absent. Safe to call repeatedly."""
    os.makedirs(base, exist_ok=True)


def og_current_path(sport: str, data_dir: str = DATA_DIR) -> str:
    """The per-sport panel feed — ``data/og_current_<sport>.json`` (sibling of the soccer file)."""
    return os.path.join(data_dir, f"og_current_{sport}.json")


def read_json(path: str, default: Any) -> Any:
    """Load JSON at ``path``; return ``default`` on any missing/corrupt/unreadable file (never raise —
    a corrupt cache must self-heal, not crash a scan)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return default


def write_json(path: str, obj: Any) -> None:
    """Atomically write ``obj`` as pretty JSON to ``path`` (tmp + os.replace)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _iso(now: datetime) -> str:
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


iso_utc = _iso              # public alias (used by scan.py / toa.py for the cycle_utc + drop stamps)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Tennis sport-key cache (discovered once per scan-day)                          #
# --------------------------------------------------------------------------- #
def load_tennis_keys(base: str = OG_MULTI_DIR) -> dict[str, Any]:
    """Return {'date','keys','billed'} or {} when never discovered / stale format."""
    obj = read_json(os.path.join(base, TENNIS_KEYS_NAME), {})
    return obj if isinstance(obj, dict) else {}


def save_tennis_keys(day: str, keys: list[str], billed: bool, base: str = OG_MULTI_DIR) -> None:
    write_json(os.path.join(base, TENNIS_KEYS_NAME),
               {"date": day, "keys": list(keys), "billed": bool(billed)})


def tennis_keys_fresh(cache: dict[str, Any], now: datetime) -> bool:
    """True when the cached tennis keys are from the CURRENT UTC scan-day (else re-discover)."""
    return bool(cache) and cache.get("date") == _iso(now)[:10]


# --------------------------------------------------------------------------- #
# Learned capability cache (valid bulk-market set per sport-key)                 #
# --------------------------------------------------------------------------- #
def load_capabilities(base: str = OG_MULTI_DIR) -> dict[str, Any]:
    """Return {sport_key: {'valid': [...], 'dropped': {market: iso_dropped}}} (or {} when absent)."""
    obj = read_json(os.path.join(base, CAPABILITIES_NAME), {})
    return obj if isinstance(obj, dict) else {}


def save_capabilities(caps: dict[str, Any], base: str = OG_MULTI_DIR) -> None:
    write_json(os.path.join(base, CAPABILITIES_NAME), caps)


# --------------------------------------------------------------------------- #
# Per-sport run stamps — the cadence gate                                        #
# --------------------------------------------------------------------------- #
def load_last_run(base: str = OG_MULTI_DIR) -> dict[str, str]:
    obj = read_json(os.path.join(base, LAST_RUN_NAME), {})
    return obj if isinstance(obj, dict) else {}


def mark_ran(sport: str, now: datetime, base: str = OG_MULTI_DIR) -> None:
    """Stamp ``sport`` as having completed a scan at ``now`` (persisted for the cadence gate)."""
    stamps = load_last_run(base)
    stamps[sport] = _iso(now)
    write_json(os.path.join(base, LAST_RUN_NAME), stamps)


def is_due(sport: str, interval_s: float, now: datetime, base: str = OG_MULTI_DIR) -> bool:
    """True when ``sport`` has never run or its last run is >= ``interval_s`` ago. One long-lived
    wrapper (sleep = the MIN interval) thus fires each sport on its own schedule."""
    last = _parse_iso(load_last_run(base).get(sport))
    if last is None:
        return True
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    return (now - last).total_seconds() >= float(interval_s) - 1e-6


# --------------------------------------------------------------------------- #
# Quota accounting — running the-odds-api credit total for the UTC day           #
# --------------------------------------------------------------------------- #
def record_credits(credits: int, now: datetime, base: str = OG_MULTI_DIR) -> int:
    """Add ``credits`` to today's running total (resetting on a UTC-day rollover) and return the new
    daily total. Used by the quota-discipline WARN in toa.py (projected month vs plan quota)."""
    day = _iso(now)[:10]
    obj = read_json(os.path.join(base, QUOTA_NAME), {})
    if not isinstance(obj, dict) or obj.get("day") != day:
        obj = {"day": day, "used_today": 0}
    obj["used_today"] = int(obj.get("used_today", 0)) + int(credits)
    write_json(os.path.join(base, QUOTA_NAME), obj)
    return int(obj["used_today"])
