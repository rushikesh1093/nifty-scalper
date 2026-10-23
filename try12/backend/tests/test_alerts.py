"""
Audit D9 (23 Oct): persistent log files and operator alerts.
"""
import asyncio
import logging
from dataclasses import replace

import pytest

from app.core import alerts
from app.core.logging import setup_logging
from app.runtime.context import using
from tests.helpers import run


@pytest.fixture(autouse=True)
def _clean():
    alerts.reset()
    yield
    alerts.reset()


def test_an_alert_is_logged_and_listed(caplog):
    with caplog.at_level(logging.INFO, logger="alerts"):
        assert alerts.alert("feed_down", "Market feed down", level=alerts.CRITICAL)
    assert "Market feed down" in caplog.text
    [a] = alerts.recent()
    assert a["key"] == "feed_down" and a["level"] == "CRITICAL"


def test_a_key_is_throttled():
    assert alerts.alert("low_disk", "low", throttle_s=3600)
    assert not alerts.alert("low_disk", "low again", throttle_s=3600)
    assert alerts.alert("other", "different key", throttle_s=3600)
    assert [a["key"] for a in alerts.recent()] == ["other", "low_disk"]


def test_a_replay_never_alerts(runtime_ctx):
    with using(replace(runtime_ctx, is_replay=True)):
        assert not alerts.alert("open:1", "OPENED in a backtest")
    assert alerts.recent() == []
    assert alerts.alert("open:2", "OPENED live")           # the live context still does


def test_telegram_is_sent_only_when_configured(monkeypatch):
    from app.core.config import settings
    sent = []

    async def _fake(token, chat, text):
        sent.append((token, chat, text))
    monkeypatch.setattr(alerts, "_send_telegram", _fake)

    async def _go(key):
        alerts.alert(key, "Risk halt DAILY_LOSS", level=alerts.CRITICAL)
        await asyncio.sleep(0)
        await asyncio.gather(*alerts._tasks)

    monkeypatch.setattr(settings, "ALERT_TELEGRAM_BOT_TOKEN", "")
    run(_go("a"))
    assert sent == []
    monkeypatch.setattr(settings, "ALERT_TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(settings, "ALERT_TELEGRAM_CHAT_ID", "42")
    run(_go("b"))
    assert sent == [("TOKEN", "42", "🚨 Risk halt DAILY_LOSS")]


def test_a_failing_push_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("config broken")
    monkeypatch.setattr(alerts, "_telegram_target", _boom)
    assert alerts.alert("x", "still fine") is False        # swallowed, logged


def test_halts_raise_an_alert_once(runtime_ctx, sqlite_db):
    from app.services.trading import risk_state

    async def _go():
        async with sqlite_db() as db:
            for _ in range(2):
                await risk_state._engage(db, runtime_ctx.run_id, runtime_ctx.clock.trading_day(),
                                         risk_state.HALT_DAILY_LOSS, scope=None,
                                         reason="loss Rs5,100", context={})
            await db.commit()
    run(_go())
    halts = [a for a in alerts.recent() if a["key"].startswith("halt:DAILY_LOSS")]
    assert len(halts) == 1 and halts[0]["level"] == "CRITICAL"
    assert "loss Rs5,100" in halts[0]["message"]


def test_log_files_are_written(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    alert_before = list(logging.getLogger("alerts").handlers)
    try:
        setup_logging("INFO", str(tmp_path), max_mb=1, backups=2)
        setup_logging("INFO", str(tmp_path))                 # a second call adds nothing
        assert sum(getattr(h, "_nifty_file", False) for h in root.handlers) == 1
        logging.getLogger("app.test").warning("plain line")
        alerts.alert("halt:x", "an alert line", level=alerts.CRITICAL)
        for h in root.handlers + logging.getLogger("alerts").handlers:
            h.flush()
        backend = (tmp_path / "backend.log").read_text(encoding="utf-8")
        alert_log = (tmp_path / "alerts.log").read_text(encoding="utf-8")
        assert "plain line" in backend and "an alert line" in backend
        assert "an alert line" in alert_log and "plain line" not in alert_log
    finally:
        for lg, keep in ((root, before), (logging.getLogger("alerts"), alert_before)):
            for h in list(lg.handlers):
                if h not in keep:
                    lg.removeHandler(h)
                    h.close()
