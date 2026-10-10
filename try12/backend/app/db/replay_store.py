"""
Replay result store registry (backtest design §4.2).

The backtest read path reads replay runs from *dedicated* stores — the
``REPLAY_DATABASE_URL`` store (``replay-db``) and generated synthetic SQLite
datasets in ``REPLAY_DATASET_DIR`` — through engines built here, never through
the application's own engine. The main ``DATABASE_URL`` is a refused target
(``assert_not_main_db``). Every engine is read-only by construction: SQLite
connections run ``PRAGMA query_only=ON``; Postgres sessions run with
``default_transaction_read_only``.

No ``Dataset.url`` ever leaves this module in a response: ``Dataset.view()``
is the only serialised form.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import HTTPException
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import app
from app.core.config import settings

log = logging.getLogger(__name__)

DATASET_ID_RE = re.compile(
    r"^(replay-db|artifacts|synthetic-\d{1,3}d-s\d{1,10}-\d{4}-\d{2}-\d{2})$")
_SYNTHETIC_ID_RE = re.compile(r"^synthetic-(\d{1,3})d-s(\d{1,10})-(\d{4}-\d{2}-\d{2})$")
SYNTHETIC_GLOB = "synthetic-*.db"
REPLAY_DB_ID = "replay-db"
ARTIFACTS_ID = "artifacts"
PAPER_DB_NAME = "nifty_scalper"      # the paper-trading database: never a replay store

BACKEND_DIR = Path(app.__file__).resolve().parents[1]     # the replay child's Popen cwd (§4.7)

_UNREACHABLE_RE = re.compile(r"readonly|locked|busy|no such table", re.IGNORECASE)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


class ReplayTargetRefused(ValueError):
    """The URL addresses the application's own (paper-trading) database."""


@dataclass(frozen=True)
class Dataset:
    dataset_id: str
    kind: str          # "replay_db" | "synthetic" | "artifacts"
    label: str         # human text — never a URL
    synthetic: bool    # True for kind == "synthetic"
    exists: bool
    url: Optional[str] = field(default=None, repr=False)   # module-internal; never serialised

    def view(self) -> dict:
        return {"dataset_id": self.dataset_id, "kind": self.kind, "label": self.label,
                "synthetic": self.synthetic, "exists": self.exists}


@dataclass(frozen=True)
class Registry:
    datasets: list[Dataset]
    alias_of: Optional[str] = None     # synthetic id replay-db collapsed onto, else None
    error: Optional[str] = None        # "not_configured" | "refused_main_db" | None


# ── URL helpers ──────────────────────────────────────────────────────────────

def _url_sqlite_path(url: str) -> Optional[Path]:
    """The file a SQLite URL names, resolved the way the replay child resolves it; None otherwise."""
    try:
        u = make_url(url)
    except ArgumentError:                                 # unparseable → not a SQLite file
        return None
    if u.get_backend_name() != "sqlite" or not u.database or u.database == ":memory:":
        return None                                       # any driver: sqlite, sqlite+aiosqlite, …
    p = Path(u.database)
    return p if p.is_absolute() else (BACKEND_DIR / p)    # NOT Path.resolve() against the API's cwd


def _sqlite_key(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))


def store_key(url: str) -> Optional[tuple]:
    """Normalised identity of the store *url* addresses; credentials ignored.
    None for URLs that address no comparable store (unparseable, empty/:memory: SQLite)."""
    try:
        u = make_url(url)
    except ArgumentError:
        return None
    family = u.get_backend_name()
    if family == "sqlite":
        p = _url_sqlite_path(url)
        return None if p is None else ("sqlite", None, None, _sqlite_key(p))
    host = (u.host or "").lower()
    if host in _LOCAL_HOSTS:
        host = "localhost"
    port = u.port or (5432 if family == "postgresql" else None)
    return (family, host, port, u.database or "")


def assert_not_main_db(url: str) -> None:
    """Raise ReplayTargetRefused when *url* addresses the app's own database."""
    key = store_key(url)
    if key is None:
        return
    if key == store_key(settings.DATABASE_URL):
        raise ReplayTargetRefused("replay target refused: application database")
    if key[0] == "postgresql" and key[3] == PAPER_DB_NAME:
        raise ReplayTargetRefused("replay target refused: paper-trading database")


