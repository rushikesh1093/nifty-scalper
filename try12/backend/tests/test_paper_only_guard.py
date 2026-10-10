"""
Paper-only guard (design §13, R1.2, R1.6).

AngelClient.place_order / cancel_order are DELETED, not guarded. These four
AST rules keep them deleted.

The allowlist below is declared here, in the test, on purpose: widening it is
a visible, reviewable diff. AngelOne groups three READ-ONLY calls under the
``/order/v1/`` prefix (instrument search, order book, positions). None of them
places, modifies or cancels anything, which is why a guard on the bare prefix
would fail against the intended end state. The allowlist is not a loophole:
rule 3 fails on any new ``/order/v1/`` path until it is added here deliberately,
and rule 4 keeps even the allowlisted paths inside angel_client.py.

Scan scope is app/ and scripts/. tests/ is excluded BY DESIGN: this file must
name the forbidden symbols in order to forbid them.
"""
import ast
import inspect
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (BACKEND / "app", BACKEND / "scripts")
ANGEL_CLIENT = BACKEND / "app" / "services" / "broker" / "angel_client.py"

FORBIDDEN_SYMBOLS = {"place_order", "placeOrder", "cancel_order", "cancelOrder",
                     "modify_order", "modifyOrder"}
FORBIDDEN_PATH_SUFFIXES = {"placeOrder", "cancelOrder", "modifyOrder"}
ALLOWED_ORDER_PATHS = {                      # read-only, verified (design §13 table)
    "/rest/secure/angelbroking/order/v1/searchScrip",
    "/rest/secure/angelbroking/order/v1/getOrderBook",
    "/rest/secure/angelbroking/order/v1/getPosition",
}


def _py_files():
    for root in SCAN_ROOTS:
        yield from sorted(root.rglob("*.py"))


def _rel(p: Path) -> str:
    return p.relative_to(BACKEND).as_posix()


def _string_constants(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def _symbol_names(tree: ast.AST):
    """Every identifier that is defined, imported, referenced or used as an attribute."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno
        elif isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.alias):
            yield node.name.split(".")[-1], getattr(node, "lineno", 0)
            if node.asname:
                yield node.asname, getattr(node, "lineno", 0)
        elif isinstance(node, ast.arg):
            yield node.arg, node.lineno
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.arg, getattr(node, "lineno", 0)


def _parsed():
    for p in _py_files():
        yield p, ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def test_rule1_no_forbidden_symbol():
    hits = [f"{_rel(p)}:{ln}: {name}"
            for p, tree in _parsed()
            for name, ln in _symbol_names(tree) if name in FORBIDDEN_SYMBOLS]
    assert not hits, "forbidden order symbols:\n" + "\n".join(hits)


def test_rule2_no_forbidden_path_literal():
    hits = [f"{_rel(p)}:{c.lineno}: {c.value!r}"
            for p, tree in _parsed()
            for c in _string_constants(tree)
            if any(s in c.value for s in FORBIDDEN_PATH_SUFFIXES)]
    assert not hits, "forbidden order path literals:\n" + "\n".join(hits)


def test_rule3_every_order_v1_literal_is_allowlisted():
    hits = [f"{_rel(p)}:{c.lineno}: {c.value!r}"
            for p, tree in _parsed()
            for c in _string_constants(tree)
            if "/order/v1/" in c.value and c.value not in ALLOWED_ORDER_PATHS]
    assert not hits, "non-allowlisted /order/v1/ literals:\n" + "\n".join(hits)


def test_rule4_allowlisted_paths_only_in_angel_client():
    hits = [f"{_rel(p)}:{c.lineno}: {c.value!r}"
            for p, tree in _parsed() if p != ANGEL_CLIENT
            for c in _string_constants(tree) if c.value in ALLOWED_ORDER_PATHS]
    assert not hits, "broker paths outside angel_client.py:\n" + "\n".join(hits)


def test_angel_client_exposes_no_forbidden_callable():
    from app.services.broker.angel_client import AngelClient
    exposed = {n for n, _ in inspect.getmembers(AngelClient, callable)}
    assert not (exposed & FORBIDDEN_SYMBOLS)
    # the three read-only order/v1 methods are still present
    assert {"search_scrip", "get_order_book", "get_positions"} <= exposed


def test_angel_client_documents_the_allowlist():
    text = ANGEL_CLIENT.read_text(encoding="utf-8")
    assert "read-only" in text and "order/v1" in text


def test_app_main_imports():
    import app.main  # noqa: F401


def test_scan_detects_a_synthetic_violation(tmp_path):
    """Negative control: the rules fire on a module that reintroduces an order call."""
    src = (
        "async def place_order(c):\n"
        "    return await c.post('/rest/secure/angelbroking/order/v1/placeOrder')\n"
    )
    tree = ast.parse(src)
    assert any(n in FORBIDDEN_SYMBOLS for n, _ in _symbol_names(tree))
    assert any(any(s in c.value for s in FORBIDDEN_PATH_SUFFIXES) for c in _string_constants(tree))
