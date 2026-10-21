"""Unit tests for the isolated migration orchestrator (no Docker/PostgreSQL)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from kai.db import migration_orchestrator as orchestrator
from kai.db.migration_orchestrator import (
    IdentityError,
    IdentityReference,
    MigrationOrchestrationError,
    NormalBaseline,
    OrchestratorConfig,
    ProvisioningError,
    PsycopgAdmin,
    _verify_docker_anchor,
    _verify_identity_freshness,
    load_config,
    run_migration_suite,
)
from kai.db.migration_subprocess import AlembicCommandError
from migration_harness import current_head

pytestmark = pytest.mark.db_guard_unit

_ADMIN_URL = "postgresql+psycopg://kai_test_admin:admin@127.0.0.1:55432/postgres"
_RUNTIME_URL = "postgresql+asyncpg://kai_test:kai@127.0.0.1:55432/kai_test"
_RUN_ID = "a1b2c3d4e5f60718"
_CONFIG = OrchestratorConfig(
    runtime_url=_RUNTIME_URL,
    admin_url=_ADMIN_URL,
    admin_user="kai_test_admin",
    container_id=None,
    identity_file=None,
    anchor_kind="compose",
    expected_project="kai-test-db",
    expected_service="db-test",
    expected_config_file=Path("docker-compose.test.yml"),
    docker_context="trusted-context",
)


class _FakeAdmin:
    def __init__(
        self,
        *,
        identifier: str = "123456789",
        privileges_error: Exception | None = None,
    ) -> None:
        self.identifier = identifier
        self.privileges_error = privileges_error
        self.created: list[tuple[str, str]] = []
        self.configured: list[tuple[str, str, str]] = []
        self.verified: list[tuple[str, str, str]] = []
        self.dropped: list[tuple[str, str, str]] = []

    def __enter__(self) -> _FakeAdmin:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def check_privileges(self, *, runtime_role: str, expected_admin: str) -> None:
        if self.privileges_error is not None:
            raise self.privileges_error

    def system_identifier(self) -> str:
        return self.identifier

    def database_exists(self, database_name: str) -> bool:
        return False

    def create_database(self, database_name: str, owner: str) -> None:
        self.created.append((database_name, owner))

    def configure_database(self, database_name: str, owner: str, run_id: str) -> None:
        self.configured.append((database_name, owner, run_id))

    def verify_database(self, database_name: str, owner: str, run_id: str) -> None:
        self.verified.append((database_name, owner, run_id))

    def active_connections(self, database_name: str) -> int:
        return 0

    def drop_own_database(self, database_name: str, owner: str, run_id: str) -> None:
        self.dropped.append((database_name, owner, run_id))


class _FakeDocker:
    def __init__(
        self,
        *,
        info: dict[str, object] | None = None,
        identifier: str = "123456789",
        daemon_ids: list[str] | None = None,
        exec_fail: bool = False,
    ) -> None:
        self.info = info if info is not None else _anchor_info()
        self.identifier = identifier
        self.daemon_ids = list(daemon_ids or [])
        self.exec_fail = exec_fail
        self.inspected: list[str] = []
        self.execs: list[tuple[str, tuple[str, ...]]] = []
        self.compose_calls: list[tuple[str, str, str]] = []
        self._daemon_calls = 0

    def daemon_id(self) -> str:
        if not self.daemon_ids:
            return "daemon-1"
        index = min(self._daemon_calls, len(self.daemon_ids) - 1)
        self._daemon_calls += 1
        return self.daemon_ids[index]

    def inspect(self, container_id: str) -> dict[str, object]:
        self.inspected.append(container_id)
        return self.info

    def compose_ps(self, project: str, service: str, compose_file: Path) -> str:
        self.compose_calls.append((project, service, str(compose_file)))
        return "a" * 64

    def exec(
        self, container_id: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        self.execs.append((container_id, args))
        if self.exec_fail:
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=1,
                stdout=f"Database system identifier: {self.identifier}\n",
                stderr="pg_controldata failed",
            )
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout=f"Database system identifier: {self.identifier}\n",
            stderr="",
        )


class _FakeAlembic:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(
        self, *args: str, target_url: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, target_url))
        if self.fail is not None and args[0] == "upgrade":
            raise self.fail
        stdout = f"{current_head()} (head)\n" if args[0] == "current" else ""
        return subprocess.CompletedProcess(
            args=list(args), returncode=0, stdout=stdout
        )


class _FakePytest:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, url: str, run_id: str, timeout: float) -> object:
        self.calls.append((url, run_id, timeout))
        if self.fail is not None:
            raise self.fail
        return subprocess.CompletedProcess(args=[], returncode=0)


_EXPECTED_TABLES = (
    "alembic_version",
    "tenants",
    "users",
    "tasks",
    "task_plans",
    "memory_entries",
    "runtime_executions",
    "approvals",
    "alerts",
)


def _baseline() -> NormalBaseline:
    return NormalBaseline(revision=current_head(), tables=("users",))


def _run(
    *,
    identity: IdentityReference | None = None,
    admin: _FakeAdmin | None = None,
    docker: _FakeDocker | None = None,
    alembic: _FakeAlembic | None = None,
    pytest_runner: _FakePytest | None = None,
    baseline_reader=lambda url: _baseline(),
    table_reader=lambda url: (
        "alembic_version",
        "tenants",
        "users",
        "tasks",
        "task_plans",
        "memory_entries",
        "runtime_executions",
        "approvals",
        "alerts",
    ),
    config: OrchestratorConfig = _CONFIG,
) -> tuple[int, _FakeAdmin, _FakeAlembic, _FakePytest]:
    fake_admin = admin or _FakeAdmin()
    fake_docker = docker or _FakeDocker()
    fake_alembic = alembic or _FakeAlembic()
    fake_pytest = pytest_runner or _FakePytest()
    result = run_migration_suite(
        config,
        identity=identity
        or IdentityReference(
            container_id="a" * 64,
            system_identifier="123456789",
            source="test",
        ),
        admin=fake_admin,
        docker=fake_docker,
        alembic=fake_alembic,
        pytest_runner=fake_pytest,
        baseline_reader=baseline_reader,
        table_reader=table_reader,
    )
    return result, fake_admin, fake_alembic, fake_pytest


def test_success_uses_one_unique_database_for_alembic_and_pytest() -> None:
    result, admin, alembic, pytest_runner = _run()
    assert result == 0
    assert len(admin.created) == 1
    name, owner = admin.created[0]
    assert name.startswith("kai_test_migrations_")
    assert len(name.removeprefix("kai_test_migrations_")) == 16
    assert owner == "kai_test"
    assert admin.dropped == [(name, "kai_test", name.removeprefix("kai_test_migrations_"))]
    assert alembic.calls[0][1].endswith(f"/{name}")
    assert pytest_runner.calls[0][0].endswith(f"/{name}")
    assert pytest_runner.calls[0][1] == name.removeprefix("kai_test_migrations_")


def test_system_identifier_mismatch_aborts_before_create() -> None:
    admin = _FakeAdmin(identifier="999")
    with pytest.raises(IdentityError, match="system identifier"):
        _run(
            identity=IdentityReference(
                container_id="a" * 64,
                system_identifier="123456789",
                source="test",
            ),
            admin=admin,
        )
    assert admin.created == []


def test_missing_identity_aborts_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(
        config: OrchestratorConfig, **kwargs: object
    ) -> IdentityReference:
        raise IdentityError("Docker anchor is required")

    monkeypatch.setattr(orchestrator, "resolve_identity", _missing)
    admin = _FakeAdmin()
    with pytest.raises(IdentityError, match="Docker anchor"):
        run_migration_suite(
            _CONFIG,
            admin=admin,
            docker=_FakeDocker(),
            alembic=_FakeAlembic(),
            pytest_runner=_FakePytest(),
            baseline_reader=lambda url: _baseline(),
            table_reader=lambda url: (),
        )
    assert admin.created == []


def test_insufficient_admin_privileges_abort_before_create() -> None:
    admin = _FakeAdmin(privileges_error=ProvisioningError("no CREATEDB"))
    with pytest.raises(ProvisioningError, match="no CREATEDB"):
        _run(admin=admin)
    assert admin.created == []


def test_failed_migration_does_not_drop_the_database() -> None:
    admin = _FakeAdmin()
    with pytest.raises(AlembicCommandError):
        _run(
            admin=admin,
            alembic=_FakeAlembic(fail=AlembicCommandError("upgrade failed")),
        )
    assert len(admin.created) == 1
    assert admin.dropped == []


def test_failed_restore_or_baseline_change_does_not_drop_the_database() -> None:
    admin = _FakeAdmin()
    baselines = iter([_baseline(), NormalBaseline(revision="other", tables=())])
    with pytest.raises(MigrationOrchestrationError, match="changed"):
        _run(admin=admin, baseline_reader=lambda url: next(baselines))
    assert admin.dropped == []


def test_admin_connect_uses_autocommit_for_create_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Conn:
        def close(self) -> None:
            captured["closed"] = True

    def _connect(**kwargs: object) -> _Conn:
        captured.update(kwargs)
        return _Conn()

    monkeypatch.setattr(orchestrator.psycopg, "connect", _connect)
    admin = PsycopgAdmin(_ADMIN_URL)
    with admin:
        pass
    assert captured["autocommit"] is True
    assert captured["dbname"] == "postgres"
    assert captured["password"] == "admin"
    assert captured["closed"] is True


def test_admin_command_execution_does_not_fetch_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        description = None

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, query: object, params: object = ()) -> None:
            return None

        def fetchone(self) -> tuple[object, ...]:
            raise AssertionError("command with no result must not be fetched")

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(orchestrator.psycopg, "connect", lambda **kw: _Conn())
    admin = PsycopgAdmin(_ADMIN_URL)
    with admin:
        admin.create_database(f"kai_test_migrations_{_RUN_ID}", "kai_test")


def test_drop_refuses_shared_database_before_any_query() -> None:
    admin = PsycopgAdmin(_ADMIN_URL)
    with pytest.raises(ProvisioningError, match="non-migration"):
        admin.drop_own_database("kai_test", "kai_test", "a1b2c3d4e5f60718")


def test_identity_file_must_not_predate_the_container() -> None:
    reference = IdentityReference(
        container_id="a" * 64,
        system_identifier="123456789",
        source="identity-file",
        created_at="2025-01-01T00:00:00+00:00",
    )
    with pytest.raises(IdentityError, match="predates"):
        _verify_identity_freshness(
            {"State": {"StartedAt": "2026-01-01T00:00:00.123456789Z"}},
            reference,
        )


def _anchor_info(
    *,
    host_port: str = "55432",
    mounts: bool = True,
    started_at: str | None = "2025-01-01T00:00:00+00:00",
) -> dict[str, object]:
    info: dict[str, object] = {
        "Id": "a" * 64,
        "Config": {
            "Image": "postgres:17-alpine",
            "Labels": {
                "com.docker.compose.project": "kai-test-db",
                "com.docker.compose.service": "db-test",
                "com.docker.compose.project.config_files": str(
                    Path("docker-compose.test.yml").resolve()
                ),
            },
        },
        "NetworkSettings": {
            "Ports": {
                "5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": host_port}]
            }
        },
    }
    if started_at is not None:
        info["State"] = {"StartedAt": started_at}
    if mounts:
        info["Mounts"] = [
            {
                "Destination": "/var/lib/postgresql/data",
                "Name": "kai-test-db_kai_test_pgdata",
            }
        ]
    return info


def _context_payload(
    *,
    host: str = "unix:///trusted.sock",
    tls_path: Path | None = None,
    tls_material: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Build one ``docker context inspect`` element with the CLI's real shape.

    ``TLSMaterial`` is ``map[endpointName][]fileName`` (docker/cli
    ``store.EndpointFiles``).  ``docker context inspect`` produces it by listing
    ``contexts/tls/<sha256(name)>/<endpointName>/`` and placing the result next
    to ``Storage.TLSPath``, which names that same TLS root.  A flat mapping such
    as ``{"CA": "ca.pem"}`` is not a shape the CLI ever emits.
    """
    payload: dict[str, object] = {
        "Name": "trusted-context",
        "Metadata": {},
        "Endpoints": {
            "docker": {
                "Host": host,
                "SkipTLSVerify": False,
            }
        },
        "TLSMaterial": tls_material or {},
    }
    if tls_path is not None:
        payload["Storage"] = {"TLSPath": str(tls_path)}
    return payload


