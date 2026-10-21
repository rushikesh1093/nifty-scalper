"""The replay launcher (backtest design §4.7, §4.8, §7.1, §10 "Jobs").

Child processes are faked through ``jobs._popen`` (the only spawn site) except
in the one real-subprocess test and the Windows console-interrupt test. Every
dataset, job and artefact directory is under tmp_path. Nothing here connects to
Postgres or Redis, and no child ever runs the real replay CLI."""
import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine

from app.core.config import settings
from app.db import replay_store
from app.db.replay_store import BACKEND_DIR, Dataset
from app.services.backtest import artifacts, jobs
from tests.replay_fixtures import run

API = "/api/v1/backtest"
KEY = "test-admin-key-0123"
HDR = {"X-API-Key": KEY}
SID = "synthetic-5d-s1-2026-09-01"


def _url(p: Path) -> str:
    return f"sqlite+aiosqlite:///{p.as_posix()}"


def _reset_jobs():
    jobs._running = None
    jobs._ring.clear()
    jobs._tasks.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    ds_dir = tmp_path / "datasets"
    monkeypatch.setattr(settings, "REPLAY_DATASET_DIR", str(ds_dir))
    monkeypatch.setattr(settings, "REPLAY_ARTIFACT_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", None)
    monkeypatch.setattr(settings, "ADMIN_API_KEY", KEY)
    _reset_jobs()
    replay_store.reset_cache()
    artifacts.reset_cache()
    yield SimpleNamespace(tmp=tmp_path, ds_dir=ds_dir, final=ds_dir / f"{SID}.db",
                          outside=_url(tmp_path / "other" / "replay.db"))
    _reset_jobs()
    replay_store.reset_cache()
    artifacts.reset_cache()


def body(kind: str = "synthetic", **kw) -> dict:
    ds = ({"kind": "replay_db"} if kind == "replay_db"
          else {"kind": "synthetic", "days": 5, "seed": 1, "start": "2026-09-01"})
    b = {"dataset": ds, "date_from": "2026-09-01", "date_to": "2026-09-07",
         "underlyings": ["NIFTY"], "seed": 7, "label": "", "notes": "", "overrides": {}}
    b.update(kw)
    return b


def req(kind: str = "synthetic", **kw) -> jobs.LaunchRequest:
    return jobs.LaunchRequest.model_validate(body(kind, **kw))


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _settle(timeout: float = 15.0) -> None:
    pending = [t for t in list(jobs._tasks) if not t.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout)


async def _until(pred, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _disk(env, job_id: str) -> dict:
    return json.loads((env.ds_dir / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))


def _staging(env) -> list:
    d = env.ds_dir / ".staging"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _make_store(path: Path, *rows: dict) -> Path:
    from app.db.models.replay import ReplayRun
    from app.services.replay.cli import create_sqlite_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    create_sqlite_schema(_url(path))
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as c:
            for r in rows:
                c.execute(ReplayRun.__table__.insert().values(**r))
    finally:
        eng.dispose()
    return path


def _row(run_id: str, *, status: str = "COMPLETE", params_hash: str = "other-hash",
         date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), seed: int = 7,
         created_at=None) -> dict:
    return {"run_id": run_id, "status": status, "params_hash": params_hash,
            "date_from": date_from, "date_to": date_to, "seed": seed,
            "created_at": created_at or datetime(2026, 10, 1, tzinfo=timezone.utc),
            "underlyings": ["NIFTY"]}


def _rec(env, kind: str = "replay_db", target: Dataset = None, **kw) -> jobs.JobRecord:
    target = target or Dataset("replay-db", "replay_db", "Replay database", False, False,
                               url=env.outside)
    return jobs.new_record(req(kind, **kw), target, None, False, 0, ["NIFTY"], "admin")


# ── fake children ────────────────────────────────────────────────────────────

class FakeProc:
    """A scripted child. Exits at once with *rc*, or (``block``) runs until kill(),
    or (``release``) until an event is set. ``wait(timeout)`` raises TimeoutExpired
    per poll while running, like the real one. ``ignore_kill_s`` delays the exit
    after kill()."""
    _next_pid = 41000

    def __init__(self, argv, kwargs, *, rc=0, out=(), err=(), block=False, release=None,
                 rc_after_kill=1, ignore_kill_s=0.0):
        self.argv, self.kwargs = argv, kwargs
        self.stdout = io.StringIO("".join(f"{line}\n" for line in out))
        self.stderr = io.StringIO("".join(f"{line}\n" for line in err))
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.rc, self.block, self.release = rc, block, release
        self.rc_after_kill, self.ignore_kill_s = rc_after_kill, ignore_kill_s
        self.killed = threading.Event()
        self._killed_at = 0.0
        self.returncode = None

    def _exited(self) -> bool:
        if self.returncode is not None:
            return True
        if self.killed.is_set() and time.monotonic() - self._killed_at >= self.ignore_kill_s:
            self.returncode = self.rc_after_kill
        elif not self.block and (self.release is None or self.release.is_set()):
            self.returncode = self.rc
        return self.returncode is not None

    def poll(self):
        return self.returncode if self._exited() else None

    def wait(self, timeout=None):
        end = time.monotonic() + (timeout or 0.0)
        while not self._exited():
            if time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            time.sleep(0.01)
        return self.returncode

    def kill(self):
        if not self.killed.is_set():
            self._killed_at = time.monotonic()
            self.killed.set()


