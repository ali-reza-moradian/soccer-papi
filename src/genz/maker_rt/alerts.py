"""Human-readable Telegram alerts for the live maker/hedger — written for a NON-TECHNICAL reader.

Every alert must be self-explanatory at a glance: plain language, emojis, REAL names (never a ticker or
a UUID), dollar amounts AND cents-prices, share counts, venue names, phase, and always the WHY. Raw
UUIDs / tickers / HTTP errors are NEVER sent to Telegram — those go to the log only.

    🟢 PLACED · 🎾 Tennis · Hanfmann vs Halys
       Offering $12.40 on Hanfmann to win @ 56¢ (22 shares) on Polymarket · in-play
       💡 If someone takes it I instantly hedge on Kalshi @ 41¢ → locks +$0.31 (+1.3%)
       ⏳ Waiting for a taker · open 2/2 · today: 3 fills, $47 of $300 used

    ✅ LOCKED · 🎾 Hanfmann vs Halys · profit is GUARANTEED either way
       Bought: Hanfmann $12.40 @ 56¢ (Poly) + Halys $9.02 @ 41¢ (Kalshi)
       Total risked $21.42 → pays $22.00 · 💵 net +$0.31 after fees (+1.45% ROI)
       📈 Today: 4 fills · +$1.12 · Lifetime: +$1.37

The executor calls ``format_event`` and never builds an alert string itself. Pure + unit-tested.
"""
from __future__ import annotations

from typing import Any, Optional

SPORT_EMOJI = {"mlb": "⚾", "tennis": "🎾", "soccer": "⚽", "ufc": "🥊"}
SPORT_LABEL = {"mlb": "MLB", "tennis": "Tennis", "soccer": "Soccer", "ufc": "UFC"}
VENUE_LABEL = {"polymarket": "Poly", "poly": "Poly", "kalshi": "Kalshi"}
VENUE_FULL = {"polymarket": "Polymarket", "poly": "Polymarket", "kalshi": "Kalshi"}

STATUS = {
    "placed":    ("🟢", "PLACED"),
    "repriced":  ("🔵", "REPRICED"),
    "cancelled": ("⚪", "CANCELLED"),
    "filled":    ("💰", "FILLED!"),
    "locked":    ("✅", "LOCKED"),
    "locked_loss": ("🔴", "ERROR"),
    "settled":   ("🏁", "SETTLED"),
    "unwound":   ("🟠", "UNWOUND"),
    "halted":    ("🔴", "STOPPED"),
    "problem":   ("⚠️", "PROBLEM"),
}

#: internal reason slugs -> a PLAIN-ENGLISH sentence a non-technical reader understands (the WHY).
REASON_HUMAN = {
    "reprice": "price moved — my offer is no longer the best price",
    "reprice_cross": "price moved — my offer is no longer the best price",
    "reprice_floor": "the edge shrank — my price stopped being profitable to hedge",
    "would_cross_at_post": "my price would have crossed the market — skipped to stay a maker",
    "below_tick": "price fell below the minimum tick",
    "below_venue_minimum": "the hedge/book was too thin to place even the minimum size",
    "disarmed": "I was switched off for this market",
    "churn_gone": "the market disappeared from the schedule",
    "age_out": "the offer sat too long unfilled — refreshing it",
    "poly_user_down": "my fill feed dropped — pulled offers until it's back",
    "kalshi_feed_down": "the Kalshi feed dropped — pulled offers until it's back",
    "kalshi_feed_grace": "the Kalshi feed dropped — pulled offers until it's back",
    "halt_after_partial": "a safety cap tripped after a partial fill — pulled the rest",
    "orphan_halt": "a position needs a human to check it",
    "hedge_thin_cooldown": "the hedge got too thin — pausing this market",
    "hedge_too_thin": "not enough hedge available to cover the offer",
    "now_behind": "someone outbid me — my price is no longer best",
    "book_gone": "the order book went away",
    "gap": "the match is about to start — pausing near kickoff",
    "kickoff_window": "the match is about to start — pausing near kickoff",
    "inplay_stale": "the book went quiet/stale — pausing this market",
    "inplay_frozen": "a sudden price move — frozen briefly for safety",
    "inplay_cooloff": "cooling off after a price move before re-offering",
    "shock_freeze": "a sudden price move — frozen briefly for safety",
    "shutdown": "shutting down cleanly",
    "market_closed": "the market has closed",
    "market_not_found": "the market is gone",
    "market_not_active": "the market isn't active",
    "market_settled": "the match already finished",
    "too_many_requests": "the venue rate-limited me — backing off",
    "insufficient_balance": "not enough balance to place it",
    "post_only_would_cross": "my price would have crossed — skipped to stay a maker",
}

