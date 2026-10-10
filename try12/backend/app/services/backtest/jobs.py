"""
Replay job manager (backtest design §4.7, §4.8) behind POST /api/v1/backtest/jobs.

The module-level state below is the job manager: ``_running`` (the one admitted
job), ``_ring`` (recent records, newest first) and ``_tasks`` (strong refs to the
background tasks). At most one job runs; admission is a synchronous
check-and-set (``_admit``) so two requests in the same event-loop tick cannot
both start a child.

A job runs up to two child processes from a worker thread (blocking ``Popen``
inside ``asyncio.to_thread``, which works under every event-loop policy): the
synthetic dataset generator when the dataset is new, then
``python -m app.services.replay.cli``. ``_run_child`` is the only spawn site.
The generator writes only ``<dataset_dir>/.staging/<job_id>.db``; ``os.replace``
is the only producer of a final ``synthetic-*.db`` name.

Nothing here opens the application database: the replay target comes from the
registry (app.db.replay_store) and is checked by ``assert_not_main_db`` and
``would_create_final_name`` immediately before every replay spawn. No response
carries a URL, path or secret: the record's internal fields are excluded from
serialisation and the child output tails are redacted at capture time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, get_args

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from sqlalchemy import desc, func, select

from app.core.config import settings
from app.core.trading_params import TradingParams, validate_overrides
from app.db import replay_store
from app.db.models.replay import ReplayRun
from app.db.replay_store import (
    BACKEND_DIR, Dataset, ReplayTargetRefused, _sqlite_url, _url_sqlite_path,
    assert_not_main_db, resolve_dataset_dir, synthetic_id, would_create_final_name,
)

log = logging.getLogger(__name__)

JobState = Literal["RUNNING", "SUCCEEDED", "NO_DATA", "FAILED", "INVALID", "TIMEOUT",
                   "REFUSED", "ERROR", "CANCELLED"]
TERMINAL = frozenset(get_args(JobState)) - {"RUNNING"}
Step = Literal["dataset", "replay"]

JOB_ID_RE = re.compile(r"^BTJ-[0-9a-f]{12}$")
RUN_ID_RE = re.compile(r"run_id=(RPL-[A-Za-z0-9]{1,26})")
NO_DATA_LINE = "status=NO_DATA exit=2"          # cli.py's final line on the NO_DATA path

UNDERLYINGS = ("NIFTY", "BANKNIFTY")
MAX_CONCURRENT_JOBS = 1
SHUTDOWN_GRACE_S = 5.0     # module constant, not a setting: inside Docker's default 10 s stop
_WAIT_POLL_S = 0.25
_KILL_GRACE_S = 5.0        # the budget after a timeout kill before the child is abandoned
TAIL_LINES = 200
TAIL_LINE_CHARS = 500
TAIL_STREAM_CHARS = 32 * 1024

LABEL_MAX, NOTES_MAX = 120, 2000
OOS_MIN, OOS_MAX, OOS_DEFAULT = 0.5, 0.95, 0.70
BOOT_MIN, BOOT_MAX, BOOT_DEFAULT = 1000, 20000, 10_000
SEED_MAX = 2**31 - 1
SYNTHETIC_MIN_DAYS = 5
START_MIN, START_MAX = date(2000, 1, 1), date(2100, 1, 1)
PARAM_VIOLATIONS = "PARAM_VIOLATIONS"
NOT_CONFIGURED = "replay_store_not_configured"
NOT_INITIALISED = "replay_store_not_initialised"
REFUSED_MAIN_DB = "replay target refused: application database"

# the launcher form's twelve knobs, pre-filled from TradingParams() defaults (§6.4)
DEFAULT_OVERRIDE_KEYS = (
    "capital", "fixed_fraction_pct", "kelly_fraction_cap_pct", "max_lots_per_trade",
    "partial_exit_enabled", "max_open_positions", "max_daily_loss", "max_daily_loss_pct",
    "max_trades_per_day", "open_exposure_cap_pct", "max_hold_minutes",
    "transition_cooldown_candles",
)
# Blanked (not removed) in the child's environment: the child's settings read
# backend/.env, so a removed variable would be refilled from that file.
_SCRUBBED_ENV = ("ADMIN_API_KEY", "ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD",
                 "ANGEL_TOTP_SECRET")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_tail() -> deque:
    return deque(maxlen=TAIL_LINES)


_finish_lock = threading.Lock()


# ── the record ───────────────────────────────────────────────────────────────

class JobRecord(BaseModel):
    """One launcher job. Mirrors types/backtest.ts JobRecord; the fields marked
    exclude=True are internal and never serialised (no URL, path or process)."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_id: str
    state: JobState = "RUNNING"
    step: Optional[Step] = None
    steps: list[Step]
    dataset: str
    dataset_reused: bool
    label: str
    notes: str = ""
    date_from: date
    date_to: date
    underlyings: list[str]
    seed: int
    oos_split: float
    overrides: dict[str, Any]
    requested_by: str
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    exit_code: Optional[int] = None
    run_id: Optional[str] = None
    run_id_source: Optional[Literal["stdout", "store_scan"]] = None
    stdout_tail: deque[str] = Field(default_factory=_new_tail)
    stderr_tail: deque[str] = Field(default_factory=_new_tail)
    error: Optional[str] = None
    timed_out: bool = False
    warnings: list[Literal["overlapping_prior_runs"]] = Field(default_factory=list)
    prior_overlapping_runs: int = 0

    # ── internal ──
    bootstrap_iterations: int = Field(BOOT_DEFAULT, exclude=True)
    url: Optional[str] = Field(None, exclude=True)              # the exact --database-url
    target: Any = Field(None, exclude=True)                     # the resolved Dataset
    final_path: Optional[Path] = Field(None, exclude=True)      # synthetic final path
    synth: Any = Field(None, exclude=True)                      # (days, seed, start)
    dataset_dir: Optional[Path] = Field(None, exclude=True)
    cancel_requested: bool = Field(False, exclude=True)
    cancel_deadline: Optional[float] = Field(None, exclude=True)
    proc: Any = Field(None, exclude=True)                       # the live Popen

    @field_serializer("stdout_tail", "stderr_tail")
    def _snapshot(self, v):
        return list(v)                     # atomic under the GIL while a drain thread appends

    def finish(self, state: str, *, step: str, error: Optional[str] = None,
               timed_out: bool = False) -> None:
        """The only terminal-state setter. The first terminal state wins; later calls are no-ops."""
        with _finish_lock:
            if self.state in TERMINAL:
                return
            self.state, self.step, self.error, self.timed_out = state, step, error, timed_out
            self.finished_at = utcnow()
            self.duration_s = ((self.finished_at - self.started_at).total_seconds()
                               if self.started_at else None)

    def view(self) -> dict:
        return self.model_dump(mode="json")

    def launch_view(self) -> dict:
        v = self.view()
        return {k: v[k] for k in LAUNCH_FIELDS}


