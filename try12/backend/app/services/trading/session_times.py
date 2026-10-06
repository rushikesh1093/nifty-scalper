"""
Trading-session time helpers.

Single source of truth for the configured square-off time
(``trading_config.square_off_time``, "HH:MM" IST) and the derived
new-entry cutoff (square-off minus ENTRY_CUTOFF_BUFFER_MIN minutes).
"""
import logging
from datetime import time as dtime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

DEFAULT_SQUARE_OFF      = dtime(15, 15)
ENTRY_CUTOFF_BUFFER_MIN = 10   # no new entries in the last 10 min before square-off

_warned_values: set[str] = set()


def parse_square_off(value: Any) -> dtime:
    """Parse "HH:MM" (or a time object). Invalid/None → DEFAULT_SQUARE_OFF (warns once per value)."""
    if isinstance(value, dtime):
        return value.replace(tzinfo=None)
    try:
        hh, mm = str(value).strip().split(":")[:2]
        return dtime(int(hh), int(mm))
    except Exception:
        key = repr(value)
        if key not in _warned_values:
            _warned_values.add(key)
            logger.warning(
                "Invalid square_off_time %s — using default %s IST",
                key, DEFAULT_SQUARE_OFF.strftime("%H:%M"),
            )
        return DEFAULT_SQUARE_OFF


def entry_cutoff(value: Any, buffer_min: int = ENTRY_CUTOFF_BUFFER_MIN) -> dtime:
    """Time after which no new entries are allowed: square-off minus *buffer_min*."""
    sq = parse_square_off(value)
    total = max(0, sq.hour * 60 + sq.minute - buffer_min)
    return dtime(total // 60, total % 60)


def is_after_entry_cutoff(now: dtime, value: Any) -> bool:
    """True when *now* (naive IST time) is at or past the entry cutoff."""
    return now.replace(tzinfo=None) >= entry_cutoff(value)


def is_market_hours(now: dtime) -> bool:
    """True between 09:15 and 15:30 IST (no holiday calendar)."""
    t = now.replace(tzinfo=None)
    return MARKET_OPEN <= t <= MARKET_CLOSE
