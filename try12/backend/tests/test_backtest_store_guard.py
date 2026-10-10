"""Replay store registry guards (backtest design §4.2, §10; C4). Unit, no network."""
import os
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db import replay_store as rs

NAME = "synthetic-5d-s1-2026-09-01.db"


def _url(p) -> str:
    return f"sqlite+aiosqlite:///{Path(p).as_posix()}"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", None)
    monkeypatch.setattr(settings, "REPLAY_DATASET_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "REPLAY_ARTIFACT_DIR", str(tmp_path / "no-artifacts"))
    rs.reset_cache()
    yield
    rs.reset_cache()


# ── C4: the main DATABASE_URL is a refused target ────────────────────────────

def _variant(url: str, **kw) -> str:
    return make_url(url).set(**kw).render_as_string(hide_password=False)


def test_the_main_database_url_is_refused_in_every_spelling():
    main = settings.DATABASE_URL
    refused = [
        main,                                                   # asyncpg
        settings.SYNC_DATABASE_URL,                             # psycopg2
        _variant(main, drivername="postgresql"),                # bare dialect
        _variant(main, password="a-different-password"),        # credentials ignored
        _variant(main, username="someone_else", password="x"),
        _variant(main, host="localhost") if make_url(main).host == "127.0.0.1" else main,
    ]
    for url in refused:
        with pytest.raises(rs.ReplayTargetRefused):
            rs.assert_not_main_db(url)


def test_the_paper_database_name_is_refused_on_any_host():
    with pytest.raises(rs.ReplayTargetRefused):
        rs.assert_not_main_db("postgresql+asyncpg://u:p@db.example:5432/nifty_scalper")


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://u:p@db.example:5432/try12_replay",
    _variant(settings.DATABASE_URL, database="try12_replay"),
    _variant(settings.DATABASE_URL, port=5433),
    "sqlite+aiosqlite:///C:/tmp/replay.db",
    "sqlite+aiosqlite://",
    "sqlite+aiosqlite:///:memory:",
])
def test_unrelated_urls_are_allowed(url):
    rs.assert_not_main_db(url)


def test_a_refused_replay_database_url_disables_the_dataset(monkeypatch):
    monkeypatch.setattr(settings, "REPLAY_DATABASE_URL", settings.SYNC_DATABASE_URL)
    reg = rs.registry()
    assert reg.error == "refused_main_db"
    assert [d.dataset_id for d in reg.datasets] == []
    with pytest.raises(HTTPException) as e:
        rs.resolve("replay-db")
    assert e.value.status_code == 503 and e.value.detail == "replay_store_misconfigured"


# ── dataset ids ──────────────────────────────────────────────────────────────

def test_dataset_id_parse_compose_round_trip():
    sid = rs.synthetic_id(25, 20260901, date(2026, 9, 1))
    assert sid == "synthetic-25d-s20260901-2026-09-01"
    assert rs.parse_synthetic_id(sid) == (25, 20260901, date(2026, 9, 1))
    assert rs.synthetic_id(*rs.parse_synthetic_id(sid)) == sid
    for ok in ("replay-db", "artifacts", sid):
        assert rs.DATASET_ID_RE.match(ok)
    for bad in ("../x", "synthetic-1d-s1-2026-9-1", "synthetic-1000d-s1-2026-09-01",
                "Synthetic-1d-s1-2026-09-01", "replay-db/../x", ""):
        assert not rs.DATASET_ID_RE.match(bad)
    assert rs.parse_synthetic_id("synthetic-1d-s1-2026-13-01") is None
    with pytest.raises(HTTPException) as e:
        rs.resolve("../etc")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        rs.resolve("synthetic-1d-s1-2026-09-01")
    assert e.value.status_code == 404


def test_dataset_view_never_carries_the_url(tmp_path):
    (tmp_path / NAME).write_bytes(b"")
    (ds,) = rs.list_datasets()
    assert ds.url and "url" not in ds.view()
    assert str(tmp_path) not in repr(ds)


# ── _url_sqlite_path ─────────────────────────────────────────────────────────

