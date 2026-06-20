"""Group-stage outcome tier: SAFE/DANGEROUS settlement split (qualify can NEVER be actionable),
capital-lockup, per-venue parsing, and the cross-exchange Yes/No arb build."""
from __future__ import annotations

from datetime import datetime, timezone

from src import group_markets as gm
from src.config import Config, Secrets
from src.logsetup import get_logger

NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _cfg():
    return Config(raw={"bankroll_total": 30000,
                       "thresholds": {"min_roi_pct": 0.5, "min_total_stake": 20,
                                      "roi_suspicious_pct": 8.0, "assumed_unknown_limit": 1000,
                                      "low_confidence_limit_floor": 10}},
                  secrets=Secrets(None, None, None))


def _type(name):
    return next(t for t in gm.MARKET_TYPES if t.name == name)


# --------------------------------------------------------------------------- #
# SAFE / DANGEROUS settlement split — the core safety invariant                 #
# --------------------------------------------------------------------------- #
def test_settlement_classes_registered():
    assert _type("group_winner").settlement_class == gm.SAFE
    assert _type("group_bottom").settlement_class == gm.SAFE
    assert _type("group_qualify").settlement_class == gm.DANGEROUS


def test_qualify_can_never_be_actionable():
    """A qualify/advance pair is DANGEROUS -> forced non-actionable EVEN when the operator turned the
    group-markets actionable flag ON (fail-closed; resolution definitions differ across venues)."""
    assert gm.actionable_for(_type("group_qualify"), True) is False
    assert gm.actionable_for(_type("group_qualify"), False) is False
    # SAFE types follow the config flag.
    assert gm.actionable_for(_type("group_winner"), True) is True
    assert gm.actionable_for(_type("group_winner"), False) is False
    assert gm.actionable_for(_type("group_bottom"), True) is True


def test_capital_lockup_days():
    assert gm.capital_lockup_days(NOW, "2026-06-27T23:59:59Z") == 8
    assert gm.capital_lockup_days(NOW, "2026-06-19T00:00:00Z") == 0   # already past -> 0, never negative


# --------------------------------------------------------------------------- #
# Per-venue parsing                                                             #
# --------------------------------------------------------------------------- #
def _kmkt(letter, team, yes_ask, no_ask, status="active"):
    return {"event_ticker": f"KXWCGROUPWIN-26{letter}", "yes_sub_title": team, "status": status,
            "yes_ask_dollars": yes_ask, "no_ask_dollars": no_ask,
            "yes_ask_size_fp": "1000", "no_ask_size_fp": "1000"}


def test_parse_kalshi_group_by_letter_and_team():
    g = gm.parse_kalshi_group([_kmkt("A", "South Africa", "0.30", "0.72"),
                               _kmkt("A", "Mexico", "0.45", "0.57"),
                               _kmkt("B", "Canada", "0.40", "0.62")])
    assert set(g) == {"a", "b"}
    assert set(g["a"]) == {"south africa", "mexico"}
    assert round(g["a"]["south africa"]["yes"][0], 3) == round(1 / 0.30, 3)


def _pmkt(team, yes_tok, no_tok):
    return {"groupItemTitle": team, "outcomes": '["Yes", "No"]',
            "clobTokenIds": f'["{yes_tok}", "{no_tok}"]'}


def test_parse_poly_group_drops_tail_buckets():
    ev = {"markets": [_pmkt("South Africa", "sa_y", "sa_n"), _pmkt("Mexico", "mx_y", "mx_n"),
                      _pmkt("Other", "o_y", "o_n"), _pmkt("Country E", "e_y", "e_n")]}
    t = gm.parse_poly_group_tokens(ev)
    assert set(t) == {"south africa", "mexico"}        # 'Other'/'Country E' dropped
    assert t["south africa"]["yes_token"] == "sa_y"


# --------------------------------------------------------------------------- #
# Cross-exchange Yes/No arb build                                               #
# --------------------------------------------------------------------------- #
def test_build_group_arb_2way_yes_no():
    # 'wins' best 2.2 (kalshi yes) + 'not wins' best 2.0 (poly no): S = 1/2.2 + 1/2.0 = 0.954 -> arb.
    built = gm.build_group_arb("a", _type("group_winner"), "South Africa",
                               kalshi_yes=(2.20, 1000), kalshi_no=(1.90, 1000),
                               poly_yes=(2.10, 1000), poly_no=(2.00, 1000), cfg=_cfg(), commission={})
    assert built is not None
    spec, res = built
    assert res.is_arb and spec.family == "group_winner" and spec.period == "group"


def test_build_group_arb_none_when_no_arb():
    assert gm.build_group_arb("a", _type("group_winner"), "X", (1.5, 1000), (1.5, 1000),
                              (1.5, 1000), (1.5, 1000), cfg=_cfg(), commission={}) is None


# --------------------------------------------------------------------------- #
# Slug/token map cache (the group-tier speed fix)                              #
# --------------------------------------------------------------------------- #
def _cfg_cache(tmp):
    return Config(raw={"group_markets": {"slugmap_cache_hours": 6, "poly_tag_id": "102232"}},
                  secrets=Secrets(None, None, None), cache_dir=str(tmp))


class _RaisingPC:
    """Any network call is a test failure — used to prove a cache HIT touches no network."""
    def _get(self, *a, **k):
        raise AssertionError("network called on cache hit")

    def events_by_slug(self, *a, **k):
        raise AssertionError("events_by_slug called on cache hit")


def test_slug_map_cache_hit_skips_network(tmp_path):
    import time
    from src import catalog
    cached = {"saved_at": time.time(),
              "by_type": {"group_winner": {"per_group": {"a": {"mexico": {"raw": "Mexico",
                          "yes_token": "y", "no_token": "n"}}}, "global": {}}}}
    catalog.save_json(str(tmp_path), gm.SLUGMAP_CACHE_FILE, cached)
    tm = gm._poly_token_map(_cfg_cache(tmp_path), _RaisingPC(), time.time(), get_logger("t"))
    assert tm["group_winner"]["per_group"]["a"]["mexico"]["yes_token"] == "y"


class _BuildPC:
    """Cache MISS: serves one page of slugs then events_by_slug payloads."""
    def __init__(self, slugs, events):
        self.slugs, self.events = slugs, events

    def _get(self, url, params):
        return [{"slug": s} for s in self.slugs] if params.get("offset", 0) == 0 else []

    def events_by_slug(self, slug):
        return self.events.get(slug)


def test_slug_map_cache_miss_builds_and_writes(tmp_path):
    import time
    from src import catalog
    ev = {"markets": [{"groupItemTitle": "Mexico", "outcomes": '["Yes","No"]',
                       "clobTokenIds": '["mx_y","mx_n"]'}]}
    pc = _BuildPC(["world-cup-group-a-winner"], {"world-cup-group-a-winner": ev})
    tm = gm._poly_token_map(_cfg_cache(tmp_path), pc, time.time(), get_logger("t"))
    assert tm["group_winner"]["per_group"]["a"]["mexico"]["yes_token"] == "mx_y"
    assert catalog.load_json(str(tmp_path), gm.SLUGMAP_CACHE_FILE)["by_type"]   # cache written
