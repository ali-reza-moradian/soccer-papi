"""Presentation helpers shared by Telegram alerts, console logs, and CSV export.

Two house rules live here so they are applied identically everywhere:

  * Every timestamp the operator ever sees is converted to Eastern Time
    (America/Toronto) — UTC never leaks into an alert, a log line, or a CSV cell.
  * Every monetary value (stake, limit, T_max, profit, total investment) is shown
    to EXACTLY two decimals with a leading ``$`` (e.g. 0.195469 -> ``$0.19``).

It also turns raw market metadata into human labels: ``1/X/2`` become the actual
home/away team names (or "Draw"), and a market's line/threshold is made explicit
(``Asian Handicap (-1.5)``, ``Total Goals (Over 2.5)``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

# Where the operator lives. All display/export timestamps are rendered here.
LOCAL_TZ_NAME = "America/Toronto"


# --------------------------------------------------------------------------- #
# Timezone                                                                      #
# --------------------------------------------------------------------------- #
def _zone(tz_name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # pragma: no cover - missing tz database / bad name
            pass
    return timezone.utc


def parse_iso(ts: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string (or pass through a datetime) into an aware datetime.

    Naive inputs are assumed to be UTC so downstream conversion is well-defined.
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_local(dt: datetime, tz_name: str = LOCAL_TZ_NAME) -> datetime:
    aware = parse_iso(dt)
    if aware is None:  # pragma: no cover - defensive
        raise ValueError(f"cannot localize {dt!r}")
    return aware.astimezone(_zone(tz_name))


def now_local(tz_name: str = LOCAL_TZ_NAME) -> datetime:
    return datetime.now(_zone(tz_name))


def fmt_dt(ts: Any, tz_name: str = LOCAL_TZ_NAME, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Full local timestamp, e.g. ``2026-06-07 15:00 EDT``. ``?`` if unparseable."""
    dt = parse_iso(ts)
    return to_local(dt, tz_name).strftime(fmt) if dt is not None else "?"


def fmt_time(ts: Any, tz_name: str = LOCAL_TZ_NAME) -> str:
    """Local wall-clock time only, e.g. ``15:00 EDT``."""
    return fmt_dt(ts, tz_name, "%H:%M %Z")


def iso_local(ts: Any, tz_name: str = LOCAL_TZ_NAME) -> str:
    """Local ISO-8601 with offset for CSV cells, e.g. ``2026-06-07T15:00:00-04:00``."""
    dt = parse_iso(ts)
    if dt is None:
        return ""
    return to_local(dt, tz_name).strftime("%Y-%m-%dT%H:%M:%S%z")


def date_local(ts: Any, tz_name: str = LOCAL_TZ_NAME) -> str:
    """Local calendar date, e.g. ``2026-06-07``."""
    dt = parse_iso(ts)
    return to_local(dt, tz_name).strftime("%Y-%m-%d") if dt is not None else ""


def window_label(from_utc: Any, to_utc: Any) -> str:
    """Compact UTC range for the Telegram header, e.g. ``Jun 10 – Jun 12 UTC``.

    Deliberately UTC (not Eastern) because the scan window itself is defined in UTC.
    """
    a, b = parse_iso(from_utc), parse_iso(to_utc)
    if a is None or b is None:
        return ""
    a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    return f"{a.strftime('%b')} {a.day} – {b.strftime('%b')} {b.day} UTC"


# --------------------------------------------------------------------------- #
# Money / numbers                                                               #
# --------------------------------------------------------------------------- #
_CENTS = Decimal("0.01")


def dec2(v: Any) -> Decimal:
    """Truncate to two decimals as a ``Decimal`` (for exact further arithmetic).

    The operator asked that 0.195469 read as 0.19 — i.e. truncation, not the
    round-half-up that ``f"{x:.2f}"`` would give (0.20). We go through
    ``Decimal(str(x))`` so float noise (0.29 stored as 0.289999…) can't bite.
    """
    return Decimal(str(float(v))).quantize(_CENTS, rounding=ROUND_DOWN)


def _two_dp(v: Any) -> str:
    return str(dec2(v))


def money(v: Any) -> str:
    """``$X.XX`` — exactly two (truncated) decimals with a leading dollar sign."""
    try:
        return f"${_two_dp(v)}"
    except (TypeError, ValueError):
        return "$0.00"


def num2(v: Any) -> str:
    """A bare number to exactly two (truncated) decimals (e.g. odds, ROI%)."""
    try:
        return _two_dp(v)
    except (TypeError, ValueError):
        return "0.00"


# --------------------------------------------------------------------------- #
# Market / outcome labels                                                       #
# --------------------------------------------------------------------------- #
_LINE_HANDICAP_FAMILIES = {"asian_handicap", "euro_handicap"}
_LINE_TOTAL_FAMILIES = {"totals", "team_totals"}


def market_label(label: str, family: str = "", line: Any = None) -> str:
    """Market name with its line/threshold made explicit.

    e.g. ``Asian Handicap`` + line -1.5 -> ``Asian Handicap (-1.5)``;
         ``Total Goals``    + line 2.5  -> ``Total Goals (2.5)``.
    Handicaps are shown signed; totals as a bare threshold. If the line already
    appears in the label we leave it alone (the API name often includes it).
    """
    label = label or ""
    if line is None:
        return label
    try:
        lf = float(line)
    except (TypeError, ValueError):
        return label
    shown = f"{lf:+g}" if family in _LINE_HANDICAP_FAMILIES else f"{lf:g}"
    if f"({shown})" in label or shown.lstrip("+") in label:
        return label
    return f"{label} ({shown})"


def outcome_label(name: Any, home: str = "", away: str = "", family: str = "", line: Any = None) -> str:
    """Human outcome name.

    ``1`` -> home team, ``2`` -> away team, ``X`` -> ``Draw``. For totals,
    ``Over``/``Under`` carry the line (``Over 2.5``). Anything else passes through.
    """
    s = str(name).strip()
    low = s.lower()
    if s == "1" or low == "home":
        return home or "Home"
    if s == "2" or low == "away":
        return away or "Away"
    if low in ("x", "draw"):
        return "Draw"
    if line is not None and family in _LINE_TOTAL_FAMILIES and low in ("over", "under"):
        try:
            return f"{s} {float(line):g}"
        except (TypeError, ValueError):
            return s
    return s
