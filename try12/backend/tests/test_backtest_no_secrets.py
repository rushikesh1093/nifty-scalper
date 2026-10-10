"""No backtest response carries a secret, a URL or a filesystem path (C7;
backtest design §7.1, §9, §10).

Every backtest route is hit with REPLAY_DATABASE_URL, ADMIN_API_KEY and
DATABASE_URL set to distinctive values, including two launcher jobs whose output
tails were captured from fake children that printed absolute paths, both
database URLs and the artefact path. Every key and string value of every JSON
body is then checked for the configured values and for path/URL patterns. The
redaction is produced at capture time (jobs._tail_line); this test proves it."""
import asyncio
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.db import replay_store
from app.db.replay_store import BACKEND_DIR
from app.services.backtest import artifacts, jobs
from tests.replay_fixtures import run
from tests.test_backtest_jobs import Spawner, gen

API = "/api/v1/backtest"
KEY = "adm-NOSECRET-5f1c9e"
HDR = {"X-API-Key": KEY}
PG_PASSWORD = "pw-NOSECRET-91b2"
PG_DB = "db_nosecret_4c7d"
SID = "synthetic-5d-s1-2026-09-01"
REAL_ARTEFACTS = ("RPL-01M4GCZX80SBAR1TXKC8KSP3WE", "RPL-01M4GM4G8715T96KC6GB2P8FM7")

PATTERNS = [re.compile(p) for p in (
    r"(?<![A-Za-z])[A-Za-z]:[\\/]",          # a drive-letter path
    r"/home/", r"/tmp/", r"/Users/", r"%TEMP%",
    r"sqlite\+", r"postgresql", r"://",
)]


def _real_runs():
    for parent in Path(__file__).resolve().parents:
        d = parent / ".agents" / "tasks" / "paper-trading-hardening" / "runs"
        if d.is_dir():
            return d
    return None


@pytest.fixture
def configured(four_trade, tmp_path, monkeypatch):
    art = tmp_path / "runs"
    art.mkdir()
    copied = []
    src = _real_runs()
    for rid in REAL_ARTEFACTS:
        if src is not None and (src / f"{rid}.json").is_file():
            shutil.copy2(src / f"{rid}.json", art / f"{rid}.json")
            copied.append(rid)
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", four_trade.url_a)
    monkeypatch.setattr(settings, "REPLAY_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(settings, "REPLAY_ARTIFACT_DIR", str(art))
    monkeypatch.setattr(settings, "ADMIN_API_KEY", KEY)
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", PG_PASSWORD)
    monkeypatch.setattr(settings, "POSTGRES_DB", PG_DB)
    jobs._running = None
    jobs._ring.clear()
    jobs._tasks.clear()
    replay_store.reset_cache()           # only the registry's engines; four_trade's stay
    artifacts.reset_cache()
    yield SimpleNamespace(store=four_trade, art=art, copied=copied, tmp=tmp_path)
    jobs._running = None
    jobs._ring.clear()
    jobs._tasks.clear()
    replay_store.reset_cache()
    artifacts.reset_cache()


def _leaky_replay(run_id: str, tmp: Path, release=None) -> dict:
    """A replay child that prints everything a real one could leak."""
    def out(argv):
        return ["Coverage: {\"days\": 1}",
                f"using --database-url {argv[argv.index('--database-url') + 1]}",
                f"run_id={run_id} trades=4",
                f"artefact: {argv[argv.index('--out') + 1]}{os.sep}{run_id}.json",
                "status=COMPLETE exit=0"]
    return {"rc": 0, "release": release, "out": out, "err": [
        f'  File "{BACKEND_DIR / "app" / "services" / "replay" / "cli.py"}", line 176',
        f'  File "{Path(sys.executable).parent / "Lib" / "asyncio" / "runners.py"}", line 118',
        f"warning: could not reach {settings.DATABASE_URL}",
        f"FAILED: OperationalError('unable to open database file {tmp / 'x.db'}')",
        "  File \"/usr/lib/python3.12/asyncio/runners.py\", line 118"]}


def _strings(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k} (key)", str(k)
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


async def _settle():
    pending = [t for t in list(jobs._tasks) if not t.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), 15)