_MONEYLINE_KEYS = {"ml", "ml2", "moneyline", "match_winner", "fight_winner", "winner"}


# --------------------------------------------------------------------------- #
# small formatters                                                              #
# --------------------------------------------------------------------------- #
def _title(s: Any) -> str:
    """'kansas city' -> 'Kansas City'; already-cased names pass through."""
    t = str(s or "").strip()
    return " ".join(w[:1].upper() + w[1:] for w in t.split()) if t else ""


def cents(price: Any) -> str:
    """0.56 -> '56¢'; None -> '—'. Rounds to the nearest cent."""
    try:
        return f"{round(float(price) * 100)}¢"
    except (TypeError, ValueError):
        return "—"


def money(v: Any) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "$—"


def signed_money(v: Any) -> str:
    """'+$0.31' / '-$0.18' / '$—'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "$—"
    return f"+{money(f)}" if f >= 0 else f"-{money(abs(f))}"


def pct(v: Any) -> str:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def dur(secs: Any) -> str:
    """4562 -> '1h 16m'; 252 -> '4m 12s'; 9 -> '9s'."""
    try:
        s = int(round(float(secs)))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def venue_label(venue: Any) -> str:
    return VENUE_LABEL.get(str(venue or "").lower(), str(venue or "").title() or "?")


def venue_full(venue: Any) -> str:
    return VENUE_FULL.get(str(venue or "").lower(), str(venue or "").title() or "?")


def phase_label(phase: Any) -> str:
    return "in-play" if str(phase) == "inplay" else "pre-game"


def humanize_reason(reason: Any) -> str:
    r = str(reason or "").strip()
    if not r:
        return "—"
    head = r.split()[0]                                  # leading token of "unwind_FAILED sell_res=..."
    return REASON_HUMAN.get(head, REASON_HUMAN.get(r, r.replace("_", " ")))


def _teams_pair(teams: Any) -> tuple[str, str]:
    """('away', 'home') from an 'AWAY vs HOME' string, else ('', '')."""
    s = str(teams or "")
    for sep in (" vs ", " v ", " @ ", "@"):
        if sep in s:
            a, _, h = s.partition(sep)
            return a.strip(), h.strip()
    return "", ""


# --------------------------------------------------------------------------- #
# names: the real team/player, never a ticker                                   #
# --------------------------------------------------------------------------- #
def bet_name(sport: Any, game: Any, market_key: Any, side: Any, teams: Any = None) -> str:
    """The bare human name of the OUTCOME we are betting: 'Hanfmann', 'Yankees', 'Total O5.5',
    'Portland O2.5'. No ' ML' suffix, never a ticker."""
    mkey, sd = str(market_key or ""), str(side or "")
    if mkey.startswith("team_total") and "|" in mkey:
        parts = mkey.split("|")
        team = _title(parts[1]) if len(parts) > 1 else _title(sd)
        line = parts[2] if len(parts) > 2 else ""
        ou = "O" if sd.lower().startswith("o") else ("U" if sd.lower().startswith("u") else "")
        return f"{team} {ou}{line}".strip()
    if mkey.startswith(("total_goals", "total", "totals", "goals")) and mkey not in _MONEYLINE_KEYS:
        line = mkey.split("|")[1] if "|" in mkey else ""
        ou = "O" if sd.lower().startswith("o") else ("U" if sd.lower().startswith("u") else "")
        return f"Total {ou}{line}".strip()
    if mkey in _MONEYLINE_KEYS or mkey.startswith(("ml", "moneyline")):
        low = sd.lower()
        if low in ("home", "away"):
            away, home = _teams_pair(teams)
            return _title((away if low == "away" else home) or sd)
        return _title(sd)                                # tennis/ufc: the side IS the player/fighter
    if mkey.startswith("spread"):
        line = mkey.split("|")[1] if "|" in mkey else ""
        return f"{_title(sd)} {line}".strip()
    return _title(sd) or str(game or "?")


def subject(sport: Any, game: Any, market_key: Any, side: Any, teams: Any = None) -> str:
    """The bet subject with its market suffix: '<name> ML' for moneyline, else the bare bet name.
    (Kept for back-compat; the new alerts prefer bet_name/matchup.)"""
    name = bet_name(sport, game, market_key, side, teams)
    mkey = str(market_key or "")
    if mkey in _MONEYLINE_KEYS or mkey.startswith(("ml", "moneyline")):
        return f"{name} ML".strip()
    return name


def matchup(sport: Any, game: Any, market_key: Any, side: Any, teams: Any = None) -> str:
    """'Hanfmann vs Halys' — the two sides of the event. Uses the real matchup when known, else the
    bet name alone (never a ticker)."""
    away, home = _teams_pair(teams)
    if away and home:
        return f"{_title(away)} vs {_title(home)}"
    return bet_name(sport, game, market_key, side, teams)


def other_name(bet: str, teams: Any) -> str:
    """The COMPLEMENT outcome's name (the side we hedge into) given the matchup, else 'the other side'."""
    away, home = _teams_pair(teams)
    a, h, b = _title(away), _title(home), str(bet or "").strip().lower()
    if a and h:
        if b == a.lower():
            return h
        if b == h.lower():
            return a
    return "the other side"


