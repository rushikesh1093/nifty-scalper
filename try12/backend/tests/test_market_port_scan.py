"""
MarketDataPort / InstrumentPort conversion completeness (design §14.1, §14.2,
§23 R4 findings 7 and 12, R5 finding 3).
"""
import inspect
import re
from datetime import date, timedelta
from pathlib import Path

from app.runtime.live import LiveInstruments, RedisMarketData
from app.runtime.ports import MarketDataPort
from app.services.cache.redis_client import redis_cache
from tests.helpers import FakeRedis, run

BACKEND = Path(__file__).resolve().parents[1]
SCOPE = ("app/services/trading", "app/strategies")
CACHE_BYPASS = re.compile(r"redis_cache|\._r\(")
INSTRUMENT_BYPASS = re.compile(r"Instrument\.is_active|get_active_expiries|get_option_tokens")


def _hits(root: Path, pattern: re.Pattern) -> list[str]:
    out = []
    for d in SCOPE:
        for p in sorted((root / d).rglob("*.py")):
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    out.append(f"{p.relative_to(root).as_posix()}:{n}: {line.strip()}")
    return out


def test_no_redis_cache_or_raw_client_in_decision_code():
    hits = _hits(BACKEND, CACHE_BYPASS)
    assert not hits, "cache access outside MarketDataPort:\n" + "\n".join(hits)


def test_no_instrument_universe_bypass_in_decision_code():
    hits = _hits(BACKEND, INSTRUMENT_BYPASS)
    assert not hits, "instrument universe outside InstrumentPort:\n" + "\n".join(hits)


def test_scans_fail_on_synthetic_modules(tmp_path):
    mod = tmp_path / "app" / "services" / "trading" / "synthetic.py"
    mod.parent.mkdir(parents=True)
    (tmp_path / "app" / "strategies").mkdir(parents=True)
    mod.write_text("x = await redis_cache._r().get(k)\n"
                   "q = select(I).where(Instrument.is_active == True)\n", encoding="utf-8")
    assert len(_hits(tmp_path, CACHE_BYPASS)) == 1
    assert len(_hits(tmp_path, INSTRUMENT_BYPASS)) == 1


def _public_async_methods(cls) -> set[str]:
    return {n for n, f in inspect.getmembers(cls, inspect.iscoroutinefunction) if not n.startswith("_")}


def test_port_surface_is_implemented_by_live_and_fake():
    port = _public_async_methods(MarketDataPort)
    assert port == {"get_ltp", "get_tick", "get_candles", "get_option_chain",
                    "get", "set", "delete", "publish_signal"}
    assert port <= _public_async_methods(RedisMarketData)
    assert port <= _public_async_methods(FakeRedis)


def test_publish_signal_signatures_agree():
    def names(f):
        return [p for p in inspect.signature(f).parameters if p != "self"]
    assert names(MarketDataPort.publish_signal) == names(RedisMarketData.publish_signal) \
        == names(redis_cache.publish_signal) == ["strategy", "signal"]


def test_regime_persistence_round_trips_objects_through_the_port(runtime_ctx):
    """Object-in/object-out (design §14.1): no double-encoded JSON strings."""
    from datetime import datetime, timezone
    from app.services.trading.regime_indicators import RegimeIndicators
    from app.services.trading.regime_persistence import RegimeState, regime_persistence

    ind = RegimeIndicators(adx=25.0, atr=5.0, atr_percentile=50.0, vwap=100.0, price=101.0,
                           vwap_cross_count=1, volume=0.0, avg_volume=0.0,
                           orb_high=None, orb_low=None, pdh=None, pdl=None)
    st = RegimeState(regime="BULL_TREND_NORMAL_VOL", underlying="NIFTY",
                     timestamp=runtime_ctx.clock.now_utc(), indicators=ind)
    run(regime_persistence.save_regime_state(st))
    run(regime_persistence.save_regime_history("NIFTY", [st]))
    stored = runtime_ctx.market.kv["regime:NIFTY:current"]
    assert isinstance(stored, dict) and stored["regime"] == "BULL_TREND_NORMAL_VOL"
    assert isinstance(runtime_ctx.market.kv["regime:NIFTY:history"], list)
    back = run(regime_persistence.load_regime_state("NIFTY"))
    assert back is not None and back.regime == st.regime
    assert [s.regime for s in run(regime_persistence.load_regime_history("NIFTY"))] == [st.regime]