PUBLIC_FIELDS = tuple(n for n, f in JobRecord.model_fields.items() if not f.exclude)
LAUNCH_FIELDS = ("job_id", "state", "dataset", "dataset_reused", "label", "created_at",
                 "steps", "warnings", "prior_overlapping_runs")


# ── requests ─────────────────────────────────────────────────────────────────

class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["replay_db", "synthetic"]
    days: Optional[int] = None
    seed: Optional[int] = None
    start: Optional[date] = None


class LaunchRequest(BaseModel):
    """No database_url field: the target is the server's choice (extra keys → 422)."""
    model_config = ConfigDict(extra="forbid")
    dataset: DatasetSpec
    date_from: date
    date_to: date
    underlyings: list[str]
    seed: Optional[int] = None
    oos_split: float = OOS_DEFAULT
    bootstrap_iterations: int = BOOT_DEFAULT
    label: str = ""
    notes: str = ""
    overrides: dict[str, Any] = Field(default_factory=dict)


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overrides: dict[str, Any] = Field(default_factory=dict)


# ── settings-derived values ──────────────────────────────────────────────────

def max_launch_range() -> tuple[int, str]:
    """(effective launch window limit in days, the setting that produced it)."""
    cli_max, ui_max = settings.REPLAY_MAX_RANGE_DAYS, settings.BACKTEST_MAX_LAUNCH_RANGE_DAYS
    return (ui_max, "BACKTEST_MAX_LAUNCH_RANGE_DAYS") if ui_max <= cli_max \
        else (cli_max, "REPLAY_MAX_RANGE_DAYS")


def artifact_write_dir() -> Path:
    """Where the child writes its artefact. Never None (the reader's artifact_dir()
    is None for a missing directory, which would become a literal 'None' argv)."""
    from app.services.replay.cli import default_artifact_dir
    return (Path(settings.REPLAY_ARTIFACT_DIR) if settings.REPLAY_ARTIFACT_DIR
            else default_artifact_dir())


# ── validation (§4.8, §8.1) ──────────────────────────────────────────────────

def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _has_control(s: str, allow: str = "") -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in allow for c in s)


def override_violations(overrides: dict) -> list[str]:
    """validate_overrides (module-level, the replay's own startup check), with a
    malformed value reported as a violation rather than a 500."""
    try:
        return validate_overrides(overrides)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        return [f"invalid TradingParams value: {type(exc).__name__}"]


