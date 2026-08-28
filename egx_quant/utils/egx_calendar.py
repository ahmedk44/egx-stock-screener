"""EGX trading calendar: Africa/Cairo timezone and session-hour helpers.

EGX regular session: Sunday-Thursday, 10:00 -> 14:30 Cairo local time
(EET UTC+2 / EEST UTC+3 handled by pytz).
"""

from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Optional

import pytz

CAIRO_TZ = pytz.timezone("Africa/Cairo")

SESSION_OPEN: dt_time = dt_time(10, 0)
SESSION_CLOSE: dt_time = dt_time(14, 30)

TRADING_WEEKDAYS = {6, 0, 1, 2, 3}


def now_cairo() -> datetime:
    """Current wall-clock time in Africa/Cairo."""
    return datetime.now(CAIRO_TZ)


def to_cairo(moment: datetime) -> datetime:
    """Convert an arbitrary datetime (naive treated as UTC) to Cairo local time."""
    if moment.tzinfo is None:
        moment = pytz.utc.localize(moment)
    return moment.astimezone(CAIRO_TZ)


def is_trading_day(day: Optional[datetime] = None) -> bool:
    """True if the given Cairo-local day falls on Sun-Thu."""
    m = to_cairo(day) if day else now_cairo()
    return m.weekday() in TRADING_WEEKDAYS


def is_market_open(moment: Optional[datetime] = None) -> bool:
    """True only during the EGX regular session (Sun-Thu 10:00-14:30 Cairo)."""
    m = to_cairo(moment) if moment else now_cairo()
    return is_trading_day(m) and SESSION_OPEN <= m.time() < SESSION_CLOSE


def session_label(moment: Optional[datetime] = None) -> str:
    return "OPEN" if is_market_open(moment) else "CLOSED"
