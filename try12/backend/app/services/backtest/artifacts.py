"""
The artefact tier (backtest design §4.3): ``<artifact_dir>/<run_id>.json``
validation reports, read for run ids no replay store holds.

A client string reaches the filesystem only here, behind three guards:
``BACKTEST_RUN_ID_RE``, ``app.api.guards.validate_run_id`` and a resolved-path
containment check. An unreadable artefact (malformed JSON, I/O error, no
``sections``, larger than 8 MB) is omitted from the index and ``load`` returns
None; it is never a 500.

A report is surfaced whole (notes, record, coverage, breakdowns, ``validation``),
so every string in it is path-redacted at parse time (``_redacted``) rather than
field by field at serialisation (C7).
"""
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from app.api.guards import RUN_ID_PATTERN, validate_run_id
from app.core.config import settings

log = logging.getLogger(__name__)

BACKTEST_RUN_ID_RE = re.compile(r"^RPL-[A-Za-z0-9]{1,26}$")
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
LRU_SIZE = 64


def check_run_id(run_id: str) -> str:
    """400 for anything but an RPL- run id; PAPER is refused by rule, not by accident."""
    if run_id and BACKTEST_RUN_ID_RE.match(run_id):
        return validate_run_id(run_id)
    if run_id and RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=400, detail="backtest routes serve replay runs only")
    raise HTTPException(status_code=400, detail=f"run_id must match {BACKTEST_RUN_ID_RE.pattern}")


def artifact_dir() -> Optional[Path]:
    """The reader's artefact directory, or None when absent (tier 2 then absent)."""
    from app.services.replay.cli import default_artifact_dir
    d = (Path(settings.REPLAY_ARTIFACT_DIR) if settings.REPLAY_ARTIFACT_DIR
         else default_artifact_dir())
    return d if d.is_dir() else None


@dataclass(frozen=True)
class ArtifactRow:
    run_id: str
    created_at: str                     # file mtime (created_at_source = "file_mtime")
    label: Optional[str]
    date_from: Optional[str]
    date_to: Optional[str]
    underlyings: Optional[list]
    seed: Optional[int]
    params_hash: Optional[str]
    code_version: Optional[str]
    trades: Optional[int]
    win_rate: Optional[float]
    expectancy_r: Optional[float]
    expectancy_inr: Optional[float]
    max_drawdown_pct: Optional[float]
    recommendation: Optional[str]
    failing_criteria: Optional[list]
    caps: Optional[list]
    synthetic: Optional[bool]
    coverage: Optional[dict]            # sections.2 (days + summary)
    metrics_available: bool
    breakdowns_available: bool


# ── parsing with an LRU keyed by (path, mtime_ns, size) ──────────────────────

_lru: "OrderedDict[tuple, dict]" = OrderedDict()


def _stat_key(p: Path) -> Optional[tuple]:
    try:
        st = p.stat()
    except OSError:
        return None
    return (str(p), st.st_mtime_ns, st.st_size)


def _parse(p: Path) -> Optional[dict]:
    key = _stat_key(p)
    if key is None:
        return None
    if key in _lru:
        _lru.move_to_end(key)
        return _lru[key]
    if key[2] > MAX_ARTIFACT_BYTES:
        log.warning("artefact %s skipped: larger than %d bytes", p.name, MAX_ARTIFACT_BYTES)
        return None
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):     # JSONDecodeError is a ValueError
        return None
    if (not isinstance(rep, dict) or not isinstance(rep.get("sections"), dict)
            or rep.get("run_id") != p.stem):
        return None
    rep = _redacted(rep)
    _lru[key] = rep
    while len(_lru) > LRU_SIZE:
        _lru.popitem(last=False)
    return rep


def _redacted(rep: dict) -> dict:
    """Every string in a parsed report passed through ``read.redact_paths`` (C7).

    Tier 1 redacts its one free-text column where the response is assembled, but a report
    artefact is surfaced whole — ``notes`` (``sections.9.declared``), the frozen record, the
    coverage rows, the breakdowns and the ``validation`` object are all served as stored — so the
    pass has to cover the tree, not a field list, or the property would depend on which section a
    future report writes a path into. Done once per file load, so it is cached with the parse."""
    from app.services.backtest.read import redact_paths      # local: read imports this module

    def walk(v):
        if isinstance(v, str):
            return redact_paths(v)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(rep)