def validate_request(req: LaunchRequest) -> list[str]:
    """400 naming the field and the limit on any violation; returns the
    normalised underlyings (upper-cased, de-duplicated, request order)."""
    if req.date_from > req.date_to:
        raise _bad("date_from must be <= date_to")
    limit, source = max_launch_range()
    if (req.date_to - req.date_from).days > limit:
        raise _bad(f"date range must be <= {limit} days ({source})")
    unds: list[str] = []
    for u in req.underlyings:
        u = (u or "").strip().upper()
        if u not in UNDERLYINGS:
            unds = []
            break
        if u not in unds:
            unds.append(u)
    if not unds:
        raise _bad(f"underlyings must be a non-empty subset of {', '.join(UNDERLYINGS)}")
    if not OOS_MIN <= req.oos_split <= OOS_MAX:
        raise _bad(f"oos_split must be between {OOS_MIN} and {OOS_MAX}")
    if not BOOT_MIN <= req.bootstrap_iterations <= BOOT_MAX:
        raise _bad(f"bootstrap_iterations must be between {BOOT_MIN} and {BOOT_MAX}")
    if len(req.label) > LABEL_MAX or _has_control(req.label):
        raise _bad(f"label must be at most {LABEL_MAX} characters with no control characters")
    if len(req.notes) > NOTES_MAX or _has_control(req.notes, allow="\n"):
        raise _bad(f"notes must be at most {NOTES_MAX} characters with no control "
                   "characters other than newline")
    if req.seed is not None and not 0 <= req.seed <= SEED_MAX:
        raise _bad(f"seed must be between 0 and {SEED_MAX}")
    spec = req.dataset
    if spec.kind == "synthetic":
        if spec.days is None or spec.seed is None or spec.start is None:
            raise _bad("dataset.days, dataset.seed and dataset.start are required "
                       "for a synthetic dataset")
        if not SYNTHETIC_MIN_DAYS <= spec.days <= settings.BACKTEST_SYNTHETIC_MAX_DAYS:
            raise _bad(f"dataset.days must be between {SYNTHETIC_MIN_DAYS} and "
                       f"{settings.BACKTEST_SYNTHETIC_MAX_DAYS} (BACKTEST_SYNTHETIC_MAX_DAYS)")
        if not 0 <= spec.seed <= SEED_MAX:
            raise _bad(f"dataset.seed must be between 0 and {SEED_MAX}")
        if not START_MIN <= spec.start <= START_MAX or spec.start.weekday() >= 5:
            raise _bad(f"dataset.start must be a weekday between {START_MIN.isoformat()} "
                       f"and {START_MAX.isoformat()}")
    if override_violations(req.overrides):
        raise _bad(PARAM_VIOLATIONS)        # the list comes from POST /params/validate
    return unds


# ── dataset resolution ───────────────────────────────────────────────────────

def replay_db_launchable(url: str) -> bool:
    """The predicate resolve_dataset refuses on (launch-options' `launchable`)."""
    return not would_create_final_name(url)


def _refuse_main_db(url: str) -> None:
    try:
        assert_not_main_db(url)
    except ReplayTargetRefused:
        log.error("backtest launch refused: the replay target is the application database")
        raise _bad(REFUSED_MAIN_DB)


def resolve_dataset(spec: DatasetSpec) -> tuple[Dataset, Optional[Path], bool]:
    """(target dataset, synthetic final path or None, reused). Step 1 of launch()."""
    if spec.kind == "replay_db":
        url = settings.REPLAY_DATABASE_URL
        if not url:
            raise _bad(NOT_CONFIGURED)
        _refuse_main_db(url)
        if not replay_db_launchable(url):
            log.info("backtest launch refused: replay-db names a synthetic dataset "
                     "that has not been generated")
            raise _bad(NOT_INITIALISED)
        return replay_store.resolve(replay_store.REPLAY_DB_ID), None, False   # alias-aware
    sid = synthetic_id(spec.days, spec.seed, spec.start)
    final = resolve_dataset_dir() / f"{sid}.db"
    if final.is_file():                        # a final name always means "generation completed"
        return replay_store.resolve(sid), final, True
    ds = Dataset(dataset_id=sid, kind="synthetic",
                 label=f"Synthetic {spec.days}d seed {spec.seed} from {spec.start.isoformat()}",
                 synthetic=True, exists=False, url=_sqlite_url(final))
    _refuse_main_db(ds.url)
    return ds, final, False


