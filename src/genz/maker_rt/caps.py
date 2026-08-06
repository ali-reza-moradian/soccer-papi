"""PRE-GAME live caps — the hard exposure limits enforced before/around every real order.

PURE and stateful (no I/O, no clients) so it is unit-tested directly; the continuous pre-game
executor (a separate, later enable) and the ``--smoke`` order both route their sizing + pre-place
decision through here. Every limit comes from the ``maker_rt.live`` config block:

  * ``quote_usd_max``       — the REST leg's notional cap ($). ``size_shares`` never sizes above it.
  * ``max_daily_stake_usd`` — the running SUM of ALL legs committed today (rest fills + hedges +
    unwinds). ``can_place`` does a PRE-PLACE projection: if committing this quote's projected pair
    stake would push the day's total past the cap, it REFUSES and HALTS quoting for the day (and the
    caller alerts Telegram). This bounds gross money at risk independently of realized pnl.
  * ``max_open_quotes``     — at most this many resting quotes at once.
  * ``max_fills_per_day``   — at most this many real fills/day.
  * ``max_daily_loss_usd``  — realized-loss auto-halt: once ``pnl_today <= -cap`` quoting halts.

Nothing here places an order; it only decides + accounts. Fail-CLOSED: any missing config value
falls back to the safe LiveConfig default.
"""
from __future__ import annotations

import math
from typing import Any, Optional


def direction_slot_ok(direction: str, open_by_direction: dict, enabled_directions: Any,
                      max_open: int, reserve: int) -> bool:
    """PER-DIRECTION SLOT RESERVATION. Of ``max_open`` total resting slots, guarantee ``reserve`` to EACH
    enabled direction; the remainder float. Returns True iff ``direction`` may claim ONE MORE slot WITHOUT
    eating into another enabled direction's still-unclaimed reserve.

    PURE (no I/O, no state) so it is unit-tested directly. ``open_by_direction`` maps direction -> current
    open-quote count. The GLOBAL ``max_open`` cap is enforced separately (LiveCaps.can_place); this only
    layers the fairness reservation on top.

      * ``reserve <= 0``            -> disabled -> always True (today's behavior; no reservation).
      * a single enabled direction  -> no 'other' to protect -> always True (single-direction unaffected).

    Example (max_open=2, reserve=1, dirs={rest-poly, rest-kalshi}): if rest-kalshi already holds 1 and
    rest-poly holds 0, rest-kalshi is REFUSED a 2nd (poly's 1 reserved slot is protected), while rest-poly
    is allowed its slot. Once both hold 1, the global max_open cap refuses either a 3rd."""
    if reserve <= 0:
        return True
    reserved_for_others = sum(max(0, reserve - int(open_by_direction.get(d, 0)))
                              for d in enabled_directions if d != direction)
    mine = int(open_by_direction.get(direction, 0))
    return mine < max_open - reserved_for_others


#: the SIZE-limiting constraints, in tie-break priority (caps before depth), plus the refusal marker.
BINDING_CONSTRAINTS = ("quote_usd_max", "pair_cap", "game_cap", "daily_stake", "hedge_depth",
                       "book_depth")
_BINDING_ORDER = {n: i for i, n in enumerate(BINDING_CONSTRAINTS)}


