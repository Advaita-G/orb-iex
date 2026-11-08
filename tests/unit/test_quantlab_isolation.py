"""Research code must never be able to send an order."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "quantlab"
FORBIDDEN_MODULES = {"orb_iex.broker", "orb_iex.engine", "orb_iex.runner", "orb_iex.paper_auto", "orb_iex.sim",
                     "orb_iex.execution", "orb_iex.stream", "orb_iex.supervisor"}
FORBIDDEN_CALLS = {"post", "put", "patch", "delete", "submit_order", "cancel_order", "replace_order"}


def test_quantlab_imports_no_order_path_and_calls_no_http_writes():
    seen = 0
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = [mod] + [f"{mod}.{a.name}" for a in node.names]
                if node.level:  # relative imports inside quantlab only
                    names = []
            assert not (set(names) & FORBIDDEN_MODULES), f"{path.name} imports an order path: {names}"
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS and isinstance(node.value, ast.Name) \
                    and node.value.id in {"requests", "client", "session", "http"}:
                raise AssertionError(f"{path.name}:{node.lineno} uses a write verb {node.attr}")
        seen += 1
    assert seen >= 4
