"""Live market-data health checks (app.services.market_data.data_health)."""
from datetime import datetime, timedelta, timezone

from app.services.market_data import data_health as dh

IST = timezone(timedelta(hours=5, minutes=30))
NOW_LIVE = datetime(2026, 11, 3, 10, 0, 30, tzinfo=IST).astimezone(timezone.utc)
NOW_SUNDAY = datetime(2026, 11, 1, 10, 0, tzinfo=IST).astimezone(timezone.utc)


def _c(minute, o, h, l, c):
    t = datetime(2026, 11, 3, 9, 15, tzinfo=IST) + timedelta(minutes=minute)
    return {"timestamp": t.isoformat(), "open": o, "high": h, "low": l, "close": c}


def _clean(n=44):
    return [_c(i, 22000 + i, 22002 + i, 21998 + i, 22001 + i) for i in range(n)]


def test_clean_candles_are_ok_in_session():
    r = dh.candle_checks(_clean(), NOW_LIVE)                          # newest 09:58 closes 09:59
    assert r["status"] == dh.OK and r["duplicates"] == 0 and not r["invalid_ohlc"]


def test_duplicate_invalid_and_outlier_candles_are_flagged():
    c = _clean()
    c.append(dict(c[10]))                                             # duplicate minute
    c[5] = _c(5, 22005, 21990, 22010, 22006)                          # high < low
    r = dh.candle_checks(c, NOW_LIVE)
    assert r["status"] == dh.FAIL and r["duplicates"] == 1 and r["invalid_ohlc"]
    c2 = _clean()
    c2[20] = _c(20, 22020, 22400, 22018, 22390)                       # a 370-pt jump in one minute
    r2 = dh.candle_checks(c2, NOW_LIVE)
    assert r2["status"] == dh.WARN and r2["outliers"][0]["move"] > 300


def test_stale_candles_fail_in_session_and_are_closed_on_a_weekend():
    assert dh.candle_checks(_clean(20), NOW_LIVE)["status"] == dh.FAIL     # newest 09:34
    assert dh.candle_checks(_clean(20), NOW_SUNDAY)["status"] == dh.CLOSED
    assert dh.candle_checks([], NOW_LIVE)["status"] == dh.FAIL


def _tick(age_s, bid, ask, lat_ms=200):
    ts = NOW_LIVE - timedelta(seconds=age_s)
    return {"ts": ts.isoformat(), "best_bid": bid, "best_ask": ask,
            "exchange_ts": int(ts.timestamp() * 1000 - lat_ms)}


def test_quotes_count_stale_crossed_one_sided_and_latency():
    ticks = {"a": _tick(1, 100, 100.5), "b": _tick(120, 50, 50.5), "c": _tick(1, 31, 30),
             "d": _tick(1, 0, 10), "e": None, "f": _tick(1, 10, 12)}
    r = dh.quote_checks(ticks, NOW_LIVE, stale_s=60, max_spread_pct=5.0)
    assert (r["tokens"], r["no_tick"], r["stale"], r["crossed"], r["one_sided"], r["wide_spread"]) == \
        (6, 1, 1, 1, 1, 1)
    assert r["latency_ms_p50"] == 200 and r["status"] == dh.WARN      # 20% stale, crossed
    assert dh.quote_checks(ticks, NOW_SUNDAY, 60, 5.0)["status"] == dh.CLOSED


def test_overall_takes_the_worst():
    assert dh.overall([dh.OK, dh.WARN]) == dh.WARN
    assert dh.overall([dh.OK, dh.FAIL, dh.WARN]) == dh.FAIL
    assert dh.overall([dh.CLOSED, dh.CLOSED]) == dh.CLOSED
    assert dh.overall([dh.CLOSED, dh.OK]) == dh.OK


def test_endpoint_reads_all_ticks_in_one_round_trip(monkeypatch):
    """Regression: the endpoint gathered one get_tick per subscribed token (173
    live), exhausting the 20-connection Redis pool ("Too many connections")."""
    import asyncio
    from app.api.routes import market as m
    calls = {"get_tick": 0, "multi": 0}

    class _Cache:
        async def get(self, key):
            return None

        async def get_candles(self, token, interval, n):
            return []

        async def get_tick(self, token):
            calls["get_tick"] += 1
            return None

        async def get_multiple_ticks(self, tokens):
            calls["multi"] += 1
            return {t: None for t in tokens}

    monkeypatch.setattr(m, "redis_cache", _Cache())
    monkeypatch.setattr(m.market_feed, "_subscriptions", {1: [str(i) for i in range(173)]})
    out = asyncio.run(m.data_health())
    assert calls == {"get_tick": 0, "multi": 1}
    assert out["quotes"]["tokens"] == 173
