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
    "auto_flattened": ("🟠", "AUTO-FLATTENED"),
    "corrected": ("📘", "CORRECTED"),
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
                 payout: Any = None, roi_pct: Any = None, was: Any = None, name: Any = None,
                 sold: Any = None, floor_pct: Any = None, lifetime_settled: Any = None,
                 untracked: Any = None, **_ignore: Any) -> str:
    """Build the plain-language alert for ``kind``. Missing facts degrade gracefully. Never emits a
    ticker or UUID — pass the human name via side/teams (or ``name`` when the caller already has it)."""
    tag = _sport_tag(sport)
    name = name or bet_name(sport, game, market_key, side, teams)
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
        rp = rest_price if rest_price is not None else price
        rs = rest_shares if rest_shares is not None else size
        naked, naked_px = _unhedged(rs, hedge_shares, rp, hedge_price)
        # A FILL THAT BOUGHT NO HEDGE OF ITS OWN NEVER USES THIS TEMPLATE. On 2026-08-04 a 1-share
        # dust fill on Jeju/Bayern U1.5 @3c was announced "profit is GUARANTEED either way · pays
        # $1.00" with the hedge rendered "$— @ —" — the message showed no hedge and promised a
        # certainty in the same breath. (It happened to be covered by the pooled Poly position, but
        # the alert did not know that and had no business claiming it.) Its own short, honest line.
        if hedge_price is None or hedge_shares in (None, 0) or float(hedge_shares or 0.0) <= 1e-9:
            stake = (float(rp) * float(rs)) if (rp is not None and rs is not None) else None
            payoff = money(float(rs)) if rs is not None else "$—"
            return (f"ℹ️ · {tag} · {match} · tiny fill, no hedge bought for it"
                    f"\n   Bought {money(stake)} of {name} @ {cents(rp)} "
                    f"({venue_label(venue)}) — too small to hedge on its own."
                    f"\n   Two outcomes: it pays {payoff} if {name} comes in, or it is worth nothing "
                    f"if not. Nothing is guaranteed here; it settles on its own.")
        rest_amt = money((float(rp) * float(rs)) if (rp is not None and rs is not None) else None)
        hedge_amt = money((float(hedge_price) * float(hedge_shares)) if (hedge_price is not None and hedge_shares is not None) else None)
        risked = _pair_risk(rest_price, rest_shares, hedge_price, hedge_shares, hedge_fee, price, size)
        hedged_sh = min(float(rs), float(hedge_shares)) if rs is not None else float(hedge_shares)
        pays = money(hedged_sh)
        # "GUARANTEED either way" IS ONLY TRUE OF THE MATCHED SHARES. When the two legs are not the
        # same size the remainder is a naked directional bet that settles on its own, and calling the
        # whole thing guaranteed is how 26AUG04JEJBMU O/U5.5 was announced as a lock while 6.5722
        # Polymarket shares rode unhedged and lost $2.27 (-100%) — more than the pair's +$1.80 made.
        if naked > _NAKED_TOL:
            at_risk = money(naked * float(naked_px)) if naked_px is not None else "$—"
            l1 = (f"{_status(kind)} · {tag} · {match} · hedged {_sh(hedged_sh)} · "
                  f"{_sh(naked)} riding unhedged ({at_risk} at risk, settles on its own)")
        else:
            l1 = f"{_status(kind)} · {tag} · {match} · profit is GUARANTEED either way"
        l2 = (f"\n   Bought: {name} {rest_amt} @ {cents(rp)} "
              f"({venue_label(venue)}) + {hedge_nm} {hedge_amt} @ {cents(hedge_price)} ({venue_label(hedge_venue)})")
        roi = f" ({pct(net_pct)} ROI)" if net_pct is not None else ""
        l3 = f"\n   Total risked {money(risked)} → pays {pays} · 💵 net {signed_money(pnl)} after fees{roi}"
        if naked > _NAKED_TOL:
            l3 += (f"\n   ⚠️ that net covers the {_sh(hedged_sh)} that are matched. The extra "
                   f"{_sh(naked)} is a one-way bet I could not pair off — it wins or loses on its own.")
        return l1 + l2 + l3 + _lifetime_tail(fills_today, today_pnl, lifetime_pnl, lifetime_settled)

    if kind == "locked_thin":
        # NEGATIVE, BUT INSIDE THE ALLOWANCE THE POLICY GRANTS. Not an error and not a win: the execution
        # floor is -1.0% on purpose, because locking a marginally negative pair is cheaper than unwinding
        # it (an unwind pays spread + taker fee, ~0.5-2%, plus brief naked exposure). There was no such
        # state, so a pair that netted -0.00% was shouted at as "HEDGED AT A LOSS ... Investigate." A
        # guard that fires on correct behaviour is a guard an operator learns to scroll past.
        hedge_nm = other_name(name, teams)
        rp = rest_price if rest_price is not None else price
        rs = rest_shares if rest_shares is not None else size
        rest_amt = money((float(rp) * float(rs)) if (rp is not None and rs is not None) else None)
        hedge_amt = money((float(hedge_price) * float(hedge_shares))
                          if (hedge_price is not None and hedge_shares is not None) else None)
        risked = _pair_risk(rest_price, rest_shares, hedge_price, hedge_shares, hedge_fee, price, size)
        roi = f" ({pct(net_pct)})" if net_pct is not None else ""
        fl = f" (floor {pct(floor_pct)})" if floor_pct is not None else ""
        return (f"\u2139\ufe0f \u00b7 {tag} \u00b7 {match} \u00b7 hedged flat \u2014 locked slightly "
                f"negative, within policy"
                f"\n   Bought: {name} {rest_amt} @ {cents(rp)} ({venue_label(venue)}) + "
                f"{hedge_nm} {hedge_amt} @ {cents(hedge_price)} ({venue_label(hedge_venue)})"
                f"\n   Total risked {money(risked)} \u00b7 \U0001f4b5 net {signed_money(pnl)} after "
                f"fees{roi}{fl}"
                f"\n   Locking this is cheaper than unwinding it \u2014 no action needed.")

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
        # The warning names the FLOOR that was actually breached, not "$1.00/share": the pair price at
        # which a hedge becomes unacceptable is a function of the CONFIGURED floor, and hard-coding
        # break-even here is what made this alert fire on pairs the policy had just accepted.
        fl = f" (floor {pct(floor_pct)})" if floor_pct is not None else ""
        warn = (f"\n   \u26a0\ufe0f rest+hedge = {money(pair_ps)}/share and the net is worse than the "
                f"execution floor{fl} \u2014 the pre-hedge check should have declined this. Investigate."
                if pair_ps is not None else
                f"\n   \u26a0\ufe0f net edge is worse than the execution floor{fl} \u2014 the pre-hedge "
                f"check should have declined this. Investigate.")
        return l1 + l2 + l3 + warn

    if kind == "settled":
        won = f" · {_title(winner)} won" if winner else ""
        life = f" · Lifetime {signed_money(lifetime_pnl)} settled" if lifetime_pnl is not None else ""
        roi = f" ({pct(roi_pct)})" if roi_pct is not None else ""
        # A NAKED REMAINDER IS NOT A SECOND TRADE. One game settling can emit two of these — the matched
        # pair and the shares that never got paired off — and unlabelled they read as two independent
        # bets, one of which mysteriously lost 100%. On 26AUG04JEJBMU O/U5.5 the pair made +$1.80 and the
        # 6.5722-share remainder lost $2.27; side by side and unnamed, that looks like a bug rather than
        # the one thing it is: the leftover from a hedge that could not be sized exactly.
        if untracked:
            return (f"{_status(kind)} · {tag} · {match}{won} · the UNHEDGED REMAINDER of this trade\n"
                    f"   These are the {money(cost)} of shares I could not pair off. Collected "
                    f"{money(payout)} · 💵 {signed_money(pnl)}{roi} — a one-way bet, so this is luck, "
                    f"not edge. The hedged pair on this game is reported separately.{life}")
        return (f"{_status(kind)} · {tag} · {match}{won}\n"
                f"   Collected {money(payout)} on {money(cost)} · 💵 {signed_money(pnl)}{roi}{life}")

    if kind == "unwound":
        return (f"{_status(kind)} · {tag} · {match} · couldn't hedge in time, sold back\n"
                f"   Cost me {money(cost)} (that's the safety net working — no open risk)")

    if kind == "auto_flattened":
        who = str(name or "").strip() or match
        return (f"{_status(kind)} · {who} · a leftover position was small enough to clear myself, so I "
                f"closed it instead of stopping\n"
                f"   Cost me {money(cost)} · I'm holding nothing on it now and I'm still trading")

    if kind == "corrected":
        who = str(name or "").strip() or match
        return (f"{_status(kind)} · {who} · I'd assumed the worst while this was open; the exchange has "
                f"now told me what it really came to\n"
                f"   Booked {signed_money(was)} → actually {signed_money(pnl)}")

    if kind == "halted":
        return f"{_status(kind)} · {str(detail or humanize_reason(reason))}"

    if kind == "problem":
        return f"{_status(kind)} · {str(detail or humanize_reason(reason))}"

    return f"{_status(kind)} · {tag} · {match}"


