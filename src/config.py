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
DEFAULT_XLSX_PATH = os.path.join(REPO_ROOT, "data", "arbs_log.xlsx")

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
    xlsx_path: str = DEFAULT_XLSX_PATH
    dry_run: bool = False
    # LOCAL_RUN: the VM is the sole scanner. Telegram alerts stay ON (unlike dry_run, which suppresses
    # them); only the git add/commit/push of data/ is skipped — that is the runner script's job (see
    # scripts/run_scan.ps1), so Python just records the mode for the logs. Independent of dry_run.
    local_run: bool = False

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

    @property
    def oddspapi_fetch_odds(self) -> bool:
        """FREE PROFILE switch. When False the scan spends NO OddsPapi odds requests — per-scan odds
        come only from the supplemental feeds (the-odds-api / kalshi / polymarket). OddsPapi is still
        used for free for the budget guard and the cached catalog + name map. Defaults True (funded
        profile: one billable odds request per granted book per scan)."""
        return bool(self.get("oddspapi", "fetch_odds", default=True))

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
    def poly_fee_rate(self) -> float:
        """Polymarket sports taker-fee rate for the exact exchange-fee model (default 0.05)."""
        return float(self.get("bookmakers", "poly_fee_rate", default=0.05) or 0.05)

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

    @property
    def bankroll_total(self) -> float:
        """Hard cap on total money staked across all legs of one arb (0/absent => no cap)."""
        return float(self.get("bankroll_total", default=0) or 0)

    @property
    def assumed_unknown_limit(self) -> float:
        """Assumed executable stake ($) for any leg lacking a real reported limit (UNVERIFIED)."""
        return float(self.get("thresholds", "assumed_unknown_limit", default=1000) or 0)

    @property
    def assumed_unknown_limit_by_book(self) -> dict[str, float]:
        """Per-book overrides of assumed_unknown_limit (book slug -> assumed $ limit)."""
        raw = self.get("thresholds", "assumed_unknown_limit_by_book", default={}) or {}
        return {str(k): float(v) for k, v in raw.items()}

    @property
    def alert_max_days_out(self) -> float:
        """Only ALERT fixtures kicking off within this many days (0/absent => no window gate)."""
        return float(self.get("alert_max_days_out", default=0) or 0)

    @property
    def alert_min_profit(self) -> float:
        """Only ALERT arbs whose guaranteed profit (post-cap) >= this many dollars (0 => no floor)."""
        return float(self.get("alert_min_profit", default=0) or 0)

    @property
    def group_alert_min_profit(self) -> float:
        """Group-stage arbs' OWN profit floor (they're small but real, and lock capital ~10d)."""
        return float(self.get("group_alert_min_profit", default=15) or 0)

    @property
    def group_alert_min_roi_pct(self) -> float:
        """Group-stage arbs alert if profit >= group_alert_min_profit OR ROI >= this %."""
        return float(self.get("group_alert_min_roi_pct", default=1.0) or 0)

    @property
    def scan_lock_max_age_s(self) -> float:
        """A scan lock older than this (seconds) is treated as stale and overridden (crash recovery)."""
        return float(self.get("scan_lock_max_age_s", default=1800) or 1800)

    @property
    def player_props_enabled(self) -> bool:
        """STEP 5 player-goals tier (Polymarket -player-props <-> Kalshi KXWCGOAL). Default OFF."""
        return bool(self.get("player_props", "enabled", default=False))

    def player_props_opt(self, key: str, default: Any) -> Any:
        return self.get("player_props", key, default=default)

    @property
    def group_markets_enabled(self) -> bool:
        """STEP/group tier: cross-exchange group-stage outcome markets (winner/bottom). Default OFF."""
        return bool(self.get("group_markets", "enabled", default=False))

    def group_markets_opt(self, key: str, default: Any) -> Any:
        return self.get("group_markets", key, default=default)

    def threshold(self, key: str, default: Any) -> Any:
        return self.get("thresholds", key, default=default)

    def telegram_opt(self, key: str, default: Any) -> Any:
        return self.get("telegram", key, default=default)

    @property
    def heartbeat_enabled(self) -> bool:
        """Send a 'bot alive' ping when a scan finds zero real arbs (throttled)."""
        return bool(self.get("telegram", "heartbeat_enabled", default=True))

    @property
    def heartbeat_min_interval_min(self) -> float:
        """Minimum minutes between heartbeats. 0 => ping on every scan."""
        return float(self.get("telegram", "heartbeat_min_interval_min", default=30) or 0)

    @property
    def shadow_window_hours(self) -> float:
        """Rolling window (hours) over which the shadow-book scoreboard aggregates arbs."""
        return float(self.get("shadow_window_hours", default=48) or 48)

    @property
    def shadow_digest_hour(self) -> int:
        """Local hour (0-23) at/after which the once-daily shadow-book digest is sent."""
        return int(self.get("shadow_digest_hour", default=9))

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
        """Master switch. While False, NO the-odds-api leg may form an actionable arb (shadow only)."""
        return bool(self.get("theoddsapi", "actionable", default=False))

    @property
    def theoddsapi_actionable_books(self) -> set[str] | None:
        """Per-book allow-list within the-odds-api: only these recovered slugs may turn an arb
        actionable (the master switch `actionable` must also be on). None/absent => no per-book
        restriction. Mirrors the `allow_books` allow-list pattern in merge_into."""
        books = self.get("theoddsapi", "actionable_books", default=None)
        return set(books) if books else None

    # -- kalshi-direct supplemental feed ---------------------------------------
    def kalshi_opt(self, key: str, default: Any) -> Any:
        return self.get("kalshi", key, default=default)

    @property
    def kalshi_enabled(self) -> bool:
        return bool(self.get("kalshi", "enabled", default=False))

    @property
    def kalshi_actionable(self) -> bool:
        """Shadow gate. While False, any arb with a kalshi-direct leg is forced non-actionable."""
        return bool(self.get("kalshi", "actionable", default=False))

    # -- polymarket-direct supplemental feed -----------------------------------
    def polymarket_opt(self, key: str, default: Any) -> Any:
        return self.get("polymarket", key, default=default)

    @property
    def polymarket_enabled(self) -> bool:
        return bool(self.get("polymarket", "enabled", default=False))

    @property
    def polymarket_actionable(self) -> bool:
        """Shadow gate. While False, any arb with a polymarket-direct leg is forced non-actionable."""
        return bool(self.get("polymarket", "actionable", default=False))

    # -- OG MULTI-SPORT (src/og_multi/) — the 4-book MLB/tennis/UFC layer -------
    def og_multi_opt(self, key: str, default: Any) -> Any:
        """Read one key of the ``og_multi:`` block (mirrors ``theoddsapi_opt`` etc.)."""
        return self.get("og_multi", key, default=default)

    @property
    def og_multi_enabled_sports(self) -> list[str]:
        """Sports the multi-sport OG cycle scans (empty/absent => the cycle is a no-op)."""
        return list(self.get("og_multi", "enabled_sports", default=[]) or [])


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
        xlsx_path=os.environ.get("XLSX_PATH", DEFAULT_XLSX_PATH),
        dry_run=_truthy(os.environ.get("DRY_RUN")),
        local_run=_truthy(os.environ.get("LOCAL_RUN")),
    )
