"""
Credential hygiene (R1.7, R16.9, R19.6; design §13).

AngelOne credentials are referenced by setting name only. The values are read
in exactly one module (the broker client), never formatted into a log line,
and never serialised into an API payload.
"""
import ast
from pathlib import Path

import httpx
import pytest

from tests.helpers import run, seed_config

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
ANGEL_CLIENT = APP / "services" / "broker" / "angel_client.py"
CONFIG = APP / "core" / "config.py"          # declares the fields; reads them only via `self.`

SECRET_SETTINGS = {"ANGEL_PASSWORD", "ANGEL_TOTP_SECRET", "ANGEL_API_KEY"}
ALL_CREDENTIAL_SETTINGS = SECRET_SETTINGS | {"ANGEL_CLIENT_ID"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _trees():
    for p in sorted(APP.rglob("*.py")):
        yield p, ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def _is_settings_attr(node: ast.AST, names: set[str]) -> bool:
    """`settings.X` or `<mod>.settings.X` with X in names."""
    if not (isinstance(node, ast.Attribute) and node.attr in names):
        return False
    v = node.value
    return (isinstance(v, ast.Name) and v.id == "settings") or \
           (isinstance(v, ast.Attribute) and v.attr == "settings")


def _is_log_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOG_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"logger", "logging", "log", "_log", "_logger"})


def test_secret_settings_read_only_in_angel_client():
    hits = []
    for p, tree in _trees():
        if p == ANGEL_CLIENT:
            continue
        for node in ast.walk(tree):
            if _is_settings_attr(node, SECRET_SETTINGS):
                hits.append(f"{p.relative_to(BACKEND).as_posix()}:{node.lineno}: settings.{node.attr}")
    assert not hits, "credential settings referenced outside angel_client.py:\n" + "\n".join(hits)


def test_no_log_format_string_names_a_credential():
    hits = []
    for p, tree in _trees():
        for node in ast.walk(tree):
            if not _is_log_call(node):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and any(n in sub.value for n in ALL_CREDENTIAL_SETTINGS):
                    hits.append(f"{p.relative_to(BACKEND).as_posix()}:{node.lineno}")
    assert not hits, "log format strings naming credentials:\n" + "\n".join(hits)


def test_no_log_call_formats_a_credential_value():
    hits = []
    for p, tree in _trees():
        for node in ast.walk(tree):
            if _is_log_call(node) and any(_is_settings_attr(s, ALL_CREDENTIAL_SETTINGS)
                                          for s in ast.walk(node)):
                hits.append(f"{p.relative_to(BACKEND).as_posix()}:{node.lineno}")
    assert not hits, "log calls passing a credential value:\n" + "\n".join(hits)


@pytest.fixture
def sentinel_credentials(monkeypatch):
    from app.core.config import settings
    values = {k: f"SENTINEL-{k}-9f3c" for k in sorted(ALL_CREDENTIAL_SETTINGS)}
    for k, v in values.items():
        monkeypatch.setattr(settings, k, v)
    return values


def test_trading_config_payload_has_no_credential_value(sqlite_db, sentinel_credentials):
    from app.main import app
    run(seed_config(sqlite_db))

    async def _get():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.get("/api/v1/trading/config")

    resp = run(_get())
    assert resp.status_code == 200, resp.text
    for v in sentinel_credentials.values():
        assert v not in resp.text