def test_no_backtest_response_leaks_a_secret_url_or_path(configured, monkeypatch):
    c0 = configured
    run1 = c0.store.run1.run_id
    hold = threading.Event()
    sp = Spawner(_leaky_replay(run1, c0.tmp, release=hold), gen(), _leaky_replay(run1, c0.tmp))
    monkeypatch.setattr(jobs, "_popen", sp)
    launch = {"dataset": {"kind": "replay_db"}, "date_from": "2026-09-01",
              "date_to": "2026-09-07", "underlyings": ["NIFTY"], "label": "no-secrets",
              "notes": "", "overrides": {"capital": 15000.0}}
    synthetic = {**launch, "dataset": {"kind": "synthetic", "days": 5, "seed": 1,
                                       "start": "2026-09-01"}}
    from app.main import app

    async def go():
        got = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            async def call(method, path, **kw):
                r = await c.request(method, f"{API}{path}", **kw)
                got.append((f"{method} {path} -> {r.status_code}", r))
                return r
            first = await call("POST", "/jobs", json=launch, headers=HDR)
            assert first.status_code == 202, first.text
            assert (await call("POST", "/jobs", json=synthetic, headers=HDR)).status_code == 409
            hold.set()
            await _settle()
            second = await call("POST", "/jobs", json=synthetic, headers=HDR)
            assert second.status_code == 202, second.text
            await _settle()
            ids = [first.json()["job_id"], second.json()["job_id"]]
            for path in ("/datasets", "/runs", "/runs?dataset=replay-db", f"/runs?dataset={SID}",
                         f"/runs/{run1}", f"/runs/{run1}/metrics", f"/runs/{run1}/metrics/by/strategy",
                         f"/runs/{run1}/metrics/by/day", f"/runs/{run1}/no-trade-decisions/summary",
                         f"/runs/{run1}/trades", f"/runs/{run1}/series",
                         "/runs/PAPER", "/runs/RPL-NOSUCHRUN1", "/runs?dataset=../x",
                         "/launch-options"):
                await call("GET", path)
            for rid in c0.copied:
                for sub in ("", "/metrics", "/metrics/by/regime", "/metrics/by/day",
                            "/no-trade-decisions/summary", "/trades", "/series"):
                    await call("GET", f"/runs/{rid}{sub}")
            if c0.copied:
                await call("GET", "/runs?dataset=artifacts")
            await call("POST", "/params/validate", headers=HDR,
                       json={"overrides": {"partial_exit_enabled": True, "max_lots_per_trade": 1}})
            await call("POST", "/jobs", json={**launch, "date_to": "2026-12-31"}, headers=HDR)
            await call("POST", "/jobs", json={**launch, "database_url": "not-a-url"}, headers=HDR)
            await call("POST", "/jobs", json=launch)                       # 401
            await call("GET", "/jobs", headers=HDR)
            for jid in ids:
                await call("GET", f"/jobs/{jid}", headers=HDR)
            await call("GET", "/jobs/BTJ-000000000000", headers=HDR)
            jobs._ring.clear()                                            # read back from disk
            await call("GET", "/jobs", headers=HDR)
            for jid in ids:
                await call("GET", f"/jobs/{jid}", headers=HDR)
        return got, ids

    got, ids = run(go())
    store_path = Path(c0.store.url_a.split(":///", 1)[1])
    secrets = [KEY, PG_PASSWORD, PG_DB, settings.DATABASE_URL, settings.REPLAY_DATABASE_URL,
               str(store_path), store_path.as_posix(), str(c0.tmp), c0.tmp.as_posix(),
               str(BACKEND_DIR), BACKEND_DIR.as_posix(), str(c0.art)]
    problems, seen = [], 0
    for label, r in got:
        assert r.headers["content-type"].startswith("application/json"), label
        for where, s in _strings(r.json()):
            seen += 1
            problems += [f"{label} {where}: configured value" for v in secrets if v and v in s]
            problems += [f"{label} {where}: {p.pattern!r} in {s[:120]!r}"
                         for p in PATTERNS if p.search(s)]
    assert problems == []
    assert seen > 500                                       # the walk really covered the bodies

    # the tails were produced by redaction, not by a quiet child
    first = next(r.json() for label, r in got if label == f"GET /jobs/{ids[0]} -> 200")
    assert f"artefact: {run1}.json" in first["stdout_tail"]
    assert "using --database-url <url>" in first["stdout_tail"]
    assert any(line.startswith('  File "<backend>') for line in first["stderr_tail"])
    assert "warning: could not reach <url>" in first["stderr_tail"]
    assert (first["run_id"], first["run_id_source"]) == (run1, "stdout")
    second = next(r.json() for label, r in got if label == f"GET /jobs/{ids[1]} -> 200")
    assert any("<datasets>" in line for line in second["stdout_tail"])
    statuses = {label.rsplit(" ", 1)[1] for label, _ in got}
    assert {"200", "202", "400", "401", "404", "409", "422"} <= statuses
