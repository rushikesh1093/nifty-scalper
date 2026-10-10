"""
Replay CLI (design §14.6, plan decision 3).

    python -m app.services.replay.cli \\
        --from 2026-09-01 --to 2026-09-30 --underlyings NIFTY,BANKNIFTY \\
        --params-file conservative.json --seed 20260901 --oos-split 0.70 \\
        --label "baseline-conservative" [--walk-forward N] [--notes "..."] \\
        [--database-url sqlite+aiosqlite:///path.db] [--out DIR]

Exit codes: 0 complete, 1 failed (the run row is FAILED with its traceback),
2 no TRADEABLE day (coverage written, no metrics, D11), 3 a startup
assertion (every violation printed, no replay_runs row written).

``--database-url`` defaults to the settings URL. A ``sqlite+aiosqlite`` URL
gets its schema from ``Base.metadata.create_all`` because migrations
0001-0009 are PostgreSQL-only. It is an argument, not a decision branch.
PAPER ONLY: the replay never places, modifies or cancels an order.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

TASK_DIR = Path(".agents") / "tasks" / "paper-trading-hardening"


def default_artifact_dir() -> Path:
    """<repo>/.agents/tasks/paper-trading-hardening/runs — outside any worktree's
    tracked tree. Found by walking up from this file."""
    from app.core.config import settings
    if settings.REPLAY_ARTIFACT_DIR:
        return Path(settings.REPLAY_ARTIFACT_DIR)
    for parent in Path(__file__).resolve().parents:
        if (parent / TASK_DIR).is_dir():
            return parent / TASK_DIR / "runs"
    return Path.cwd() / "replay_runs"


def register_sqlite_bigint() -> None:
    """SQLite only aliases rowid for "INTEGER PRIMARY KEY": render BigInteger as
    INTEGER there so autoincrement ids work (the test suite does the same)."""
    from sqlalchemy import BigInteger
    from sqlalchemy.ext.compiler import compiles

    @compiles(BigInteger, "sqlite")
    def _bigint_sqlite(type_, compiler, **kw):        # noqa: ARG001
        return "INTEGER"


def create_sqlite_schema(async_url: str) -> None:
    """Base.metadata.create_all on the SQLite file behind *async_url*."""
    from sqlalchemy import create_engine

    import app.db.models  # noqa: F401  (registers every table)
    from app.db.base import Base

    register_sqlite_bigint()
    sync_url = async_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    engine = create_engine(sync_url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def session_factory_for(url: Optional[str]):
    """(session factory, engine to dispose or None)."""
    if not url:
        from app.db.base import AsyncSessionLocal
        return AsyncSessionLocal, None
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    if url.startswith("sqlite+aiosqlite://"):
        from sqlalchemy import event

        create_sqlite_schema(url)
        engine = create_async_engine(url, poolclass=NullPool)

        @event.listens_for(engine.sync_engine, "connect")
        def _scratch_pragmas(dbapi_conn, _record):        # noqa: ARG001
            # A scratch replay file: per-commit fsync dominates the run time on
            # Windows and buys nothing (a crashed run is re-run, not recovered).
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=OFF")
            cur.close()
    else:
        engine = create_async_engine(url, pool_pre_ping=True)
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), engine


def parse_args(argv=None) -> argparse.Namespace:
    from app.core.config import settings
    ap = argparse.ArgumentParser(prog="python -m app.services.replay.cli",
                                 description="Replay stored history through the production "
                                             "paper-trading code path (PAPER ONLY).")
    ap.add_argument("--from", dest="date_from", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="date_to", required=True, type=date.fromisoformat)
    ap.add_argument("--underlyings", default="NIFTY",
                    help="comma-separated: NIFTY,BANKNIFTY")
    ap.add_argument("--params-file", type=Path, default=None,
                    help="JSON object of TradingParams overrides (+ square_off_time, "
                         "user_strategy_flags, study_phase, trading_halted)")
    ap.add_argument("--seed", type=int, default=settings.REPLAY_DEFAULT_SEED)
    ap.add_argument("--oos-split", type=float, default=0.70)
    ap.add_argument("--walk-forward", type=int, default=0)
    ap.add_argument("--label", default="")
    ap.add_argument("--notes", default="",
                    help="declared fitted parameters for this run (report item 9)")
    ap.add_argument("--bootstrap-iterations", type=int, default=10_000)
    ap.add_argument("--database-url", default=None,
                    help="defaults to the settings DATABASE_URL")
    ap.add_argument("--out", type=Path, default=None, help="artefact directory")
    args = ap.parse_args(argv)
    if not 0.0 < args.oos_split < 1.0:
        ap.error("--oos-split must be in (0, 1)")
    if (args.date_to - args.date_from).days > settings.REPLAY_MAX_RANGE_DAYS:
        ap.error(f"range must be <= {settings.REPLAY_MAX_RANGE_DAYS} days")
    return args


async def main_async(argv=None) -> int:
    from app.services.replay.engine import ReplayArgs, ReplayEngine
    from app.services.replay.report import build_and_write

    a = parse_args(argv)
    overrides = {}
    if a.params_file is not None:
        overrides = json.loads(a.params_file.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            print("--params-file must hold a JSON object", file=sys.stderr)
            return 1
    sessions, engine = session_factory_for(a.database_url)
    args = ReplayArgs(
        date_from=a.date_from, date_to=a.date_to,
        underlyings=tuple(u.strip().upper() for u in a.underlyings.split(",") if u.strip()),
        params_overrides=overrides, seed=a.seed, oos_split=a.oos_split,
        walk_forward=a.walk_forward, label=a.label, notes=a.notes,
        bootstrap_iterations=a.bootstrap_iterations,
        out_dir=a.out or default_artifact_dir(),
    )
    try:
        result = await ReplayEngine(sessions, report_builder=build_and_write).run(args)
    finally:
        if engine is not None:
            await engine.dispose()

    if result.violations:
        print("Startup assertions failed:" if result.exit_code == 3 else "Invalid arguments:",
              file=sys.stderr)
        for v in result.violations:
            print(f"  - {v}", file=sys.stderr)
    if result.coverage is not None:
        print("Coverage:", json.dumps(result.coverage.summary(), default=str))
    if result.metrics is not None:
        m = result.metrics
        print(f"run_id={result.run_id} trades={m.trades} expectancy_r={m.expectancy_r} "
              f"expectancy_inr={m.expectancy_inr} determinism_hash={result.determinism_hash}")
    if result.validation:
        print(result.validation.get("recommendation_line", ""))
    for path in result.artefacts:
        print(f"artefact: {path}")
    if result.error:
        print(f"FAILED: {result.error}", file=sys.stderr)
    print(f"status={result.status} exit={result.exit_code}")
    return result.exit_code


def main(argv=None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
