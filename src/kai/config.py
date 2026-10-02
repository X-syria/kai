import json
from functools import lru_cache
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "change-me-in-production"
DEFAULT_GOOGLE_CRED_SECRET = "change-me-google-token-encryption"


def _split_csv(value: Any) -> Any:
    """Accept a JSON array or a comma-separated string as a list of strings.

    Deployment tooling differs on how it passes a list: Docker Compose and most
    CI systems hand over a bare comma-separated string, while a JSON array is
    the convention pydantic-settings would normally parse. Supporting both means
    an operator can write ``TRUSTED_HOSTS=api.example.com,www.example.com`` in a
    compose file without the process failing to boot.

    An empty or blank value becomes an empty list rather than an error, because
    "no origins configured" is the documented default and is exactly what an
    unset compose variable resolves to.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]
    return value


#: A list of strings that also accepts a comma-separated environment value.
CsvList = Annotated[list[str], BeforeValidator(_split_csv)]

#: Production floor for ``google_creds_encryption_secret``, matching the "32+
#: random bytes" its own documentation requires. Enforced only in production so
#: local and test runs keep working with the shipped defaults.
_MIN_GOOGLE_SECRET_LENGTH: Final[int] = 32

#: Production floor for ``jwt_secret``. A JWT secret is the entire trust anchor
#: for authentication: anyone holding the database plus a candidate secret can
#: mint valid tokens offline. A short secret is enumerable, so production
#: requires real entropy rather than merely "not the default".
_MIN_JWT_SECRET_LENGTH: Final[int] = 32

#: Signing algorithms this service will ever use. The allowlist is closed
#: rather than open so that setting ``JWT_ALGORITHM`` cannot introduce "none"
#: or an asymmetric algorithm that the shared-secret design does not support.
SUPPORTED_JWT_ALGORITHMS: Final[frozenset[str]] = frozenset({"HS256", "HS384", "HS512"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Without this, pydantic-settings JSON-decodes every complex-typed
        # field before user validation runs, so "api.example.com,www.example.com"
        # raises a JSONDecodeError instead of reaching ``_split_csv``. Turning
        # it off hands the raw string to the validator, which accepts CSV, a
        # JSON array, and the empty string.
        enable_decoding=False,
    )

    app_name: str = "KAI"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai"
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_access_token_minutes: int = 120
    # ``iss``/``aud`` are verified on every decode, so a token minted by some
    # other service that happens to share the secret is rejected rather than
    # silently accepted. They are not secrets and need no production override.
    jwt_issuer: str = "kai"
    jwt_audience: str = "kai-api"
    # Clock skew tolerated when checking ``exp``. Small on purpose: a large
    # leeway silently extends the life of every token by that margin.
    jwt_leeway_seconds: int = 5

    # Rate limiting. Backed by Redis when REDIS_URL is set, otherwise by a
    # single-process in-memory store. The in-memory store is per-process, so a
    # multi-replica deployment that leaves REDIS_URL unset gets a limit that is
    # only correct for whichever replica the request happened to land on.
    rate_limit_enabled: bool = True
    rate_limit_backend: Literal["auto", "memory", "redis"] = "auto"
    redis_url: str = ""
    auth_rate_limit_requests: int = 10
    auth_rate_limit_window_seconds: int = 60

    # CORS. Empty means "no cross-origin browser access at all", which is the
    # correct default for an API that is not meant to be called from a browser
    # on another origin. A production deployment serving a web client must list
    # its exact origins here; "*" is rejected at startup because it is
    # incompatible with credentialed requests and hides the real origin.
    cors_allowed_origins: CsvList = []

    # Trusted hosts. Requests whose ``Host`` header is not in this list get 400.
    # Empty disables the check, which is only safe when the service is reached
    # solely through a host-based router that already filters Host.
    trusted_hosts: CsvList = []

    # Google OAuth — least-privilege by default: identity only, no Gmail/Drive/
    # Calendar/Contacts access. Scopes are configurable but the default is the
    # minimal set Google requires to establish an authenticated identity.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    google_oauth_scopes: str = "openid email"
    # Read-only Google Contacts access for the current vertical slice. This is
    # the least privileged scope that lets the app read a user's contacts:
    # deliberately not "contacts" (read/write), Gmail, Calendar, or Drive.
    google_contacts_scope: str = "https://www.googleapis.com/auth/contacts.readonly"
    # Least-privilege Google Calendar scope for the current vertical slice.
    # "calendar.events" is the narrowest scope that supports BOTH reading a
    # user's events (availability lookups) AND creating/reading-back events,
    # which the slice requires. It deliberately excludes the broader
    # "calendar" scope (calendar metadata, sharing/ACL, settings) and
    # "calendar.readonly" would be too narrow (the slice must create events).
    google_calendar_scope: str = "https://www.googleapis.com/auth/calendar.events"
    # Least-privilege Gmail scopes: read and send are deliberately kept as two
    # separate scopes so read access is never granted send permission and vice
    # versa. "gmail.readonly" covers search + reading messages for context;
    # "gmail.send" alone covers sending, with no read. The broader "gmail" and
    # "gmail.modify" (read/write/labels/trash) are never requested, nor is
    # "gmail.compose" (draft management on the user's behalf).
    google_gmail_read_scope: str = "https://www.googleapis.com/auth/gmail.readonly"
    google_gmail_send_scope: str = "https://www.googleapis.com/auth/gmail.send"
    google_oauth_access_token_minutes: int = 60
    # 32+ random bytes used to derive the Fernet key that encrypts stored
    # Google credentials at rest. Never the plaintext client secret itself.
    google_creds_encryption_secret: str = DEFAULT_GOOGLE_CRED_SECRET

    # Planner — exactly one fixed model in this phase; no routing. The URL is
    # an OpenAI-compatible ``/chat/completions`` endpoint. The API key (if the
    # endpoint requires one) must come from the environment, never be baked
    # into code. An empty URL means the provider refuses to run (tests use a
    # deterministic stub and never hit the network).
    planner_model: str = "kai-planner-v0.5"
    planner_model_url: str = ""
    planner_model_api_key: str = ""
    planner_model_timeout_seconds: float = 60.0

    # Memory — deterministic, per-user long-term facts. TTL levels translate to
    # concrete lifetimes (PERMANENT never expires); freshness decays from
    # ``updated_at`` with an injectable ``now`` and is never a grant of any
    # kind. Memory can never change policy, permissions, approvals, or identity.
    memory_long_ttl_days: int = 365
    memory_short_ttl_days: int = 30
    memory_ephemeral_hours: int = 24
    memory_freshness_half_life_days: int = 14

    # Approval Manager — every approval is time-boxed; a PENDING or APPROVED
    # request past its expiration is lazy-expired and never authorizes.
    approval_ttl_minutes: float = 1440.0  # 24h
    # Approval fatigue: N approvals for the same (tool, risk, recipient_class)
    # inside the window trigger ONE rule suggestion, which is a non-authorizing
    # proposal only. Rejecting it starts the cooldown; suggestions never grant
    # permission automatically.
    approval_fatigue_threshold: int = 5
    approval_fatigue_window_days: int = 14
    approval_fatigue_cooldown_days: int = 30
    approval_suggestion_expiration_days: int = 30


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.environment != "production":
        return settings

    # Both secrets below ship in this source tree, so a deployment that never
    # overrides one runs on a value that is public by construction. Fail at
    # startup rather than encrypting a whole database of Google refresh tokens
    # under a key anyone can derive from the repository.
    if settings.jwt_secret == DEFAULT_JWT_SECRET:
        raise RuntimeError("JWT_SECRET must be set to a non-default value in production")
    if settings.google_creds_encryption_secret == DEFAULT_GOOGLE_CRED_SECRET:
        raise RuntimeError(
            "GOOGLE_CREDS_ENCRYPTION_SECRET must be set to a non-default value in production"
        )
    # The contract for this secret is "32+ random bytes" (see the field above);
    # production enforces it, because a short value is a real-looking secret
    # that would otherwise pass a non-default check and protect nothing.
    if len(settings.google_creds_encryption_secret.strip()) < _MIN_GOOGLE_SECRET_LENGTH:
        raise RuntimeError(
            "GOOGLE_CREDS_ENCRYPTION_SECRET must be at least "
            f"{_MIN_GOOGLE_SECRET_LENGTH} characters in production; it derives the "
            "Fernet key that encrypts every stored Google credential"
        )
    _validate_production_security_settings(settings)
    return settings


def _validate_production_security_settings(settings: Settings) -> None:
    """Refuse production configs whose security controls are silently off.

    Each of these is a control that looks enabled while doing nothing: an
    algorithm nobody pinned, a wildcard CORS origin, a rate limiter that counts
    per process behind a load balancer, or a JWT secret too weak to brute-force
    offline. A production deployment has to either configure them properly or
    fail to start.
    """
    if settings.jwt_algorithm not in SUPPORTED_JWT_ALGORITHMS:
        raise RuntimeError(
            f"JWT_ALGORITHM must be one of {sorted(SUPPORTED_JWT_ALGORITHMS)} in "
            f"production; got {settings.jwt_algorithm!r}. 'none' and any asymmetric "
            "algorithm are not accepted because this service signs with a shared "
            "symmetric secret."
        )
    if "*" in settings.cors_allowed_origins:
        raise RuntimeError(
            "CORS_ALLOWED_ORIGINS must not contain '*' in production; list the exact "
            "origins of your web clients instead"
        )
    if settings.rate_limit_enabled and settings.rate_limit_backend != "redis":
        raise RuntimeError(
            "production requires RATE_LIMIT_BACKEND=redis (and REDIS_URL) so the auth "
            "rate limit is shared across replicas; the in-memory store counts per "
            "process and a per-process limit is not a limit under a load balancer. "
            "Set RATE_LIMIT_ENABLED=false only if an external limiter already "
            "enforces these routes."
        )
    if settings.rate_limit_enabled and settings.rate_limit_backend == "redis" and not (
        settings.redis_url
    ):
        raise RuntimeError(
            "RATE_LIMIT_BACKEND=redis requires REDIS_URL in production"
        )
    if len(settings.jwt_secret.strip()) < _MIN_JWT_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET must be at least {_MIN_JWT_SECRET_LENGTH} characters in "
            "production; a short secret is brute-forceable offline by anyone who "
            "obtains a token, which makes every other control here irrelevant"
        )
