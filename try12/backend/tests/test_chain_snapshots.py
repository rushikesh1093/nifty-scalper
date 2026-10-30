"""Option-chain snapshots persisted by the study collector (blockers-plan item 1).

No angel_client, network or live DB: the chain source is a fake async callable,
expiries are monkeypatched, Redis is FakeRedis and the DB is a tmp SQLite.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models.option_chain import OptionChainSnapshot
from app.services.cache.redis_client import redis_cache
from app.services.market_data import chain_snapshots as cs
from app.services.market_data import study_collector as sc_mod
from app.services.market_data.study_collector import StudyCollector
from app.services.replay.data_loader import load_day
from tests.conftest import _patch_session_factory
from tests.helpers import run
from tests.replay_fixtures import make_db

UTC = timezone.utc
EXPIRY = date(2026, 9, 8)
DAY = date(2026, 9, 1)
IN_SESSION = datetime(2026, 9, 1, 4, 32, 10, tzinfo=UTC)          # 10:02:10 IST
BUCKET = datetime(2026, 9, 1, 4, 30, tzinfo=UTC)
LOGGER = "app.services.market_data.study_collector"


def _raw_strikes(underlying, n=3):
    base = 22600 if underlying == "NIFTY" else 48000
    step = 50 if underlying == "NIFTY" else 100
    out = []
    for i in range(n):
        k = base + i * step
        for kind, off in (("CE", 0), ("PE", 1)):
            out.append({"token": f"{underlying[:1]}{k}{off}", "strikePrice": k, "optionType": kind,
                        "lastPrice": 100.0 + i, "bidprice": 99.5 + i, "askprice": 100.5 + i,
                        "openInterest": 1000 * (i + 1), "tradedVolume": 50, "impliedVolatility": 0.25})
    return out


class FakeSource:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    async def __call__(self, name, expiry_str, strike):
        self.calls.append((name, expiry_str, strike))
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return self.result
        return _raw_strikes(name)


@pytest.fixture
def chain_env(runtime_ctx, fake_redis, monkeypatch):
    """In-session FrozenClock, spot LTPs in FakeRedis, one active expiry, a
    recording set_option_chain. DB = runtime_ctx's sqlite_db."""
    import app.services.broker.instrument_sync as isync

    async def _expiries(db, name, *, as_of):
        return [EXPIRY]

    monkeypatch.setattr(isync, "get_active_expiries", _expiries)
    runtime_ctx.clock.set(IN_SESSION)
    fake_redis.kv[f"ltp:{settings.NIFTY_TOKEN}"] = 22610.0
    fake_redis.kv[f"ltp:{settings.BANKNIFTY_TOKEN}"] = 48020.0
    written = []

    async def _set_chain(underlying, expiry, chain):
        written.append((underlying, expiry, chain))

    monkeypatch.setattr(redis_cache, "set_option_chain", _set_chain)
    return runtime_ctx, written


async def _count(Session):
    async with Session() as db:
        return (await db.execute(select(func.count(OptionChainSnapshot.id)))).scalar_one()


# (1) round trip through the replay loader ─────────────────────────────────────

def test_collector_rows_round_trip_through_load_day(chain_env, tmp_path, monkeypatch):
    rc, written = chain_env
    Session, engine, _ = make_db(tmp_path / "full.db")          # full schema for load_day
    _patch_session_factory(monkeypatch, Session)
    try:
        out = run(StudyCollector(chain_source=FakeSource())._fetch_option_chain("NIFTY"))
        assert (out.status, out.strikes, out.rows_written) == ("OK", 6, 6)
        assert written and written[0][0] == "NIFTY"

        async def _load():
            async with Session() as db:
                return await load_day(db, DAY, ["NIFTY"])
        data = run(_load())
        sets = data.chains[("NIFTY", EXPIRY.isoformat())]
        assert len(sets) == 1
        ts, strikes = sets[0]
        assert ts.replace(tzinfo=UTC) == BUCKET if ts.tzinfo is None else ts == BUCKET
        assert len(strikes) == 6
        assert {s["option_type"] for s in strikes} == {"CE", "PE"}
        assert data.snapshots["N226000"][0]["ltp"] == 100.0
        assert data.snapshots["N226000"][0]["iv"] == 25.0
    finally:
        run(engine.dispose())


