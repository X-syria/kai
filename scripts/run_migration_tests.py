"""Run the isolated Alembic migration test group.

This is the only supported entry point for ``pytest -m db_migration``.  It
provisions a unique database, runs the migrations and migration tests against
that database only, verifies the normal test database was untouched, and cleans
up only the database created by this process.

It is intentionally not run by the ordinary unit-test command.  See
``docs/TEST_DATABASE.md`` for the required environment and CI flow.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

# Running ``python scripts/run_migration_tests.py`` puts ``scripts/`` first on
# sys.path, not the repository root.  Make the editable source importable when
# the package is not installed in the active interpreter.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kai.db.migration_orchestrator import (  # noqa: E402
    IdentityError,
    MigrationOrchestrationError,
    ProvisioningError,
    load_config,
    run_migration_suite,
)
from kai.db.migration_subprocess import sanitize_text  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision and run the isolated Alembic migration tests."
    )
    parser.add_argument(
        "--keep-database",
        action="store_true",
        help="leave the generated database in place for investigation",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
        if args.keep_database:
            config = replace(config, keep_database=True)
        return run_migration_suite(config)
    except (IdentityError, ProvisioningError, MigrationOrchestrationError) as exc:
        detail = sanitize_text(str(exc))
        print(f"migration test orchestration failed: {detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Never dump a driver traceback that may contain a connection URL or a
        # password. Unknown failures are reported through the same scrubber.
        detail = sanitize_text(str(exc))
        print(f"migration test orchestration failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
