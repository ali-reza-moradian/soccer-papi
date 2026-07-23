"""Slot-starvation fixes (the "0 quotes, 3,042 slot-refuses suppressed, open 1" live digest):

  1. STALE RELEASE — a tracked order the venue no longer shows resting is released, freeing its slot
     (and the caps counter is resynced to ground truth).
  2. RESERVE NON-BLOCKING — a reserved slot never blocks another direction when the reserving
     direction has no viable candidate this cycle.
  3. AGE-OUT — a resting order older than max_quote_age_s is cancelled so no slot is held forever.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.genz.maker_rt.caps import LiveCaps, direction_slot_ok
from src.genz.maker_rt import config as mrt_config

from .test_maker_rt_pregame import (_KalshiExec, _KalshiOC, _Log, _OrderClient, _Poly, _State, _Store,
                                    _cand, _cand_kalshi, _dec, _exec, _exec_kalshi)

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. stale release + counter resync                                            #
# --------------------------------------------------------------------------- #
class _PolyGone(_Poly):
    """get_order returns {} — the venue no longer knows this order (cancelled + purged)."""
    def get_order(self, oid):
        return {}


def test_stale_order_is_released_and_frees_the_slot(tmp_path):
    poly = _PolyGone()
    oc = _OrderClient()
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly)
    ex.place_or_reprice(_cand(), _dec(0.46), None, _Store(poly_best_ask=0.60, poly_best_bid=0.45),
                        NOW, 1000.0, "pre")
    key = _cand().key
    assert key in ex.open_orders and ex.caps.open_quotes == 1
    # Past the placement grace, the venue shows nothing -> release the phantom slot.
    ex.poll_open_orders(_Store(), NOW, 1000.0 + ex._stale_grace_s + 1)
    assert key not in ex.open_orders, "a stale (venue-gone) order must be released"
    assert ex.caps.open_quotes == 0 and ex._slot_released == 1
    assert any(r["event"] == "slot_released" for r in ex.state.rows)


def test_fresh_gone_order_is_NOT_released_within_grace(tmp_path):
    """A just-placed order the venue hasn't indexed yet must survive the grace window."""
    ex, _ = _exec(tmp_path, order_client=_OrderClient(), poly=_PolyGone())
    ex.place_or_reprice(_cand(), _dec(0.46), None, _Store(poly_best_ask=0.60, poly_best_bid=0.45),
                        NOW, 1000.0, "pre")
    ex.poll_open_orders(_Store(), NOW, 1000.0 + 1)            # within the 5s grace
    assert _cand().key in ex.open_orders, "must not drop an order the venue hasn't indexed yet"


def test_counter_drift_is_resynced_to_tracked(tmp_path):
    ex, _ = _exec(tmp_path, order_client=_OrderClient())
    ex.log = _Log()
    ex.caps.open_quotes = 2                                   # drifted high with an empty book
    ex.poll_open_orders(_Store(), NOW, 2000.0)
    assert ex.caps.open_quotes == 0                          # resynced to len(open_orders)
    assert any("slot counter drift" in w for w in ex.log.warns)


# --------------------------------------------------------------------------- #
# 2. reserve non-blocking                                                      #
# --------------------------------------------------------------------------- #
def test_direction_slot_ok_pure_reserve_blocks_when_other_viable():
    # max_open=2, reserve=1: rest-kalshi holds 1, rest-poly holds 0 and IS viable -> kalshi refused a 2nd.
    assert direction_slot_ok("rest-kalshi", {"rest-kalshi": 1}, {"rest-poly", "rest-kalshi"}, 2, 1) is False


def test_direction_slot_ok_pure_reserve_frees_when_other_absent():
    # if only rest-kalshi is in the enabled/viable set, its reserve is the only one -> 2nd slot allowed.
    assert direction_slot_ok("rest-kalshi", {"rest-kalshi": 1}, {"rest-kalshi"}, 2, 1) is True


def test_idle_direction_reserve_does_not_block_active_one(tmp_path):
    """THE deadlock: rest-poly holds a slot and wants the 2nd; the reserve protects rest-kalshi's slot.
    When rest-kalshi has NO viable candidate this cycle, its reserve must not block rest-poly."""
    cfg = mrt_config.MakerRtConfig()
    cfg.live.enabled = True
    cfg.live.max_open_quotes = 2
    caps = LiveCaps(cfg.live)
    ex, _ = _exec(tmp_path, caps=caps)
    ex.directions = {"rest-poly", "rest-kalshi"}
    ex.reserve_per_direction = 1
    # one rest-poly order already open (holds a slot); rest-poly wants the 2nd
    ex.place_or_reprice(_cand("rest-poly", "A"), _dec(0.46), None,
                        _Store(poly_best_ask=0.60, poly_best_bid=0.45), NOW, 1.0, "pre")
    assert ex.open_count() == 1
    # BOTH viable -> rest-poly refused the 2nd (rest-kalshi's reserve is protected).
    ex.set_viable_directions({"rest-poly", "rest-kalshi"})
    assert ex._reservation_ok("rest-poly") is False
    # rest-kalshi IDLE this cycle -> its reserve must NOT block rest-poly's 2nd slot.
    ex.set_viable_directions({"rest-poly"})
    assert ex._reservation_ok("rest-poly") is True


# --------------------------------------------------------------------------- #
# 3. age-out                                                                   #
# --------------------------------------------------------------------------- #
def test_age_out_cancels_a_too_old_resting_order(tmp_path):
    oc = _OrderClient()
    # get_order returns a RESTING order (dict, not empty, no fill) so only the age-out path can fire.
    poly = _Poly(order_status="resting", size_matched=0.0)
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly)
    ex.max_quote_age_s = 900.0
    ex.place_or_reprice(_cand(), _dec(0.46), None, _Store(poly_best_ask=0.60, poly_best_bid=0.45),
                        NOW, 1000.0, "pre")
    key = _cand().key
    ex.poll_open_orders(_Store(), NOW, 1000.0 + 800)         # not yet aged out
    assert key in ex.open_orders
    ex.poll_open_orders(_Store(), NOW, 1000.0 + 901)         # past the cap -> cancelled, slot freed
    assert key not in ex.open_orders and ex._aged_out == 1
    assert oc.cancels, "the aged-out order must be cancelled at the venue"


def test_slot_wait_gauge_tracks_and_prunes(tmp_path):
    ex, _ = _exec(tmp_path, order_client=_OrderClient())
    ex.log = _Log()
    # a candidate refused for a slot starts waiting
    ex._slot_wait_since[("k",)] = [1000.0, 1005.0]
    ex.sample_slot_wait(1010.0)
    assert ex.slot_wait_max_s == 10.0
    # not refused for >30s -> pruned (no longer waiting)
    ex.sample_slot_wait(1040.0)
    assert ex.slot_wait_max_s == 0.0 and ("k",) not in ex._slot_wait_since


def test_snapshot_exposes_slot_health(tmp_path):
    ex, _ = _exec(tmp_path, order_client=_OrderClient())
    snap = ex.snapshot(1000.0)
    for k in ("max_open", "slot_wait_max_s", "slot_released", "aged_out", "max_quote_age_s"):
        assert k in snap
