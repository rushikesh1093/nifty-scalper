"""
Tick retention: back up, then delete, ticks older than TICK_RETENTION_DAYS.

Runs once a day after the close (TICK_CLEANUP_TIME_IST) and once at startup when
that time has already passed today. For every IST day older than the cutoff:

  1. stream the day's rows, in id order, to ``<TICK_BACKUP_DIR>/ticks_<day>.csv.gz``
     (written to a temp file, then renamed);
  2. re-read the backup and check its row count equals the day's count in the DB;
  3. only then delete exactly those rows (id <= the last id backed up), in batches.

A day whose backup cannot be written or verified is NOT deleted. Live trading
never reads the ticks table (it uses Redis); only backtests and diagnostics do.

Postgres reuses the space of deleted rows for new ticks (autovacuum) but does not
hand it back to the OS. Each run also logs a WARNING when free disk space is
below LOW_DISK_WARN_GB, since a full disk stops the database.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.db.models.tick import Tick
from app.services.trading.session_times import trading_day_bounds_utc

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
BATCH = 50_000
COLUMNS = [c.name for c in Tick.__table__.columns]


@dataclass
class CleanupResult:
    cutoff: Optional[date] = None
    days_backed_up: list = field(default_factory=list)      # [(day, rows, path)]
    days_failed: list = field(default_factory=list)         # [(day, reason)]
    rows_deleted: int = 0
    free_gb: Optional[float] = None


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Whole IST calendar day (00:00-24:00), as aware UTC — wider than the session."""
    start = datetime.combine(day, dtime(0, 0), IST).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _plain(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


async def _backup_day(db, day: date, out_dir: Path) -> tuple[int, int, Path]:
    """Write the day's ticks to a verified .csv.gz. Returns (rows, max_id, path)."""
    lo, hi = _day_bounds(day)
    in_day = (Tick.timestamp >= lo, Tick.timestamp < hi)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"ticks_{day.isoformat()}.csv.gz"
    tmp = final.with_suffix(".gz.tmp")
    rows, last_id = 0, 0
    with gzip.open(tmp, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        while True:
            chunk = (await db.execute(select(Tick).where(*in_day, Tick.id > last_id)
                                      .order_by(Tick.id).limit(BATCH))).scalars().all()
            if not chunk:
                break
            for t in chunk:
                w.writerow([_plain(v) if isinstance(v, datetime) else v
                            for v in (getattr(t, c) for c in COLUMNS)])
            rows += len(chunk)
            last_id = chunk[-1].id
            db.expunge_all()
    # verify: what is in the file is what is in the DB for that day
    with gzip.open(tmp, "rt", newline="", encoding="utf-8") as fh:
        in_file = sum(1 for _ in csv.reader(fh)) - 1
    in_db = (await db.execute(select(func.count()).select_from(Tick)
                              .where(*in_day, Tick.id <= last_id))).scalar_one()
    if in_file != rows or in_db != rows:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup check failed: wrote {rows}, file {in_file}, db {in_db}")
    os.replace(tmp, final)
    return rows, last_id, final


async def _delete_day(db, day: date, max_id: int) -> int:
    lo, hi = _day_bounds(day)
    deleted = 0
    while True:
        ids = select(Tick.id).where(Tick.timestamp >= lo, Tick.timestamp < hi,
                                    Tick.id <= max_id).limit(BATCH)
        res = await db.execute(delete(Tick).where(Tick.id.in_(ids)))
        await db.commit()
        n = res.rowcount or 0
        deleted += n
        if n == 0:
            return deleted


def free_disk_gb(path: Path) -> Optional[float]:
    try:
        return round(shutil.disk_usage(path).free / 1024 ** 3, 2)
    except Exception:
        return None


async def run_cleanup(session_factory: Callable, *, retention_days: int, backup_dir: Path,
                      today: date, low_disk_warn_gb: float = 2.0) -> CleanupResult:
    """One pass. Days with ticks older than *today* - *retention_days* are backed
    up then deleted, oldest first. Never raises for a single bad day."""
    res = CleanupResult(cutoff=today - timedelta(days=retention_days))
    cutoff_utc = _day_bounds(res.cutoff)[0]
    async with session_factory() as db:
        oldest = (await db.execute(select(func.min(Tick.timestamp))
                                   .where(Tick.timestamp < cutoff_utc))).scalar_one_or_none()
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            day = oldest.astimezone(IST).date()
            while day < res.cutoff:
                try:
                    rows, max_id, path = await _backup_day(db, day, backup_dir)
                    if rows:
                        res.rows_deleted += await _delete_day(db, day, max_id)
                        res.days_backed_up.append((day, rows, str(path)))
                        logger.info("Tick retention: %s backed up (%d rows -> %s) and deleted",
                                    day, rows, path.name)
                    else:
                        path.unlink(missing_ok=True)             # nothing that day
                except Exception as exc:
                    await db.rollback()
                    res.days_failed.append((day, f"{type(exc).__name__}: {exc}"))
                    logger.error("Tick retention: %s NOT deleted (backup failed): %s", day, exc)
                # jump to the next day that HAS old ticks: probing every empty day
                # costs a scan of the whole table each time (18M rows: ~15 s a day)
                nxt = (await db.execute(select(func.min(Tick.timestamp)).where(
                    Tick.timestamp >= _day_bounds(day + timedelta(days=1))[0],
                    Tick.timestamp < cutoff_utc))).scalar_one_or_none()
                if nxt is None:
                    break
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=timezone.utc)
                day = nxt.astimezone(IST).date()
    res.free_gb = free_disk_gb(backup_dir if backup_dir.exists() else Path("."))
    if res.free_gb is not None and res.free_gb < low_disk_warn_gb:
        logger.warning("LOW DISK: %.2f GB free (warning below %.1f GB). A full disk stops the "
                       "database; free space or shorten TICK_RETENTION_DAYS.",
                       res.free_gb, low_disk_warn_gb)
        from app.core.alerts import alert
        alert("low_disk", f"Low disk: {res.free_gb:.1f} GB free (warning below "
                          f"{low_disk_warn_gb:.0f} GB)", throttle_s=3600)
    return res


async def quarantine_days(session_factory: Callable, days, out_dir: Path,
                          reason: str) -> list[tuple[date, int, str]]:
    """Move whole IST days of bad ticks out of the table (audit D8): each day is
    written to a verified ``<out_dir>/ticks_<day>.csv.gz`` exactly like a
    retention backup, then deleted, and *reason* is appended to
    ``<out_dir>/QUARANTINE.txt``. Reversible: the file holds every column.
    A day whose backup fails raises and is not deleted."""
    done = []
    out_dir.mkdir(parents=True, exist_ok=True)
    async with session_factory() as db:
        for day in sorted(set(days)):
            rows, max_id, path = await _backup_day(db, day, out_dir)
            if not rows:
                path.unlink(missing_ok=True)
                continue
            deleted = await _delete_day(db, day, max_id)
            with open(out_dir / "QUARANTINE.txt", "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(IST).isoformat(timespec='seconds')}  {day}  "
                         f"{rows} rows -> {path.name}  ({deleted} deleted)  {reason}\n")
            logger.warning("Tick quarantine: %s, %d rows -> %s (%s)", day, rows, path, reason)
            done.append((day, rows, str(path)))
    return done


class TickRetention:
    """Daily scheduler around run_cleanup (wall clock: maintenance, not trading)."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._last_run: Optional[date] = None

    def _due_at(self, day: date) -> datetime:
        hh, mm = (int(x) for x in settings.TICK_CLEANUP_TIME_IST.split(":")[:2])
        return datetime.combine(day, dtime(hh, mm), IST)

    async def run_now(self) -> CleanupResult:
        from app.db.base import AsyncSessionLocal
        today = datetime.now(IST).date()
        self._last_run = today
        return await run_cleanup(AsyncSessionLocal, retention_days=settings.TICK_RETENTION_DAYS,
                                 backup_dir=Path(settings.TICK_BACKUP_DIR), today=today,
                                 low_disk_warn_gb=settings.LOW_DISK_WARN_GB)

    async def _loop(self) -> None:
        while True:
            try:
                now = datetime.now(IST)
                due = self._due_at(now.date())
                if now >= due and self._last_run != now.date():
                    r = await self.run_now()
                    logger.info("Tick retention done: cutoff %s, %d day(s) removed (%d rows), "
                                "%d failed, %.2f GB free", r.cutoff, len(r.days_backed_up),
                                r.rows_deleted, len(r.days_failed), r.free_gb or -1)
                    due = self._due_at(now.date() + timedelta(days=1))
                elif now >= due:
                    due = self._due_at(now.date() + timedelta(days=1))
                await asyncio.sleep(max(60.0, (due - datetime.now(IST)).total_seconds()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Tick retention loop error: %s", exc, exc_info=True)
                await asyncio.sleep(3600)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


tick_retention = TickRetention()