async def count_overlaps(target: Dataset, req: LaunchRequest, underlyings: list[str]) -> int:
    """Prior COMPLETE runs in the target store over an overlapping window with a
    different params_hash, the hash computed by the engine's own functions."""
    from app.services.replay import engine

    path = _url_sqlite_path(target.url or "")
    if not target.exists or (path is not None and not path.is_file()):
        return 0                               # never open a store that does not exist
    overrides = json.loads(json.dumps(req.overrides))      # what params.json round-trips to
    p, extra, _ = engine.resolve_params(overrides)
    cfg = engine.run_config_for(p, extra, tuple(underlyings))
    _, h = engine.params_record(p, cfg)
    async with replay_store.store_session(target) as db:
        n = await db.scalar(select(func.count()).select_from(ReplayRun).where(
            ReplayRun.status == "COMPLETE", ReplayRun.params_hash != h,
            ReplayRun.date_from <= req.date_to, ReplayRun.date_to >= req.date_from))
    return int(n or 0)


def overlap_warning(overlaps: int, notes: str) -> bool:
    """== report._search_trail's empty-notes test; ignores the overlap threshold (advisory)."""
    return overlaps > 0 and not notes.strip()


def force_label(label: str, kind: str) -> str:
    """A run on a synthetic dataset always carries the word (the report's cap, §4.9)."""
    if kind == "synthetic" and "synthetic" not in label.lower():
        return f"{label} (synthetic)" if label else "synthetic"
    return label


def new_record(req: LaunchRequest, target: Dataset, final: Optional[Path], reused: bool,
               overlaps: int, underlyings: list[str], requested_by: str) -> JobRecord:
    spec = req.dataset
    fresh = final is not None and not reused
    return JobRecord(
        job_id="BTJ-" + uuid.uuid4().hex[:12], state="RUNNING",
        steps=["dataset", "replay"] if fresh else ["replay"],
        dataset=target.dataset_id, dataset_reused=reused,
        label=force_label(req.label, target.kind), notes=req.notes,
        date_from=req.date_from, date_to=req.date_to, underlyings=list(underlyings),
        seed=req.seed if req.seed is not None else settings.REPLAY_DEFAULT_SEED,
        oos_split=req.oos_split, overrides=json.loads(json.dumps(req.overrides)),
        requested_by=requested_by, created_at=utcnow(),
        warnings=["overlapping_prior_runs"] if overlap_warning(overlaps, req.notes) else [],
        prior_overlapping_runs=overlaps,
        bootstrap_iterations=req.bootstrap_iterations, url=target.url, target=target,
        final_path=final if fresh else None,
        synth=(spec.days, spec.seed, spec.start) if fresh else None,
        dataset_dir=resolve_dataset_dir(),
    )


# ── staging ──────────────────────────────────────────────────────────────────

def params_path(rec: JobRecord) -> Path:
    return rec.dataset_dir / "jobs" / rec.job_id / "params.json"


def _stage_params(rec: JobRecord) -> None:
    p = params_path(rec)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec.overrides), encoding="utf-8")


def _unstage_params(rec: JobRecord) -> None:
    p = params_path(rec)
    try:
        p.unlink(missing_ok=True)
        if p.parent.is_dir():
            p.parent.rmdir()
    except OSError:
        log.warning("could not remove the staged parameter file for job %s", rec.job_id)


def replay_argv(rec: JobRecord) -> list[str]:
    """An argument list, never a shell string. The two free-text values use the
    --opt=value form so a leading '-' cannot be read as an option."""
    argv = [sys.executable, "-m", "app.services.replay.cli",
            "--from", rec.date_from.isoformat(), "--to", rec.date_to.isoformat(),
            "--underlyings", ",".join(rec.underlyings),
            "--params-file", str(params_path(rec)),
            "--seed", str(rec.seed), "--oos-split", f"{rec.oos_split:.4f}",
            "--bootstrap-iterations", str(rec.bootstrap_iterations),
            f"--label={rec.label}", f"--notes={rec.notes}",
            "--database-url", rec.url, "--out", str(artifact_write_dir())]
    if getattr(rec.target, "kind", None) == "synthetic":    # the RESOLVED kind (alias-aware)
        argv.append("--synthetic")
    return argv


def _child_spawn_kwargs() -> dict:
    """Console/terminal interrupts must reach only the API; the API cancels the child itself."""
    if os.name == "nt":
        # own process group (Ctrl+C disabled for it) and no console at all (nothing to deliver to)
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}       # own session: not in the terminal's foreground group


def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in _SCRUBBED_ENV and not k.upper().startswith("ANGEL_")}
    for k in _SCRUBBED_ENV:
        env[k] = ""
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"        # the pipes are decoded as UTF-8
    env["REPLAY_ARTIFACT_DIR"] = str(artifact_write_dir())
    return env


# ── tail redaction (C7) ──────────────────────────────────────────────────────

_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://\S*")
_WIN_ABS = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|,;)]*")
_POSIX_ABS = re.compile(r"(?<![\w:/.>\-])/(?:[\w.\-]+/)+[\w.\-]*")


def _last_component(s: str) -> str:
    s = s.rstrip("\\/")
    return re.split(r"[\\/]", s)[-1] if s else ""


