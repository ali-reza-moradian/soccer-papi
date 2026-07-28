"""CONTINUOUS LIVE executor — rest-POLY only, BOTH phases (pre-game + in-play).

Owns the real Polymarket resting orders for armed rest-poly candidates end-to-end. ONE instance
governs PRE-GAME and IN-PLAY with a SHARED budget (one :class:`LiveCaps`) and ONE global in-flight
hedge guard, so pre+inplay exposure is a single pool.

  * place / reprice / cancel — driven by the QuoteDriver's quote lifecycle. A REPRICE is
    cancel -> CONFIRM cancelled -> place (never exceeds ``max_open_quotes`` mid-transition); the
    never-crossable rule is RE-CHECKED against the live book immediately before every POST. IN-PLAY
    placement is additionally gated by the driver's anti-phantom rails + a cool-off (``cooloff_ok``):
    the node must be unfrozen AND both books fresh for >= ``freeze_cooloff_s`` before any in-play POST,
    and any freeze/stale on a node with a live resting order CANCELS it (not just disarms shadow).
  * fill -> hedge (identical both phases) — a fill is detected off the Poly USER socket (size_matched
    delta) AND a throttled REST backup. The hedge is RE-VERIFIED on the live Kalshi book; below the
    -1% decline floor we DECLINE + market-unwind, else lift the Kalshi IOC hedge sized to the match.
    ONE in-flight hedge GLOBALLY across pre+inplay.
  * caps — SHARED ``LiveCaps`` (quote_usd_max sizing; pre-place projected-pair-stake vs
    max_daily_stake_usd -> refuse+HALT+Telegram; max_open_quotes / max_fills_per_day / max_daily_loss).
  * in-play FIRST-FILL soft circuit — after the day's FIRST in-play fill, pause in-play placement for
    ``first_fill_pause_s`` and Telegram the full fill->hedge->locked_net chain (pre-game unaffected);
    resume automatically. Any in-play fill with locked_net <= ``halt_locked_net`` (-2%) HALTS in-play
    for the day (pre-game continues).
  * user-feed-down safety — the Poly USER socket dropping HALTS placement + cancels open quotes.

Every live action -> events CSV (mode 'live') + Telegram.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Optional

from . import alerts
from . import config as mrt_config
from . import hedge as hedge_mod
from . import settle as settle_mod
from .caps import direction_slot_ok
from .live import assert_live_allowed
from .quotes import POLY_MIN_SHARES

_UNREAD = object()               # sentinel: "_venue_order_state was not handed a pre-read order dict"
HEDGE_DECLINE_FLOOR = -0.010     # re-verify at fill: walked locked-net below this -> decline+unwind (both phases)
HEDGE_SHARE_TOL = 0.5            # share tolerance for "fully hedged"/"flat" position reads (< 1 contract)
UNWIND_MAX_ATTEMPTS = 3         # reconcile-to-flat: re-sell the naked remainder up to this many times
# SMALLEST SIZE EACH VENUE WILL PRICE. Below it there is no order we are able to place, so a residue that
# small is not an orphan — it is un-closable dust that must ride to settlement (and gets booked there at
# venue truth). Kalshi orders carry whole contracts (fmt_count/clamp_count); Poly's floor is 5 shares.
MIN_TRADABLE = {"kalshi": 1.0, "polymarket": float(POLY_MIN_SHARES)}
# A full-slot refusal (max_open_quotes / reserve_per_direction) recurs EVERY debounce loop while the caps
# are full — the driver retries each viable candidate ~4x/s. Log a given node+direction's slot refusal at
# most once per this window; the suppressed count is surfaced in the Telegram digest line instead.
REFUSE_LOG_EVERY_S = 300.0
_SLOT_REFUSE_REASONS = ("max_open_quotes", "reserve_per_direction")


def _epoch(now: Any) -> float:
    """Unix seconds from a datetime-ish ``now`` (0.0 when it cannot be read) — audit metadata only."""
    try:
        return float(now.timestamp())
    except Exception:  # noqa: BLE001
        return 0.0


@dataclass
class _LiveOrder:
    key: tuple
    order_id: str
    token: str
    price: float
    size: float
    side: str
    direction: str
    sport: str
    game: str
    market_key: str
    hedge_lookup: dict
    poly_rate: float
    placed_ts: float
    phase: str = "pre"
    best_bid: Optional[float] = None
    matched_seen: float = 0.0
    rest_venue: str = "polymarket"     # "polymarket" | "kalshi" — dispatches place/cancel/fill/unwind/verify
    kalshi_side: str = ""              # YES|NO when rest_venue == "kalshi" (rest_ref[2])
    teams: str = ""                   # 'AWAY vs HOME' — for the human alert name
    client_order_id: str = ""          # our coid (Kalshi mrt-*) — re-resolves the order in the resting list


class PregameLiveExecutor:
    """Unified live executor for pre-game AND in-play rest-poly (the ``pregame_exec`` on the driver)."""

    def __init__(self, cfg: Any, gate: Any, order_client: Any, hedger: Any, caps: Any, poly: Any, *,
                 in_flight: Any = None, telegram: Any = None, state: Any = None, log: Any = None,
                 kalshi_order_client: Any = None, kalshi: Any = None) -> None:
        self.cfg = cfg
        self.gate = gate
        self.order_client = order_client        # PolyOrderClient (rest_poly rest/cancel)
        self.kalshi_order_client = kalshi_order_client   # KalshiOrderClient (rest_kalshi rest/cancel/status)
        self.hedger = hedger                     # LiveHedger (venue-aware: kalshi IOC | poly FAK)
        self.caps = caps                         # LiveCaps (SHARED across directions + phases)
        self.poly = poly                         # PolyExec (REST get_order / neg_risk / conditional_balance)
        self.kalshi = kalshi                     # KalshiExec (REST order status / positions / unwind sell)
        self.in_flight = in_flight               # ONE global in-flight guard (all directions + phases)
        self.telegram = telegram
        self.state = state
        self.log = log
        self.open_orders: dict = {}              # candidate key -> _LiveOrder
        self.feed_ok: bool = False               # Poly USER socket health (starts DOWN until it connects)
        self.kalshi_feed_ok: bool = False        # Kalshi WS health (rest_kalshi fill signal; DOWN until up)
        # LIVE-eligible rest directions (normalised, e.g. {"rest-poly","rest-kalshi"}). rest_kalshi stays
        # OFF until its SMOKE-KALSHI passes and config maker_rt.directions adds it.
        self.directions = {str(d).replace("_", "-") for d in getattr(cfg, "directions", ("rest_poly",))}
        # PER-DIRECTION SLOT RESERVATION: guarantee each enabled direction this many of max_open_quotes so
        # one direction (e.g. rest-kalshi) can't monopolize every slot. 0 = off / single-direction no-op.
        self.reserve_per_direction = int(getattr(cfg, "reserve_per_direction", 0))
        # A reserve is only PROTECTED for a direction that currently HAS a viable candidate. The driver
        # refreshes this each cycle; the default (all enabled) preserves the old behaviour until it does.
        # This is the fix for the slot deadlock: rest-poly's reserved slot must not block rest-kalshi from
        # the physically-free slot when rest-poly has nothing to place.
        self._viable_directions: set = set(self.directions)
        # SLOT AGE-OUT: a resting order this old is repriced-or-cancelled so no order holds a slot forever
        # (the live digest showed one order held a slot for 46 min, behind best, while 3,042 candidates
        # were refused). The driver reprices if still viable; otherwise the age-out cancel frees the slot.
        self.max_quote_age_s = float(getattr(getattr(cfg, "live", None), "max_quote_age_s", 900.0))
        self._stale_grace_s = 5.0                # don't release a just-placed order the venue hasn't indexed
        self._slot_released = 0                  # tracked orders released as stale (venue no longer resting)
        self._aged_out = 0                       # tracked orders cancelled by the age-out
        self._implausible_refused = 0            # quotes rejected by the sanity ceiling (probable pricing bug)
        self._slot_wait_since: dict = {}         # candidate key -> ts it FIRST got a slot refusal (reset on place)
        self.slot_wait_max_s = 0.0               # longest any candidate is currently waiting for a slot (panel)
        self._refuse_log_at: dict = {}           # (candidate key, reason) -> last log ts (throttles slot-refusal spam)
        self._neg_cache: dict = {}               # token -> neg_risk (fetched once)
        self._traded_tickers: set = set()        # kalshi tickers we've rested on -> reconcile for flatness
        # IN-PLAY first-fill circuit (per UTC day; replaces the calendar guard).
        li = getattr(cfg, "live_inplay", None)
        self.inplay_first_fill_pause_s = float(getattr(li, "first_fill_pause_s", 120.0))
        self.inplay_halt_locked_net = float(getattr(li, "halt_locked_net", -0.020))
        self.inplay_cooloff_s = float(getattr(li, "freeze_cooloff_s", 10.0))
        self.inplay_fills_today = 0
        self.inplay_pause_until = 0.0
        self.inplay_halted = False
        self._day = ""
        # REPRICE HYSTERESIS + connection-freshness knobs.
        self.reprice_min_ticks = int(getattr(cfg, "reprice_min_ticks", 2))
        self.min_rest_s = float(getattr(cfg, "min_rest_s", 20.0))
        ip = getattr(cfg, "inplay", None)
        self.conn_fresh_s = float(getattr(ip, "conn_fresh_s", 30.0))
        self.node_quiet_max_s = float(getattr(ip, "node_quiet_max_s", 180.0))
        # TELEGRAM digest (routine quote/reprice/cancel collapse; fills/hedges/pause/halt/errors instant).
        self.digest_min = float(getattr(cfg, "telegram_digest_min", 15.0))
        self._digest = {"quotes": 0, "cancels": {}, "fills": 0, "refuse_suppressed": 0, "best_edge": 0.0,
                        "kalshi_flaps": 0, "kalshi_down_s": 0.0, "poly_flaps": 0, "poly_down_s": 0.0,
                        "prehedge_declines": 0, "binding": {}}
        self._digest_since = 0.0
        self._feed_health: dict = {}     # feed name -> (reconnect_attempts, reconnect_success)
        # BINDING-CONSTRAINT diagnostic: which limit set each quote's SIZE (quote_usd_max | pair_cap |
        # hedge_depth | book_depth | venue_minimum). Cumulative counts drive the panel so we can tell
        # whether raising caps would grow fills or whether depth / the pilot minimum is the ceiling — a
        # $2.80 fill against a $20 cap is the minimum-size floor at work, not the cap.
        self._binding_counts: dict = {}
        # LIFETIME metrics (live quotes only): closed-quote lifetimes + an at-best time sampler.
        self._lifetimes: list = []
        self._atbest_hits = 0
        self._atbest_samples = 0
        # POSITION INTEGRITY: an ORPHAN (unwind not confirmed flat / reconciliation mismatch) latches a
        # full halt until manual clearance. auto_flatten (config) decides whether reconciliation re-sells.
        self.orphan: Optional[dict] = None
        self._traded_tokens: set = set()             # tokens we've placed on -> reconcile these for flatness
        self.auto_flatten = bool(getattr(getattr(cfg, "live", None), "auto_flatten", False))
        # BOUNDED AUTO-FLATTEN ($): an orphan whose worst case (full notional) is at or under this is swept
        # out and quoting RESUMES; anything larger halts for a human exactly as before. This is what ends
        # the three-hour-halt-over-eleven-cents pattern. 0 disables it (always halt).
        self.auto_flatten_max_usd = float(getattr(getattr(cfg, "live", None),
                                                  "auto_flatten_max_usd", 0.0) or 0.0)
        # PROVISIONAL MARKS: instrument -> the worst-case number we booked for a position that is still
        # OPEN. Every one is rebooked at venue truth by settle_provisional_marks once it closes/settles.
        self._provisional: dict = {}
        self._provisional_path = mrt_config.runtime_path("provisional_marks")
        # Persist the watch-set so a token left non-flat by a CRASHED prior run is re-checked by the
        # startup reconcile. The in-memory set alone dies with the process, and a blanket
        # list_positions() sweep is unusable on this funder wallet (hundreds of unrelated positions ->
        # every one a false orphan; see reconcile_positions).
        # BOTH paths come from config.runtime_path(): these used to be derived by hand from the arm
        # file's directory, which was a SECOND path derivation that silently bypassed OPS_DIR — exactly
        # the kind of one-off join that made isolating test writes a matter of remembering.
        self._traded_path = mrt_config.runtime_path("traded_tokens")
        # ORPHAN durability: the halt banner is written to disk so it survives a restart AND is visible to
        # the panel even when Telegram is unreachable (Telegram 429'd 1,682x in the incident log — an
        # alert channel is NEVER the detection channel).
        self._orphan_path = mrt_config.runtime_path("orphan")
        # WS-INDEPENDENT FILL AUTHORITY: REST is the primary detector, the socket is an accelerator.
        self.fill_poll_s = float(getattr(getattr(cfg, "live", None), "fill_poll_s", 10.0))
        self._force_fill_poll = False        # set by a cancel that failed because the order FILLED
        self._last_fills_sweep_ts = 0.0      # unix-seconds low-water mark for the /portfolio/fills sweep
        self._seen_fill_ids: set = set()     # fill_id dedupe across sweeps + the socket
        # PLACE-FAILURE BACKOFF: a venue refusal (esp. "market closed") must not retry ~1x/s forever.
        self._place_fail_until: dict = {}    # candidate key -> ts before which we won't retry placement
        self._place_fail_n: dict = {}        # candidate key -> consecutive refusals (alert once, then log)
        self.place_backoff_s = 60.0
        self.place_backoff_terminal_s = 86400.0             # closed/settled market -> done for the day
        self.flaps = {"kalshi": 0, "poly_user": 0}          # reconnect counters (panel)
        self.flap_down_since: dict = {}                     # venue -> ts of the current DOWN edge
        self.flap_secs = {"kalshi": 0.0, "poly_user": 0.0}  # cumulative seconds spent DOWN
        # KALSHI WS FLAP DEBOUNCE: the socket drops `connected` on EVERY reconnect (incl. the quiet-market
        # ping probe). REST is the WS-INDEPENDENT fill authority, so a brief blip is NOT a fill-signal
        # outage — cancelling rest-kalshi quotes on it just shreds queue position. Only declare the feed
        # DOWN (cancel) after it has been continuously down >= this grace; a reconnect inside it is a no-op.
        self.kalshi_feed_grace_s = float(getattr(getattr(cfg, "live", None), "kalshi_feed_grace_s", 20.0))
        self._kalshi_down_since: Optional[float] = None     # ts the Kalshi socket first dropped (None = up)
        self._kalshi_declared_down = False                  # True once the grace elapsed and we cancelled
        # SETTLED-P&L: per-market COST BASIS of every hedged pair we booked (rest leg + hedge leg), keyed
        # by (game, market_key). The reconciler nets this against BOTH venues' settlement to write the
        # venue-truth realized pnl once the market settles. Persisted so a settlement landing after a
        # restart (games settle hours after the fill; the maker restarts ~10x/day) is still reconciled.
        self._market_legs: dict = {}
        self._settled_path = mrt_config.runtime_path("settled_ledger")
        # EXPECTED POSITIONS: every leg we hold on PURPOSE — a filled rest leg AND its live hedge — is an
        # EXPECTED venue position until its market SETTLES. Registered the moment a pair LOCKS, persisted
        # across restarts (a settlement lands hours after the fill, and the maker restarts ~10x/day), and
        # pruned when the market settles. Reconciliation compares venue positions against expected = open
        # rest legs + live hedges and only screams for a GENUINELY unexplained holding. Without this, the
        # account fill-sweep saw our own Kalshi HEDGE as an "UNTRACKED venue fill on a NON-FLAT position"
        # and false-halted (3rd occurrence 2026-07-24, HANHAL). Keyed by (venue, instrument).
        self._expected: dict = {}
        self._expected_path = mrt_config.runtime_path("expected_positions")
        from .settle import SettledPnlReconciler
        self._settle_reconciler = SettledPnlReconciler(
            kalshi=self.kalshi, poly=self.poly,
            record=(self.state.record if self.state is not None else None), log=self.log,
            max_pair_stake_usd=float(getattr(self.caps, "max_pair_stake_usd", 100.0)))
        # DAILY CAPS PERSISTENCE: LiveCaps' per-day counters (stake_today / fills_today / pnl_today) are
        # in-memory and reset to 0 on every process start, so a mid-day deploy silently RESET the daily
        # budget (12:08Z restart wiped the day's stake to $0). Persist them (keyed by UTC day) and restore
        # them at startup so a restart can't reopen a spent daily cap. LiveCaps stays PURE — the executor
        # (which already persists orphan/expected/traded state) owns this I/O.
        self._daily_caps_path = mrt_config.runtime_path("daily_caps")
        self._daily_caps_saved_ts = 0.0
        self._load_daily_caps()
        self._load_traded_tokens()
        self._load_settled_ledger()
        self._load_expected_positions()
        self._load_provisional()
        self._load_orphan()

    # -- daily roll ----------------------------------------------------------
    def roll_day(self, now: Any) -> None:
        """Reset the per-day in-play circuit AND the shared caps' daily counters at UTC midnight.

        The reset is announced LOUDLY on purpose. A silent $155.60 -> $0.00 at 00:00Z is indistinguishable
        in the heartbeat from the persistence bug this system already had once, and on 2026-07-28 it was
        read as exactly that (it was not — no restart was involved, see ``_load_daily_caps``). Saying
        'new UTC day' at the moment of the reset is what makes the two cases tellable apart."""
        day = now.strftime("%Y%m%d")
        if day == self._day:
            return
        # STARTUP PRIMING vs a REAL rollover. ``self._day`` is "" until the first tick, so the first call
        # is only priming — it must stay silent and, critically, must not look like a reset: on a restart
        # ``_load_daily_caps`` has already restored today's counters and pinned ``caps._day``, which makes
        # the caps.roll() below a no-op. Announcing here would invent the very false alarm this is for.
        priming = not self._day
        rolling = getattr(self.caps, "_day", None) != day        # True only when caps really will reset
        prior_stake = float(getattr(self.caps, "stake_today", 0.0) or 0.0)
        prior_fills = int(getattr(self.caps, "fills_today", 0) or 0)
        prior_pnl = float(getattr(self.caps, "pnl_today", 0.0) or 0.0)
        if not priming and rolling:
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] NEW UTC DAY %s -> %s — daily counters reset BY SCHEDULE "
                                 "(not a restart): stake $%.2f -> $0.00, fills %d -> 0, pnl $%.2f -> $0.00.",
                                 self._day, day, prior_stake, prior_fills, prior_pnl)
            if prior_stake or prior_fills:
                self._send_telegram(
                    f"🕛 New day (00:00 UTC) · yesterday finished with {prior_fills} fills, "
                    f"{alerts.money(prior_stake)} staked, {alerts.signed_money(prior_pnl)}. "
                    "Daily counters now start from zero — this is the scheduled reset, not a restart.")
        self._day = day
        self.inplay_fills_today = 0
        self.inplay_pause_until = 0.0
        self.inplay_halted = False
        self._lifetimes = []
        self._atbest_hits = 0
        self._atbest_samples = 0
        self._place_fail_until = {}          # a new day reopens markets -> clear placement backoffs
        self._place_fail_n = {}
        if hasattr(self.caps, "roll"):
            self.caps.roll(day)
        self.persist_daily_caps()                    # a new UTC day -> persist the reset counters

    # -- daily-caps persistence (survives a mid-day restart) -----------------
    def _load_daily_caps(self) -> None:
        """Restore today's committed stake / fills / pnl at startup so a restart can't reopen a spent
        daily cap. Only a SAME-UTC-DAY snapshot is restored; a stale (prior-day) file is ignored. Sets
        ``caps._day`` to the restored day so the first roll_day does NOT wipe the restored counters."""
        import json
        from .state import utcnow
        if not self._daily_caps_path or not os.path.exists(self._daily_caps_path):
            return
        try:
            with open(self._daily_caps_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            today = utcnow().strftime("%Y%m%d")
            if not isinstance(data, dict) or str(data.get("day")) != today:
                return                                # prior day -> let the normal midnight roll reset it
            self.caps.stake_today = float(data.get("stake_today", 0.0) or 0.0)
            self.caps.fills_today = int(data.get("fills_today", 0) or 0)
            self.caps.pnl_today = float(data.get("pnl_today", 0.0) or 0.0)
            self.caps._day = today                    # so caps.roll(today) is a no-op (don't wipe on restart)
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] restored today's daily caps across restart: "
                                 "stake $%.2f, fills %d, pnl $%.2f (cap $%.0f).", self.caps.stake_today,
                                 self.caps.fills_today, self.caps.pnl_today, self.caps.max_daily_stake_usd)
        except Exception:  # noqa: BLE001 — a corrupt/locked file must never block startup
            pass

    def persist_daily_caps(self) -> None:
        """Atomically write today's caps counters (best-effort — never blocks trading)."""
        import json
        from .state import utcnow
        if not self._daily_caps_path:
            return
        try:
            mrt_config.assert_writable(self._daily_caps_path)
            tmp = self._daily_caps_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"day": utcnow().strftime("%Y%m%d"),
                           "stake_today": round(float(self.caps.stake_today), 4),
                           "fills_today": int(self.caps.fills_today),
                           "pnl_today": round(float(self.caps.pnl_today), 4)}, fh)
            os.replace(tmp, self._daily_caps_path)
        except Exception:  # noqa: BLE001
            pass

    def maybe_persist_daily_caps(self, now_ts: float) -> None:
        """Throttled daily-caps persist for the heartbeat loop (fills persist immediately)."""
        if now_ts - self._daily_caps_saved_ts < 30.0:
            return
        self._daily_caps_saved_ts = now_ts
        self.persist_daily_caps()

    # -- gate / eligibility --------------------------------------------------
    @staticmethod
    def _armed(block: Any) -> bool:
        """CHEAP arm check for a gate block: enabled + on-disk arm file. The expensive startup self-check
        already passed (this executor is only built when a gate armed at startup)."""
        if not getattr(block, "enabled", False):
            return False
        arm = getattr(block, "arm_file", "")
        return bool(arm and os.path.exists(arm))

    def pre_armed(self) -> bool:
        return self._armed(getattr(self.cfg, "live", None))

    def inplay_armed(self) -> bool:
        return self._armed(getattr(self.cfg, "live_inplay", None))

    def eligible(self, c: Any, phase: str, now_ts: float = 0.0) -> bool:
        """Live-eligible iff the direction is enabled (config maker_rt.directions), its FILL feed is UP,
        caps not halted, AND the phase's own gate is armed. rest-poly needs the Poly USER socket; rest-kalshi
        needs the Kalshi WS (its fill channel). In-play additionally requires: not in-play-halted AND not
        inside the first-fill pause. (The driver enforces the freeze/stale/persistence/cool-off rails first.)"""
        if c.direction not in self.directions or self.caps.halted:
            return False
        feed_ok = self.feed_ok if c.direction == "rest-poly" else self.kalshi_feed_ok
        if not feed_ok:
            return False
        if phase == "pre":
            return self.pre_armed()
        if phase == "inplay":
            return bool(self.inplay_armed() and not self.inplay_halted and now_ts >= self.inplay_pause_until)
        return False

    def cooloff_ok(self, store: Any, c: Any, freeze_until_ts: float, now_ts: float) -> bool:
        """IN-PLAY cool-off: node NOT frozen now, thawed >= ``freeze_cooloff_s`` ago, AND both books
        CONNECTION-fresh (a quiet book on a healthy socket is fresh). Stricter than the shadow rail."""
        if now_ts < float(freeze_until_ts or 0.0):
            return False
        if freeze_until_ts and (now_ts - float(freeze_until_ts)) < self.inplay_cooloff_s:
            return False
        return bool(store.node_fresh(c.rest_id, now_ts, self.conn_fresh_s, self.node_quiet_max_s)
                    and store.node_fresh(c.hedge_id, now_ts, self.conn_fresh_s, self.node_quiet_max_s))

    # -- venue dispatch (rest_poly = Polymarket | rest_kalshi = Kalshi) ------
    def _rest_tick(self, c: Any, store: Any) -> float:
        return 0.01 if c.rest_venue == "kalshi" else (store.poly_tick(c.rest_ref[1], 0.01) or 0.01)

    def _rest_view_c(self, c: Any, store: Any):
        """Live rest-book SideView for a CANDIDATE (venue-dispatched)."""
        if c.rest_venue == "kalshi":
            return store.kalshi_view(c.rest_ref[1], c.rest_ref[2])
        return store.poly_view(c.rest_ref[1])

    def _rest_view_lo(self, lo: _LiveOrder, store: Any):
        """Live rest-book SideView for an OPEN ORDER (venue-dispatched)."""
        if lo.rest_venue == "kalshi":
            return store.kalshi_view(lo.token, lo.kalshi_side)
        return store.poly_view(lo.token)

    def _do_rest(self, c: Any, price: float, size: float, tick: float, store: Any) -> dict:
        """Post the resting maker order on the candidate's REST venue. Returns the normalized result."""
        if c.rest_venue == "kalshi":
            return self.kalshi_order_client.rest(c.rest_ref[1], c.rest_ref[2], price, size)
        neg = self._neg_for(c.rest_ref[1], store)
        return self.order_client.rest(c.rest_ref[1], price, size, tick_size=tick, neg_risk=neg)

    def _venue_get_order(self, lo: _LiveOrder) -> Any:
        """Venue-dispatched single-order status read (for cancel-confirm + REST fill poll)."""
        if lo.rest_venue == "kalshi":
            return self.kalshi_order_client.order_status(lo.order_id)
        return self.poly.get_order(lo.order_id)

    @staticmethod
    def _order_matched(lo: _LiveOrder, o: dict) -> Any:
        """Matched quantity from a venue order dict (Poly: size_matched; Kalshi: fill_count or
        initial-remaining). Kalshi v2 suffixes every count with ``_fp`` and sends it as a fixed-point
        STRING, so this MUST go through fp_num — reading only the bare v1 names is what made 6,126
        consecutive REST polls report "no fill" on two fully-executed orders on 2026-07-23."""
        if lo.rest_venue == "kalshi":
            from ...executor.kalshi_exec import fp_num
            n = fp_num(o, "fill_count", "taker_fill_count", "filled_count", "count_filled")
            if n is not None:
                return n
            init = fp_num(o, "initial_count", "count")
            rem = fp_num(o, "remaining_count")
            if init is not None and rem is not None:
                return init - rem
            # Terminal-but-countless: an 'executed' order with no readable count is FULLY filled.
            if str(o.get("status") or "").lower() == "executed":
                return float(lo.size)
            return None
        return o.get("size_matched")

    def _venue_cancel(self, lo: _LiveOrder) -> Any:
        if lo.rest_venue == "kalshi":
            return self.kalshi_order_client.cancel(lo.order_id)
        return self.order_client.cancel(lo.order_id)

    def _track_rested(self, lo: _LiveOrder) -> None:
        """Add the rested instrument to the maker-scoped reconcile watch-set (persisted for restarts)."""
        if lo.rest_venue == "kalshi":
            if lo.token not in self._traded_tickers:
                self._traded_tickers.add(lo.token)
                self._persist_traded_tokens()
        elif lo.token not in self._traded_tokens:
            self._traded_tokens.add(lo.token)
            self._persist_traded_tokens()

    # -- placement -----------------------------------------------------------
    def place_or_reprice(self, c: Any, dec: Any, rest: Any, store: Any, now: Any, now_ts: float,
                         phase: str = "pre") -> None:
        """Place (or reprice) the real GTC rest for ``c`` at ``dec.quote_price`` in ``phase``. Re-checks
        never-crossable on the LIVE book, enforces the shared caps, reprices via cancel->confirm->place."""
        armed = self.pre_armed() if phase == "pre" else self.inplay_armed()
        assert_live_allowed(phase, armed)             # HARD (cheap) re-lock immediately before any order
        if now_ts < self._place_fail_until.get(c.key, 0.0):   # venue refused recently -> back off
            return
        token = c.rest_ref[1]
        price = float(dec.quote_price)
        tick = self._rest_tick(c, store)
        live_rest = self._rest_view_c(c, store) or rest
        best_ask = getattr(live_rest, "best_ask", None)
        if best_ask is not None and price > best_ask - tick + 1e-9:    # would CROSS now -> never place
            if c.key in self.open_orders:
                self._cancel(c.key, now, "would_cross_at_post")
            return
        if price < tick - 1e-9:
            if c.key in self.open_orders:
                self._cancel(c.key, now, "below_tick")
            return
        existing = self.open_orders.get(c.key)
        if existing is not None:
            cur = existing.price
            floor = getattr(dec, "floor", None)
            crosses = best_ask is not None and cur > best_ask - tick + 1e-9
            below_floor = floor is not None and cur > floor + 1e-9      # resting ABOVE floor -> nets < target
            if crosses or below_floor:
                # MANDATORY reprice: the resting price is now UNECONOMIC (crosses the live book / under
                # target). Cancel + replace immediately.
                if not self._cancel(c.key, now, "reprice_cross" if crosses else "reprice_floor"):
                    return
            else:
                if abs(cur - price) < tick - 1e-9:
                    return                                             # same price -> keep resting
                # VOLUNTARY upward reprice ONLY: >= reprice_min_ticks better (or no-longer-at-best) AND the
                # order has rested >= min_rest_s. Otherwise KEEP RESTING to preserve queue position.
                rested = (now_ts - existing.placed_ts) >= self.min_rest_s
                higher = price >= cur + self.reprice_min_ticks * tick - 1e-9
                rest_bid = getattr(live_rest, "best_bid", None)
                no_longer_best = rest_bid is not None and rest_bid > cur + 1e-9
                if not (rested and (higher or no_longer_best)):
                    return
                if not self._cancel(c.key, now, "reprice"):            # cancel not confirmed -> do NOT dup-place
                    return
        hedge_ask = dec.hedge_best_ask if dec.hedge_best_ask is not None else price
        hedge_depth, book_depth = self._depths_for(c, hedge_ask, store, live_rest)
        vmin = self._venue_minimum(c.rest_venue, price)
        # SIZE = the LARGEST whole-share count that fits every constraint (notional/pair/daily caps +
        # hedge/book depth). The venue minimum is a FLOOR: if even the minimum doesn't fit, REFUSE — never
        # clamp DOWN to the minimum (that bug pinned every quote at 5 shares against a $20 cap).
        from .caps import plan_size
        plan = plan_size(price, hedge_ask, quote_usd_max=self.caps.quote_usd_max,
                         max_pair_stake_usd=self.caps.max_pair_stake_usd,
                         daily_stake_headroom=self.caps.max_daily_stake_usd - self.caps.stake_today,
                         hedge_depth=hedge_depth, book_depth=book_depth, venue_minimum=vmin)
        if plan["refused"]:
            self._note_binding_count("below_venue_minimum")
            self._refuse(c, phase, price, "below_venue_minimum", 0.0, now_ts)
            self._record(c, "expire", now, phase, price=price, size=vmin, hedge_ask=hedge_ask,
                         reason="below_venue_minimum")
            if self.log:
                self.log.info("[MAKER_RT][LIVE] REFUSE %s [%s] @ %.4f — max fittable %d sh < venue min %d "
                              "(limiter %s; hedgeDepth=%s bookDepth=%s)", self._name_for_c(c), phase, price,
                              plan["max_fit"], vmin, plan["limiter"],
                              (int(hedge_depth) if hedge_depth is not None else "n/a"),
                              (int(book_depth) if book_depth is not None else "n/a"))
            return
        size = plan["size"]
        projected = self.caps.projected_pair_stake(price, size, hedge_ask, size)
        ok, reason = self.caps.can_place(projected)
        if ok and not self._reservation_ok(c.direction):     # per-direction slot fairness (global cap passed)
            ok, reason = False, "reserve_per_direction"
        if not ok:
            self._refuse(c, phase, price, reason, projected, now_ts)
            self._record(c, "expire", now, phase, price=price, size=size, hedge_ask=hedge_ask, reason=reason)
            return
        # PRE-PLACEMENT STACK GUARD (belt): a NEW order (no tracked order for this key) must never rest on
        # a market that already carries an untracked order of ours (a ghost a failed cancel left live).
        # A reprice already cancel-confirmed its own order, so it is exempt.
        if existing is None and not self._stack_guard_ok(c, now, now_ts):
            self._record(c, "expire", now, phase, price=price, size=size, hedge_ask=hedge_ask,
                         reason="stack_guard_untracked_resting")
            return
        try:
            res = self._do_rest(c, price, size, tick, store)
        except Exception as exc:  # noqa: BLE001
            self._on_place_failed(c, phase, price, exc, now_ts)
            return
        oid = res.get("order_id")
        if not oid:
            if self.log:                                 # raw response is LOG-ONLY (never Telegram)
                self.log.warning("[MAKER_RT][LIVE] place returned no order_id (%s): %s", c.game, res)
            return
        lo = _LiveOrder(key=c.key, order_id=oid, token=token, price=price, size=float(size),
                        side=c.rest_side, direction=c.direction, sport=c.sport, game=c.game,
                        market_key=c.market_key, hedge_lookup=dict(c.hedge_lookup), poly_rate=c.poly_rate,
                        placed_ts=now_ts, phase=phase, best_bid=getattr(rest, "best_bid", None),
                        rest_venue=c.rest_venue,
                        kalshi_side=(c.rest_ref[2] if c.rest_venue == "kalshi" else ""),
                        teams=getattr(c, "teams", ""),
                        client_order_id=str(res.get("client_order_id") or ""))
        self.open_orders[c.key] = lo
        self._place_fail_n.pop(c.key, None)            # a successful place clears the refusal streak
        self._slot_wait_since.pop(c.key, None)         # got its slot -> no longer waiting
        self._track_rested(lo)                         # reconcile this instrument for flatness (persisted)
        self.caps.on_open()
        kind = "reprice" if existing is not None else "quote"
        self._record(c, kind, now, phase, price=price, size=size, hedge_ask=hedge_ask, order_id=oid)
        self._note_binding(c, price, size, hedge_ask, plan["binding"], hedge_depth, book_depth, phase)
        if getattr(dec, "net_at_quote", None) is not None:       # best edge SEEN this digest window (panel)
            self._digest["best_edge"] = max(self._digest.get("best_edge", 0.0),
                                            float(dec.net_at_quote) * 100.0)
        if existing is not None:
            self._emit_event("repriced", lo, instant=False, digest_kind="reprice",
                             old_price=existing.price, new_price=price)
        else:
            net = getattr(dec, "net_at_quote", None)
            self._emit_event("placed", lo, instant=False, digest_kind="quote", price=price, size=size,
                             hedge_venue=c.hedge_venue, hedge_price=hedge_ask,
                             exp_net_usd=(float(net) * size if net is not None else None),
                             exp_net_pct=(float(net) * 100.0 if net is not None else None),
                             **self._live_ctx())
        matched = float(res.get("shares") or 0)          # a maker shouldn't fill on POST, but the book can move
        if matched > 0:
            self._on_fill_detected(c.key, matched, float(res.get("avg_price") or price), store, now, now_ts)

    def _on_place_failed(self, c: Any, phase: str, price: float, exc: Any, now_ts: float) -> None:
        """A venue REFUSED the placement. Back the candidate off instead of retrying ~1x/second forever.

        A closed market never reopens, so retrying it is pure churn — and because every attempt used to
        fire an instant Telegram, one closed tennis market produced a steady 1/s alert stream. That is
        how the alert channel earns HTTP 429s (1,682 of them during the invisible-fill incident), and a
        rate-limited alert channel is one that cannot deliver the ORPHAN scream when it matters."""
        msg = str(exc)
        msg_l = msg.lower()          # venues return error codes in mixed case — match case-INSENSITIVELY
        # TERMINAL = the market itself is gone/finished, so no amount of waiting brings it back.
        terminal = any(s in msg_l for s in ("market_closed", "market_not_active", "market_settled",
                                            "market_not_found", "not_active", "closed"))
        n = self._place_fail_n.get(c.key, 0) + 1
        self._place_fail_n[c.key] = n
        # Non-terminal refusals escalate (60s, 120s, 240s... capped at an hour) so a persistently
        # unplaceable candidate cannot settle into a steady retry drip either.
        wait = (self.place_backoff_terminal_s if terminal
                else min(self.place_backoff_s * (2 ** (n - 1)), 3600.0))
        self._place_fail_until[c.key] = now_ts + wait
        subj = alerts.bet_name(c.sport, c.game, c.market_key, c.rest_side, getattr(c, "teams", ""))
        # Prefer the venue's error CODE ("market_closed", "too_many_requests"); NEVER put the raw HTTP
        # line / exception in the Telegram text (that goes to the log). Fall back to a plain generic.
        import re as _re
        m = _re.search(r'"code"\s*:\s*"([^"]+)"', msg)
        code = (m.group(1).lower() if m else "")
        why = ("the market has closed or finished" if terminal
               else (alerts.humanize_reason(code) if code else "the venue refused it"))
        detail = (f"Couldn't place my offer on {subj} — {why}."
                  + (" I'll stop trying this one today." if terminal
                     else f" I'll retry automatically in {wait:.0f}s."))
        if n == 1:                                   # scream ONCE per candidate, then log-only
            self._emit_event("problem", instant=True, detail=detail)
            if self.log:                             # the RAW error is log-only (has HTTP/UUID)
                self.log.warning("[MAKER_RT][LIVE] PLACE FAILED %s: %s", subj, msg)
        elif self.log:
            self.log.warning("[MAKER_RT][LIVE] PLACE FAILED %s: %s (suppressed repeat #%d)", subj, msg, n)

    def set_viable_directions(self, dirs: Any) -> None:
        """The driver reports which directions currently have a viable candidate. A reserved slot is then
        only protected for a direction in this set — so a reserve can NEVER block another direction when
        the reserving direction has nothing to place (the slot-starvation deadlock)."""
        self._viable_directions = {str(d).replace("_", "-") for d in (dirs or ())} & self.directions

    def _reservation_ok(self, direction: str) -> bool:
        """True iff ``direction`` may claim one more of max_open_quotes without eating another VIABLE
        direction's guaranteed reserve (per-direction slot fairness). Counts the CURRENT opens per
        direction from the live order book. Off (or single direction) -> always True.

        NON-BLOCKING: only directions that currently HAVE a viable candidate protect their reserve, so
        an idle direction's reserved slot never starves an active one. ``direction`` itself always
        counts as viable (it is asking to place right now)."""
        open_by_direction: dict = {}
        for lo in self.open_orders.values():
            open_by_direction[lo.direction] = open_by_direction.get(lo.direction, 0) + 1
        protected = set(self._viable_directions) | {direction}     # only protect reserves of viable dirs
        return direction_slot_ok(direction, open_by_direction, protected,
                                 self.caps.max_open_quotes, self.reserve_per_direction)

    def _refuse(self, c: Any, phase: str, price: float, reason: str, projected: float,
                now_ts: float) -> None:
        """Log a placement refusal. A full-slot refusal (max_open_quotes / reserve_per_direction) recurs
        every loop while the caps are full, so it is throttled to once per REFUSE_LOG_EVERY_S per
        node+direction+reason; suppressed hits are counted for the digest line instead of spamming."""
        text = (f"[MAKER_RT][LIVE] REFUSE {c.game} {c.direction} [{phase}] @ {price:.4f}: {reason} "
                f"(projected pair ${projected:.2f}, stake_today ${self.caps.stake_today:.2f})")
        if reason in _SLOT_REFUSE_REASONS:
            # SLOT-WAIT metric: a candidate refused for a slot has been WAITING since the first refusal.
            # Store [first_seen, last_seen]; a candidate that stops being refused is pruned (no longer waiting).
            w = self._slot_wait_since.get(c.key)
            if w is None:
                self._slot_wait_since[c.key] = [now_ts, now_ts]
            else:
                w[1] = now_ts
            k = (c.key, reason)
            if now_ts - self._refuse_log_at.get(k, -1e18) < REFUSE_LOG_EVERY_S:
                self._digest["refuse_suppressed"] = self._digest.get("refuse_suppressed", 0) + 1
                return
            self._refuse_log_at[k] = now_ts
        self._routine("refuse", text)

    def _depths_for(self, c: Any, hedge_ask: Optional[float], store: Any,
                    live_rest: Any) -> tuple[Optional[float], Optional[float]]:
        """(hedge_depth_at_ask, book_depth) from the live books, for sizing + the binding diagnostic.
        hedge_depth = resting hedge shares within ~1 tick of the ask (what we can actually hedge);
        book_depth = the rest-book's near liquidity. Guarded — a read failure just yields None (no cap)."""
        try:
            hl = c.hedge_lookup or {}
            if str(hl.get("venue") or getattr(c, "hedge_venue", "")) == "polymarket":
                hv = store.poly_view(hl.get("token"))
            else:
                hv = store.kalshi_view(hl.get("ticker"), hl.get("side"))
            ha = float(hedge_ask) if hedge_ask else None
            band = (ha + 0.011) if ha is not None else 1e9
            ladder = list(getattr(hv, "ask_ladder", None) or [])
            hedge_depth = sum(float(s) for p, s in ladder if float(p) <= band) if ladder else None
            rest_ladder = list(getattr(live_rest, "ask_ladder", None) or [])
            book_depth = sum(float(s) for _p, s in rest_ladder) if rest_ladder else None
            return hedge_depth, book_depth
        except Exception:  # noqa: BLE001
            return None, None

    @staticmethod
    def _venue_minimum(rest_venue: str, price: float) -> int:
        """Rest-leg venue MINIMUM (a floor): Kalshi = 1 contract (no notional minimum); Polymarket = 5
        shares AND enough to clear the ~$1 notional minimum (ceil(1/price))."""
        if rest_venue == "kalshi":
            return 1
        import math
        return max(POLY_MIN_SHARES, int(math.ceil(1.0 / max(float(price), 1e-9))))

    def _note_binding_count(self, name: str) -> None:
        """Tally a binding-constraint outcome (incl. the below_venue_minimum refusal) for panel + digest."""
        self._binding_counts[name] = self._binding_counts.get(name, 0) + 1
        self._digest.setdefault("binding", {})
        self._digest["binding"][name] = self._digest["binding"].get(name, 0) + 1

    def _note_binding(self, c: Any, price: float, size: float, hedge_ask: Optional[float],
                      binding: str, hedge_depth: Optional[float], book_depth: Optional[float],
                      phase: str) -> None:
        """DIAGNOSTIC: log + count the BINDING CONSTRAINT that set this quote's size (computed by
        plan_size). Best-effort — never interferes with placement."""
        try:
            self._note_binding_count(binding)
            if self.log:
                ha = float(hedge_ask) if hedge_ask else None
                self.log.info("[MAKER_RT][LIVE] SIZE %g sh @ %.4f ($%.2f) [%s] binding=%s — "
                              "cap<=%d pair<=%s hedgeDepth=%s bookDepth=%s min=%d", size, price,
                              price * size, phase, binding,
                              int(self.caps.quote_usd_max / max(price, 1e-9)),
                              (int(self.caps.max_pair_stake_usd / (price + ha)) if ha else "n/a"),
                              (int(hedge_depth) if hedge_depth is not None else "n/a"),
                              (int(book_depth) if book_depth is not None else "n/a"),
                              self._venue_minimum(c.rest_venue, price))
        except Exception:  # noqa: BLE001 — the binding diagnostic must never block a real order
            pass

    def _name_for_c(self, c: Any) -> str:
        """Human name for a CANDIDATE (mirror of _name_for for an order)."""
        return alerts.head(c.sport, c.game, c.market_key, getattr(c, "rest_side", ""), getattr(c, "teams", ""))

    # -- cancels -------------------------------------------------------------
    def cancel(self, c: Any, now: Any, reason: str) -> bool:
        return self._cancel(c.key, now, reason)

    def cancel_key(self, key: tuple, now: Any, reason: str) -> bool:
        return self._cancel(key, now, reason)

    def _cancel(self, key: tuple, now: Any, reason: str) -> bool:
        """Cancel the tracked order and CONFIRM cancellation. Returns True only when confirmed cancelled
        (so a reprice never double-places). An unconfirmed cancel leaves it tracked for a later sweep."""
        lo = self.open_orders.get(key)
        if lo is None:
            return True
        resp = None
        try:
            resp = self._venue_cancel(lo)
        except Exception as exc:  # noqa: BLE001 — order id + raw error are LOG-ONLY (never Telegram)
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] cancel raised for %s: %s", lo.order_id, exc)
        if not self._cancel_confirmed(lo, resp):
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] cancel NOT confirmed %s (%s) — keeping tracked.",
                                 lo.order_id, reason)
            return False
        self.open_orders.pop(key, None)
        self.caps.on_close()
        self._record_lifetime(lo, now)
        self._record_lo(lo, "expire", now, reason=reason)
        age_s = None
        try:
            age_s = (now.timestamp() - float(lo.placed_ts)) if now is not None else None
        except Exception:  # noqa: BLE001
            age_s = None
        self._emit_event("cancelled", lo, instant=False, digest_kind="cancel", reason=reason, age_s=age_s)
        return True

    #: venue order statuses that mean "this order is gone because it TRADED", not because we cancelled it.
    _FILLED_STATUSES = ("EXECUTED", "FILLED", "MATCHED", "COMPLETE", "COMPLETED")

    def _cancel_confirmed(self, lo: _LiveOrder, resp: Any) -> bool:
        """True ONLY when the cancel is VENUE-CONFIRMED terminal (canceled). A cancel that "succeeded"
        with a 404/empty response is NOT proof the order is gone — the 2026-07-25 ghost orders came from
        exactly that: DELETEs 404'd, the response/single-read looked empty, this returned True, the slot
        was freed, and a replacement stacked on top of the STILL-LIVE order. Now:

          * the cancel response's own ``canceled`` list is honored (fast path);
          * otherwise VENUE TRUTH decides — canceled -> True; FILLED -> route to fill detection + False
            (a fill is not a cancel; the position must be hedged, not replaced); still RESTING or an
            UNREADABLE/UNKNOWN state -> False (keep it tracked and retry, NEVER free the slot)."""
        if isinstance(resp, dict):
            canceled = resp.get("canceled") or resp.get("cancelled") or []
            if lo.order_id in canceled:
                return True
        state, _matched = self._venue_order_state(lo)
        if state == "canceled":
            return True
        if state == "filled":
            self._force_fill_poll = True          # NOT a cancel — the venue filled us; route to the hedge
            if self.log:
                self.log.error("[MAKER_RT][LIVE] cancel of %s did NOT cancel — the order FILLED; routing "
                               "to fill detection (slot NOT released).", lo.order_id)
        return False

    def _venue_order_state(self, lo: _LiveOrder, o: Any = _UNREAD) -> tuple:
        """VENUE TRUTH for one open order: ('canceled' | 'filled' | 'resting' | 'unknown', matched|None).

        Reads the order by id; if that single read is EMPTY (Kalshi's single-order GET can 404 while the
        order is still RESTING — the exact hole the ghost-order stack fell through) it RE-RESOLVES the
        order in the venue's RESTING list by (order_id, client_order_id). FAIL CLOSED: any unreadable step
        yields 'unknown', so the caller keeps the order tracked and never frees its slot or places a
        replacement on a possibly-live order. ``o`` may be a single-read the caller already did (avoids a
        redundant GET)."""
        if o is _UNREAD:
            try:
                o = self._venue_get_order(lo)
            except Exception:  # noqa: BLE001 — a raised single-read is UNREADABLE; re-resolve below
                o = None
        if isinstance(o, dict) and o:
            status = str(o.get("status") or "").upper()
            matched = self._order_matched(lo, o)
            if status in ("CANCELED", "CANCELLED"):
                return "canceled", matched
            if status in self._FILLED_STATUSES or (matched or 0) > lo.matched_seen + 1e-9:
                return "filled", matched
            return "resting", matched
        # Empty/failed single-order read -> re-resolve in the venue's RESTING list (by id, then coid).
        try:
            resting = self._venue_find_resting(lo)
        except Exception:  # noqa: BLE001 — could not read the resting list -> UNKNOWN -> fail closed
            return "unknown", None
        if resting is not None:
            return "resting", self._order_matched(lo, resting)
        # Positively ABSENT from the resting list == terminal (canceled, or filled + purged). A fill is
        # independently caught by the account-wide fill sweep / the matched branch, so calling this
        # 'canceled' only ever frees a slot whose order is genuinely no longer resting.
        return "canceled", None

    def _venue_find_resting(self, lo: _LiveOrder) -> Any:
        """Venue-dispatched re-resolution of ``lo`` in the RESTING-orders list: the order dict if still
        resting, None if positively absent. Raises on a read failure (caller fails closed). Kalshi matches
        by order_id OR our client_order_id (ticker-scoped); Poly matches its account open-orders by id (a
        client/fake without a lister degrades to None — Poly's single-order read is already reliable)."""
        if lo.rest_venue == "kalshi":
            if self.kalshi_order_client is None or not hasattr(self.kalshi_order_client, "find_resting"):
                return None
            return self.kalshi_order_client.find_resting(
                order_id=lo.order_id, client_order_id=(lo.client_order_id or None), ticker=lo.token)
        lister = getattr(self.poly, "open_orders", None)
        if not callable(lister):
            return None
        oo = lister()
        orders = oo if isinstance(oo, list) else ((oo or {}).get("data") or (oo or {}).get("orders") or [])
        for o in orders or []:
            if isinstance(o, dict) and lo.order_id in (o.get("id"), o.get("order_id"), o.get("orderID")):
                return o
        return None

    # -- pre-placement stack guard (never add a 2nd order to a market with a live ghost) --------------
    @staticmethod
    def _same_market(lo: _LiveOrder, c: Any) -> bool:
        return lo.rest_venue == c.rest_venue and lo.token == c.rest_ref[1]

    @staticmethod
    def _resting_order_id(o: Any) -> Any:
        return (o.get("order_id") or o.get("id") or o.get("orderID")) if isinstance(o, dict) else None

    def _our_resting_on_market(self, c: Any) -> Any:
        """OUR resting venue orders on candidate ``c``'s market. Kalshi: client_order_id-prefixed orders
        for the ticker. Poly: resting orders on THIS token only (strict match — never touch another
        market's order). None when the venue isn't listable for this leg (no client). Raises on a read
        failure (caller fails closed)."""
        if c.rest_venue == "kalshi":
            if self.kalshi_order_client is None or not hasattr(self.kalshi_order_client, "resting_orders"):
                return None
            return list(self.kalshi_order_client.resting_orders(ticker=c.rest_ref[1]) or [])
        lister = getattr(self.poly, "open_orders", None)
        if not callable(lister):
            return None
        oo = lister()
        orders = oo if isinstance(oo, list) else ((oo or {}).get("data") or (oo or {}).get("orders") or [])
        tok = c.rest_ref[1]
        out = []
        for o in orders or []:
            if not isinstance(o, dict):
                continue
            asset = o.get("asset_id") or o.get("token_id") or o.get("tokenID")
            if asset is not None and asset == tok:        # strict: only orders on THIS market's token
                out.append(o)
        return out

    def _stack_guard_ok(self, c: Any, now: Any, now_ts: float) -> bool:
        """PRE-PLACEMENT STACK GUARD (the belt behind the cancel fix). Before adding a NEW resting order
        to a market, ensure we hold NO UNTRACKED order of ours already resting there — a ghost a prior
        failed cancel left live. List our resting orders on this market; CANCEL-VERIFY any we are not
        tracking; if any survives, REFUSE to place (never stack). A LIST-read failure also refuses (fail
        closed). Returns True iff the market is clean enough to place."""
        try:
            ours = self._our_resting_on_market(c)
        except Exception as exc:  # noqa: BLE001 — cannot list our resting orders -> fail closed
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] STACK-GUARD list failed for %s — not placing this "
                                 "cycle: %s", self._name_for_c(c), exc)
            return False
        if ours is None:                                  # venue not listable for this leg -> nothing to guard
            return True
        tracked_ids = {lo.order_id for lo in self.open_orders.values() if self._same_market(lo, c)}
        extras = [o for o in ours if self._resting_order_id(o) and self._resting_order_id(o) not in tracked_ids]
        if not extras:
            return True
        cleared = sum(1 for o in extras if self._cancel_ghost_order(c, self._resting_order_id(o), now))
        clean = cleared == len(extras)
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] STACK-GUARD %s: %d untracked resting order(s) on this "
                             "market — cleared %d/%d%s.", self._name_for_c(c), len(extras), cleared,
                             len(extras), "" if clean else " — some SURVIVED, refusing to place (no stack)")
        return clean

    def _cancel_ghost_order(self, c: Any, oid: Any, now: Any) -> bool:
        """Cancel an UNTRACKED ghost order (not in open_orders) on ``c``'s market and confirm it is gone
        from the resting book. Routes a ghost that FILLED to fill detection (a naked fill is the fill
        sweep's / orphan system's problem, not a reason to stack more resting orders). Returns True iff
        the order is venue-confirmed no longer resting (canceled or filled)."""
        if not oid:
            return False
        ghost = _LiveOrder(key=c.key, order_id=oid, token=c.rest_ref[1], price=0.0, size=0.0,
                           side=getattr(c, "rest_side", ""), direction=c.direction, sport=c.sport,
                           game=c.game, market_key=c.market_key,
                           hedge_lookup=dict(getattr(c, "hedge_lookup", {}) or {}),
                           poly_rate=getattr(c, "poly_rate", 0.0), placed_ts=0.0, rest_venue=c.rest_venue,
                           kalshi_side=(c.rest_ref[2] if c.rest_venue == "kalshi" else ""))
        resp = None
        try:
            resp = self._venue_cancel(ghost)
        except Exception as exc:  # noqa: BLE001 — order id + raw error are LOG-ONLY
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] STACK-GUARD cancel raised for ghost %s: %s", oid, exc)
        if isinstance(resp, dict) and oid in (resp.get("canceled") or resp.get("cancelled") or []):
            return True
        state, _m = self._venue_order_state(ghost)
        if state == "filled":
            self._force_fill_poll = True                  # a filled ghost is a naked fill -> route it
        return state in ("canceled", "filled")

    def cancel_all(self, reason: str, now: Any = None) -> int:
        """Cancel EVERY open order (shutdown / halt / feed-down). Best-effort venue cancel-all + per-key
        confirm. Returns how many were open."""
        n = len(self.open_orders)
        try:
            self.poly.cancel_all()                    # Poly: every resting order on the account is ours
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] cancel_all() failed: %s", exc)
        if self.kalshi_order_client is not None:      # Kalshi: cancel our TRACKED resting orders (maker-scoped)
            try:
                self.kalshi_order_client.cancel_all()
            except Exception as exc:  # noqa: BLE001
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] kalshi cancel_all() failed: %s", exc)
        from .state import utcnow
        for key in list(self.open_orders):
            self._cancel(key, now or utcnow(), reason)
        return n

    def cancel_inplay_open(self, now: Any, reason: str = "inplay_halt") -> int:
        """Cancel only the IN-PLAY open orders (pre-game orders keep resting)."""
        keys = [k for k, lo in self.open_orders.items() if lo.phase == "inplay"]
        for k in keys:
            self._cancel(k, now, reason)
        return len(keys)

    def enforce_arm_state(self, now: Any) -> None:
        """Each loop: if a phase's gate was disarmed (arm file removed / enabled off), cancel that phase's
        open orders immediately."""
        pre_ok, ip_ok = self.pre_armed(), self.inplay_armed()
        for k, lo in list(self.open_orders.items()):
            if (lo.phase == "pre" and not pre_ok) or (lo.phase == "inplay" and not ip_ok):
                self._cancel(k, now, "disarmed")

    # -- feed health ---------------------------------------------------------
    def set_feed_ok(self, ok: bool, now: Any = None) -> None:
        """User-socket health. On a DOWN transition (was up, now down): HALT placement + cancel every open
        quote (a fill we cannot observe cannot be hedged). On an UP transition: re-enable placement."""
        was = self.feed_ok
        self.feed_ok = bool(ok)
        if was and not ok:
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] Poly USER socket DOWN — halting placement + cancelling opens.")
            self._send_telegram(alerts.format_event("problem", detail=(
                "My fill feed dropped — I've paused new offers and pulled the open ones so I can't miss a "
                "fill. I'll resume automatically when it reconnects.")))
            self.cancel_all("poly_user_down", now)
        elif ok and not was and self.log:
            self.log.info("[MAKER_RT][LIVE] Poly USER socket UP -- placement enabled.")

    # -- fill detection + hedge ----------------------------------------------
    def on_order_update(self, event: dict, store: Any, now: Any, now_ts: float) -> None:
        """Poly USER-socket order update -> detect our fills by size_matched delta (matched to our oid)."""
        oid = event.get("order_id")
        key = self._key_for_oid(oid)
        if key is None:
            return
        matched = event.get("size_matched")
        if matched is None:
            return
        self._on_fill_detected(key, float(matched), event.get("price"), store, now, now_ts)

    def on_kalshi_fill(self, event: dict, store: Any, now: Any, now_ts: float) -> None:
        """Kalshi WS 'fill' message -> detect our rest-kalshi fills by order_id. Kalshi fills arrive as
        DELTAS (contracts filled in this print), so accumulate onto matched_seen. A maker fill executes at
        our resting price (lo.price)."""
        key = self._key_for_oid(event.get("order_id"))
        if key is None:
            return
        lo = self.open_orders.get(key)
        if lo is None or lo.rest_venue != "kalshi":
            return
        count = event.get("count")
        if count in (None, ""):
            return
        total = lo.matched_seen + float(count)               # DELTA -> cumulative high-water mark
        self._on_fill_detected(key, total, lo.price, store, now, now_ts)

    def set_kalshi_feed_ok(self, ok: bool, now: Any = None, now_ts: Optional[float] = None) -> None:
        """Kalshi WS health (rest-kalshi FILL signal), DEBOUNCED. The socket drops `connected` on every
        reconnect (incl. the quiet-market ping probe), so acting on the raw flag cancelled our resting
        quotes on every blip and destroyed queue position. Because REST is the WS-INDEPENDENT fill
        authority, a brief outage is safe: we only declare the feed DOWN (cancel open rest-kalshi quotes)
        once it has been CONTINUOUSLY down for ``kalshi_feed_grace_s``. A reconnect inside the grace is a
        no-op (queue preserved). ``kalshi_feed_ok`` (gating placement) mirrors the debounced state."""
        import time as _time
        ts = now_ts if now_ts is not None else _time.time()
        if ok:
            self._kalshi_down_since = None
            if not self.kalshi_feed_ok:
                self.kalshi_feed_ok = True
                if self._kalshi_declared_down and self.log:
                    self.log.info("[MAKER_RT][LIVE] Kalshi WS UP -- rest-kalshi placement re-enabled.")
                self._kalshi_declared_down = False
            return
        # ok is False (socket currently shows down). Start / continue the grace timer.
        if self._kalshi_down_since is None:
            self._kalshi_down_since = ts
        down_for = ts - self._kalshi_down_since
        if down_for < self.kalshi_feed_grace_s:
            return                                            # brief blip within grace -> keep resting
        if self._kalshi_declared_down:
            return                                            # already handled this outage
        self._kalshi_declared_down = True
        self.kalshi_feed_ok = False
        from .state import utcnow
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] Kalshi WS DOWN %.0fs (> %.0fs grace) — cancelling rest-kalshi "
                             "quotes (fills unobservable).", down_for, self.kalshi_feed_grace_s)
        self._send_telegram(alerts.format_event("problem", detail=(
            "The Kalshi feed dropped — I pulled my Kalshi offers so I can't miss a fill there. My other "
            "offers keep running; I'll re-offer on Kalshi when it reconnects.")))
        for k, lo in list(self.open_orders.items()):
            if lo.rest_venue == "kalshi":
                self._cancel(k, now or utcnow(), "kalshi_feed_down")

    def needs_fill_poll(self) -> bool:
        """True when something (a cancel that failed because the order FILLED) demands the fill poll run
        NOW rather than at the next cadence tick."""
        return self._force_fill_poll

    def poll_open_orders(self, store: Any, now: Any, now_ts: float) -> None:
        """WS-INDEPENDENT FILL AUTHORITY. While ANY live order is open, read its REST status and hedge
        any matched delta the socket missed; also drop orders cancelled out from under us.

        This is the PRIMARY fill detector, not a backup: the private WS 'fill' channel is an accelerator
        whose callback can be lost (feed respawn) or whose frames can be missed (flap). Nothing here
        depends on a socket being connected."""
        self._force_fill_poll = False
        for key, lo in list(self.open_orders.items()):
            try:
                o = self._venue_get_order(lo)        # venue-dispatched single-order read
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(o, dict) or not o:
                # The single-order read is EMPTY. That is NOT proof the order is gone: a still-RESTING
                # Kalshi order reads empty when its single-order GET 404s — releasing the slot here (as the
                # old code did unconditionally) is exactly what let a replacement stack on the live order
                # (2026-07-25). Re-resolve against VENUE TRUTH before releasing; only a confirmed-terminal
                # order frees its slot, an UNKNOWN/still-resting one stays tracked. A short grace still
                # protects a just-placed order the venue hasn't indexed yet.
                if lo.matched_seen <= 1e-9 and now_ts - lo.placed_ts >= self._stale_grace_s:
                    state, matched = self._venue_order_state(lo, o)
                    if state == "canceled":
                        self._release_stale(key, lo, now)         # venue-confirmed gone -> free the slot
                    elif state == "filled" or (matched is not None and matched > lo.matched_seen + 1e-9):
                        self._force_fill_poll = True              # it FILLED (not cancelled) -> route to hedge
                    # 'resting' / 'unknown' -> keep tracked (never free a slot on an unreadable venue)
                continue
            matched = self._order_matched(lo, o)
            status = str(o.get("status") or "").upper()
            price = o.get("price") if lo.rest_venue == "polymarket" else lo.price   # kalshi maker fills at our rest
            if matched is not None and float(matched) > lo.matched_seen + 1e-9:
                self._on_fill_detected(key, float(matched), price, store, now, now_ts)
            elif status in ("CANCELED", "CANCELLED") and lo.matched_seen <= 1e-9:
                self._release_stale(key, lo, now)    # cancelled out-of-band -> stop tracking, free the slot
            elif (self.max_quote_age_s > 0 and now_ts - lo.placed_ts >= self.max_quote_age_s
                  and lo.matched_seen <= 1e-9):
                # AGE-OUT: a resting order this old is holding a slot indefinitely (behind best, edge gone,
                # the driver never revisits it because its node stopped producing a viable decision).
                # Cancel it; if the node is still viable the driver re-places next cycle, otherwise the slot
                # is freed for another candidate.
                self._aged_out += 1
                if self.log:
                    self.log.info("[MAKER_RT][LIVE] AGE-OUT %s — resting %.0fmin > %.0fmin cap; freeing slot.",
                                  self._name_for(lo), (now_ts - lo.placed_ts) / 60, self.max_quote_age_s / 60)
                self._cancel(key, now, "age_out")    # emits the human 'CANCELLED … held too long (aged out)'
        # RESYNC the caps counter to ground truth. can_place() gates on caps.open_quotes while the reserve
        # counts len(open_orders); if they ever drift (a release/fill path that missed a decrement), one
        # slot is silently lost. len(open_orders) is authoritative — every entry is a tracked live order.
        if self.caps.open_quotes != len(self.open_orders):
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] slot counter drift: caps.open_quotes=%d vs tracked=%d "
                                 "— resyncing to tracked.", self.caps.open_quotes, len(self.open_orders))
            self.caps.open_quotes = len(self.open_orders)

    def _release_stale(self, key: tuple, lo: Any, now: Any) -> None:
        """Drop a tracked order the venue no longer shows resting (cancelled/purged) and free its slot."""
        if self.open_orders.pop(key, None) is None:
            return
        self.caps.on_close()
        self._slot_released += 1
        self._record_lo(lo, "slot_released", now, reason="venue_not_resting")
        if self.log:
            self.log.info("[MAKER_RT][LIVE] released stale slot %s (venue no longer resting).", lo.order_id)

    def sample_slot_wait(self, now_ts: float) -> None:
        """Recompute the panel's slot-wait gauge: the longest a currently-waiting candidate has gone
        without a slot. A candidate not refused within the last 30s is pruned (it stopped waiting)."""
        for k, w in list(self._slot_wait_since.items()):
            if now_ts - w[1] > 30.0:
                self._slot_wait_since.pop(k, None)
        self.slot_wait_max_s = max((now_ts - w[0] for w in self._slot_wait_since.values()), default=0.0)

    def poll_kalshi_fills(self, store: Any, now: Any, now_ts: float) -> int:
        """Account-wide Kalshi fill sweep (GET /portfolio/fills) — ONE call that covers every open order
        and needs no socket at all. Catches a fill even when the order id has already left our book
        (the exact hole the 2026-07-23 incident fell through). Returns how many fills were routed."""
        if self.kalshi_order_client is None or not hasattr(self.kalshi_order_client, "fills_since"):
            return 0
        # Look back a generous window on the first sweep so a fill during startup/downtime is not missed.
        first = self._last_fills_sweep_ts == 0.0
        since = int(self._last_fills_sweep_ts or (now_ts - 3600.0)) - 5
        fills = self.kalshi_order_client.fills_since(since)
        self._last_fills_sweep_ts = now_ts
        if first:
            # PRIME ONLY. Fills that predate this process were already hedged (or are covered by the
            # startup reconciliation via the persisted watch-set); replaying them as "surprises" would
            # false-positive an orphan halt on every restart.
            self._seen_fill_ids.update(f.get("fill_id") or f.get("trade_id") for f in fills
                                       if (f.get("fill_id") or f.get("trade_id")))
            if fills and self.log:
                self.log.info("[MAKER_RT][LIVE] fills sweep primed with %d pre-existing fill(s).",
                              len(fills))
            return 0
        routed = 0
        for f in fills:
            fid = f.get("fill_id") or f.get("trade_id")
            if fid and fid in self._seen_fill_ids:
                continue
            key = self._key_for_oid(f.get("order_id"))
            if key is None:
                self._untracked_fill(f, now)          # a fill on NO tracked order -> naked, scream
                if fid:
                    self._seen_fill_ids.add(fid)
                continue
            if fid:
                self._seen_fill_ids.add(fid)
            lo = self.open_orders.get(key)
            if lo is None:
                continue
            from ...executor.kalshi_exec import fp_num
            cnt = fp_num(f, "count") or 0.0
            if cnt <= 0:
                continue
            routed += 1
            self._on_fill_detected(key, lo.matched_seen + cnt, lo.price, store, now, now_ts)
        if len(self._seen_fill_ids) > 5000:           # bound the dedupe set
            self._seen_fill_ids = set(list(self._seen_fill_ids)[-2500:])
        return routed

    def on_feed_reconnect(self, venue: str, store: Any, now: Any, now_ts: float) -> None:
        """A socket just came back UP. NEVER trust the stream to have carried what happened while it was
        away: immediately REST-poll every open order + sweep the account fill history before resuming."""
        if self.log:
            self.log.info("[MAKER_RT][LIVE] %s reconnect — REST-polling %d open order(s) before "
                          "trusting the stream.", venue, len(self.open_orders))
        try:
            self.poll_open_orders(store, now, now_ts)
            self.poll_kalshi_fills(store, now, now_ts)
        except Exception as exc:  # noqa: BLE001 — a reconnect poll must never kill the loop
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] reconnect poll failed: %s", exc)

    def note_flap(self, venue: str, up: bool, now_ts: float) -> None:
        """Count a socket DOWN->UP cycle and how long it stayed down (panel + re-arm evidence)."""
        if not up:
            if venue not in self.flap_down_since:
                self.flap_down_since[venue] = now_ts
            return
        t0 = self.flap_down_since.pop(venue, None)
        if t0 is None:
            return
        dur = max(0.0, now_ts - t0)
        self.flaps[venue] = self.flaps.get(venue, 0) + 1
        self.flap_secs[venue] = self.flap_secs.get(venue, 0.0) + dur
        if venue == "kalshi":                                # surface WS flakiness in the routine digest
            self._digest["kalshi_flaps"] = self._digest.get("kalshi_flaps", 0) + 1
            self._digest["kalshi_down_s"] = self._digest.get("kalshi_down_s", 0.0) + dur
        elif venue == "poly_user":                           # our FILL feed — its downtime is the riskiest
            self._digest["poly_flaps"] = self._digest.get("poly_flaps", 0) + 1
            self._digest["poly_down_s"] = self._digest.get("poly_down_s", 0.0) + dur
        if self.log:
            self.log.warning("[MAKER_RT] %s flap #%d — down %.1fs (cumulative %.0fs).",
                             venue, self.flaps[venue], dur, self.flap_secs[venue])

    def _untracked_fill(self, f: dict, now: Any) -> None:
        """A venue fill we have NO open order for — always ledgered, and escalated to an ORPHAN only if
        the position is actually NON-FLAT **beyond what we EXPECT to hold**.

        An unmatched fill is not itself proof of nakedness. TWO benign shapes reach here:
          * a fill routed via the socket closes its order out of ``open_orders`` before this sweep runs,
            so the same fill legitimately arrives here with no match; and
          * OUR OWN HEDGE — a rest-poly fill hedges by BUYING the complement on Kalshi, and that hedge
            fill lands in the account sweep on an order id we never rested. That hedge is an EXPECTED
            position, not an orphan (the 2026-07-24 HANHAL false halt).
        So NAKED means the venue says we hold MORE than we expect. We ask the venue and subtract the
        expected (rest + hedge) shares. A read failure is treated as non-flat (fail closed)."""
        from ...executor.kalshi_exec import fp_num
        tk = f.get("ticker") or f.get("market_ticker") or "?"
        cnt = fp_num(f, "count") or 0.0
        px = f.get("yes_price_dollars") if str(f.get("side", "")).lower() == "yes" else f.get("no_price_dollars")
        try:
            pos = self._kalshi_position(tk)
        except Exception:  # noqa: BLE001 — cannot read == cannot prove flat == treat as naked
            pos = None
        expected = self._expected_shares("kalshi", tk)
        explained = pos is not None and abs(pos) - expected <= HEDGE_SHARE_TOL
        # LEDGER LABEL. A fill that matches a REGISTERED expected hedge/rest leg is OUR OWN hedge
        # confirming on the account sweep — it lands on an order id we never rested, but it is a leg we
        # HOLD ON PURPOSE. Book it as ``hedge_confirmed``, NOT as an untracked surprise. Only a
        # genuinely-unexplained fill is ``fill_untracked``. (Sunday's ZHELAN 26sh + TORBOS 21sh Kalshi
        # hedges were logged fill_untracked despite matching expected-hedges because this row was written
        # BEFORE the expected check — reordering it is the fix; the untracked bucket now holds only truly
        # naked fills like the UFC ghost stack.)
        is_expected_leg = explained and expected > HEDGE_SHARE_TOL
        if self.state is not None:
            self.state.record({"event": "hedge_confirmed" if is_expected_leg else "fill_untracked",
                               "mode": "live", "phase": "?", "game": tk, "market_key": tk,
                               "side": str(f.get("side") or ""), "direction": "rest-kalshi",
                               "rest_venue": "kalshi", "quote_price": px or "", "size": round(cnt, 2),
                               "reason": f.get("order_id") or ""}, now)
        if explained:
            # Flat, or fully EXPLAINED by an expected hedge/rest leg we booked -> ledger, do NOT halt.
            if self.log:
                how = "FLAT" if abs(pos) <= HEDGE_SHARE_TOL else f"EXPECTED hedge/rest leg (hold {expected:g})"
                self.log.info("[MAKER_RT][LIVE] untracked fill on %s (order %s, %.0f) — position %s "
                              "(read=%.2f); logged %s, no orphan.", tk, f.get("order_id"), cnt, how, pos,
                              "hedge_confirmed" if is_expected_leg else "ledgered")
            return
        self._traded_tickers.add(tk)                    # make sure reconciliation keeps watching it
        self._persist_traded_tokens()
        naked = (abs(pos) - expected) if pos is not None else cnt
        self._orphan_detected(tk, tk, "?", pos if pos is not None else cnt,
                              f"UNTRACKED venue fill (order {f.get('order_id')}) on a NON-FLAT position "
                              f"(read={pos}, expected {expected:g}, unexplained {naked:g})", now)

    def _on_fill_detected(self, key: tuple, total_matched: float, avg_price: Any, store: Any,
                          now: Any, now_ts: float) -> None:
        lo = self.open_orders.get(key)
        if lo is None:
            return
        delta = float(total_matched) - lo.matched_seen
        if delta <= 1e-9:
            return
        # ONE in-flight hedge GLOBALLY (pre+inplay). If busy, DEFER without advancing matched_seen so the
        # next detection (socket or REST poll) retries this delta once the guard frees.
        if self.in_flight is not None and not self.in_flight.acquire(("live", key)):
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] hedge in-flight busy — deferring fill of %s", lo.game)
            return
        try:
            lo.matched_seen = float(total_matched)
            fill_price = float(avg_price) if avg_price not in (None, "") else lo.price
            result = self._hedge_fill(lo, delta, fill_price, store, now, now_ts)
        finally:
            if self.in_flight is not None:
                self.in_flight.release(("live", key))
        self._digest["fills"] += 1                          # count for the digest (the hedge is instant-alerted)
        self.persist_daily_caps()                           # a fill moved stake/fills/pnl -> persist NOW
        if lo.phase == "inplay":
            self._apply_inplay_circuit(lo, result, now, now_ts)
        if total_matched >= lo.size - 1e-9:                # fully filled -> no remainder resting
            self._record_lifetime(lo, now)
            self.open_orders.pop(key, None)
            self.caps.on_close()
        elif self.caps.halted or (lo.phase == "inplay" and self.inplay_halted):
            self._cancel(key, now, "halt_after_partial")   # partial + a cap/circuit tripped -> pull remainder

    def _hedge_fill(self, lo: _LiveOrder, matched: float, fill_price: float, store: Any,
                    now: Any, now_ts: float) -> dict:
        """Hedge one fill: re-verify -> DECLINE+unwind, or lift the Kalshi IOC (a miss/partial unwinds the
        UNHEDGED remainder). EVERY unwind goes through _verified_unwind (REST-confirm flat or SCREAM+HALT).
        Returns the result dict (outcome, locked_net, pnl, hedge order id, chain)."""
        self.caps.commit_stake(matched * fill_price)              # rest leg committed
        hl = lo.hedge_lookup
        hedge_venue = hl.get("venue", "kalshi")
        hv = store.kalshi_view(hl.get("ticker"), hl.get("side")) if hedge_venue == "kalshi" \
            else store.poly_view(hl.get("token"))
        # ONE shared PRE-HEDGE gate for BOTH directions (rest-poly->hedge-kalshi AND rest-kalshi->hedge-poly):
        # walk the ACTUAL current hedge book for the full fill size + that venue's taker fee -> the achievable
        # locked net (the rest leg is a maker, fee 0, on both sides). This is what re-checks a quote whose
        # hedge moved against us between quote and fill (adverse selection) — the exact failure that let a
        # rest-kalshi golubic fill hedge into a -2% guaranteed loss.
        re_mark = hedge_mod.mark_hedge(hv.ask_ladder, matched, hedge_venue, lo.poly_rate) if hv else None
        locked = hedge_mod.locked_net(fill_price, re_mark["cost_per_share"]) if re_mark else None
        hedge_px_est = re_mark["avg_price"] if re_mark else None
        self._record_fill(lo, matched, fill_price, now)          # ledger chain head: fill -> hedge_* -> unwind
        self._emit_event("filled", lo, instant=True, price=fill_price, size=matched)
        # DECLINE (same code path, both directions): the walked hedge can't lock a net above the decline
        # floor, OR the walked pair already costs >= $1.00/share (a guaranteed loss before fees), OR there
        # is no readable hedge book. Do NOT leg in — unwind the WHOLE rest fill (verified) + log hedge_declined.
        decline = self._prehedge_decline_reason(locked, fill_price, hedge_px_est,
                                                marked_shares=(re_mark or {}).get("shares"),
                                                requested_shares=matched)
        if decline:
            if self.log:
                walked = float((re_mark or {}).get("shares") or 0.0)
                self.log.error("[MAKER_RT][LIVE] PRE-HEDGE DECLINE (%s) %s: rest %.4f x%.2f, walked hedge "
                               "%s for %.2f/%.2f sh, locked_net %s — NOT hedging; unwinding the rest fill.",
                               decline, self._name_for(lo), fill_price, matched,
                               ("%.4f" % hedge_px_est) if hedge_px_est is not None else "n/a",
                               walked, matched, ("%.4f" % locked) if locked is not None else "n/a")
            self._digest["prehedge_declines"] = self._digest.get("prehedge_declines", 0) + 1
            return self._unwind_and_record(lo, matched, fill_price, locked, "hedge_declined", now)
        # FIRE the hedge on the COMPLEMENT venue (rest_poly -> Kalshi IOC; rest_kalshi -> Poly FAK), with an
        # EXPLICIT price cap so the executed hedge can never be worse than the one the gate just approved.
        cap = self._hedge_price_cap(fill_price, hedge_venue, lo.poly_rate)
        if hedge_venue == "polymarket":
            res = self.hedger.hedge_poly({"price": fill_price, "size": matched},
                                         {"token": hl.get("token"), "best_ask": getattr(hv, "best_ask", None),
                                          "max_price": cap})
        else:
            res = self.hedger.hedge({"token_id": lo.token, "side": "BUY", "price": fill_price, "size": matched},
                                    {"ticker": hl.get("ticker"), "side": hl.get("side", "yes"),
                                     "best_ask": getattr(hv, "best_ask", None), "max_price": cap})
        status = getattr(res, "status", "error")
        hedged = float(getattr(res, "hedged_shares", 0.0) or 0.0)
        hedge_avg = getattr(res, "hedge_avg_price", None)
        hedge_fee = getattr(res, "hedge_fee", None)          # ACTUAL taker fee on the hedge leg ($)
        _detail = getattr(res, "detail", None) or {}
        hedge_oid = ((_detail.get("kalshi") or _detail.get("poly") or {}) or {}).get("order_id") \
            if isinstance(_detail, dict) else None
        if status == "locked":
            pnl = float(getattr(res, "locked_pnl", 0.0) or 0.0)
            # FEE-HONEST locked net: from the ACTUAL hedge fill price + actual fee, not the pre-fire
            # quoted-ask estimate (``locked``). ~1 fee (Kalshi ceil-to-cent 0.07·C·P·(1-P)) is comparable
            # to the whole edge at these sizes, so the estimate is not good enough to book.
            locked_actual = self._actual_locked_net(fill_price, hedge_avg, hedge_fee, hedged, locked)
            return self._record_hedge_locked(lo, matched, fill_price, hedged, hedge_avg, hedge_oid,
                                             locked_actual, hedge_venue, now, pnl=pnl, hedge_fee=hedge_fee)
        # NOT reported-locked. Before unwinding ANYTHING, prove the naked exposure against VENUE TRUTH: a
        # venue's own fill count can UNDER-report (a Poly BUY response reports USDC, not shares -> a FULL
        # hedge masquerades as a partial). Read the COMPLEMENT position we actually hold; the truly naked
        # amount is (rest held) - (complement held). If that is ~0 the fill IS hedged and the unwind is
        # UNREACHABLE — a successful hedge must never fall through to an unwind + phantom orphan (TBTOR).
        complement = self._complement_shares(hedge_venue, hl, lo.matched_seen)
        if complement is not None:
            naked = max(0.0, lo.matched_seen - complement)
            if naked <= HEDGE_SHARE_TOL:                       # venue confirms fully hedged -> LOCKED
                true_hedged = min(lo.matched_seen, max(hedged, complement))
                locked_actual = self._actual_locked_net(fill_price, hedge_avg, hedge_fee, true_hedged, locked)
                pnl = float(locked_actual) * true_hedged if locked_actual is not None else 0.0
                # OVER-FILL ACCOUNTING: a $-sized Poly hedge sweep can fill at a better avg price and return
                # MORE shares than requested. pnl is booked on the PAIRED amount (true_hedged), but the
                # EXPECTED position must register the ACTUAL VENUE-HELD shares (``complement``) — never the
                # clamped/derived amount — so a benign over-hedge can never read as a naked orphan on the
                # next reconcile (the 2026-07-27 19:09 halt: 82.59 held vs 79 registered).
                return self._record_hedge_locked(lo, matched, fill_price, true_hedged, hedge_avg,
                                                 hedge_oid, locked_actual, hedge_venue, now, pnl=pnl,
                                                 hedge_fee=hedge_fee, verified=True,
                                                 actual_hedge_shares=complement)
            hedged = max(hedged, complement)                   # never unwind shares we actually hold hedged
        # MISS / PARTIAL / ERROR -> unwind the genuinely UNHEDGED remainder (verified). A partial hedge
        # locks its part.
        if hedged > 0:
            self.caps.commit_stake(min(hedged, matched) * float(hedge_avg or 0.0))
        remainder = max(0.0, matched - hedged)
        return self._unwind_and_record(lo, remainder, fill_price, locked, "hedge_unwound", now,
                                       hedge_oid=hedge_oid)

    @staticmethod
    def _prehedge_decline_reason(locked_net_est: Optional[float], fill_price: float,
                                 hedge_price_est: Optional[float],
                                 marked_shares: Optional[float] = None,
                                 requested_shares: Optional[float] = None) -> Optional[str]:
        """THE shared pre-hedge gate (identical for BOTH directions), as a REASON. Returns None to hedge,
        else the reason to DECLINE (do not hedge; unwind the rest fill instead):
          * ``no_hedge_book``   — nothing readable, so no profit can be proven;
          * ``hedge_too_thin``  — the walk did NOT cover the whole fill. A shallow walk's VWAP is the price
            of the few shares that WERE there, not of the sweep we are about to send, so it systematically
            UNDER-states the real cost. Declining on short depth is what the 01:21Z PHIMIA loss needed:
            the gate priced ~5c off a partial ladder and the real FAK paid 7c ($1.01/share, -1.35%);
          * ``below_floor``     — the achievable locked net is under ``HEDGE_DECLINE_FLOOR``;
          * ``dollar_pair``     — rest fill + hedge already costs >= $1.00/share, a guaranteed loss BEFORE
            fees, so no fee model can rescue it.
        Pure so it is unit-tested directly against the production numbers."""
        if locked_net_est is None:
            return "no_hedge_book"                               # no readable hedge -> can't prove a profit
        # DEPTH COVERAGE FIRST: an optimistic partial walk must never be read as a full-size hedge price.
        if requested_shares is not None and marked_shares is not None:
            if float(marked_shares) < float(requested_shares) - HEDGE_SHARE_TOL:
                return "hedge_too_thin"
        if float(locked_net_est) < HEDGE_DECLINE_FLOOR:
            return "below_floor"
        if hedge_price_est is not None and (float(fill_price) + float(hedge_price_est)) >= 1.0 - 1e-9:
            return "dollar_pair"                                 # rest + hedge >= $1/share == guaranteed loss
        return None

    @staticmethod
    def _prehedge_declines(locked_net_est: Optional[float], fill_price: float,
                           hedge_price_est: Optional[float],
                           marked_shares: Optional[float] = None,
                           requested_shares: Optional[float] = None) -> bool:
        """Boolean form of ``_prehedge_decline_reason`` (True == DO NOT HEDGE)."""
        return PregameLiveExecutor._prehedge_decline_reason(
            locked_net_est, fill_price, hedge_price_est, marked_shares, requested_shares) is not None

    @staticmethod
    def _hedge_price_cap(fill_price: float, hedge_venue: str, poly_rate: float = 0.05) -> float:
        """The HIGHEST hedge price whose fee-inclusive locked net still meets ``HEDGE_DECLINE_FLOOR`` —
        sent to the venue as the hedge order's LIMIT.

        Without this the hedger picks its own limit from a book it re-fetches at hedge time
        (``best_ask + 2 ticks`` on Poly), so a book that moved between the gate and the order could be
        swept at ANY price — the gate approved ~5c and the sweep paid 7c. Capping means a moved book fills
        less (or nothing) and falls through to the existing VERIFIED unwind, instead of locking a loss.

        Solves ``1 - fill - p - fee(p) = floor`` EXACTLY for each venue's taker-fee curve, because an
        approximation here is not free: rounding the cap down by even one tick turns a hedge we want into
        a miss + unwind, and rounding it up re-opens the loss this cap exists to prevent.
          * Kalshi  fee = 0.07*p*(1-p)      -> 0.07p² - 1.07p + room = 0 (take the low root)
          * Poly    fee = rate*min(p, 1-p)  -> two branches, joined at p = 0.5"""
        room = 1.0 - float(fill_price) - HEDGE_DECLINE_FLOOR
        if room <= 0.0:
            return 0.0
        if str(hedge_venue).lower() in ("polymarket", "poly"):
            rate = float(poly_rate)
            cap = room / (1.0 + rate)                            # cheap side: fee = rate*p
            if cap > 0.5:                                        # dear side: fee = rate*(1-p)
                cap = (room - rate) / (1.0 - rate) if rate < 1.0 else room
        else:
            disc = 1.07 ** 2 - 4.0 * 0.07 * room
            cap = room if disc < 0 else (1.07 - math.sqrt(disc)) / (2.0 * 0.07)
        return max(0.0, min(0.99, cap))

    @staticmethod
    def _pair_is_profit(fill_price: float, hedge_avg: Any, locked: Optional[float]) -> bool:
        """A completed hedge is a GENUINE profit only when the actual locked net is > 0 AND the pair
        (rest + hedge per share) costs < $1.00. Anything else is a locked LOSS (alert it as an ERROR, never
        'GUARANTEED')."""
        if locked is None or float(locked) <= 1e-9:
            return False
        pair_ps = float(fill_price) + float(hedge_avg or 0.0)
        return pair_ps < 1.0 - 1e-9

    @staticmethod
    def _actual_locked_net(fill_price: float, hedge_avg: Any, hedge_fee: Any, hedged: Any,
                           fallback: Optional[float]) -> Optional[float]:
        """Per-share locked net from the ACTUAL hedge fill: 1 − rest_fill_price − hedge_avg − fee/share.
        Falls back to the pre-fire estimate only when the actual hedge price is unavailable (a
        complement-verified fill that under-reported its own price)."""
        if hedge_avg is None or hedged in (None, 0) or float(hedged) <= 0:
            return fallback
        fee_ps = (float(hedge_fee) / float(hedged)) if hedge_fee is not None else 0.0
        return 1.0 - float(fill_price) - float(hedge_avg) - fee_ps

    def _record_hedge_locked(self, lo: _LiveOrder, matched: float, fill_price: float, hedged: float,
                             hedge_avg: Any, hedge_oid: Any, locked: Optional[float], hedge_venue: str,
                             now: Any, *, pnl: float, hedge_fee: Any = None, verified: bool = False,
                             actual_hedge_shares: Any = None) -> dict:
        """Book a LOCKED hedge (reported OR position-verified): commit the hedge stake, count the fill,
        ledger the hedge_locked chain row + instant alert. ``locked`` is the FEE-HONEST net (actual hedge
        price + actual fee). ``verified`` marks the path where the venue's fill count under-reported but
        the COMPLEMENT position proves fully hedged (unwind suppressed)."""
        self.caps.commit_stake(float(hedged) * float(hedge_avg or 0.0) + float(hedge_fee or 0.0))
        self.caps.on_fill(pnl)
        # POST-HEDGE HONESTY GUARD: a "LOCKED / profit is GUARANTEED" alert may ONLY be emitted when the
        # pair genuinely profits (locked_net > 0 AND rest+hedge < $1/share). A pair that summed >= $1/share
        # or nets <= 0 is a guaranteed LOSS the pre-hedge gate should have declined — alert it as a red
        # ERROR (never "GUARANTEED") so a -2% hedge like the 2026-07-27 golubic fill can't read as a win.
        profit = self._pair_is_profit(fill_price, hedge_avg, locked)
        self._record_lo(lo, "hedge_locked", now, price=fill_price, size=matched, locked_net=locked,
                        locked_pnl=pnl, hedge_avg=hedge_avg, hedge_order_id=hedge_oid)
        if not profit and self.log:
            self.log.error("[MAKER_RT][LIVE] HEDGED AT A LOSS %s: rest %.4f + hedge %.4f = %.4f/sh, "
                           "locked_net %s, pnl $%.2f — booked, alerted ERROR (not GUARANTEED).",
                           self._name_for(lo), float(fill_price), float(hedge_avg or 0.0),
                           float(fill_price) + float(hedge_avg or 0.0),
                           ("%.2f%%" % (locked * 100.0)) if locked is not None else "n/a", pnl)
        self._emit_event("locked" if profit else "locked_loss", lo, instant=True, pnl=pnl,
                         net_pct=(locked * 100.0 if locked is not None else None),
                         hedge_price=hedge_avg, hedge_venue=hedge_venue,
                         rest_price=fill_price, rest_shares=matched, hedge_shares=hedged,
                         hedge_fee=hedge_fee, **self._live_ctx())
        self._note_pair_legs(lo, matched, fill_price, hedged, hedge_avg, hedge_fee)   # fee-honest cost basis
        # Register the ACTUAL venue-held hedge shares (never the derived/clamped amount) so an over-fill is
        # accounted, not orphaned. Falls back to ``hedged`` when the caller has no separate venue read.
        hedge_held = actual_hedge_shares if actual_hedge_shares is not None else hedged
        self._note_expected_legs(lo, matched, hedge_held, hedge_venue, now)    # rest+hedge legs we now HOLD
        if verified and self.log:
            self.log.warning("[MAKER_RT][LIVE] hedge reported non-locked but the COMPLEMENT position "
                             "confirms FULLY HEDGED (%.2f sh) — booked LOCKED, unwind suppressed (no "
                             "phantom orphan). %s", float(hedged), self._name_for(lo))
        return {"outcome": "hedge_locked", "locked_net": locked, "pnl": pnl, "hedge_order_id": hedge_oid,
                "chain": self._chain(lo, matched, fill_price, "locked", locked, pnl, hedge_oid)}

    def _complement_shares(self, hedge_venue: str, hl: dict, target: float) -> Optional[float]:
        """VENUE-TRUTH shares of the hedge COMPLEMENT we currently hold (Poly token balance / Kalshi
        position). Proves a fill is actually hedged even when the order response under-reports its own
        fill. Polls briefly so a just-filled BUY's balance settles before we read it. Returns None when
        the read is impossible (no client / error) so the caller FAILS SAFE (unwinds the reported
        remainder) — this guard can only ever PREVENT an unwind, never cause a naked position."""
        try:
            if hedge_venue == "polymarket":
                tok = hl.get("token")
                poly = getattr(self.hedger, "poly", None) or self.poly
                if not tok or poly is None:
                    return None
                if hasattr(poly, "settle_conditional_balance"):
                    bal = poly.settle_conditional_balance(
                        tok, lambda b: b is not None and b >= float(target) - HEDGE_SHARE_TOL)
                else:
                    bal = poly.conditional_balance(tok)
                return None if bal is None else float(bal)
            if self.kalshi is None:
                return None
            return self._poll_kalshi_position(hl.get("ticker"), float(target))
        except Exception:  # noqa: BLE001 - unknown -> fail safe (caller unwinds the reported remainder)
            return None

    def _poll_kalshi_position(self, ticker: Any, target: float, *, tries: int = 4,
                              poll_s: float = 0.5) -> Optional[float]:
        """Poll the Kalshi position until it reaches ``target`` (a just-lifted hedge may settle late) or
        ``tries`` exhausted. Returns the last read (None if every read failed)."""
        import time as _time
        last: Optional[float] = None
        for i in range(max(1, tries)):
            try:
                last = self._kalshi_position(ticker)
            except Exception:  # noqa: BLE001
                last = None
            if last is not None and last >= float(target) - HEDGE_SHARE_TOL:
                return last
            if i < tries - 1:
                _time.sleep(poll_s)
        return last

    def _unwind_and_record(self, lo: _LiveOrder, shares: float, fill_price: float, locked: Optional[float],
                           ok_outcome: str, now: Any, hedge_oid: Any = None) -> dict:
        """Unwind ``shares`` of the naked fill and VERIFY the position is flat. On success record
        ``ok_outcome`` (hedge_declined | hedge_unwound). On failure record 'unwind_FAILED' + SCREAM + HALT
        all live quoting -- NO 'unwound' row is ever written without a venue-confirmed flat position."""
        if shares <= 1e-9:                                    # nothing naked (fully hedged) -> flat by construction
            self._record_lo(lo, ok_outcome, now, price=fill_price, size=0, locked_net=locked, unwind_cost=0.0)
            return {"outcome": ok_outcome, "locked_net": locked, "pnl": 0.0, "hedge_order_id": hedge_oid,
                    "chain": self._chain(lo, 0, fill_price, ok_outcome, locked, 0.0, hedge_oid)}
        if shares < MIN_TRADABLE.get(lo.rest_venue, 1.0):
            # UN-CLOSABLE DUST. Kalshi fills fractional counts but only accepts WHOLE contracts on the way
            # out, so a 0.49-share residue has no order that could close it: attempting one sells 0 (or
            # rounds UP and oversells), the position never reads flat, and verify-or-scream then halts the
            # whole bot over eleven cents. Book the residue, keep it on the watch-set, and let it settle —
            # settle_provisional_marks writes its ACTUAL outcome. Never a halt.
            return self._book_dust(lo, shares, fill_price, locked, ok_outcome, now, hedge_oid)
        u = self._verified_unwind(lo, shares, fill_price)
        if u["ok"]:
            cost = u["cost"] or 0.0
            self.caps.on_fill(-cost)
            if u["sold"] > 0 and u["sell_px"] is not None:
                self.caps.commit_stake(float(u["sell_px"]) * float(u["sold"]))
            self._record_lo(lo, ok_outcome, now, price=fill_price, size=shares, locked_net=locked,
                            unwind_cost=cost)
            self._emit_event("unwound", lo, instant=True, size=u["sold"], price=u["sell_px"], cost=cost,
                             reason=("hedge too dear" if ok_outcome == "hedge_declined" else "hedge missed"))
            return {"outcome": ok_outcome, "locked_net": locked, "pnl": -cost, "hedge_order_id": hedge_oid,
                    "chain": self._chain(lo, shares, fill_price, ok_outcome, locked, -cost, hedge_oid)}
        # The unwind did not leave us provably flat. Before booking anything, try the BOUNDED AUTO-FLATTEN:
        # if the whole position is small enough that its worst case is affordable, sweep it out and carry
        # on. Only if that is refused or fails do we book the worst case and halt — booking first would
        # trip the daily-loss rail on a position we are about to close for a fraction of that.
        rem = u["remaining"]
        naked = float(rem if rem is not None else shares)
        flat = self._auto_flatten_orphan(lo, naked, fill_price, now)
        if flat is not None:
            self.caps.on_fill(flat)
            self._record_lo(lo, "auto_flattened", now, price=fill_price, size=naked, locked_net=locked,
                            unwind_cost=-flat)
            self.persist_daily_caps()
            return {"outcome": ok_outcome, "locked_net": locked, "pnl": flat, "hedge_order_id": hedge_oid,
                    "chain": self._chain(lo, naked, fill_price, "auto_flattened", locked, flat, hedge_oid)}
        # VERIFY-OR-SCREAM: still naked -> ORPHAN. Book the worst-case loss as a PROVISIONAL mark (the
        # position is open; its real outcome is unknown), so it can be rebooked at VENUE TRUTH the moment
        # it closes or settles — see ``settle_provisional_marks``.
        est_loss = round(float(fill_price) * naked, 4)
        self.caps.on_fill(-est_loss)
        self._record_lo(lo, "unwind_FAILED", now, price=fill_price, size=shares, locked_net=locked,
                        unwind_cost=est_loss)
        self._mark_provisional(lo, naked, fill_price, -est_loss, now, "unwind_FAILED")
        self._orphan(lo, rem, u.get("sell_res"), now)
        return {"outcome": "unwind_FAILED", "locked_net": locked, "pnl": -est_loss,
                "hedge_order_id": hedge_oid,
                "chain": self._chain(lo, shares, fill_price, "unwind_FAILED", locked, -est_loss, hedge_oid)}

    def _apply_inplay_circuit(self, lo: _LiveOrder, result: dict, now: Any, now_ts: float) -> None:
        """After an IN-PLAY fill: (a) if locked_net <= halt threshold -> HALT in-play for the day + cancel
        in-play opens; (b) on the day's FIRST in-play fill -> pause in-play placement + Telegram the chain.
        Pre-game is never affected."""
        self.inplay_fills_today += 1
        locked = (result or {}).get("locked_net")
        if locked is not None and locked <= self.inplay_halt_locked_net and not self.inplay_halted:
            self.inplay_halted = True
            self._emit_event("halted", instant=True,
                             detail=(f"in-play day-halt — fill locked {locked*100:.2f}% "
                                     f"≤ {self.inplay_halt_locked_net*100:.1f}% floor; in-play stopped "
                                     f"for the day (pre-game continues)"))
            self.cancel_inplay_open(now, "inplay_day_halt")
        if self.inplay_fills_today == 1:
            self.inplay_pause_until = now_ts + self.inplay_first_fill_pause_s
            if self.log:                                 # the technical chain (order ids) is LOG-ONLY
                self.log.warning("[MAKER_RT][INPLAY] first in-play fill — pausing in-play %.0fs. chain: %s",
                                 self.inplay_first_fill_pause_s, (result or {}).get("chain"))
            self._send_telegram(
                f"ℹ️ First in-play fill of the day — pausing in-play offers for "
                f"{self.inplay_first_fill_pause_s:.0f}s as a safety check. Pre-game offers keep running.")

    def _chain(self, lo: _LiveOrder, matched: float, fill_price: float, outcome: str,
               locked: Optional[float], pnl: float, hedge_oid: Any) -> str:
        return (f"{lo.game} {lo.market_key} [{lo.phase}] fill {matched:.0f}@{fill_price:.4f} rest_id={lo.order_id} "
                f"-> {outcome} hedge_id={hedge_oid} locked_net={'n/a' if locked is None else f'{locked*100:.2f}%'} "
                f"pnl=${pnl:.2f}")

    def _verified_unwind(self, lo: _LiveOrder, shares: float, fill_price: float) -> dict:
        """VENUE-DISPATCHED verified unwind: market-sell the naked fill AND REST-verify the position is flat.
        Returns {ok, sold, sell_px, cost, remaining, sell_res}. ok is True ONLY when the position read
        confirms flat. A read failure is treated as NOT flat (fail-closed). Identical doctrine both venues."""
        self._pull_own_book_side(lo)          # our own resting order would BLOCK this sell — see below
        if lo.rest_venue == "kalshi":
            return self._verified_unwind_kalshi(lo, shares, fill_price)
        return self._verified_unwind_poly(lo, shares, fill_price)

    def _pull_own_book_side(self, lo: _LiveOrder) -> int:
        """Cancel every order of OURS still resting on ``lo``'s instrument BEFORE we try to unwind it.

        A rest order is usually only PARTIALLY filled — the remainder is still live on the book at our bid.
        An unwind sells into that book, so the first thing it crosses is our own resting bid, and Kalshi's
        ``self_trade_prevention_type: taker_at_cross`` responds by CANCELLING the taker: the venue returns
        ``fill_count 0, remaining_count 0, status canceled``, which is indistinguishable from "the book was
        empty". That is precisely what happened at 2026-07-28 17:21:50Z and 17:21:52Z on
        KXUECLTOTAL-26JUL28CSKTRN-4 — two unwind sells of a 114-lot were killed by our own 318-lot bid,
        which we did not cancel until 17:21:56Z (four seconds later, as part of the orphan halt). The bot
        then declared an orphan and sat halted for three hours over a position that was one cent from flat.

        Best-effort by design: a cancel that fails must not stop the unwind (a naked position is the worse
        risk). Returns how many cancels were attempted."""
        n = 0
        try:
            if lo.rest_venue == "kalshi" and self.kalshi_order_client is not None:
                resting = self.kalshi_order_client.resting_orders(ticker=lo.token) or []
                ids = {o.get("order_id") or o.get("id") for o in resting}
                ids.add(lo.order_id)
                for oid in [i for i in ids if i]:
                    try:
                        self.kalshi_order_client.cancel(oid)
                        n += 1
                    except Exception as exc:  # noqa: BLE001 — a 404 here means already terminal
                        if self.log:
                            self.log.info("[MAKER_RT][LIVE] pre-unwind cancel %s: %s", oid, exc)
            elif lo.rest_venue == "polymarket" and self.order_client is not None:
                self.order_client.cancel(lo.order_id)
                n += 1
        except Exception as exc:  # noqa: BLE001 — never let the tidy-up block the unwind
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] pre-unwind cancel sweep failed on %s: %s", lo.token, exc)
        if n and self.log:
            self.log.info("[MAKER_RT][LIVE] pulled %d of our own resting order(s) on %s before unwinding "
                          "(self-trade prevention would otherwise kill the sell).", n, lo.token)
        # Deliberately NOT popping lo from open_orders: the slot accounting belongs to the fill/cancel
        # paths that own it (a pop here double-decrements caps on a full fill and strands the slot on a
        # partial). poll_open_orders sees the now-terminal order and releases it.
        return n

    @staticmethod
    def _self_trade_killed(res: Any) -> bool:
        """True when a venue order terminated with NOTHING filled and NOTHING left resting — the signature
        of a self-trade-prevention cancel (``taker_at_cross``), not of an empty book. Kept separate from
        'missed' so the failure detail names the real cause instead of blaming liquidity."""
        if not isinstance(res, dict):
            return False
        raw = res.get("raw") if isinstance(res.get("raw"), dict) else res
        from ...executor.kalshi_exec import fp_num
        filled = fp_num(raw, "fill_count", "filled_count") or 0.0
        remaining = fp_num(raw, "remaining_count")
        status = str(raw.get("status") or "").lower()
        return filled <= 0 and remaining is not None and remaining <= 0 and status in ("canceled", "cancelled")

    def _verified_unwind_poly(self, lo: _LiveOrder, shares: float, fill_price: float) -> dict:
        """FAK-sell the naked Poly position, RE-READ the balance, and RECONCILE-TO-FLAT: if a thin book
        only partly filled, RE-SELL the remainder (up to UNWIND_MAX_ATTEMPTS) rather than leave a
        mismatched leg. ok is True ONLY when the balance read confirms flat (fail-closed on read error)."""
        poly = getattr(self.hedger, "poly", None) or self.poly
        total_sold, px_num, px_den = 0.0, 0.0, 0.0
        to_sell, prev_remaining, remaining, sell_res = float(shares), None, None, None
        for _ in range(UNWIND_MAX_ATTEMPTS):
            if to_sell <= HEDGE_SHARE_TOL:
                break
            try:
                sell_res = poly.place_market_sell(lo.token, to_sell)
            except Exception as exc:  # noqa: BLE001
                sell_res = {"status": "error", "error": str(exc)}
            got = float((sell_res or {}).get("shares") or 0.0) if isinstance(sell_res, dict) else 0.0
            spx = (sell_res or {}).get("avg_price") if isinstance(sell_res, dict) else None
            total_sold += got
            if spx is not None and got > 0:
                px_num += float(spx) * got
                px_den += got
            try:                                                 # SOURCE OF TRUTH (not the sell response);
                # settlement is NOT instant -- poll (forcing a re-sync) so a stale PRE-sell balance can't
                # falsely read non-flat and scream unwind_FAILED. Stops as soon as it reads <= 0.5.
                if hasattr(poly, "settle_conditional_balance"):
                    remaining = poly.settle_conditional_balance(lo.token, lambda b: b <= 0.5)
                else:                                            # test doubles: plain read
                    remaining = poly.conditional_balance(lo.token)
            except Exception as exc:  # noqa: BLE001 - read failed -> UNKNOWN -> fail closed
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] position read failed for %s: %s", lo.token[:12], exc)
                remaining = None
            if remaining is None or remaining <= HEDGE_SHARE_TOL:
                break
            if prev_remaining is not None and remaining >= prev_remaining - HEDGE_SHARE_TOL:
                break                                            # a re-sell made NO progress -> stop hammering
            prev_remaining, to_sell = remaining, float(remaining)
        sell_px = (px_num / px_den) if px_den else None
        flat = remaining is not None and remaining <= HEDGE_SHARE_TOL
        if not flat and total_sold > 0 and self.log:
            self.log.error("[MAKER_RT][LIVE] partial unwind NOT reconciled to flat on %s: sold %.2f, "
                           "remaining %s.", lo.token[:12], total_sold, remaining)
        cost = round((float(fill_price) - float(sell_px)) * total_sold, 4) \
            if (sell_px is not None and total_sold > 0) else None
        return {"ok": bool(flat), "sold": total_sold, "sell_px": sell_px, "cost": cost,
                "remaining": remaining, "sell_res": sell_res}

    def _verified_unwind_kalshi(self, lo: _LiveOrder, shares: float, fill_price: float) -> dict:
        """IOC-sell the naked Kalshi position, REST-verify FLAT via the portfolio positions endpoint, and
        RECONCILE-TO-FLAT: re-sell the remainder (up to UNWIND_MAX_ATTEMPTS) rather than leave a
        mismatched leg on a thin/moving book."""
        total_sold, px_num, px_den = 0.0, 0.0, 0.0
        # FLOOR, never round: Kalshi fills fractional counts but takes whole contracts on an order, and
        # rounding 114.6 UP to 115 would SELL A CONTRACT WE DO NOT HOLD (an opening short, not an unwind).
        # The sub-contract remainder is dust — below the smallest order the venue will accept.
        to_sell, prev_remaining, remaining, sell_res = int(math.floor(float(shares))), None, None, None
        tol = MIN_TRADABLE["kalshi"]                        # < 1 contract == as flat as we can get
        for _ in range(UNWIND_MAX_ATTEMPTS):
            if to_sell <= 0:
                break
            try:
                sell_res = self.kalshi.place_market_sell(lo.token, lo.kalshi_side, to_sell)
            except Exception as exc:  # noqa: BLE001
                sell_res = {"status": "error", "error": str(exc)}
            if self._self_trade_killed(sell_res) and self.log:
                self.log.error("[MAKER_RT][LIVE] unwind sell on %s was KILLED by self-trade prevention "
                               "(0 filled, 0 resting) — one of our own orders is still on this book.",
                               lo.token)
            got = float((sell_res or {}).get("fill_count") or 0.0) if isinstance(sell_res, dict) else 0.0
            spx = (sell_res or {}).get("avg_price") if isinstance(sell_res, dict) else None
            total_sold += got
            if spx is not None and got > 0:
                px_num += float(spx) * got
                px_den += got
            try:
                remaining = self._settle_kalshi_flat(lo.token)   # SOURCE OF TRUTH — portfolio positions
            except Exception as exc:  # noqa: BLE001 - read failed -> UNKNOWN -> fail closed
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] kalshi position read failed for %s: %s", lo.token, exc)
                remaining = None
            if remaining is None or abs(remaining) < tol:
                break
            if prev_remaining is not None and abs(remaining) >= abs(prev_remaining) - HEDGE_SHARE_TOL:
                break                                            # a re-sell made NO progress -> stop hammering
            prev_remaining, to_sell = remaining, int(math.floor(abs(remaining)))
        sell_px = (px_num / px_den) if px_den else None
        flat = remaining is not None and abs(remaining) < tol
        if not flat and total_sold > 0 and self.log:
            self.log.error("[MAKER_RT][LIVE] partial kalshi unwind NOT reconciled to flat on %s: sold "
                           "%.0f, remaining %s.", lo.token, total_sold, remaining)
        cost = round((float(fill_price) - float(sell_px)) * total_sold, 4) \
            if (sell_px is not None and total_sold > 0) else None
        return {"ok": bool(flat), "sold": total_sold, "sell_px": sell_px, "cost": cost,
                "remaining": remaining, "sell_res": sell_res}

    def _kalshi_position(self, ticker: str) -> Optional[float]:
        """Net |contracts| held on ``ticker`` from the Kalshi portfolio positions endpoint.

        Returns None when the read is UNUSABLE (endpoint error, or a row we found but cannot parse) and
        0.0 only when the account is genuinely flat. That distinction is the whole guard: the old version
        returned 0.0 for "ticker absent" AND for "field name unrecognized", so a v2 payload using the
        ``_fp`` count names read as FLAT and reconciliation happily pruned real naked positions off the
        watch-set instead of screaming. Counts go through fp_num (v1 bare + v2 _fp)."""
        from ...executor.kalshi_exec import fp_num
        resp = self.kalshi.get_positions()
        rows = resp.get("market_positions") if isinstance(resp, dict) else (resp or [])
        if not rows and isinstance(resp, dict):
            rows = resp.get("positions") or resp.get("data") or []
        for p in rows or []:
            if not isinstance(p, dict):
                continue
            if p.get("ticker") == ticker or p.get("market_ticker") == ticker:
                n = fp_num(p, "position", "net_position", "count", "market_position")
                if n is None:
                    if self.log:                     # found the row but cannot read it -> NOT flat
                        self.log.error("[MAKER_RT][LIVE] unreadable Kalshi position row for %s: %s",
                                       ticker, sorted(p)[:12])
                    return None
                return abs(n)
        return 0.0                                    # absent from the positions list == flat

    def _settle_kalshi_flat(self, ticker: str, *, tries: int = 4, poll_s: float = 0.5) -> Optional[float]:
        """Poll the Kalshi position until it reads flat (< 1 contract, the smallest order the venue takes)
        or ``tries`` exhausted — mirrors the Poly settle poll so a brief post-sell lag can't falsely scream
        unwind_FAILED. Returns the last read."""
        import time as _time
        last: Optional[float] = None
        for i in range(max(1, tries)):
            try:
                last = self._kalshi_position(ticker)
            except Exception:  # noqa: BLE001
                last = None
            if last is not None and abs(last) < MIN_TRADABLE["kalshi"]:
                return last
            if i < tries - 1:
                _time.sleep(poll_s)
        return last

    def _orphan(self, lo: _LiveOrder, remaining: Any, sell_res: Any, now: Any) -> None:
        self._orphan_detected(lo.game, lo.token, lo.phase, remaining, f"unwind_FAILED sell_res={sell_res}", now)

    def _orphan_detected(self, game: str, token: str, phase: str, remaining: Any, detail: str,
                         now: Any) -> None:
        """A naked ORPHAN (unwind not confirmed flat, or a reconciliation mismatch). HALT ALL live quoting,
        cancel opens, SCREAM (CRITICAL Telegram). Latched until manually cleared. The red panel banner +
        the 5-min reconciliation loop surface it — this is what makes the class self-detecting."""
        if self.orphan is not None:                          # already latched -> don't re-scream every loop
            return
        # LAST-LINE GUARD: before ANY halt, re-verify the position against the venue and match it against
        # the EXPECTED (rest + hedge) legs we hold on purpose. If the venue holds no more than we expect,
        # this is not an orphan — log INFO and continue. (Callers already subtract expected; this catches
        # any path that reaches the halt with a fully-explained holding, e.g. our own hedge.)
        if self._reverify_explained(token):
            if self.log:
                self.log.info("[MAKER_RT][LIVE] orphan candidate %s [%s] re-verified against the venue as "
                              "an EXPECTED hedge/rest leg (hold %g) — NOT halting. %s",
                              str(token)[:24], phase, self._expected_shares_any(token), detail)
            return
        try:
            detected = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            detected = ""
        self.orphan = {"game": game, "token": token, "remaining": remaining, "phase": phase,
                       "detected": detected, "detail": detail}
        # ORDER MATTERS. Halt + persist + log FIRST; Telegram LAST and never load-bearing. Telegram
        # returned 429 (rate limit) 1,682 times in the incident window — if the scream came first and
        # raised, the halt would never land. _send_telegram also swallows its own errors.
        self.caps.halted = True
        self.caps.halt_reason = "orphan_position"
        self._persist_orphan()                               # survives restart + drives the panel banner
        self.cancel_all("orphan_halt", now)
        # TECHNICAL detail (ticker/token/remaining) -> LOG ONLY. Telegram gets a PLAIN, actionable line.
        if self.log:
            self.log.error("[MAKER_RT][LIVE] ORPHAN POSITION %s [%s] token %s remaining=%s (%s) — HALTED.",
                           game, phase, token, remaining, detail)
        self._send_telegram(alerts.format_event(
            "halted", detail=("Trading is PAUSED — one position needs a human to check. "
                              "What to check: confirm both venues are flat or fully hedged, then delete "
                              "data/ops/maker_rt_ORPHAN.json to resume.")))

    def _persist_orphan(self) -> None:
        """Write the latched orphan to disk (atomic). The panel reads this file, so the red ORPHAN banner
        appears even if every alert channel is down, and a restart re-latches the halt."""
        import json
        if not self._orphan_path or self.orphan is None:
            return
        try:
            mrt_config.assert_writable(self._orphan_path)   # never write live state under pytest
            tmp = self._orphan_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.orphan, fh, default=str)
            os.replace(tmp, self._orphan_path)
        except Exception as exc:  # noqa: BLE001 — persistence must never crash the halt path
            if self.log:
                self.log.error("[MAKER_RT][LIVE] could not persist ORPHAN banner: %s", exc)

    def _load_orphan(self) -> None:
        """Re-latch a persisted orphan at startup: an unresolved naked position must NOT be cleared by a
        restart. The latch is marked ``pending_verify`` so ``verify_latched_orphan`` can retire it against
        VENUE TRUTH — see that method for why an un-retirable latch is an availability bug."""
        import json
        if not self._orphan_path or not os.path.exists(self._orphan_path):
            return
        try:
            with open(self._orphan_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data:
                data["pending_verify"] = True     # re-check against the venue before trusting it forever
                self.orphan = data
                self.caps.halted = True
                self.caps.halt_reason = "orphan_position"
                if self.log:
                    self.log.error("[MAKER_RT][LIVE] STARTUP: persisted ORPHAN re-latched — live HALTED "
                                   "(pending venue verification). %s", data)
        except Exception:  # noqa: BLE001
            pass

    def _clear_orphan(self, why: str, now: Any = None) -> None:
        """Retire a latched orphan that VENUE TRUTH says is not a position, and resume live quoting.
        The file is renamed (never silently deleted) so the incident stays auditable."""
        data = self.orphan
        self.orphan = None
        if self.caps.halt_reason == "orphan_position":
            self.caps.halted = False
            self.caps.halt_reason = None
        try:
            if self._orphan_path and os.path.exists(self._orphan_path):
                os.replace(self._orphan_path, self._orphan_path + ".verified_flat.bak")
        except Exception:  # noqa: BLE001 — clearing state must never crash the loop
            pass
        if self.log:                                      # ticker/token stays LOG-ONLY (plain-language rule)
            self.log.warning("[MAKER_RT][LIVE] ORPHAN CLEARED (%s) — venue reads FLAT; live RESUMED. %s",
                             why, data)
        self._send_telegram("🟢 RESUMED · the stray position I was paused over is gone from the exchange "
                            "(it settled while I was away). I checked with the exchange and I'm holding "
                            "nothing, so I'm trading again.")

    def verify_latched_orphan(self, now: Any = None) -> bool:
        """Re-check a ``pending_verify`` orphan against the venue and CLEAR it when we are provably flat.

        WHY THIS EXISTS: an orphan latch survives restarts on purpose, but nothing used to retire it, and
        ``reconcile_positions`` returns early whenever ``self.orphan`` is set — so a latch could never be
        re-examined by the process. On 2026-07-28 the PHIMIA leg SETTLED at 01:56Z (+$8.16, fully hedged);
        the 02:06Z restart re-latched the orphan written at 01:21Z mid-chain and the bot stayed halted and
        idle for ~11h with healthy feeds. A settled/flat instrument is NOT a naked position.

        FAILS CLOSED: an unreadable position (network/auth error) leaves the halt in place. Returns True
        when the orphan was cleared."""
        o = self.orphan
        if not isinstance(o, dict) or not o.get("pending_verify"):
            return False
        inst = o.get("token")
        if not inst:
            return False
        held = self._instrument_shares(inst)
        if held is None:                                  # unreadable -> keep the halt (fail closed)
            return False
        if held > HEDGE_SHARE_TOL:                        # genuinely still holding -> confirmed orphan
            o.pop("pending_verify", None)
            if self.log:
                self.log.error("[MAKER_RT][LIVE] latched ORPHAN CONFIRMED by venue: %s holds %.2f sh — "
                               "live stays HALTED.", inst, held)
            return False
        self._clear_orphan(f"{inst} reads flat ({held:.2f} sh)", now)
        return True

    def _instrument_shares(self, inst: str) -> Optional[float]:
        """Shares actually held for an instrument, from whichever venue owns it. A Kalshi ticker is
        alphanumeric/dashed; a Poly token id is a long decimal string. Returns None if unreadable."""
        is_poly = str(inst).isdigit()
        try:
            if is_poly:
                bal = self.poly.conditional_balance(inst)
                return None if bal is None else abs(float(bal))
            pos = self._kalshi_position(inst)
            return None if pos is None else abs(float(pos))
        except Exception:  # noqa: BLE001 — unreadable -> caller keeps the halt
            return None

    # -- un-closable dust ----------------------------------------------------
    def _book_dust(self, lo: _LiveOrder, shares: float, fill_price: float, locked: Optional[float],
                   ok_outcome: str, now: Any, hedge_oid: Any) -> dict:
        """Book a naked remainder too small for the venue to price. NOT a loss and NOT an orphan: it is an
        open position we are physically unable to close, so it rides to settlement and is booked there at
        venue truth. Registered as an EXPECTED leg (so reconciliation stays quiet) and marked provisional
        at cost so the settlement correction knows what it replaced."""
        self._register_expected(lo.rest_venue, lo.token, lo.kalshi_side or lo.side, shares,
                                lo.game, lo.market_key, now)
        self._track_rested(lo)
        self._mark_provisional(lo, shares, fill_price, 0.0, now, "dust")
        self._record_lo(lo, ok_outcome, now, price=fill_price, size=shares, locked_net=locked,
                        unwind_cost=0.0, reason="dust_below_venue_minimum")
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] %s left %.2f sh on %s — below the %g the venue will price, "
                             "so there is no closing order to place. Booked as dust; it settles on its own.",
                             ok_outcome, float(shares), lo.token, MIN_TRADABLE.get(lo.rest_venue, 1.0))
        return {"outcome": ok_outcome, "locked_net": locked, "pnl": 0.0, "hedge_order_id": hedge_oid,
                "chain": self._chain(lo, shares, fill_price, f"{ok_outcome}(dust)", locked, 0.0, hedge_oid)}

    # -- provisional marks: worst-case now, VENUE TRUTH when it resolves -------
    def _mark_provisional(self, lo: _LiveOrder, shares: Any, fill_price: float, booked_pnl: float,
                          now: Any, reason: str) -> None:
        """Record that we booked ``booked_pnl`` for a position that is STILL OPEN.

        A worst-case mark is the right thing to do the instant an unwind fails — we do not know the
        outcome and must not flatter the day. It is the WRONG thing to leave in the ledger forever. On
        2026-07-28 the CSKA leg was booked at -$25.19 (the whole notional) and the position then closed
        for -$0.27: a 92x overstatement that sat in the daily pnl. Everything marked here is revisited by
        ``settle_provisional_marks`` and rebooked at what the venue actually paid."""
        try:
            ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            ts = ""
        self._provisional[lo.token] = {
            "venue": lo.rest_venue, "instrument": lo.token, "game": lo.game, "sport": lo.sport,
            "market_key": lo.market_key, "side": lo.kalshi_side or lo.side, "teams": lo.teams,
            "shares": float(shares or 0.0), "price": float(fill_price), "booked_pnl": float(booked_pnl),
            "day": getattr(self.caps, "day", "") or self._day, "opened": ts, "reason": reason,
            "since_ts": _epoch(now),
        }
        self._persist_provisional()

    def _persist_provisional(self) -> None:
        import json
        if not self._provisional_path:
            return
        try:
            mrt_config.assert_writable(self._provisional_path)
            tmp = self._provisional_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(list(self._provisional.values()), fh, default=str)
            os.replace(tmp, self._provisional_path)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never crash the loop
            if self.log:
                self.log.error("[MAKER_RT][LIVE] could not persist provisional marks: %s", exc)

    def _load_provisional(self) -> None:
        import json
        if not self._provisional_path or not os.path.exists(self._provisional_path):
            return
        try:
            with open(self._provisional_path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            for r in rows or []:
                if isinstance(r, dict) and r.get("instrument"):
                    self._provisional[r["instrument"]] = r
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] could not load provisional marks: %s", exc)

    def settle_provisional_marks(self, now: Any) -> list:
        """Rebook every provisional mark whose position has CLOSED or SETTLED, at the venue's own number.

        THE RULE: a position booked at a worst-case mark is booked at its ACTUAL venue outcome as soon as
        that outcome exists — whether it got there by settling, by the bot flattening it, or by a human
        cashing it out by hand. Only positions still open keep the conservative mark.

        The correction row carries the DELTA (actual - provisional) so the daily/lifetime pnl converges on
        truth without double-counting. Runs on the reconcile cadence and never raises into the loop."""
        if not self._provisional:
            return []
        out: list = []
        for inst, m in list(self._provisional.items()):
            try:
                held = self._instrument_shares(inst)
            except Exception:  # noqa: BLE001
                continue
            if held is None:                       # unreadable -> leave the mark, retry next pass
                continue
            if held >= MIN_TRADABLE.get(m.get("venue") or "kalshi", 1.0):
                continue                           # still open -> the conservative mark stands
            actual = self._venue_realized(m)
            if actual is None:                     # closed but the venue has not shown us the money yet
                continue
            booked = float(m.get("booked_pnl") or 0.0)
            delta = round(float(actual) - booked, 4)
            # TWO different books, two different numbers, no double count. ``caps.pnl_today`` already
            # carries the provisional, so it gets the DELTA (it converges on truth). The lifetime realized
            # ledger never saw this position at all — a naked leg produces no hedged trade_settled row —
            # so it gets the FULL venue-truth number, in the untracked bucket where naked outcomes belong.
            self.caps.adjust_pnl(delta)          # a RESTATEMENT, not a new fill — see LiveCaps.adjust_pnl
            self.persist_daily_caps()
            cost = round(abs(float(m.get("shares") or 0.0) * float(m.get("price") or 0.0)), 4)
            row = {"event": "mark_corrected", "mode": "live", "sport": m.get("sport") or "",
                   "phase": "settled", "game": m.get("game") or "", "market_key": m.get("market_key") or "",
                   "side": m.get("side") or "", "rest_venue": m.get("venue") or "",
                   "quote_price": m.get("price"), "size": round(float(m.get("shares") or 0.0), 4),
                   "realized_pnl_usd": round(float(actual), 4), "correction_usd": delta,
                   "settled_cost_usd": cost, "untracked": True,
                   "reason": (f"{inst} provisional ${booked:+.4f} ({m.get('reason')}) rebooked at VENUE "
                              f"TRUTH ${float(actual):+.4f} -> correction ${delta:+.4f}")}
            if self.state is not None:
                self.state.record(row, now)
            self._provisional.pop(inst, None)
            self._persist_provisional()
            self._forget_instrument(inst)
            out.append(row)
            if self.log:
                self.log.warning("[MAKER_RT][SETTLE] %s", row["reason"])
            self._send_telegram(alerts.format_event(
                "corrected", pnl=float(actual), was=booked,
                name=alerts.bet_name(m.get("sport"), m.get("game"), m.get("market_key"),
                                     m.get("side"), m.get("teams", ""))))
        return out

    def _venue_realized(self, mark: dict) -> Optional[float]:
        """The ACTUAL realized dollars for a closed/settled instrument, from the venue's own fills (plus
        settlement revenue if it settled while held). None when the venue cannot tell us yet."""
        if (mark.get("venue") or "kalshi") != "kalshi" or self.kalshi is None:
            return None                            # Poly redemption is inferred by the settle reconciler
        inst = mark.get("instrument")
        try:
            fills = self.kalshi.get_fills(ticker=inst)
        except Exception:  # noqa: BLE001
            return None
        if not fills:
            return None
        settlement = None
        try:
            resp = self.kalshi.get_settlements(limit=200)
            rows = resp.get("settlements") if isinstance(resp, dict) else resp
            settlement = next((s for s in (rows or []) if isinstance(s, dict) and s.get("ticker") == inst),
                              None)
        except Exception:  # noqa: BLE001 — a missing settlement read just means "fills only"
            settlement = None
        return settle_mod.realized_from_kalshi_fills(fills, settlement)

    def _forget_instrument(self, inst: Any) -> None:
        """Stop watching a resolved instrument (watch-set + expected legs)."""
        self._traded_tickers.discard(inst)
        self._traded_tokens.discard(inst)
        for venue in ("kalshi", "polymarket"):
            self._expected.pop(self._exp_key(venue, inst), None)
        self._persist_traded_tokens()
        self._persist_expected_positions()

    # -- bounded AUTO-FLATTEN -------------------------------------------------
    def _auto_flatten_orphan(self, lo: _LiveOrder, shares: Any, fill_price: float,
                             now: Any) -> Optional[float]:
        """Clear a small orphan ourselves instead of halting the whole bot for a human.

        A halt is the correct response to an exposure we cannot bound. It is a terrible response to eleven
        cents: the CSKA position cost $25.19 and the bot stopped trading for three hours over it. So we
        bound the exposure FIRST — worst case for a long position is its full notional, which is what we
        would lose if the flatten sold at zero — and if that worst case is at or under
        ``live.auto_flatten_max_usd`` we sweep it out at a capped price, PROVE flat against the venue, and
        resume. Anything larger still halts exactly as before.

        Fails CLOSED: if the sweep does not leave us provably flat, we halt. Returns the REALIZED pnl of
        the flatten (negative = it cost us) when the position is now provably flat, else None."""
        cap = float(self.auto_flatten_max_usd or 0.0)
        if cap <= 0:
            return None
        n = float(shares or 0.0)
        exposure = round(n * float(fill_price), 4)          # worst case: it sells for nothing
        if exposure > cap:
            if self.log:
                self.log.error("[MAKER_RT][LIVE] orphan on %s is $%.2f at risk — above the $%.2f "
                               "auto-flatten ceiling; HALTING for a human.", lo.token, exposure, cap)
            return None
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] orphan on %s bounded at $%.2f (<= $%.2f) — auto-flattening.",
                             lo.token, exposure, cap)
        u = self._verified_unwind(lo, n, fill_price)
        if not u.get("ok"):
            if self.log:
                self.log.error("[MAKER_RT][LIVE] AUTO-FLATTEN FAILED on %s (remaining=%s) — HALTING.",
                               lo.token, u.get("remaining"))
            return None
        cost = float(u.get("cost") or 0.0)                  # (entry - exit) x sold; negative == we gained
        if u.get("sold", 0) > 0 and u.get("sell_px") is not None:
            self.caps.commit_stake(float(u["sell_px"]) * float(u["sold"]))
        self._send_telegram(alerts.format_event(
            "auto_flattened", name=self._name_for(lo), cost=cost, sold=float(u.get("sold") or 0.0)))
        return round(-cost, 4)

    def _load_traded_tokens(self) -> None:
        """Reload the persisted watch-set at startup (best-effort; a corrupt/locked file never blocks
        startup). This is what lets the startup reconcile catch a position stranded by a crashed run."""
        import json
        if not self._traded_path or not os.path.exists(self._traded_path):
            return
        try:
            with open(self._traded_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):                       # legacy bare list = poly tokens only
                self._traded_tokens.update(str(t) for t in data if t)
            elif isinstance(data, dict):
                self._traded_tokens.update(str(t) for t in (data.get("tokens") or []) if t)
                self._traded_tickers.update(str(t) for t in (data.get("tickers") or []) if t)
        except Exception:  # noqa: BLE001
            pass

    def _persist_traded_tokens(self) -> None:
        """Atomically write the watch-set (poly tokens + kalshi tickers). Best-effort: persistence must
        NEVER crash live trading."""
        import json
        if not self._traded_path:
            return
        try:
            mrt_config.assert_writable(self._traded_path)   # never write live state under pytest
            tmp = self._traded_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"tokens": sorted(self._traded_tokens),
                           "tickers": sorted(self._traded_tickers)}, fh)
            os.replace(tmp, self._traded_path)
        except Exception:  # noqa: BLE001
            pass

    # -- settled-P&L cost basis + reconciliation -----------------------------
    _LEG_SEP = "\x1f"                                    # (game, market_key) -> one JSON string key

    def _note_pair_legs(self, lo: _LiveOrder, rest_shares: float, rest_price: float,
                        hedge_shares: Any, hedge_avg: Any, hedge_fee: Any = 0.0) -> None:
        """Accumulate the FEE-HONEST COST BASIS of a hedged pair (rest leg + hedge leg), keyed by
        (game, market_key), for the settled-pnl reconciler. The rest leg is a MAKER order (fee 0 on both
        venues for our series); the hedge is a TAKER lift whose ACTUAL fee is added here so settled net
        is not ~1 fee optimistic. Multi-fill markets accumulate. Best-effort — never blocks the hedge."""
        try:
            hl = lo.hedge_lookup or {}
            rest_cost = float(rest_shares) * float(rest_price)                       # maker leg: fee 0
            hedge_cost = float(hedge_shares or 0.0) * float(hedge_avg or 0.0) + float(hedge_fee or 0.0)
            if lo.rest_venue == "kalshi":
                k_ticker, k_side, k_sh, k_cost = lo.token, (lo.kalshi_side or "yes"), float(rest_shares), rest_cost
                p_token, p_sh, p_cost = hl.get("token"), float(hedge_shares or 0.0), hedge_cost
            else:
                k_ticker, k_side = hl.get("ticker"), str(hl.get("side") or "yes")
                k_sh, k_cost = float(hedge_shares or 0.0), hedge_cost
                p_token, p_sh, p_cost = lo.token, float(rest_shares), rest_cost
            key = f"{lo.game}{self._LEG_SEP}{lo.market_key}"
            rec = self._market_legs.get(key)
            if rec is None:
                rec = {"sport": lo.sport, "game": lo.game, "market_key": lo.market_key,
                       "side": lo.side, "teams": getattr(lo, "teams", ""), "rest_venue": lo.rest_venue,
                       "kalshi": {"ticker": k_ticker, "side": k_side, "shares": 0.0, "cost": 0.0},
                       "poly": {"token": p_token, "shares": 0.0, "cost": 0.0}}
                self._market_legs[key] = rec
            rec["kalshi"].update(ticker=k_ticker, side=k_side)
            rec["kalshi"]["shares"] = round(rec["kalshi"]["shares"] + k_sh, 4)
            rec["kalshi"]["cost"] = round(rec["kalshi"]["cost"] + k_cost, 4)
            rec["poly"]["token"] = p_token
            rec["poly"]["shares"] = round(rec["poly"]["shares"] + p_sh, 4)
            rec["poly"]["cost"] = round(rec["poly"]["cost"] + p_cost, 4)
            self._persist_settled_ledger()
        except Exception:  # noqa: BLE001 — cost-basis bookkeeping must never crash the hedge
            pass

    # -- expected positions (rest legs + live hedges we HOLD on purpose) ------
    @staticmethod
    def _exp_key(venue: str, instrument: Any) -> str:
        return f"{str(venue)}\x1f{str(instrument)}"

    def _register_expected(self, venue: str, instrument: Any, side: Any, shares: float,
                           game: str, market_key: str, now: Any) -> None:
        """Record that we HOLD ``shares`` of ``instrument`` on ``venue`` on purpose (a filled rest leg or
        a live hedge). Accumulates across multi-fill markets. Persisted so a settlement/reconcile after a
        restart still recognises it. Best-effort — never blocks the hedge path."""
        if not instrument or float(shares or 0.0) <= 0.0:
            return
        try:
            ts = now.strftime("%Y-%m-%dT%H:%M:%SZ") if now is not None else ""
        except Exception:  # noqa: BLE001
            ts = ""
        k = self._exp_key(venue, instrument)
        rec = self._expected.get(k)
        if rec is None:
            rec = {"venue": str(venue), "instrument": str(instrument), "side": str(side or ""),
                   "shares": 0.0, "game": game, "market_key": market_key, "ts": ts}
            self._expected[k] = rec
        rec["shares"] = round(float(rec["shares"]) + float(shares), 4)
        rec["game"], rec["market_key"], rec["ts"] = game, market_key, ts
        self._persist_expected_positions()

    def _note_expected_legs(self, lo: _LiveOrder, rest_shares: float, hedge_shares: Any,
                            hedge_venue: str, now: Any) -> None:
        """Register BOTH legs of a freshly-locked pair as expected positions: the rest leg we just filled
        AND the hedge leg we just lifted. Both are held until the market settles."""
        try:
            hl = lo.hedge_lookup or {}
            self._register_expected(lo.rest_venue, lo.token, (lo.kalshi_side or lo.side),
                                    rest_shares, lo.game, lo.market_key, now)
            if hedge_venue == "polymarket":
                self._register_expected("polymarket", hl.get("token"), "buy",
                                        float(hedge_shares or 0.0), lo.game, lo.market_key, now)
            else:
                self._register_expected("kalshi", hl.get("ticker"), hl.get("side", "yes"),
                                        float(hedge_shares or 0.0), lo.game, lo.market_key, now)
        except Exception:  # noqa: BLE001 — expected-position bookkeeping must never crash the hedge
            pass

    def _expected_shares(self, venue: str, instrument: Any) -> float:
        """Shares we EXPECT to hold on (venue, instrument) — 0.0 if none registered."""
        rec = self._expected.get(self._exp_key(venue, instrument))
        return float(rec["shares"]) if rec else 0.0

    def _expected_any(self, instrument: Any) -> Optional[dict]:
        """The expected record for ``instrument`` on EITHER venue (the re-verify guard only has the
        instrument id, not the venue), or None."""
        s = str(instrument)
        for rec in self._expected.values():
            if rec.get("instrument") == s and float(rec.get("shares") or 0.0) > 0.0:
                return rec
        return None

    def _expected_shares_any(self, instrument: Any) -> float:
        rec = self._expected_any(instrument)
        return float(rec.get("shares") or 0.0) if rec else 0.0

    def _reverify_explained(self, instrument: Any) -> bool:
        """Re-read the venue position for ``instrument`` and return True iff we hold NO MORE than the
        expected (rest + hedge) shares booked for it — i.e. the position is fully explained and is NOT an
        orphan. A missing expected record, a read failure, or an unreadable position -> False (fail
        CLOSED: proceed to halt). This guard can only ever PREVENT a halt for a genuinely-held leg."""
        rec = self._expected_any(instrument)
        if rec is None:
            return False
        exp = float(rec.get("shares") or 0.0)
        venue = rec.get("venue")
        try:
            if venue == "kalshi":
                pos = self._kalshi_position(instrument)
            elif self.poly is not None:
                pos = self.poly.conditional_balance(instrument)
            else:
                return False
        except Exception:  # noqa: BLE001 — cannot re-read == cannot prove explained == fail closed
            return False
        if pos is None:
            return False
        return abs(float(pos)) - exp <= HEDGE_SHARE_TOL

    def _forget_settled_instruments(self, rec: Optional[dict]) -> None:
        """A market has SETTLED -> stop watching BOTH its legs for nakedness. The winning leg redeemed to
        cash (balance -> 0) and the LOSING leg is now WORTHLESS but its token/contract balance can remain
        non-zero in the wallet (Polymarket does not auto-burn a losing outcome token). Left in the
        watch-set, that worthless leg reads as a naked position on the NEXT reconcile and false-orphans
        the bot (exactly the stranded HANHAL hanfmann leg). Once the trade is booked in settled pnl,
        neither leg carries risk, so forget both. Best-effort."""
        if not isinstance(rec, dict):
            return
        tk = (rec.get("kalshi") or {}).get("ticker")
        tok = (rec.get("poly") or {}).get("token")
        changed = False
        if tk and tk in self._traded_tickers:
            self._traded_tickers.discard(tk)
            changed = True
        if tok and tok in self._traded_tokens:
            self._traded_tokens.discard(tok)
            changed = True
        if changed:
            self._persist_traded_tokens()

    def _prune_expected(self, game: str, market_key: str) -> None:
        """Drop every expected leg of a market that has SETTLED (its positions redeemed to cash)."""
        drop = [k for k, r in self._expected.items()
                if r.get("game") == game and r.get("market_key") == market_key]
        for k in drop:
            self._expected.pop(k, None)
        if drop:
            self._persist_expected_positions()

    def _load_expected_positions(self) -> None:
        """Reload the expected-position registry at startup (best-effort). This is what lets a settlement
        or reconcile landing after a restart still recognise a leg we hold on purpose."""
        import json
        if not self._expected_path or not os.path.exists(self._expected_path):
            return
        try:
            with open(self._expected_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k, r in data.items():
                    if isinstance(r, dict) and r.get("instrument"):
                        self._expected[str(k)] = r
        except Exception:  # noqa: BLE001
            pass

    def _persist_expected_positions(self) -> None:
        """Atomically write the expected-position registry. Best-effort: persistence must NEVER crash
        live trading."""
        import json
        if not self._expected_path:
            return
        try:
            mrt_config.assert_writable(self._expected_path)   # never write live state under pytest
            tmp = self._expected_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._expected, fh)
            os.replace(tmp, self._expected_path)
        except Exception:  # noqa: BLE001
            pass

    def reconcile_settlements(self, now: Any) -> list:
        """Pull BOTH venues' settlement for our booked hedged pairs and write the venue-truth
        ``trade_settled`` rows (net + ROI, both legs). Idempotent; prunes reconciled markets from the
        local ledger + alerts each settled trade. Best-effort — never raises into the loop."""
        if not self._market_legs:
            return []
        try:
            emitted = self._settle_reconciler.reconcile(list(self._market_legs.values()), now)
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[MAKER_RT][SETTLE] reconcile pass failed: %s", exc)
            return []
        if emitted:
            recs = {}
            for key in list(self._market_legs):
                game, _, mk = key.partition(self._LEG_SEP)
                if self._settle_reconciler.already_settled(game, mk):
                    rec = self._market_legs.pop(key, None)
                    recs[(game, mk)] = rec
                    self._prune_expected(game, mk)            # legs redeemed to cash -> no longer held
                    self._forget_settled_instruments(rec)     # ...and stop watching them for nakedness
            self._persist_settled_ledger()
            for row in emitted:
                if self.log:                                  # keep the TECHNICAL reason in the log only
                    self.log.warning("[MAKER_RT][SETTLE] %s", row.get("reason"))
                self._send_telegram(self._settled_alert(row, recs.get((row.get("game"),
                                                                       row.get("market_key")))))
        return emitted

    def _settled_alert(self, row: dict, rec: Optional[dict]) -> str:
        """Render the plain-language SETTLED alert (no ticker) from the settled row + booked leg meta."""
        rec = rec or {}
        sport = rec.get("sport") or row.get("sport")
        side, teams, rest_venue = rec.get("side"), rec.get("teams", ""), rec.get("rest_venue")
        our_bet = alerts.bet_name(sport, row.get("game"), rec.get("market_key") or row.get("market_key"),
                                  side, teams)
        winner_venue = row.get("winner_venue")
        we_won = winner_venue is not None and winner_venue == rest_venue
        winner = our_bet if we_won else alerts.other_name(our_bet, teams)
        life = 0.0
        if self.state is not None:
            life = float(getattr(self.state, "settled_pnl_lifetime", 0.0) or 0.0)
        return alerts.format_event("settled", sport=sport, game=row.get("game"),
                                   market_key=rec.get("market_key") or row.get("market_key"),
                                   side=side, teams=teams, winner=winner,
                                   payout=row.get("settled_revenue_usd"), cost=row.get("settled_cost_usd"),
                                   pnl=row.get("realized_pnl_usd"),
                                   roi_pct=(float(row.get("roi") or 0.0) * 100.0),
                                   lifetime_pnl=round(life, 4))

    def _live_ctx(self) -> dict:
        """Daily-usage + lifetime context for the rich PLACED/LOCKED alerts (open slots, fills, $ used,
        today pnl, and lifetime = settled realized + today's locked estimate)."""
        life = float(getattr(self.state, "settled_pnl_lifetime", 0.0) or 0.0) if self.state is not None else 0.0
        return {"open_now": len(self.open_orders), "max_open": self.caps.max_open_quotes,
                "fills_today": self.caps.fills_today, "stake_today": round(self.caps.stake_today, 2),
                "stake_cap": self.caps.max_daily_stake_usd, "today_pnl": round(self.caps.pnl_today, 4),
                "lifetime_pnl": round(life + self.caps.pnl_today, 4)}

    def _persist_settled_ledger(self) -> None:
        import json
        if not self._settled_path:
            return
        try:
            mrt_config.assert_writable(self._settled_path)
            tmp = self._settled_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._market_legs, fh)
            os.replace(tmp, self._settled_path)
        except Exception:  # noqa: BLE001
            pass

    def _load_settled_ledger(self) -> None:
        import json
        if not self._settled_path or not os.path.exists(self._settled_path):
            return
        try:
            with open(self._settled_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._market_legs.update(data)
        except Exception:  # noqa: BLE001
            pass

    def reconcile_positions(self, now: Any) -> Optional[dict]:
        """Read ACTUAL positions and diff against the bot's belief (flat -- a resting order is NOT a
        position; every fill is hedged or unwound-to-flat). Any non-zero holding on a token WE TRADED
        this run is an ORPHAN. Runs at startup + every 5 min while armed. If ``auto_flatten`` it also
        market-sells the orphan to flat (default False -> halt + scream only). Returns the orphan or None.

        Scoped to ``self._traded_tokens`` ON PURPOSE: this funder wallet holds hundreds of unrelated
        positions (other bots/markets -- Bitcoin up/down, other sports, etc.). A blanket list_positions()
        sweep would flag EVERY one of those as an orphan and instantly halt live quoting on a false
        positive. Only the tokens this maker actually placed on can be a maker orphan."""
        # FIRST, and even while halted: any position we booked at a worst-case mark that has since closed
        # or settled gets rebooked at what the venue actually paid. A halted bot's ledger must still tell
        # the truth — the CSKA mark was 92x the real loss and nothing was going to correct it.
        try:
            self.settle_provisional_marks(now)
        except Exception as exc:  # noqa: BLE001 — bookkeeping never blocks reconciliation
            if self.log:
                self.log.warning("[MAKER_RT][SETTLE] provisional-mark pass failed: %s", exc)
        if self.orphan is not None:
            # A latch carried in from a previous process gets ONE venue re-check per cycle; if the
            # instrument reads flat (e.g. it settled while we were down) the halt is retired and this
            # reconcile continues normally. A confirmed or unreadable latch still short-circuits.
            if not self.verify_latched_orphan(now):
                return self.orphan
        open_toks = {lo.token for lo in self.open_orders.values()}
        suspects: dict = {}
        flat_toks: list = []
        for tok in list(self._traded_tokens):                # POLY: reliable per-token read (CLOB), maker-scoped
            try:
                bal = self.poly.conditional_balance(tok)
            except Exception:  # noqa: BLE001 - unknown -> keep watching (don't prune on a read failure)
                continue
            if bal is None:
                continue
            # Subtract what we EXPECT to hold (a filled rest leg / live hedge). Only the UNEXPLAINED
            # surplus is naked; a holding that matches an expected leg is not an orphan (it settles).
            unexplained = float(bal) - self._expected_shares("polymarket", tok)
            if unexplained > 0.5:
                suspects[tok] = round(unexplained, 4)
            elif bal <= 0.5 and tok not in open_toks:        # confirmed flat + not actively quoting -> drop
                flat_toks.append(tok)
        if flat_toks:                                        # bound the watch-set to open + non-flat tokens
            self._traded_tokens.difference_update(flat_toks)
            self._persist_traded_tokens()
        flat_tks: list = []
        for tk in list(self._traded_tickers):                # KALSHI: maker-scoped portfolio position read
            try:
                pos = self._kalshi_position(tk)
            except Exception:  # noqa: BLE001
                continue
            if pos is None:                                  # unreadable -> keep watching (never prune/orphan here)
                continue
            unexplained = abs(pos) - self._expected_shares("kalshi", tk)
            if unexplained > 0.5:
                suspects[tk] = round(unexplained, 4)
            elif abs(pos) <= 0.5 and tk not in open_toks:
                flat_tks.append(tk)
        if flat_tks:
            self._traded_tickers.difference_update(flat_tks)
            self._persist_traded_tokens()
        if not suspects:
            return None
        tok, bal = next(iter(suspects.items()))
        if self.auto_flatten and tok in self._traded_tokens:  # optional flatten (POLY only; kalshi needs a side)
            try:
                self.poly.place_market_sell(tok, bal)
                bal2 = self.poly.conditional_balance(tok)
                if bal2 is not None and bal2 <= 0.5:
                    if self.log:                         # token id is LOG-ONLY
                        self.log.error("[MAKER_RT][CRITICAL] auto-flattened orphan %s (%s sh) — now flat; "
                                       "halting for review anyway.", tok, bal)
                    self._send_telegram("🔴 STOPPED · I auto-closed a stray position (now flat) and paused "
                                        "trading for a human to review.")
            except Exception as exc:  # noqa: BLE001
                if self.log:
                    self.log.error("[MAKER_RT][CRITICAL] auto-flatten FAILED %s: %s", tok, exc)
                self._send_telegram("🔴 STOPPED · I tried to auto-close a stray position and it FAILED — "
                                    "it needs a human right now.")
        self._orphan_detected("reconciliation", tok, "?", bal,
                              f"{len(suspects)} orphan instrument(s); auto_flatten={self.auto_flatten}", now)
        return self.orphan

    # -- helpers -------------------------------------------------------------
    def _neg_for(self, token: str, store: Any) -> Optional[bool]:
        if token in self._neg_cache:
            return self._neg_cache[token]
        neg = None
        try:
            _tick, neg = self.poly._tick_and_negrisk(token)
        except Exception:  # noqa: BLE001 — leave neg None; PolyExec re-fetches at place time
            neg = None
        self._neg_cache[token] = neg
        return neg

    def _key_for_oid(self, oid: Any) -> Optional[tuple]:
        for key, lo in self.open_orders.items():
            if lo.order_id == oid:
                return key
        return None

    def note_implausible(self) -> None:
        """The driver rejected a quote whose computed edge exceeded the sanity ceiling (probable
        pricing/pairing bug). Count it for the panel — a rising counter means the pairing needs review."""
        self._implausible_refused += 1

    def open_count(self) -> int:
        return len(self.open_orders)

    def snapshot(self, now_ts: float = 0.0) -> dict:
        """Live state for the panel: per-phase open counts, shared stake/fills/pnl, in-play circuit, and a
        LIVE QUOTES detail list (market, phase, our price, best bid, size, age, order id)."""
        pre_open = sum(1 for lo in self.open_orders.values() if lo.phase == "pre")
        ip_open = sum(1 for lo in self.open_orders.values() if lo.phase == "inplay")
        quotes = [{"game": lo.game, "market_key": lo.market_key, "phase": lo.phase,
                   "price": round(lo.price, 4), "best_bid": lo.best_bid, "size": int(lo.size),
                   "age_s": round(now_ts - lo.placed_ts, 1) if now_ts else None,
                   "order_id": (lo.order_id[:10] + "…") if lo.order_id else None}
                  for lo in self.open_orders.values()]
        lt = sorted(self._lifetimes)
        n = len(lt)
        med = None if n == 0 else round(lt[n // 2] if n % 2 else (lt[n // 2 - 1] + lt[n // 2]) / 2.0, 1)
        atbest = round(self._atbest_hits / self._atbest_samples, 4) if self._atbest_samples else None
        return {"open_quotes": len(self.open_orders), "pre_open": pre_open, "inplay_open": ip_open,
                "stake_today": round(self.caps.stake_today, 2), "stake_cap": self.caps.max_daily_stake_usd,
                "fills_today": self.caps.fills_today, "pnl_today": round(self.caps.pnl_today, 4),
                "halted": self.caps.halted, "feed_ok": self.feed_ok,
                "inplay_fills": self.inplay_fills_today,
                "inplay_paused": bool(now_ts and now_ts < self.inplay_pause_until),
                "inplay_halted": self.inplay_halted, "orphan": self.orphan,
                # FLAPS: reconnect count + cumulative downtime per socket. A rising kalshi flap count is
                # the early warning that the fill channel is unreliable — the REST poll covers us, but
                # the operator must see it rather than infer it from the log.
                "flaps": dict(self.flaps), "flap_secs": {k: round(v, 1) for k, v in self.flap_secs.items()},
                "fill_poll_s": self.fill_poll_s,
                # SLOT HEALTH: max_open (Y in "open X/Y"), how long the longest candidate has waited for a
                # slot, and how many stale/aged slots we've reclaimed (the slot-starvation guards at work).
                "max_open": self.caps.max_open_quotes, "slot_wait_max_s": round(self.slot_wait_max_s, 1),
                "slot_released": self._slot_released, "aged_out": self._aged_out,
                "implausible_refused": self._implausible_refused,
                # EXPECTED POSITIONS: legs we hold on purpose (filled rest + live hedges) awaiting
                # settlement. A rising count with no fills is a settlement-pruning problem; it also proves
                # the false-orphan guard has something to match against.
                "expected_positions": len(self._expected),
                # BINDING CONSTRAINT tallies (cumulative): which limit set each quote's size. A
                # 'venue_minimum'-dominated tally means raising caps won't grow fills (the maker rests the
                # pilot minimum); a 'hedge_depth'/'book_depth' tally means depth is the ceiling.
                "binding_counts": dict(self._binding_counts),
                "viable_directions": sorted(self._viable_directions),
                "max_quote_age_s": self.max_quote_age_s,
                "median_quote_age_s": med, "time_at_best_share": atbest, "quotes": quotes}

    # -- CSV + telegram ------------------------------------------------------
    def _record(self, c: Any, event: str, now: Any, phase: str, *, price: float = None, size: float = None,
                hedge_ask: float = None, order_id: str = None, reason: str = "") -> None:
        if self.state is None:
            return
        self.state.record({"event": event, "mode": "live", "sport": c.sport, "phase": phase,
                           "game": c.game, "market_key": c.market_key, "side": c.rest_side,
                           "direction": c.direction, "rest_venue": c.rest_venue,
                           "hedge_venue": c.hedge_venue,
                           "quote_price": round(price, 4) if price is not None else "",
                           "size": round(size, 2) if size is not None else "",
                           "hedge_ask": round(hedge_ask, 4) if hedge_ask is not None else "",
                           "reason": reason or (order_id or "")}, now)

    def _record_lo(self, lo: _LiveOrder, event: str, now: Any, *, price: float = None, size: float = None,
                   locked_net: float = None, locked_pnl: float = None, unwind_cost: float = None,
                   hedge_avg: float = None, hedge_order_id: Any = None, reason: str = "") -> None:
        if self.state is None:
            return
        row = {"event": event, "mode": "live", "sport": lo.sport, "phase": lo.phase, "game": lo.game,
               "market_key": lo.market_key, "side": lo.side, "direction": lo.direction,
               "quote_price": round(price if price is not None else lo.price, 4),
               "size": round(size if size is not None else lo.size, 2),
               "reason": reason or lo.order_id}
        if locked_net is not None:
            row["locked_net"] = round(float(locked_net) * 100, 4)
        if hedge_avg is not None:
            row["hedge_avg"] = round(float(hedge_avg), 4)
        if hedge_order_id is not None:
            row["hedge_order_id"] = hedge_order_id
        # REALIZED pnl ($): a locked hedge's pnl, else the NEGATIVE of the unwind/orphan cost.
        realized = locked_pnl if locked_pnl is not None else (
            -float(unwind_cost) if unwind_cost is not None else None)
        if realized is not None:
            row["realized_pnl_usd"] = round(float(realized), 4)
        for k, v in (("locked_pnl", locked_pnl), ("unwind_cost", unwind_cost)):
            if v is not None:
                row[k] = v
        self.state.record(row, now)

    def _record_fill(self, lo: _LiveOrder, matched: float, fill_price: float, now: Any) -> None:
        """The head of the ledger chain: a live 'fill' row (fill -> hedge_* -> unwind|unwind_FAILED)."""
        if self.state is None:
            return
        self.state.record({"event": "fill", "mode": "live", "sport": lo.sport, "phase": lo.phase,
                           "game": lo.game, "market_key": lo.market_key, "side": lo.side,
                           "direction": lo.direction, "quote_price": round(float(fill_price), 4),
                           "size": round(float(matched), 2), "reason": lo.order_id}, now)

    # -- alerting: INSTANT (fill/hedge/unwind/pause/halt/feed/error) vs ROUTINE (quote/reprice/cancel) --
    def _send_telegram(self, text: str) -> None:
        if not self.telegram:
            return
        try:
            self.telegram(text)
        except Exception as exc:  # noqa: BLE001 — a telegram failure never blocks execution
            if self.log:
                self.log.warning("[MAKER_RT][LIVE] telegram send failed: %s", exc)

    def _instant(self, text: str) -> None:
        """A material event -> WARNING log + Telegram immediately."""
        if self.log:
            self.log.warning(text)
        self._send_telegram(text)

    _alert = _instant                                # back-compat: existing instant call sites

    # -- human alert names ---------------------------------------------------
    def _name_for(self, lo: Any) -> str:
        """'⚾ MLB · Yankees ML' for a live order — the real team/player name, never the ticker."""
        return alerts.head(lo.sport, lo.game, lo.market_key, lo.side, getattr(lo, "teams", ""))

    def _emit_event(self, kind: str, lo: Any = None, *, instant: bool, digest_kind: str = "",
                    **fields: Any) -> None:
        """Build the human alert line for ``kind`` and route it (instant Telegram vs digest bucket)."""
        base = {}
        if lo is not None:
            base = {"sport": lo.sport, "game": lo.game, "market_key": lo.market_key,
                    "side": lo.side, "teams": getattr(lo, "teams", ""),
                    "venue": lo.rest_venue, "phase": lo.phase}
        text = alerts.format_event(kind, **{**base, **fields})
        if instant:
            self._instant(text)
        else:
            self._routine(digest_kind or kind, text, reason=fields.get("reason"))

    @staticmethod
    def _digest_bucket(reason: Any) -> str:
        r = str(reason or "")
        if "reprice" in r or r in ("would_cross_at_post", "below_tick"):
            return "reprice"
        if "stale" in r:
            return "stale"
        if "thin" in r:
            return "thin"
        return "expire"

    def _routine(self, kind: str, text: str, reason: Any = None) -> None:
        """A routine event (quote/reprice/cancel/refuse): INFO log, then either count into the digest or
        (when telegram_digest_min == 0) Telegram immediately, like the old per-event behavior."""
        if self.log:
            self.log.info(text)
        if self.digest_min <= 0:
            self._send_telegram(text)
            return
        if kind == "quote":
            self._digest["quotes"] += 1
        elif kind == "cancel":
            b = self._digest_bucket(reason)
            self._digest["cancels"][b] = self._digest["cancels"].get(b, 0) + 1

    def maybe_flush_digest(self, now_ts: float) -> None:
        """Every ``telegram_digest_min`` minutes send ONE digest line.

        It is sent UNCONDITIONALLY. The digest used to be suppressed on a fully quiet interval, so a bot
        that was HALTED and idle said nothing at all — 2026-07-28 ran 05:21Z to 14:11Z (~9h) without a
        single digest while live sat halted on a stale orphan and three feed drops went unmentioned
        (only KALSHI flaps could break the silence). Silence is indistinguishable from death, and "it
        went quiet" is the one thing an operator must never have to infer."""
        if self.digest_min <= 0:
            return
        if self._digest_since == 0.0:
            self._digest_since = now_ts
            return
        if now_ts - self._digest_since < self.digest_min * 60.0:
            return
        d = self._digest
        cancelled = sum(d["cancels"].values())
        best = d.get("best_edge", 0.0)
        binding = d.get("binding") or {}
        top_binding = max(binding.items(), key=lambda kv: kv[1])[0] if binding else None
        why = None
        if d["fills"] == 0:
            if self.caps.halted:
                why = f"I am HALTED ({self.caps.halt_reason or 'unknown'}) — not quoting until that clears"
            elif not d["quotes"]:
                why = ("no market offered enough edge to be worth quoting" if self.feed_ok
                       else "my feeds were down, so I held off")
            else:
                why = ("sizes were limited by hedge/book depth" if top_binding in
                       ("hedge_depth", "book_depth", "below_venue_minimum")
                       else "nobody crossed our price yet")
        line = alerts.digest_line(self.digest_min, placed=d["quotes"], cancelled=cancelled,
                                  fills=d["fills"], open_now=len(self.open_orders),
                                  max_open=self.caps.max_open_quotes,
                                  best_edge_pct=(best if best else None),
                                  kalshi_flaps=int(d.get("kalshi_flaps", 0) or 0),
                                  kalshi_down_s=float(d.get("kalshi_down_s", 0.0)),
                                  poly_flaps=int(d.get("poly_flaps", 0) or 0),
                                  poly_down_s=float(d.get("poly_down_s", 0.0)),
                                  reconnects=dict(self._feed_health),
                                  prehedge_declines=int(d.get("prehedge_declines", 0) or 0),
                                  binding=top_binding, stake_today=round(self.caps.stake_today, 2),
                                  stake_cap=self.caps.max_daily_stake_usd,
                                  today_pnl=round(self.caps.pnl_today, 4), why_no_fills=why)
        self._send_telegram(line)
        if self.log:
            self.log.warning(line)
        self._reset_digest()
        self._digest_since = now_ts

    def _reset_digest(self) -> None:
        self._digest = {"quotes": 0, "cancels": {}, "fills": 0, "refuse_suppressed": 0, "best_edge": 0.0,
                        "kalshi_flaps": 0, "kalshi_down_s": 0.0, "poly_flaps": 0, "poly_down_s": 0.0,
                        "prehedge_declines": 0, "binding": {}}

    def note_feed_health(self, feeds: dict) -> None:
        """Record live reconnect attempt/success totals from the feed objects for the digest."""
        for name, feed in (feeds or {}).items():
            if feed is None:
                continue
            self._feed_health[name] = (int(getattr(feed, "reconnect_attempts", 0) or 0),
                                       int(getattr(feed, "reconnect_success", 0) or 0))

    def sample_metrics(self, store: Any, now_ts: float) -> None:
        """Each loop: sample whether each LIVE quote is currently AT BEST (our price >= live best bid)."""
        for lo in self.open_orders.values():
            v = store.poly_view(lo.token)
            bb = getattr(v, "best_bid", None) if v is not None else None
            self._atbest_samples += 1
            if bb is None or lo.price >= bb - 1e-9:
                self._atbest_hits += 1

    def _record_lifetime(self, lo: _LiveOrder, now: Any) -> None:
        """A live quote closed (cancel or full fill): record how long it rested (the churn metric)."""
        try:
            age = now.timestamp() - float(lo.placed_ts)
        except Exception:  # noqa: BLE001
            return
        if age >= 0:
            self._lifetimes.append(age)
            if len(self._lifetimes) > 5000:
                self._lifetimes = self._lifetimes[-5000:]
