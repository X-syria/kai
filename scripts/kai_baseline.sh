#!/usr/bin/env bash
set -euo pipefail

# Safe handover baseline. Never prints secret values.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "== KAI baseline =="
printf 'branch: '; git branch --show-current
printf 'commit: '; git log -1 --oneline
printf 'python: '; python --version
printf 'changed files: '; git status --short | wc -l

echo "-- migration heads --"
if command -v alembic >/dev/null 2>&1; then
  alembic heads || { echo 'BLOCKED: alembic heads failed'; exit 2; }
else
  echo 'BLOCKED: alembic is not installed'
fi

echo "-- secret file policy --"
if [[ -f .env ]]; then
  echo 'BLOCKED: .env exists in the working tree; do not package or commit it'
  exit 3
fi
if git ls-files | grep -E '(^|/)\.env$|\.pem$|\.key$' >/dev/null; then
  echo 'BLOCKED: tracked secret-like file detected'
  exit 4
fi

echo 'baseline checks complete'
