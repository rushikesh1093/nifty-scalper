"""
Live-path hardening for the three strategies: per-minute candle volume from the
feed's cumulative volume, the ORB futures-volume rule, bar exits that cannot be
skipped by a runner cycle that misses a minute, regime ADX warm-up on history,
and per-day strategy state rebuilt from persisted positions after a restart.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.trading import exit_plan as xp
from app.services.trading.exit_plan import ExitPlan
from app.strategies.base import Direction
from app.strategies.indicators import compute_all
from app.strategies.strategies import (
    ORBBreakoutStrategy, SupertrendEMAStrategy, VWAPEMAPullbackStrategy,
)
from tests.helpers import run
from tests.test_three_strategies import (
    OPEN, OR_BARS, at, evaluate, expand, orb_candles, pullback_bars, st_bars,
)


# ── per-minute volume from the cumulative feed volume ────────────────────────

def _tick(token, ltp, vol, at_):
    from app.services.market_data.feed import TickData
    t = TickData(token=token, exchange="NFO", ltp=ltp)
    t.volume, t.received_at = vol, at_
    return t


def test_candle_volume_is_per_minute_not_the_day_total():
    from app.services.market_data.tick_writer import CandleBuilder
    b = CandleBuilder()
    t0 = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
    out = []
    for sec, vol in ((0, 1000), (30, 1600), (60, 1700), (90, 2500), (120, 2600)):
        done = b.update(_tick("F1", 100.0, vol, t0 + timedelta(seconds=sec)))
        if done:
            out.append(done)
    assert [c["volume"] for c in out] == [600, 900]       # 1000→1600, then 1600→2500
    assert not any(k.startswith("_") for k in out[0])     # no builder internals leak


def test_candle_volume_restarts_cleanly_when_the_cumulative_resets():
    from app.services.market_data.tick_writer import CandleBuilder
    b = CandleBuilder()
    t0 = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
    b.update(_tick("F1", 100.0, 9000, t0))
    first = b.update(_tick("F1", 100.0, 50, t0 + timedelta(minutes=1)))   # new session
    second = b.update(_tick("F1", 100.0, 80, t0 + timedelta(minutes=2)))
    assert first["volume"] == 0 and second["volume"] == 50


# ── ORB futures-volume rule ──────────────────────────────────────────────────

BREAK = [(22045, 22058, 22030, 22050), (22050, 22080, 22048, 22075)]


def _futures(vols_per_5m):
    """Futures 1-min candles whose 5-min bars carry *vols_per_5m* (per bar)."""
    out, t = [], OPEN
    for v in vols_per_5m:
        for k in range(5):
            out.append(dict(timestamp=t.isoformat(), open=1, high=1, low=1, close=1,
                            volume=v / 5))
            t += timedelta(minutes=1)
    return out


@pytest.mark.parametrize("last_vol,fires", [(2000, True), (1100, False)])
def test_orb_needs_futures_volume_above_1_2x_the_last_five_bars(last_vol, fires):
    c = orb_candles(*BREAK)
    with at(25):
        s = ORBBreakoutStrategy()
        s.futures_candles = _futures([1000, 1000, 1000, 1000, last_vol])
        assert (evaluate(s, c) is not None) is fires


def test_orb_without_futures_volume_still_trades_and_says_so(caplog):
    with at(25):
        s = ORBBreakoutStrategy()
        assert evaluate(s, orb_candles(*BREAK)) is not None
    assert "volume rule not checked" in caplog.text


# ── bar exits walk every completed bar since the last check ──────────────────

def test_pullback_vwap_exit_on_a_skipped_bar_is_not_missed():
    strat = VWAPEMAPullbackStrategy()
    bars = pullback_bars()
    trigger_start = OPEN + timedelta(minutes=3 * (len(bars) - 1))
    plan = ExitPlan(strategy=strat.name, side="BUY", spot_entry=22042.0, spot_stop=22031.0,
                    spot_t1=22058.5, last_bar_start=trigger_start.isoformat())
    # bar 1 closes far below VWAP; bar 2 closes back above it. A runner cycle
    # that only looked at the newest bar would miss the hard exit.
    later = bars + [(22042.0, 22043.0, 21950.0, 21960.0), (21960.0, 22100.0, 21959.0, 22090.0)]
    c = expand(later, 3)
    with at(3 * len(later)):
        assert strat.candle_exit(plan, c, compute_all(c)) == xp.VWAP_EXIT


def test_bar_exits_never_re_read_the_trigger_bar_or_a_seen_bar():
    strat = VWAPEMAPullbackStrategy()
    bars = pullback_bars()
    c = expand(bars, 3)
    with at(3 * len(bars)):
        sig = evaluate(strat, c)
        plan = ExitPlan.from_dict(sig.exit_plan)
        assert plan.last_bar_start == (OPEN + timedelta(minutes=3 * (len(bars) - 1))).isoformat()
        ind = dict(compute_all(c), vwap=30000.0)            # everything "below VWAP"
        assert strat.candle_exit(plan, c, ind) is None      # only the trigger bar exists


def test_supertrend_flip_on_a_skipped_minute_is_not_missed():
    strat = SupertrendEMAStrategy()
    up = st_bars(6)
    p = up[-1][3]
    down = [(p, p + 1, p - 60, p - 59)]                      # one bar flips the trend
    back = [(p - 59, p + 80, p - 60, p + 70)]                # the next bar flips it back
    flip_start = OPEN + timedelta(minutes=len(up))
    plan = ExitPlan(strategy=strat.name, side="BUY", spot_entry=p, spot_stop=p - 200,
                    premium_target_pts=20.0, time_stop_minutes=10,
                    last_bar_start=(flip_start - timedelta(minutes=1)).isoformat())
    c = expand(up + down + back, 1)
    with at(len(up) + 2):
        assert strat.candle_exit(plan, c, {}) == xp.ST_FLIP


# ── regime ADX warm-up on history ────────────────────────────────────────────

def test_regime_adx_is_available_at_the_open_with_history():
    from app.services.trading.regime_config import RegimeConfig
    from app.services.trading.regime_indicators import build_regime_indicators
    hist = expand(st_bars(0), 1, OPEN - timedelta(days=1))
    today = expand(OR_BARS + [(22045, 22058, 22030, 22050)], 5)[:20]
    with at(20):
        ind = compute_all(today)
        cold = build_regime_indicators(today, ind, RegimeConfig())
        warm = build_regime_indicators(today, ind, RegimeConfig(), history=hist + today)
    assert cold.adx is None and warm.adx is not None
    assert cold.atr_percentile == 50.0                       # the neutral fallback


# ── per-day state rebuilt from persisted positions ───────────────────────────

def _pos(kind, exit_reason=None, open_=False, exit_at=None):
    return SimpleNamespace(option_type=kind, exit_reason=exit_reason, is_open=open_,
                           exit_time=exit_at)


def test_orb_restore_keeps_one_per_direction_and_the_reverse_rule():
    with at(60):
        s = ORBBreakoutStrategy()
        s.restore_day([_pos("CE", xp.SPOT_SL)])
        assert s._allowed(Direction.SELL) and not s._allowed(Direction.BUY)
        assert s._roll_day() is False                        # restore does not get wiped
        s.restore_day([_pos("CE", "TARGET")])
        assert not s._allowed(Direction.SELL)
        s.restore_day([_pos("PE", None, open_=True)])
        assert s.open_positions == 1


def test_supertrend_restore_keeps_the_daily_cap_and_cooldown():
    with at(100) as rc:
        s = SupertrendEMAStrategy()
        last = rc.clock.now_utc() - timedelta(minutes=1)
        s.restore_day([_pos("CE", "TIME_EXIT", exit_at=last)] * 6)
        assert s._trades_today == 6 and s._last_exit_at == last
        assert run(s.evaluate(expand(st_bars(3), 1), {})) is None


def test_runner_restores_today_only_from_the_db(runtime_ctx, sqlite_db, monkeypatch):
    from app.services.trading.strategy_runner import strategy_runner
    from tests.helpers import add_position
    now = runtime_ctx.clock.now_utc()
    run(add_position(sqlite_db, strategy_name="ORB_BREAKOUT", option_type="CE",
                     is_open=False, exit_reason=xp.SPOT_SL, entry_time=now - timedelta(hours=1),
                     exit_time=now - timedelta(minutes=30)))
    run(add_position(sqlite_db, strategy_name="ORB_BREAKOUT", option_type="PE",
                     is_open=False, exit_reason="TARGET", entry_time=now - timedelta(days=1)))
    orb = ORBBreakoutStrategy()
    monkeypatch.setattr(strategy_runner, "_strategies", {"NIFTY": [orb]})
    run(strategy_runner._restore_strategy_state())
    assert orb._trades == [[Direction.BUY, xp.SPOT_SL]]       # yesterday's trade ignored


# ── futures plumbing: lookup at startup, candles handed to the ORB ───────────

def test_nearest_futures_picks_the_front_month(sqlite_db, monkeypatch):
    from datetime import date
    import app.services.market_data.market_manager as mm
    from app.db.models.instrument import Instrument, InstrumentType
    monkeypatch.setattr(mm, "AsyncSessionLocal", sqlite_db)
    pytest.importorskip("aiosqlite")
    from app.db.base import Base
    from sqlalchemy import create_engine

    async def _seed():
        async with sqlite_db() as db:
            conn = await db.connection()
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Instrument.__table__]))
            today = date.today()
            for tok, exp in (("F2", today + timedelta(days=40)), ("F1", today + timedelta(days=9)),
                             ("F0", today - timedelta(days=5))):
                db.add(Instrument(token=tok, symbol=f"NIFTY{tok}FUT", name="NIFTY", expiry=exp,
                                  instrument_type=InstrumentType.FUTIDX, lot_size=65,
                                  tick_size=0.05, exchange="NFO", is_active=True))
            await db.commit()
    run(_seed())
    out = run(mm.MarketManager()._nearest_futures())
    assert out == [("F1", "NIFTYF1FUT", "NIFTY")]           # expired F0 and back-month F2 skipped


def test_runner_hands_futures_candles_to_the_orb(runtime_ctx, fake_redis, monkeypatch):
    from app.services.trading.strategy_runner import strategy_runner

    async def _candles(token, interval, count=100):
        return [{"token": token, "volume": 5}] if token == "F1" else []

    run(fake_redis.set("fut_token:NIFTY", "F1"))
    monkeypatch.setattr(fake_redis, "get_candles", _candles)        # runtime_ctx's market
    assert run(strategy_runner._futures_candles("NIFTY")) == [{"token": "F1", "volume": 5}]
    assert run(strategy_runner._futures_candles("BANKNIFTY")) == []      # none published


# ── index history: AngelOne wants the 999-prefixed token ─────────────────────

def test_spot_seed_asks_angelone_for_the_999_index_token(monkeypatch):
    import app.services.market_data.market_manager as mm
    asked, stored = [], {}

    async def _candles(exchange, symbol_token, interval, from_date, to_date):
        asked.append((exchange, symbol_token))
        return [["2026-10-21T09:15:00+05:30", 1.0, 2.0, 0.5, 1.5, 0]]

    async def _set(token, interval, candles):
        stored[token] = candles

    monkeypatch.setattr(mm.angel_client, "get_candle_data", _candles)
    monkeypatch.setattr(mm.redis_cache, "set_candles", _set)
    m = mm.MarketManager()
    run(m._seed_one_token_exchange("26000", "NIFTY", "NSE"))
    run(m._seed_one_token_exchange("48704", "NIFTY27OCT26FUT", "NFO"))
    assert asked == [("NSE", "99926000"), ("NFO", "48704")]
    assert set(stored) == {"26000", "48704"}                 # stored under the feed token
    assert stored["26000"][0]["token"] == "26000"


# ── watchlist: the two nearest expiries ──────────────────────────────────────

def test_watchlist_subscribes_the_two_nearest_expiries(monkeypatch):
    import app.services.market_data.market_manager as mm
    from datetime import date
    exps = [date(2026, 10, 27), date(2026, 11, 3), date(2026, 11, 10)]
    asked = []

    async def _expiries(db, underlying, as_of=None):
        return exps

    async def _tokens(db, underlying, expiry, strike_range):
        asked.append((underlying, expiry))
        return [type("I", (), {"token": f"{underlying}{expiry}", "symbol": "S"})()]

    async def _ltp(self, underlying):
        return 22650.0

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mm, "get_active_expiries", _expiries)
    monkeypatch.setattr(mm, "get_option_tokens", _tokens)
    monkeypatch.setattr(mm, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(mm.MarketManager, "_get_spot_ltp", _ltp)
    out = run(mm.MarketManager()._build_watchlist())
    assert [e for u, e in asked if u == "NIFTY"] == exps[:2]        # not the third
    assert len(out) == 4                                             # 2 underlyings x 2 expiries


# ── AngelOne historical: rate-limit retry ────────────────────────────────────

class _Resp:
    def __init__(self, code, text="", data=None):
        self.status_code, self.text, self._data = code, text, data

    def json(self):
        return {"status": True, "data": self._data or []}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _client_with(monkeypatch, responses):
    import app.services.broker.angel_client as ac
    calls, sleeps = [], []

    class _Http:
        async def post(self, url, json=None, headers=None):
            calls.append(json)
            return responses.pop(0)

    async def _http(self):
        return _Http()

    async def _ok(self):
        return None

    async def _sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(ac.AngelClient, "_client", _http)
    monkeypatch.setattr(ac.AngelClient, "ensure_logged_in", _ok)
    monkeypatch.setattr(ac.asyncio, "sleep", _sleep)
    return ac, calls, sleeps


def test_historical_rate_limit_is_retried_with_backoff(monkeypatch):
    limited = "Access denied because of exceeding access rate"
    ac, calls, sleeps = _client_with(monkeypatch, [
        _Resp(403, limited), _Resp(403, limited), _Resp(200, data=[["t", 1, 2, 0.5, 1.5, 0]])])
    rows = run(ac.AngelClient().get_candle_data("NSE", "99926000", "ONE_MINUTE", "a", "b"))
    assert rows == [["t", 1, 2, 0.5, 1.5, 0]] and len(calls) == 3
    assert [s for s in sleeps if s >= 1] == [1.5, 3.0]               # backoff, not a hammer


def test_other_403s_are_not_retried(monkeypatch):
    ac, calls, _ = _client_with(monkeypatch, [_Resp(403, "Invalid token")])
    with pytest.raises(RuntimeError):
        run(ac.AngelClient().get_candle_data("NSE", "99926000", "ONE_MINUTE", "a", "b"))
    assert len(calls) == 1


def test_spot_and_futures_are_seeded_before_the_option_watchlist():
    import inspect
    import app.services.market_data.market_manager as mm
    src = inspect.getsource(mm.MarketManager.startup)
    assert src.index("_seed_one_token_exchange(s_token") < src.index("_seed_option_history(watchlist)")
    assert src.index("_nearest_futures()") < src.index("_seed_option_history(watchlist)")
    # and startup does not wait for the option history
    assert "asyncio.create_task(self._seed_option_history(watchlist))" in src
    assert "await self._seed_historical_ohlc(watchlist)" not in src
