"""Static isolation of the backtest code (backtest design §9, §11.1, C1).

The backtest read path and the launcher must never read the live runtime
context (whose capital is the PAPER run's) and must never open the application
engine (app/db/base.py builds it from DATABASE_URL at import time, and every
other router takes Depends on its session getter). The matcher is specified, not
a substring list: the call regex runs over the raw text, so a comment or
docstring that spells a banned call fails too and must be rephrased, never
exempted. Importing a model is allowed; naming the main engine is not."""
import ast
import re
from pathlib import Path

import pytest

from tests.test_capital_isolation import scan_unscoped

BACKEND = Path(__file__).resolve().parents[1]
TARGETS = [BACKEND / "app" / "api" / "routes" / "backtest.py",
           *sorted((BACKEND / "app" / "services" / "backtest").rglob("*.py"))]

BANNED_CALL = re.compile(r"(?<![\w.])(ctx|params)\s*\(")      # over the raw source text
BANNED_MODULES = {"app.db.base", "app.runtime.context"}
BANNED_NAMES = {"get_db", "AsyncSessionLocal"}


def violations(src: str) -> list[str]:
    out = [f"call {m.group(0)!r}" for m in BANNED_CALL.finditer(src)]
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names
                    if a.name in BANNED_MODULES or a.name.startswith(tuple(m + "." for m in BANNED_MODULES))]
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if mod in BANNED_MODULES or any(f"{mod}.{a.name}" in BANNED_MODULES for a in n.names):
                out.append(mod)
            out += [a.name for a in n.names if a.name in BANNED_NAMES]
        elif isinstance(n, ast.Name) and n.id in BANNED_NAMES:
            out.append(n.id)
        elif isinstance(n, ast.Attribute) and n.attr in BANNED_NAMES:
            out.append(n.attr)
    return out


def _rel(p: Path) -> str:
    return p.relative_to(BACKEND).as_posix()


def test_the_scan_covers_every_backtest_module():
    assert {"app/api/routes/backtest.py", "app/services/backtest/__init__.py",
            "app/services/backtest/artifacts.py", "app/services/backtest/jobs.py",
            "app/services/backtest/read.py", "app/services/backtest/series.py"} \
        <= {_rel(p) for p in TARGETS}


def test_backtest_code_never_uses_the_live_context_or_the_main_engine():
    found = {}
    for p in TARGETS:
        v = violations(p.read_text(encoding="utf-8"))
        if v:
            found[_rel(p)] = v
    assert found == {}


@pytest.mark.parametrize("src", ["run_params(r)", "TradingParams(x)", "x.params(1)",
                                 "from app.db.models.replay import ReplayRun"])
def test_the_matcher_allows(src):
    assert violations(src) == []


@pytest.mark.parametrize("src", ["params()", "ctx()",
                                 "from app.db.base import AsyncSessionLocal",
                                 "import app.db.base", "from app.db import base",
                                 "Depends(get_db)", "mod.AsyncSessionLocal()",
                                 "from app.runtime.context import bind",
                                 "# see params (frozen)\n"])
def test_the_matcher_flags(src):
    assert violations(src) != []


def test_backtest_queries_over_run_shared_tables_are_run_scoped():
    """The D12 scanner, extended to the backtest code (§9)."""
    found = {}
    for p in TARGETS:
        hits = scan_unscoped(p.read_text(encoding="utf-8"))
        if hits:
            found[_rel(p)] = hits
    assert found == {}


def test_the_run_scope_scan_still_detects_an_unscoped_backtest_query():
    assert scan_unscoped("q = select(PositionEvent).where(PositionEvent.kind == 'x')\n") == [1]
