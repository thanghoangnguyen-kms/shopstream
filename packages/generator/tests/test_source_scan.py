"""The D-30 source scan: nothing under packages/generator/src may reintroduce nondeterminism.

`findings` parses a module with `ast` and reports the constructs that would make two machines
disagree: a libm or random variate call, a `math` import, an RNG outside `rng.py`, a float
literal, true division, a wall-clock or timestamp conversion, and a database-clock token in a
string (no generator SQL reads a clock: ADR-004 C1). Docstrings may name these rules without
tripping the scan. Phase 4's pacer will need an explicit allow entry here for its single
wall-clock read.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "packages/generator/src/shopstream_generator"
RNG_FILE = "rng.py"

RANDOM_CALLS = frozenset(
    {
        "random",
        "randrange",
        "randint",
        "choice",
        "choices",
        "shuffle",
        "sample",
        "uniform",
        "triangular",
        "gauss",
        "expovariate",
        "lognormvariate",
        "normalvariate",
        "vonmisesvariate",
        "gammavariate",
        "betavariate",
        "paretovariate",
        "weibullvariate",
        "binomialvariate",
    }
)
TIME_ATTRIBUTES = frozenset({"fromtimestamp", "utcfromtimestamp", "utcnow", "now", "today"})
DATABASE_CLOCKS = (
    "now()",
    "current_timestamp",
    "clock_timestamp",
    "statement_timestamp",
    "transaction_timestamp",
    "localtimestamp",
)


def _imports(node: ast.AST, module: str) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == module or alias.name.startswith(f"{module}.") for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        name = node.module or ""
        return node.level == 0 and (name == module or name.startswith(f"{module}."))
    return False


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the constant nodes that are module, class or function docstrings."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def findings(path_label: str, source: str) -> list[str]:
    """Every rule a module breaks, as `path:line: (rule) what`, in source order."""
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
    is_rng = Path(path_label).name == RNG_FILE
    found: list[tuple[int, str]] = []

    def flag(node: ast.AST, rule: str, what: str) -> None:
        found.append(
            (
                getattr(node, "lineno", 0),
                f"{path_label}:{getattr(node, 'lineno', 0)}: ({rule}) {what}",
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            if name in RANDOM_CALLS:
                flag(node, "a", f"call to {name}")
        if _imports(node, "math"):
            flag(node, "b", "import of math")
        if _imports(node, "random") and not is_rng:
            flag(node, "c", "import of random outside rng.py")
        if isinstance(node, ast.Constant) and isinstance(node.value, float | complex):
            flag(node, "d", "float or complex literal")
        if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ast.Div):
            flag(node, "e", "true division")
        if isinstance(node, ast.Attribute) and node.attr in TIME_ATTRIBUTES:
            flag(node, "f", f"wall-clock or timestamp conversion {node.attr}")
        if _imports(node, "time"):
            flag(node, "f", "import of time")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            lowered = node.value.lower()
            for token in DATABASE_CLOCKS:
                if token in lowered:
                    flag(node, "g", f"database clock token {token}")
    return [text for _, text in sorted(found, key=lambda item: item[0])]


def test_generator_source_is_clean() -> None:
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        problems += findings(path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8"))
    assert problems == []


TEETH = [
    pytest.param("a", "import x\nx.expovariate(2)\n", id="a-variate-call"),
    pytest.param("a", "gauss(1, 2)\n", id="a-bare-call"),
    pytest.param("b", "import math\n", id="b-import-math"),
    pytest.param("b", "from math import floor\n", id="b-from-math"),
    pytest.param("c", "import random\n", id="c-import-random"),
    pytest.param("c", "from random import Random\n", id="c-from-random"),
    pytest.param("d", "X = 1.5\n", id="d-float"),
    pytest.param("d", "X = 2j\n", id="d-complex"),
    pytest.param("e", "X = 1 / 2\n", id="e-true-division"),
    pytest.param("e", "x = 4\nx /= 2\n", id="e-augmented-division"),
    pytest.param("f", "import datetime\nX = datetime.datetime.now()\n", id="f-now"),
    pytest.param(
        "f", "import datetime\nX = datetime.datetime.fromtimestamp(0)\n", id="f-fromtimestamp"
    ),
    pytest.param("f", "import time\n", id="f-import-time"),
    pytest.param("g", 'SQL = "SELECT NOW()"\n', id="g-now"),
    pytest.param("g", 'SQL = "x DEFAULT CURRENT_TIMESTAMP"\n', id="g-current-timestamp"),
]


@pytest.mark.parametrize(("rule", "snippet"), TEETH)
def test_the_scan_has_teeth(rule: str, snippet: str) -> None:
    flagged = findings("packages/generator/src/shopstream_generator/probe.py", snippet)
    assert any(f"({rule})" in text for text in flagged)


def test_the_scan_allows_what_it_should() -> None:
    assert findings("x/rng.py", "import random\nR = random.Random('a')\n") == []
    docstring = '"""Never call now() or CURRENT_TIMESTAMP, or use 1.5 / math."""\nX = 1 // 2\n'
    assert findings("x/other.py", docstring) == []
    assert findings("x/other.py", "def f() -> None:\n    'docs may say now()'\n") == []
    assert findings("x/other.py", "import datetime\nX = datetime.timedelta(microseconds=1)\n") == []


def test_rng_holds_the_only_s311_noqa() -> None:
    hits = [
        path.relative_to(SRC).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        for text in path.read_text(encoding="utf-8").splitlines()
        if "noqa: S311" in text
    ]
    assert hits == [RNG_FILE]
