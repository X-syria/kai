"""CLI-01: packaging must not advertise an entry point it cannot deliver.

Installing this project generated a real ``kai`` executable. Its generated
wrapper runs ``from kai.cli import cli``. No such module has ever existed in
this package, so the command died before it could print anything::

    ModuleNotFoundError: No module named 'kai.cli'

The contract under test is a packaging contract, not a feature list. Packaging
publishes a script as ``module:attribute``; Python resolves that into an import
followed by a ``getattr``. This test drives that *same* resolver
(``importlib.metadata.EntryPoint.load``) over every console script the project
declares, so a declaration can never again outrun its implementation.

The four concerns are kept apart on purpose. A script that resolves but is not
callable is a different defect from one that does not resolve at all, and a
failure caused by an unrelated environment is neither.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

PYPROJECT: Path = Path(__file__).resolve().parents[1] / "pyproject.toml"
CONSOLE_SCRIPTS: str = "console_scripts"


def declared_console_scripts() -> dict[str, str]:
    """Read ``[project.scripts]`` the way the build backend publishes it."""
    with PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    scripts = pyproject.get("project", {}).get("scripts", {})
    return {str(name): str(target) for name, target in scripts.items()}


def resolve(target: str) -> Any:
    """Resolve ``module:attribute`` exactly as a console-script wrapper does."""
    return EntryPoint(name="kai", value=target, group=CONSOLE_SCRIPTS).load()


def test_declared_console_scripts_resolve_to_an_importable_target() -> None:
    """Importability: every declared target must be importable and gettable.

    This is the assertion that failed against the ``kai.cli:cli`` declaration.
    """
    for name, target in declared_console_scripts().items():
        try:
            resolve(target)
        except Exception as exc:
            pytest.fail(
                f"CLI-01: declared console script {name!r} points at {target!r}, "
                f"which does not resolve: {type(exc).__name__}: {exc}"
            )


def test_declared_console_scripts_resolve_to_a_callable_target() -> None:
    """Callability: a console-script wrapper calls its target with no arguments."""
    for name, target in declared_console_scripts().items():
        loaded = resolve(target)
        assert callable(loaded), (
            f"CLI-01: declared console script {name!r} points at {target!r}, "
            f"which resolves to {loaded!r} and is not callable"
        )


def test_asgi_app_is_the_entry_point_this_project_actually_ships() -> None:
    """The run path that exists must stay resolvable, and is the ASGI app.

    ``Dockerfile`` and ``README`` both start this project as
    ``uvicorn kai.main:app``. That is the invocation contract actually in force,
    so it is resolved through the same mechanism and held to the same standard
    the removed ``kai`` console script failed to meet.
    """
    loaded = resolve("kai.main:app")
    assert isinstance(loaded, FastAPI), (
        f"CLI-01: run path 'kai.main:app' resolved to {loaded!r}, expected a FastAPI application"
    )
