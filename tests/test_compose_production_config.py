"""Every process KAI runs in production must be able to start in production.

``docker-compose.yml`` wires the environment for three processes that boot the
application: the API, the one-shot migration, and the one-shot purge. Settings
validation in production is deliberately fail-closed -- a process whose security
controls are silently off refuses to start rather than serving traffic.

That makes the compose file load-bearing in a way nothing else tests. A service
missing one variable does not degrade, it does not run: `ENVIRONMENT=production`
plus a missing rate-limit backend raises at startup, so the migration never
applies and the purge never deletes anything, while every check that inspects
the compose file for the presence of a key still looks green.

These tests resolve each service's real environment and run it through the real
validators. They are the reason the one-shot jobs carry ``RATE_LIMIT_BACKEND``
and ``REDIS_URL``: those jobs serve no HTTP, but validation is process-wide, and
a purge that cannot start under production is not a purge.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from kai.config import Settings, _validate_production_security_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

#: Services that boot the application and therefore run Settings validation.
KAI_SERVICES = ("app", "migrate", "purge-revocations")

#: The two secrets Compose deliberately ships as placeholders that fail
#: production validation. They are deployment-supplied, so the test supplies
#: real values and asserts only that the *rest* of the service is sound.
_DEPLOYMENT_SUPPLIED_SECRETS = {
    "JWT_SECRET": "j" * 64,
    "GOOGLE_CREDS_ENCRYPTION_SECRET": "g" * 64,
}

_VAR = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def _resolve(raw: str, env: dict[str, str]) -> str:
    """Expand a Compose ``${VAR}`` / ``${VAR:-default}`` reference.

    Deliberately not a full Compose interpolation: the one-shot services only
    use the ``:-default`` form, and implementing the rest would be untested
    machinery in a test that is supposed to be simple to trust.
    """
    return _VAR.sub(
        lambda m: env.get(m.group("name"), m.group("default") or ""),
        raw,
    )


def _service_env(name: str, compose: dict[str, Any]) -> dict[str, str]:
    """The environment one Compose service would actually start with."""
    service = compose["services"][name]
    resolved: dict[str, str] = {}
    for key, raw in (service.get("environment") or {}).items():
        resolved[key] = _resolve(str(raw), _DEPLOYMENT_SUPPLIED_SECRETS)
    return resolved


def _as_settings(env: dict[str, str]) -> Settings:
    """Build Settings from a service environment.

    Keyed by field name rather than injected into ``os.environ`` so the result
    depends only on what Compose declares: a stray variable in the developer's
    shell cannot make an incomplete service look complete.
    """
    fields = {name.lower(): value for name, value in env.items()}
    return Settings(**fields)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_every_kai_service_is_present(compose: dict[str, Any]) -> None:
    """Guard the fixture: a renamed service must fail, not silently shrink scope."""
    for name in KAI_SERVICES:
        assert name in compose["services"], f"{name} is missing from docker-compose.yml"


@pytest.mark.parametrize("service", KAI_SERVICES)
def test_service_can_start_in_production(compose: dict[str, Any], service: str) -> None:
    """The service's declared environment must pass the production validators.

    This is the invariant the Compose file can break without any other check
    noticing: the process exits non-zero at startup, and a scheduled job that
    exits non-zero every fifteen minutes deletes nothing.
    """
    env = _service_env(service, compose)
    settings = _as_settings(env)
    assert settings.environment == "production", (
        f"{service} defaults to ENVIRONMENT={settings.environment!r}; a deployment "
        f"that forgets the variable would run it without production checks"
    )
    _validate_production_security_settings(settings)


@pytest.mark.parametrize("service", KAI_SERVICES)
def test_one_shot_jobs_are_not_rate_limited_off(compose: dict[str, Any], service: str) -> None:
    """Name the specific missing variable, so a failure is actionable.

    The generic validator above reports "production requires
    RATE_LIMIT_BACKEND=redis". This asserts the cause directly against the file,
    which survives a reworded error message.
    """
    env = _service_env(service, compose)
    assert env.get("RATE_LIMIT_BACKEND") == "redis", (
        f"{service} must declare RATE_LIMIT_BACKEND=redis; got "
        f"{env.get('RATE_LIMIT_BACKEND')!r}. Validation is process-wide, so "
        f"omitting it stops this process from starting in production."
    )
    assert env.get("REDIS_URL"), f"{service} must declare REDIS_URL alongside the redis backend"


def test_the_purge_is_scheduled_but_never_a_service(compose: dict[str, Any]) -> None:
    """The purge must stay one-shot: the schedule belongs to the operator.

    ``restart: "no"`` plus a command that exits is what keeps Compose from
    turning the job into a daemon. The cadence lives in deploy/scheduler/.
    """
    purge = compose["services"]["purge-revocations"]
    assert purge["restart"] == "no"
    assert purge["command"] == ["kai-purge-revocations"]


def test_metrics_can_be_enabled_without_shadowing_a_real_endpoint(
    compose: dict[str, Any],
) -> None:
    """Turning on metrics for alerting must be a legal production config.

    Alerting depends on the endpoint existing, so the operator has to set
    ``METRICS_ENABLED=true``. Validation must accept that combination, and must
    still refuse a path that would displace ``/api/v1/health``.
    """
    app = compose["services"]["app"]
    resolved = {
        key: _resolve(str(value), _DEPLOYMENT_SUPPLIED_SECRETS)
        for key, value in app["environment"].items()
    }
    resolved["METRICS_ENABLED"] = "true"
    resolved["METRICS_PATH"] = "/api/v1/metrics"
    _validate_production_security_settings(_as_settings(resolved))

    resolved["METRICS_PATH"] = "/api/v1/health"
    with pytest.raises(RuntimeError, match="shadow"):
        _validate_production_security_settings(_as_settings(resolved))
