"""Human-readable Telegram alert formatting for the live maker/hedger.

ONE line per event, emoji-led, all the facts, real team/player names (never the raw ticker). Pure and
unit-tested — the executor calls ``format_event`` and never builds an alert string itself.

    🟢 PLACED   ⚾ MLB · Yankees ML · $4.30 @ 86c · Kalshi · pre
    🔵 REPRICED ⚾ MLB · Yankees ML · 62c → 64c · Poly · in-play
    ⚪ CANCELLED ⚾ MLB · Yankees ML · reason: price moved
    💰 FILLED   🎾 Tennis · Sinner ML · $4.75 @ 95c · hedging…
    ✅ LOCKED   🎾 Tennis · Sinner ML · +$0.06 net (+1.3%) · hedge 4c Poly
    🟠 UNWOUND  ⚾ MLB · Yankees ML · sold 5 @ 61c · hedge missed
    🔴 HALTED   day loss cap hit (-$25.10)
    ⚠️ PROBLEM  place failed: market closed
"""
from __future__ import annotations

from typing import Any, Optional

SPORT_EMOJI = {"mlb": "⚾", "tennis": "🎾", "soccer": "⚽", "ufc": "🥊"}
SPORT_LABEL = {"mlb": "MLB", "tennis": "Tennis", "soccer": "Soccer", "ufc": "UFC"}
VENUE_LABEL = {"polymarket": "Poly", "poly": "Poly", "kalshi": "Kalshi"}

STATUS = {
    "placed":    ("🟢", "PLACED"),
    "repriced":  ("🔵", "REPRICED"),
    "cancelled": ("⚪", "CANCELLED"),
    "filled":    ("💰", "FILLED"),
    "locked":    ("✅", "LOCKED"),
    "unwound":   ("🟠", "UNWOUND"),
    "halted":    ("🔴", "HALTED"),
    "problem":   ("⚠️", "PROBLEM"),
}

# Internal reason slugs -> plain English (cancels / halts / problems).
REASON_HUMAN = {
    "reprice": "price moved", "reprice_cross": "price moved", "reprice_floor": "edge shrank",
    "would_cross_at_post": "would cross the book", "below_tick": "price below tick",
    "disarmed": "disarmed", "churn_gone": "market dropped", "age_out": "held too long (aged out)",
    "poly_user_down": "fill feed down", "kalshi_feed_down": "kalshi feed down",
    "halt_after_partial": "cap hit after partial fill", "orphan_halt": "orphan position",
    "hedge_thin_cooldown": "hedge too thin", "now_behind": "no longer best", "book_gone": "book gone",
    "inplay_stale": "book went stale", "inplay_frozen": "price shock — frozen",
    "shutdown": "shutting down", "market_closed": "market closed", "market_not_found": "market gone",
    "too_many_requests": "rate limited", "insufficient_balance": "insufficient balance",
    "post_only_would_cross": "would cross (post-only)", "market_not_active": "market not active",
}

_MONEYLINE_KEYS = {"ml", "ml2", "moneyline", "match_winner", "fight_winner", "winner"}


def _title(s: Any) -> str:
    """'kansas city' -> 'Kansas City'; already-cased names pass through."""
    t = str(s or "").strip()
    return " ".join(w[:1].upper() + w[1:] for w in t.split()) if t else ""


def cents(price: Any) -> str:
    """0.86 -> '86c'; None -> '—'. Rounds to the nearest cent."""
    try:
        return f"{round(float(price) * 100)}c"
    except (TypeError, ValueError):
        return "—"


def money(v: Any) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "$—"


def venue_label(venue: Any) -> str:
    return VENUE_LABEL.get(str(venue or "").lower(), str(venue or "").title() or "?")


def phase_label(phase: Any) -> str:
    return "in-play" if str(phase) == "inplay" else "pre"


def humanize_reason(reason: Any) -> str:
    r = str(reason or "").strip()
    if not r:
        return "—"
    # take the leading token for compound slugs like "unwind_FAILED sell_res=..."
    head = r.split()[0]
    return REASON_HUMAN.get(head, REASON_HUMAN.get(r, r.replace("_", " ")))


def _teams_pair(teams: Any) -> tuple[str, str]:
    """('away', 'home') from a 'AWAY vs HOME' string, else ('', '')."""
    s = str(teams or "")
    for sep in (" vs ", " v ", " @ ", "@"):
        if sep in s:
            a, _, h = s.partition(sep)
            return a.strip(), h.strip()
    return "", ""


