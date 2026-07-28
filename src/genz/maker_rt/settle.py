"""SETTLED-P&L reconciliation — the venue-truth realized number for a hedged pair.

The fill-time ``locked_pnl`` written on a ``hedge_locked`` row is an ESTIMATE (the walked hedge net at
fill time). The REAL pnl of a hedged bet is known only once BOTH legs settle/redeem, and it must net
BOTH venues — the rest leg AND the hedge leg — including settlement/redemption, not just the rest leg.

This module is PURE + I/O-free at its core:

  * ``net_pnl(legs)``   — sum costs + settlement values across the two legs -> {cost, revenue, net, roi}.
  * ``settled_row(...)``— build the ``trade_settled`` ledger row (true net + ROI, human ``reason``).
  * ``SettledPnlReconciler`` — given the maker's per-market cost basis (its own recorded legs) and the
    venues' settlement reads, emit one ``trade_settled`` row per market, ONCE (idempotent). It never
    raises into the caller; a venue read that fails just leaves that market un-reconciled (retried next
    pass). The panel/summary lifetime pnl reads the aggregated settled rows, NOT the fill-time estimate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

# SANITY RAILS on a settled trade. A hedged maker pair nets ~1% of a sub-$100 stake, so a settled row
# claiming a huge ROI or a net larger than one whole pair's stake is a UNIT/PAIRING BUG, not a windfall
# (the 2026-07-24 HANHAL row read $500.00 for a $5.00 Kalshi payout -> +$495.15 net, +10209% ROI). Such
# a row is REFUSED, logged CRITICAL, and never enters lifetime pnl.
SETTLED_ROI_CEILING = 0.50               # |net/cost| above this (50%) is implausible for a hedged pair
SETTLED_MAX_NET_USD_DEFAULT = 100.0      # |net| above one pair's stake cap is implausible (config overrides)
SETTLE_SHARE_TOL = 0.5                   # < 1 contract of leg imbalance is rounding, not a naked position


@dataclass
class SettledLeg:
    """One venue leg of a settled hedged pair, in VENUE-TRUTH dollars."""
    venue: str                    # "kalshi" | "polymarket"
    instrument: str               # kalshi ticker | poly token
    side: str
    shares: float
    cost_usd: float               # what we PAID to acquire the leg (fills incl. fees)
    settle_value_usd: float       # what we GOT BACK at settlement/redemption ($1/contract if it won)


def net_pnl(legs: list) -> dict:
    """Net realized pnl + ROI across BOTH legs: revenue (settlement/redemption) minus cost (fills)."""
    cost = sum(max(0.0, float(l.cost_usd)) for l in legs)
    revenue = sum(float(l.settle_value_usd) for l in legs)
    net = revenue - cost
    return {"cost": round(cost, 4), "revenue": round(revenue, 4), "net": round(net, 4),
            "roi": round(net / cost, 6) if cost > 1e-9 else 0.0}


def settled_row(*, sport: str, game: str, market_key: str, legs: list, settled_ts: str,
                market_id: Optional[str] = None, extra_reason: str = "", untracked: bool = False) -> dict:
    """The ``trade_settled`` event row: VENUE-TRUTH net + ROI, with an auditable per-leg ``reason``.
    ``settled_cost_usd`` rides on the row for the ROI denominator (read by state.record, dropped by the
    CSV writer — it is not a column, like unwind_cost). ``untracked=True`` flags a NAKED (unhedged)
    position — e.g. the 2026-07-25 UFC ghost stack — whose honest settled outcome is a full-stake
    loss/win, not a ~1% hedged edge; the sanity guard loosens the ROI ceiling for those rows so the true
    number reaches lifetime pnl (a plain refuse would have silently dropped a real -$98)."""
    n = net_pnl(legs)
    legdesc = "; ".join(
        f"{l.venue} {l.side} {float(l.shares):g}sh cost ${float(l.cost_usd):.2f} -> ${float(l.settle_value_usd):.2f}"
        for l in legs)
    reason = (f"{market_id or game} SETTLED{' [UNTRACKED naked]' if untracked else ''} "
              f"net ${n['net']:+.4f} ROI {n['roi'] * 100:+.2f}% on ${n['cost']:.2f} [{legdesc}]")
    if extra_reason:
        reason = f"{reason} {extra_reason}"
    winner_venue = next((l.venue for l in legs if float(l.settle_value_usd) > 1e-9), None)
    row = {"event": "trade_settled", "mode": "live", "sport": sport, "phase": "settled",
           "game": game, "market_key": market_key, "realized_pnl_usd": n["net"],
           "settled_cost_usd": n["cost"], "settled_revenue_usd": n["revenue"], "roi": n["roi"],
           "winner_venue": winner_venue, "fill_ts": settled_ts, "reason": reason}
    if untracked:
        row["untracked"] = True
    return row


class SettledPnlReconciler:
    """Pull BOTH venues' settlement/redemption for the maker's traded hedged pairs and emit a
    ``trade_settled`` row (true net + ROI) per market — ONCE. Best-effort + idempotent."""

    def __init__(self, *, kalshi: Any = None, poly: Any = None,
                 record: Optional[Callable] = None, log: Any = None,
                 max_pair_stake_usd: float = SETTLED_MAX_NET_USD_DEFAULT) -> None:
        self.kalshi = kalshi                       # KalshiExec (get_settlements)
        self.poly = poly                           # PolyExec (redemption inference / balance)
        self.record = record or (lambda row, now: None)
        self.log = log
        self.max_pair_stake_usd = float(max_pair_stake_usd)   # sanity-guard ceiling for |net| (config-driven)
        self._settled_keys: set = set()            # (game, market_key) already reconciled
        self._manual_keys: set = set()             # non-binary (walkover/void) settlements -> logged ONCE, never auto-booked

    def reconcile(self, pairs: list, now: Any) -> list:
        """``pairs``: the maker's traded hedged markets with per-leg COST BASIS, each:
              {sport, game, market_key, settled_ts?,
               kalshi:{ticker, side, shares, cost}, poly:{token, shares, cost}}
        Returns the rows emitted this pass (also handed to ``record``)."""
        if not pairs:
            return []
        settlements = self._kalshi_settlements_by_ticker()
        emitted: list = []
        for p in pairs:
            key = (p.get("game"), p.get("market_key"))
            if key in self._settled_keys:
                continue
            try:
                rows = self._reconcile_pair(p, settlements, now)
            except Exception as exc:  # noqa: BLE001 — one bad market must not stop the sweep
                if self.log:
                    self.log.warning("[MAKER_RT][SETTLE] reconcile failed for %s: %s", key, exc)
                rows = None
            if not rows:
                continue
            # A market yields the HEDGED pair row and, when the legs were unequal, an UNTRACKED row for
            # the naked excess. Each is sanity-guarded on its OWN terms (a naked row's ROI is legitimately
            # far outside a hedged pair's ceiling), and the market is marked settled either way.
            self._settled_keys.add(key)
            for row in rows:
                untracked = bool(row.get("untracked"))
                ok, why = sane_settled(row.get("realized_pnl_usd", 0.0),
                                       row.get("settled_cost_usd", 0.0),
                                       max_net_usd=self.max_pair_stake_usd, untracked=untracked)
                if not ok:
                    # REFUSE at the source: a bug (bad unit / mispaired legs) produced an implausible
                    # settled net. NEVER emit or record it — it must not reach lifetime pnl. A human
                    # reconciles by hand.
                    _log_critical(self.log, "[MAKER_RT][SETTLE][CRITICAL] REFUSED implausible trade_settled "
                                  "%s (%s): net $%s cost $%s — NOT counted. %s", key, why,
                                  row.get("realized_pnl_usd"), row.get("settled_cost_usd"),
                                  row.get("reason"))
                    continue
                self.record(row, now)
                emitted.append(row)
        return emitted

    def already_settled(self, game: str, market_key: str) -> bool:
        return (game, market_key) in self._settled_keys

    def mark_settled(self, game: str, market_key: str) -> None:
        """Seed a key as already-reconciled (e.g. a row backfilled offline) so we never double-count it."""
        self._settled_keys.add((game, market_key))

    # -- internals -----------------------------------------------------------
    def _kalshi_settlements_by_ticker(self) -> dict:
        """{ticker -> settlement dict} from GET /portfolio/settlements (best-effort; {} on any error)."""
        if self.kalshi is None or not hasattr(self.kalshi, "get_settlements"):
            return {}
        try:
            resp = self.kalshi.get_settlements()
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[MAKER_RT][SETTLE] kalshi settlements read failed: %s", exc)
            return {}
        rows = resp.get("settlements") if isinstance(resp, dict) else resp
        rows = rows or (resp.get("data") if isinstance(resp, dict) else None) or []
        out: dict = {}
        for s in rows:
            if isinstance(s, dict) and (s.get("ticker") or s.get("market_ticker")):
                out[s.get("ticker") or s.get("market_ticker")] = s
        return out

    def _reconcile_pair(self, p: dict, settlements: dict, now: Any) -> Optional[list]:
        """Net both legs once the Kalshi leg has settled. The Poly complement (the OPPOSITE outcome of the
        same event) redeems 1:1 iff the Kalshi leg LOST — so one settlement read resolves both legs.

        Returns a LIST of rows: the hedged-pair row plus, when the legs were unequal, one ``untracked``
        row per naked excess (see the split note below). None means 'not settled yet, retry'."""
        k = p.get("kalshi") or {}
        pl = p.get("poly") or {}
        st = settlements.get(k.get("ticker"))
        if st is None:                              # Kalshi leg not settled yet -> retry next pass
            return None
        result = str(st.get("market_result") or st.get("result") or "").lower()   # 'yes' | 'no' | 'scalar' | ...
        # NON-BINARY settlement (e.g. a tennis walkover/retirement -> Kalshi ``market_result: "scalar"``,
        # refunding ~last price; Poly settles by its OWN rules). The poly complement's redemption CANNOT be
        # inferred from a scalar Kalshi settlement, so auto-booking it GUESSES a windfall — that guess
        # booked the 2026-07-27 MICDRA +$15.94 phantom (it assumed the poly leg redeemed a full $36). REFUSE
        # + flag for manual reconciliation; never emit a guessed number.
        if result not in ("yes", "no"):
            key = (p.get("game"), p.get("market_key"))
            if key not in self._manual_keys:
                self._manual_keys.add(key)
                _log_critical(self.log, "[MAKER_RT][SETTLE][CRITICAL] %s settled market_result=%r "
                              "(non-binary — walkover/void/scalar). Poly redemption is NOT inferable; NOT "
                              "auto-booked. Reconcile by hand (read the actual poly redemption).",
                              k.get("ticker"), result)
            return None
        k_shares = float(k.get("shares") or 0.0)
        k_side = str(k.get("side") or "yes").lower()
        k_won = (result == k_side) if result in ("yes", "no") else None
        # Prefer the venue's own revenue figure; else $1/contract if the leg won.
        rev = st.get("revenue")
        if rev is None:
            rev = st.get("settled_value") or st.get("payout")
        if rev is not None:
            k_settle = kalshi_settle_dollars(rev)          # CENTS -> dollars, always (central normalizer)
        elif k_won is not None:
            k_settle = k_shares if k_won else 0.0
        else:
            return None                             # can't determine the result -> don't guess, retry
        poly_shares = float(pl.get("shares") or 0.0)
        if k_won is None:                           # revenue known but result unknown -> infer won from revenue
            k_won = k_settle >= k_shares - 0.5
        poly_settle = poly_shares if not k_won else 0.0   # complement wins iff the Kalshi leg lost
        settled_ts = p.get("settled_ts") or _settle_ts(st, now)
        k_cost, poly_cost = float(k.get("cost") or 0.0), float(pl.get("cost") or 0.0)
        common = dict(sport=p.get("sport") or "", game=p.get("game") or "",
                      market_key=p.get("market_key") or "", settled_ts=settled_ts,
                      market_id=k.get("ticker"))
        # HEDGED vs NAKED SPLIT. The two legs are only a hedge up to ``paired`` shares; any excess on one
        # side is an UNHEDGED position that happens to ride the same event. Booking the blend as one
        # hedged row misreports the maker's edge by orders of magnitude: on 2026-07-28 PHIMIA settled
        # 122 Kalshi vs 130.393 Poly and the 8.393 naked Poly shares (cost $0.40) redeemed $8.39 — so a
        # +$0.17 hedged pair (+0.14%) was published as "+$8.16, ROI +6.68%". Same rule as the UFC ghost:
        # luck is real money but it is NOT edge, so it rides the ``untracked`` bucket.
        paired = min(k_shares, poly_shares)
        rows: list = []
        if paired > SETTLE_SHARE_TOL:
            rows.append(settled_row(legs=[
                SettledLeg("kalshi", k.get("ticker") or "", k_side, paired,
                           _prorate(k_cost, paired, k_shares), _prorate(k_settle, paired, k_shares)),
                SettledLeg("polymarket", pl.get("token") or "", "buy", paired,
                           _prorate(poly_cost, paired, poly_shares),
                           _prorate(poly_settle, paired, poly_shares)),
            ], **common))
        for venue, inst, side, shares, cost, settle in (
                ("kalshi", k.get("ticker") or "", k_side, k_shares, k_cost, k_settle),
                ("polymarket", pl.get("token") or "", "buy", poly_shares, poly_cost, poly_settle)):
            excess = shares - paired
            if excess <= SETTLE_SHARE_TOL:
                continue
            rows.append(settled_row(legs=[SettledLeg(venue, inst, side, excess,
                                                     _prorate(cost, excess, shares),
                                                     _prorate(settle, excess, shares))],
                                    untracked=True,
                                    extra_reason=f"({excess:g} sh unhedged excess over the {paired:g}-sh "
                                                 f"pair — luck, not maker edge)",
                                    **common))
        return rows


def _prorate(total: float, part: float, whole: float) -> float:
    """Split ``total`` (a cost or a settlement value) in proportion to ``part``/``whole`` shares."""
    if whole is None or float(whole) <= 1e-9:
        return 0.0
    return float(total) * (float(part) / float(whole))


def kalshi_settle_dollars(v: Any) -> float:
    """THE central Kalshi settlement money normalizer. Kalshi portfolio money (revenue / settled_value /
    payout / market_exposure) is quoted in CENTS — a 5-contract win settles ``revenue=500`` meaning
    $5.00. The prior heuristic only divided values >= $1000, so a $5.00 payout (500) sailed through as
    $500.00 and booked a phantom +$495.15 / +10209% ROI that halted the bot and poisoned lifetime pnl.
    Kalshi settlement is ALWAYS cents, so ALWAYS divide by 100 — one rule, one place, no magnitude
    guessing. (Applied to every settlement money field read; the per-contract $1 fallback in
    ``_reconcile_pair`` is already in dollars and does not pass through here.)"""
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return 0.0


def sane_settled(net: float, cost: float, *, max_net_usd: float = SETTLED_MAX_NET_USD_DEFAULT,
                 roi_ceiling: float = SETTLED_ROI_CEILING, untracked: bool = False) -> tuple[bool, str]:
    """Guard a ``trade_settled`` net/cost before it can touch lifetime pnl. Returns (ok, reason).
    For a HEDGED pair (the default) REFUSES when |net| exceeds one pair's stake cap OR |net/cost| exceeds
    ``roi_ceiling`` — either is a unit/pairing bug for a pair whose real edge is ~1%.

    For an ``untracked`` NAKED position the ROI ceiling does NOT apply: its honest settled outcome is a
    full-stake loss (ROI -100%) or a full-value win, so only the gross UNIT error is guarded — |net| may
    not exceed a generous multiple of the position's own cost basis (this still catches the $500-for-$5
    100x bug while letting a real -$98 UFC-stack loss through). PURE (no I/O) so both the reconciler and
    the state aggregator gate on the same rule."""
    try:
        n, c = float(net), float(cost)
    except (TypeError, ValueError):
        return False, "unparseable net/cost"
    if untracked:
        limit = max(float(max_net_usd), 2.0 * abs(c))
        if abs(n) > limit + 1e-9:
            return False, f"untracked |net| ${abs(n):.2f} > bound ${limit:.2f} (unit-error guard)"
        return True, "ok (untracked naked)"
    if abs(n) > float(max_net_usd) + 1e-9:
        return False, f"|net| ${abs(n):.2f} > max_pair_stake ${float(max_net_usd):.2f}"
    roi = (n / c) if abs(c) > 1e-9 else 0.0
    if abs(roi) > float(roi_ceiling) + 1e-9:
        return False, f"|ROI| {abs(roi) * 100:.1f}% > {float(roi_ceiling) * 100:.0f}% ceiling"
    return True, "ok"


def _log_critical(log: Any, msg: str, *args: Any) -> None:
    """CRITICAL log via the passed logger if it exposes .critical, else .error, else the module logger.
    A refused settled row must scream loudly regardless of which logger the caller injected."""
    for name in ("critical", "error", "warning"):
        fn = getattr(log, name, None)
        if callable(fn):
            fn(msg, *args)
            return
    logging.getLogger("maker_rt").critical(msg, *args)


def _settle_ts(st: dict, now: Any) -> str:
    for k in ("settled_time", "settled_ts", "ts", "updated_ts"):
        if st.get(k):
            return str(st[k])
    try:
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return ""
