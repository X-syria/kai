"""The scheduled job's contract is its exit code, so it is tested as one.

``deploy/scheduler/run-purge-revocations.sh`` is the only thing standing between
the operator's cron and the purge command. The command's own tests prove it
purges correctly; these prove the wrapper preserves that result faithfully.

Two properties matter and neither is visible from the Python side:

* **Exit codes survive.** A run that failed must not be reported as success to
  the scheduler, and a run that lost the advisory lock must be reported as
  success. Inverting either turns a 15-minute job into a source of noise or of
  silence.
* **Every run is recorded.** "The purge stopped running" is only detectable if
  something records that it ran, so a successful run must emit a record too and
  not only the failing ones.

The tests run the real script through ``sh`` with ``docker`` and ``timeout``
replaced by stubs. No test container, no database, no network, and no real
destructive command is reachable from here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "deploy" / "scheduler" / "run-purge-revocations.sh"
CRONTAB = REPO_ROOT / "deploy" / "scheduler" / "kai.cron"

#: Exit codes the wrapper must map to each recorded status.
EXPECTED_STATUS = {
    0: "ok",
    1: "failed",
    2: "failed",
    124: "timeout",
    137: "timeout",
}

_DOCKER_STUB = """#!/bin/sh
exit "${STUB_EXIT:-0}"
"""

#: A stand-in for GNU timeout. The script must detect this one, so it has to be
#: named exactly as the real thing. It has to consume every argument the script
#: passes -- both the long options and the bare duration -- before exec'ing the
#: rest, or it would exec the duration as if it were the command.
_TIMEOUT_STUB = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    --signal=*|--kill-after=*|[0-9]*) shift ;;
    *) break ;;
  esac
done
exec "$@"
"""


