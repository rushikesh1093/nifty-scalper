"""
Tick retention: ticks older than the retention window are backed up to a
verified .csv.gz and only then deleted; recent ticks are untouched; a day whose
backup fails is kept; and a LOW DISK warning is logged below the threshold.
"""
import csv
import gzip
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.tick import Tick
from app.services.market_data import tick_retention as tr
from tests.helpers import run

TODAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def _ticks_table(sqlite_db):
    from app.db.base import Base

    async def _go():
        async with sqlite_db() as db:
            conn = await db.connection()
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Tick.__table__]))
            await db.commit()
    run(_go())


def _add_ticks(Session, day: date, n: int, token="T1"):
    """n ticks at 10:00 IST on *day* (04:30 UTC), one second apart."""
    base = datetime(day.year, day.month, day.day, 4, 30, tzinfo=timezone.utc)

    async def _go():
        async with Session() as db:
            for i in range(n):
                db.add(Tick(token=token, symbol="S", exchange="NFO", ltp=100.0 + i,
                            best_bid=99.0, best_ask=101.0, volume=i, oi=10,
                            timestamp=base + timedelta(seconds=i)))
            await db.commit()
    run(_go())


def _count(Session, day=None):
    async def _go():
        async with Session() as db:
            q = select(func.count()).select_from(Tick)
            if day is not None:
                lo, hi = tr._day_bounds(day)
                q = q.where(Tick.timestamp >= lo, Tick.timestamp < hi)
            return (await db.execute(q)).scalar_one()
    return run(_go())


def test_old_days_are_backed_up_then_deleted_and_recent_days_kept(sqlite_db, tmp_path):
    old1, old2, recent = TODAY - timedelta(days=40), TODAY - timedelta(days=31), TODAY - timedelta(days=5)
    _add_ticks(sqlite_db, old1, 7)
    _add_ticks(sqlite_db, old2, 5)
    _add_ticks(sqlite_db, recent, 4)
    res = run(tr.run_cleanup(sqlite_db, retention_days=30, backup_dir=tmp_path, today=TODAY))
    assert [(d, n) for d, n, _ in res.days_backed_up] == [(old1, 7), (old2, 5)]
    assert res.rows_deleted == 12 and not res.days_failed
    assert _count(sqlite_db) == 4 and _count(sqlite_db, recent) == 4
    # the backup holds every column of every deleted row
    with gzip.open(tmp_path / f"ticks_{old1.isoformat()}.csv.gz", "rt", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == tr.COLUMNS and len(rows) == 1 + 7
    assert [float(r[rows[0].index("ltp")]) for r in rows[1:]] == [100.0 + i for i in range(7)]


def test_the_cutoff_day_itself_is_kept(sqlite_db, tmp_path):
    edge = TODAY - timedelta(days=30)
    _add_ticks(sqlite_db, edge, 3)
    res = run(tr.run_cleanup(sqlite_db, retention_days=30, backup_dir=tmp_path, today=TODAY))
    assert res.rows_deleted == 0 and _count(sqlite_db) == 3


def test_a_failed_backup_deletes_nothing(sqlite_db, tmp_path, monkeypatch):
    old = TODAY - timedelta(days=45)
    _add_ticks(sqlite_db, old, 6)

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(tr.gzip, "open", _boom)
    res = run(tr.run_cleanup(sqlite_db, retention_days=30, backup_dir=tmp_path, today=TODAY))
    assert res.rows_deleted == 0 and res.days_failed and "disk full" in res.days_failed[0][1]
    assert _count(sqlite_db) == 6                                   # still there


def test_a_backup_that_does_not_match_the_db_deletes_nothing(sqlite_db, tmp_path, monkeypatch):
    old = TODAY - timedelta(days=45)
    _add_ticks(sqlite_db, old, 6)
    real_reader = tr.csv.reader

    def _short(fh):                                                  # a truncated file
        rows = list(real_reader(fh))
        return iter(rows[:-1])
    monkeypatch.setattr(tr.csv, "reader", _short)
    res = run(tr.run_cleanup(sqlite_db, retention_days=30, backup_dir=tmp_path, today=TODAY))
    assert res.rows_deleted == 0 and "backup check failed" in res.days_failed[0][1]
    assert _count(sqlite_db) == 6
    assert not list(tmp_path.glob("*.csv.gz"))                       # no half backup left


def test_running_again_is_a_no_op(sqlite_db, tmp_path):
    _add_ticks(sqlite_db, TODAY - timedelta(days=40), 3)
    run(tr.run_cleanup(sqlite_db, retention_days=30, backup_dir=tmp_path, today=TODAY))
    again = run(tr.run_cleanup(sqlite_db, retention_days=30, backup_dir=tmp_path, today=TODAY))
    assert again.rows_deleted == 0 and not again.days_backed_up


def test_low_disk_is_logged(sqlite_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tr, "free_disk_gb", lambda p: 1.4)
    run(tr.run_cleanup(sqlite_db, retention_days=30, backup_dir=tmp_path, today=TODAY,
                       low_disk_warn_gb=2.0))
    assert "LOW DISK" in caplog.text


def test_the_job_is_started_and_stopped_with_the_app():
    import inspect
    import app.main as main
    src = inspect.getsource(main.lifespan)
    assert "tick_retention.start()" in src and "tick_retention.stop()" in src
