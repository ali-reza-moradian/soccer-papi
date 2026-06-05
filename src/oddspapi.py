"""OddsPapi v4 HTTP client.

Facts baked in (verified against https://oddspapi.io/us/docs):
  * Base URL: https://api.oddspapi.io
  * Auth: apiKey query parameter on every call (no header).
  * Billing: most /v4/* endpoints cost exactly 1 request each. /v4/account and
    /v4/historical-odds are free. 429 => REQUEST_LIMIT_EXCEEDED, stop immediately.
  * Cooldowns: 1000ms most endpoints, 2000ms for /v4/fixtures.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

BASE_URL = "https://api.oddspapi.io"

# Per-endpoint cooldown in seconds.
_COOLDOWNS = {
    "/v4/fixtures": 2.0,
}
_DEFAULT_COOLDOWN = 1.0


class QuotaExceeded(Exception):
    """Raised on HTTP 429 / REQUEST_LIMIT_EXCEEDED. Never retried — stop the run."""


class OddsPapiError(Exception):
    """Non-recoverable API error (bad request, auth, etc.)."""


class OddsPapiClient:
    def __init__(
        self,
        api_key: str,
        logger: logging.Logger | None = None,
        throttle: bool = True,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise OddsPapiError("ODDS_PAPI_KEY is empty — cannot call the API.")
        self.api_key = api_key
        self.log = logger or logging.getLogger("arb")
        self.throttle = throttle
        self.timeout = timeout
        self.session = session or requests.Session()
        self._last_call_at = 0.0
        self.billable_count = 0  # billable requests this process actually consumed

    # -- internals -------------------------------------------------------------
    def _cooldown_for(self, path: str) -> float:
        return _COOLDOWNS.get(path, _DEFAULT_COOLDOWN)

    def _sleep_if_needed(self, path: str) -> None:
        if not self.throttle:
            return
        wait = self._cooldown_for(path) - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _request(self, path: str, params: dict[str, Any]) -> requests.Response:
        self._sleep_if_needed(path)
        full = {**{k: v for k, v in params.items() if v is not None}, "apiKey": self.api_key}
        try:
            resp = self.session.get(BASE_URL + path, params=full, timeout=self.timeout)
        finally:
            self._last_call_at = time.monotonic()
        return resp

    def _get(self, path: str, params: dict[str, Any] | None = None, *, billable: bool = True) -> Any:
        resp = self._request(path, params or {})

        if resp.status_code == 429:
            # Quota already gone. This request was rejected before doing work and is NOT counted.
            raise QuotaExceeded(f"429 REQUEST_LIMIT_EXCEEDED on {path}")

        # A request that reaches the endpoint counts (1 request) regardless of 2xx/4xx.
        if billable:
            self.billable_count += 1

        if resp.status_code >= 400:
            body = resp.text[:300]
            raise OddsPapiError(f"{resp.status_code} on {path}: {body}")

        try:
            return resp.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise OddsPapiError(f"Non-JSON response on {path}: {resp.text[:200]}") from exc

    # -- endpoints -------------------------------------------------------------
    def account(self) -> dict[str, Any]:
        """Free, never-blocked endpoint. Returns the active subscription's budget."""
        data = self._get("/v4/account", billable=False)
        subs = data.get("subscriptions") or []
        active = next(
            (s for s in subs if s.get("subscription_id") == data.get("current_subscription_id")),
            None,
        )
        if active is None:
            active = next((s for s in subs if s.get("is_active")), None)
        if active is None and subs:
            active = subs[0]
        active = active or {}

        limit = active.get("request_limit")
        count = active.get("request_count")
        remaining = (limit - count) if (limit is not None and count is not None) else None
        # The subscription's `bookmakers` map tells us which books this plan can query.
        grant = active.get("bookmakers")
        granted_books = sorted(grant.keys()) if isinstance(grant, dict) else None
        return {
            "limit": limit,
            "count": count,
            "remaining": remaining,
            "bookmakers": granted_books,   # None if the plan doesn't enumerate them
            "subscription_id": active.get("subscription_id"),
            "valid_until": active.get("valid_until"),
        }

    def sports(self) -> Any:
        return self._get("/v4/sports")

    def bookmakers(self) -> Any:
        return self._get("/v4/bookmakers")

    def markets(self, language: str = "en") -> Any:
        return self._get("/v4/markets", {"language": language})

    def tournaments(self, sport_id: int, language: str = "en") -> Any:
        return self._get("/v4/tournaments", {"sportId": sport_id, "language": language})

    def fixtures(
        self,
        sport_id: int,
        tournament_id: int | None = None,
        from_utc: str | None = None,
        to_utc: str | None = None,
        status_id: int | None = None,
        has_odds: bool | None = None,
        language: str = "en",
    ) -> Any:
        params: dict[str, Any] = {"sportId": sport_id, "language": language}
        if tournament_id is not None:
            params["tournamentId"] = tournament_id
        if from_utc:
            params["from"] = from_utc
        if to_utc:
            params["to"] = to_utc
        if status_id is not None:
            params["statusId"] = status_id
        if has_odds is not None:
            params["hasOdds"] = str(has_odds).lower()
        return self._get("/v4/fixtures", params)

    def odds_by_tournaments(
        self,
        tournament_ids: list[int] | str,
        bookmaker: str | None = None,
        verbosity: int = 3,
        odds_format: str = "decimal",
        language: str = "en",
    ) -> Any:
        """Odds for all events in the given leagues, for ONE bookmaker, in one billable call.

        NOTE: although the public docs say `bookmakers` is optional (omit = all books), the
        live free/standard subscription rejects that with 400 INVALID_PARAMETER and requires
        EXACTLY ONE book via the singular `bookmaker` query param. So cross-book arbitrage
        costs one request per book — the caller loops over the books it wants (budget-capped).
        """
        ids = tournament_ids if isinstance(tournament_ids, str) else ",".join(str(i) for i in tournament_ids)
        params: dict[str, Any] = {
            "tournamentIds": ids,
            "verbosity": verbosity,
            "oddsFormat": odds_format,
            "language": language,
        }
        if bookmaker:
            params["bookmaker"] = bookmaker
        return self._get("/v4/odds-by-tournaments", params)

    def odds(self, fixture_id: str, verbosity: int = 3, odds_format: str = "decimal", language: str = "en") -> Any:
        """Single-fixture odds (all books) — fallback for deeper markets on one fixture."""
        return self._get(
            "/v4/odds",
            {"fixtureId": fixture_id, "verbosity": verbosity, "oddsFormat": odds_format, "language": language},
        )


def check_budget(client: OddsPapiClient, safety_margin: int, log: logging.Logger) -> dict[str, Any]:
    """Pre-flight budget guard. Returns the account dict and whether it is safe to spend."""
    acct = client.account()
    remaining = acct.get("remaining")
    if remaining is None:
        log.warning("Could not read request budget from /v4/account; proceeding cautiously.")
        acct["safe_to_run"] = True
        return acct

    log.info("BUDGET: %s/%s used (remaining %s, safety margin %s)",
             acct.get("count"), acct.get("limit"), remaining, safety_margin)
    acct["safe_to_run"] = remaining > safety_margin
    return acct