def head(sport: Any, game: Any, market_key: Any, side: Any, teams: Any = None) -> str:
    """'⚾ MLB · Yankees ML' — kept for the binding/size log lines (not the Telegram alerts)."""
    sp = str(sport or "").lower()
    return f"{SPORT_EMOJI.get(sp, '•')} {SPORT_LABEL.get(sp, sp.upper() or '?')} · {subject(sport, game, market_key, side, teams)}"


def _sport_tag(sport: Any) -> str:
    sp = str(sport or "").lower()
    return f"{SPORT_EMOJI.get(sp, '•')} {SPORT_LABEL.get(sp, sp.upper() or '?')}"


def _status(kind: str) -> str:
    e, label = STATUS.get(kind, ("•", kind.upper()))
    return f"{e} {label}"


# --------------------------------------------------------------------------- #
# the alerts                                                                    #
# --------------------------------------------------------------------------- #
def format_event(kind: str, *, sport: Any = None, game: Any = None, market_key: Any = None,
                 side: Any = None, teams: Any = None, venue: Any = None, phase: Any = None,
                 price: Any = None, size: Any = None, old_price: Any = None, new_price: Any = None,
                 reason: Any = None, pnl: Any = None, net_pct: Any = None, hedge_price: Any = None,
                 hedge_venue: Any = None, detail: Any = None, age_s: Any = None,
                 hedge_shares: Any = None, hedge_fee: Any = None, rest_price: Any = None,
                 rest_shares: Any = None, exp_net_usd: Any = None, exp_net_pct: Any = None,
                 open_now: Any = None, max_open: Any = None, fills_today: Any = None,
                 stake_today: Any = None, stake_cap: Any = None, today_pnl: Any = None,
                 lifetime_pnl: Any = None, cost: Any = None, winner: Any = None,
                 payout: Any = None, roi_pct: Any = None, **_ignore: Any) -> str:
    """Build the plain-language alert for ``kind``. Missing facts degrade gracefully. Never emits a
    ticker or UUID — pass the human name via side/teams."""
    tag = _sport_tag(sport)
    name = bet_name(sport, game, market_key, side, teams)
    match = matchup(sport, game, market_key, side, teams)

    if kind == "placed":
        amt = money((float(price) * float(size)) if (price is not None and size is not None) else None)
        sz = f"{int(size)} shares" if size is not None else "—"
        l1 = f"{_status(kind)} · {tag} · {match}"
        l2 = f"   Offering {amt} on {name} to win @ {cents(price)} ({sz}) on {venue_full(venue)} · {phase_label(phase)}"
        hedge = ""
        if hedge_price is not None:
            lock = ""
            if exp_net_usd is not None:
                lock = f" → locks {signed_money(exp_net_usd)}" + (f" ({pct(exp_net_pct)})" if exp_net_pct is not None else "")
            hedge = f"\n   💡 If someone takes it I instantly hedge on {venue_full(hedge_venue)} @ {cents(hedge_price)}{lock}"
        used = _daily_tail(fills_today, stake_today, stake_cap)
        slots = f" · open {int(open_now)}/{int(max_open)}" if (open_now is not None and max_open is not None) else ""
        l4 = f"\n   ⏳ Waiting for a taker{slots}{used}"
        return l1 + "\n" + l2 + hedge + l4

    if kind == "repriced":
        return (f"{_status(kind)} · {tag} · {match} · moved my offer {cents(old_price)} → {cents(new_price)} "
                f"on {venue_label(venue)} · {phase_label(phase)}")

    if kind == "cancelled":
        after = f" · offer pulled after {dur(age_s)}" if age_s is not None else " · offer pulled"
        return f"{_status(kind)} · {tag} · {match}{after}\n   Reason: {humanize_reason(reason)}"

    if kind == "filled":
        amt = money((float(price) * float(size)) if (price is not None and size is not None) else None)
        sz = f"({int(size)} sh)" if size is not None else ""
        return (f"{_status(kind)} · {tag} · {match}\n"
                f"   Someone took {amt} of {name} @ {cents(price)} {sz} · buying the hedge now…")

    if kind == "locked":
        hedge_nm = other_name(name, teams)
        rest_amt = money((float(rest_price) * float(rest_shares)) if (rest_price is not None and rest_shares is not None) else (float(price) * float(size) if (price is not None and size is not None) else None))
        hedge_amt = money((float(hedge_price) * float(hedge_shares)) if (hedge_price is not None and hedge_shares is not None) else None)
        risked = _pair_risk(rest_price, rest_shares, hedge_price, hedge_shares, hedge_fee, price, size)
        pays = money(float(rest_shares) if rest_shares is not None else (float(size) if size is not None else None))
        l1 = f"{_status(kind)} · {tag} · {match} · profit is GUARANTEED either way"
        l2 = (f"\n   Bought: {name} {rest_amt} @ {cents(rest_price if rest_price is not None else price)} "
              f"({venue_label(venue)}) + {hedge_nm} {hedge_amt} @ {cents(hedge_price)} ({venue_label(hedge_venue)})")
        roi = f" ({pct(net_pct)} ROI)" if net_pct is not None else ""
        l3 = f"\n   Total risked {money(risked)} → pays {pays} · 💵 net {signed_money(pnl)} after fees{roi}"
        l4 = ""
        if today_pnl is not None or lifetime_pnl is not None:
            ft = f"{int(fills_today)} fills · " if fills_today is not None else ""
            l4 = f"\n   📈 Today: {ft}{signed_money(today_pnl)} · Lifetime: {signed_money(lifetime_pnl)}"
        return l1 + l2 + l3 + l4

    if kind == "locked_loss":
        # A completed pair that does NOT profit (locked_net <= 0, or rest+hedge >= $1.00/share). This is
        # NEVER "GUARANTEED" — it is a guaranteed LOSS that the pre-hedge check should have declined, so it
        # is a red ERROR the operator must see, with the exact numbers that prove it.
        hedge_nm = other_name(name, teams)
        rp = rest_price if rest_price is not None else price
        rs = rest_shares if rest_shares is not None else size
        rest_amt = money((float(rp) * float(rs)) if (rp is not None and rs is not None) else None)
        hedge_amt = money((float(hedge_price) * float(hedge_shares)) if (hedge_price is not None and hedge_shares is not None) else None)
        risked = _pair_risk(rest_price, rest_shares, hedge_price, hedge_shares, hedge_fee, price, size)
        pays = money(float(rs) if rs is not None else None)
        pair_ps = None
        if rp is not None and hedge_price is not None:
            pair_ps = float(rp) + float(hedge_price)
        l1 = f"{_status(kind)} · {tag} · {match} · HEDGED AT A LOSS — this pair cannot profit"
        l2 = (f"\n   Bought: {name} {rest_amt} @ {cents(rp)} ({venue_label(venue)}) + "
              f"{hedge_nm} {hedge_amt} @ {cents(hedge_price)} ({venue_label(hedge_venue)})")
        roi = f" ({pct(net_pct)})" if net_pct is not None else ""
        l3 = f"\n   Total risked {money(risked)} → pays {pays} · 💵 net {signed_money(pnl)} after fees{roi}"
        warn = (f"\n   ⚠️ rest+hedge = {money(pair_ps)}/share (≥ $1.00) — the pre-hedge check should have "
                f"declined this. Investigate." if (pair_ps is not None and pair_ps >= 1.0 - 1e-9)
                else "\n   ⚠️ net edge is ≤ 0 after fees — the pre-hedge check should have declined this. Investigate.")
        return l1 + l2 + l3 + warn

    if kind == "settled":
        won = f" · {_title(winner)} won" if winner else ""
        life = f" · Lifetime {signed_money(lifetime_pnl)}" if lifetime_pnl is not None else ""
        roi = f" ({pct(roi_pct)})" if roi_pct is not None else ""
        return (f"{_status(kind)} · {tag} · {match}{won}\n"
                f"   Collected {money(payout)} on {money(cost)} · 💵 {signed_money(pnl)}{roi}{life}")

    if kind == "unwound":
        return (f"{_status(kind)} · {tag} · {match} · couldn't hedge in time, sold back\n"
                f"   Cost me {money(cost)} (that's the safety net working — no open risk)")

    if kind == "halted":
        return f"{_status(kind)} · {str(detail or humanize_reason(reason))}"

    if kind == "problem":
        return f"{_status(kind)} · {str(detail or humanize_reason(reason))}"

    return f"{_status(kind)} · {tag} · {match}"