def subject(sport: Any, game: Any, market_key: Any, side: Any, teams: Any = None) -> str:
    """The human subject of a quote: '<team/player> <market>'. Uses the real name, never the ticker.

    match/fight-winner & moneyline -> '<name> ML'; team totals -> '<Team> O2.5'/'U2.5';
    game totals -> 'Total O5.5'/'U5.5'. Falls back to a titled side + raw market_key when a shape is
    unrecognised (never the bare ticker)."""
    mkey = str(market_key or "")
    sd = str(side or "")

    # -- team totals: "team_total|<team>|<line>" ------------------------------
    if mkey.startswith("team_total") and "|" in mkey:
        parts = mkey.split("|")
        team = _title(parts[1]) if len(parts) > 1 else _title(sd)
        line = parts[2] if len(parts) > 2 else ""
        ou = "O" if sd.lower().startswith("o") else ("U" if sd.lower().startswith("u") else "")
        return f"{team} {ou}{line}".strip()

    # -- game totals: "total_goals|<line>", "total|<line>", "totals" ----------
    if mkey.startswith(("total_goals", "total", "totals", "goals")) and mkey not in _MONEYLINE_KEYS:
        line = mkey.split("|")[1] if "|" in mkey else ""
        ou = "O" if sd.lower().startswith("o") else ("U" if sd.lower().startswith("u") else "")
        return f"Total {ou}{line}".strip()

    # -- moneyline / match winner / fight winner ------------------------------
    if mkey in _MONEYLINE_KEYS or mkey.startswith(("ml", "moneyline")):
        low = sd.lower()
        if low in ("home", "away"):                       # resolve home/away via the matchup
            away, home = _teams_pair(teams)
            name = (away if low == "away" else home) or sd
        else:
            name = sd                                     # tennis/ufc: side IS the player/fighter
        return f"{_title(name)} ML".strip()

    # -- spreads --------------------------------------------------------------
    if mkey.startswith("spread"):
        line = mkey.split("|")[1] if "|" in mkey else ""
        return f"{_title(sd)} {line}".strip()

    # -- fallback: a titled side + the market key (readable, never the ticker) -
    return f"{_title(sd) or mkey}".strip() or str(game or "?")


def head(sport: Any, game: Any, market_key: Any, side: Any, teams: Any = None) -> str:
    """'⚾ MLB · Yankees ML' — the sport-emoji + league + subject prefix shared by every alert."""
    sp = str(sport or "").lower()
    emoji = SPORT_EMOJI.get(sp, "•")
    league = SPORT_LABEL.get(sp, (sp.upper() or "?"))
    return f"{emoji} {league} · {subject(sport, game, market_key, side, teams)}"


def _status(kind: str) -> str:
    e, label = STATUS.get(kind, ("•", kind.upper()))
    return f"{e} {label}"


def format_event(kind: str, *, sport: Any = None, game: Any = None, market_key: Any = None,
                 side: Any = None, teams: Any = None, venue: Any = None, phase: Any = None,
                 price: Any = None, size: Any = None, old_price: Any = None, new_price: Any = None,
                 reason: Any = None, pnl: Any = None, net_pct: Any = None, hedge_price: Any = None,
                 hedge_venue: Any = None, detail: Any = None) -> str:
    """Build the one-line alert for ``kind`` (a STATUS key). Missing facts degrade gracefully."""
    h = head(sport, game, market_key, side, teams)
    if kind == "placed":
        stake = money((float(price) * float(size)) if (price is not None and size is not None) else None)
        return f"{_status(kind)} {h} · {stake} @ {cents(price)} · {venue_label(venue)} · {phase_label(phase)}"
    if kind == "repriced":
        return (f"{_status(kind)} {h} · {cents(old_price)} → {cents(new_price)} · "
                f"{venue_label(venue)} · {phase_label(phase)}")
    if kind == "cancelled":
        return f"{_status(kind)} {h} · reason: {humanize_reason(reason)}"
    if kind == "filled":
        amt = money((float(price) * float(size)) if (price is not None and size is not None) else None)
        return f"{_status(kind)} {h} · {amt} @ {cents(price)} · hedging…"
    if kind == "locked":
        pnl_s = f"+{money(pnl)}" if (pnl is not None and float(pnl) >= 0) else money(pnl)
        pct = f" ({net_pct:+.1f}%)" if net_pct is not None else ""
        hedge = f" · hedge {cents(hedge_price)} {venue_label(hedge_venue)}" if hedge_price is not None else ""
        return f"{_status(kind)} {h} · {pnl_s} net{pct}{hedge}"
    if kind == "unwound":
        sold = f"sold {int(size)} @ {cents(price)}" if size is not None else "unwound"
        why = f" · {humanize_reason(reason)}" if reason else ""
        return f"{_status(kind)} {h} · {sold}{why}"
    if kind == "halted":
        return f"{_status(kind)} {str(detail or humanize_reason(reason))}"
    if kind == "problem":
        return f"{_status(kind)} {str(detail or humanize_reason(reason))}"
    return f"{_status(kind)} {h}"


def digest_line(minutes: float, *, placed: int, cancelled: int, fills: int, open_now: int,
                max_open: int, best_edge_pct: Optional[float] = None,
                kalshi_flaps: int = 0, kalshi_down_s: float = 0.0) -> str:
    """The periodic roll-up. Shows 'open X/Y', the best edge SEEN, and (when non-zero) Kalshi WS flap
    count + downtime this window — so a flaky fill socket is visible rather than inferred from the log.

        📊 15m · 12 placed · 9 cancelled · 0 fills · open 1/2 · best edge seen 0.9% · ⚠️ kalshi ws 3 flaps (12s down)
    """
    edge = f" · best edge seen {best_edge_pct:.1f}%" if best_edge_pct is not None else ""
    flap = (f" · ⚠️ kalshi ws {int(kalshi_flaps)} flap{'s' if int(kalshi_flaps) != 1 else ''} "
            f"({kalshi_down_s:.0f}s down)") if kalshi_flaps else ""
    return (f"📊 {int(minutes)}m · {placed} placed · {cancelled} cancelled · {fills} fills · "
            f"open {open_now}/{max_open}{edge}{flap}")