def _storage_id(context_name: str) -> str:
    """The CLI context-store id for one context name.

    docker/cli ``store.contextdirOf`` is
    ``digest.FromString(name).Encoded()`` with the canonical SHA-256 algorithm:
    the lower-case hex SHA-256 of the UTF-8 name.  The same id names
    ``contexts/meta/<id>`` and ``contexts/tls/<id>``.
    """
    return hashlib.sha256(context_name.encode("utf-8")).hexdigest()


def _snapshot_paths(config_dir: Path, context_name: str) -> tuple[Path, Path]:
    """The exact metadata file and TLS root the CLI resolves for the snapshot."""
    storage_id = _storage_id(context_name)
    return (
        config_dir / "contexts" / "meta" / storage_id / "meta.json",
        config_dir / "contexts" / "tls" / storage_id,
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` keyed by its POSIX relative path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_pinned_snapshot(
    config_dir: Path,
    context_name: str,
    *,
    tls_files: Mapping[str, Mapping[str, bytes]],
    expected_host: str,
) -> None:
    """Assert the pinned store layout, the metadata contract, and file bytes.

    Deliberately location-exact: ``meta.json`` and every TLS file must sit at the
    path the CLI derives from the pinned name.  A recursive search would also
    accept a snapshot written under the wrong storage id, endpoint directory, or
    file name, so this names each path instead of looking for it.
    """
    meta_path, tls_root = _snapshot_paths(config_dir, context_name)

    registered = sorted(path.name for path in (config_dir / "contexts" / "meta").iterdir())
    assert registered == [_storage_id(context_name)]
    assert meta_path.is_file()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # ``store.Metadata`` is Name/Metadata/Endpoints and nothing else: the CLI
    # ignores unknown keys, so an extra key here is not part of the contract.
    assert set(meta) == {"Name", "Metadata", "Endpoints"}
    assert meta["Name"] == context_name
    assert meta["Metadata"] == {}
    assert meta["Endpoints"]["docker"]["Host"] == expected_host

    expected_files = sorted(
        f"{endpoint}/{name}" for endpoint, files in tls_files.items() for name in files
    )
    if expected_files:
        copied = sorted(
            path.relative_to(tls_root).as_posix()
            for path in tls_root.rglob("*")
            if path.is_file()
        )
    else:
        # No declared TLS material means no TLS tree at all.
        assert not tls_root.exists()
        copied = []
    assert copied == expected_files

    for endpoint, files in tls_files.items():
        for name, content in files.items():
            assert (tls_root / endpoint / name).read_bytes() == content


def _write_decoy_snapshot(
    config_dir: Path,
    context_name: str,
    *,
    files: Mapping[str, Mapping[str, bytes]],
    storage_id: str | None = None,
    extra_meta_keys: Mapping[str, object] | None = None,
) -> None:
    """Write a hand-built snapshot tree used to prove the assertions bite."""
    meta_dir = config_dir / "contexts" / "meta" / (storage_id or _storage_id(context_name))
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "meta.json").write_text(
        json.dumps(
            {
                "Name": context_name,
                "Metadata": {},
                "Endpoints": {"docker": {"Host": "unix:///trusted.sock"}},
                **(extra_meta_keys or {}),
            }
        ),
        encoding="utf-8",
    )
    for endpoint, endpoint_files in files.items():
        endpoint_dir = (
            config_dir / "contexts" / "tls" / (storage_id or _storage_id(context_name)) / endpoint
        )
        endpoint_dir.mkdir(parents=True, exist_ok=True)
        for name, content in endpoint_files.items():
            (endpoint_dir / name).write_bytes(content)


def test_docker_anchor_accepts_matching_ci_container() -> None:
    ci_config = replace(_CONFIG, anchor_kind="ci", container_id="a" * 64)
    _verify_docker_anchor(_anchor_info(), ci_config, "a" * 64)


def test_docker_anchor_rejects_wrong_port_or_missing_volume() -> None:
    ci_config = replace(_CONFIG, anchor_kind="ci", container_id="a" * 64)
    with pytest.raises(IdentityError, match="port mapping"):
        _verify_docker_anchor(_anchor_info(host_port="55433"), ci_config, "a" * 64)
    with pytest.raises(IdentityError, match="mounts"):
        _verify_docker_anchor(_anchor_info(mounts=False), ci_config, "a" * 64)


def test_docker_anchor_rejects_wrong_compose_labels() -> None:
    info = _anchor_info()
    config = info["Config"]
    assert isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    labels["com.docker.compose.service"] = "db"
    with pytest.raises(IdentityError, match="service label"):
        _verify_docker_anchor(info, _CONFIG, "a" * 64)


def test_drop_refuses_active_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = PsycopgAdmin(_ADMIN_URL)
    monkeypatch.setattr(admin, "verify_database", lambda *args: None)
    monkeypatch.setattr(admin, "active_connections", lambda name: 1)
    with pytest.raises(ProvisioningError, match="active connections"):
        admin.drop_own_database(
            f"kai_test_migrations_{_RUN_ID}", "kai_test", _RUN_ID
        )


def test_load_config_accepts_a_complete_admin_url_without_a_second_password() -> None:
    config = load_config(
        {
            "DATABASE_URL": _RUNTIME_URL,
            "KAI_TEST_ADMIN_DATABASE_URL": (
                "postgresql+psycopg://other_admin:other@127.0.0.1:55432/postgres"
            ),
            "KAI_TEST_DOCKER_CONTEXT": "default",
        }
    )
    assert config.admin_user == "other_admin"
    assert "other_admin" in config.admin_url
    assert config.docker_context == "default"


def test_load_config_rejects_shared_or_preset_targets() -> None:
    base = {
        "DATABASE_URL": _RUNTIME_URL,
        "KAI_TEST_POSTGRES_ADMIN_PASSWORD": "admin",
        "KAI_TEST_DOCKER_CONTEXT": "default",
    }
    with pytest.raises(ProvisioningError, match="pre-set"):
        load_config(
            {
                **base,
                "KAI_TEST_MIGRATIONS_DATABASE_URL": _RUNTIME_URL,
            }
        )
    with pytest.raises(ProvisioningError, match="TRUNCATE"):
        load_config({**base, "KAI_TEST_ALLOW_DB_TRUNCATE": "1"})


def _write_identity_file(
    path: Path,
    *,
    container_id: str = "a" * 64,
    identifier: str = "123456789",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "container_id": container_id,
                "system_identifier": identifier,
                "created_at": created_at,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_identity_file_is_bound_to_the_container_and_allows_admin_stage(
    tmp_path: Path,
) -> None:
    identity_file = _write_identity_file(tmp_path / "identity.json")
    config = replace(
        _CONFIG,
        identity_file=identity_file,
        container_id="a" * 64,
        anchor_kind="ci",
    )
    admin = _FakeAdmin()
    docker = _FakeDocker()
    result = run_migration_suite(
        config,
        admin=admin,
        docker=docker,
        alembic=_FakeAlembic(),
        pytest_runner=_FakePytest(),
        baseline_reader=lambda url: _baseline(),
        table_reader=lambda url: _EXPECTED_TABLES,
    )
    assert result == 0
    assert docker.inspected == ["a" * 64]
    assert docker.execs == [("a" * 64, ("pg_controldata",))]
    assert len(admin.created) == 1


def test_identity_file_identifier_mismatch_aborts_before_create(
    tmp_path: Path,
) -> None:
    identity_file = _write_identity_file(
        tmp_path / "identity.json", identifier="999999"
    )
    config = replace(
        _CONFIG,
        identity_file=identity_file,
        container_id="a" * 64,
        anchor_kind="ci",
    )
    admin = _FakeAdmin()
    with pytest.raises(IdentityError, match="does not match"):
        run_migration_suite(
            config,
            admin=admin,
            docker=_FakeDocker(identifier="123456789"),
            alembic=_FakeAlembic(),
            pytest_runner=_FakePytest(),
            baseline_reader=lambda url: _baseline(),
            table_reader=lambda url: _EXPECTED_TABLES,
        )
    assert admin.created == []


def test_identity_file_container_mismatch_aborts_before_create(
    tmp_path: Path,
) -> None:
    identity_file = _write_identity_file(
        tmp_path / "identity.json", container_id="b" * 64
    )
    config = replace(
        _CONFIG,
        identity_file=identity_file,
        container_id="a" * 64,
        anchor_kind="ci",
    )
    admin = _FakeAdmin()
    with pytest.raises(IdentityError, match="another container"):
        run_migration_suite(
            config,
            admin=admin,
            docker=_FakeDocker(),
            alembic=_FakeAlembic(),
            pytest_runner=_FakePytest(),
            baseline_reader=lambda url: _baseline(),
            table_reader=lambda url: _EXPECTED_TABLES,
        )
    assert admin.created == []


def test_injected_identity_cannot_bypass_the_live_container_identifier() -> None:
    admin = _FakeAdmin()
    docker = _FakeDocker(identifier="999999")
    with pytest.raises(IdentityError, match="does not match"):
        _run(
            identity=IdentityReference(
                container_id="a" * 64,
                system_identifier="123456789",
                source="test",
            ),
            admin=admin,
            docker=docker,
        )
    assert docker.inspected == ["a" * 64]
    assert admin.created == []


def test_pg_controldata_failure_aborts_before_create() -> None:
    admin = _FakeAdmin()
    with pytest.raises(IdentityError, match="cannot read"):
        _run(admin=admin, docker=_FakeDocker(exec_fail=True))
    assert admin.created == []


def test_missing_docker_context_aborts_before_create() -> None:
    admin = _FakeAdmin()
    config = replace(_CONFIG, docker_context=None)
    with pytest.raises(IdentityError, match="KAI_TEST_DOCKER_CONTEXT"):
        run_migration_suite(
            config,
            identity=IdentityReference(
                container_id="a" * 64,
                system_identifier="123456789",
                source="test",
            ),
            admin=admin,
            alembic=_FakeAlembic(),
            pytest_runner=_FakePytest(),
            baseline_reader=lambda url: _baseline(),
            table_reader=lambda url: _EXPECTED_TABLES,
        )
    assert admin.created == []


def test_docker_daemon_change_between_stages_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_ids = iter(["daemon-1", "daemon-2"])

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "context" in argv and "inspect" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=json.dumps([_context_payload()]),
                stderr="",
            )
        if "info" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=next(daemon_ids), stderr=""
            )
        if "inspect" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=json.dumps([_anchor_info()]),
                stderr="",
            )
        raise AssertionError(f"unexpected docker command: {argv}")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    docker = orchestrator.DockerCli(
        replace(_CONFIG, docker_context="trusted-context")
    )
    docker.inspect("a" * 64)
    with pytest.raises(IdentityError, match="changed"):
        docker.exec("a" * 64, "pg_controldata")


def test_all_docker_commands_use_the_explicit_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "untrusted")
    monkeypatch.setenv("DOCKER_CONFIG", "C:/untrusted-docker-config")
    monkeypatch.setenv("KAI_TEST_POSTGRES_ADMIN_PASSWORD", "admin-secret")

    captured: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append((list(argv), dict(kwargs)))
        if "context" in argv and "inspect" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=json.dumps([_context_payload()]),
                stderr="",
            )
        if "info" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="daemon-1\n", stderr=""
            )
        if "inspect" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=json.dumps([_anchor_info()]),
                stderr="",
            )
        if "compose" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="a" * 64 + "\n", stderr=""
            )
        if "exec" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="Database system identifier: 123456789\n",
                stderr="",
            )
        raise AssertionError(f"unexpected docker command: {argv}")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    docker = orchestrator.DockerCli(
        replace(_CONFIG, docker_context="trusted-context")
    )
    docker.inspect("a" * 64)
    docker.compose_ps("kai-test-db", "db-test", Path("docker-compose.test.yml"))
    docker.exec("a" * 64, "pg_controldata")

    assert captured
    bootstrap_argv, bootstrap_kwargs = captured[0]
    assert bootstrap_argv[:4] == [
        "docker",
        "--context",
        "trusted-context",
        "context",
    ]
    bootstrap_env = bootstrap_kwargs["env"]
    assert isinstance(bootstrap_env, dict)
    assert "DOCKER_CONFIG" not in bootstrap_env

    pinned_context = captured[1][0][2]
    pinned_capture_env = captured[1][1]["env"]
    assert isinstance(pinned_capture_env, dict)
    pinned_config = Path(str(pinned_capture_env["DOCKER_CONFIG"]))
    assert pinned_context.startswith("kai-pinned-")
    assert pinned_context != "trusted-context"
    assert pinned_config.is_dir()
    meta_path, tls_root = _snapshot_paths(pinned_config, pinned_context)
    assert meta_path.is_file()
    # This context declares no TLS material, so no TLS tree is created.
    assert not tls_root.exists()

    for argv, kwargs in captured[1:]:
        assert argv[:3] == ["docker", "--context", pinned_context]
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["DOCKER_CONFIG"] == str(pinned_config)
        assert "DOCKER_HOST" not in env
        assert "DOCKER_CONTEXT" not in env
        assert "KAI_TEST_POSTGRES_ADMIN_PASSWORD" not in env


def test_context_snapshot_is_isolated_from_source_context_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_config = tmp_path / "source-docker"
    source_config.mkdir()
    payload = _context_payload(host="unix:///original.sock")
    commands: list[tuple[list[str], dict[str, object]]] = []
    context_calls = 0

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal context_calls
        commands.append((list(argv), dict(kwargs)))
        if "context" in argv and "inspect" in argv:
            context_calls += 1
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=json.dumps([payload]), stderr=""
            )
        if "info" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="daemon-1\n", stderr=""
            )
        if "exec" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="Database system identifier: 123456789\n",
                stderr="",
            )
        raise AssertionError(f"unexpected docker command: {argv}")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    docker = orchestrator.DockerCli(
        replace(
            _CONFIG,
            docker_context="source-context",
            docker_config_dir=source_config,
        )
    )
    docker.exec("a" * 64, "pg_controldata")

    endpoints = payload["Endpoints"]
    assert isinstance(endpoints, dict)
    docker_endpoint = endpoints["docker"]
    assert isinstance(docker_endpoint, dict)
    docker_endpoint["Host"] = "tcp://changed.example:2375"
    docker.exec("a" * 64, "pg_controldata")

    assert context_calls == 1
    bootstrap_env = commands[0][1]["env"]
    assert isinstance(bootstrap_env, dict)
    assert bootstrap_env["DOCKER_CONFIG"] == str(source_config)
    pinned_argv, pinned_kwargs = next(
        (argv, kwargs)
        for argv, kwargs in commands
        if not ("context" in argv and "inspect" in argv)
    )
    pinned_env = pinned_kwargs["env"]
    assert isinstance(pinned_env, dict)
    pinned_config = Path(str(pinned_env["DOCKER_CONFIG"]))
    assert pinned_config != source_config
    # The pinned context name must resolve to its own store id, not the source's.
    meta_path, _ = _snapshot_paths(pinned_config, pinned_argv[2])
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["Endpoints"]["docker"]["Host"] == "unix:///original.sock"


_TLS_ENDPOINT_FILES: dict[str, dict[str, bytes]] = {
    "docker": {
        "ca.pem": b"-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n",
        "cert.pem": b"-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n",
        "key.pem": b"-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n",
    }
}


def _write_source_tls(root: Path, files: Mapping[str, Mapping[str, bytes]]) -> None:
    """Create the CLI's source TLS layout: ``<root>/<endpoint>/<fileName>``."""
    for endpoint, endpoint_files in files.items():
        (root / endpoint).mkdir(parents=True, exist_ok=True)
        for name, content in endpoint_files.items():
            (root / endpoint / name).write_bytes(content)


def _pin_with_stubbed_cli(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> orchestrator.DockerCli:
    """Pin one context whose only Docker call is the snapshot inspect."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        assert "context" in argv and "inspect" in argv
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=json.dumps([payload]), stderr=""
        )

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    docker = orchestrator.DockerCli(replace(_CONFIG, docker_context="tls-context"))
    assert len(calls) == 1
    return docker


def test_context_tls_material_is_copied_to_the_pinned_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_tls = tmp_path / "source-tls"
    _write_source_tls(source_tls, _TLS_ENDPOINT_FILES)
    payload = _context_payload(
        tls_path=source_tls,
        tls_material={"docker": sorted(_TLS_ENDPOINT_FILES["docker"])},
    )

    docker = _pin_with_stubbed_cli(monkeypatch, payload)

    assert docker._config_dir != source_tls
    _assert_pinned_snapshot(
        docker._config_dir,
        docker._context,
        tls_files=_TLS_ENDPOINT_FILES,
        expected_host="unix:///trusted.sock",
    )


def test_context_tls_copy_leaves_the_source_files_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_tls = tmp_path / "source-tls"
    _write_source_tls(source_tls, _TLS_ENDPOINT_FILES)
    before = _tree_bytes(source_tls)
    payload = _context_payload(
        tls_path=source_tls,
        tls_material={"docker": sorted(_TLS_ENDPOINT_FILES["docker"])},
    )

    _pin_with_stubbed_cli(monkeypatch, payload)

    assert _tree_bytes(source_tls) == before


def test_mutating_the_source_tls_after_pinning_does_not_change_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_tls = tmp_path / "source-tls"
    _write_source_tls(source_tls, _TLS_ENDPOINT_FILES)
    payload = _context_payload(
        tls_path=source_tls,
        tls_material={"docker": sorted(_TLS_ENDPOINT_FILES["docker"])},
    )
    docker = _pin_with_stubbed_cli(monkeypatch, payload)
    contexts_before = _tree_bytes(docker._config_dir / "contexts")

    (source_tls / "docker" / "ca.pem").write_bytes(b"rotated")
    (source_tls / "docker" / "extra.pem").write_bytes(b"extra")

    assert _tree_bytes(docker._config_dir / "contexts") == contexts_before
    _meta_path, tls_root = _snapshot_paths(docker._config_dir, docker._context)
    assert (tls_root / "docker" / "ca.pem").read_bytes() == (
        _TLS_ENDPOINT_FILES["docker"]["ca.pem"]
    )
    assert not (tls_root / "docker" / "extra.pem").exists()


def test_tls_material_without_a_storage_path_aborts_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _context_payload(tls_material={"docker": ["ca.pem"]})

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=json.dumps([payload]), stderr=""
        )

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    with pytest.raises(IdentityError, match="without a path"):
        orchestrator.DockerCli(replace(_CONFIG, docker_context="tls-context"))


def test_tls_material_path_that_is_not_a_directory_aborts_before_provisioning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tls_path = tmp_path / "ca.pem"
    tls_path.write_bytes(b"not a directory")
    payload = _context_payload(tls_path=tls_path, tls_material={"docker": ["ca.pem"]})

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=json.dumps([payload]), stderr=""
        )

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    with pytest.raises(IdentityError, match="not a directory"):
        orchestrator.DockerCli(replace(_CONFIG, docker_context="tls-context"))


def test_snapshot_check_rejects_a_tree_written_under_the_wrong_storage_id(
    tmp_path: Path,
) -> None:
    context_name = "kai-pinned-0123456789abcdef"
    config_dir = tmp_path / "pinned-docker"
    _write_decoy_snapshot(
        config_dir,
        context_name,
        files=_TLS_ENDPOINT_FILES,
        storage_id=_storage_id("some-other-context"),
    )

    with pytest.raises(AssertionError):
        _assert_pinned_snapshot(
            config_dir,
            context_name,
            tls_files=_TLS_ENDPOINT_FILES,
            expected_host="unix:///trusted.sock",
        )


def test_snapshot_check_rejects_changed_tls_file_content(tmp_path: Path) -> None:
    context_name = "kai-pinned-0123456789abcdef"
    config_dir = tmp_path / "pinned-docker"
    _write_decoy_snapshot(
        config_dir,
        context_name,
        files={"docker": {"ca.pem": b"ca-tampered"}},
    )

    with pytest.raises(AssertionError):
        _assert_pinned_snapshot(
            config_dir,
            context_name,
            tls_files=_TLS_ENDPOINT_FILES,
            expected_host="unix:///trusted.sock",
        )


def test_snapshot_check_rejects_an_omitted_tls_file(tmp_path: Path) -> None:
    context_name = "kai-pinned-0123456789abcdef"
    config_dir = tmp_path / "pinned-docker"
    _write_decoy_snapshot(
        config_dir,
        context_name,
        files={"docker": {"ca.pem": _TLS_ENDPOINT_FILES["docker"]["ca.pem"]}},
    )

    with pytest.raises(AssertionError):
        _assert_pinned_snapshot(
            config_dir,
            context_name,
            tls_files=_TLS_ENDPOINT_FILES,
            expected_host="unix:///trusted.sock",
        )


def test_snapshot_check_rejects_tls_material_persisted_in_meta_json(
    tmp_path: Path,
) -> None:
    context_name = "kai-pinned-0123456789abcdef"
    config_dir = tmp_path / "pinned-docker"
    _write_decoy_snapshot(
        config_dir,
        context_name,
        files=_TLS_ENDPOINT_FILES,
        extra_meta_keys={"TLSMaterial": {"docker": ["ca.pem"]}},
    )

    with pytest.raises(AssertionError):
        _assert_pinned_snapshot(
            config_dir,
            context_name,
            tls_files=_TLS_ENDPOINT_FILES,
            expected_host="unix:///trusted.sock",
        )


def test_context_snapshot_failure_aborts_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "context" in argv and "inspect" in argv
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout="", stderr="missing context"
        )

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    admin = _FakeAdmin()
    with pytest.raises(IdentityError, match="cannot snapshot Docker context"):
        run_migration_suite(
            _CONFIG,
            identity=IdentityReference(
                container_id="a" * 64,
                system_identifier="123456789",
                source="test",
            ),
            admin=admin,
            alembic=_FakeAlembic(),
            pytest_runner=_FakePytest(),
            baseline_reader=lambda url: _baseline(),
            table_reader=lambda url: _EXPECTED_TABLES,
        )
    assert admin.created == []


def test_context_snapshot_without_a_docker_endpoint_aborts_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "context" in argv and "inspect" in argv
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps([{"Name": "broken", "Endpoints": {}}]),
            stderr="",
        )

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    admin = _FakeAdmin()
    with pytest.raises(IdentityError, match="no docker endpoint"):
        run_migration_suite(
            _CONFIG,
            identity=IdentityReference(
                container_id="a" * 64,
                system_identifier="123456789",
                source="test",
            ),
            admin=admin,
            alembic=_FakeAlembic(),
            pytest_runner=_FakePytest(),
            baseline_reader=lambda url: _baseline(),
            table_reader=lambda url: _EXPECTED_TABLES,
        )
    assert admin.created == []


def test_docker_env_excludes_ambient_routing_and_admin_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "untrusted")
    monkeypatch.setenv("DOCKER_CONFIG", "C:/untrusted-docker-config")
    monkeypatch.setenv("KAI_TEST_POSTGRES_ADMIN_PASSWORD", "admin-secret")

    env = orchestrator._docker_env()
    assert "DOCKER_HOST" not in env
    assert "DOCKER_CONTEXT" not in env
    assert "DOCKER_CONFIG" not in env
    assert "KAI_TEST_POSTGRES_ADMIN_PASSWORD" not in env

    explicit = orchestrator._docker_env(Path("C:/trusted-docker-config"))
    assert explicit["DOCKER_CONFIG"] == str(Path("C:/trusted-docker-config"))


def test_daemon_id_failure_aborts_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "context" in argv and "inspect" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=json.dumps([_context_payload()]),
                stderr="",
            )
        assert "info" in argv
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout="", stderr="no daemon"
        )

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    admin = _FakeAdmin()
    with pytest.raises(IdentityError, match="cannot verify"):
        run_migration_suite(
            _CONFIG,
            identity=IdentityReference(
                container_id="a" * 64,
                system_identifier="123456789",
                source="test",
            ),
            admin=admin,
            alembic=_FakeAlembic(),
            pytest_runner=_FakePytest(),
            baseline_reader=lambda url: _baseline(),
            table_reader=lambda url: _EXPECTED_TABLES,
        )
    assert admin.created == []


def test_load_config_requires_an_explicit_docker_context() -> None:
    with pytest.raises(ProvisioningError, match="KAI_TEST_DOCKER_CONTEXT"):
        load_config(
            {
                "DATABASE_URL": _RUNTIME_URL,
                "KAI_TEST_POSTGRES_ADMIN_PASSWORD": "admin",
            }
        )


def test_ci_workflow_sets_and_uses_the_explicit_docker_context() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/quality-gate.yml"
    )
    text = workflow.read_text(encoding="utf-8")
    assert "KAI_TEST_DOCKER_CONTEXT: default" in text
    assert text.count('docker --context "$KAI_TEST_DOCKER_CONTEXT" exec') >= 4
    assert "docker exec" not in text
