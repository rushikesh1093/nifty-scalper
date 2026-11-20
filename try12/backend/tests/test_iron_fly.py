"""Expiry-day iron fly paper book (app.services.trading.iron_fly)."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.trading_params import TradingParams
from app.runtime.context import using
from app.services.trading import iron_fly as fly
from tests.helpers import option_row, with_instruments

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY = date(2026, 9, 1)                      # a Tuesday
SPOT = 24010.0                                 # ATM 24000, wings 23800 / 24200


def ist(h, m, day=EXPIRY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def rows(expiry=EXPIRY):
    out = []
    for k in range(23500, 24550, 50):
        for kind in ("CE", "PE"):
            out.append(option_row(k, expiry, kind=kind))
    return out


def put_quotes(fake, quotes):
    """quotes: {(strike, kind): (bid, ask)}"""
    for (k, kind), (bid, ask) in quotes.items():
        fake.put_tick(f"NIFTY-{kind}-{k}", ltp=(bid + ask) / 2, bid=bid, ask=ask)


ENTRY_QUOTES = {(24000, "CE"): (80.0, 80.5), (24000, "PE"): (70.0, 70.5),
                (24200, "CE"): (12.0, 12.5), (23800, "PE"): (10.0, 10.5)}


@pytest.fixture
def book(runtime_ctx, fake_redis, sqlite_db):
    """Runtime context with the instrument rows, a paper_spreads table and the clock."""
    from sqlalchemy import create_engine
    from app.db.base import Base
    from app.db.models.paper_spread import PaperSpread
    url = str(sqlite_db.kw["bind"].url).replace("+aiosqlite", "")
    eng = create_engine(url)
    Base.metadata.create_all(eng, tables=[PaperSpread.__table__])
    eng.dispose()
    rc = with_instruments(runtime_ctx, rows())
    with using(rc):
        yield SimpleNamespace(clock=runtime_ctx.clock, redis=fake_redis, rc=rc)


async def spreads():
    from sqlalchemy import select
    from app.db.models.paper_spread import PaperSpread
    from app.runtime.context import ctx
    async with ctx().sessions() as db:
        return (await db.execute(select(PaperSpread))).scalars().all()


async def open_at_0931(book):
    book.clock.set(ist(9, 31))
    book.redis.put_tick(settings.NIFTY_TOKEN, ltp=SPOT)
    put_quotes(book.redis, ENTRY_QUOTES)
    await fly.IronFlyPaper().cycle()


# ── pure arithmetic ──────────────────────────────────────────────────────────

def test_pick_legs_takes_atm_shorts_and_wings():
    legs = fly.pick_legs(rows(), EXPIRY, 24000.0)
    assert [(l["strike"], l["option_type"], l["side"]) for l in legs] == [
        (24000.0, "CE", "SELL"), (24000.0, "PE", "SELL"), (24200.0, "CE", "BUY"), (23800.0, "PE", "BUY")]


def test_pick_legs_none_when_a_wing_is_missing():
    assert fly.pick_legs([r for r in rows() if r.strike != 24200], EXPIRY, 24000.0) is None


def test_two_sided_rejects_stale_one_sided_and_crossed_quotes():
    now = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)
    fresh = (now - timedelta(seconds=5)).replace(tzinfo=None).isoformat()
    old = (now - timedelta(seconds=120)).replace(tzinfo=None).isoformat()
    assert fly.two_sided({"best_bid": 10, "best_ask": 10.5, "ts": fresh}, now) == (10.0, 10.5)
    assert fly.two_sided({"best_bid": 10, "best_ask": 10.5, "ts": old}, now) is None
    assert fly.two_sided({"best_bid": 0, "best_ask": 10.5, "ts": fresh}, now) is None
    assert fly.two_sided({"best_bid": 11, "best_ask": 10.5, "ts": fresh}, now) is None
    assert fly.two_sided(None, now) is None


def test_settle_arithmetic():
    legs = [
        {"side": "SELL", "entry_price": 80.0, "exit_price": 30.0},
        {"side": "SELL", "entry_price": 70.0, "exit_price": 60.0},
        {"side": "BUY", "entry_price": 12.5, "exit_price": 2.0},
        {"side": "BUY", "entry_price": 10.5, "exit_price": 15.0},
    ]
    p = TradingParams()
    res = fly.settle(legs, 65, p)
    # (80-30) + (70-60) + (2-12.5) + (15-10.5) = 54 pts x 65
    assert res["gross_pnl"] == pytest.approx(54 * 65)
    assert res["brokerage"] == pytest.approx(8 * fly.fills.brokerage(1, p))
    assert res["net_pnl"] == pytest.approx(res["gross_pnl"] - res["brokerage"] - res["statutory_charges"])
    assert res["statutory_charges"] > 0


def test_intrinsic():
    assert fly.intrinsic({"strike": 24000, "option_type": "CE"}, 24100) == 100
    assert fly.intrinsic({"strike": 24000, "option_type": "PE"}, 24100) == 0
    assert fly.intrinsic({"strike": 23800, "option_type": "PE"}, 23700) == 100


# ── the book ─────────────────────────────────────────────────────────────────

async def test_opens_once_on_expiry_day_at_the_quotes(book):
    await open_at_0931(book)
    await fly.IronFlyPaper().cycle()                       # a second cycle (or a restart) adds nothing
    (s,) = await spreads()
    assert s.status == fly.OPEN and s.trading_day == EXPIRY and s.lot_size == 65
    prices = {(l["strike"], l["option_type"]): l["entry_price"] for l in s.legs}
    # sold at the bid, bought at the ask
    assert prices == {(24000.0, "CE"): 80.0, (24000.0, "PE"): 70.0, (24200.0, "CE"): 12.5, (23800.0, "PE"): 10.5}
    assert s.credit_pts == pytest.approx(127.0)
    assert s.max_loss == pytest.approx((200 - 127.0) * 65)


async def test_no_entry_on_a_non_expiry_day(book):
    with using(with_instruments(book.rc, rows(expiry=date(2026, 9, 8)))):
        await open_at_0931(book)
        assert await spreads() == []


async def test_no_entry_outside_the_window(book):
    book.clock.set(ist(9, 50))
    book.redis.put_tick(settings.NIFTY_TOKEN, ltp=SPOT)
    put_quotes(book.redis, ENTRY_QUOTES)
    await fly.IronFlyPaper().cycle()
    assert await spreads() == []


async def test_no_entry_without_a_quote_on_every_leg(book):
    book.clock.set(ist(9, 31))
    book.redis.put_tick(settings.NIFTY_TOKEN, ltp=SPOT)
    put_quotes(book.redis, {k: v for k, v in ENTRY_QUOTES.items() if k != (23800, "PE")})
    await fly.IronFlyPaper().cycle()
    assert await spreads() == []


async def test_closes_at_1500_at_the_quotes(book):
    await open_at_0931(book)
    book.clock.set(ist(15, 0))
    book.redis.put_tick(settings.NIFTY_TOKEN, ltp=24020.0)
    put_quotes(book.redis, {(24000, "CE"): (24.0, 24.5), (24000, "PE"): (5.0, 5.5),
                            (24200, "CE"): (0.5, 0.6), (23800, "PE"): (0.4, 0.5)})
    await fly.IronFlyPaper().cycle()
    (s,) = await spreads()
    assert s.status == fly.CLOSED and s.exit_reason == fly.TIME_EXIT
    # bought back shorts at the ask, sold wings at the bid
    gross = ((80.0 - 24.5) + (70.0 - 5.5) + (0.5 - 12.5) + (0.4 - 10.5)) * 65
    assert s.gross_pnl == pytest.approx(gross)
    assert s.net_pnl == pytest.approx(s.gross_pnl - s.brokerage - s.statutory_charges)
    assert s.r_multiple == pytest.approx(s.net_pnl / s.max_loss, abs=1e-4)


async def test_waits_for_quotes_then_settles_at_intrinsic_after_the_close(book):
    await open_at_0931(book)
    for token in [f"NIFTY-{k}-{s}" for s, k in ENTRY_QUOTES]:
        book.redis.drop_price(token)
    book.clock.set(ist(15, 10))
    await fly.IronFlyPaper().cycle()
    (s,) = await spreads()
    assert s.status == fly.OPEN                             # no quotes yet, market still open

    book.clock.set(ist(15, 31))
    book.redis.put_tick(settings.NIFTY_TOKEN, ltp=24150.0, age_s=60)
    await fly.IronFlyPaper().cycle()
    (s,) = await spreads()
    assert s.status == fly.CLOSED and s.exit_reason == fly.EXPIRY_SETTLE
    exits = {(l["strike"], l["option_type"]): l["exit_price"] for l in s.legs}
    assert exits == {(24000.0, "CE"): 150.0, (24000.0, "PE"): 0.0, (24200.0, "CE"): 0.0, (23800.0, "PE"): 0.0}
    assert s.gross_pnl == pytest.approx((127.0 - 150.0) * 65)


async def test_a_spread_left_open_from_an_earlier_day_is_marked_unsettled(book):
    await open_at_0931(book)
    book.clock.set(ist(9, 20, day=date(2026, 9, 2)))
    await fly.IronFlyPaper().cycle()
    (s,) = await spreads()
    assert s.status == fly.UNSETTLED and s.net_pnl is None


async def test_snapshot_reports_the_open_spread_with_mark_to_market(book):
    await open_at_0931(book)
    snap = await fly.IronFlyPaper().snapshot()
    (s,) = snap["spreads"]
    assert s["status"] == fly.OPEN
    # closing at the entry quotes costs the spread: shorts at ask, wings at bid
    assert s["mtm_gross_pnl"] == pytest.approx(((80 - 80.5) + (70 - 70.5) + (12 - 12.5) + (10 - 10.5)) * 65)
    assert snap["summary"]["trades"] == 0
