"""Free-profile tests: config switches (oddspapi.fetch_odds, LOCAL_RUN) and the TTL-based
catalog auto-refresh in run._ensure_catalogs (the blocker root-cause fix)."""
from __future__ import annotations

import os

import pytest

from src import catalog
from src.config import Config, Secrets, load_config
from src.logsetup import get_logger
from src.run import _ensure_catalogs

NOW_EPOCH = 1_780_000_000.0


# --------------------------------------------------------------------------- #
# Config switches                                                              #
# --------------------------------------------------------------------------- #
def test_oddspapi_fetch_odds_defaults_true():
    """Funded profile by default — absent config means OddsPapi odds are still fetched."""
    assert Config(raw={}, secrets=Secrets(None, None, None)).oddspapi_fetch_odds is True


def test_oddspapi_fetch_odds_false_in_free_profile():
    cfg = Config(raw={"oddspapi": {"fetch_odds": False}}, secrets=Secrets(None, None, None))
    assert cfg.oddspapi_fetch_odds is False


def test_local_run_parsed_from_env(monkeypatch):
    monkeypatch.setenv("LOCAL_RUN", "1")
    monkeypatch.setenv("ODDS_PAPI_KEY", "k")
    assert load_config().local_run is True
    monkeypatch.setenv("LOCAL_RUN", "0")
    assert load_config().local_run is False


def test_local_run_independent_of_dry_run(monkeypatch):
    """LOCAL_RUN must NOT imply DRY_RUN — Telegram stays on in local mode."""
    monkeypatch.setenv("LOCAL_RUN", "1")
    monkeypatch.delenv("DRY_RUN", raising=False)
    cfg = load_config()
    assert cfg.local_run is True and cfg.dry_run is False


# --------------------------------------------------------------------------- #
# Catalog TTL auto-refresh                                                     #
# --------------------------------------------------------------------------- #
def _write_catalogs(cache_dir, age_hours):
    """Write the three catalog files and back-date their mtime to `age_hours` before NOW_EPOCH."""
    for name in (catalog.MARKETS_FILE, catalog.BOOKMAKERS_FILE, catalog.TOURNAMENTS_FILE):
        catalog.save_json(cache_dir, name, [{"x": 1}])
        mtime = NOW_EPOCH - age_hours * 3600.0
        os.utime(os.path.join(cache_dir, name), (mtime, mtime))


def _cfg(cache_dir, catalog_ttl=336):
    raw = {"budget": {"catalog_cache_hours": catalog_ttl, "refresh_min_remaining": 24}, "sport_id": 10}
    return Config(raw=raw, secrets=Secrets("k", None, None), cache_dir=str(cache_dir))


