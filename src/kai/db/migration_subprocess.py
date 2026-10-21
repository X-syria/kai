"""Run Alembic for migration tests with a minimal, explicit child environment.

The migration suite is intentionally different from the normal test suite: it
must never inherit an administrator connection, an administrator password, or a
future secret that nobody remembered to add to a denylist.  Every child
environment is therefore built from an allowlist, not copied from
``os.environ``.

The module is import-safe: it opens no connection and runs no process until
:func:`run_alembic` is called.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

from sqlalchemy.engine.url import make_url

from kai.db.target_policy import migration_run_target_error

DEFAULT_TIMEOUT_SECONDS: Final = 120.0
_REPO_ROOT: Final = Path(__file__).resolve().parents[3]

# Variables Python, the OS loader, and a console need in order to start.  They
# are not secrets.  Keep this list deliberately short.  ``PYTHONPATH`` and
# ``PYTHONHOME`` are omitted: the repository is editable-installed and
# ``alembic.ini`` already prepends the repository root.
_OS_ENV_NAMES: Final = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "VIRTUAL_ENV",
)

# Explicit, non-secret environment variables a migration child may need.  The
# target URL is added by ``build_child_env``; nothing else is inherited.
_MIGRATION_ENV_NAMES: Final = (
    "KAI_TEST_MIGRATIONS_DATABASE_URL",
    "KAI_TEST_MIGRATIONS_RUN_ID",
)

_URL_RE: Final = re.compile(r"postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?://\S+")
_PASSWORD_RE: Final = re.compile(
    r"(?i)\b(password|pgpassword)\s*=\s*[^\s,;]+"
)


class AlembicTimeout(RuntimeError):
    """The Alembic child exceeded its bounded execution time."""


class AlembicCommandError(RuntimeError):
    """A checked Alembic command exited non-zero."""


def build_child_env(
    target_url: str,
    *,
    include_migration_context: bool = False,
    run_id: str | None = None,
) -> dict[str, str]:
    """Return a minimal environment for one migration child process.

    The allowlist is matched case-insensitively so Windows' case-insensitive
    environment variables are preserved without copying the parent environment.
    Unknown variables -- including any future administrator secret -- are
    excluded by default.
    """
    allowed = {name.upper() for name in _OS_ENV_NAMES}
    if include_migration_context:
        allowed.update(name.upper() for name in _MIGRATION_ENV_NAMES)

    child: dict[str, str] = {}
    for name, value in os.environ.items():
        if name.upper() in allowed:
            child[name] = value

    child["DATABASE_URL"] = target_url
    if include_migration_context:
        child["KAI_TEST_MIGRATIONS_DATABASE_URL"] = target_url
        if run_id is not None:
            child["KAI_TEST_MIGRATIONS_RUN_ID"] = run_id
    return child


def sanitize_text(text: str, *secrets: str | None) -> str:
    """Remove URLs and known secret values from child output.

    Alembic and database drivers sometimes include a connection URL in an error.
    We never echo that raw.  The returned text is safe for a test failure, a CI
    log, or an operator message.
    """
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "<redacted>")
    cleaned = _URL_RE.sub("<redacted-url>", cleaned)
    return _PASSWORD_RE.sub(r"\1=<redacted>", cleaned)


def _validated_target(target_url: str | None) -> str:
    error = migration_run_target_error(target_url)
    if error is not None:
        raise RuntimeError(error)
    assert target_url is not None
    return target_url


def run_alembic(
    *args: str,
    target_url: str | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m alembic`` against one validated per-run target.

    Validation happens before ``subprocess.run``, so an unsafe target never
    creates a process.  The URL is passed through the child environment, never
    through argv, and the URL/password are scrubbed from timeout messages.
    """
    if not args:
        raise ValueError("an alembic command is required")

    target = _validated_target(target_url)
    env = build_child_env(target)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
            cwd=str(_REPO_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        raise AlembicTimeout(
            f"alembic {args[0]} exceeded {timeout:g}s; target is untrusted"
        ) from exc

    # Do not hand raw child output back to assertion messages or logs. Alembic
    # and the driver can print the connection URL (including its password) in a
    # traceback even when the command itself was addressed correctly.
    password = None
    with contextlib.suppress(Exception):
        password = make_url(target).password
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        sanitize_text(result.stdout or "", target, password),
        sanitize_text(result.stderr or "", target, password),
    )


def run_alembic_checked(
    *args: str,
    target_url: str | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run Alembic and raise a sanitized error on a non-zero exit."""
    target = _validated_target(target_url)
    result = run_alembic(*args, target_url=target, timeout=timeout)
    if result.returncode != 0:
        password = None
        with contextlib.suppress(Exception):
            password = make_url(target).password
        detail = sanitize_text(result.stderr or result.stdout or "", target, password)
        detail = detail.strip()
        if len(detail) > 800:
            detail = detail[:800] + "..."
        suffix = f": {detail}" if detail else ""
        raise AlembicCommandError(
            f"alembic {args[0]} failed with exit code {result.returncode}{suffix}"
        )
    return result
