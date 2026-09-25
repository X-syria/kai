from functools import lru_cache
from typing import Final, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "change-me-in-production"
DEFAULT_GOOGLE_CRED_SECRET = "change-me-google-token-encryption"

#: Production floor for ``google_creds_encryption_secret``, matching the "32+
#: random bytes" its own documentation requires. Enforced only in production so
#: local and test runs keep working with the shipped defaults.
_MIN_GOOGLE_SECRET_LENGTH: Final[int] = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "KAI"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://kai:kai@127.0.0.1:5432/kai"
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_access_token_minutes: int = 120

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
    return settings