def resolve_dataset_dir() -> Path:
    """The one dataset directory: REPLAY_DATASET_DIR, else <temp>/nifty-replay-datasets;
    a relative setting is anchored at BACKEND_DIR."""
    d = Path(settings.REPLAY_DATASET_DIR or Path(tempfile.gettempdir()) / "nifty-replay-datasets")
    return (d if d.is_absolute() else BACKEND_DIR / d).resolve()


def would_create_final_name(url: str) -> bool:
    """True when opening *url* with the replay CLI would create a final synthetic
    dataset name in the dataset directory. Pure; opens nothing, creates nothing."""
    p = _url_sqlite_path(url)
    if p is None:
        return False
    rp = p.resolve()
    return (rp.parent == resolve_dataset_dir()
            and rp.match(SYNTHETIC_GLOB)
            and not rp.is_file())


# ── dataset ids ──────────────────────────────────────────────────────────────

def synthetic_id(days: int, seed: int, start: date) -> str:
    return f"synthetic-{int(days)}d-s{int(seed)}-{start.isoformat()}"


def parse_synthetic_id(dataset_id: str) -> Optional[tuple[int, int, date]]:
    """(days, seed, start) for a synthetic id, else None."""
    m = _SYNTHETIC_ID_RE.match(dataset_id or "")
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2)), date.fromisoformat(m.group(3))
    except ValueError:
        return None


def _sqlite_url(p: Path) -> str:
    return f"sqlite+aiosqlite:///{p.as_posix()}"


def _synthetic_label(days: int, seed: int, start: date) -> str:
    return f"Synthetic {days}d seed {seed} from {start.isoformat()}"


# ── registry ─────────────────────────────────────────────────────────────────

_alias_logged: set[str] = set()
_refused_logged: set[str] = set()


def _synthetic_datasets() -> list[tuple[Dataset, str]]:
    """(Dataset, identity key) per synthetic-*.db in the dataset dir, newest mtime first."""
    d = resolve_dataset_dir()
    if not d.is_dir():
        return []
    found = []
    for p in d.glob(SYNTHETIC_GLOB):                     # non-recursive: .staging/, jobs/ never listed
        parsed = parse_synthetic_id(p.stem)
        if parsed is None or not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime_ns
        except OSError:
            continue
        days, seed, start = parsed
        ds = Dataset(dataset_id=p.stem, kind="synthetic", label=_synthetic_label(days, seed, start),
                     synthetic=True, exists=True, url=_sqlite_url(p))
        found.append((mtime, ds, _sqlite_key(p)))
    found.sort(key=lambda x: (-x[0], x[1].dataset_id))
    return [(ds, key) for _, ds, key in found]


def _artifacts_dataset() -> Optional[Dataset]:
    from app.services.backtest import artifacts
    d = artifacts.artifact_dir()
    if d is None or not any(d.glob("RPL-*.json")):
        return None
    return Dataset(dataset_id=ARTIFACTS_ID, kind="artifacts", label="Report artefacts (read-only)",
                   synthetic=False, exists=True)


def registry() -> Registry:
    """The datasets, in registry order. Not cached: re-globbed on every call."""
    synth = _synthetic_datasets()
    out: list[Dataset] = []
    alias_of: Optional[str] = None
    error: Optional[str] = None
    url = settings.REPLAY_DATABASE_URL
    if not url:
        error = "not_configured"
    else:
        try:
            assert_not_main_db(url)
        except ReplayTargetRefused:
            error = "refused_main_db"
            if REPLAY_DB_ID not in _refused_logged:
                _refused_logged.add(REPLAY_DB_ID)
                log.error("REPLAY_DATABASE_URL addresses the application database; "
                          "the replay-db dataset is disabled")
        if error is None:
            key = store_key(url)
            for ds, skey in synth:
                if key is not None and key[0] == "sqlite" and key[3] == skey:
                    alias_of = ds.dataset_id
                    break
            if alias_of is not None:
                if alias_of not in _alias_logged:
                    _alias_logged.add(alias_of)
                    log.info("replay-db is the same store as dataset %s; serving it under "
                             "the synthetic id", alias_of)
            else:
                p = _url_sqlite_path(url)
                exists = p.is_file() if p is not None else True
                out.append(Dataset(dataset_id=REPLAY_DB_ID, kind="replay_db",
                                   label="Replay database", synthetic=False, exists=exists,
                                   url=url))
    out.extend(ds for ds, _ in synth)
    art = _artifacts_dataset()
    if art is not None:
        out.append(art)
    return Registry(datasets=out, alias_of=alias_of, error=error)


