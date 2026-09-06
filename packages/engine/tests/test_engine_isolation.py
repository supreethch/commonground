"""The engine must not import the application.

docs/architecture.md claims the engine can be evaluated without a database or a
web server. That claim is only worth making if something enforces it, because
the first time someone needs a config value in a hurry, the quickest fix is an
import that quietly welds the engine to the API.
"""

from __future__ import annotations

import ast
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent / "commonground_engine"

# Anything that implies I/O, a framework, or application state.
FORBIDDEN_ROOTS = {
    "fastapi",
    "starlette",
    "sqlalchemy",
    "psycopg",
    "redis",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "commonground_api",
}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports have no module root to check.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_engine_has_no_application_or_io_imports() -> None:
    offenders: dict[str, set[str]] = {}
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        bad = _imported_roots(path) & FORBIDDEN_ROOTS
        if bad:
            offenders[str(path.relative_to(ENGINE_ROOT))] = bad
    assert not offenders, (
        "the engine imported application or I/O modules: "
        f"{offenders}. Move the boundary crossing into apps/api instead."
    )


def test_engine_package_is_importable_on_its_own() -> None:
    import commonground_engine

    assert commonground_engine.ENGINE_VERSION
