"""the-odds-api adapter: sport-key resolution, capability learning (422 drop-retry-learn), tennis
discovery filter + per-day cache, and quota-watch WARN. Uses a fake client (no network)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from src.theoddsapi import TheOddsApiError
from src.og_multi import state, toa

NOW = datetime(2026, 7, 19, 23, 0, 0, tzinfo=timezone.utc)
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fix(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class _RecLog:
    def __init__(self):
        self.infos, self.warnings = [], []

    def info(self, msg, *a):
        self.infos.append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.warnings.append(msg % a if a else msg)

    def debug(self, *a, **k):
        pass


class FakeClient:
    """Mimics TheOddsApiClient: sports()/odds() + the x-requests-* counters (including x-requests-last,
    the per-request cost the adapter actually bills on). ``odds_map`` keys a frozenset of requested
    markets to either an events list OR the sentinel '422:<market>'."""
    def __init__(self, *, odds_map=None, sports_payload=None, used=1000, remaining=9000):
        self.requests_used, self.requests_remaining, self.requests_last = used, remaining, None
        self._odds_map = odds_map or {}
        self._sports = sports_payload or []
        self.sports_calls, self.odds_calls = 0, []

    def sports(self):
        self.sports_calls += 1          # /v4/sports is FREE -> x-requests-last = 0, used unchanged
        self.requests_last = 0
        return self._sports

    def odds(self, sport_key, regions, markets, odds_format="decimal"):
        self.odds_calls.append(markets)
        mk = markets.split(",")
        val = self._odds_map.get(frozenset(mk))
        if isinstance(val, str) and val.startswith("422:"):
            self.requests_last = 0      # an INVALID_MARKET 422 is rejected pre-pricing -> not billed
            bad = val[4:]
            raise TheOddsApiError(f"422 on /odds: INVALID_MARKET {bad}", status_code=422,
                                  body=json.dumps({"error_code": "INVALID_MARKET",
                                                   "message": f"The market {bad} is not available"}))
        self.requests_used += len(mk)   # bill (#markets x 1 region)
        self.requests_remaining -= len(mk)
        self.requests_last = len(mk)
        return val or []


# --- sport-key resolution --------------------------------------------------- #
def test_static_sport_key_no_discovery():
    c = FakeClient()
    keys, credits = toa.resolve_sport_keys("mlb", "baseball_mlb", c, NOW, _RecLog())
    assert keys == ["baseball_mlb"] and credits == 0 and c.sports_calls == 0


def test_tennis_auto_discovery_filters_active_tennis_only(tmp_path):
    b = str(tmp_path)
    c = FakeClient(sports_payload=_fix("toa_sports_list.json"))
    keys, _ = toa.resolve_sport_keys("tennis", "AUTO", c, NOW, _RecLog(), base=b)
    assert keys == ["tennis_atp_wimbledon", "tennis_wta_wimbledon"]   # active Tennis only (french_open inactive)
    assert c.sports_calls == 1
    # Same scan-day -> served from cache, no second /v4/sports call.
    keys2, _ = toa.resolve_sport_keys("tennis", "AUTO", c, NOW, _RecLog(), base=b)
    assert keys2 == keys and c.sports_calls == 1


# --- capability learning (the btts lesson, generalized) --------------------- #
def test_422_drop_retry_learn_same_cycle(tmp_path):
    b = str(tmp_path)
    log = _RecLog()
    c = FakeClient(odds_map={frozenset(["h2h", "totals"]): "422:totals",
                             frozenset(["h2h"]): _fix("toa_ufc.json")})
    fetch = toa.run_toa("ufc", client=c, region="eu", toa_sport_key="mma_mixed_martial_arts",
                        now=NOW, log=log, base=b)
    # It dropped totals and RETRIED with h2h in the SAME cycle -> still got data.
    assert c.odds_calls == ["h2h,totals", "h2h"]
    assert fetch.events and fetch.markets_served == ["h2h"]
    caps = state.load_capabilities(b)
    assert caps["mma_mixed_martial_arts"]["valid"] == ["h2h"]
    assert "totals" in caps["mma_mixed_martial_arts"]["dropped"]
    assert any("INVALID_MARKET" in w for w in log.warnings)


def test_dropped_market_skipped_for_a_week_then_reprobed():
    caps = {"mma_mixed_martial_arts": {"dropped": {"totals": state.iso_utc(NOW - timedelta(days=3))}}}
    # dropped 3 days ago -> still skipped
    assert toa.desired_markets("ufc", "mma_mixed_martial_arts", caps, NOW) == ["h2h"]
    caps["mma_mixed_martial_arts"]["dropped"]["totals"] = state.iso_utc(NOW - timedelta(days=8))
    # dropped 8 days ago -> the weekly re-probe is due
    assert toa.desired_markets("ufc", "mma_mixed_martial_arts", caps, NOW) == ["h2h", "totals"]


def test_h2h_never_dropped_even_if_marked():
    caps = {"baseball_mlb": {"dropped": {"h2h": state.iso_utc(NOW)}}}
    assert "h2h" in toa.desired_markets("mlb", "baseball_mlb", caps, NOW)


# --- happy path + quota ----------------------------------------------------- #
def test_run_toa_counts_credits_and_serves_markets(tmp_path):
    b = str(tmp_path)
    c = FakeClient(odds_map={frozenset(["h2h", "spreads", "totals"]): _fix("toa_mlb.json")})
    fetch = toa.run_toa("mlb", client=c, region="eu", toa_sport_key="baseball_mlb", now=NOW,
                        log=_RecLog(), base=b)
    assert len(fetch.events) == 1 and fetch.credits == 3        # 3 markets x 1 region
    assert fetch.markets_served == ["h2h", "spreads", "totals"] and fetch.daily_total == 3


def test_credits_from_x_requests_last_not_cumulative_delta():
    """REGRESSION: a fresh client reports its lifetime x-requests-used (e.g. 50890) on its FIRST
    response; per-cycle spend must come from x-requests-last (the per-request cost), never
    used-minus-zero — else one cycle is mis-billed the whole account lifetime (~50k credits)."""
    class _Fresh:                       # a real fresh client just after one paid odds() call
        requests_used, requests_remaining, requests_last = "50890", "49110", "3"
    assert toa._call_credits(_Fresh(), used_before=0) == 3       # x-requests-last wins, NOT 50890

    class _NoLast:                      # header absent (defensive) -> never explode
        requests_used, requests_remaining, requests_last = "50890", "49110", None
    assert toa._call_credits(_NoLast(), used_before=0) == 0       # no header + no baseline -> 0
    assert toa._call_credits(_NoLast(), used_before=50887) == 3   # real baseline -> honest delta


def test_free_sports_discovery_bills_zero(tmp_path):
    """/v4/sports carries x-requests-last: 0 -> discovery must never be counted as spend."""
    b = str(tmp_path)
    c = FakeClient(sports_payload=_fix("toa_sports_list.json"))
    _keys, billed = toa.discover_tennis_keys(c, NOW, _RecLog(), base=b)
    assert billed == 0


def test_quota_watch_warns_when_projected_month_high(tmp_path):
    b = str(tmp_path)
    state.record_credits(250, NOW, b)                          # already spent heavily today
    log = _RecLog()
    c = FakeClient(odds_map={frozenset(["h2h", "spreads", "totals"]): _fix("toa_mlb.json")},
                   used=1000, remaining=9000)                  # plan quota = 10000
    toa.run_toa("mlb", client=c, region="eu", toa_sport_key="baseball_mlb", now=NOW, log=log,
                warn_pct=60, base=b)
    # daily ~253 -> projected month ~7590 > 60% of 10000 -> WARN
    assert any("QUOTA WATCH" in w for w in log.warnings)