def plan_size(price: float, hedge_ask: Optional[float], *, quote_usd_max: float,
              max_pair_stake_usd: float, daily_stake_headroom: float, hedge_depth: Optional[float],
              book_depth: Optional[float], venue_minimum: int,
              game_stake_headroom: Optional[float] = None) -> dict:
    """The LARGEST whole-share rest-leg size that fits EVERY constraint — the venue minimum is a FLOOR,
    NEVER a ceiling. (The old ``min(floor_min, cap)`` clamped every quote DOWN to 5 shares, so a $2.80
    fill rested against a $20 cap.) Take the smallest of the resource allowances:

      * ``quote_usd_max`` — rest-leg notional cap:            quote_usd_max / price
      * ``pair_cap``      — whole-pair stake cap:             max_pair_stake_usd / (price + hedge_ask)
      * ``game_cap``      — what THIS GAME has left:          game_stake_headroom / (price + hedge_ask)
      * ``daily_stake``   — remaining daily budget:           daily_stake_headroom / (price + hedge_ask)
      * ``hedge_depth``   — resting hedge shares at the ask:  hedge_depth
      * ``book_depth``    — rest-book liquidity:              book_depth

    ``game_stake_headroom`` is what stops a single match accumulating exposure one correlated line at a
    time (N15): the per-pair cap is enforced per QUOTE, so six totals lines on one match are six
    separate "within the cap" decisions that a single goal settles together. None -> unconstrained.

    ``pair_cap``/``daily_stake`` divide by the PAIR notional (both legs count toward those caps), so the
    resulting size can never breach them. floor() to whole shares. If that largest size is BELOW the
    venue minimum, the quote is REFUSED (``binding='below_venue_minimum'``) — we never clamp DOWN to the
    minimum. Returns {size, binding, refused, limiter}. PURE."""
    px = max(float(price), 1e-9)
    ha = max(float(hedge_ask or 0.0), 0.0)
    pair_px = px + ha if (px + ha) > 1e-9 else px
    inf = float("inf")
    allow = {
        "quote_usd_max": float(quote_usd_max) / px,
        "pair_cap": float(max_pair_stake_usd) / pair_px,
        "game_cap": (max(0.0, float(game_stake_headroom)) / pair_px
                     if game_stake_headroom is not None else inf),
        "daily_stake": max(0.0, float(daily_stake_headroom)) / pair_px,
        "hedge_depth": float(hedge_depth) if hedge_depth is not None else inf,
        "book_depth": float(book_depth) if book_depth is not None else inf,
    }
    limiter = min(allow, key=lambda k: (allow[k], _BINDING_ORDER.get(k, 99)))
    size = int(math.floor(min(allow.values()) + 1e-9))
    vmin = int(max(1, venue_minimum))
    if size < vmin:
        # The market/caps can't even support the venue minimum -> REFUSE the quote (never clamp down).
        return {"size": 0, "binding": "below_venue_minimum", "refused": True, "limiter": limiter,
                "max_fit": size, "venue_minimum": vmin}
    return {"size": size, "binding": limiter, "refused": False, "limiter": limiter,
            "max_fit": size, "venue_minimum": vmin}


