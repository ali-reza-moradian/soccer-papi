"""Quote driver — the event-driven glue over the pure engine.

For every universe market it evaluates BOTH rest directions on BOTH outcomes (rest on Poly hedge on
Kalshi, and vice-versa), arms/reprices/expires shadow quotes in the fill model, and on public trade
prints records shadow fills, marks the fee-inclusive hedge on the live book, and schedules
adverse-selection drift reads at +1/+5/+30s. It NEVER places an order (that is the live hedger, gated
elsewhere). Debounced calls come from the async loop.

PHASES (per candidate, from its kickoff): 'pre' (quoted as always, tradeable when live), 'gap'
(kickoff-120s .. kickoff — NO quotes) and 'inplay' (>= kickoff — quoted under the anti-phantom rails:
both-books-fresh, shock-freeze, and a persistence timer). Live is HARD-forbidden in-play regardless.
Every evaluation also computes the ACHIEVABLE net (join-the-bid edge) into the per-sport/phase summary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import hedge as hedge_mod
from .fills import ShadowFillModel
from .quotes import achievable_net, compute_quote, needs_reprice, poly_leg_exceeds_cap
from .state import utcnow


@dataclass
class Candidate:
    key: tuple
    sport: str
    game: str
    market_key: str
    rest_side: str
    direction: str            # "rest-poly" | "rest-kalshi"
    rest_ref: tuple
    rest_venue: str
    hedge_venue: str
    tick: float
    hedge_tick: float
    poly_rate: float
    hedge_lookup: dict        # how to fetch the hedge SideView from the store at any moment
    kickoff_ts: float
    poly_leg_cap: Optional[float] = None   # per-sport poly-leg cap (None = uncapped sport)
    teams: str = ""           # 'AWAY vs HOME' — for human alert names (resolves MLB home/away sides)

    @property
    def node3(self) -> tuple:
        return (self.sport, self.game, self.market_key)

    @property
    def rest_id(self) -> Optional[str]:
        return self.rest_ref[1]

    @property
    def hedge_id(self) -> Optional[str]:
        return self.hedge_lookup.get("token") or self.hedge_lookup.get("ticker")


class QuoteDriver:
    def __init__(self, cfg: Any, state: Any, *, log: Any = None, inplay_exec: Any = None,
                 pregame_exec: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.log = log
        # The IN-PLAY LIVE executor (built + tested, but LOCKED — armed() is False in this build, so every
        # hook below is a no-op and the shadow path is byte-identical). Set only by __main__.
        self.inplay_exec = inplay_exec
        # The PRE-GAME CONTINUOUS LIVE executor (rest-poly only). None in shadow; when set + eligible, the
        # arm branch drives a REAL order and every disarm path cancels it. Set only by __main__ when armed.
        self.pregame_exec = pregame_exec
        self.fills = ShadowFillModel()
        self.universe: list = []
        self.prev: dict = {}                 # key -> last QuoteDecision (reprice detection)
        self.last_event: dict = {}           # key -> last emitted event kind (dedupe per-tick spam)
        self.drift_pending: list = []        # [{key, hedge_lookup, fill_ts, marks:{}}]
        self._cands: list = []
        # In-play state.
        self.freeze_until: dict = {}         # node3 -> ts a shock-freeze expires
        self.viable_since: dict = {}         # candidate key -> ts a direction first became viable (in-play)
        self.stale_since: dict = {}          # candidate key -> ts a node first went stale (anti-flap grace)
        self.thin_refusals: dict = {}        # node3 -> [ts] of recent hedge_too_thin refusals (10-min window)
        self.thin_cooldown_until: dict = {}  # node3 -> ts a hedge-thin cooldown expires (after 3 in 10 min)
        self.hedge_persist_s = float(getattr(cfg, "hedge_persist_s", 10.0))  # continuous-depth pre-filter window
        self._achv_sample_ts: dict = {}      # node3 -> last achievable_sample CSV ts (1/min throttle)
        # SANITY CEILING (%): a computed edge above this is a probable pricing/pairing bug, not an
        # opportunity -> REJECT + loud log, never quote/place (mirrors the detector's plausible-ROI guard).
        self.max_plausible_edge_pct = float(getattr(cfg, "max_plausible_edge_pct", 5.0))

    # -- universe ----------------------------------------------------------
    def set_universe(self, universe: list, now: Any = None) -> None:
        """Adopt a new universe (on tree reload). CHURN SAFETY: a fight/match that vanished or swapped an
        opponent leaves its old candidate key absent from the rebuilt set — disarm any lingering shadow
        quote / prev / drift for that key so it can never fill on a stale book (UFC fights cancel late)."""
        self.universe = universe
        self._cands = [c for qm in universe for c in self._candidates(qm)]
        live = {c.key for c in self._cands}
        for key in list(self.fills.quotes):
            if key not in live:                         # a node no longer in the tree -> disarm it
                self.fills.disarm(key)
                sport, game, mkey, side, direction = key
                self.state.record({"event": "expire", "mode": "shadow", "sport": sport, "game": game,
                                   "market_key": mkey, "side": side, "direction": direction,
                                   "reason": "churn_gone"}, now or utcnow())
        for key in list(self.prev):
            if key not in live:
                self.prev.pop(key, None)
                self.last_event.pop(key, None)
                self.viable_since.pop(key, None)
                self.stale_since.pop(key, None)
        self.drift_pending = [d for d in self.drift_pending if d["key"] in live]
        # LIVE CHURN SAFETY: cancel any REAL order whose candidate vanished from the rebuilt universe
        # (a game/market that dropped can never fill on a stale book).
        if self.pregame_exec is not None:
            for key in list(self.pregame_exec.open_orders):
                if key not in live:
                    self.pregame_exec.cancel_key(key, now or utcnow(), "churn_gone")

    def _candidates(self, qm: Any) -> list:
        out: list = []
        sides = list(qm.sides.items())
        if len(sides) != 2:
            return out
        # PER-SPORT poly-leg cap (tennis walkover / ufc cancel-draw-NC tail). None for uncapped sports.
        cap = (getattr(self.cfg, "poly_leg_cap", None) or {}).get(getattr(qm, "sport", ""))
        for i in (0, 1):
            rest_side, rest_node = sides[i]
            hedge_node = sides[1 - i][1]
            # rest on POLY, hedge on KALSHI
            if rest_node.poly_token_id and hedge_node.kalshi_ticker:
                out.append(Candidate(
                    key=(qm.sport, qm.game, qm.market_key, rest_side, "rest-poly"),
                    sport=qm.sport, game=qm.game, market_key=qm.market_key, rest_side=rest_side,
                    direction="rest-poly", rest_ref=("polymarket", rest_node.poly_token_id, "BUY"),
                    rest_venue="polymarket", hedge_venue="kalshi", tick=0.01, hedge_tick=0.01,
                    poly_rate=self.cfg.poly_rate if hasattr(self.cfg, "poly_rate") else self.cfg.poly_fee_rate,
                    hedge_lookup={"venue": "kalshi", "ticker": hedge_node.kalshi_ticker,
                                  "side": hedge_node.kalshi_side},
                    kickoff_ts=qm.kickoff_ts, poly_leg_cap=cap, teams=getattr(qm, 'teams', '')))
            # rest on KALSHI, hedge on POLY (skip if this series charges a maker fee)
            series = str(rest_node.kalshi_ticker or "").split("-", 1)[0]
            maker_fee = series in getattr(self.cfg, "kalshi_maker_fee_series", ())
            if rest_node.kalshi_ticker and hedge_node.poly_token_id and not maker_fee:
                out.append(Candidate(
                    key=(qm.sport, qm.game, qm.market_key, rest_side, "rest-kalshi"),
                    sport=qm.sport, game=qm.game, market_key=qm.market_key, rest_side=rest_side,
                    direction="rest-kalshi", rest_ref=("kalshi", rest_node.kalshi_ticker, rest_node.kalshi_side),
                    rest_venue="kalshi", hedge_venue="polymarket", tick=0.01, hedge_tick=0.01,
                    poly_rate=float(hedge_node.poly_fee_rate or self.cfg.poly_fee_rate),
                    hedge_lookup={"venue": "polymarket", "token": hedge_node.poly_token_id},
                    kickoff_ts=qm.kickoff_ts, poly_leg_cap=cap, teams=getattr(qm, 'teams', '')))
        return out

    # -- lookups + phase ---------------------------------------------------
    def _rest_view(self, store: Any, c: Candidate):
        if c.rest_venue == "polymarket":
            return store.poly_view(c.rest_ref[1])
        return store.kalshi_view(c.rest_ref[1], c.rest_ref[2])

    def _hedge_view(self, store: Any, lookup: dict):
        if lookup.get("venue") == "polymarket":
            return store.poly_view(lookup.get("token"))
        return store.kalshi_view(lookup.get("ticker"), lookup.get("side"))

    def phase(self, kickoff_ts: float, now_ts: float) -> str:
        """'pre' (< kickoff-120s), 'gap' (kickoff-120s .. kickoff), or 'inplay' (>= kickoff)."""
        cutoff = float(getattr(self.cfg, "expire_before_kickoff_s", 120))
        if now_ts < kickoff_ts - cutoff:
            return "pre"
        if now_ts < kickoff_ts:
            return "gap"
        return "inplay"

    def _ip(self):
        return getattr(self.cfg, "inplay", None)

    def _rails_ok(self, store: Any, c: Candidate, now_ts: float) -> bool:
        """Data-quality rail for the achievable ladder (ALL phases): both books fresh AND the node not
        shock-frozen right now. A rails-failed evaluation is kept OUT of the summary ladder (ghost
        inflation from stale/frozen phantom edges)."""
        if now_ts < self.freeze_until.get(c.node3, 0.0):
            return False
        return self._node_fresh(store, c, now_ts)

    def _note_thin_refusal(self, node3: tuple, now_ts: float) -> None:
        """Record a hedge_too_thin refusal; after >= 3 within a 10-min window, COOLDOWN the node 15 min
        (stop arming a node whose hedge book can't reliably cover us -> arm-then-cancel churn)."""
        r = [t for t in self.thin_refusals.get(node3, []) if t >= now_ts - 600.0]
        r.append(now_ts)
        self.thin_refusals[node3] = r
        if len(r) >= 3:
            self.thin_cooldown_until[node3] = now_ts + 900.0

    def _node_fresh(self, store: Any, c: Candidate, now_ts: float) -> bool:
        """CONNECTION-based freshness for BOTH legs: each venue connection alive (recent activity, no
        pending resync) AND the node's book ticked within node_quiet_max_s. A QUIET book on a HEALTHY
        socket is FRESH — this is what stops the stale-cancel quote-churn."""
        ip = self._ip()
        cf = float(getattr(ip, "conn_fresh_s", 30.0)) if ip is not None else 30.0
        nq = float(getattr(ip, "node_quiet_max_s", 180.0)) if ip is not None else 180.0
        return bool(store.node_fresh(c.rest_id, now_ts, cf, nq)
                    and store.node_fresh(c.hedge_id, now_ts, cf, nq))

    # -- quote refresh -----------------------------------------------------
    def refresh_quotes(self, store: Any, now: Any, now_ts: float) -> None:
        """Re-evaluate every candidate against the current books; arm/reprice/expire + record events.
        In-play candidates pass the anti-phantom rails (fresh / shock-freeze / persistence) first."""
        ip = self._ip()
        viable_dirs: set = set()          # directions that produced a viable placement this cycle
        for c in self._cands:
            phase = self.phase(c.kickoff_ts, now_ts)
            rest = self._rest_view(store, c)
            hedge = self._hedge_view(store, c.hedge_lookup)
            if rest is None or hedge is None:
                self._expire_if_open(c, now, "book_gone", phase)
                self.last_event[c.key] = None
                self.viable_since.pop(c.key, None)
                continue

            # ACHIEVABLE-NET — computed on EVERY evaluation (both phases), independent of target_net:
            # the edge if we simply JOINED the current best bid. Accumulated per sport/phase; a throttled
            # 1/min/market sample also lands in the CSV.
            achv = achievable_net(rest.best_bid, hedge.best_ask, c.hedge_venue, c.poly_rate)
            rails_ok = self._rails_ok(store, c, now_ts)
            self.state.record_achievable(c.sport, phase, achv, now, rails_ok=rails_ok)
            self._maybe_sample_achievable(c, phase, achv, rails_ok, now, now_ts)

            # GAP: no quotes between kickoff-120s and kickoff.
            if phase == "gap":
                self._expire_if_open(c, now, "gap", phase)
                self.last_event[c.key] = None
                self.viable_since.pop(c.key, None)
                continue

            # IN-PLAY RAILS (quoting + fill counting): both-books-fresh, shock-freeze, persistence.
            if phase == "inplay" and ip is not None:
                if now_ts < self.freeze_until.get(c.node3, 0.0):
                    self._expire_if_open(c, now, "inplay_frozen", phase)
                    self.last_event[c.key] = None
                    self.viable_since.pop(c.key, None)
                    continue
                move = max(store.mid_move(c.rest_id, now_ts, ip.shock_window_s),
                           store.mid_move(c.hedge_id, now_ts, ip.shock_window_s))
                if move >= ip.shock_move - 1e-9:
                    self._freeze_node(c, now_ts, now_ts + ip.freeze_s, now, move)
                    self.viable_since.pop(c.key, None)
                    continue
                if not self._node_fresh(store, c, now_ts):
                    # ANTI-FLAP GRACE: cancel a live resting order for staleness ONLY after the stale
                    # condition holds CONTINUOUSLY for stale_grace_s (a brief blip must not shred queue
                    # position). Freeze/shock cancels above stay IMMEDIATE (those are real events).
                    since = self.stale_since.setdefault(c.key, now_ts)
                    if (now_ts - since) >= float(getattr(ip, "stale_grace_s", 5.0)):
                        self._expire_if_open(c, now, "inplay_stale", phase)
                    self.last_event[c.key] = None
                    self.viable_since.pop(c.key, None)
                    continue
                self.stale_since.pop(c.key, None)             # fresh again -> reset the grace timer

            # HEDGE-THIN COOLDOWN: skip a node that recently refused hedge_too_thin repeatedly (stop the
            # arm-then-cancel churn on chronically-thin-hedge nodes for the cooldown window).
            if now_ts < self.thin_cooldown_until.get(c.node3, 0.0):
                self._expire_if_open(c, now, "hedge_thin_cooldown", phase)
                self.last_event[c.key] = None
                self.viable_since.pop(c.key, None)
                continue
            tick = store.poly_tick(c.rest_ref[1]) if c.rest_venue == "polymarket" else c.tick
            hedge_tick = store.poly_tick(c.hedge_lookup.get("token"), 0.01) if c.hedge_venue == "polymarket" else 0.01
            dec = compute_quote(rest, hedge, hedge_venue=c.hedge_venue, tick=tick,
                                target_net=self.cfg.target_net, quote_usd=self.cfg.quote_usd,
                                poly_rate=c.poly_rate, hedge_tick=hedge_tick)
            prev = self.prev.get(c.key)
            self.prev[c.key] = dec
            # SANITY CEILING: a computed edge above the plausible bound is almost certainly a PRICING/
            # PAIRING bug (wrong markets paired, a stale/one-sided book), NOT a real opportunity. REJECT +
            # log loudly, never quote/place — same doctrine as the detector's max_plausible_roi_pct guard.
            # Counted for the panel; logged/recorded ONCE per transition (a mispriced node ticks ~4x/s).
            if dec.net_at_quote is not None and dec.net_at_quote * 100.0 > self.max_plausible_edge_pct + 1e-9:
                self._expire_if_open(c, now, "implausible_edge", phase)
                if self.last_event.get(c.key) != "implausible_edge":
                    if self.log:
                        self.log.warning("[MAKER_RT] REJECTED implausible edge %s %s %s [%s]: net %.2f%% > "
                                         "%.1f%% ceiling @ %.4f (hedge_ask %.4f) — probable pricing/pairing "
                                         "bug, NOT quoted.", c.game, c.market_key, c.direction, phase,
                                         dec.net_at_quote * 100.0, self.max_plausible_edge_pct,
                                         dec.quote_price, dec.hedge_best_ask if dec.hedge_best_ask else -1.0)
                    self.state.record(self._row("implausible_edge", c, now, dec, phase, tick=tick), now)
                    if self.pregame_exec is not None:
                        self.pregame_exec.note_implausible()
                self.last_event[c.key] = "implausible_edge"
                self.viable_since.pop(c.key, None)
                continue
            # PER-SPORT poly-leg cap: skip a direction whose Polymarket leg prices above the cap.
            if poly_leg_exceeds_cap(c.rest_venue, dec.quote_price, dec.hedge_best_ask, c.poly_leg_cap):
                self._expire_if_open(c, now, "poly_leg_cap", phase)
                self.last_event[c.key] = None
                self.viable_since.pop(c.key, None)
                continue
            if dec.would_be_behind:
                # A LIVE resting order momentarily behind best is KEPT (queue preservation) — route through
                # the executor's reprice HYSTERESIS (mandatory reprice only if economics break); the shadow
                # path expires as before. A brief best-bid flicker no longer shreds queue position.
                if (self.pregame_exec is not None and c.key in self.pregame_exec.open_orders
                        and self.pregame_exec.eligible(c, phase, now_ts)):
                    viable_dirs.add(c.direction)     # a live order momentarily behind still holds a viable slot
                    self.pregame_exec.place_or_reprice(c, dec, rest, store, now, now_ts, phase)
                else:
                    self._expire_if_open(c, now, "now_behind", phase)
                self.viable_since.pop(c.key, None)
                # DEDUPE: record 'behind' only on the TRANSITION (or a >=1-tick move) — not per tick.
                if self.last_event.get(c.key) != "behind" or needs_reprice(prev, dec, tick):
                    self.state.record(self._row("behind", c, now, dec, phase, tick=tick), now)
                    self.last_event[c.key] = "behind"
                continue
            if not dec.viable:
                if dec.reason == "hedge_too_thin":       # HEDGE-THIN pre-filter: cooldown a chronically-thin node
                    self._note_thin_refusal(c.node3, now_ts)
                self._expire_if_open(c, now, dec.reason, phase)
                self.last_event[c.key] = None
                self.viable_since.pop(c.key, None)
                continue

            # (c) PERSISTENCE: a direction must be continuously VIABLE (which includes hedge depth — a
            # hedge_too_thin refusal above is non-viable and pops viable_since) for >= the phase window
            # before it arms. In-play: ip.persist_ms (anti-phantom — a flicker that's viable one tick never
            # quotes). Pre-game HEDGE-THIN pre-filter: ONLY a node that recently refused hedge_too_thin must
            # prove the hedge held for hedge_persist_s (10s) before it RE-arms — a flickering-thin hedge
            # otherwise arms-then-cancels and shreds quote lifetime. Healthy nodes (no recent thinness) arm
            # immediately (persist_ms=0). Pairs with the 15-min cooldown after 3 refusals / 10 min.
            inplay_persist = phase == "inplay" and ip is not None
            recent_thin = any(t >= now_ts - 600.0 for t in self.thin_refusals.get(c.node3, []))
            if inplay_persist or recent_thin:
                persist_ms = float(ip.persist_ms) if inplay_persist else self.hedge_persist_s * 1000.0
                since = self.viable_since.get(c.key)
                if since is None:
                    self.viable_since[c.key] = now_ts
                    continue                             # first viable tick — start the timer, don't arm yet
                if (now_ts - since) * 1000.0 < persist_ms - 1e-9:
                    continue                             # still building persistence
            else:
                self.viable_since.pop(c.key, None)

            # STRICTER LIVE IN-PLAY RAIL: place only once the node has been unfrozen AND both-books-fresh
            # for >= freeze_cooloff_s. No-op in shadow (in-play gate not armed -> inplay_armed() False).
            if (phase == "inplay" and self.pregame_exec is not None and self.pregame_exec.inplay_armed()
                    and not self.pregame_exec.cooloff_ok(store, c, self.freeze_until.get(c.node3, 0.0), now_ts)):
                self._expire_if_open(c, now, "inplay_cooloff", phase)
                continue

            # LIVE (rest-poly only, gate armed for this phase): drive a REAL resting order. The executor
            # owns the REPRICE HYSTERESIS (mandatory on floor/never-crossable break; voluntary only on
            # >= reprice_min_ticks improvement after min_rest_s) so it is called every tick and decides
            # place / reprice / keep-resting itself — no 1-tick churn gate here.
            if self.pregame_exec is not None and self.pregame_exec.eligible(c, phase, now_ts):
                viable_dirs.add(c.direction)          # this direction HAS a viable candidate this cycle
                self.pregame_exec.place_or_reprice(c, dec, rest, store, now, now_ts, phase)
                self.last_event[c.key] = "quote"
                continue

            was_open = c.key in self.fills.quotes
            if was_open and not needs_reprice(prev, dec, tick):
                continue                                  # unchanged within a tick
            queue_ahead = rest.bid_size_at(dec.quote_price)
            self.fills.arm(c.key, c.rest_ref, dec.quote_price, dec.size_shares, queue_ahead,
                           dec.at_best, now_ts, hedge_ctx={"lookup": c.hedge_lookup,
                           "hedge_venue": c.hedge_venue, "poly_rate": c.poly_rate, "phase": phase,
                           "node3": c.node3})
            self.state.record(self._row("reprice" if was_open else "quote", c, now, dec, phase,
                                        tick=tick, queue_ahead=queue_ahead), now)
            self.last_event[c.key] = "quote"
            if self.log and not was_open:
                self.log.info("[MAKER_RT] QUOTE %s %s %s [%s] @ %.3f (floor %.3f, hedge_ask %.3f, net "
                              "%.3f%%, at_best=%s, q_ahead=%.0f)", c.game, c.market_key, c.direction, phase,
                              dec.quote_price, dec.floor or 0, dec.hedge_best_ask or 0,
                              (dec.net_at_quote or 0) * 100, dec.at_best, queue_ahead)
        # SLOT-RESERVE NON-BLOCKING: tell the executor which directions actually have a viable candidate
        # this cycle, so an idle direction's reserved slot never starves an active one.
        if self.pregame_exec is not None and hasattr(self.pregame_exec, "set_viable_directions"):
            self.pregame_exec.set_viable_directions(viable_dirs)

    def _freeze_node(self, c: Candidate, now_ts: float, until_ts: float, now: Any, move: float) -> None:
        """Shock freeze: disarm every open quote on the shocked node AND on every other line of the SAME
        GAME, and place none until ``until_ts``.

        GAME-WIDE on purpose (N15). A shock is news about the MATCH, not about one totals line: a goal
        moves over-1.5, over-2.5, over-3.5, both moneylines and the draw at the same instant. Freezing
        only the line that happened to tick first left the other five quoting into the same event with
        stale prices — which is the concentration risk and the stale-quote risk arriving together. The
        freeze window is keyed per node so each line thaws on its own clock, but the shock sets all of
        them."""
        node3 = c.node3
        newly = now_ts >= self.freeze_until.get(node3, 0.0)   # a fresh freeze (not an extension) -> log once
        game2 = (c.sport, c.game)
        frozen_nodes = {c2.node3 for c2 in self._cands if (c2.sport, c2.game) == game2} or {node3}
        for n3 in frozen_nodes:
            self.freeze_until[n3] = max(until_ts, self.freeze_until.get(n3, 0.0))
        for key in list(self.fills.quotes):
            if key[:3] in frozen_nodes:
                self.fills.disarm(key)
        if self.pregame_exec is not None:                 # LIVE: cancel every real order on the game
            for key in list(self.pregame_exec.open_orders):
                if key[:3] in frozen_nodes:
                    self.pregame_exec.cancel_key(key, now, "shock_freeze")
        for c2 in self._cands:
            if c2.node3 in frozen_nodes:
                self.last_event[c2.key] = None
        self.state.record({"event": "expire", "mode": "shadow", "sport": c.sport, "game": c.game,
                           "market_key": c.market_key, "side": c.rest_side, "direction": c.direction,
                           "phase": "inplay", "reason": "shock_freeze"}, now)
        if self.log and newly:
            self.log.info("[MAKER_RT] FREEZE %s %s (mid move %.3f >= shock) - disarmed %d line(s) of this "
                          "game, no quotes for the freeze window.", c.game, c.market_key, move,
                          len(frozen_nodes))

    def _maybe_sample_achievable(self, c: Candidate, phase: str, achv: Optional[float], rails_ok: bool,
                                 now: Any, now_ts: float) -> None:
        """Write AT MOST one achievable_sample CSV row per minute per MARKET (this evaluates thousands of
        times/day — the aggregate lives in the summary, only a heartbeat sample lands in the CSV). The
        RAW sample is kept regardless of rails; ``rails_ok`` records whether it fed the summary ladder."""
        if achv is None:
            return
        last = self._achv_sample_ts.get(c.node3, -1e18)
        if now_ts - last < 60.0:
            return
        self._achv_sample_ts[c.node3] = now_ts
        self.state.record({"event": "achievable_sample", "mode": "shadow", "sport": c.sport,
                           "phase": phase, "game": c.game, "market_key": c.market_key, "side": c.rest_side,
                           "direction": c.direction, "achievable_net": round(achv * 100, 4),
                           "rails_ok": rails_ok}, now)

    def _expire_if_open(self, c: Candidate, now: Any, reason: str, phase: str = "pre") -> None:
        # LIVE first: cancel a real resting order (with confirmation) on ANY expire/refuse reason.
        if self.pregame_exec is not None and c.key in self.pregame_exec.open_orders:
            self.pregame_exec.cancel(c, now, reason)
        if c.key in self.fills.quotes:
            self.fills.disarm(c.key)
            self.state.record({"event": "expire", "mode": "shadow", "sport": c.sport, "game": c.game,
                               "market_key": c.market_key, "side": c.rest_side, "direction": c.direction,
                               "phase": phase, "reason": reason}, now)

    # -- fills (shadow) ----------------------------------------------------
    def consume_prints(self, prints: list, store: Any, now: Any, now_ts: float) -> None:
        for rest_ref, price, volume in prints or []:
            for fe in self.fills.consume_print(rest_ref, price, volume, now_ts):
                self._on_shadow_fill(fe, store, now, now_ts)

    def _on_shadow_fill(self, fe: Any, store: Any, now: Any, now_ts: float) -> None:
        ctx = fe.hedge_ctx or {}
        phase = ctx.get("phase", "pre")
        # IN-PLAY FILL RAIL: a fill on an in-play quote counts only if the node isn't frozen AND both
        # books are still fresh at fill time (belt-and-suspenders over the arm-time rails).
        ip = self._ip()
        if phase == "inplay" and ip is not None:
            node3 = ctx.get("node3")
            frozen = node3 is not None and now_ts < self.freeze_until.get(node3, 0.0)
            rest_id = fe.rest_ref[1] if len(getattr(fe, "rest_ref", ()) or ()) > 1 else None
            hedge_id = (ctx.get("lookup") or {}).get("token") or (ctx.get("lookup") or {}).get("ticker")
            cf, nq = float(getattr(ip, "conn_fresh_s", 30.0)), float(getattr(ip, "node_quiet_max_s", 180.0))
            fresh = store.node_fresh(rest_id, now_ts, cf, nq) and store.node_fresh(hedge_id, now_ts, cf, nq)
            if frozen or not fresh:
                if self.log:
                    self.log.info("[MAKER_RT] in-play fill VETOED %s %s (frozen=%s fresh=%s) - not counted.",
                                  fe.key[1], fe.key[2], frozen, fresh)
                return
        hv = self._hedge_view(store, ctx.get("lookup", {}))
        mark = hedge_mod.mark_hedge(hv.ask_ladder, fe.size, ctx.get("hedge_venue", "kalshi"),
                                    ctx.get("poly_rate", self.cfg.poly_fee_rate)) if hv else None
        locked = hedge_mod.locked_net(fe.quote_price, mark["cost_per_share"]) if mark else None
        mid0 = hedge_mod.hedge_mid(hv) if hv else None      # drift baseline: hedge mid AT fill time
        # IN-PLAY LIVE: once the in-play gate is armed, the executor OWNS this fill (re-verify hedge, fire
        # or decline+unwind, caps, one-in-flight, Telegram + CSV). LOCKED in this build (armed() is False),
        # so control falls through to the shadow measurement below, byte-identical.
        if phase == "inplay" and self.inplay_exec is not None and self.inplay_exec.armed():
            self.inplay_exec.on_fill(fe, ctx, store, now, now_ts, hedge_view=hv, mark=mark)
            return
        sport, game, mkey, side, direction = fe.key
        row = {"event": "fill", "mode": "shadow", "sport": sport, "phase": phase, "game": game,
               "market_key": mkey, "side": side, "direction": direction,
               "quote_price": round(fe.quote_price, 4), "size": round(fe.size, 2), "at_best": fe.at_best,
               "trigger": fe.trigger, "quote_age_s": round(fe.quote_age_s, 2),
               "hedge_avg": round(mark["avg_price"], 4) if mark else "",
               "hedge_fee": round(mark["fee"], 4) if mark else "",
               "locked_net": round(locked * 100, 4) if locked is not None else None,
               "locked_pnl": round(locked * fe.size, 4) if locked is not None else None}
        self.state.record(row, now)
        if self.log:
            self.log.info("[MAKER_RT] SHADOW FILL %s %s %s [%s] @ %.3f (%s, age %.1fs) -> locked net %s%%",
                          game, mkey, direction, phase, fe.quote_price, fe.trigger, fe.quote_age_s,
                          f"{locked*100:.2f}" if locked is not None else "n/a")
        self.drift_pending.append({"key": fe.key, "lookup": ctx.get("lookup", {}), "phase": phase,
                                   "fill_ts": now_ts, "fill_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "mid0": mid0, "marks": {}, "sport": sport, "game": game,
                                   "market_key": mkey, "side": side, "direction": direction})

    def process_drift(self, store: Any, now: Any, now_ts: float) -> None:
        """Sample the hedge mid at each +drift_marks_s after a shadow fill; once the LAST mark is due,
        emit a PAIRED ``fill_drift`` event. The CSV is append-only so the fill row can't be updated in
        place — the fill_drift row is linked back by ``fill_ts`` + identity, and its drift_1/5/30 are the
        ADVERSE-SELECTION deltas mid(t) − mid_at_fill (positive = the hedge moved against us). These
        numbers gate in-play arming, so they must reliably exist — hence the shutdown flush below."""
        marks = tuple(self.cfg.drift_marks_s)
        still: list = []
        for d in self.drift_pending:
            age = now_ts - d["fill_ts"]
            hv = self._hedge_view(store, d["lookup"])
            mid = hedge_mod.hedge_mid(hv) if hv else None
            for m in marks:
                if m not in d["marks"] and age >= m:
                    d["marks"][m] = mid                  # first hedge-mid reading at/after this mark's age
            if age >= max(marks):
                self._emit_fill_drift(d, now)
            else:
                still.append(d)
        self.drift_pending = still

    def flush_drift(self, now: Any, now_ts: Optional[float] = None) -> None:
        """Emit a (possibly partial) fill_drift for EVERY still-pending fill — called on graceful
        shutdown so a HEAD-change/restart never silently drops a fill's drift. Marks not yet reached are
        blank; the row is tagged reason='partial'."""
        for d in self.drift_pending:
            self._emit_fill_drift(d, now, partial=True)
        self.drift_pending = []

    def _emit_fill_drift(self, d: dict, now: Any, *, partial: bool = False) -> None:
        marks = tuple(self.cfg.drift_marks_s)
        mid0 = d.get("mid0")

        def delta(m):
            mv = d["marks"].get(m)
            return round(mv - mid0, 6) if (mv is not None and mid0 is not None) else None

        self.state.record({"event": "fill_drift", "mode": "shadow", "sport": d["sport"],
                           "phase": d.get("phase", "pre"), "game": d["game"], "market_key": d["market_key"],
                           "side": d["side"], "direction": d["direction"], "fill_ts": d.get("fill_iso", ""),
                           "drift_1": delta(marks[0]) if len(marks) >= 1 else None,
                           "drift_5": delta(marks[1]) if len(marks) >= 2 else None,
                           "drift_30": delta(marks[-1]) if marks else None,
                           "reason": "partial" if partial else ""}, now)

    def expire_kickoff(self, now: Any, now_ts: float) -> None:
        """Disarm any quote that entered the GAP window (kickoff-120s .. kickoff). In-play quotes
        (now >= kickoff) are NOT touched here — the refresh loop's in-play rails govern them."""
        cutoff = self.cfg.expire_before_kickoff_s
        for c in self._cands:
            if c.kickoff_ts - cutoff <= now_ts < c.kickoff_ts and c.key in self.fills.quotes:
                self._expire_if_open(c, now, "kickoff_window", "gap")

    def open_quote_count(self) -> int:
        return len(self.fills.quotes)

    def _row(self, event: str, c: Candidate, now: Any, dec: Any, phase: str = "pre", *,
             tick: float = 0.01, queue_ahead: Optional[float] = None) -> dict:
        return {"event": event, "mode": "shadow", "sport": c.sport, "phase": phase, "game": c.game,
                "market_key": c.market_key, "side": c.rest_side, "direction": c.direction,
                "rest_venue": c.rest_venue, "hedge_venue": c.hedge_venue,
                "quote_price": round(dec.quote_price, 4) if dec.quote_price is not None else "",
                "size": round(dec.size_shares, 2) if dec.size_shares is not None else "",
                "floor": round(dec.floor, 4) if dec.floor is not None else "",
                "at_best": dec.at_best, "hedge_ask": round(dec.hedge_best_ask, 4) if dec.hedge_best_ask else "",
                "net_at_quote": round(dec.net_at_quote * 100, 4) if dec.net_at_quote is not None else "",
                "queue_ahead": round(queue_ahead, 2) if queue_ahead is not None else "",
                "reason": dec.reason}
