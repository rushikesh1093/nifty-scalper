"""
NSE trading calendar (equity derivatives segment).

A trading day is a weekday that is not an exchange holiday. Sources for the
2026 list (checked 2026-10-20; NSE circular "Trading holidays for the
calendar year 2026" — add each new year's list when NSE publishes it):
  https://upstox.com/stocks-market/nse-holidays/
  https://cleartax.in/s/nse-holidays-2026
Weekend holidays (15 Feb, 21 Mar, 15 Aug, 8 Nov) are omitted: weekends are
already closed. Special weekend sessions (Budget day 1 Feb, Muhurat 8 Nov) are
not traded by this system.
"""
from __future__ import annotations

from datetime import date

NSE_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 15): "Maharashtra municipal elections",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

# Years whose holiday list is loaded; outside them only weekends are known.
KNOWN_YEARS = frozenset(d.year for d in NSE_HOLIDAYS)


def holiday_name(day: date) -> str | None:
    return NSE_HOLIDAYS.get(day)


def is_trading_day(day: date) -> bool:
    """Weekday and not a listed NSE holiday."""
    return day.weekday() < 5 and day not in NSE_HOLIDAYS


def closed_reason(day: date) -> str | None:
    """Why the exchange is closed on *day*, or None on a trading day."""
    if day.weekday() >= 5:
        return "weekend"
    name = NSE_HOLIDAYS.get(day)
    return f"NSE holiday ({name})" if name else None
