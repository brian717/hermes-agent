"""Guard the hand-maintained ``[tool.setuptools] py-modules`` list in pyproject.toml.

Top-level single-file modules only land in a wheel when they are named in
``py-modules``. The list is edited by hand, so it drifts whenever a module is
split into siblings: ``hermes_state`` was split into ``hermes_state_common`` /
``_portability`` / ``_schema`` / ``_search`` without the new names being added,
so every wheel-based install (pip/uv, nix, distro packaging) imported
``hermes_state`` and died on ``No module named 'hermes_state_common'`` — the
session store silently disabled, with no persistence, ``--continue``,
``--resume``, or session search (#74287).

Git installs never hit it because they run from the repo tree, where the files
are present regardless of packaging. So the closure below is the only thing
that catches the drift before a release does.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _declared_py_modules() -> list[str]:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return list(data["tool"]["setuptools"]["py-modules"])


def _repo_root_modules() -> set[str]:
    """Top-level ``*.py`` files that are importable as bare module names.

    ``setup.py`` is a build script, never imported as a module.
    """
    return {
        path.stem
        for path in REPO_ROOT.glob("*.py")
        if path.stem != "setup"
    }


def _imported_root_modules(module: str) -> set[str]:
    """Repo-root modules that ``module`` imports, anywhere in its source.

    Deliberately not limited to module-level imports: a lazy import inside a
    function fails just as hard at runtime when the target was never packaged.
    """
    source = (REPO_ROOT / f"{module}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    root_modules = _repo_root_modules()
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) can't reach a top-level module.
            if node.level == 0 and node.module:
                found.add(node.module.split(".", 1)[0])

    return found & root_modules


def test_declared_py_modules_all_exist() -> None:
    """Every name in py-modules must be a real repo-root module."""
    missing = sorted(set(_declared_py_modules()) - _repo_root_modules())
    assert not missing, (
        f"pyproject.toml py-modules names modules that do not exist: {missing}"
    )


def test_py_modules_is_import_closed() -> None:
    """py-modules must be closed under the imports of the modules it declares.

    A declared module that imports a sibling repo-root module drags that
    sibling into the wheel's runtime requirements, so the sibling has to be
    declared too. Anything reachable but undeclared is missing from the
    distribution and will raise ModuleNotFoundError on a wheel install.
    """
    declared = set(_declared_py_modules())
    undeclared: dict[str, set[str]] = {}

    for module in sorted(declared):
        gaps = _imported_root_modules(module) - declared
        if gaps:
            undeclared[module] = gaps

    assert not undeclared, (
        "pyproject.toml py-modules is missing top-level modules that packaged "
        "modules import — they will be absent from every wheel-based install:\n"
        + "\n".join(
            f"  {module} imports {sorted(gaps)}"
            for module, gaps in sorted(undeclared.items())
        )
    )
