"""
Operator alerts (audit D9): feed down/up, low disk, risk halts, entries and exits.

``alert()`` is synchronous and never raises. Each alert is
  * logged on the ``alerts`` logger (console, backend.log and alerts.log),
  * kept in a small in-memory list served by GET /alerts,
  * sent to Telegram when ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID
    are set (fire-and-forget on the running event loop).
Alerts raised inside a replay/backtest context are dropped: a backtest must
not page the operator. A key repeats at most once per *throttle_s*.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("alerts")

RECENT_MAX = 100
_recent: deque = deque(maxlen=RECENT_MAX)
_last_sent: dict[str, float] = {}
_tasks: set = set()

INFO, WARNING, CRITICAL = "INFO", "WARNING", "CRITICAL"
_ICON = {INFO: "ℹ️", WARNING: "⚠️", CRITICAL: "🚨"}


def _in_replay() -> bool:
    try:
        from app.runtime.context import ctx, is_bound
        return is_bound() and bool(ctx().is_replay)
    except Exception:
        return False


def alert(key: str, message: str, *, level: str = WARNING, throttle_s: float = 0,
          **context) -> bool:
    """Raise an alert. Returns True if it was emitted (not throttled or dropped)."""
    try:
        if _in_replay():
            return False
        now = time.monotonic()
        last = _last_sent.get(key)
        if throttle_s and last is not None and now - last < throttle_s:
            return False
        _last_sent[key] = now
        item = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "key": key, "level": level, "message": message, "context": context}
        _recent.append(item)
        logger.log(getattr(logging, level, logging.WARNING), "[%s] %s%s", key, message,
                   f" {context}" if context else "")
        _push(item)
        return True
    except Exception as exc:                       # an alert must never break trading
        logging.getLogger(__name__).error("alert %s failed: %s", key, exc)
        return False


def recent(limit: int = 50) -> list[dict]:
    return list(_recent)[-limit:][::-1]


def reset() -> None:
    """Tests only."""
    _recent.clear()
    _last_sent.clear()


def _telegram_target() -> Optional[tuple[str, str]]:
    from app.core.config import settings
    token = getattr(settings, "ALERT_TELEGRAM_BOT_TOKEN", None)
    chat = getattr(settings, "ALERT_TELEGRAM_CHAT_ID", None)
    return (token, chat) if token and chat else None


def _push(item: dict) -> None:
    target = _telegram_target()
    if target is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return                                     # no loop (scripts): the log is enough
    t = loop.create_task(_send_telegram(*target, f"{_ICON.get(item['level'], '')} "
                                                  f"{item['message']}"))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _send_telegram(token: str, chat_id: str, text: str) -> None:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat_id, "text": text[:4000]})
            if r.status_code != 200:
                logging.getLogger(__name__).warning("Telegram alert refused: HTTP %s",
                                                    r.status_code)
    except Exception as exc:
        # never log the URL: it contains the bot token
        logging.getLogger(__name__).warning("Telegram alert failed: %s", type(exc).__name__)
