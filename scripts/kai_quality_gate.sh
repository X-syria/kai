#!/usr/bin/env bash
set -euo pipefail

# Local/CI gate. Database-dependent checks are explicit; never silently skipped.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python -m compileall -q src tests
python scripts/kai_baseline.sh

if ! command -v ruff >/dev/null 2>&1 || ! command -v mypy >/dev/null 2>&1 || ! command -v pytest >/dev/null 2>&1; then
  echo 'BLOCKED: install project dev dependencies before running the full gate' >&2
  exit 10
fi

ruff check .
mypy
pytest -q -m 'not db_migration'

if [[ "${KAI_RUN_MIGRATION_TESTS:-0}" == "1" ]]; then
  python scripts/run_migration_tests.py
else
  echo 'NOTICE: migration tests not run; set KAI_RUN_MIGRATION_TESTS=1 in an isolated PostgreSQL environment'
fi
