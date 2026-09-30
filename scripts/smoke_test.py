"""Production smoke test: build, migrate, serve, restart, persist.

Run from the repository root:

    python scripts/smoke_test.py

This exercises the deployment path a real release takes, against a throwaway
database, and asserts the outcome at each step. It is not a unit test and it
never calls Google or the planner: it checks that the artifact starts, reaches
its schema, serves traffic, and keeps data across a restart.

Exit code 0 means every gate below passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

COMPOSE = ["docker", "compose"]
HEALTH_URL = "http://127.0.0.1:8000/api/v1/health"
REGISTER_URL = "http://127.0.0.1:8000/api/v1/auth/register"
DB_NAME = "kai_smoke"
DB_USER = "kai"
EXPECTED_HEAD = "c8e1f3a5b7d9"

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} ===")


def ok(detail: str) -> None:
    print(f"  PASS  {detail}")


def fail(detail: str) -> None:
    print(f"  FAIL  {detail}")
    failures.append(f"{name_of[0]}: {detail}")


name_of = ["?"]


def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)  # type: ignore[arg-type]


def wait_for_health(timeout: float = 90.0) -> bool:
    """Poll the readiness endpoint until it reports the database reachable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=4) as response:
                if response.status == 200:
                    body = json.loads(response.read())
                    if body.get("database") == "reachable":
                        return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(2)
    return False


def psql(sql: str, database: str) -> str:
    result = run(
        ["docker", "exec", "kai-db-1", "psql", "-U", DB_USER, "-d", database, "-t", "-A", "-c", sql]
    )
    return result.stdout.strip()


def main() -> int:
    # 1. Build -----------------------------------------------------------------
    name_of[0] = "build"
    step("1. Build image")
    if run(COMPOSE + ["build", "app"]).returncode != 0:
        fail("docker compose build app did not succeed")
    else:
        ok("image built")

    # 2. Database --------------------------------------------------------------
    name_of[0] = "database"
    step("2. Start database")
    # Self-contained: the script brings up its own database rather than assuming
    # a compose stack is already running. POSTGRES_DB is set first so the db
    # container, the migrate job and the app all agree on the same database.
    os.environ["POSTGRES_DB"] = DB_NAME
    run(COMPOSE + ["up", "-d", "db"])
    db_up = False
    for _ in range(30):
        probe = run(["docker", "exec", "kai-db-1", "pg_isready", "-U", DB_USER, "-d", DB_NAME])
        if probe.returncode == 0:
            db_up = True
            break
        time.sleep(2)
    if not db_up:
        fail("database never became ready")
    else:
        ok("database accepting connections")
    run(["docker", "exec", "kai-db-1", "psql", "-U", DB_USER, "-d", "postgres", "-c",
         f'DROP DATABASE IF EXISTS {DB_NAME};'])
    created = run(["docker", "exec", "kai-db-1", "psql", "-U", DB_USER, "-d", "postgres", "-c",
                   f'CREATE DATABASE {DB_NAME};'])
    if created.returncode != 0:
        fail(f"could not create throwaway database {DB_NAME}")
    else:
        ok(f"throwaway database {DB_NAME} created")

    # 3. Migrations ------------------------------------------------------------
    name_of[0] = "migrations"
    step("3. Apply migrations")
    os.environ["POSTGRES_DB"] = DB_NAME
    migrated = run(COMPOSE + ["run", "--rm", "migrate"])
    if migrated.returncode != 0:
        fail("migrate service did not succeed")
    else:
        ok("migrate service exited 0")
    revision = psql("SELECT version_num FROM alembic_version;", DB_NAME)
    if revision == EXPECTED_HEAD:
        ok(f"database at expected head {EXPECTED_HEAD}")
    else:
        fail(f"expected head {EXPECTED_HEAD}, found {revision or 'nothing'}")
    table_count = psql(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';", DB_NAME
    )
    if table_count and int(table_count) >= 10:
        ok(f"{table_count} tables present")
    else:
        fail(f"expected at least 10 tables, found {table_count or 0}")

    # 4. Serve -----------------------------------------------------------------
    # NOTE: `docker compose up` has no `-e` flag (only `docker compose run`
    # does), so the target database is selected the way compose actually reads
    # it: the POSTGRES_DB substitution. Setting it in this process's
    # environment makes the db service, the migrate job and the app all point at
    # the same throwaway database.
    name_of[0] = "startup"
    step("4. Start application")
    os.environ["POSTGRES_DB"] = DB_NAME
    up = run(COMPOSE + ["up", "-d", "app"])
    if up.returncode != 0:
        fail(f"app service did not start: {up.stderr.strip()[:300]}")
    else:
        ok("app container started")

    # 5. Health ----------------------------------------------------------------
    name_of[0] = "health"
    step("5. Health check")
    if wait_for_health():
        ok(f"{HEALTH_URL} reports database reachable")
    else:
        fail("application never became healthy")

    # 6. API responds ----------------------------------------------------------
    name_of[0] = "api"
    step("6. API accepts a write")
    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    payload = json.dumps({"email": email, "password": "smoke-test-password"}).encode()
    request = urllib.request.Request(
        REGISTER_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status == 201:
                ok(f"register returned 201 for {email}")
            else:
                fail(f"register returned {response.status}, expected 201")
    except urllib.error.HTTPError as exc:
        fail(f"register failed with HTTP {exc.code}: {exc.read()[:200]!r}")
    except OSError as exc:
        fail(f"register could not be reached: {exc}")

    # 7. Restart and persist ---------------------------------------------------
    name_of[0] = "persistence"
    step("7. Restart and confirm state survives")
    before = psql("SELECT count(*) FROM users;", DB_NAME)
    run(COMPOSE + ["restart", "app"])
    if not wait_for_health():
        fail("application did not recover after restart")
    else:
        ok("application healthy again after restart")
    after = psql("SELECT count(*) FROM users;", DB_NAME)
    if before and before == after:
        ok(f"user count unchanged across restart ({after})")
    else:
        fail(f"state did not survive restart: {before or '?'} -> {after or '?'}")

    # Cleanup ------------------------------------------------------------------
    run(["docker", "exec", "kai-db-1", "psql", "-U", DB_USER, "-d", "postgres", "-c",
         f'DROP DATABASE IF EXISTS {DB_NAME};'])

    # Verdict ------------------------------------------------------------------
    print("\n" + "=" * 62)
    if failures:
        print(f"SMOKE TEST FAILED ({len(failures)} gate(s) failed)")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("SMOKE TEST PASSED - all gates green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
