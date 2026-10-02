"""Configuration guards — production refuses to start on a public secret.

The secrets these checks protect are the ones that decide whether a leaked
database is a leaked credential store: ``jwt_secret`` signs access tokens, and
``google_creds_encryption_secret`` derives the Fernet key that encrypts every
stored Google refresh token. Both ship in this repository, so "unset" and
"publicly known" are the same state, and production has to refuse it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kai.config import (
    DEFAULT_GOOGLE_CRED_SECRET,
    DEFAULT_JWT_SECRET,
    get_settings,
)

_STRONG_JWT = "j" * 48
_STRONG_GOOGLE = "g" * 48


def _secure_rate_limit(env: pytest.MonkeyPatch) -> None:
    """Give a production test the shared limiter production demands.

    Production refuses to start on the per-process store, so any test that is
    genuinely exercising "production starts" has to opt into a shared backend
    too. Tests about the limiter itself set their own environment instead.
    """
    env.setenv("RATE_LIMIT_BACKEND", "redis")
    env.setenv("REDIS_URL", "redis://redis.invalid:6379/0")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """A settings cache that never survives the test that asked for it."""
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_development_runs_on_the_shipped_defaults(env: pytest.MonkeyPatch) -> None:
    env.setenv("ENVIRONMENT", "development")
    env.delenv("JWT_SECRET", raising=False)
    env.delenv("GOOGLE_CREDS_ENCRYPTION_SECRET", raising=False)

    settings = get_settings()

    assert settings.jwt_secret == DEFAULT_JWT_SECRET
    assert settings.google_creds_encryption_secret == DEFAULT_GOOGLE_CRED_SECRET


def test_test_environment_runs_on_the_shipped_defaults(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("ENVIRONMENT", "test")
    env.delenv("JWT_SECRET", raising=False)
    env.delenv("GOOGLE_CREDS_ENCRYPTION_SECRET", raising=False)

    assert get_settings().google_creds_encryption_secret == DEFAULT_GOOGLE_CRED_SECRET


def test_production_refuses_the_default_google_secret(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", DEFAULT_GOOGLE_CRED_SECRET)

    with pytest.raises(RuntimeError, match="GOOGLE_CREDS_ENCRYPTION_SECRET"):
        get_settings()


def test_production_refuses_an_unset_google_secret(env: pytest.MonkeyPatch) -> None:
    """Unset is not safer than default: the default is what unset resolves to."""
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.delenv("GOOGLE_CREDS_ENCRYPTION_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="GOOGLE_CREDS_ENCRYPTION_SECRET"):
        get_settings()


def test_production_refuses_a_weak_google_secret(env: pytest.MonkeyPatch) -> None:
    """A non-default value is not automatically a secret worth protecting."""
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", "hunter2")

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        get_settings()


def test_production_refuses_a_blank_google_secret(env: pytest.MonkeyPatch) -> None:
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", " " * 64)

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        get_settings()


def test_production_still_refuses_the_default_jwt_secret(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("ENVIRONMENT", "production")
    env.delenv("JWT_SECRET", raising=False)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        get_settings()


def test_production_checks_both_secrets_independently(
    env: pytest.MonkeyPatch,
) -> None:
    """A good JWT secret must not mask a default Google secret."""
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", DEFAULT_GOOGLE_CRED_SECRET)

    with pytest.raises(RuntimeError) as excinfo:
        get_settings()

    assert "JWT_SECRET" not in str(excinfo.value)


def test_production_starts_with_both_secrets_set(env: pytest.MonkeyPatch) -> None:
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    _secure_rate_limit(env)

    settings = get_settings()

    assert settings.environment == "production"
    assert settings.google_creds_encryption_secret == _STRONG_GOOGLE


def test_production_refuses_a_short_jwt_secret(env: pytest.MonkeyPatch) -> None:
    """Not-the-default is not enough: a short secret is enumerable offline."""
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", "short-but-not-the-default")
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    _secure_rate_limit(env)

    with pytest.raises(RuntimeError, match="JWT_SECRET must be at least"):
        get_settings()


def test_production_refuses_the_none_algorithm(env: pytest.MonkeyPatch) -> None:
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    env.setenv("JWT_ALGORITHM", "none")
    _secure_rate_limit(env)

    with pytest.raises(RuntimeError, match="JWT_ALGORITHM"):
        get_settings()


def test_production_refuses_wildcard_cors(env: pytest.MonkeyPatch) -> None:
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    env.setenv("CORS_ALLOWED_ORIGINS", '["*"]')
    _secure_rate_limit(env)

    with pytest.raises(RuntimeError, match=r"must not contain '\*'"):
        get_settings()


def test_production_refuses_a_per_process_rate_limiter(
    env: pytest.MonkeyPatch,
) -> None:
    """A per-process limit is not a limit behind a load balancer."""
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    env.setenv("RATE_LIMIT_BACKEND", "memory")

    with pytest.raises(RuntimeError, match="RATE_LIMIT_BACKEND=redis"):
        get_settings()


def test_production_refuses_redis_backend_without_a_url(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    env.setenv("RATE_LIMIT_BACKEND", "redis")
    env.delenv("REDIS_URL", raising=False)

    with pytest.raises(RuntimeError, match="REDIS_URL"):
        get_settings()


# ---------------------------------------------------------------------------
# List-valued settings must survive a real deployment environment.
#
# These came from a failure that only appeared in the container: Docker Compose
# passes every declared variable through, including ones set to an empty string,
# and pydantic refuses to parse "" as a list. The process then died at import
# with a SettingsError before serving a single request. Tests never caught it
# because a test process has no empty-string variables in its environment.
# ---------------------------------------------------------------------------


def test_empty_env_value_becomes_an_empty_list(env: pytest.MonkeyPatch) -> None:
    env.setenv("CORS_ALLOWED_ORIGINS", "")
    env.setenv("TRUSTED_HOSTS", "")

    settings = get_settings()

    assert settings.cors_allowed_origins == []
    assert settings.trusted_hosts == []


def test_comma_separated_env_value_becomes_a_list(env: pytest.MonkeyPatch) -> None:
    """Compose hands over a bare CSV string; that must not fail to parse."""
    env.setenv("TRUSTED_HOSTS", "api.example.com, www.example.com")

    assert get_settings().trusted_hosts == ["api.example.com", "www.example.com"]


def test_json_list_env_value_is_accepted(env: pytest.MonkeyPatch) -> None:
    env.setenv("CORS_ALLOWED_ORIGINS", '["https://app.example"]')

    assert get_settings().cors_allowed_origins == ["https://app.example"]


def test_production_rejects_wildcard_from_a_comma_separated_value(
    env: pytest.MonkeyPatch,
) -> None:
    """The wildcard check must survive CSV parsing, not just JSON form."""
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_SECRET", _STRONG_JWT)
    env.setenv("GOOGLE_CREDS_ENCRYPTION_SECRET", _STRONG_GOOGLE)
    _secure_rate_limit(env)
    env.setenv("CORS_ALLOWED_ORIGINS", "https://app.example,*")

    with pytest.raises(RuntimeError, match=r"must not contain '\*'"):
        get_settings()
