"""Load config.yaml + environment secrets / workflow-dispatch overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")
DEFAULT_CACHE_DIR = os.path.join(REPO_ROOT, "data", "cache")
DEFAULT_CSV_PATH = os.path.join(REPO_ROOT, "data", "arbitrage_opportunities.csv")


def _truthy(val: str | None) -> bool:
    return str(val).strip().lower() in {"1", "true", "yes", "on"} if val is not None else False


@dataclass
class Secrets:
    odds_papi_key: str | None
    telegram_bot_key: str | None
    telegram_group_id: str | None

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_key and self.telegram_group_id)


@dataclass
class Config:
    """Thin typed wrapper over config.yaml plus resolved env overrides."""

    raw: dict[str, Any]
    secrets: Secrets
    cache_dir: str = DEFAULT_CACHE_DIR
    csv_path: str = DEFAULT_CSV_PATH
    dry_run: bool = False

    # -- convenience accessors -------------------------------------------------
    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def sport_id(self) -> int:
        return int(self.get("sport_id", default=10))

    # NOTE: NO hardcoded date fallback. The scan window is computed at runtime in
    # src/run.py (_resolve_window writes a rolling 2-day range onto target_window before
    # these are read). If nothing has been set, these return None rather than leaking a
    # stale literal date — a missing window is a bug to surface, not a date to invent.
    @property
    def from_utc(self) -> str | None:
        return self.get("target_window", "from_utc", default=None)

    @property
    def to_utc(self) -> str | None:
        return self.get("target_window", "to_utc", default=None)

    @property
    def actionable_books(self) -> list[str]:
        return list(self.get("bookmakers", "actionable", default=[]) or [])

    @property
    def tracked_books(self) -> list[str]:
        return list(self.get("bookmakers", "tracked", default=[]) or [])

    @property
    def exchanges(self) -> set[str]:
        return set(self.get("bookmakers", "exchanges", default=[]) or [])

    @property
    def commission(self) -> dict[str, float]:
        return {k: float(v) for k, v in (self.get("bookmakers", "commission", default={}) or {}).items()}

    @property
    def pinned_tournament_ids(self) -> list[int]:
        return [int(x) for x in (self.get("tournaments", "pinned_ids", default=[]) or [])]

    @property
    def tournament_regex(self) -> str:
        return self.get("tournaments", "match_name_regex", default="friendl")

    @property
    def national_teams_only(self) -> bool:
        return bool(self.get("tournaments", "national_teams_only", default=True))

    @property
    def exclude_market_names(self) -> list[str]:
        return [str(x).lower() for x in (self.get("markets", "exclude_names", default=["double chance"]) or [])]

    @property
    def allow_quarter_lines(self) -> bool:
        return bool(self.get("markets", "allow_quarter_lines", default=False))

    def threshold(self, key: str, default: Any) -> Any:
        return self.get("thresholds", key, default=default)

    def telegram_opt(self, key: str, default: Any) -> Any:
        return self.get("telegram", key, default=default)

    def budget_opt(self, key: str, default: Any) -> Any:
        return self.get("budget", key, default=default)

    def mapping_guard_opt(self, key: str, default: Any) -> Any:
        return self.get("mapping_guard", key, default=default)

    def api_opt(self, key: str, default: Any) -> Any:
        return self.get("api", key, default=default)


def load_config(config_path: str | None = None) -> Config:
    """Load config.yaml, layer in env-based workflow inputs, and read secrets."""
    path = config_path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # ---- workflow_dispatch / env overrides ----------------------------------
    if os.environ.get("FROM_DATE"):
        raw.setdefault("target_window", {})["from_utc"] = os.environ["FROM_DATE"]
    if os.environ.get("TO_DATE"):
        raw.setdefault("target_window", {})["to_utc"] = os.environ["TO_DATE"]
    if os.environ.get("MIN_ROI_PCT"):
        try:
            raw.setdefault("thresholds", {})["min_roi_pct"] = float(os.environ["MIN_ROI_PCT"])
        except ValueError:
            pass
    if os.environ.get("TOURNAMENT_IDS"):
        ids = [int(x) for x in os.environ["TOURNAMENT_IDS"].replace(" ", "").split(",") if x]
        raw.setdefault("tournaments", {})["pinned_ids"] = ids

    secrets = Secrets(
        odds_papi_key=os.environ.get("ODDS_PAPI_KEY"),
        telegram_bot_key=os.environ.get("TELEGRAM_BOT_KEY"),
        telegram_group_id=os.environ.get("TELEGRAM_GROUP_ID"),
    )

    return Config(
        raw=raw,
        secrets=secrets,
        cache_dir=os.environ.get("CACHE_DIR", DEFAULT_CACHE_DIR),
        csv_path=os.environ.get("CSV_PATH", DEFAULT_CSV_PATH),
        dry_run=_truthy(os.environ.get("DRY_RUN")),
    )