class _SpyCatalog:
    """Records refresh_catalogs calls and rewrites fresh catalog files when invoked."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.calls = 0

    def refresh(self, client, cache_dir, sport_id, log):
        self.calls += 1
        _write_catalogs(cache_dir, age_hours=0)
        return {"markets": 1, "bookmakers": 1, "markets_": 1, "tournaments": 1}


def _patch_refresh(monkeypatch, cache_dir):
    spy = _SpyCatalog(cache_dir)
    monkeypatch.setattr(catalog, "refresh_catalogs", spy.refresh)
    return spy


def test_fresh_catalog_is_not_refreshed(monkeypatch, tmp_path):
    _write_catalogs(tmp_path, age_hours=1)            # well within the 336h TTL
    spy = _patch_refresh(monkeypatch, tmp_path)
    acct = {"remaining": 200}
    cats = _ensure_catalogs(object(), _cfg(tmp_path), acct, NOW_EPOCH, get_logger("t"))
    assert cats and cats["markets"] and spy.calls == 0  # served from cache, no billable refresh


def test_stale_catalog_triggers_refresh(monkeypatch, tmp_path):
    _write_catalogs(tmp_path, age_hours=400)          # older than the 336h TTL
    spy = _patch_refresh(monkeypatch, tmp_path)
    acct = {"remaining": 200}
    cats = _ensure_catalogs(object(), _cfg(tmp_path), acct, NOW_EPOCH, get_logger("t"))
    assert spy.calls == 1 and cats and cats["markets"]


def test_missing_catalog_triggers_refresh(monkeypatch, tmp_path):
    spy = _patch_refresh(monkeypatch, tmp_path)        # no files written -> missing
    acct = {"remaining": 200}
    cats = _ensure_catalogs(object(), _cfg(tmp_path), acct, NOW_EPOCH, get_logger("t"))
    assert spy.calls == 1 and cats and cats["markets"]


def test_stale_catalog_kept_when_budget_low(monkeypatch, tmp_path):
    """A stale-but-present catalog is reused (not refreshed, not discarded) when the budget is low."""
    _write_catalogs(tmp_path, age_hours=400)
    spy = _patch_refresh(monkeypatch, tmp_path)
    acct = {"remaining": 10}                            # below refresh_min_remaining (24)
    cats = _ensure_catalogs(object(), _cfg(tmp_path), acct, NOW_EPOCH, get_logger("t"))
    assert spy.calls == 0 and cats and cats["markets"]  # kept stale cache, no refresh


def test_missing_catalog_with_low_budget_is_fatal(monkeypatch, tmp_path):
    spy = _patch_refresh(monkeypatch, tmp_path)
    acct = {"remaining": 10}
    cats = _ensure_catalogs(object(), _cfg(tmp_path), acct, NOW_EPOCH, get_logger("t"))
    assert spy.calls == 0 and cats is None             # missing + no budget -> cannot run


# --------------------------------------------------------------------------- #
# run_cycle gating: the free profile spends ZERO OddsPapi odds requests        #
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Stand-in OddsPapiClient: free /v4/account, and a fetch spy that must never fire here."""

    def __init__(self, *a, **k):
        self.billable_count = 0

    def account(self):
        return {"limit": 250, "count": 50, "remaining": 200, "bookmakers": ["pinnacle", "1xbet"],
                "subscription_id": "s", "valid_until": None}


def test_free_profile_skips_oddspapi_odds_fetch(monkeypatch, tmp_path):
    import src.run as run

    fetched = {"called": False}

    def _spy_fetch(*a, **k):
        fetched["called"] = True
        return {}, [], []

    # Fresh names cache so no names refresh is attempted (would be a billable OddsPapi call).
    catalog.save_json(str(tmp_path), catalog.NAMES_FILE, {"by_fixture": {}, "by_participant": {}})

    monkeypatch.setattr(run, "OddsPapiClient", _FakeClient)
    monkeypatch.setattr(run, "_ensure_catalogs",
                        lambda *a, **k: {"markets": [], "bookmakers": [], "tournaments": []})
    monkeypatch.setattr(run, "_resolve_tournaments", lambda *a, **k: ([16], []))
    monkeypatch.setattr(run, "_fetch_odds_per_book", _spy_fetch)
    # Supplemental feeds are exercised live in verification; stub them out for this offline unit test.
    monkeypatch.setattr(run, "_merge_theoddsapi", lambda *a, **k: {})
    monkeypatch.setattr(run, "_merge_kalshi", lambda *a, **k: {})
    monkeypatch.setattr(run, "_merge_polymarket", lambda *a, **k: {})

    raw = {
        "oddspapi": {"fetch_odds": False},
        "budget": {"safety_margin": 15, "names_cache_hours": 12, "catalog_cache_hours": 336},
        "target_window": {"from_utc": "2026-06-16T00:00:00Z", "to_utc": "2026-06-18T23:59:59Z"},
        "bookmakers": {"actionable": ["pinnacle", "1xbet"], "tracked": ["pinnacle", "1xbet"]},
    }
    cfg = Config(raw=raw, secrets=Secrets("k", None, None), cache_dir=str(tmp_path), dry_run=True)
    rc = run.run_cycle(cfg, get_logger("t"))
    assert rc == 0
    assert fetched["called"] is False                  # free profile: NEVER fetch OddsPapi odds
