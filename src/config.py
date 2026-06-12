"""Load config.yaml + environment secrets / workflow-dispatch overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")
DEFAULT_CACHE_DIR = os.path.join(REPO_ROOT, "data", "cache")
DEFAULT_CSV_PATH = os.path.join(REPO_ROOT, "data", "arbitrage_opportunities.csv")

# Tournament-level futures resolve weeks out (well past the 2-day scan window) and are out of scope.
# Used when config.yaml omits markets.exclude_future_names. Substrings are matched case-insensitively
# against marketName; chosen to catch real futures without touching per-match markets (a bare
# "winner" is deliberately NOT here — "Match Winner"/"1X2" is a per-match market we DO scan).
_DEFAULT_FUTURE_NAMES = [
    "outright", "to qualify", "to advance", "to reach", "reach the final", "finalist",
    "golden boot", "golden ball", "golden glove", "top goalscorer", "top scorer",
    "tournament winner", "group winner", "winner of group", "to win group", "to win the group",
    "to win their group", "group betting", "stage of elimination", "to be eliminated",
    "to win the tournament", "to win outright", "to lift", "to win the world cup", "champion",
]


def _truthy(val: str | None) -> bool:
    return str(val).strip().lower() in {"1", "true", "yes", "on"} if val is not None else False


def _rolling_window(now: datetime) -> tuple[str, str]:
    """Rolling UTC scan window: ``from`` == now, ``to`` == end of the calendar day two days out.

    All arithmetic is UTC. A naive ``now`` is treated as UTC (no local-timezone leak). ``to`` is
    (UTC today + 2 days) at 23:59:59Z, so a run any time on day D covers D, D+1 and D+2. Month and
    year rollovers fall out of ``timedelta`` plus a date/time recombination — e.g. Jun 29 -> Jul 1,
    Dec 30 -> Jan 1 of the next year.
    """
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    end_date = now.date() + timedelta(days=2)
    to_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Secrets:
    odds_papi_key: str | None
    telegram_bot_key: str | None
    telegram_group_id: str | None
    odds_api_key: str | None = None       # the-odds-api.com (supplemental feed)

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

    # NOTE: NO hardcoded date fallback. load_config() resolves the scan window — a rolling 2-day
    # range from the current UTC instant, or a FROM_DATE/TO_DATE workflow_dispatch override — and
    # writes it onto target_window before these are read. If somehow unset they return None rather
    # than leaking a stale literal date: a missing window is a bug to surface, not a date to invent.
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
    def exclude_future_names(self) -> list[str]:
        """Substrings flagging out-of-scope tournament-level futures (settle past the scan window)."""
        return [str(x).lower() for x in
                (self.get("markets", "exclude_future_names", default=_DEFAULT_FUTURE_NAMES) or [])]

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

    # -- the-odds-api supplemental feed ----------------------------------------
    def theoddsapi_opt(self, key: str, default: Any) -> Any:
        return self.get("theoddsapi", key, default=default)

    @property
    def theoddsapi_enabled(self) -> bool:
        return bool(self.get("theoddsapi", "enabled", default=False))

    @property
    def theoddsapi_actionable(self) -> bool:
        """While False, NO the-odds-api leg may form an actionable arb (shadow/tracked only)."""
        return bool(self.get("theoddsapi", "actionable", default=False))


def load_config(config_path: str | None = None) -> Config:
    """Load config.yaml, layer in env-based workflow inputs, and read secrets."""
    path = config_path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # ---- scan window: rolling 2-day default, workflow_dispatch overrides -----
    # When a dispatch date is empty, default to the rolling window computed from the current UTC
    # instant: from = now, to = end of (UTC today + 2 days) at 23:59:59Z. An explicit FROM_DATE /
    # TO_DATE (or a target_window pinned in YAML) still wins, per-field. No literal dates live here.
    roll_from, roll_to = _rolling_window(datetime.now(timezone.utc))
    tw = raw.setdefault("target_window", {})
    tw["from_utc"] = os.environ["FROM_DATE"] if os.environ.get("FROM_DATE") else (tw.get("from_utc") or roll_from)
    tw["to_utc"] = os.environ["TO_DATE"] if os.environ.get("TO_DATE") else (tw.get("to_utc") or roll_to)

    # ---- other workflow_dispatch / env overrides ----------------------------
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
        odds_api_key=os.environ.get("ODDS_API_KEY"),
    )

    return Config(
        raw=raw,
        secrets=secrets,
        cache_dir=os.environ.get("CACHE_DIR", DEFAULT_CACHE_DIR),
        csv_path=os.environ.get("CSV_PATH", DEFAULT_CSV_PATH),
        dry_run=_truthy(os.environ.get("DRY_RUN")),
    )