def _daily_tail(fills_today: Any, stake_today: Any, stake_cap: Any) -> str:
    if fills_today is None and stake_today is None:
        return ""
    ft = f"{int(fills_today)} fills" if fills_today is not None else "—"
    used = (f", {money(stake_today)} of {money(stake_cap)} used"
            if (stake_today is not None and stake_cap is not None) else "")
    return f" · today: {ft}{used}"


def _pair_risk(rest_price: Any, rest_shares: Any, hedge_price: Any, hedge_shares: Any,
               hedge_fee: Any, price: Any, size: Any) -> Optional[float]:
    """Total $ risked on a locked pair: rest notional + hedge notional + hedge fee."""
    rp = rest_price if rest_price is not None else price
    rs = rest_shares if rest_shares is not None else size
    total = 0.0
    ok = False
    if rp is not None and rs is not None:
        total += float(rp) * float(rs); ok = True
    if hedge_price is not None and hedge_shares is not None:
        total += float(hedge_price) * float(hedge_shares); ok = True
    if hedge_fee is not None:
        total += float(hedge_fee)
    return round(total, 4) if ok else None


def digest_line(minutes: float, *, placed: int, cancelled: int, fills: int, open_now: int,
                max_open: int, best_edge_pct: Optional[float] = None,
                kalshi_flaps: int = 0, kalshi_down_s: float = 0.0,
                binding: Optional[str] = None, stake_today: Optional[float] = None,
                stake_cap: Optional[float] = None, today_pnl: Optional[float] = None,
                why_no_fills: Optional[str] = None) -> str:
    """The periodic plain-language roll-up. Line 1: activity + best edge. Line 2: current state + daily
    usage. Line 3 (when there were 0 fills): WHY, in plain words. Kalshi WS flakiness is surfaced too."""
    edge = f" · best edge seen {best_edge_pct:.1f}%" if best_edge_pct is not None else ""
    l1 = f"📊 {int(minutes)}m summary · {placed} offers placed · {fills} taken{edge}"
    used = (f" · today {money(stake_today)}/{money(stake_cap)} used"
            if (stake_today is not None and stake_cap is not None) else "")
    pnl = f" · {signed_money(today_pnl)}" if today_pnl is not None else ""
    l2 = f"\n   Currently offering {open_now}/{max_open}{used} · {fills} fills{pnl}"
    flap = (f"\n   ⚠️ Kalshi feed was flaky: {int(kalshi_flaps)} drop{'s' if int(kalshi_flaps) != 1 else ''} "
            f"({kalshi_down_s:.0f}s) — REST poll covered fills") if kalshi_flaps else ""
    why = ""
    if fills == 0 and (why_no_fills or placed):
        reason = why_no_fills or "nobody crossed our price yet"
        why = f"\n   Why no fills: {reason}"
    return l1 + l2 + flap + why
