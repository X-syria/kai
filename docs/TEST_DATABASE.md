# Test Database

This document describes the dedicated, isolated **test** PostgreSQL stack
defined in [`docker-compose.test.yml`](../docker-compose.test.yml).

> This document and the Compose file it describes do **not** create or start a
> database. They prepare configuration only. Isolation is **not** proven until
> it is verified at runtime (see [Runtime isolation is not
> proven by configuration](#runtime-isolation-is-not-proven-by-configuration)).

## Purpose

Tests must truncate every mutable table before and after each test (see the
`_clean_state` fixture in [`tests/conftest.py`](../tests/conftest.py)). Running
that suite against the development database would erase real local data.

To make that safe, `docker-compose.test.yml` stands up a **separate**
PostgreSQL instance that shares nothing with the development stack:

- a distinct service (`db-test`, not `db`),
- a distinct named volume (`kai_test_pgdata`, not `kai_pgdata`),
- a distinct host port (loopback `55432`, not `5432`),
- a distinct database name (`kai_test`),
- a dedicated **runtime role** (`kai_test`) that owns that database and its
  `public` schema, provisioned on first initialization by an init script from a
  separate **bootstrap admin** (`kai_test_admin`),
- credentials supplied only at run time, as file-backed secrets (never in the
  repository).

### Development stack vs. test stack

| | Development (`docker-compose.yml`) | Test (`docker-compose.test.yml`) |
|---|---|---|
| Service name | `db` | `db-test` |
| Container | `kai-db-1` (default project `kai`) | `kai-test-db-db-test-1` (project `kai-test-db`) |
| Named volume | `kai_pgdata` | `kai_test_pgdata` (Compose key; Docker volume `kai-test-db_kai_test_pgdata`) |
| Host port | `5432` (`${POSTGRES_PORT:-5432}`) | `55432` (`${KAI_TEST_POSTGRES_PORT:-55432}`) |
| Database | `kai` (`${POSTGRES_DB:-kai}`) | `kai_test` (`${KAI_TEST_POSTGRES_DB:-kai_test}`) |
| User | `kai` (`${POSTGRES_USER:-kai}`) | runtime `kai_test` (`${KAI_TEST_POSTGRES_USER:-kai_test}`); bootstrap admin `kai_test_admin` (`${KAI_TEST_POSTGRES_ADMIN_USER:-kai_test_admin}`) |
| Password | `POSTGRES_PASSWORD` (defaults to `kai`) | file secrets: `KAI_TEST_POSTGRES_ADMIN_PASSWORD_FILE` + `KAI_TEST_POSTGRES_PASSWORD_FILE` (**both required; they hold paths, not values**) |
| Other services | `redis`, `app`, `migrate`, `purge-revocations`, `dispatch-alerts` | none |

The test stack contains **only** what is needed to run PostgreSQL. It has no
application, no Redis, and no migrations service.

The table lists the Compose **key** `kai_test_pgdata`. Because the Compose file
sets `name: kai-test-db`, Docker namespaces that key with the project name, so
the volume actually created is `kai-test-db_kai_test_pgdata`. That exact name —
not the bare `kai_test_pgdata` — is what a runtime resource check must look for.

## Required and optional environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `KAI_TEST_POSTGRES_ADMIN_PASSWORD_FILE` | **Yes** | none (fail-closed) | Path to a LOCAL file holding the bootstrap admin password. **Never commit the file or its contents.** |
| `KAI_TEST_POSTGRES_PASSWORD_FILE` | **Yes** | none (fail-closed) | Path to a LOCAL file holding the `kai_test` runtime password. **Never commit the file or its contents.** |
| `KAI_TEST_POSTGRES_ADMIN_USER` | No | `kai_test_admin` | Bootstrap superuser created by the image for initialization only. Never used by KAI. |
| `KAI_TEST_POSTGRES_USER` | No | `kai_test` | Runtime role created by the init script; the account migrations and tests use. |
| `KAI_TEST_POSTGRES_PORT` | No | `55432` | Host port (loopback only) published to container port `5432`. |
| `KAI_TEST_POSTGRES_DB` | No | `kai_test` | Test database name. Must match the safety gate's allowlist (`^kai_test(?:_[A-Za-z0-9]+)*$`). |

### Two roles, two files — no repo secrets

The stack separates the **bootstrap admin** from the **runtime role**:

- `kai_test_admin` is the `POSTGRES_USER` superuser. The official `postgres`
  image uses it **only** to initialize a fresh cluster and to run the init
  script. It is **not** used by KAI connections, migrations, or tests.
- `kai_test` is a **limited runtime role** — `LOGIN NOSUPERUSER NOCREATEDB
  NOCREATEROLE NOREPLICATION` — created by
  [`docker/test-db-init/10-create-test-runtime-role.sh`](../docker/test-db-init/10-create-test-runtime-role.sh).
  It **owns** the `kai_test` database and the `public` schema and is the **only
  account** named in the test `DATABASE_URL` and used by migrations/tests.

Neither password is stored in the repository. `docker-compose.test.yml`
declares them as Compose **file-backed secrets** whose `file:` values are:

```yaml
${KAI_TEST_POSTGRES_ADMIN_PASSWORD_FILE:?...}
${KAI_TEST_POSTGRES_PASSWORD_FILE:?...}
```

Those environment variables hold **paths** to local password files, not the
secrets themselves. Compose's `:?` form is fail-closed: if either variable is
unset or empty, `docker compose` aborts with the message after `:?` instead of
substituting an empty value or a default. The same applies to
`docker compose ... config`, so a missing path is a loud configuration error,
not a silently-weak database. No password value is ever interpolated into this
file, and no password value is ever passed on a command line.

#### Create the two local password files yourself

Choose a location **outside the tracked repository** (for example, under
`$env:USERPROFILE`). Supply a strong random value from your password manager;
do not reuse the development password.

```powershell
# Outside the repository, e.g. C:\Users\<you>\kai-test-secrets\
$dir = "$env:USERPROFILE\kai-test-secrets"
New-Item -ItemType Directory -Path $dir -Force | Out-Null

# One file for the bootstrap admin, one for the runtime role.
Set-Content -LiteralPath "$dir\kai_test_admin_password" -NoNewline -Value "<admin password>"
Set-Content -LiteralPath "$dir\kai_test_password"       -NoNewline -Value "<runtime password>"

# Point Compose at the files by PATH (never by value).
$env:KAI_TEST_POSTGRES_ADMIN_PASSWORD_FILE = "$dir\kai_test_admin_password"
$env:KAI_TEST_POSTGRES_PASSWORD_FILE       = "$dir\kai_test_password"
```

Do **not** commit these files, do **not** pass the password values on the
command line, and do **not** place them inside the repository — a repo-local
copy would show up in `git status` and risk being committed.

## Bring it up (documented — do not run as part of this documentation step)

Set the two **path** variables from the previous section, then:

```sh
docker compose -f docker-compose.test.yml --project-name kai-test-db up -d
```

The file itself sets `name: kai-test-db`, so `--project-name kai-test-db` is
explicit and consistent with it. Use `down` to stop it (`down` without `-v`
keeps the volume). The `kai_test_pgdata` volume is separate from development
data; removing it is a destructive action that should be a deliberate choice,
never a test side effect.

### The init script runs only on a FRESH data directory

Everything under `/docker-entrypoint-initdb.d` (here,
[`10-create-test-runtime-role.sh`](../docker/test-db-init/10-create-test-runtime-role.sh))
is executed by the official `postgres` entrypoint **only when the data
directory is empty** — that is, on the first `up` against a brand-new
`kai_test_pgdata` volume. If `kai_test_pgdata` already exists, **the init script
does NOT re-run** and the runtime role is not (re)provisioned by it.

- Do **not** assume the volume is absent.
- Do **not** delete or replace an existing `kai_test_pgdata` volume.
- If the volume pre-exists without the runtime role, provision it through a
  **deliberate, separately-authorized** path — not as a side effect of this
  configuration, and not by casually removing the volume.

If re-provisioning from scratch is ever truly required, it must be an explicit,
authorized decision. `docker compose -f docker-compose.test.yml down`
(**without `-v`**) only stops the container and keeps the volume; never remove
the volume as a routine step.

### The healthcheck is a liveness probe, not a readiness or isolation proof

`docker-compose.test.yml` defines a `healthcheck` that runs
`pg_isready -U <admin> -d <db>`. `pg_isready` reports only whether the
PostgreSQL server is responding and accepting connections under those
arguments. A `healthy` container therefore means **only** that the server is
answering:

- it does **not** prove the runtime role `kai_test` exists, can log in, or holds
  the ownership/privileges the suite needs;
- it does **not** prove the init script finished provisioning successfully;
- it does **not** prove the container, volume, or port is the intended isolated
  test resource.

Treating `healthy` as proof of isolation would be wrong. Confirming the live
connection identity and the Compose resource is a separate, still-required
runtime step (see below).

## Point the test suite at it

The test `DATABASE_URL` uses the **runtime role** (`kai_test`), never the
bootstrap admin. Apply the normal schema with the same URL, then run the normal
suite with the truncation opt-in:

```sh
export DATABASE_URL=postgresql+asyncpg://kai_test:<runtime password>@127.0.0.1:55432/kai_test
export KAI_TEST_ALLOW_DB_TRUNCATE=1
alembic upgrade head                    # applies the schema to kai_test
pytest -q -m "not db_migration"         # normal, truncating suite
```

Notes:

- The runtime user is `kai_test`; the port `55432` and database name `kai_test`
  must match the values used by `docker-compose.test.yml` (or your overrides).
  `<runtime password>` is the value stored in
  `KAI_TEST_POSTGRES_PASSWORD_FILE`; the **admin** password is never used here.
- `KAI_TEST_ALLOW_DB_TRUNCATE=1` is the explicit opt-in the safety gate
  requires; without it the suite refuses to truncate.
- `alembic upgrade head` must be run with the **same** `DATABASE_URL`, so the
  schema lands on the test database, not the development one.
- Do **not** run bare `pytest`. Migration tests are a separate group and the
  conftest rejects a process that mixes them with the normal suite.

## Isolated migration tests (P2)

Migration tests mutate the schema, so they do **not** run against `kai_test`.
They run against one database created for the run:

`kai_test_migrations_<16 lowercase hex characters>`

The only supported entry point is:

```sh
python scripts/run_migration_tests.py
```

The orchestrator performs these steps without using the normal suite's
`TRUNCATE` path:

1. Reads the explicit normal `DATABASE_URL` and the administrator credentials.
2. Selects one explicit Docker context from `KAI_TEST_DOCKER_CONTEXT`, reads its
   endpoint and TLS material once with `docker context inspect`, and copies them
   into a private per-run Docker config directory. Every later command uses a
   pinned context name from that private directory, then verifies the selected
   daemon's id with `docker info`. It never infers routing from `DOCKER_HOST`,
   `DOCKER_CONTEXT`, `DOCKER_CONFIG`, `DOCKER_TLS_VERIFY`, or
   `DOCKER_CERT_PATH`, and it does not re-resolve the user's mutable source
   context after the snapshot.
3. Resolves the expected `system_identifier` from a trusted Docker anchor: the
   local Compose container (`kai-test-db` / `db-test`) or the CI service
   container id. It reads `pg_controldata`, not the URL, binds the value to the
   container id, and rejects an identity file whose `created_at` predates the
   container's start.
4. Re-inspects the named container and reads `pg_controldata` from that same
   container again. The value must equal the identity reference exactly, whether
   the reference came from an identity file or from an internal caller. A
   caller-supplied `identity` does not bypass this check.
5. Checks the administrator's `current_user`, `CREATEDB`, ability to assign
   `kai_test` ownership, and permission to read `pg_control_system()`.
6. Opens the same administrator connection used for those checks, reads the live
   `system_identifier`, compares it with the Docker reference, and only then
   creates the unique database. Any missing or mismatched anchor fails before
   `CREATE DATABASE`.
7. Sets and verifies `kai_test` database/schema ownership, writes a run-id
   provenance comment, and builds
   `KAI_TEST_MIGRATIONS_DATABASE_URL` by replacing only the database name.
8. Runs `alembic upgrade head` and `pytest -q -m db_migration` against that
   database only. `DATABASE_URL`, `KAI_TEST_MIGRATIONS_DATABASE_URL`, and every
   inspection helper use the same target; there is no
   `get_settings().database_url` fallback.
9. Verifies the revision and expected tables, reads the normal `kai_test`
   baseline again, and fails if its revision or table set changed.
10. Removes only the database it created, after proving exact name, owner,
    run-id comment, and zero active connections. It never uses
   `DROP ... FORCE`, never drops by name pattern, and never sweeps old
   databases. A failed run leaves the database in place and prints its name
   (never its URL or password) for investigation.

### Migration-test environment

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Normal `kai_test` target; also the source for the runtime credentials and host/port. |
| `KAI_TEST_POSTGRES_ADMIN_USER` | Bootstrap admin login, default `kai_test_admin`. |
| `KAI_TEST_POSTGRES_ADMIN_PASSWORD` | Admin password value (CI-only throwaway); alternatively use `KAI_TEST_POSTGRES_ADMIN_PASSWORD_FILE`. |
| `KAI_TEST_PG_CONTAINER_ID` | Trusted Docker container id. Required in CI; optional locally when Compose discovery can identify `db-test`. |
| `KAI_TEST_PG_IDENTITY_FILE` | JSON file with `container_id`, `system_identifier`, and `created_at`, produced from the trusted container. The orchestrator re-reads `pg_controldata` from that container and rejects any mismatch. |
| `KAI_TEST_PG_ANCHOR_KIND` | `ci` to verify the service container id/image/port without Compose labels; `compose` to require project/service/config-file/volume labels. |
| `KAI_TEST_DOCKER_CONTEXT` | **Required.** The explicit Docker context to snapshot once at startup. Use `default` for the standard local/CI daemon, or the name reported by `docker context show`. |
| `KAI_TEST_DOCKER_CONFIG` | Optional. Source Docker config directory used only for the one-time `docker context inspect`. If unset, Docker's normal `~/.docker` config is used. All later commands use a private per-run copy, not this directory; ambient `DOCKER_CONFIG` is never inherited. |
| `KAI_TEST_KEEP_MIGRATION_DB` | `1`/`true` leaves the run database for investigation. |
| `KAI_TEST_MIGRATIONS_RUN_ID` | Set by the orchestrator for pytest; must equal the suffix of the per-run database. |

`KAI_TEST_DOCKER_CONTEXT` is required even for local runs. Set it explicitly
(for example, `export KAI_TEST_DOCKER_CONTEXT=default`) instead of exporting
`DOCKER_HOST` or `DOCKER_CONTEXT`; the orchestrator ignores those ambient
routing variables. The selected context is snapshotted once into a private
temporary Docker config (including its endpoint and any TLS material), and every
later command uses that pinned copy. Changing the original context definition
after startup cannot silently redirect later commands. The orchestrator then
runs `docker --context <pinned> info`, records the daemon id, and re-verifies it
before every subsequent Docker command. A missing context, an unreadable
snapshot, an unreadable daemon id, or a daemon change between the container
inspection, `pg_controldata` read, or later checks aborts before provisioning.
The temporary config directory is removed at normal process exit. The snapshot
protects against later changes to the source context; it does not claim to protect
the one-time copy from a local actor who can already modify the user's Docker config
or the temporary directory with the same privileges.

The orchestrator keeps administrator credentials in its own process. Alembic
and pytest children receive only an explicit allowlist: the target URL, the run
id for the migration group, and the OS/Python variables needed to start. They do
not receive admin credentials, `PGPASSWORD`, `PGPASSFILE`, or
`KAI_TEST_ALLOW_DB_TRUNCATE`. The Alembic runner has a 120-second default
timeout; a timeout or failed restore marks the group untrusted and prevents a
second automatic restore attempt.

The Docker identity file and the P2 source/test files are currently untracked.
They must be added to the tracked tree before CI can use this flow. No live
Docker, PostgreSQL, or Alembic execution has been performed for P2.

## Compatibility with the safety gate in `tests/conftest.py`

The suite's destructive fixture consults three pure decision functions before it
opens any session. This configuration is designed to satisfy all three
**without weakening them**.

### `truncate_authorization_error(database_url, allow_flag)`

Truncation is authorized only when **every** condition holds:

1. `KAI_TEST_ALLOW_DB_TRUNCATE` is truthy (`1`/`true`/`yes`/`on`,
   case-insensitive); absent or false refuses.
2. `DATABASE_URL` is set and non-empty.
3. It parses and its backend is PostgreSQL (`postgresql` /
   `postgresql+asyncpg`); sqlite and anything else are refused.
4. It names a database (non-empty database component).
5. The database name is not the built-in default `kai`.
6. The name matches `^kai_test(?:_[A-Za-z0-9]+)*$` — merely containing "test"
   is not enough (`mytest` and `kai_testing` are refused).
7. The name is not a per-run `kai_test_migrations_<16 hex>` database; those are
   reserved for the migration orchestrator.
8. (Binding check, see below.)

### `effective_target_error(declared_url, effective_url)`

Returns `None` only when the declared `DATABASE_URL` and the SQLAlchemy engine's
**effective** URL (`engine.url`) are the same connection target. Every component
must match: `drivername`, `username`, `password`, `host`, `port`, `database`,
and `query`. A mismatch is refused, never "fixed" by rebuilding the engine.

### `runtime_role_error(effective_url)`

Returns `None` only when the engine's **effective** connection user
(`engine.url.username`) is the dedicated, non-superuser runtime role `kai_test`.
A superuser (for example the bootstrap `kai_test_admin`) or any other login is
refused even when the database name is allowlisted, and a connection that names
no user is refused. This is a least-privilege check on the identity the suite
will actually connect with; it does **not** prove the cluster or host identity.

### How this configuration satisfies the gate

- The documented `DATABASE_URL` uses the `postgresql+asyncpg` driver, so
  condition 3 holds.
- Its database component is `kai_test`, so conditions 4, 5, and 6 hold (it is
  neither `kai` nor a non-allowlisted name), and condition 7 holds because it is
  not a per-run migration database.
- Its user is the runtime role `kai_test`, so `runtime_role_error` holds. The
  bootstrap `kai_test_admin` account is never named in the test `DATABASE_URL`
  and would be refused if it were.
- `KAI_TEST_ALLOW_DB_TRUNCATE=1` supplies condition 1.
- Because the suite builds its engine from the `DATABASE_URL` environment
  variable, supplying that single value for the run makes the declared URL and
  `engine.url` identical, so condition 8 holds.

No condition was relaxed: the gate still refuses everything it refused before,
and it additionally refuses a non-runtime login. The configuration aims at a
target the gate accepts.

## Runtime isolation is not proven by configuration

> **Warning.** `DATABASE_URL` matching `engine.url` proves only that the *suite
> and the engine agree on a connection target*. It does **not** prove that the
> target is isolated or that it is the intended test database.

Before running any test that may execute `TRUNCATE` — and especially before
using production-shaped credentials — perform a **separate, independent
runtime verification** of the connected instance and database identity, for
example:

- confirm `current_database()` on the live connection returns `kai_test`;
- confirm host/instance identity (the server you actually connected to), e.g.
  via the server's system identifier or `version()`. Note that
  `inet_server_addr()`/`inet_server_port()` report the server's own view (inside
  the container), not the host-side published mapping, so they do **not** prove
  that a host connection used `127.0.0.1:55432` — that is a Compose resource
  check, not a SQL one;
- confirm that the credentials you used are scoped to the test instance and not
  shared with development or production.

Matching names alone are not evidence of isolation; a mislabelled host, a shared
cluster, or over-broad credentials can still make two "different" names reach
the same data. Matching a database name and a runtime role does not prove the
cluster or host identity either — confirming the intended container, volume, and
port mapping is a separate Compose resource check.

## Status

This document and the Compose files add **configuration, documentation, and an
init script only**. They do not create, start, migrate, or truncate any
database, and they do not change any environment variable or secret.

P2 adds source-level orchestration and unit tests for the isolated migration
database. It does not execute Docker, PostgreSQL, Alembic, or a live migration
test; the Docker/PostgreSQL/Alembic behavior remains unverified until an
authorized runtime check is performed.

Static remediation (Phase 2D.10) tightened three things without any runtime
action: the init script now fails closed on an empty or blank runtime password
file; the safety gate now also requires the engine to connect as the `kai_test`
runtime role; and the `healthcheck` is documented as a liveness probe only.
These strengthen least privilege and refuse an invalid secret, but they
**do not** prove isolation.

Separating the bootstrap admin from the limited runtime role reduces the blast
radius of the account tests use, but it is **not** a substitute for container,
volume, and port isolation. The runtime verification described above — Step 1.9
— remains a **separate, still-required, not-yet-proven** step.