def list_datasets() -> list[Dataset]:
    return registry().datasets


def validate_dataset_id(dataset_id: str) -> str:
    if not dataset_id or not DATASET_ID_RE.match(dataset_id):
        raise HTTPException(status_code=400, detail=f"dataset must match {DATASET_ID_RE.pattern}")
    return dataset_id


def resolve(dataset_id: str, reg: Optional[Registry] = None) -> Dataset:
    """The registered dataset for *dataset_id*: 400 on a malformed id, 404 when absent.
    ``replay-db`` resolves to the synthetic dataset it aliases (§4.2)."""
    validate_dataset_id(dataset_id)
    reg = reg or registry()
    want = reg.alias_of if (dataset_id == REPLAY_DB_ID and reg.alias_of) else dataset_id
    for ds in reg.datasets:
        if ds.dataset_id == want:
            return ds
    if dataset_id == REPLAY_DB_ID and reg.error == "refused_main_db":
        raise HTTPException(status_code=503, detail="replay_store_misconfigured")
    raise HTTPException(status_code=404, detail=f"dataset '{dataset_id}' not found")


# ── engines and sessions ─────────────────────────────────────────────────────

_engines: dict[tuple[str, str], AsyncEngine] = {}


def _unreachable(dataset_id: str, exc: BaseException) -> HTTPException:
    log.error("replay store %r unreachable (%s)", dataset_id, type(exc).__name__)
    return HTTPException(status_code=503, detail=f"replay store '{dataset_id}' unreachable")


def _build_engine(ds: Dataset) -> AsyncEngine:
    from sqlalchemy import event
    from sqlalchemy.pool import NullPool

    from app.services.replay.cli import register_sqlite_bigint

    url = ds.url or ""
    assert_not_main_db(url)
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        register_sqlite_bigint()
        engine = create_async_engine(url, poolclass=NullPool)

        @event.listens_for(engine.sync_engine, "connect")
        def _read_only(dbapi_conn, _record):              # noqa: ARG001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA query_only=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
        return engine
    if make_url(url).drivername == "postgresql+asyncpg":
        return create_async_engine(
            url, pool_pre_ping=True, pool_size=2, max_overflow=2,
            connect_args={"server_settings": {"default_transaction_read_only": "on"}})
    return create_async_engine(url, pool_pre_ping=True)


def engine_for(ds: Dataset) -> AsyncEngine:
    """The cached read-only engine for a tier-1 dataset. A SQLite engine is only
    built for a file that already exists (aiosqlite would create it)."""
    if ds.kind == "artifacts" or not ds.url:
        raise ValueError(f"dataset {ds.dataset_id!r} has no store")
    p = _url_sqlite_path(ds.url)
    if p is not None and not p.is_file():
        raise _unreachable(ds.dataset_id, FileNotFoundError())
    key = (ds.dataset_id, ds.url)
    eng = _engines.get(key)
    if eng is None:
        try:
            eng = _build_engine(ds)
        except ReplayTargetRefused:
            raise HTTPException(status_code=503, detail="replay_store_misconfigured")
        except Exception as exc:                          # bad driver / URL: unreachable, never 500
            raise _unreachable(ds.dataset_id, exc)
        _engines[key] = eng
    return eng


@asynccontextmanager
async def store_session(ds: Dataset) -> AsyncIterator[AsyncSession]:
    """A read-only session over *ds*'s store. Connect failures and the
    readonly|locked|busy|no such table OperationalErrors are 503, never 500."""
    eng = engine_for(ds)
    factory = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        try:
            await session.connection()
        except HTTPException:
            raise
        except Exception as exc:
            raise _unreachable(ds.dataset_id, exc)
        try:
            yield session
        except OperationalError as exc:
            if _UNREACHABLE_RE.search(str(exc)):
                raise _unreachable(ds.dataset_id, exc)
            raise


def reset_cache() -> None:
    """Drop cached engines (not disposed: callers own nothing here) and the log-once sets."""
    _engines.clear()
    _alias_logged.clear()
    _refused_logged.clear()


async def dispose_all() -> None:
    engines = list(_engines.values())
    _engines.clear()
    for eng in engines:
        try:
            await eng.dispose()
        except Exception as exc:                          # never raises into the lifespan
            log.warning("replay store engine dispose failed (%s)", type(exc).__name__)