def _replace_roots(s: str, dataset_dir: Optional[Path] = None) -> str:
    for root, placeholder in ((BACKEND_DIR, "<backend>"),
                              (dataset_dir or resolve_dataset_dir(), "<datasets>"),
                              (artifact_write_dir(), "<artifacts>")):
        for real in (str(root), Path(root).as_posix()):
            if real:
                s = s.replace(real, placeholder)
    return s


def _redact_rest(s: str) -> str:
    s = _URL_RE.sub("<url>", s)
    s = _WIN_ABS.sub(lambda m: _last_component(m.group(0)), s)
    return _POSIX_ABS.sub(lambda m: _last_component(m.group(0)), s)


def _tail_line(s: str, dataset_dir: Optional[Path] = None) -> str:
    s = s.rstrip()[:TAIL_LINE_CHARS]
    s = _replace_roots(s, dataset_dir)
    if s.startswith("artefact:"):
        return f"artefact: {_last_component(s.split(':', 1)[1].strip())}"
    return _redact_rest(s)


def _redact_error(exc: BaseException) -> str:
    return _redact_rest(_replace_roots(f"{type(exc).__name__}: {exc}"))[:TAIL_LINE_CHARS]


# ── the child process ────────────────────────────────────────────────────────

def _popen(argv: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(argv, **kwargs)


def _kill(proc) -> None:
    try:
        proc.kill()
    except OSError:
        pass                                 # already gone


def _drain(stream, tail: deque, dataset_dir: Optional[Path]) -> None:
    """Daemon reader: redacted lines into a bounded tail (200 lines, 32 KB)."""
    size = sum(len(x) for x in tail)
    try:
        for raw in stream:
            line = _tail_line(raw, dataset_dir)
            if tail.maxlen is not None and len(tail) == tail.maxlen:
                size -= len(tail[0])
            tail.append(line)
            size += len(line)
            while size > TAIL_STREAM_CHARS and tail:
                size -= len(tail.popleft())
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def map_exit_code(rc: Optional[int], stdout_tail) -> str:
    """cli.py's codes. Exit 2 is NO_DATA only with the CLI's own status line;
    any other exit 2 is argparse rejecting the argv."""
    if rc == 0:
        return "SUCCEEDED"
    if rc == 2:
        return "NO_DATA" if any(line.strip() == NO_DATA_LINE for line in stdout_tail) \
            else "INVALID"
    if rc == 3:
        return "INVALID"
    return "FAILED"


def _run_child(rec: JobRecord, argv: list[str], *, timeout: float, step: str) -> Optional[int]:
    """The only spawn site, for both steps. Sets the job's terminal state unless the
    dataset step exited 0 (then _dataset_step decides)."""
    try:
        proc = _popen(argv, cwd=str(BACKEND_DIR), env=_child_env(), stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                      encoding="utf-8", errors="replace", **_child_spawn_kwargs())
    except Exception as exc:                 # FileNotFoundError, NotImplementedError, PermissionError
        log.error("backtest job %s: the %s step could not start", rec.job_id, step, exc_info=True)
        rec.finish("ERROR", step=step, error=_redact_error(exc))
        return None
    rec.step = step                          # so shutdown()'s fallback names the step
    rec.proc = proc
    if rec.cancel_requested:                 # shutdown() ran between Popen and the line above
        _kill(proc)
    readers = [threading.Thread(target=_drain, args=(s, t, rec.dataset_dir), daemon=True)
               for s, t in ((proc.stdout, rec.stdout_tail), (proc.stderr, rec.stderr_tail))]
    for t in readers:
        t.start()
    rc, timed_out, started, killed_at = None, False, time.monotonic(), 0.0
    while True:                              # bounded poll instead of one wait(timeout)
        try:
            rc = proc.wait(timeout=_WAIT_POLL_S)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if rec.cancel_requested and rec.cancel_deadline is not None \
                    and now >= rec.cancel_deadline:
                log.error("backtest job %s: child pid %s did not exit within the shutdown "
                          "grace; abandoning it", rec.job_id, proc.pid)
                break                        # rc stays None; the thread returns
            if not timed_out and now - started > timeout:
                timed_out, killed_at = True, now
                _kill(proc)                  # then keep polling
            elif timed_out and now - killed_at > _KILL_GRACE_S:
                log.error("backtest job %s: child pid %s survived kill(); abandoning it",
                          rec.job_id, proc.pid)
                break
    rec.proc = None
    rec.exit_code = rc                       # None when the child was abandoned
    for t in readers:
        t.join(timeout=2.0 if rc is not None else 0.1)
    if rec.state in TERMINAL:                # never overwrite a terminal state already set
        return rc
    if rec.cancel_requested:
        rec.finish("CANCELLED", step=step)   # any rc, including the kill's, a timeout and None
    elif timed_out:
        rec.finish("TIMEOUT", step=step, timed_out=True,
                   error=f"stopped at the {timeout:g} s wall clock")
    elif step == "replay":
        state = map_exit_code(rc, list(rec.stdout_tail))
        rec.finish(state, step="replay",
                   error=None if state in ("SUCCEEDED", "NO_DATA")
                   else f"replay exited with code {rc}")
    elif rc != 0:
        rec.finish("FAILED", step="dataset", error=f"dataset generator exited with code {rc}")
    return rc


def _sweep(staging_dir: Path) -> None:
    """One job at a time, so any file left here is an orphan of a crashed process."""
    try:
        entries = list(staging_dir.iterdir())
    except OSError:
        return
    for p in entries:
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                log.warning("could not remove an orphaned staging file (%s)", p.name)


def _dataset_step(rec: JobRecord, final: Path) -> bool:
    """Generate into .staging/, then os.replace onto the final name. Every
    non-success outcome removes the staging file; the final path stays absent."""
    staging_dir = rec.dataset_dir / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    _sweep(staging_dir)
    staging = staging_dir / f"{rec.job_id}.db"
    days, seed, start = rec.synth
    ok = False
    try:
        rc = _run_child(rec, [sys.executable,
                              str(BACKEND_DIR / "scripts" / "make_synthetic_replay_db.py"),
                              "--out", str(staging), "--days", str(days), "--seed", str(seed),
                              "--start", start.isoformat()],
                        timeout=settings.REPLAY_DATASET_TIMEOUT_S, step="dataset")
        if rec.state != "RUNNING":           # FAILED / TIMEOUT / ERROR / CANCELLED already set
            return ok
        if rc == 0 and staging.is_file():
            try:
                os.replace(staging, final)   # atomic on one volume; the only final-name writer
            except OSError as exc:
                log.error("backtest job %s: could not publish the generated dataset (%s)",
                          rec.job_id, type(exc).__name__)
                rec.finish("ERROR", step="dataset", error=type(exc).__name__)
                return ok
            ok = True
        else:                                # exit 0 but no file: a failed generation
            rec.finish("FAILED", step="dataset", error="dataset generator produced no file")
        return ok
    finally:
        if not ok:
            for p in (staging, *(staging.with_name(staging.name + s)
                                 for s in ("-journal", "-wal", "-shm"))):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not remove staging file for job %s", rec.job_id)


def _spawn(rec: JobRecord) -> None:
    """Runs in the to_thread worker: dataset step (when planned), then replay."""
    if "dataset" in rec.steps and not _dataset_step(rec, rec.final_path):
        return                               # terminal, step "dataset"
    if rec.cancel_requested:                 # shutdown() between the steps
        rec.finish("CANCELLED", step="replay")
        return
    try:                                     # pre-Popen guards, on the exact --database-url
        assert_not_main_db(rec.url)
    except ReplayTargetRefused:
        log.error("backtest job %s refused: the replay target is the application database",
                  rec.job_id)
        rec.finish("REFUSED", step="replay", error=REFUSED_MAIN_DB)
        return
    if would_create_final_name(rec.url):
        log.error("backtest job %s refused: the target would create a synthetic dataset name",
                  rec.job_id)
        rec.finish("REFUSED", step="replay", error=NOT_INITIALISED)
        return
    _run_child(rec, replay_argv(rec), timeout=settings.REPLAY_JOB_TIMEOUT_S, step="replay")


# ── persistence ──────────────────────────────────────────────────────────────

def _record_path(dataset_dir: Path, job_id: str) -> Path:
    return dataset_dir / "jobs" / f"{job_id}.json"


def _persist(rec: JobRecord) -> None:
    """<dataset_dir>/jobs/<id>.json via .tmp + os.replace: a reader never sees a torn file."""
    final = _record_path(rec.dataset_dir, rec.job_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    tmp.write_text(json.dumps(rec.view(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


def _read_record(path: Path) -> Optional[dict]:
    """A persisted record, or None when unreadable. RUNNING on disk means the
    process that owned it is gone, so it reads as CANCELLED."""
    try:
        v = JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))).view()
    except (OSError, ValueError) as exc:
        log.warning("backtest job file %s unreadable (%s)", path.stem, type(exc).__name__)
        return None
    if v["state"] == "RUNNING":
        log.debug("backtest job %s found RUNNING on disk; reporting CANCELLED", v["job_id"])
        v["state"] = "CANCELLED"
    return v


async def _resolve_run_id(rec: JobRecord) -> None:
    """stdout's run_id= line, else the newest of the target store's 10 newest rows
    matching (date_from, date_to, seed) — unambiguous with one job at a time."""
    for line in reversed(list(rec.stdout_tail)):
        m = RUN_ID_RE.search(line)
        if m:
            rec.run_id, rec.run_id_source = m.group(1), "stdout"
            return
    if rec.step != "replay" or rec.exit_code is None \
            or rec.state not in ("SUCCEEDED", "NO_DATA", "FAILED") or rec.target is None:
        return
    path = _url_sqlite_path(rec.url or "")
    if path is not None and not path.is_file():
        return
    try:
        async with replay_store.store_session(rec.target) as db:
            rows = (await db.execute(
                select(ReplayRun.run_id, ReplayRun.date_from, ReplayRun.date_to, ReplayRun.seed)
                .order_by(desc(ReplayRun.created_at)).limit(10))).all()
    except Exception as exc:                 # advisory: the job's outcome is already set
        log.warning("backtest job %s: run id store scan failed (%s)", rec.job_id,
                    type(exc).__name__)
        return
    for run_id, d_from, d_to, seed in rows:
        if (d_from, d_to, seed) == (rec.date_from, rec.date_to, rec.seed):
            rec.run_id, rec.run_id_source = run_id, "store_scan"
            return


# ── admission and lifecycle ──────────────────────────────────────────────────

_running: Optional[JobRecord] = None
_ring: deque[JobRecord] = deque(maxlen=settings.REPLAY_JOB_HISTORY)
_tasks: set[asyncio.Task] = set()            # strong refs so the task is not GC'd


def _job_running(job_id: str) -> HTTPException:
    log.info("backtest launch rejected: job %s is running", job_id)
    return HTTPException(status_code=409, detail={"code": "JOB_RUNNING", "job_id": job_id})


def _admit(rec: JobRecord) -> None:
    """NO await anywhere in this function: atomic under a single-threaded loop."""
    global _running
    if _running is not None:
        raise _job_running(_running.job_id)
    _running = rec
    _ring.appendleft(rec)                    # visible to GET /jobs/{id} from this instant


def _log_outcome(rec: JobRecord) -> None:
    level = (logging.INFO if rec.state in ("SUCCEEDED", "NO_DATA")
             else logging.WARNING if rec.state == "CANCELLED" else logging.ERROR)
    log.log(level, "backtest job %s finished %s (step %s)", rec.job_id, rec.state, rec.step)


async def _run(rec: JobRecord) -> None:
    """The background task owns the lifecycle; its finally is the normal persister."""
    global _running
    rec.started_at = utcnow()
    try:
        try:
            await asyncio.to_thread(_spawn, rec)
        except Exception as exc:
            log.error("backtest job %s failed unexpectedly", rec.job_id, exc_info=True)
            rec.finish("ERROR", step=rec.step or rec.steps[0], error=type(exc).__name__)
        await _resolve_run_id(rec)
        _log_outcome(rec)
    finally:
        if _running is rec:
            _running = None
        try:
            _persist(rec)
        except Exception:
            log.error("backtest job %s could not be persisted", rec.job_id, exc_info=True)


async def launch(req: LaunchRequest, requested_by: str) -> dict:
    """The POST /jobs handler body → the 202 LaunchResponse projection."""
    global _running
    # step 0: non-authoritative fast path — a running job is 409 before anything can 503
    if _running is not None:
        raise _job_running(_running.job_id)
    # step 1: every fallible step, awaits allowed, nothing admitted yet
    underlyings = validate_request(req)
    target, final, reused = resolve_dataset(req.dataset)
    overlaps = await count_overlaps(target, req, underlyings)
    rec = new_record(req, target, final, reused, overlaps, underlyings, requested_by)
    try:
        artifact_write_dir().mkdir(parents=True, exist_ok=True)
        _stage_params(rec)
    except OSError:
        log.error("backtest job %s: could not stage the run", rec.job_id, exc_info=True)
        _unstage_params(rec)
        raise HTTPException(status_code=500, detail="could not stage the run")
    # steps 2-3: synchronous, no await, nothing fallible by design
    try:
        try:
            _admit(rec)
        except HTTPException:
            _unstage_params(rec)             # a 409'd request leaves no params.json
            raise
        t = asyncio.create_task(_run(rec))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    except HTTPException:
        raise
    except BaseException:                    # guard: never leave _running stuck
        if _running is rec:
            _running = None
        try:
            _ring.remove(rec)
        except ValueError:
            pass
        _unstage_params(rec)
        log.error("backtest job %s could not be started", rec.job_id, exc_info=True)
        raise
    return rec.launch_view()


async def shutdown(grace_s: float = SHUTDOWN_GRACE_S) -> None:
    """Lifespan shutdown: cancel the running job and let its own finally persist
    CANCELLED; persist it here only if the grace expires first. Never raises."""
    try:
        rec = _running
        if rec is None:
            return
        rec.cancel_deadline = time.monotonic() + grace_s    # read by _run_child's wait loop
        rec.cancel_requested = True                          # BEFORE the kill: its rc must not win
        proc = rec.proc
        if proc is not None and proc.poll() is None:
            _kill(proc)
        pending = [t for t in _tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=grace_s)
        if rec.state not in TERMINAL:                        # grace expired, or no step running
            rec.finish("CANCELLED", step=rec.step or rec.steps[0])
            _persist(rec)
        log.warning("backtest job %s cancelled by API shutdown (%s)", rec.job_id, rec.state)
    except Exception:                                        # never abort the rest of the lifespan
        log.error("backtest job shutdown failed", exc_info=True)


# ── reads ────────────────────────────────────────────────────────────────────

def check_job_id(job_id: str) -> str:
    if not job_id or not JOB_ID_RE.match(job_id):
        raise _bad(f"job_id must match {JOB_ID_RE.pattern}")
    return job_id


def get_job(job_id: str) -> dict:
    """Ring buffer, else <dataset_dir>/jobs/<id>.json, else 404."""
    check_job_id(job_id)
    for rec in list(_ring):
        if rec.job_id == job_id:
            return rec.view()
    path = _record_path(resolve_dataset_dir(), job_id)
    v = _read_record(path) if path.is_file() else None
    if v is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    return v


def list_jobs() -> dict:
    """The ring buffer, back-filled from the newest REPLAY_JOB_HISTORY job files."""
    items = [r.view() for r in list(_ring)]
    seen = {i["job_id"] for i in items}
    d = resolve_dataset_dir() / "jobs"
    files: list[tuple[int, Path]] = []
    if d.is_dir():
        for p in d.glob("BTJ-*.json"):
            if JOB_ID_RE.match(p.stem) and p.stem not in seen:
                try:
                    files.append((p.stat().st_mtime_ns, p))
                except OSError:
                    continue
    files.sort(key=lambda x: x[0], reverse=True)
    back = [v for v in (_read_record(p) for _, p in files[:settings.REPLAY_JOB_HISTORY])
            if v is not None]
    back.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    items += back
    return {"items": items, "total": len(items),
            "running_job_id": _running.job_id if _running is not None else None,
            "max_concurrent_jobs": MAX_CONCURRENT_JOBS}


def launch_options() -> dict:
    """GET /launch-options: no secret, no URL. The replay-db entry is offered only
    when REPLAY_DATABASE_URL is set and is not the application database."""
    from app.api.guards import admin_key_configured

    datasets: list[dict] = []
    url = settings.REPLAY_DATABASE_URL
    refused = False
    if url:
        try:
            assert_not_main_db(url)
        except ReplayTargetRefused:
            refused = True
    if url and not refused:
        reg = replay_store.registry()
        if reg.alias_of:
            synthetic, alias_of, exists = True, reg.alias_of, True
        else:
            ds = next((d for d in reg.datasets if d.dataset_id == replay_store.REPLAY_DB_ID), None)
            synthetic, alias_of, exists = False, None, bool(ds and ds.exists)
        datasets.append({"dataset_id": replay_store.REPLAY_DB_ID, "kind": "replay_db",
                         "label": "Replay database (configured)", "synthetic": synthetic,
                         "alias_of": alias_of, "exists": exists,
                         "launchable": replay_db_launchable(url)})
    datasets.append({"dataset_id": "synthetic", "kind": "synthetic_template",
                     "label": "Generate synthetic dataset", "synthetic": True,
                     "alias_of": None, "exists": None, "launchable": True})
    p = TradingParams()
    overrides = {k: getattr(p, k) for k in DEFAULT_OVERRIDE_KEYS}
    overrides["max_hold_minutes"] = dict(sorted(p.max_hold_minutes.items()))
    return {
        "admin_key_configured": admin_key_configured(),
        "datasets": datasets,
        "limits": {"max_range_days": settings.REPLAY_MAX_RANGE_DAYS,
                   "max_launch_range_days": max_launch_range()[0],
                   "synthetic_min_days": SYNTHETIC_MIN_DAYS,
                   "synthetic_max_days": settings.BACKTEST_SYNTHETIC_MAX_DAYS,
                   "job_timeout_s": settings.REPLAY_JOB_TIMEOUT_S,
                   "bootstrap_iterations": {"min": BOOT_MIN, "max": BOOT_MAX,
                                            "default": BOOT_DEFAULT},
                   "oos_split": {"min": OOS_MIN, "max": OOS_MAX, "default": OOS_DEFAULT}},
        "defaults": {"seed": settings.REPLAY_DEFAULT_SEED, "overrides": overrides},
        "underlyings": list(UNDERLYINGS),
        "running_job_id": _running.job_id if _running is not None else None,
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
    }
