"""
Copy a window of RECORDED live market data into a fresh SQLite replay store,
so the replay CLI can run on it without touching the live database.

    cd try12/backend
    python scripts/export_live_replay_window.py --env-file <live backend .env> \\
        --from 2026-10-16 --to 2026-10-29 --out <scratch>/live-window.db

Read-only on the source: the Postgres connection is opened with
``default_transaction_read_only=on`` and only SELECTs are issued. Credentials
are read from --env-file and never printed. The output file must not exist
(pass --replace to overwrite a previous export).

Copied (NIFTY only):
  ohlc                    spot (NIFTY_TOKEN from the env file) and NIFTY FUTIDX
                          1-min candles in the window (history lookback included)
  ticks                   option ticks, 09:00-15:31 IST, of the expiry the
                          production ``choose_expiry`` picks for that day; one row
                          per token per whole second (the last one). The replay
                          engine steps option ticks per whole second and reads the
                          last tick at or before the clock, so this is lossless for
                          the engine; other expiries cannot be bought that day.
  instruments             rows of the copied option tokens + NIFTY FUTIDX
  vix_data, market_daily_snapshots   all rows up to --to

Run the replay with NIFTY_TOKEN / VIX_TOKEN set to the live values (printed at
the end; they are instrument tokens, not secrets).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IST = timezone(timedelta(hours=5, minutes=30))


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def naive_utc(ts):
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def ist_bounds(day: date, t0: dtime, t1: dtime) -> tuple[datetime, datetime]:
    return (datetime.combine(day, t0, IST).astimezone(timezone.utc),
            datetime.combine(day, t1, IST).astimezone(timezone.utc))


async def export(a) -> None:
    import asyncpg
    from sqlalchemy import create_engine, insert

    import app.db.models  # noqa: F401
    from app.core.trading_params import TradingParams
    from app.db.base import Base
    from app.services.replay.cli import create_sqlite_schema
    from app.services.trading.option_selector import choose_expiry

    env = read_env(a.env_file)
    spot = env.get("NIFTY_TOKEN", "26000")
    vix_token = env.get("VIX_TOKEN", "26017")
    if a.out.exists():
        if not a.replace:
            raise SystemExit(f"{a.out} exists (pass --replace)")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{a.out}{suffix}").unlink(missing_ok=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{a.out.resolve().as_posix()}"
    create_sqlite_schema(url)
    dst = create_engine(url.replace("sqlite+aiosqlite://", "sqlite://", 1))
    T = Base.metadata.tables

    con = await asyncpg.connect(
        host=env.get("POSTGRES_HOST", "localhost"), port=int(env.get("POSTGRES_PORT", "5432")),
        user=env["POSTGRES_USER"], password=env.get("POSTGRES_PASSWORD", ""),
        database=env["POSTGRES_DB"], server_settings={"default_transaction_read_only": "on"})
    try:
        assert await con.fetchval("show default_transaction_read_only") == "on"

        def put(table: str, rows) -> int:
            rows = [{k: naive_utc(v) for k, v in dict(r).items()} for r in rows]
            if rows:
                cols = {c.name for c in T[table].columns}
                rows = [{k: v for k, v in r.items() if k in cols} for r in rows]
                with dst.begin() as c:
                    for i in range(0, len(rows), 20_000):
                        c.execute(insert(T[table]), rows[i:i + 20_000])
            return len(rows)

        lo = datetime.combine(a.date_from, dtime(0), IST).astimezone(timezone.utc)
        hi = datetime.combine(a.date_to + timedelta(days=1), dtime(0), IST).astimezone(timezone.utc)

        futs = await con.fetch("select * from instruments where name='NIFTY' "
                               "and instrument_type::text='FUTIDX'")
        fut_tokens = [r["token"] for r in futs]
        n = put("ohlc", await con.fetch(
            "select * from ohlc where interval='ONE_MINUTE' and token = any($1::text[]) "
            "and timestamp >= $2 and timestamp < $3 order by timestamp, id",
            [spot, *fut_tokens], lo, hi))
        print(f"ohlc: {n} rows (spot + {len(fut_tokens)} futures tokens)")
        print(f"instruments FUTIDX: {put('instruments', futs)}")
        print(f"vix_data: {put('vix_data', await con.fetch('select * from vix_data where date <= $1', a.date_to))}")
        print(f"market_daily_snapshots: {put('market_daily_snapshots', await con.fetch("select * from market_daily_snapshots where underlying='NIFTY' and date <= $1", a.date_to))}")

        tp = TradingParams()
        kept_tokens: set[str] = set()
        day = a.date_from
        while day <= a.date_to:
            if day.weekday() < 5:
                s, e = ist_bounds(day, dtime(9, 0), dtime(15, 31))
                opts = await con.fetch(
                    "select distinct i.token, i.expiry from ticks t join instruments i "
                    "on i.token = t.token where i.name='NIFTY' and i.instrument_type::text='OPTIDX' "
                    "and t.timestamp >= $1 and t.timestamp < $2", s, e)
                choice = choose_expiry([r["expiry"] for r in opts], today=day,
                                       now_t=dtime(9, 15), underlying="NIFTY", tp=tp)
                toks = sorted(r["token"] for r in opts if choice.expiry is not None
                              and r["expiry"] == choice.expiry)
                rows = await con.fetch(
                    "select distinct on (token, ceil(extract(epoch from timestamp))) * "
                    "from ticks where token = any($1::text[]) and timestamp >= $2 and timestamp < $3 "
                    "order by token, ceil(extract(epoch from timestamp)), timestamp desc, id desc",
                    toks, s, e)
                rows = sorted(rows, key=lambda r: (r["timestamp"], r["id"]))
                kept_tokens |= set(toks)
                print(f"{day}: expiry {choice.expiry} ({choice.reason}); "
                      f"{len(toks)} tokens; {put('ticks', rows)} ticks")
            day += timedelta(days=1)
        if kept_tokens:
            print("instruments OPTIDX:", put("instruments", await con.fetch(
                "select * from instruments where token = any($1::text[])", sorted(kept_tokens))))
    finally:
        await con.close()
        dst.dispose()
    print(f"done: {a.out} ({a.out.stat().st_size / 1e6:.0f} MB); "
          f"run the replay with NIFTY_TOKEN={spot} VIX_TOKEN={vix_token}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", type=Path, required=True)
    ap.add_argument("--from", dest="date_from", type=date.fromisoformat, required=True)
    ap.add_argument("--to", dest="date_to", type=date.fromisoformat, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--replace", action="store_true")
    asyncio.run(export(ap.parse_args()))


if __name__ == "__main__":
    main()
