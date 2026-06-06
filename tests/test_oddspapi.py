"""Client-level tests: a dead/forbidden key (429/403) raises QuotaExceeded, not a traceback."""
from __future__ import annotations

import json

import pytest

from src.oddspapi import OddsPapiClient, OddsPapiError, QuotaExceeded


class _Resp:
    def __init__(self, status_code: int, text: str = "{}") -> None:
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class _Session:
    """Minimal stand-in for requests.Session that always returns one canned response."""

    def __init__(self, status_code: int, text: str = "{}") -> None:
        self._resp = _Resp(status_code, text)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self._resp


def _client(status_code: int, text: str = "{}") -> OddsPapiClient:
    return OddsPapiClient("key", throttle=False, session=_Session(status_code, text))


def test_429_raises_quota_exceeded():
    client = _client(429, "REQUEST_LIMIT_EXCEEDED")
    with pytest.raises(QuotaExceeded):
        client.sports()
    assert client.billable_count == 0  # rejected before doing work -> not counted


def test_403_raises_quota_exceeded():
    """A forbidden/exhausted key (403) is treated like a quota hit, not a crash."""
    client = _client(403, "Forbidden")
    with pytest.raises(QuotaExceeded):
        client.bookmakers()
    assert client.billable_count == 0


def test_400_is_skippable_error_not_key_exhaustion():
    """A book outside the plan returns 400 INVALID_PARAMETER — a per-call error the caller
    skips, NOT a whole-run key-exhaustion abort."""
    client = _client(400, "INVALID_PARAMETER")
    with pytest.raises(OddsPapiError):
        client.odds_by_tournaments([17], bookmaker="somebook")
    assert client.billable_count == 1  # reached the endpoint -> still counts


def test_success_returns_json_and_counts():
    client = _client(200, '{"ok": true}')
    assert client.sports() == {"ok": True}
    assert client.billable_count == 1
