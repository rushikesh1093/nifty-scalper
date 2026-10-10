"""R13.1 / R13.2 (design §14.2): replay reuses the decision code, it does not
branch inside it.

Two scans over the shared decision code (app/services/trading, app/strategies,
app/services/metrics): no ``is_replay`` / ``REPLAY`` / ``if replay`` token in
code (comments and docstrings are prose and are skipped), and no import of
app.services.replay. Each scan has a negative control.
"""
import ast
import io
import pathlib
import re
import tokenize

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
SCOPE = (APP / "services" / "trading", APP / "strategies", APP / "services" / "metrics")
FORBIDDEN = re.compile(r"\bis_replay\b|REPLAY|\bif\s+(not\s+)?replay\b")


def code_only(source: str) -> str:
    """The source with comments and string literals (docstrings included) removed."""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER):
            out.append(tok.string)
        elif tok.type in (tokenize.NEWLINE, tokenize.NL):
            out.append("\n")
    return " ".join(out)


def branch_hits(source: str) -> list[str]:
    return [m.group(0) for m in FORBIDDEN.finditer(code_only(source))]


def replay_imports(source: str) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.startswith("app.services.replay")]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("app.services.replay") or (
                    node.module == "app.services"
                    and any(a.name == "replay" for a in node.names)):
                hits.append(node.module)
    return hits


def _files():
    for root in SCOPE:
        yield from sorted(root.rglob("*.py"))


def test_no_replay_branch_in_decision_code():
    hits = {p.relative_to(APP.parent).as_posix(): h
            for p in _files() if (h := branch_hits(p.read_text(encoding="utf-8")))}
    assert hits == {}


def test_decision_code_never_imports_the_replay_package():
    hits = {p.relative_to(APP.parent).as_posix(): h
            for p in _files() if (h := replay_imports(p.read_text(encoding="utf-8")))}
    assert hits == {}


def test_the_branch_scan_catches_a_replay_branch():
    assert branch_hits("if ctx().is_replay:\n    x = 1\n") == ["is_replay"]
    assert branch_hits("if replay:\n    pass\n") == ["if replay"]
    assert branch_hits("X = params().REPLAY_ALLOW\n") == ["REPLAY"]
    # prose is not code
    assert branch_hits('"""a replay never is_replay"""\n# REPLAY\nx = 1\n') == []
    # the D8 knob is a plain parameter and passes (§14.2)
    assert branch_hits("ok = params().allow_ltp_only_fill\n") == []


def test_the_import_scan_catches_a_replay_import():
    assert replay_imports("from app.services.replay.engine import ReplayEngine\n")
    assert replay_imports("import app.services.replay.cli\n")
    assert replay_imports("from app.services import replay\n")
    assert replay_imports("from app.services import trading\n") == []


def test_the_engine_drives_the_production_step_functions():
    """The engine calls run_cycle / monitor_once / square_off_all and the startup
    checks; it defines no decision function of its own."""
    src = (APP / "services" / "replay" / "engine.py").read_text(encoding="utf-8")
    for call in ("strategy_runner.run_cycle(", "paper_engine.monitor_once(",
                 'paper_engine.square_off_all("EOD")', "strategy_runner._pre_market_setup(",
                 "run_startup_checks(require_config_row=False)", "wire_signal_handlers()",
                 "assert_paper_mode(", "reconcile_performance(", 'mode="PAPER"'):
        assert call in src, call
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not defined & {"handle_signal", "select", "evaluate_pre_selection",
                          "evaluate_sizing", "entry_fill", "exit_fill", "next_stop"}