def _section(rep: dict, n: str) -> dict:
    s = (rep.get("sections") or {}).get(n)
    return s if isinstance(s, dict) else {}


def _row(p: Path, rep: dict) -> ArtifactRow:
    s1, s4 = _section(rep, "1"), _section(rep, "4")
    m = s4.get("metrics") if isinstance(s4.get("metrics"), dict) else None
    mtime = p.stat().st_mtime
    mm = m or {}
    return ArtifactRow(
        run_id=p.stem,
        created_at=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        label=rep.get("label") or None,
        date_from=s1.get("date_from"), date_to=s1.get("date_to"),
        underlyings=s1.get("underlyings"), seed=s1.get("seed"),
        params_hash=s1.get("params_hash"), code_version=s1.get("code_version"),
        trades=mm.get("trades"), win_rate=mm.get("win_rate"),
        expectancy_r=mm.get("expectancy_r"), expectancy_inr=mm.get("expectancy_inr"),
        max_drawdown_pct=mm.get("max_equity_drawdown_pct"),
        recommendation=rep.get("recommendation"),
        failing_criteria=rep.get("failing_criteria"), caps=rep.get("caps"),
        synthetic=rep.get("synthetic"),
        coverage=_section(rep, "2") or None,
        metrics_available=m is not None,
        breakdowns_available="6" in (rep.get("sections") or {}),
    )


def _candidates(d: Path) -> list[Path]:
    out = []
    for p in d.glob("RPL-*.json"):
        if not BACKTEST_RUN_ID_RE.match(p.stem):
            continue
        try:
            out.append((p.stat().st_mtime_ns, p))
        except OSError:
            continue
    out.sort(key=lambda x: (-x[0], x[1].name))
    return [p for _, p in out]


_rows: dict[tuple, Optional[ArtifactRow]] = {}     # index cache, keyed like the LRU


def scan() -> tuple[list[ArtifactRow], bool]:
    """(rows newest first, truncated). Bounded by BACKTEST_MAX_ARTIFACT_FILES;
    each file's row is cached by (path, mtime_ns, size), so a poll re-parses nothing."""
    d = artifact_dir()
    if d is None:
        return [], False
    files = _candidates(d)
    cap = settings.BACKTEST_MAX_ARTIFACT_FILES
    truncated = len(files) > cap
    rows, live = [], set()
    for p in files[:cap]:
        key = _stat_key(p)
        if key is None:
            continue
        live.add(key)
        if key not in _rows:
            rep = _parse(p)
            try:
                _rows[key] = _row(p, rep) if rep is not None else None
            except OSError:
                _rows[key] = None
        if _rows[key] is not None:
            rows.append(_rows[key])
    for key in [k for k in _rows if k not in live]:
        del _rows[key]
    return rows, truncated


def index() -> list[ArtifactRow]:
    return scan()[0]


def _path_for(run_id: str) -> Optional[Path]:
    if not run_id or not BACKTEST_RUN_ID_RE.match(run_id):
        return None
    validate_run_id(run_id)
    d = artifact_dir()
    if d is None:
        return None
    root = d.resolve()
    p = (d / f"{run_id}.json").resolve()
    if p.parent != root:
        return None
    return p


def load(run_id: str) -> Optional[dict]:
    """The parsed <run_id>.json, or None (absent, unreadable or a rejected id)."""
    p = _path_for(run_id)
    if p is None or not p.is_file():
        return None
    return _parse(p)


def row_for(run_id: str) -> Optional[ArtifactRow]:
    p = _path_for(run_id)
    if p is None or not p.is_file():
        return None
    rep = _parse(p)
    if rep is None:
        return None
    try:
        return _row(p, rep)
    except OSError:
        return None


def reset_cache() -> None:
    _lru.clear()
    _rows.clear()