# ── LiveInstruments (R5 finding 3) ───────────────────────────────────────────

async def _seed_instruments(Session, today: date):
    from app.db.models.instrument import Instrument, InstrumentType, OptionType
    rows = []
    for k, dte in enumerate((2, 9, 16)):
        for ot in (OptionType.CE, OptionType.PE):
            rows.append(Instrument(token=f"T{k}{ot.value}", symbol=f"NIFTY{k}{ot.value}", name="NIFTY",
                                   expiry=today + timedelta(days=dte), strike=22600.0, option_type=ot,
                                   instrument_type=InstrumentType.OPTIDX, lot_size=75, is_active=True))
    rows.append(Instrument(token="OLD", symbol="NIFTYOLD", name="NIFTY", expiry=today - timedelta(days=1),
                           strike=22600.0, option_type=OptionType.CE,
                           instrument_type=InstrumentType.OPTIDX, lot_size=75, is_active=True))
    rows.append(Instrument(token="DEAD", symbol="NIFTYDEAD", name="NIFTY", expiry=today + timedelta(days=2),
                           strike=22650.0, option_type=OptionType.CE,
                           instrument_type=InstrumentType.OPTIDX, lot_size=75, is_active=False))
    async with Session() as db:
        db.add_all(rows)
        await db.commit()


def _instrument_table(Session):
    from sqlalchemy import create_engine
    from app.db.base import Base
    from app.db.models.instrument import Instrument
    url = str(Session.kw["bind"].url).replace("+aiosqlite", "")
    eng = create_engine(url)
    Base.metadata.create_all(eng, tables=[Instrument.__table__])
    eng.dispose()


class _CountingSession:
    """Wraps an AsyncSession and counts execute() calls."""

    def __init__(self, inner):
        self._inner, self.executes = inner, 0

    async def execute(self, *a, **k):
        self.executes += 1
        return await self._inner.execute(*a, **k)

    def expunge(self, obj):
        self._inner.expunge(obj)


def test_live_instruments_spans_expiries_and_caches_per_day(runtime_ctx, sqlite_db):
    _instrument_table(sqlite_db)
    today = runtime_ctx.clock.trading_day()
    run(_seed_instruments(sqlite_db, today))
    port = LiveInstruments()

    async def _go():
        async with sqlite_db() as db:
            spy = _CountingSession(db)
            first = await port.instruments_for(spy, "nifty")
            second = await port.instruments_for(spy, "NIFTY")
            return first, second, spy.executes

    first, second, executes = run(_go())
    assert sorted({i.expiry for i in first}) == [today + timedelta(days=d) for d in (2, 9, 16)]
    assert {i.token for i in first}.isdisjoint({"OLD", "DEAD"})      # is_active and expiry >= as_of
    assert [i.token for i in second] == [i.token for i in first]
    assert executes == 1                                              # second call: no query

    port.invalidate()

    async def _again():
        async with sqlite_db() as db:
            spy = _CountingSession(db)
            await port.instruments_for(spy, "NIFTY")
            return spy.executes
    assert run(_again()) == 1


def test_instrument_sync_route_invalidates_the_cache(runtime_ctx, monkeypatch):
    from app.api.routes import instruments as routes

    async def _sync(db):
        return 7
    monkeypatch.setattr(routes, "sync_instruments", _sync)
    calls = []
    monkeypatch.setattr(runtime_ctx.instruments, "invalidate", lambda: calls.append(1))
    assert run(routes.trigger_instrument_sync(db=None)) == {"status": "ok", "synced": 7}
    assert calls == [1]
