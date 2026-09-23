from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "change-me-in-production"
DEFAULT_GOOGLE_CRED_SECRET = "change-me-google-token-encryption"


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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if (
        settings.environment == "production"
        and settings.jwt_secret == DEFAULT_JWT_SECRET
    ):
        raise RuntimeError("JWT_SECRET must be set to a non-default value in production")
    return settings
