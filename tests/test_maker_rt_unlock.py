"""Tests for the PRE-GAME intentional unlock: guarded client construction, the revised live caps
(quote_usd_max sizing + the new max_daily_stake_usd pre-place halt + fills/open caps), the smoke
gate-refusal, the startup stray-cancel, and the shutdown cancel-all. No network, no real orders."""
from __future__ import annotations

import asyncio

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt import live, smoke
from src.genz.maker_rt.caps import LiveCaps, direction_slot_ok
from src.genz.maker_rt.clients import build_pregame_order_clients


# --------------------------------------------------------------------------- #
# fakes                                                                          #
# --------------------------------------------------------------------------- #
class _OkClient:
    def get_balance(self):
        return {"balance": 1000}

    def can_place_polymarket_orders(self):
        return (True, "ok")


class _BadClient:
    def get_balance(self):
        raise RuntimeError("no balance")


class _FakePoly:
    def __init__(self, open_orders):
        self._open = open_orders
        self.cancelled = []
        self.cancel_all_called = 0

    def open_orders(self, *, market=None, token_id=None):
        return self._open

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"ok": True}

    def cancel_all(self):
        self.cancel_all_called += 1
        return {"ok": True}


class _FakeKalshi:
    def __init__(self, orders):
        self._orders = orders
        self.cancelled = []

    def get_orders(self, *, status=None, ticker=None):
        return {"orders": self._orders}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"ok": True}


def _cfg():
    return mrt_config.MakerRtConfig()


# --------------------------------------------------------------------------- #
# 1) clients ONLY when enabled                                                  #
# --------------------------------------------------------------------------- #
def test_clients_none_in_shadow():
    k, p = build_pregame_order_clients(_cfg())            # live.enabled default False
    assert k is None and p is None


def test_clients_built_when_enabled():
    cfg = _cfg()
    cfg.live.enabled = True
    k, p = build_pregame_order_clients(cfg)
    from src.executor.kalshi_exec import KalshiExec
    from src.executor.poly_exec import PolyExec
    assert isinstance(k, KalshiExec) and isinstance(p, PolyExec)


# --------------------------------------------------------------------------- #
# 2) config: revised caps load                                                  #
# --------------------------------------------------------------------------- #
def test_live_caps_defaults():
    lc = mrt_config.LiveConfig()
    assert lc.quote_usd_max == 5.0
    assert lc.max_daily_stake_usd == 100.0
    assert lc.max_fills_per_day == 10
    assert lc.max_open_quotes == 2 and lc.max_daily_loss_usd == 25.0


def test_live_caps_load_from_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("maker_rt:\n  live:\n    enabled: true\n    quote_usd_max: 7\n"
                 "    max_daily_stake_usd: 42\n    max_fills_per_day: 3\n", encoding="utf-8")
    cfg = mrt_config.load_maker_rt_config(str(p))
    assert cfg.live.quote_usd_max == 7.0
    assert cfg.live.max_daily_stake_usd == 42.0
    assert cfg.live.max_fills_per_day == 3


# --------------------------------------------------------------------------- #
# 3) caps: quote_usd_max sizing                                                 #
# --------------------------------------------------------------------------- #
def test_caps_sizing_respects_quote_usd_max():
    caps = LiveCaps(mrt_config.LiveConfig(quote_usd_max=5.0))
    assert caps.size_shares(0.30) == 5              # 5 shares -> $1.50
    assert caps.size_shares(0.99) == 5              # $4.95
    assert caps.size_shares(0.10) == 10             # venue ~$1 minimum
    for px in (0.02, 0.05, 0.2, 0.5, 0.9, 0.99):
        assert caps.size_shares(px) * px <= 5.0 + 1e-9      # never above the cap


# --------------------------------------------------------------------------- #
# 4) caps: max_daily_stake_usd pre-place check + halt                           #
# --------------------------------------------------------------------------- #
def test_caps_daily_stake_allows_within():
    caps = LiveCaps(mrt_config.LiveConfig(max_daily_stake_usd=100, max_open_quotes=99,
                                          max_fills_per_day=99))
    caps.commit_stake(80.0)
    ok, reason = caps.can_place(15.0)               # 80 + 15 = 95 <= 100
    assert ok is True and reason == "ok" and caps.halted is False


def test_caps_daily_stake_breach_halts_and_alerts():
    sent = []
    caps = LiveCaps(mrt_config.LiveConfig(max_daily_stake_usd=100, max_open_quotes=99,
                                          max_fills_per_day=99), telegram=sent.append)
    caps.commit_stake(96.0)
    ok, reason = caps.can_place(10.0)               # 96 + 10 = 106 > 100 -> refuse + HALT
    assert ok is False and reason == "max_daily_stake_usd"
    assert caps.halted is True and sent             # Telegram alerted on halt
    ok2, r2 = caps.can_place(0.0)                    # halted -> everything refused after
    assert ok2 is False and r2.startswith("halted")


