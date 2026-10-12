"""Canonical timestamp handling for market candles."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

IST = ZoneInfo("Asia/Kolkata")
# AngelOne candle timestamps are emitted by tick_writer in IST when naive values
# are encountered. Keeping this explicit avoids silently treating unknown naive
# feed values as UTC.
FEED_SOURCE_TIMEZONE = IST


def normalize_candle_timestamp(raw: Any, *, naive_source_tz: ZoneInfo = FEED_SOURCE_TIMEZONE) -> datetime:
    """Return an aware timestamp in Asia/Kolkata.

    Offset-aware values retain their instant. Naive values are interpreted using
    the explicitly supplied feed source timezone; callers must not infer this
    from the string or the server's local timezone.
    """
    if isinstance(raw, datetime):
        parsed = raw
    else:
        value = str(raw or "").strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=naive_source_tz)
    return parsed.astimezone(IST)


def try_normalize_candle_timestamp(raw: Any, *, naive_source_tz: ZoneInfo = FEED_SOURCE_TIMEZONE) -> datetime | None:
    try:
        return normalize_candle_timestamp(raw, naive_source_tz=naive_source_tz)
    except (TypeError, ValueError, OverflowError):
        return None
