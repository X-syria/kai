#!/bin/sh
#
# Provision the LIMITED runtime role for the dedicated TEST PostgreSQL stack.
#
# Runs ONCE, during image initialization, on a FRESH data directory. The
# official postgres image executes everything in /docker-entrypoint-initdb.d
# only when the data directory is empty; if the `kai_test_pgdata` volume
# already exists this script does NOT run again (see docs/TEST_DATABASE.md).
#
# Why this exists: the official postgres image makes POSTGRES_USER a superuser.
# That is acceptable for a bootstrap admin used only to initialize the cluster,
# but it must NOT be the account KAI connects with. This script creates a
# dedicated, least-privilege runtime role and gives it exactly what it needs to
# own the test schema:
#
#   * CREATE ROLE ... LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
#   * own the test database    (ALTER DATABASE ... OWNER TO ...)
#   * own the public schema    (ALTER SCHEMA public OWNER TO ...)
#
# Secret handling: the runtime password is read from a file (a Docker secret),
# loaded into a psql client variable via \getenv, and emitted with :'runtime_pw'
# so psql performs the string quoting. The password is NEVER printed, NEVER
# placed in argv, and NEVER embedded as a literal in the SQL text.
#
# Inputs (supplied by docker-compose.test.yml):
#   POSTGRES_USER                    bootstrap admin (superuser)
#   POSTGRES_DB                      database handed to the runtime role
#   KAI_TEST_RUNTIME_USER            runtime role name (default: kai_test)
#   KAI_TEST_POSTGRES_PASSWORD_FILE  path to the runtime password file
#
# This script touches only this test instance's own database and role. It makes
# no reference to the development database (kai), its volume, or any other
# resource.

set -eu

runtime_user="${KAI_TEST_RUNTIME_USER:-kai_test}"
runtime_db="${POSTGRES_DB:-kai_test}"
password_file="${KAI_TEST_POSTGRES_PASSWORD_FILE:-/run/secrets/kai_test_password}"

# Fail closed if the secret file is missing or unreadable. Never echo it.
if [ ! -r "$password_file" ]; then
  echo "test-db-init: runtime password file is not readable: $password_file" >&2
  exit 1
fi

# Load the secret into this process's environment ONLY, so psql's \getenv can
# read it client-side. It never appears in argv or in the SQL text.
KAI_TEST_RUNTIME_PASSWORD="$(cat "$password_file")"

# Fail closed if the secret is empty or consists solely of whitespace. The
# readability check above only covers a missing or unreadable file, so an
# existing-but-blank file would otherwise provision the runtime role with a
# blank password and complete initialization without an explicit refusal. The
# POSIX `case` test below matches any value containing at least one
# non-whitespace character; it needs no subprocess and never echoes the value.
case "$KAI_TEST_RUNTIME_PASSWORD" in
  *[![:space:]]*) ;;
  *)
    unset KAI_TEST_RUNTIME_PASSWORD
    echo "test-db-init: runtime password file is empty or blank; aborting initialization" >&2
    exit 1
    ;;
esac

export KAI_TEST_RUNTIME_PASSWORD

# Provision the runtime role and hand it ownership of its database and schema.
#   \getenv runtime_pw KAI_TEST_RUNTIME_PASSWORD  loads the secret into the psql
#       variable `runtime_pw` without it ever entering argv or the SQL text.
#   :'runtime_pw'  -> psql emits a safely-quoted string literal.
#   :"runtime_user" / :"runtime_db" -> psql emits safely-quoted identifiers.
if psql \
      --username "$POSTGRES_USER" \
      --dbname "$runtime_db" \
      -v ON_ERROR_STOP=1 \
      -v runtime_user="$runtime_user" \
      -v runtime_db="$runtime_db" <<'SQL'
\getenv runtime_pw KAI_TEST_RUNTIME_PASSWORD
CREATE ROLE :"runtime_user" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'runtime_pw';
ALTER DATABASE :"runtime_db" OWNER TO :"runtime_user";
ALTER SCHEMA public OWNER TO :"runtime_user";
GRANT ALL ON SCHEMA public TO :"runtime_user";
SQL
then
  # Success: drop the secret from this process's environment immediately.
  unset KAI_TEST_RUNTIME_PASSWORD
else
  status=$?
  unset KAI_TEST_RUNTIME_PASSWORD
  echo "test-db-init: failed to provision runtime role; aborting initialization" >&2
  exit "$status"
fi