def test_url_sqlite_path_table(tmp_path, monkeypatch):
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.setattr(rs, "BACKEND_DIR", tmp_path)
    monkeypatch.chdir(elsewhere)
    assert rs._url_sqlite_path(f"sqlite+aiosqlite:///{NAME}") == tmp_path / NAME   # not cwd / NAME
    assert rs._url_sqlite_path("sqlite+aiosqlite:///./x.db") == tmp_path / "x.db"
    absolute = tmp_path / NAME
    assert rs._url_sqlite_path(f"sqlite:///{absolute.as_posix()}") == absolute      # sync driver
    assert rs._url_sqlite_path(_url(absolute)) == absolute
    for none in ("sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:", "sqlite://",
                 "postgresql+asyncpg://u:p@h:5432/d", "not a url"):
        assert rs._url_sqlite_path(none) is None
    assert list(tmp_path.glob("*.db")) == [] and list(elsewhere.iterdir()) == []


# ── would_create_final_name (evidence r6 §2 + r7 §4) ─────────────────────────

def test_would_create_final_name_table(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / ".staging").mkdir()
    spelled = str(tmp_path / "sub" / ".." / NAME)
    if os.name == "nt":
        spelled = spelled.upper()
    assert rs.would_create_final_name(_url(tmp_path / NAME)) is True
    assert rs.would_create_final_name(_url(spelled)) is True                 # case + ..
    assert rs.would_create_final_name(_url(tmp_path / "replay.db")) is False   # non-synthetic name
    assert rs.would_create_final_name(_url(tmp_path / "other" / NAME)) is False  # outside the dir
    assert rs.would_create_final_name(_url(tmp_path / ".staging" / NAME)) is False
    assert rs.would_create_final_name("postgresql+asyncpg://u:p@h:5432/try12_replay") is False
    assert rs.would_create_final_name(f"sqlite:///{(tmp_path / NAME).as_posix()}") is True
    assert rs.would_create_final_name("sqlite+aiosqlite://") is False
    assert rs.would_create_final_name("sqlite+aiosqlite:///:memory:") is False
    # the predicate created nothing
    assert list(tmp_path.rglob("*.db")) == []
    (tmp_path / NAME).write_bytes(b"")
    assert rs.would_create_final_name(_url(tmp_path / NAME)) is False        # the file exists


def test_relative_url_is_anchored_at_backend_dir(tmp_path, monkeypatch):
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.setattr(rs, "BACKEND_DIR", tmp_path)
    monkeypatch.chdir(elsewhere)
    url = f"sqlite+aiosqlite:///{NAME}"
    assert rs._url_sqlite_path(url) == tmp_path / NAME
    assert rs.would_create_final_name(url) is True
    monkeypatch.setattr(settings, "REPLAY_DATASET_DIR", "rel-datasets")
    assert rs.resolve_dataset_dir() == (tmp_path / "rel-datasets").resolve()
    assert list(elsewhere.iterdir()) == []


# ── identity key: one physical store is one dataset ──────────────────────────

def test_identity_key_matches_across_five_spellings(tmp_path, monkeypatch):
    f = tmp_path / NAME
    f.write_bytes(b"")
    monkeypatch.setattr(rs, "BACKEND_DIR", tmp_path)
    (tmp_path / "sub").mkdir()
    spellings = [
        _url(f),                                                  # forward slashes
        f"sqlite+aiosqlite:///{tmp_path / 'sub' / '..' / NAME}",  # a .. segment, native slashes
        f"sqlite:///{f.as_posix()}",                              # sync driver
        f"sqlite+aiosqlite:///{NAME}",                            # relative to BACKEND_DIR
        f"sqlite+aiosqlite:///{str(f).upper() if os.name == 'nt' else f}",  # case (Windows)
    ]
    keys = {rs.store_key(u) for u in spellings}
    assert len(keys) == 1 and None not in keys
    other = tmp_path / "synthetic-5d-s2-2026-09-01.db"
    assert rs.store_key(_url(other)) not in keys
    assert rs.store_key("postgresql+asyncpg://a:b@LOCALHOST/db") == \
        rs.store_key("postgresql+psycopg2://c:d@127.0.0.1:5432/db")