class Spawner:
    """Replaces jobs._popen: one script per call, in order; records every child."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls: list[FakeProc] = []
        self.started = threading.Event()

    def __call__(self, argv, **kwargs):
        script = dict(self.scripts.pop(0)) if self.scripts else dict(REPLAY_OK)
        on_start = script.pop("on_start", None)
        if on_start is not None:
            on_start(argv)
        if callable(script.get("out")):
            script["out"] = script["out"](argv)
        p = FakeProc(argv, kwargs, **script)
        self.calls.append(p)
        self.started.set()
        return p


REPLAY_OK = {"rc": 0, "out": ["run_id=RPL-JOBTEST1 trades=0 expectancy_r=None",
                              "status=COMPLETE exit=0"]}


def _write_staging(argv, valid: bool = True) -> None:
    from app.services.replay.cli import create_sqlite_schema
    out = Path(argv[argv.index("--out") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        create_sqlite_schema(_url(out))
    else:
        out.write_bytes(b"half-built")


def gen(rc: int = 0, valid: bool = True, **kw) -> dict:
    """The generator: writes its --out staging file and prints the real path."""
    return {"rc": rc, "on_start": lambda argv: _write_staging(argv, valid),
            "out": lambda argv: [f"SYNTHETIC replay database written: "
                                 f"{argv[argv.index('--out') + 1]}"], **kw}


# ── argv, exit codes, labels, redaction (unit) ───────────────────────────────

@pytest.mark.parametrize("label", ["-x", "--database-url", "; rm -rf /", "a=b", ""])
def test_argv_round_trips_through_the_real_parser(env, label):
    from app.services.replay.cli import parse_args

    rec = _rec(env, label=label, notes="-v")
    argv = jobs.replay_argv(rec)
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert argv[:3] == [sys.executable, "-m", "app.services.replay.cli"]
    args = parse_args(argv[3:])
    assert args.label == label and args.notes == "-v"
    assert args.database_url == rec.url == env.outside          # the server's own target
    assert args.synthetic is None                                # a non-aliased replay-db
    assert (args.date_from, args.date_to, args.seed) == (rec.date_from, rec.date_to, 7)
    assert args.params_file == jobs.params_path(rec)


def test_argv_for_a_synthetic_target_forces_the_label_and_the_flag(env):
    from app.services.replay.cli import parse_args

    target = Dataset(SID, "synthetic", "s", True, True, url=_url(env.final))
    rec = _rec(env, target=target, label="capital-15000-A")
    args = parse_args(jobs.replay_argv(rec)[3:])
    assert args.synthetic is True and args.label == "capital-15000-A (synthetic)"
    assert args.database_url == _url(env.final)


def test_exit_code_mapper():
    m = jobs.map_exit_code
    assert m(0, []) == "SUCCEEDED"
    assert m(2, ["Coverage: {}", "status=NO_DATA exit=2"]) == "NO_DATA"
    assert m(2, []) == "INVALID"                       # argparse: message on stderr, no status
    assert m(3, ["status=INVALID exit=3"]) == "INVALID"
    assert m(1, ["status=FAILED exit=1"]) == "FAILED"
    assert m(-9, []) == "FAILED" and m(None, []) == "FAILED"


def test_label_forcing():
    f = jobs.force_label
    assert f("", "synthetic") == "synthetic"
    assert f("capital-15000-A", "synthetic") == "capital-15000-A (synthetic)"
    assert f("my Synthetic run", "synthetic") == "my Synthetic run"
    assert f("capital-15000-A", "replay_db") == "capital-15000-A"


def test_tail_line_redaction(env):
    t = jobs._tail_line
    assert t("artefact: C:\\Users\\someone\\runs\\RPL-X1.json") == "artefact: RPL-X1.json"
    assert t(f"artefact: {jobs.artifact_write_dir() / 'RPL-X2.json'}") == "artefact: RPL-X2.json"
    line = t(f"error in {BACKEND_DIR / 'app' / 'main.py'}")
    assert line.startswith("error in <backend>") and str(BACKEND_DIR) not in line
    staged = t(f"SYNTHETIC replay database written: "
               f"{env.ds_dir.resolve() / '.staging' / 'BTJ-0123456789ab.db'}")
    assert "<datasets>" in staged and staged.endswith("BTJ-0123456789ab.db")
    assert str(env.tmp) not in staged
    assert len(t("x" * 900)) == 500
    url = t("FAILED: OperationalError('sqlite+aiosqlite:///C:/a/b.db')")
    assert "://" not in url and "sqlite+" not in url and "C:/" not in url
    assert "C:\\" not in t('  File "C:\\Python312\\Lib\\asyncio\\runners.py", line 118')
    assert t("/home/u/app/x.py failed") == "x.py failed"


def test_child_env_blanks_secrets(env, monkeypatch):
    monkeypatch.setenv("ANGEL_API_KEY", "angel-secret-1")
    monkeypatch.setenv("ANGEL_EXTRA_TOKEN", "angel-secret-2")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret-3")
    e = jobs._child_env()
    for k in ("ADMIN_API_KEY", "ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD",
              "ANGEL_TOTP_SECRET"):
        assert e[k] == ""                     # blank, so backend/.env cannot refill it
    assert not any(k.upper() == "ANGEL_EXTRA_TOKEN" for k in e)
    assert not {"angel-secret-1", "angel-secret-2", "admin-secret-3"} & set(e.values())
    assert e["PYTHONUNBUFFERED"] == "1"
    assert e["REPLAY_ARTIFACT_DIR"] == str(jobs.artifact_write_dir())


def test_artifact_write_dir_is_created_and_never_none(env, monkeypatch):
    missing = env.tmp / "nope" / "runs"
    monkeypatch.setattr(settings, "REPLAY_ARTIFACT_DIR", str(missing))
    sp = Spawner(gen(), REPLAY_OK)
    monkeypatch.setattr(jobs, "_popen", sp)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body(), headers=HDR)
        assert r.status_code == 202, r.text
        assert missing.is_dir()                       # created in step 1, before admission
        await _settle()
    run(go())
    assert len(sp.calls) == 2
    for p in sp.calls:
        assert not any("None" in a for a in p.argv)
    replay = sp.calls[1].argv
    assert replay[replay.index("--out") + 1] == str(missing)
    assert sp.calls[1].kwargs["env"]["REPLAY_ARTIFACT_DIR"] == str(missing)
    assert jobs.artifact_write_dir() is not None


# ── spawn kwargs (h) ─────────────────────────────────────────────────────────

def test_h_both_steps_spawn_with_the_isolating_kwargs(env, monkeypatch):
    sp = Spawner(gen(), REPLAY_OK)
    monkeypatch.setattr(jobs, "_popen", sp)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            assert r.status_code == 202, r.text
            await _settle()
            return (await c.get(f"{API}/jobs/{r.json()['job_id']}", headers=HDR)).json()
    rec = run(go())
    assert rec["state"] == "SUCCEEDED" and rec["steps"] == ["dataset", "replay"]
    assert (rec["run_id"], rec["run_id_source"]) == ("RPL-JOBTEST1", "stdout")
    assert len(sp.calls) == 2
    assert sp.calls[0].argv[1].endswith("make_synthetic_replay_db.py")
    assert sp.calls[1].argv[1:3] == ["-m", "app.services.replay.cli"]
    want = jobs._child_spawn_kwargs()
    for p in sp.calls:
        assert {k: p.kwargs[k] for k in want} == want
        assert "shell" not in p.kwargs and p.kwargs["cwd"] == str(BACKEND_DIR)
        if os.name == "nt":
            assert p.kwargs["creationflags"] == (subprocess.CREATE_NEW_PROCESS_GROUP
                                                 | subprocess.CREATE_NO_WINDOW)
            assert "start_new_session" not in p.kwargs
        else:
            assert p.kwargs["start_new_session"] is True and "creationflags" not in p.kwargs


def test_h_posix_branch_of_the_spawn_kwargs(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(os, "name", "posix")
        got = jobs._child_spawn_kwargs()
    assert got == {"start_new_session": True}


@pytest.mark.skipif(os.name != "nt", reason="subprocess.CREATE_* exist only on Windows")
def test_h_windows_branch_of_the_spawn_kwargs(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        got = jobs._child_spawn_kwargs()
    assert got == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                   | subprocess.CREATE_NO_WINDOW}


# ── JobRecord ────────────────────────────────────────────────────────────────

def test_finish_is_the_single_guarded_terminal_setter(env):
    rec = _rec(env)
    rec.started_at = jobs.utcnow()
    rec.finish("TIMEOUT", step="replay", timed_out=True, error="slow")
    before = (rec.state, rec.step, rec.error, rec.timed_out, rec.finished_at)
    rec.finish("CANCELLED", step="dataset")
    assert (rec.state, rec.step, rec.error, rec.timed_out, rec.finished_at) == before
    fresh = _rec(env)
    fresh.finish("CANCELLED", step="replay", error="x", timed_out=True)
    assert (fresh.state, fresh.step, fresh.error, fresh.timed_out) == \
        ("CANCELLED", "replay", "x", True)
    assert fresh.finished_at is not None and fresh.duration_s is None   # never started


def test_launch_response_projection_persistence_and_overlap_read_back(env, monkeypatch):
    store = _make_store(env.tmp / "other" / "replay.db", _row("RPL-PRIOR1"))
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", _url(store))
    hold = threading.Event()
    sp = Spawner({**REPLAY_OK, "release": hold})
    monkeypatch.setattr(jobs, "_popen", sp)
    assert set(jobs.LAUNCH_FIELDS) < set(jobs.PUBLIC_FIELDS)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body("replay_db"), headers=HDR)
            assert r.status_code == 202, r.text
            launch = r.json()
            admitted = jobs._ring[0].view()
            hold.set()
            await _settle()
            detail = (await c.get(f"{API}/jobs/{launch['job_id']}", headers=HDR)).json()
            jobs._ring.clear()                                # forget the in-memory record
            from_disk = (await c.get(f"{API}/jobs/{launch['job_id']}", headers=HDR)).json()
            listed = (await c.get(f"{API}/jobs", headers=HDR)).json()
        return launch, admitted, detail, from_disk, listed
    launch, admitted, detail, from_disk, listed = run(go())
    assert list(launch) == list(jobs.LAUNCH_FIELDS)
    assert launch == {k: admitted[k] for k in jobs.LAUNCH_FIELDS}
    assert launch["state"] == "RUNNING" and launch["job_id"].startswith("BTJ-")
    assert (launch["dataset"], launch["dataset_reused"], launch["steps"]) == \
        ("replay-db", False, ["replay"])
    assert launch["warnings"] == ["overlapping_prior_runs"]
    assert launch["prior_overlapping_runs"] == 1
    disk = _disk(env, launch["job_id"])
    for v in (detail, disk, from_disk):
        assert set(v) == set(jobs.PUBLIC_FIELDS)
        assert v["warnings"] == ["overlapping_prior_runs"] and v["prior_overlapping_runs"] == 1
        assert v["dataset_reused"] is False and v["steps"] == ["replay"]
        assert v["state"] == "SUCCEEDED" and v["requested_by"] == settings.ADMIN_API_KEY_LABEL
    assert [i["job_id"] for i in listed["items"]] == [launch["job_id"]]
    assert listed["running_job_id"] is None and listed["max_concurrent_jobs"] == 1
    assert not any(k in disk for k in ("url", "target", "proc", "dataset_dir", "final_path"))


def test_persisted_reads_running_on_disk_404_and_malformed(env):
    rec = _rec(env)
    (env.ds_dir / "jobs").mkdir(parents=True)
    jobs._persist(rec)                                   # a RUNNING record, owner gone
    (env.ds_dir / "jobs" / "BTJ-badbadbadbad.json").write_text("{not json", encoding="utf-8")

    async def go():
        async with _client() as c:
            return [await c.get(f"{API}/jobs/{rec.job_id}", headers=HDR),
                    await c.get(f"{API}/jobs/BTJ-000000000000", headers=HDR),
                    await c.get(f"{API}/jobs/BTJ-badbadbadbad", headers=HDR),
                    await c.get(f"{API}/jobs/BTJ-XYZ", headers=HDR),
                    await c.get(f"{API}/jobs", headers=HDR)]
    one, absent, malformed, bad_id, listed = run(go())
    assert one.status_code == 200 and one.json()["state"] == "CANCELLED"
    assert absent.status_code == 404 and absent.json()["detail"] == "job 'BTJ-000000000000' not found"
    assert malformed.status_code == 404
    assert bad_id.status_code == 400
    assert [(i["job_id"], i["state"]) for i in listed.json()["items"]] == [(rec.job_id, "CANCELLED")]


# ── overlap warning ──────────────────────────────────────────────────────────

def test_count_overlaps_uses_the_engine_hash(env, monkeypatch):
    from app.services.replay import engine

    p, extra, _ = engine.resolve_params({})
    _, h = engine.params_record(p, engine.run_config_for(p, extra, ("NIFTY",)))
    store = _make_store(env.tmp / "other" / "replay.db", _row("RPL-SAMEHASH", params_hash=h))
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", _url(store))
    target = replay_store.resolve("replay-db")

    def count(**kw):
        return run(jobs.count_overlaps(target, req("replay_db", **kw), ["NIFTY"]))
    assert count(overrides={}) == 0
    assert count(overrides={"capital": 15000.0}) == 1
    assert count(overrides={"capital": 15000.0}, date_from="2026-11-02",
                 date_to="2026-11-06") == 0
    assert jobs.overlap_warning(1, "") is True
    assert jobs.overlap_warning(1, "   \n") is True
    assert jobs.overlap_warning(1, "tuned capital") is False
    assert jobs.overlap_warning(0, "") is False


# ── concurrency ──────────────────────────────────────────────────────────────

def _held_spawn(monkeypatch):
    hold, calls = threading.Event(), []

    def fake(rec):
        calls.append(rec.job_id)
        hold.wait(10)
        rec.finish("SUCCEEDED", step="replay")
    monkeypatch.setattr(jobs, "_spawn", fake)
    return hold, calls


def test_concurrent_launches_admit_exactly_one(env, monkeypatch):
    hold, calls = _held_spawn(monkeypatch)

    async def yielding_count(*a, **k):
        await asyncio.sleep(0)               # both requests pass step 0 before either admits
        return 0
    monkeypatch.setattr(jobs, "count_overlaps", yielding_count)

    async def go():
        results = await asyncio.gather(jobs.launch(req(), "admin"), jobs.launch(req(), "admin"),
                                       return_exceptions=True)
        ok = [r for r in results if isinstance(r, dict)]
        rejected = [r for r in results if isinstance(r, HTTPException)]
        assert len(ok) == 1 and len(rejected) == 1, results
        assert rejected[0].status_code == 409
        assert rejected[0].detail == {"code": "JOB_RUNNING", "job_id": ok[0]["job_id"]}
        await _until(lambda: calls)
        await asyncio.sleep(0.2)
        assert calls == [ok[0]["job_id"]]                    # exactly one _spawn
        staged = sorted(p.name for p in (env.ds_dir / "jobs").iterdir() if p.is_dir())
        assert staged == [ok[0]["job_id"]]                   # the 409'd request staged nothing
        async with _client() as c:
            r = await c.get(f"{API}/jobs/{ok[0]['job_id']}", headers=HDR)
        assert r.status_code == 200 and r.json()["state"] == "RUNNING"   # before release
        # A sequential pair is kept for contrast. It would pass even against a racy
        # lock-checked-in-the-handler design, so it is not the real test.
        with pytest.raises(HTTPException) as e:
            await jobs.launch(req(), "admin")
        assert e.value.status_code == 409
        hold.set()
        await _settle()
    run(go())
    assert jobs._running is None


def test_a_staging_failure_leaves_the_slot_free(env, monkeypatch):
    monkeypatch.setattr(jobs, "_spawn", lambda rec: rec.finish("SUCCEEDED", step="replay"))
    real_stage, attempts = jobs._stage_params, []

    def fails_once(rec):
        attempts.append(rec.job_id)
        if len(attempts) == 1:
            raise OSError("disk full")
        real_stage(rec)
    monkeypatch.setattr(jobs, "_stage_params", fails_once)

    async def go():
        async with _client() as c:
            first = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            assert first.status_code == 500 and first.json() == {"detail": "could not stage the run"}
            assert jobs._running is None and list(jobs._ring) == []
            second = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            assert second.status_code == 202, second.text
            await _settle()
    run(go())


def test_the_fast_path_answers_409_before_any_store_access(env, monkeypatch):
    hold, calls = _held_spawn(monkeypatch)
    counted = []
    real_count = jobs.count_overlaps

    async def spy(*a, **k):
        counted.append(1)
        return await real_count(*a, **k)
    monkeypatch.setattr(jobs, "count_overlaps", spy)

    async def go():
        async with _client() as c:
            first = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            assert first.status_code == 202
            jid = first.json()["job_id"]
            counted.clear()
            second = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            assert second.status_code == 409
            assert second.json()["detail"] == {"code": "JOB_RUNNING", "job_id": jid}
            assert counted == []
            assert sorted(p.name for p in (env.ds_dir / "jobs").iterdir()) == [jid]

            async def unreachable(*a, **k):
                raise HTTPException(status_code=503, detail="replay store 'x' unreachable")
            monkeypatch.setattr(jobs, "count_overlaps", unreachable)
            third = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            assert third.status_code == 409
            hold.set()
            await _settle()
    run(go())


# ── cancellation precedence (a)–(i) ──────────────────────────────────────────

def test_a_lifespan_shutdown_persists_cancelled_never_failed(env, sqlite_db, fake_redis,
                                                             lifespan_spies, monkeypatch):
    from starlette.testclient import TestClient

    from app.main import app
    from tests.helpers import seed_config

    run(seed_config(sqlite_db, max_lots_per_trade=2, max_daily_loss=500.0))
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)
    sp = Spawner({"block": True})
    monkeypatch.setattr(jobs, "_popen", sp)
    real_shutdown, real_dispose = jobs.shutdown, replay_store.dispose_all

    async def spy_shutdown(*a, **k):
        lifespan_spies.append("jobs.shutdown")
        await real_shutdown(*a, **k)
        lifespan_spies.append("jobs.shutdown.done")

    async def spy_dispose():
        lifespan_spies.append("replay_store.dispose_all")
        await real_dispose()
    monkeypatch.setattr(jobs, "shutdown", spy_shutdown)
    monkeypatch.setattr(replay_store, "dispose_all", spy_dispose)

    with TestClient(app) as c:
        r = c.post(f"{API}/jobs", json=body("replay_db"), headers=HDR)
        assert r.status_code == 202, r.text
        jid = r.json()["job_id"]
        assert sp.started.wait(10)
        assert c.get(f"{API}/jobs/{jid}", headers=HDR).json()["state"] == "RUNNING"
    disk = _disk(env, jid)                  # the persisted file, not the in-memory record
    assert (disk["state"], disk["step"]) == ("CANCELLED", "replay")
    assert disk["finished_at"] is not None
    assert sp.calls[0].killed.is_set()
    order = list(lifespan_spies)
    assert order.index("jobs.shutdown.done") < order.index("replay_store.dispose_all") \
        < order.index("redis.disconnect")


def test_b_shutdown_during_the_dataset_step(env, monkeypatch):
    """Also dataset staging (iii)."""
    sp = Spawner(gen(block=True))
    monkeypatch.setattr(jobs, "_popen", sp)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body(), headers=HDR)
        assert r.status_code == 202
        await _until(sp.started.is_set)
        await jobs.shutdown(grace_s=5.0)
        return r.json()["job_id"]
    jid = run(go())
    disk = _disk(env, jid)
    assert (disk["state"], disk["step"]) == ("CANCELLED", "dataset")
    assert len(sp.calls) == 1                                  # the replay step never started
    assert _staging(env) == [] and not env.final.exists()


def test_c_a_terminal_record_is_not_overwritten_by_shutdown(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)
    hold = threading.Event()

    def timed_out(rec):
        rec.finish("TIMEOUT", step="replay", timed_out=True)
        hold.wait(10)
    monkeypatch.setattr(jobs, "_spawn", timed_out)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body("replay_db"), headers=HDR)
        rec = jobs._running
        await _until(lambda: rec.state == "TIMEOUT")
        sd = asyncio.create_task(jobs.shutdown(grace_s=5.0))
        await _until(lambda: rec.cancel_requested)
        hold.set()
        await sd
        return r.json()["job_id"]
    disk = _disk(env, run(go()))
    assert (disk["state"], disk["step"], disk["timed_out"]) == ("TIMEOUT", "replay", True)


def test_d_cancel_between_popen_and_proc_assignment(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)

    def flag(argv):
        jobs._running.cancel_requested = True   # shutdown() lands before rec.proc = proc
    sp = Spawner({"block": True, "on_start": flag})
    monkeypatch.setattr(jobs, "_popen", sp)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body("replay_db"), headers=HDR)
        await _settle()
        return r.json()["job_id"]
    disk = _disk(env, run(go()))
    assert sp.calls[0].killed.is_set()
    assert (disk["state"], disk["step"], disk["exit_code"]) == ("CANCELLED", "replay", 1)


def test_e_shutdown_between_the_steps(env, monkeypatch):
    from app.services.replay.cli import create_sqlite_schema

    between, release = threading.Event(), threading.Event()

    def dataset_step(rec, final):
        create_sqlite_schema(_url(final))
        between.set()
        release.wait(10)
        return True
    monkeypatch.setattr(jobs, "_dataset_step", dataset_step)
    sp = Spawner()
    monkeypatch.setattr(jobs, "_popen", sp)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body(), headers=HDR)
        rec = jobs._running
        await _until(between.is_set)
        sd = asyncio.create_task(jobs.shutdown(grace_s=5.0))
        await _until(lambda: rec.cancel_requested)
        release.set()
        await sd
        return r.json()["job_id"]
    disk = _disk(env, run(go()))
    assert (disk["state"], disk["step"]) == ("CANCELLED", "replay")
    assert sp.calls == []                                      # zero replay spawns
    assert env.final.is_file()                                 # the complete dataset stays


def test_f_a_child_ignoring_kill_is_abandoned_within_the_grace(env, monkeypatch, caplog):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)
    sp = Spawner({"block": True, "ignore_kill_s": 5.0})
    monkeypatch.setattr(jobs, "_popen", sp)
    caplog.set_level(logging.ERROR, logger="app.services.backtest.jobs")

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body("replay_db"), headers=HDR)
        await _until(sp.started.is_set)
        task = next(iter(jobs._tasks))
        t0 = time.monotonic()
        await jobs.shutdown(grace_s=1.0)
        took = time.monotonic() - t0
        await asyncio.wait({task}, timeout=max(0.0, 2.0 - (time.monotonic() - t0)))
        return r.json()["job_id"], took, task.done(), time.monotonic() - t0
    jid, took, done, total = run(go())
    assert took < 2.0 and done and total < 2.0
    assert _disk(env, jid)["state"] == "CANCELLED"
    pid = str(sp.calls[0].pid)
    assert [r for r in caplog.records if r.levelno == logging.ERROR and pid in r.getMessage()]


def test_g_shutdown_without_a_job_and_a_failing_persist(env, monkeypatch, caplog):
    run(jobs.shutdown())
    assert not (env.ds_dir / "jobs").exists()
    rec = _rec(env)
    jobs._running = rec

    def boom(r):
        raise OSError("read-only volume")
    monkeypatch.setattr(jobs, "_persist", boom)
    caplog.set_level(logging.ERROR, logger="app.services.backtest.jobs")
    run(jobs.shutdown(grace_s=0.1))                             # does not propagate
    assert rec.state == "CANCELLED"
    assert any(r.getMessage() == "backtest job shutdown failed" for r in caplog.records)


_HARNESS = r'''
import ctypes, json, subprocess, sys, time
flags, out = int(sys.argv[1]), sys.argv[2]
sleeper = [sys.executable, "-c", "import asyncio; asyncio.run(asyncio.sleep(30))"]
kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
isolated = subprocess.Popen(sleeper, creationflags=flags, **kw)
control = subprocess.Popen(sleeper, **kw)          # no flags: shares this console
time.sleep(1.5)
k32 = ctypes.windll.kernel32
k32.SetConsoleCtrlHandler(None, True)              # this harness ignores the event itself
k32.GenerateConsoleCtrlEvent(0, 0)                 # CTRL_C_EVENT to every process on the console
time.sleep(2.0)
res = {"isolated_alive": isolated.poll() is None, "control_rc": control.poll()}
isolated.kill()
control.kill()
_, err = isolated.communicate(timeout=10)
control.communicate(timeout=10)
res["isolated_kbi"] = "KeyboardInterrupt" in err
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(res))
'''


@pytest.mark.skipif(os.name != "nt", reason="Windows console control events")
def test_i_a_console_ctrl_c_never_reaches_the_child(tmp_path):
    """Automates evidence r8 in a hidden console, so the event never reaches pytest."""
    script, out = tmp_path / "harness.py", tmp_path / "result.json"
    script.write_text(_HARNESS, encoding="utf-8")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                                         # SW_HIDE
    flags = jobs._child_spawn_kwargs()["creationflags"]
    subprocess.run([sys.executable, str(script), str(flags), str(out)],
                   creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si, timeout=60)
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["control_rc"] is not None          # the event was delivered on that console
    assert res["isolated_alive"] is True          # ...and the isolated child survived it
    assert res["isolated_kbi"] is False


# ── dataset staging (i)–(vii) ────────────────────────────────────────────────

def _launch_and_settle(*bodies):
    async def go():
        out = []
        async with _client() as c:
            for b in bodies:
                r = await c.post(f"{API}/jobs", json=b, headers=HDR)
                assert r.status_code == 202, r.text
                await _settle()
                out.append((r.json(), (await c.get(f"{API}/jobs/{r.json()['job_id']}",
                                                   headers=HDR)).json()))
        return out
    return run(go())


def test_staging_i_a_failed_generator_leaves_no_final_name(env, monkeypatch):
    sp = Spawner(gen(rc=1, valid=False, err=["Traceback: boom"]), gen(), REPLAY_OK)
    monkeypatch.setattr(jobs, "_popen", sp)
    [(first, rec)] = _launch_and_settle(body())
    assert (rec["state"], rec["step"]) == ("FAILED", "dataset")
    assert rec["stderr_tail"] == ["Traceback: boom"]
    assert len(sp.calls) == 1                                  # replay never invoked
    assert _staging(env) == [] and not env.final.exists()
    [(retry, _)] = _launch_and_settle(body())                  # same (days, seed, start)
    assert (retry["dataset_reused"], retry["steps"]) == (False, ["dataset", "replay"])


def test_staging_ii_a_generator_timeout(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATASET_TIMEOUT_S", 0)
    sp = Spawner(gen(block=True))
    monkeypatch.setattr(jobs, "_popen", sp)
    [(_, rec)] = _launch_and_settle(body())
    assert (rec["state"], rec["step"], rec["timed_out"]) == ("TIMEOUT", "dataset", True)
    assert len(sp.calls) == 1 and _staging(env) == [] and not env.final.exists()


def test_staging_iv_os_replace_raising_is_error(env, monkeypatch):
    real_replace = os.replace

    def flaky(src, dst):
        if ".staging" in str(src):
            raise OSError("cross-device")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    sp = Spawner(gen())
    monkeypatch.setattr(jobs, "_popen", sp)
    [(_, rec)] = _launch_and_settle(body())
    assert (rec["state"], rec["step"], rec["error"]) == ("ERROR", "dataset", "OSError")
    assert len(sp.calls) == 1 and _staging(env) == [] and not env.final.exists()


def test_staging_v_an_unremovable_staging_file_is_swept_next_time(env, monkeypatch, caplog):
    real_unlink = Path.unlink
    blocked = {"on": True}

    def flaky(self, missing_ok=False):
        if blocked["on"] and ".staging" in self.parts:
            raise PermissionError("handle still held")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", flaky)
    sp = Spawner(gen(rc=1, valid=False))
    monkeypatch.setattr(jobs, "_popen", sp)
    caplog.set_level(logging.WARNING, logger="app.services.backtest.jobs")
    [(first, rec)] = _launch_and_settle(body())
    assert (rec["state"], rec["step"]) == ("FAILED", "dataset")      # state unchanged
    assert any("could not remove staging file" in r.getMessage() for r in caplog.records)
    assert _staging(env) == [f"{first['job_id']}.db"] and not env.final.exists()
    blocked["on"] = False
    sp.scripts = [gen(), REPLAY_OK]
    [(second, rec2)] = _launch_and_settle(body())
    assert rec2["state"] == "SUCCEEDED"
    assert _staging(env) == [] and env.final.is_file()               # the orphan was swept


def test_staging_vi_success_publishes_and_the_next_launch_reuses(env, monkeypatch):
    sp = Spawner(gen(), REPLAY_OK, REPLAY_OK)
    monkeypatch.setattr(jobs, "_popen", sp)
    [(first, rec), (second, rec2)] = _launch_and_settle(body(), body())
    assert rec["state"] == "SUCCEEDED" and env.final.is_file() and _staging(env) == []
    assert (first["dataset_reused"], first["steps"]) == (False, ["dataset", "replay"])
    assert (second["dataset_reused"], second["steps"]) == (True, ["replay"])
    assert second["dataset"] == SID and rec2["state"] == "SUCCEEDED"
    assert len(sp.calls) == 3
    sep = os.sep
    assert rec["stdout_tail"][0] == (f"SYNTHETIC replay database written: "
                                     f"<datasets>{sep}.staging{sep}{first['job_id']}.db")


def test_staging_vii_a_half_built_dataset_is_never_listed(env, monkeypatch):
    release = threading.Event()
    sp = Spawner(gen(rc=1, release=release))
    monkeypatch.setattr(jobs, "_popen", sp)

    async def go():
        async with _client() as c:
            r = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            assert r.status_code == 202
            await _until(lambda: _staging(env))
            ds = await c.get(f"{API}/datasets")
            runs = await c.get(f"{API}/runs")
            release.set()
            await _settle()
        return ds, runs
    ds, runs = run(go())
    assert ds.status_code == 200 and SID not in [i["dataset_id"] for i in ds.json()["items"]]
    assert runs.status_code == 200


def test_a_popen_failure_is_error_with_a_redacted_message(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)

    def cannot(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "C:\\Python\\python.exe")
    monkeypatch.setattr(jobs, "_popen", cannot)
    [(_, rec)] = _launch_and_settle(body("replay_db"))
    assert (rec["state"], rec["step"]) == ("ERROR", "replay")
    assert rec["error"].startswith("FileNotFoundError") and "C:\\" not in rec["error"]


def test_a_replay_timeout_keeps_the_partial_tails(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)
    monkeypatch.setattr(settings, "REPLAY_JOB_TIMEOUT_S", 0)
    sp = Spawner({"block": True, "out": ["Coverage: {}"], "err": ["warming up"]})
    monkeypatch.setattr(jobs, "_popen", sp)
    [(_, rec)] = _launch_and_settle(body("replay_db"))
    assert (rec["state"], rec["step"], rec["timed_out"]) == ("TIMEOUT", "replay", True)
    assert rec["stdout_tail"] == ["Coverage: {}"] and rec["stderr_tail"] == ["warming up"]
    assert sp.calls[0].killed.is_set()


def test_no_data_resolves_the_run_by_store_scan(env, monkeypatch):
    store = _make_store(
        env.tmp / "other" / "replay.db",
        _row("RPL-OLDWINDOW", status="COMPLETE", date_from=date(2026, 8, 1),
             date_to=date(2026, 8, 5), created_at=datetime(2026, 10, 2, tzinfo=timezone.utc)),
        _row("RPL-NODATA1", status="NO_DATA", date_from=date(2026, 9, 1),
             date_to=date(2026, 9, 7), created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", _url(store))
    sp = Spawner({"rc": 2, "out": ["Coverage: {}", "status=NO_DATA exit=2"]})
    monkeypatch.setattr(jobs, "_popen", sp)
    [(_, rec)] = _launch_and_settle(body("replay_db"))
    assert (rec["state"], rec["exit_code"]) == ("NO_DATA", 2)
    assert (rec["run_id"], rec["run_id_source"]) == ("RPL-NODATA1", "store_scan")


def test_an_argparse_exit_2_is_invalid_not_no_data(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)
    sp = Spawner({"rc": 2, "err": ["cli: error: argument --label: expected one argument"]})
    monkeypatch.setattr(jobs, "_popen", sp)
    [(_, rec)] = _launch_and_settle(body("replay_db"))
    assert rec["state"] == "INVALID" and rec["run_id"] is None
    assert rec["stderr_tail"] == ["cli: error: argument --label: expected one argument"]


# ── replay-db final-name refusal ─────────────────────────────────────────────

def test_replay_db_naming_an_ungenerated_synthetic_file_is_refused(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", _url(env.final))
    spawned = []
    monkeypatch.setattr(jobs, "_spawn", lambda rec: (spawned.append(rec.job_id),
                                                     rec.finish("SUCCEEDED", step="replay")))

    async def go():
        async with _client() as c:
            opts = (await c.get(f"{API}/launch-options")).json()
            refused = await c.post(f"{API}/jobs", json=body("replay_db"), headers=HDR)
            state = (jobs._running, list(env.ds_dir.glob("synthetic-*.db")),
                     (env.ds_dir / "jobs").exists(), list(spawned))
            synthetic = await c.post(f"{API}/jobs", json=body(), headers=HDR)
            await _settle()
        return opts, refused, state, synthetic
    opts, refused, (running, globbed, jobs_dir, spawned_then), synthetic = run(go())
    replay_db, template = opts["datasets"]
    assert replay_db == {"dataset_id": "replay-db", "kind": "replay_db",
                         "label": "Replay database (configured)", "synthetic": False,
                         "alias_of": None, "exists": False, "launchable": False}
    assert (template["alias_of"], template["exists"], template["launchable"]) == \
        (None, None, True)
    assert refused.status_code == 400 and refused.json() == {"detail": "replay_store_not_initialised"}
    assert running is None and globbed == [] and jobs_dir is False and spawned_then == []
    assert synthetic.status_code == 202 and synthetic.json()["dataset_reused"] is False
    assert not env.final.exists()


@pytest.mark.parametrize("where", ["outside", "non-synthetic-name"])
def test_replay_db_refusal_is_not_over_broad(env, monkeypatch, where):
    target = (env.tmp / "other" / f"{SID}.db" if where == "outside"
              else env.ds_dir / "replay.db")
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", _url(target))
    monkeypatch.setattr(jobs, "_spawn", lambda rec: rec.finish("SUCCEEDED", step="replay"))
    [(launch, rec)] = _launch_and_settle(body("replay_db"))
    assert launch["dataset"] == "replay-db" and rec["state"] == "SUCCEEDED"


def test_the_pre_popen_recheck_refuses(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", env.outside)
    calls = []

    def flips(url):
        calls.append(url)
        return len(calls) > 1                  # allowed at resolution, deleted before spawn
    monkeypatch.setattr(jobs, "would_create_final_name", flips)
    sp = Spawner()
    monkeypatch.setattr(jobs, "_popen", sp)
    [(_, rec)] = _launch_and_settle(body("replay_db"))
    assert (rec["state"], rec["step"], rec["error"]) == \
        ("REFUSED", "replay", "replay_store_not_initialised")
    assert sp.calls == [] and len(calls) == 2


def test_a_replay_db_launch_into_an_aliased_synthetic_file(env, monkeypatch):
    _make_store(env.final)
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", _url(env.final))
    sp = Spawner(REPLAY_OK)
    monkeypatch.setattr(jobs, "_popen", sp)

    async def opts():
        async with _client() as c:
            return (await c.get(f"{API}/launch-options")).json()["datasets"][0]
    entry = run(opts())
    assert (entry["synthetic"], entry["alias_of"], entry["exists"], entry["launchable"]) == \
        (True, SID, True, True)
    [(launch, rec)] = _launch_and_settle(body("replay_db", label="plain"))
    assert (launch["dataset"], launch["label"], launch["steps"]) == \
        (SID, "plain (synthetic)", ["replay"])
    argv = sp.calls[0].argv
    assert "--synthetic" in argv and "--label=plain (synthetic)" in argv
    db = argv[argv.index("--database-url") + 1]
    assert replay_store._url_sqlite_path(db).resolve() == env.final.resolve()


# ── routes: guards, body, enforcement, launch-options ────────────────────────

def _calls(*specs):
    async def go():
        async with _client() as c:
            return [await c.request(m, f"{API}{p}", **kw) for m, p, kw in specs]
    return run(go())


def test_admin_key_guards(env, monkeypatch):
    monkeypatch.setattr(jobs, "_spawn", lambda rec: rec.finish("SUCCEEDED", step="replay"))
    no_key, wrong, v_no_key, list_no_key, list_key = _calls(
        ("POST", "/jobs", {"json": body()}),
        ("POST", "/jobs", {"json": body(), "headers": {"X-API-Key": "nope"}}),
        ("POST", "/params/validate", {"json": {"overrides": {}}}),
        ("GET", "/jobs", {}),
        ("GET", "/jobs", {"headers": HDR}))
    assert no_key.status_code == wrong.status_code == v_no_key.status_code == 401
    assert list_no_key.status_code == 401 and list_key.status_code == 200
    monkeypatch.setattr(settings, "ADMIN_API_KEY", None)
    unset, v_unset, list_unset, opts = _calls(
        ("POST", "/jobs", {"json": body(), "headers": HDR}),
        ("POST", "/params/validate", {"json": {"overrides": {}}, "headers": HDR}),
        ("GET", "/jobs", {}),
        ("GET", "/launch-options", {}))
    assert unset.status_code == v_unset.status_code == 503
    assert unset.json() == {"detail": "admin key not configured"}
    assert list_unset.status_code == 200
    assert opts.json()["admin_key_configured"] is False
    assert jobs._running is None and list(jobs._ring) == []


def test_a_database_url_in_the_body_is_422(env, monkeypatch):
    monkeypatch.setattr(jobs, "_spawn", lambda rec: rec.finish("SUCCEEDED", step="replay"))
    top, nested, kind = _calls(
        ("POST", "/jobs", {"json": {**body(), "database_url": "sqlite+aiosqlite:///x.db"},
                           "headers": HDR}),
        ("POST", "/jobs", {"json": body(dataset={"kind": "replay_db",
                                                 "database_url": "postgresql://x/y"}),
                           "headers": HDR}),
        ("POST", "/jobs", {"json": body(dataset={"kind": "postgres"}), "headers": HDR}))
    assert top.status_code == nested.status_code == kind.status_code == 422
    assert jobs._running is None and not (env.ds_dir / "jobs").exists()


@pytest.mark.parametrize("change, needle", [
    ({"date_from": "2026-09-08"}, "date_from must be <= date_to"),
    ({"date_to": "2026-12-01"}, "90 days (BACKTEST_MAX_LAUNCH_RANGE_DAYS)"),
    ({"underlyings": ["NIFTY", "FINNIFTY"]}, "underlyings"),
    ({"underlyings": []}, "underlyings"),
    ({"oos_split": 0.4}, "oos_split"),
    ({"oos_split": 0.96}, "oos_split"),
    ({"bootstrap_iterations": 999}, "bootstrap_iterations"),
    ({"bootstrap_iterations": 20001}, "bootstrap_iterations"),
    ({"label": "x" * 121}, "label"),
    ({"label": "a\nb"}, "label"),
    ({"notes": "bell\x07"}, "notes"),
    ({"seed": -1}, "seed"),
    ({"seed": 2**31}, "seed"),
    ({"dataset": {"kind": "synthetic", "days": 4, "seed": 1, "start": "2026-09-01"}},
     "dataset.days"),
    ({"dataset": {"kind": "synthetic", "days": 61, "seed": 1, "start": "2026-09-01"}},
     "BACKTEST_SYNTHETIC_MAX_DAYS"),
    ({"dataset": {"kind": "synthetic", "days": 5, "seed": 1, "start": "2026-09-05"}},
     "weekday"),
    ({"dataset": {"kind": "synthetic", "days": 5, "start": "2026-09-01"}}, "required"),
    ({"overrides": {"partial_exit_enabled": True, "max_lots_per_trade": 1}}, "PARAM_VIOLATIONS"),
    ({"overrides": {"not_a_knob": 1}}, "PARAM_VIOLATIONS"),
    ({"dataset": {"kind": "replay_db"}}, "replay_store_not_configured"),
])
def test_range_and_parameter_enforcement_is_400(env, monkeypatch, change, needle):
    spawned = []
    monkeypatch.setattr(jobs, "_spawn", lambda rec: spawned.append(rec))
    [r] = _calls(("POST", "/jobs", {"json": body(**change), "headers": HDR}))
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]
    assert jobs._running is None and spawned == [] and not (env.ds_dir / "jobs").exists()


def test_the_effective_range_limit_names_its_source(env, monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_MAX_RANGE_DAYS", 30)
    [r, opts] = _calls(("POST", "/jobs", {"json": body(date_to="2026-10-15"), "headers": HDR}),
                       ("GET", "/launch-options", {}))
    assert r.status_code == 400 and r.json()["detail"] == \
        "date range must be <= 30 days (REPLAY_MAX_RANGE_DAYS)"
    assert opts.json()["limits"]["max_launch_range_days"] == 30


def test_params_validate_returns_the_full_list_at_200(env):
    bad, good = _calls(
        ("POST", "/params/validate", {"json": {"overrides": {"partial_exit_enabled": True,
                                                             "max_lots_per_trade": 1}},
                                      "headers": HDR}),
        ("POST", "/params/validate", {"json": {"overrides": {"capital": 15000.0}},
                                      "headers": HDR}))
    assert bad.status_code == 200 and bad.json()["valid"] is False
    assert any("max_lots_per_trade" in v for v in bad.json()["violations"])
    assert good.json() == {"valid": True, "violations": []}


def test_launch_options_shape(env):
    [r] = _calls(("GET", "/launch-options", {}))
    o = r.json()
    assert r.status_code == 200 and o["admin_key_configured"] is True
    assert [d["dataset_id"] for d in o["datasets"]] == ["synthetic"]   # REPLAY_DATABASE_URL unset
    assert o["limits"] == {"max_range_days": settings.REPLAY_MAX_RANGE_DAYS,
                           "max_launch_range_days": min(settings.REPLAY_MAX_RANGE_DAYS,
                                                        settings.BACKTEST_MAX_LAUNCH_RANGE_DAYS),
                           "synthetic_min_days": 5,
                           "synthetic_max_days": settings.BACKTEST_SYNTHETIC_MAX_DAYS,
                           "job_timeout_s": settings.REPLAY_JOB_TIMEOUT_S,
                           "bootstrap_iterations": {"min": 1000, "max": 20000, "default": 10000},
                           "oos_split": {"min": 0.5, "max": 0.95, "default": 0.7}}
    assert tuple(o["defaults"]["overrides"]) == jobs.DEFAULT_OVERRIDE_KEYS
    assert o["defaults"]["overrides"]["capital"] == 200000.0
    assert o["defaults"]["overrides"]["max_hold_minutes"] == \
        {"DEFAULT": 45, "RSI_VWAP": 25, "VWAP_PULLBACK": 25, "VWAP_REVERSAL": 25,
         "TREND_ENGINE": 60}
    assert o["defaults"]["seed"] == settings.REPLAY_DEFAULT_SEED
    assert o["underlyings"] == ["NIFTY", "BANKNIFTY"]
    assert (o["running_job_id"], o["max_concurrent_jobs"]) == (None, 1)


# ── the one real child process ───────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("BACKTEST_SKIP_SUBPROCESS") == "1",
                    reason="BACKTEST_SKIP_SUBPROCESS=1 (constrained CI)")
def test_one_real_child_process_through_popen_in_a_thread(env):
    rec = _rec(env)
    rc = run(asyncio.to_thread(jobs._run_child, rec,
                               [sys.executable, "-c", "print('run_id=RPL-REALCHILD1 trades=0')"],
                               timeout=60, step="replay"))
    assert rc == 0 and rec.state == "SUCCEEDED" and rec.exit_code == 0
    assert list(rec.stdout_tail) == ["run_id=RPL-REALCHILD1 trades=0"]
    # Popen -> list2cmdline -> the child's argv, for the --label= / --notes= elements
    label = 'he said "go" \\'
    rec2 = _rec(env, label=label, notes="two words")
    elems = [a for a in jobs.replay_argv(rec2) if a.startswith(("--label=", "--notes="))]
    assert elems == [f"--label={label}", "--notes=two words"]
    rc = jobs._run_child(rec2, [sys.executable, "-c",
                                "import sys, json; print(json.dumps(sys.argv[1:]))", *elems],
                         timeout=60, step="replay")
    assert rc == 0 and json.loads(rec2.stdout_tail[-1]) == elems
