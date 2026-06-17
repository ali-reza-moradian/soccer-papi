"""Format and send the top-N opportunities to the Telegram group.

Failures never crash the run — they are logged and retried a couple of times.
"""
from __future__ import annotations

import html
import logging
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from . import formatting as fmt

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


UNVERIFIED_NOTE = ("Size unverified — check the real max on the book before trusting this profit.")
SHADOW_LABEL = "SHADOW — not bettable, no account there"


def format_opportunity(arb: dict[str, Any], local_tz: str = fmt.LOCAL_TZ_NAME) -> str:
    """Build a clean, scannable HTML block for one opportunity (REAL or SHADOW).

    Layout per arb:
        <game> · <kickoff>
        <market> — REAL | SHADOW — not bettable, no account there
          <book> — <outcome> @ <odds> -> stake $X (limit $Y | UNVERIFIED)
        ROI <r>% · profit $P on total $T
        [Size unverified — ...]   (only when a leg has no verified limit)

    Money is whole dollars; odds keep two decimals; ROI two decimals. Minimal emoji.
    """
    home = arb.get("home_team") or ""
    away = arb.get("away_team") or ""
    family = arb.get("market_family") or ""
    line = arb.get("market_line")
    market = fmt.market_label(arb.get("market", ""), family, line)

    is_real = bool(arb.get("actionable"))
    kind = "REAL" if is_real else SHADOW_LABEL
    extra = []
    if arb.get("suspicious"):
        extra.append("⚠️ suspicious")
    extra_s = ("  " + " · ".join(extra)) if extra else ""

    game = arb.get("match", "?")
    lines = [
        f"<b>{_esc(game)}</b> · {_esc(fmt.fmt_dt(arb.get('kickoff_utc'), local_tz))}",
        f"{_esc(market)} — <b>{_esc(kind)}</b>{extra_s}",
    ]

    legs = arb.get("legs", [])
    any_unverified = False
    for leg in legs:
        outcome = fmt.outcome_label(leg.get("outcome", ""), home, away, family, line)
        unverified = bool(leg.get("unverified"))
        limit = leg.get("limit")
        if unverified or not isinstance(limit, (int, float)) or not limit:
            any_unverified = any_unverified or unverified
            limit_s = "UNVERIFIED"
        else:
            limit_s = f"limit {fmt.money0(limit)}"
        stake = leg.get("stake")
        stake_s = fmt.money0(stake) if isinstance(stake, (int, float)) else "?"
        lines.append(
            f"  {_esc(leg.get('book'))} — {_esc(outcome)} @ <b>{fmt.num2(leg.get('decimal_odds'))}</b> "
            f"→ stake {stake_s} ({_esc(limit_s)})"
        )

    roi = arb.get("roi_pct")
    profit = arb.get("max_profit")
    total = arb.get("total_investment", arb.get("max_liquidity"))
    lines.append(
        f"ROI <b>{fmt.num2(roi)}%</b> · profit <b>{fmt.money0(profit)}</b> on total <b>{fmt.money0(total)}</b>"
    )
    if any_unverified:
        lines.append(_esc(UNVERIFIED_NOTE))
    return "\n".join(lines)


def build_message(opportunities: list[dict[str, Any]], header: str, local_tz: str) -> str:
    blocks = [header]
    for arb in opportunities:
        blocks.append("")  # blank line separates arbs
        blocks.append(format_opportunity(arb, local_tz))
    return "\n".join(blocks)


@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    reraise=True,
)
def _post(url: str, payload: dict[str, Any], timeout: float) -> requests.Response:
    return requests.post(url, json=payload, timeout=timeout)


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    log: logging.Logger,
    disable_preview: bool = True,
) -> bool:
    """Send one HTML message. Returns True on success; never raises."""
    if not bot_token or not chat_id:
        log.warning("Telegram not configured (missing token/chat_id) — skipping send.")
        return False
    url = TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    try:
        resp = _post(url, payload, timeout=20)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        log.error("Telegram send failed: %s %s", resp.status_code, resp.text[:300])
        return False
    except Exception as exc:  # pragma: no cover - network
        log.error("Telegram send error: %s", exc)
        return False
