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
METRICS_URL = "http://127.0.0.1:8000/api/v1/metrics"
DB_NAME = "kai_smoke"
DB_USER = "kai"
EXPECTED_HEAD = "e6f7a8b9c0d1"

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


def _status(request: urllib.request.Request) -> int:
    """Return the HTTP status of ``request``, treating any HTTP error as a status.

    A 401 or 429 is the expected outcome in several gates here, so an
    ``HTTPError`` is a result rather than a failure of the probe.
    """
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except OSError:
        return 0


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
    # The app container from a previous run is still connected to the throwaway
    # database, and PostgreSQL refuses to drop a database that has live
    # connections. Left in place, the DROP fails silently and the CREATE below
    # then reports "already exists", which looks like a broken stack rather than
    # a leftover from the last run.
    run(COMPOSE + ["rm", "-sf", "app"])
    dropped = run(["docker", "exec", "kai-db-1", "psql", "-U", DB_USER, "-d", "postgres", "-c",
                   f'DROP DATABASE IF EXISTS {DB_NAME};'])
    if dropped.returncode != 0:
        fail(f"could not drop the previous {DB_NAME}: {dropped.stderr.strip()[:200]}")
    created = run(["docker", "exec", "kai-db-1", "psql", "-U", DB_USER, "-d", "postgres", "-c",
                   f'CREATE DATABASE {DB_NAME};'])
    if created.returncode != 0:
        fail(f"could not create throwaway database {DB_NAME}: {created.stderr.strip()[:200]}")
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
    register_token: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status == 201:
                ok(f"register returned 201 for {email}")
                register_token = json.loads(response.read()).get("access_token")
                if not register_token:
                    fail("register returned no access_token")
            else:
                fail(f"register returned {response.status}, expected 201")
    except urllib.error.HTTPError as exc:
        fail(f"register failed with HTTP {exc.code}: {exc.read()[:200]!r}")
    except OSError as exc:
        fail(f"register could not be reached: {exc}")

    # 7. Security: logout really revokes --------------------------------------
    # Asserted over real HTTP against the running container, because the point
    # of revocation is that a request presenting a revoked token is refused; a
    # helper-level check would not prove the middleware ever sees it.
    name_of[0] = "revocation"
    step("7. Logout revokes the access token")
    if register_token is None:
        fail("cannot test revocation: registration did not return a token")
    else:
        auth = {"Authorization": f"Bearer {register_token}"}
        me_url = "http://127.0.0.1:8000/api/v1/auth/me"
        logout_url = "http://127.0.0.1:8000/api/v1/auth/logout"

        before = _status(urllib.request.Request(me_url, headers=auth))
        if before == 200:
            ok("token authenticates before logout")
        else:
            fail(f"token was rejected before logout (HTTP {before})")

        out = _status(urllib.request.Request(logout_url, data=b"", headers=auth))
        if out == 200:
            ok("logout accepted")
        else:
            fail(f"logout returned HTTP {out}, expected 200")

        after = _status(urllib.request.Request(me_url, headers=auth))
        if after == 401:
            ok("revoked token is refused afterwards")
        else:
            fail(f"revoked token still authenticated (HTTP {after}, expected 401)")

        revoked_rows = psql("SELECT count(*) FROM revoked_tokens;", DB_NAME)
        if revoked_rows and revoked_rows.strip() not in ("", "0"):
            ok("revocation persisted in the database")
        else:
            fail("no revoked_tokens row was written")

    # 8. Observability and the purge job ---------------------------------------
    # Asserted over the running container rather than in-process, because the
    # claim under test is that these work as deployed: the route is not mounted
    # unless asked for, every response carries a correlation id, and the one-shot
    # command an external scheduler will actually invoke exits 0.
    name_of[0] = "observability"
    step("8. Observability and revocation purge")

    metrics_code = _status(urllib.request.Request(METRICS_URL))
    if metrics_code == 404:
        ok(f"{METRICS_URL} is not mounted while METRICS_ENABLED is false")
    else:
        fail(
            f"metrics route answered HTTP {metrics_code} without METRICS_ENABLED; "
            "an unauthenticated route must be opt-in and must not leak its shape"
        )

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=15) as response:
            request_id = response.headers.get("X-Request-ID")
        if request_id:
            ok(f"every response carries an X-Request-ID ({request_id})")
        else:
            fail("no X-Request-ID response header")
    except (urllib.error.URLError, OSError) as exc:
        fail(f"could not read response headers: {exc}")

    purged = run(COMPOSE + ["run", "--rm", "purge-revocations"])
    if purged.returncode == 0:
        ok("kai-purge-revocations exited 0")
    else:
        fail(f"purge job exited {purged.returncode}: {purged.stderr.strip()[:300]}")

    if purged.returncode == 0:
        still_there = psql("SELECT count(*) FROM revoked_tokens;", DB_NAME)
        if still_there and still_there.strip() not in ("", "0"):
            ok("purge left the unexpired revocation in place")
        else:
            fail(
                "purge deleted a revocation whose token had not expired; "
                "a revoked token must stay revoked until its own exp passes"
            )

    # 9. Restart and persist ---------------------------------------------------
    name_of[0] = "persistence"
    step("9. Restart and confirm state survives")
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