# (2) idempotent per 300 s bucket ──────────────────────────────────────────────

def test_same_bucket_writes_once_next_bucket_adds_a_set(chain_env):
    rc, _ = chain_env
    strikes = cs.normalise_chain(_raw_strikes("NIFTY"))

    async def _persist(at):
        return await cs.persist_chain_snapshot(
            rc.sessions, underlying="NIFTY", expiry=EXPIRY, spot=22610.0,
            strikes=strikes, snapshot_time=cs.snapshot_bucket(at))

    assert run(_persist(IN_SESSION)) == 6
    assert run(_persist(IN_SESSION + timedelta(seconds=100))) == 0     # same bucket
    assert run(_count(rc.sessions)) == 6
    assert run(_persist(IN_SESSION + timedelta(seconds=300))) == 6     # next bucket
    assert run(_count(rc.sessions)) == 12

    async def _times():
        async with rc.sessions() as db:
            return sorted({t for t in (await db.execute(
                select(OptionChainSnapshot.snapshot_time))).scalars().all()})
    t = run(_times())
    assert len(t) == 2 and (t[1] - t[0]).total_seconds() == 300


def test_snapshot_bucket_floors_to_300s_utc():
    assert cs.snapshot_bucket(IN_SESSION) == BUCKET
    ist = IN_SESSION.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert cs.snapshot_bucket(ist) == BUCKET and cs.snapshot_bucket(ist).utcoffset() == timedelta(0)


# (3) empty body: fail-soft, one WARNING per interval ──────────────────────────