class LiveCaps:
    """Per-day live exposure accounting + the pre-place decision. One instance per process/day."""

    def __init__(self, live_cfg: Any, *, telegram: Any = None, log: Any = None) -> None:
        self.quote_usd_max = float(getattr(live_cfg, "quote_usd_max", 5.0))
        self.max_open_quotes = int(getattr(live_cfg, "max_open_quotes", 2))
        self.max_fills_per_day = int(getattr(live_cfg, "max_fills_per_day", 10))
        self.max_daily_loss_usd = float(getattr(live_cfg, "max_daily_loss_usd", 25.0))
        self.max_daily_stake_usd = float(getattr(live_cfg, "max_daily_stake_usd", 100.0))
        # PER-PAIR cap: ``quote_usd_max`` bounds the REST leg only, but a cheap rest leg's HEDGE can dwarf
        # it (TBTOR: rest leg $1.02, hedge $16.21 — 16x). This bounds the WHOLE pair (rest + worst-case
        # hedge) for ONE bet, so a single low-priced quote can't commit an outsized hedge. A breach
        # REFUSES that one quote (it's a sizing limit, not a daily breach — no day-halt).
        self.max_pair_stake_usd = float(getattr(live_cfg, "max_pair_stake_usd", 25.0))
        # ---- THE IN-PLAY RING-FENCE (P3 from REPORT_inplay_funnel.txt) --------------------------
        # In-play gets its OWN daily reservation pool that pre-game cannot consume, and pre-game keeps
        # ``max_daily_stake_usd`` to itself. The two pools never borrow from each other.
        #
        # WHY: the funnel measured on 2026-08-06 showed in-play reaching the executor 0.69% of the time
        # and being refused 100% of the time it got there, always on `daily_stake` — because
        # ``max_open_quotes`` x the typical projected pair was numerically equal to the whole daily
        # budget, so pre-game's resting quotes RESERVED the entire pool and in-play was sized at 0
        # shares forever. Raising the shared budget alone would not have fixed it: pre-game would simply
        # reserve the bigger number too. A ring-fence is the only shape that guarantees in-play a slice.
        #
        # 0.0 disables the ring-fence and restores the single shared pool.
        self.inplay_pool_usd = float(getattr(live_cfg, "inplay_pool_usd", 0.0) or 0.0)
        # IN-PLAY-ONLY REALIZED LOSS SUB-CAP ($, positive number). In-play lifetime is -$6.69 over 12
        # fills and its p50 locked net is negative — only the tail pays. This is the "fail cheaply and
        # visibly" rail: breaching it stops IN-PLAY for the UTC day while pre-game (the proven earner)
        # keeps running. It counts UNWIND TOLLS, because the tolls are what actually lost the money
        # (-$2.20, -$4.75, -$0.96), not the pairs. 0.0 disables it.
        self.inplay_max_loss_usd = float(getattr(live_cfg, "inplay_max_loss_usd", 0.0) or 0.0)
        self.telegram = telegram
        self.log = log
        # running day state
        self.stake_today = 0.0            # SUM of all legs actually committed today ($), BOTH phases
        self.fills_today = 0
        self.pnl_today = 0.0
        self.open_quotes = 0
        # PER-PHASE splits of the three running numbers above. The totals stay authoritative for the
        # SHARED rails (the $50 daily-loss brake, the fills-per-day cap); these exist so the two stake
        # POOLS and the in-play loss sub-cap can be enforced without either phase seeing the other's.
        self.stake_by_phase: dict = {"pre": 0.0, "inplay": 0.0}
        self.reserved_by_phase: dict = {"pre": 0.0, "inplay": 0.0}
        self.pnl_by_phase: dict = {"pre": 0.0, "inplay": 0.0}
        #: Latched when the in-play realized loss sub-cap breaches. Read by the executor's ``eligible()``
        #: so enforcement cannot depend on remembering to call a checker.
        self.inplay_budget_halted = False
        # RESERVED: the SUM of the projected pair costs of the quotes resting RIGHT NOW (N14). The daily
        # cap used to check only the ONE quote being placed against ``stake_today``, so twelve slots at a
        # $350 pair cap were $4,200 of committable exposure against an $800 "cap" — the cap throttled the
        # RATE of placement and never bounded what was outstanding. A resting order is a promise to spend
        # its pair cost, so it is counted from the moment it rests and released when it stops resting.
        self.reserved_stake = 0.0
        self.halted = False
        self.halt_reason: Optional[str] = None
        self._day = ""                   # UTC day of the running counters (rolled by roll())

    #: Halts that a new UTC day must NOT clear. A DAILY-cap halt is a budget that genuinely resets at
    #: midnight; a SAFETY halt is a latched statement that something is unresolved, and unresolved
    #: things do not become resolved because the clock rolled over. This happened twice in production
    #: (TBTOR 2026-07-23, ZAYRZE 2026-07-25): a "MANUAL CHECK REQUIRED" freeze silently expired at
    #: 00:00Z and the bot resumed quoting over a naked, unverified position.
    #: ``unreadable_state`` joins them for the same reason: a state file we cannot parse is still
    #: unparseable tomorrow, and it is the file that tells us which positions to watch.
    STICKY_HALTS = ("orphan_position", "booking_quarantine", "unreadable_state")

    def roll(self, day: str) -> None:
        """Reset the per-DAY counters (stake/fills/pnl + daily-cap halt) at a new UTC day.
        ``open_quotes`` is NOT reset — a resting order survives midnight. A daily-cap halt clears with
        the new day; a STICKY_HALTS safety latch does NOT."""
        if day == self._day:
            return
        self._day = day
        self.stake_today = 0.0
        self.fills_today = 0
        self.pnl_today = 0.0
        self.stake_by_phase = {"pre": 0.0, "inplay": 0.0}
        self.pnl_by_phase = {"pre": 0.0, "inplay": 0.0}
        # The in-play loss sub-cap is a DAILY budget, so it resets with the day exactly as the daily
        # stake cap does. (Reserved-by-phase is deliberately NOT reset — a resting order survives
        # midnight, and so must its reservation; same rule as ``open_quotes``.)
        self.inplay_budget_halted = False
        if self.halt_reason in self.STICKY_HALTS:
            return                        # keep halted + keep the reason: only a human clears these
        self.halted = False
        self.halt_reason = None

    # -- sizing --------------------------------------------------------------
    def size_shares(self, price: float, *, min_shares: int = 5) -> int:
        """Whole-share size for a REST leg at ``price``: at least ``min_shares`` AND the venue's ~$1
        minimum, but never a notional above ``quote_usd_max``. If even ``min_shares`` would exceed the
        cap the cap wins (returns the largest whole-share count that fits, >= 1)."""
        px = max(float(price), 1e-9)
        floor_min = max(int(min_shares), math.ceil(1.0 / px))     # venue min (>= $1) and >= min_shares
        max_by_cap = int(self.quote_usd_max / px + 1e-9)          # largest whole-share count <= cap $
        if max_by_cap < 1:
            return 1                                              # price alone exceeds the cap -> 1 share
        return max(1, min(floor_min, max_by_cap))

    @staticmethod
    def projected_pair_stake(rest_price: float, rest_shares: float,
                             hedge_price: float, hedge_shares: float) -> float:
        """The $ BOTH legs of a hedged pair would commit if the rest leg fully fills and is hedged."""
        return float(rest_price) * float(rest_shares) + float(hedge_price) * float(hedge_shares)

    # -- the two stake POOLS -------------------------------------------------
    @staticmethod
    def _ph(phase: Any) -> str:
        """Normalise a phase to a pool key. Anything that is not in-play draws on the pre-game pool —
        'gap' never quotes, and an unknown phase must not silently get its own budget."""
        return "inplay" if str(phase) == "inplay" else "pre"

    def pool_for(self, phase: Any = "pre") -> float:
        """The daily stake POOL this phase may draw on. In-play gets its ring-fenced pool when one is
        configured; everything else gets ``max_daily_stake_usd``. With the ring-fence off (0.0) both
        phases share ``max_daily_stake_usd`` exactly as before."""
        if self._ph(phase) == "inplay" and self.inplay_pool_usd > 0.0:
            return self.inplay_pool_usd
        return self.max_daily_stake_usd

    def inplay_loss_left(self) -> float:
        """$ of in-play realized loss still allowed today (inf when the sub-cap is disabled)."""
        if self.inplay_max_loss_usd <= 0.0:
            return float("inf")
        return max(0.0, self.inplay_max_loss_usd + float(self.pnl_by_phase.get("inplay", 0.0)))

    def _check_inplay_loss(self) -> None:
        """Latch the in-play sub-cap the moment in-play's realized pnl breaches it."""
        if self.inplay_max_loss_usd <= 0.0 or self.inplay_budget_halted:
            return
        if float(self.pnl_by_phase.get("inplay", 0.0)) <= -self.inplay_max_loss_usd:
            self.inplay_budget_halted = True
            if self.log:
                self.log.error("[MAKER_RT][INPLAY] IN-PLAY DAILY LOSS SUB-CAP BREACHED: in-play realized "
                               "$%.2f today, limit -$%.2f (unwind tolls included). IN-PLAY IS STOPPED "
                               "for the UTC day. Pre-game is UNAFFECTED and keeps running.",
                               float(self.pnl_by_phase.get("inplay", 0.0)), self.inplay_max_loss_usd)

    # -- pre-place decision --------------------------------------------------
    def can_place(self, projected_pair_stake: float, phase: Any = "pre") -> tuple[bool, str]:
        """Decide whether a NEW quote may rest, given the $ its pair (rest + worst-case hedge) would
        commit if it fills. Refuses (and HALTS for the day, alerting) when the projected stake would
        breach ``max_daily_stake_usd``; refuses this ONE quote (no day-halt) when the pair alone breaches
        ``max_pair_stake_usd``. Also refuses on the open-quote cap, the fills-per-day cap, or an existing
        halt. Returns (ok, reason)."""
        ph = self._ph(phase)
        if self.halted:
            return False, f"halted:{self.halt_reason}"
        # THE SHARED BRAKE, unchanged and deliberately not per-phase: $50 bounds the whole day's
        # realized loss across both phases. The in-play sub-cap below is an ADDITIONAL, tighter rail on
        # the unproven phase, never a replacement for this one.
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")
            return False, "max_daily_loss_usd"
        # IN-PLAY ONLY: its own realized-loss sub-cap. Refuses in-play; pre-game is untouched and there
        # is no day-halt — that is the whole point of ring-fencing the experiment.
        if ph == "inplay":
            self._check_inplay_loss()
            if self.inplay_budget_halted:
                return False, "inplay_daily_loss"
        if self.open_quotes >= self.max_open_quotes:
            return False, "max_open_quotes"
        if self.fills_today >= self.max_fills_per_day:
            return False, "max_fills_per_day"
        # FILLS-PER-DAY HEADROOM. Every resting quote is a fill waiting to happen, so holding more open
        # quotes than the day has fills left is a promise the caps cannot keep: 12 opens against 3
        # remaining fills means 9 of them must be cancelled or breach the rail. Refuse the excess up
        # front instead of discovering it at fill time. (No day-halt — the budget is not spent, and it
        # frees itself as quotes close.)
        if self.open_quotes >= max(0, self.max_fills_per_day - self.fills_today):
            return False, "fills_per_day_headroom"
        # PER-PAIR sizing cap FIRST (a single oversized pair is refused, not day-halted).
        if float(projected_pair_stake) > self.max_pair_stake_usd + 1e-9:
            return False, "max_pair_stake_usd"
        # EXPOSURE TRUTH: spent + OUTSTANDING + this one, against THIS PHASE'S pool.
        pool = self.pool_for(ph)
        if self.committed_stake(ph) + float(projected_pair_stake) > pool + 1e-9:
            # NOT a day-halt. Unlike money already spent, a reservation is released when its quote is
            # cancelled or ages out, so this is a "full right now" condition, not a spent budget — and
            # halting the day over a temporary one would be the concentration bug wearing a rail's
            # clothes. The day-halt below still fires on genuinely SPENT stake.
            if self.stake_by_phase.get(ph, 0.0) + float(projected_pair_stake) > pool + 1e-9:
                # An in-play pool breach REFUSES in-play and leaves pre-game alone — halting the day
                # because the $500 experiment ran out of room would stop the proven earner to protect
                # the unproven one, which is backwards.
                if ph == "inplay":
                    return False, "inplay_pool_spent"
                self._halt("max_daily_stake_usd")
                return False, "max_daily_stake_usd"
            return False, ("inplay_pool_reserved" if ph == "inplay" else "daily_stake_reserved")
        return True, "ok"

    def committed_stake(self, phase: Any = None) -> float:
        """Spent PLUS outstanding reservations. ``phase`` None = both phases pooled (reporting and the
        legacy single-pool readers); a phase = only that pool's own commitments.

        WITH THE RING-FENCE OFF there are no separate pools, so a phase-scoped question has to be
        answered with the GLOBAL total — otherwise turning the fence off would leave each phase blind to
        the other's reservations against a budget they are still sharing, which is strictly worse than
        either posture and would silently double the effective cap."""
        if phase is None or self.inplay_pool_usd <= 0.0:
            return float(self.stake_today) + max(0.0, float(self.reserved_stake))
        ph = self._ph(phase)
        return float(self.stake_by_phase.get(ph, 0.0)) + max(0.0, float(self.reserved_by_phase.get(ph, 0.0)))

    def daily_stake_headroom(self, phase: Any = "pre") -> float:
        """What a NEW quote in ``phase`` may still project, once that pool's outstanding reservations
        are honoured. THIS is the number that sizes every quote (plan_size), so getting the phase wrong
        here is what would let one phase eat the other's ring-fence."""
        return max(0.0, float(self.pool_for(phase)) - self.committed_stake(phase))

    # -- accounting (called on real venue events) ----------------------------
    # EVERY mutator below takes the PHASE that caused it. The totals keep their old meaning (the shared
    # rails read them); the per-phase splits are what make the ring-fence real. A caller that forgets
    # the phase attributes the money to pre-game, which is the SAFE direction: it can only ever make
    # in-play look like it has more room than it spent, never let it quietly spend pre-game's pool.
    def commit_stake(self, usd: float, phase: Any = "pre") -> None:
        """Add REAL committed notional from a leg (a rest fill, a hedge lift, or an unwind sell)."""
        self.stake_today += float(usd)
        ph = self._ph(phase)
        self.stake_by_phase[ph] = self.stake_by_phase.get(ph, 0.0) + float(usd)

    def on_open(self, projected_pair_stake: float = 0.0, phase: Any = "pre") -> None:
        """A quote is now RESTING: hold its projected pair cost against its OWN pool until it stops."""
        self.open_quotes += 1
        amt = max(0.0, float(projected_pair_stake))
        self.reserved_stake += amt
        ph = self._ph(phase)
        self.reserved_by_phase[ph] = self.reserved_by_phase.get(ph, 0.0) + amt

    def on_close(self, projected_pair_stake: float = 0.0, phase: Any = "pre") -> None:
        """A quote stopped resting (cancelled, aged out, or filled). Release its reservation — on a FILL
        the real legs arrive through ``commit_stake``, so releasing here is what stops double-counting."""
        self.open_quotes = max(0, self.open_quotes - 1)
        amt = max(0.0, float(projected_pair_stake))
        self.reserved_stake = max(0.0, self.reserved_stake - amt)
        ph = self._ph(phase)
        self.reserved_by_phase[ph] = max(0.0, self.reserved_by_phase.get(ph, 0.0) - amt)

    #: The plausible-pnl bound is derived from what a hedged pair CAN produce, not from its stake: a
    #: pair's pnl is ``locked_net x shares``, ``locked_net`` is capped by the sanity ceiling, and the
    #: stake is capped by ``max_pair_stake_usd`` — so |pnl| <= ceiling x pair_cap, times headroom for
    #: fees, rounding and an over-fill. Bounding by STAKE alone is far too loose to be useful: 2x the
    #: $350 pair cap is $700, and the Fortaleza fiction was $320.
    FILL_PNL_SANITY_EDGE = 0.05      # mirrors max_plausible_edge_pct (the ceiling that gates quoting)
    FILL_PNL_SANITY_MULT = 3.0       # headroom
    FILL_PNL_SANITY_FLOOR = 5.0      # never bound tighter than this, whatever the pair cap is

    def fill_pnl_bound(self, locked: bool = True) -> float:
        """The largest |pnl| a single fill can plausibly book.

        TWO regimes, because the two outcomes are bounded by different things:
          * ``locked=True``  — a HEDGED pair. Its pnl is ``locked_net x shares`` and ``locked_net`` is
            capped by the sanity ceiling, so the bound is ceiling x pair-cap. This is the tight one,
            and the one the Fortaleza fiction had to pass.
          * ``locked=False`` — a realized UNWIND / flatten / orphan mark. That is a position outcome,
            not an edge, so it is bounded by the money committed rather than by the ceiling. Applying
            the tight bound here would refuse honest losses (a naked leg can lose its whole stake)."""
        if not locked:
            return max(self.FILL_PNL_SANITY_FLOOR, 2.0 * self.max_pair_stake_usd)
        return max(self.FILL_PNL_SANITY_FLOOR,
                   self.FILL_PNL_SANITY_MULT * self.FILL_PNL_SANITY_EDGE * self.max_pair_stake_usd)

    def implausible_fill_pnl(self, pnl: float, locked: bool = True) -> bool:
        """Is this single-fill pnl outside anything the strategy could have produced?

        ``on_fill`` is the ONLY feeder of the daily-loss rail, and it accepted any number at all. On
        2026-07-29 a +$320.05 booking on a $14.12 rest leg sailed straight through — minutes after the
        SAME subsystem rejected a 12.6% quote-time edge as a probable bug — and re-based ``pnl_today``
        so far positive that the -$50 rail would have needed a REAL -$380 to trip. A rail fed by an
        unbounded number is not a rail."""
        return abs(float(pnl)) > self.fill_pnl_bound(locked) + 1e-9

    def on_fill(self, pnl: float = 0.0, *, locked: bool = True, phase: Any = "pre") -> None:
        """Count a fill and move the daily pnl. An IMPLAUSIBLE pnl is booked as ZERO and halts for
        review rather than re-basing the loss rail in either direction (a phantom gain disables it; a
        phantom loss false-halts the day). ``locked`` selects which bound applies — see
        ``fill_pnl_bound``."""
        self.fills_today += 1
        if self.implausible_fill_pnl(pnl, locked):
            self._halt("implausible_fill_pnl")
            return
        self.pnl_today += float(pnl)
        ph = self._ph(phase)
        self.pnl_by_phase[ph] = self.pnl_by_phase.get(ph, 0.0) + float(pnl)
        self._check_inplay_loss()
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    def on_loss(self, usd: float, phase: Any = "pre") -> None:
        """Record a realized loss (positive ``usd`` = $ lost), e.g. an unwind cost."""
        self.pnl_today -= float(usd)
        ph = self._ph(phase)
        self.pnl_by_phase[ph] = self.pnl_by_phase.get(ph, 0.0) - float(usd)
        self._check_inplay_loss()
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    def adjust_pnl(self, usd: float, phase: Any = "pre") -> None:
        """Restate today's pnl WITHOUT counting a fill (signed: + improves the day).

        For a correction to an ALREADY-counted trade — a provisional worst-case mark rebooked at what the
        venue actually paid. Routing that through ``on_fill`` would spend one of the day's scarce
        max_fills_per_day slots on a bookkeeping entry: the CSKA restatement pushed fills_today 7 -> 8
        without a single new order being placed. The daily-loss rail is still re-checked, because a
        correction can move the day in either direction."""
        self.pnl_today += float(usd)
        ph = self._ph(phase)
        self.pnl_by_phase[ph] = self.pnl_by_phase.get(ph, 0.0) + float(usd)
        # THE UNWIND TOLL IS THE POINT. In-play's entire lifetime loss came through this path, not
        # through booked pairs, so the sub-cap has to see it here or it would never fire at all.
        self._check_inplay_loss()
        if self.pnl_today <= -self.max_daily_loss_usd:
            self._halt("max_daily_loss_usd")

    # -- halt ----------------------------------------------------------------
    def _halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        # LOG the technical reason; send a PLAIN, self-explanatory STOPPED alert to Telegram.
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] AUTO-HALT (%s): stake_today=$%.2f pnl_today=$%.2f — "
                             "quoting stopped for the day.", reason, self.stake_today, self.pnl_today)
        human = {
            "max_daily_loss_usd": (f"I hit today's loss limit (down ${abs(self.pnl_today):.2f}) — I've "
                                   f"stopped placing new bets for the day."),
            "max_daily_stake_usd": (f"I've used today's full budget (${self.stake_today:.2f} of "
                                    f"${self.max_daily_stake_usd:.0f}) — no new bets today."),
        }.get(reason, "I've stopped placing new bets for the day (a daily safety limit was reached).")
        if self.telegram:
            try:
                self.telegram(f"🔴 STOPPED · {human} Anything already placed stays fully hedged; "
                              f"I resume automatically tomorrow.")
            except Exception as exc:  # noqa: BLE001 — a telegram failure never blocks the halt
                if self.log:
                    self.log.warning("[MAKER_RT][LIVE] telegram halt-alert failed: %s", exc)