# --------------------------------------------------------------------------- #
# 5) caps: open / fills / loss                                                  #
# --------------------------------------------------------------------------- #
def test_caps_open_and_fills_and_loss():
    caps = LiveCaps(mrt_config.LiveConfig(max_open_quotes=1, max_fills_per_day=2,
                                          max_daily_loss_usd=25, max_daily_stake_usd=10_000))
    caps.on_open()
    ok, r = caps.can_place(1.0)
    assert ok is False and r == "max_open_quotes"
    caps.on_close()
    caps.fills_today = 2
    ok, r = caps.can_place(1.0)
    assert ok is False and r == "max_fills_per_day"
    caps.fills_today = 0
    caps.on_loss(30.0)                              # pnl -30 <= -25 -> halt
    assert caps.halted is True
    ok, r = caps.can_place(1.0)
    assert ok is False and r.startswith("halted")


# --------------------------------------------------------------------------- #
# 5b) per-direction slot reservation (pure)                                     #
# --------------------------------------------------------------------------- #
def test_direction_slot_reservation_pure():
    dirs = {"rest-poly", "rest-kalshi"}
    M, R = 2, 1
    # OFF (reserve 0) -> always allowed regardless of holdings (today's behavior).
    assert direction_slot_ok("rest-kalshi", {"rest-kalshi": 1}, dirs, M, 0) is True
    # kalshi holds 1, poly holds 0: kalshi CANNOT take poly's reserved slot; poly CAN take its own.
    assert direction_slot_ok("rest-kalshi", {"rest-kalshi": 1}, dirs, M, R) is False
    assert direction_slot_ok("rest-poly", {"rest-kalshi": 1}, dirs, M, R) is True
    # symmetric: poly holds 1, kalshi holds 0.
    assert direction_slot_ok("rest-poly", {"rest-poly": 1}, dirs, M, R) is False
    assert direction_slot_ok("rest-kalshi", {"rest-poly": 1}, dirs, M, R) is True
    # both idle -> either may claim its own guaranteed slot.
    assert direction_slot_ok("rest-poly", {}, dirs, M, R) is True
    assert direction_slot_ok("rest-kalshi", {}, dirs, M, R) is True
    # SINGLE-direction config -> no 'other' to protect -> may use every slot even with reserve on.
    assert direction_slot_ok("rest-poly", {"rest-poly": 1}, {"rest-poly"}, M, R) is True


# --------------------------------------------------------------------------- #
# 6) gate matrix (with injected fakes) — arm needs all three; in-play refused    #
# --------------------------------------------------------------------------- #
def test_gate_arms_only_with_all_three(tmp_path):
    arm = tmp_path / "ARM_MAKER"
    cfg = _cfg(); cfg.live.enabled = True; cfg.live.arm_file = str(arm)
    # enabled but no arm file
    assert live.LiveGate(cfg, kalshi_client=_OkClient(), poly_client=_OkClient()).evaluate().armed is False
    arm.write_text("1")
    # arm file present but a failing self-check
    assert live.LiveGate(cfg, kalshi_client=_BadClient(), poly_client=_OkClient()).evaluate().armed is False
    # all three -> armed
    assert live.LiveGate(cfg, kalshi_client=_OkClient(), poly_client=_OkClient()).evaluate().armed is True


def test_inplay_refused_even_with_clients_and_pregame_armed(tmp_path):
    arm = tmp_path / "ARM_MAKER"
    arm.write_text("1")
    cfg = _cfg(); cfg.live.enabled = True; cfg.live.arm_file = str(arm)
    g = live.LiveGate(cfg, kalshi_client=_OkClient(), poly_client=_OkClient())
    assert g.evaluate().armed is True                       # pre-game armed
    assert g.evaluate_inplay().armed is False               # in-play STILL refused (enabled False)


# --------------------------------------------------------------------------- #
# 7) smoke refuses when the gate would not arm                                   #
# --------------------------------------------------------------------------- #
def test_smoke_refuses_when_gate_not_armed(tmp_path):
    cfg = _cfg()
    cfg.live.enabled = True
    cfg.live.arm_file = str(tmp_path / "ABSENT_ARM")        # gate refuses before any self-check/network
    rc = asyncio.run(smoke.run_smoke(cfg, log=None, hold_s=0.0))
    assert rc == 2


# --------------------------------------------------------------------------- #
# 8) startup stray-cancel (both venues)                                          #
# --------------------------------------------------------------------------- #
def test_startup_stray_cancel_both_venues():
    poly = _FakePoly([{"id": "p1"}, {"orderID": "p2"}])
    kalshi = _FakeKalshi([{"order_id": "k1", "client_order_id": "mrt-abc"},
                          {"order_id": "k2", "client_order_id": "someone-else"}])
    n = smoke._startup_stray_cancel(poly, kalshi, log=None)
    assert set(poly.cancelled) == {"p1", "p2"}              # every resting poly order is ours
    assert kalshi.cancelled == ["k1"]                       # only ours by client_order_id prefix
    assert n == 3


# --------------------------------------------------------------------------- #
# 9) shutdown cancel-all                                                         #
# --------------------------------------------------------------------------- #
def test_shutdown_cancel_all_fires():
    poly = _FakePoly([])
    n = smoke._cancel_tracked(poly, ["a", "b"], None, None)
    assert set(poly.cancelled) == {"a", "b"}
    assert poly.cancel_all_called == 1 and n == 2