def test_empty_body_never_raises_and_warns_once_per_interval(chain_env, caplog):
    rc, written = chain_env
    src = FakeSource(exc=json.JSONDecodeError("Expecting value", "", 0))
    col = StudyCollector(chain_source=src)
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    for expected_streak in (1, 2):
        caplog.clear()
        run(col._refresh_all_option_chains())
        warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "EMPTY_BODY" in msg and "NIFTY=EMPTY_BODY" in msg and "BANKNIFTY=EMPTY_BODY" in msg
        assert f"consecutive failing intervals: {expected_streak}" in msg
        assert col._chain_fail_streak == expected_streak
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert run(_count(rc.sessions)) == 0
    assert written == []

    caplog.clear()
    src.exc = None
    run(col._refresh_all_option_chains())
    infos = [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    assert any("recovered" in m and "2 failing intervals" in m for m in infos)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert col._chain_fail_streak == 0
    assert run(_count(rc.sessions)) == 12                  # 6 NIFTY + 6 BANKNIFTY


def test_classify_fetch_error():
    import httpx
    req = httpx.Request("GET", "https://example.invalid/x")
    assert cs.classify_fetch_error(json.JSONDecodeError("Expecting value", "", 0)) == "EMPTY_BODY"
    assert cs.classify_fetch_error(ValueError("x")) == "EMPTY_BODY"
    assert cs.classify_fetch_error(httpx.HTTPStatusError(
        "boom", request=req, response=httpx.Response(500, request=req))) == "HTTP_ERROR"
    assert cs.classify_fetch_error(httpx.ConnectError("down", request=req)) == "NETWORK_ERROR"
    assert cs.classify_fetch_error(OSError("x")) == "NETWORK_ERROR"
    assert cs.classify_fetch_error(RuntimeError("x")) == "ERROR"


# (4) unparseable payloads ─────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [{}, {"data": "x"}])
def test_unparseable_payload_writes_nothing(chain_env, payload):
    rc, written = chain_env
    out = run(StudyCollector(chain_source=FakeSource(result=payload))._fetch_option_chain("NIFTY"))
    assert out.status == "UNPARSEABLE" and out.rows_written == 0
    assert run(_count(rc.sessions)) == 0
    assert written == []


# (5) DB failure: Redis still written, no raise ───────────────────────────────

class _RaisingSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        raise RuntimeError("db down")

    async def commit(self):
        raise RuntimeError("db down")

    async def rollback(self):
        pass

    def add_all(self, rows):
        pass


def test_db_error_is_reported_and_redis_still_written(chain_env, monkeypatch):
    rc, written = chain_env
    monkeypatch.setattr(sc_mod, "AsyncSessionLocal", lambda: _RaisingSession())
    out = run(StudyCollector(chain_source=FakeSource())._fetch_option_chain("NIFTY"))
    assert out.status == "DB_ERROR" and out.rows_written == 0
    assert "db down" in out.detail
    assert len(written) == 1 and len(written[0][2]["strikes"]) == 6


# (6) out of session: Redis only ───────────────────────────────────────────────

def test_out_of_session_writes_redis_only(chain_env):
    rc, written = chain_env
    rc.clock.set(datetime(2026, 9, 1, 11, 0, tzinfo=UTC))           # 16:30 IST
    out = run(StudyCollector(chain_source=FakeSource())._fetch_option_chain("NIFTY"))
    assert out.status == "OUT_OF_SESSION" and out.rows_written == 0
    assert len(written) == 1
    assert run(_count(rc.sessions)) == 0


# (7) normalise_chain golden outputs (identical to the old inline code) ───────

_LIST_IN = [
    {"token": "101", "strikePrice": "22600", "optionType": "CE", "lastPrice": "120.5",
     "bidprice": 120, "askprice": 121, "openInterest": 5000, "tradedVolume": 300,
     "impliedVolatility": 0.25},
    {"symboltoken": 102, "strike_price": 22600, "option_type": "pe", "ltp": 80, "bid": 79.5,
     "ask": 80.5, "oi": 4000, "volume": 200, "iv": 17.2},
    {"strike": 22650},          # no token -> skipped
    "junk",                     # not a dict -> skipped
]
_LIST_OUT = [
    {"token": "101", "strike": 22600.0, "option_type": "CE", "ltp": 120.5, "bid": 120.0,
     "ask": 121.0, "oi": 5000, "volume": 300, "iv": 25.0},
    {"token": "102", "strike": 22600.0, "option_type": "PE", "ltp": 80.0, "bid": 79.5,
     "ask": 80.5, "oi": 4000, "volume": 200, "iv": 17.2},
]


def test_normalise_chain_list():
    assert cs.normalise_chain([dict(s) if isinstance(s, dict) else s for s in _LIST_IN]) == _LIST_OUT


def test_normalise_chain_data_wrapper():
    assert cs.normalise_chain({"data": [dict(s) if isinstance(s, dict) else s
                                        for s in _LIST_IN]}) == _LIST_OUT


def test_normalise_chain_ce_pe_dicts():
    raw = {"CE": {"22600": {"token": "201", "strikePrice": 22600, "ltp": 10}},
           "PE": {"22600": {"token": "202", "strikePrice": 22600, "ltp": 12}},
           "meta": {"x": 1}}
    assert cs.normalise_chain(raw) == [
        {"token": "201", "strike": 22600.0, "option_type": "CE", "ltp": 10.0, "bid": 0.0,
         "ask": 0.0, "oi": 0, "volume": 0, "iv": 0.0},
        {"token": "202", "strike": 22600.0, "option_type": "PE", "ltp": 12.0, "bid": 0.0,
         "ask": 0.0, "oi": 0, "volume": 0, "iv": 0.0},
    ]


def test_normalise_chain_unparseable():
    assert cs.normalise_chain({}) == []
    assert cs.normalise_chain({"data": "x"}) == []
    assert cs.normalise_chain(None) == []


# (6) the chain built from the live feed's ticks (the default source) ─────────

def test_implied_vol_recovers_the_volatility_of_a_black_scholes_price():
    for call in (True, False):
        for vol in (0.10, 0.18, 0.35):
            price = cs._bs_price(22600.0, 22650.0, 5 / 365, vol, call)
            assert cs.implied_vol(price, 22600.0, 22650.0, 5 / 365, call) == pytest.approx(vol * 100, abs=0.05)


def test_implied_vol_none_without_time_value_or_inputs():
    assert cs.implied_vol(100.0, 22700.0, 22600.0, 5 / 365, True) is None     # = intrinsic
    assert cs.implied_vol(0.0, 22600.0, 22600.0, 5 / 365, True) is None
    assert cs.implied_vol(50.0, 22600.0, 22600.0, 0.0, True) is None


def _opt_rows(strikes, expiry=EXPIRY):
    from tests.helpers import option_row
    return [option_row(k, expiry, kind=kind, token=f"T{k}{kind}") for k in strikes for kind in ("CE", "PE")]


def test_chain_from_ticks_keeps_fresh_ticks_and_implies_iv():
    now = IN_SESSION
    close = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    fresh = (now - timedelta(seconds=5)).replace(tzinfo=None).isoformat()
    stale = (now - timedelta(seconds=600)).replace(tzinfo=None).isoformat()
    ticks = {"T22600CE": {"ltp": 120.0, "best_bid": 119.5, "best_ask": 120.5, "oi": 900, "volume": 40, "ts": fresh},
             "T22600PE": {"ltp": 110.0, "best_bid": 109.5, "best_ask": 110.5, "ts": stale}}
    out = cs.chain_from_ticks(_opt_rows([22600]), ticks, 22610.0, close, now)
    assert [(s["token"], s["option_type"], s["oi"]) for s in out] == [("T22600CE", "CE", 900)]
    assert 5 < out[0]["iv"] < 60


def test_collector_builds_the_chain_from_ticks_by_default(chain_env, sqlite_db, monkeypatch, fake_redis):
    rc, written = chain_env
    import app.services.broker.instrument_sync as isync
    rows = _opt_rows(range(22100, 23150, 50))

    async def _tokens(db, name, expiry, strike_range=None):
        lo, hi = strike_range
        return [r for r in rows if name == "NIFTY" and lo <= r.strike <= hi]

    monkeypatch.setattr(isync, "get_option_tokens", _tokens)
    for r in rows:
        mid = max(cs._bs_price(22610.0, r.strike, 7 / 365, 0.15, r.option_type.value == "CE"), 1.0)
        fake_redis.put_tick(r.token, ltp=mid, bid=mid - 0.25, ask=mid + 0.25, oi=5000)
    out = run(StudyCollector()._fetch_option_chain("NIFTY"))
    assert out.status == "OK" and out.strikes == 42 and out.rows_written == 42
    chain = written[0][2]
    atm = [s for s in chain["strikes"] if s["strike"] == 22600 and s["option_type"] == "CE"][0]
    assert atm["iv"] == pytest.approx(15.0, abs=1.5)        # calendar time to 15:30 on expiry vs 7/365
    assert run(_count(sqlite_db)) == 42

    bn = run(StudyCollector()._fetch_option_chain("BANKNIFTY"))
    assert bn.status == cs.NO_TICKS


def test_the_cached_chain_outlives_the_rebuild_interval():
    """The chain is rebuilt every OPTION_CHAIN_INTERVAL; a shorter Redis TTL left the
    option selector without IV for most of every interval."""
    from app.services.cache import redis_client
    assert redis_client.TTL_OPTION_CHAIN > 2 * sc_mod.OPTION_CHAIN_INTERVAL
