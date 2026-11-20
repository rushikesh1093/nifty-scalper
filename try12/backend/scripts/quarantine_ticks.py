"""
Move whole days of bad ticks out of the ``ticks`` table (audit D8).

    python scripts/quarantine_ticks.py --day 2026-10-05 --day 2026-10-06 --reason "..."
    python scripts/quarantine_ticks.py --day 2026-10-05 ... --reason "..." --apply

DRY RUN BY DEFAULT: prints each day's tick count and IST time range. With
``--apply`` each day is written to a verified
``<TICK_BACKUP_DIR>/quarantine/ticks_<day>.csv.gz`` and only then deleted; the
reason is appended to ``QUARANTINE.txt`` beside the files. Uses the backend's
.env database (run from try12/backend). Nothing else is touched.

5-8 Oct 2026: every tick was stamped 5h30m early (UTC wall time recorded as
IST); shifted +5:30 they match AngelOne's candles within 0-0.7 pts. The ohlc
rows for those days are correct and are kept.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.base import AsyncSessionLocal  # noqa: E402
from app.db.models.tick import Tick  # noqa: E402
from app.services.market_data import tick_retention as tr  # noqa: E402


async def _survey(days):
    async with AsyncSessionLocal() as db:
        for d in days:
            lo, hi = tr._day_bounds(d)
            n, a, b = (await db.execute(select(func.count(), func.min(Tick.timestamp),
                                               func.max(Tick.timestamp))
                                        .where(Tick.timestamp >= lo, Tick.timestamp < hi))).one()
            rng = (f"{a.astimezone(tr.IST):%H:%M}-{b.astimezone(tr.IST):%H:%M} IST" if n else "-")
            print(f"  {d}  {n:>10,} ticks  {rng}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--day", action="append", required=True, type=date.fromisoformat)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    days = sorted(set(a.day))
    out = Path(settings.TICK_BACKUP_DIR) / "quarantine"
    print(f"database {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")
    asyncio.run(_survey(days))
    if not a.apply:
        print("dry run: nothing changed (add --apply)")
        return 0
    done = asyncio.run(tr.quarantine_days(AsyncSessionLocal, days, out, a.reason))
    for d, n, p in done:
        print(f"  quarantined {d}: {n:,} rows -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
