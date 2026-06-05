"""Format and send the top-N opportunities to the Telegram group.

Failures never crash the run — they are logged and retried a couple of times.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _fmt_kickoff(kickoff_utc: Optional[str], local_tz: str) -> str:
    if not kickoff_utc:
        return "?"
    try:
        dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    except ValueError:
        return _esc(kickoff_utc)
    utc_s = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if ZoneInfo is not None:
        try:
            local = dt.astimezone(ZoneInfo(local_tz))
            return f"{utc_s} / {local.strftime('%H:%M %Z')}"
        except Exception:  # pragma: no cover - bad tz name
            pass
    return utc_s


def format_opportunity(arb: dict[str, Any], local_tz: str) -> str:
    """Build an HTML message body for one opportunity from its CSV-row dict."""
    flags = []
    if arb.get("suspicious"):
        flags.append("⚠️ SUSPICIOUS")
    if arb.get("low_confidence"):
        flags.append("🌫 low-confidence")
    if arb.get("involves_exchange"):
        flags.append("🔁 exchange")
    if not arb.get("actionable"):
        flags.append("👀 shadow")
    flag_line = ("  " + " · ".join(flags)) if flags else ""

    lines = [
        f"<b>{_esc(arb.get('match', '?'))}</b>{flag_line}",
        f"🕑 {_fmt_kickoff(arb.get('kickoff_utc'), local_tz)}",
        f"🏆 {_esc(arb.get('tournament', ''))}",
        f"📊 <b>{_esc(arb.get('market', ''))}</b>",
    ]

    legs = arb.get("legs", [])
    for leg in legs:
        limit = leg.get("limit")
        limit_s = f"limit {limit:g}" if isinstance(limit, (int, float)) and limit else "limit n/a"
        stake = leg.get("stake")
        stake_s = f" → stake {stake:g}" if isinstance(stake, (int, float)) else ""
        lines.append(
            f"  • {_esc(leg.get('book'))} — {_esc(leg.get('outcome'))} "
            f"@ <b>{leg.get('decimal_odds'):g}</b> ({_esc(limit_s)}){stake_s}"
        )

    roi = arb.get("roi_pct")
    tmax = arb.get("max_liquidity")
    profit = arb.get("max_profit")
    lines.append(
        f"💰 ROI <b>{roi:.2f}%</b> · T_max <b>{tmax:g}</b> · profit <b>{profit:g}</b>"
    )

    links = arb.get("bet_links", {})
    if links:
        link_bits = [f'<a href="{_esc(url)}">{_esc(book)}</a>' for book, url in links.items() if url]
        if link_bits:
            lines.append("🔗 " + " · ".join(link_bits))

    return "\n".join(lines)


def build_message(opportunities: list[dict[str, Any]], header: str, local_tz: str) -> str:
    blocks = [header]
    for i, arb in enumerate(opportunities, start=1):
        blocks.append(f"\n<b>#{i}</b>")
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