#: Shares below this are rounding, not exposure. Poly fills fractional counts to 6dp and Kalshi to 2dp,
#: so a matched pair routinely differs by ~1e-4 of a share; treating that as "riding unhedged" would put
#: a warning on every single lock.
_NAKED_TOL = 0.01


def _sh(n: Any) -> str:
    """'6.5722 sh' / '21 sh' — share counts, trimmed. Both venues fill fractionally, so the decimals are
    real and dropping them would hide exactly the remainder this exists to name."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return "— sh"
    return f"{int(f)} sh" if abs(f - round(f)) < 1e-9 else f"{f:.4f}".rstrip("0").rstrip(".") + " sh"


def _unhedged(rest_shares: Any, hedge_shares: Any, rest_price: Any,
              hedge_price: Any) -> tuple[float, Optional[float]]:
    """(shares riding unhedged, the price they are exposed at) for a booked pair.

    The remainder can sit on EITHER leg and the two mean different things, so the price follows the side
    that actually holds it: more hedge than rest is an over-bought hedge exposed at ``hedge_price``
    (26AUG04JEJBMU O/U5.5 bought 142.2222 Polymarket shares against 135.65 on Kalshi); more rest than
    hedge is an under-hedged fill exposed at ``rest_price``. Returns (0.0, None) when either count is
    unknown — an unknown remainder must not be reported as a known zero."""
    try:
        r, h = float(rest_shares), float(hedge_shares)
    except (TypeError, ValueError):
        return 0.0, None
    d = h - r
    if abs(d) <= _NAKED_TOL:
        return 0.0, None
    return (abs(d), hedge_price) if d > 0 else (abs(d), rest_price)


def _lifetime_tail(fills_today: Any, today_pnl: Any, lifetime_pnl: Any,
                   lifetime_settled: Any = None) -> str:
    """The 'Today / Lifetime' footer, with LIFETIME SAYING WHICH LIFETIME IT MEANS.

    ``lifetime_pnl`` on a fill alert is settled venue truth PLUS today's fill-time locked ESTIMATE of
    pairs that have not settled. Those are two different kinds of number and the label said neither: on
    2026-08-04 the overnight alerts read "Lifetime: +$33.62" while the settled truth after the morning
    was +$32.38. Where the caller knows the settled figure, both are printed and named; where it does
    not, the compound number is at least labelled as compound."""
    if fills_today is None and today_pnl is None and lifetime_pnl is None:
        return ""
    ft = f"{int(fills_today)} fills · " if fills_today is not None else ""
    if lifetime_settled is not None:
        life = (f" · Lifetime: {signed_money(lifetime_settled)} settled "
                f"({signed_money(lifetime_pnl)} including today's estimate)")
    elif lifetime_pnl is not None:
        life = f" · Lifetime: {signed_money(lifetime_pnl)} (settled + today's estimate)"
    else:
        life = ""
    return f"\n   📈 Today: {ft}{signed_money(today_pnl)}{life}"


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
                poly_flaps: int = 0, poly_down_s: float = 0.0,
                reconnects: Optional[dict] = None, prehedge_declines: int = 0,
                binding: Optional[str] = None, stake_today: Optional[float] = None,
                stake_cap: Optional[float] = None, today_pnl: Optional[float] = None,
                why_no_fills: Optional[str] = None, refuse_suppressed: int = 0,
                closed_markets: Optional[list] = None, fills_today: Optional[int] = None) -> str:
    """The periodic plain-language roll-up. Line 1: activity + best edge. Line 2: current state + daily
    usage. Then, when they happened: feed flakiness, reconnect attempt/success totals, pre-hedge declines,
    and (on 0 fills) WHY in plain words.

    THE TWO LINES COVER TWO DIFFERENT WINDOWS, and each now says which. Line 1 is THIS interval; line 2 is
    TODAY. They used to be mixed: the daily stake and the daily pnl sat next to the INTERVAL's fill count,
    so an eleven-fill day printed "0 fills · +$1.67" — a number that is either impossible or an admission
    that the counters disagree, and no way to tell which from the message."""
    edge = f" · best edge seen {best_edge_pct:.1f}%" if best_edge_pct is not None else ""
    l1 = f"📊 {int(minutes)}m summary · {placed} offers placed · {fills} taken{edge}"
    # Every figure after "Today:" is a TODAY figure — including the fill count, which is where the
    # interval's count used to be spliced in.
    today = []
    if fills_today is not None:
        today.append(f"{int(fills_today)} fills")
    if stake_today is not None and stake_cap is not None:
        today.append(f"{money(stake_today)}/{money(stake_cap)} used")
    if today_pnl is not None:
        today.append(signed_money(today_pnl))
    day = f"\n   Today: {' · '.join(today)}" if today else ""
    l2 = f"\n   Currently offering {open_now}/{max_open}{day}"
    flap = (f"\n   ⚠️ Kalshi feed was flaky: {int(kalshi_flaps)} drop{'s' if int(kalshi_flaps) != 1 else ''} "
            f"({kalshi_down_s:.0f}s) — REST poll covered fills") if kalshi_flaps else ""
    pflap = (f"\n   ⚠️ Polymarket fill feed was flaky: {int(poly_flaps)} "
             f"drop{'s' if int(poly_flaps) != 1 else ''} ({poly_down_s:.0f}s) — REST poll covered fills"
             ) if poly_flaps else ""
    # Reconnect ledger: "tried N, came back M". M < N on a settled digest == a feed that is still down.
    _human = {"poly_user": "my Polymarket fill feed", "poly_market": "the Polymarket price feed",
              "kalshi": "the Kalshi feed"}
    rec = ""
    for _name, _pair in sorted((reconnects or {}).items()):
        try:
            _att, _ok = int(_pair[0]), int(_pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if _att:
            mark = "✅" if _ok >= _att else "❗"
            label = _human.get(_name, _name)
            rec += (f"\n   {mark} {label} dropped {_att}x and came back {_ok}x"
                    + ("" if _ok >= _att else " — still down"))
    decl = (f"\n   🛡️ {int(prehedge_declines)} fill{'s' if int(prehedge_declines) != 1 else ''} refused a "
            f"hedge that would have lost money (unwound instead)") if prehedge_declines else ""
    # The number that was counted and never shown: how many times a market I wanted to offer on had to
    # wait because every slot was already in use. It is the ONLY signal that says "more slots would mean
    # more offers", and it was write-only for as long as it existed.
    wait = (f"\n   ⏳ {int(refuse_suppressed)} times I wanted to offer but every slot was busy"
            ) if refuse_suppressed else ""
    # FINISHED MATCHES, batched and NAMED. Each of these used to be its own instant "something went
    # wrong" alert that did not even say which game — and a dozen arrive together at the end of a slate.
    # A market closing is the ordinary end of its life, not an incident, so it belongs in the roll-up.
    cm = [str(x) for x in (closed_markets or [])]
    shown = ", ".join(cm[:4]) + (f" +{len(cm) - 4} more" if len(cm) > 4 else "")
    closed = f"\n   🏁 {len(cm)} market(s) finished, dropped for today: {shown}" if cm else ""
    why = f"\n   Why no fills: {why_no_fills}" if (fills == 0 and why_no_fills) else ""
    return l1 + l2 + flap + pflap + rec + decl + wait + closed + why
