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

    settings = get_settings()

    assert settings.environment == "production"
    assert settings.google_creds_encryption_secret == _STRONG_GOOGLE