def _has_gnu_timeout() -> bool:
    """Would the script find a *GNU* timeout when it runs?

    Probed through ``sh`` rather than ``shutil.which`` on purpose. The script
    resolves ``timeout`` using the POSIX PATH of the shell that runs it, so
    asking Python would test a different PATH than the one that matters: on
    Windows, ``shutil.which("timeout")`` finds ``timeout.exe``, which waits for
    a keypress and accepts none of the flags the script passes.
    """
    if shutil.which("sh") is None:
        return False
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["sh", "-c", "timeout --version 2>&1 | head -1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "GNU coreutils" in (probe.stdout + probe.stderr)


requires_posix_tools = pytest.mark.skipif(
    shutil.which("sh") is None or not _has_gnu_timeout(),
    reason="POSIX sh and GNU timeout are required to run the scheduler script",
)


def _posix_path(path: Path) -> str | None:
    """Express ``path`` the way the POSIX shell that runs the script sees it.

    On Linux this is the path itself. On Windows under Git Bash the script runs
    under an MSYS shell whose PATH is POSIX-separated and rooted, so a
    ``C:/Users/...`` entry is not resolvable and every stub invocation dies with
    127. ``cygpath`` performs that translation. ``None`` means this host cannot
    run the script at all, and the caller should skip rather than assert.
    """
    raw = str(path)
    if os.name != "nt":
        return raw
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["cygpath", "-u", raw], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return proc.stdout.strip() or None


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Create stub ``docker`` and ``timeout``, and return the stub directory.

    The directory is handed to the shell rather than prepended to this
    process's PATH. The two need different PATH formats - Windows resolves
    ``sh.exe`` with ``;`` separators, while the shell resolves the stubs with
    ``:`` - so rebuilding one PATH to satisfy both breaks the other.

    The execute bit is set through ``sh`` rather than ``Path.chmod``. On Windows
    ``chmod`` only toggles the read-only attribute, so a stub written that way
    is not executable and every invocation fails with 127.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    written: list[Path] = []
    for name, body in (("docker", _DOCKER_STUB), ("timeout", _TIMEOUT_STUB)):
        target = bindir / name
        target.write_text(body, encoding="utf-8")
        written.append(target)

    posix_bindir = _posix_path(bindir)
    if posix_bindir is None:
        pytest.skip("no POSIX path translation available (cygpath missing)")

    chmod = subprocess.run(  # noqa: S603 - fixed argv via sh, no user input
        ["sh", "-c", 'chmod +x "$@"', "sh", *[_posix_path(p) or str(p) for p in written]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert chmod.returncode == 0, chmod.stderr

    monkeypatch.setenv("KAI_TEST_STUB_DIR", posix_bindir)
    return posix_bindir


#: Runs the script with the stub directory first on the *shell's* PATH.
_RUNNER = 'PATH="$KAI_TEST_STUB_DIR:$PATH"; exec sh "$KAI_TEST_SCRIPT"'


def _run(
    stubbed: str, script_env_exit: int, extra_env: dict[str, str] | None = None
) -> tuple[int, str]:
    env = dict(os.environ)
    env["STUB_EXIT"] = str(script_env_exit)
    env["KAI_TEST_STUB_DIR"] = stubbed
    env["KAI_TEST_SCRIPT"] = _script_path()
    env.update(extra_env or {})
    proc = subprocess.run(  # noqa: S603 - fixed argv via sh -c, no user input
        ["sh", "-c", _RUNNER], capture_output=True, text=True, env=env, check=False
    )
    return proc.returncode, proc.stdout


def _script_path() -> str:
    posix = _posix_path(SCRIPT)
    return posix if posix is not None else str(SCRIPT)


def _trace(stubbed: str, exit_code: int) -> str:
    """Run the script under ``sh -x`` and return everything it did."""
    env = dict(os.environ)
    env["STUB_EXIT"] = str(exit_code)
    env["KAI_TEST_STUB_DIR"] = stubbed
    env["KAI_TEST_SCRIPT"] = _script_path()
    proc = subprocess.run(  # noqa: S603 - fixed argv via sh -c, no user input
        ["sh", "-c", 'PATH="$KAI_TEST_STUB_DIR:$PATH"; exec sh -x "$KAI_TEST_SCRIPT"'],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc.stdout + proc.stderr


def test_the_script_is_valid_posix_sh() -> None:
    """A syntax error in a cron entrypoint is found at 03:00, not in review."""
    if shutil.which("sh") is None:
        pytest.skip("sh is unavailable")
    proc = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
        ["sh", "-n", _script_path()], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


@requires_posix_tools
@pytest.mark.parametrize(("code", "status"), sorted(EXPECTED_STATUS.items()))
def test_exit_code_is_preserved_and_recorded(stubbed: str, code: int, status: str) -> None:
    returned, stdout = _run(stubbed, code)
    assert returned == code, "the scheduler must see the command's own exit code"
    record = json.loads(stdout.strip().splitlines()[-1])
    assert record["status"] == status
    assert record["job"] == "kai_purge_revocations"
    assert record["exit_code"] == str(code)


@requires_posix_tools
def test_every_run_emits_exactly_one_parseable_record(stubbed: str) -> None:
    """Silence is how a stopped job stays invisible, so success must log too."""
    for code in EXPECTED_STATUS:
        _, stdout = _run(stubbed, code)
        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected one record for exit {code}, got {lines}"
        record = json.loads(lines[0])
        assert record["event"] == "scheduler_run"
        assert record["project"]


@requires_posix_tools
def test_lock_not_acquired_is_success_not_failure(stubbed: str) -> None:
    """An overlapping run exits 0 and must never be recorded as a fault.

    The purge command returns 0 when another run holds the advisory lock, which
    is the scheduler working as designed. If this wrapper ever recorded that as
    ``failed`` it would page an operator every time two windows collided.
    """
    returned, stdout = _run(stubbed, 0)
    assert returned == 0
    assert json.loads(stdout.strip())["status"] == "ok"


@requires_posix_tools
def test_compose_project_is_taken_from_the_environment(stubbed: str) -> None:
    """The job must reach the deployment's stack, not create a second one.

    A purge pointed at the wrong Compose project deletes nothing and exits 0,
    so it looks like it worked. The project name is therefore recorded on every
    run, and is overridable without editing the file.
    """
    _, stdout = _run(stubbed, 0, {"KAI_COMPOSE_PROJECT_NAME": "kai-prod-eu"})
    assert json.loads(stdout.strip())["project"] == "kai-prod-eu"


@requires_posix_tools
def test_the_job_runs_the_purged_service_not_the_api(stubbed: str) -> None:
    """A scheduled run must not start the API service.

    The wrapper drives ``docker compose run purge-revocations``, which is the
    same image with a different entrypoint. Invoking ``up``/``start`` here would
    leave a service running after the cron job finished, which is the one way a
    "one-shot" scheduled job can quietly become a daemon.
    """
    combined = _trace(stubbed, 0)
    assert "docker compose" in combined
    assert "run --rm -T purge-revocations" in combined
    for forbidden in (
        "compose up",
        "compose start",
        "compose restart",
        "compose down",
        "compose logs",
        "compose kill",
    ):
        assert forbidden not in combined, f"scheduler must never run `{forbidden}`"


def test_crontab_invokes_the_wrapper_every_fifteen_minutes() -> None:
    """The cadence lives in the crontab, never in Python.

    ``*/15 * * * *`` is a 15-minute cadence in every timezone that exists, because
    every real UTC offset is a whole number of quarter-hours. The only thing
    ``CRON_TZ`` changes is which absolute instants are chosen.
    """
    crontab = CRONTAB.read_text(encoding="utf-8")
    schedule_lines = [
        line
        for line in crontab.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    job_lines = [line for line in schedule_lines if "run-purge-revocations.sh" in line]
    assert len(job_lines) == 1
    job = job_lines[0]
    fields = job.split()
    assert fields[:5] == ["*/15", "*", "*", "*", "*"], job
    # A cadence is not a fixed wall-clock time; pinning it to :00 would make the
    # window collide with whatever else runs on the hour.
    assert fields[5].startswith("TZ=UTC"), job


def _effective_lines(path: Path) -> str:
    """A crontab or script with its comments removed.

    Prose describing what a file must never contain will itself contain the word
    it forbids, so the check has to run against what the interpreter executes.
    """
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def test_crontab_contains_no_secret_material() -> None:
    """The crontab is committed, so it must carry no credential of any kind."""
    text = _effective_lines(CRONTAB).lower()
    for marker in ("password", "secret", "token", "jwt", "postgresql://", "api_key"):
        assert marker not in text, f"crontab must not mention {marker}"


def test_scheduler_script_contains_no_secret_material() -> None:
    text = _effective_lines(SCRIPT).lower()
    for marker in ("password", "jwt_secret", "postgresql://", "google_creds"):
        assert marker not in text, f"scheduler script must not embed {marker}"


@pytest.mark.parametrize("path", [SCRIPT, CRONTAB], ids=lambda p: p.name)
def test_scheduler_artifacts_use_unix_line_endings(path: Path) -> None:
    """A carriage return makes the job fail on every run, not fail to install.

    ``/bin/sh`` reads ``\\r`` as part of the command name, so a CRLF script dies
    with "not found" and a CRLF crontab line names a command that does not exist.
    Both are installed and reported as working before the first scheduled run
    exposes them. ``.gitattributes`` pins the line endings; this fails if the
    working tree drifts back anyway.
    """
    raw = path.read_bytes()
    assert b"\r" not in raw, (
        f"{path.name} contains CR bytes; run "
        f"`git add --renormalize {path.relative_to(REPO_ROOT)}`"
    )
    assert raw.endswith(b"\n"), f"{path.name} must end with a newline"