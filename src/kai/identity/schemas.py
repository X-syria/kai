"""Structured Identity Resolver input.

``IdentityQuery`` is built by the *application* layer (task planner / future
agent runtime) from trusted execution metadata plus the identifiers it wants the
resolver to verify. The query is data, not authority:

* Tenant/user/account scope is **never** part of the query — it always comes
  from the trusted ``ToolContext``, so an external payload can never change the
  scope resolution runs against.
* ``name`` and ``external_id`` never establish identity by themselves: a
  display name or a claimed id inside a message is data, not proof.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_NAME = 512
_MAX_EMAIL = 320
_MAX_PHONE = 64
_MAX_PROVIDER = 64
_MAX_PROVIDER_ACCOUNT_ID = 512
_MAX_CHANNEL = 64
_MAX_EXTERNAL_ID = 512


class IdentityQuery(BaseModel):
    """Identifiers to resolve against trustable sources, one entity per query."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=_MAX_NAME)
    email: str | None = Field(default=None, max_length=_MAX_EMAIL)
    phone: str | None = Field(default=None, max_length=_MAX_PHONE)
    provider: str | None = Field(default=None, max_length=_MAX_PROVIDER)
    provider_account_id: str | None = Field(default=None, max_length=_MAX_PROVIDER_ACCOUNT_ID)
    channel: str | None = Field(default=None, max_length=_MAX_CHANNEL)
    external_id: str | None = Field(default=None, max_length=_MAX_EXTERNAL_ID)

    @field_validator(
        "name",
        "email",
        "phone",
        "provider",
        "provider_account_id",
        "channel",
        "external_id",
    )
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped if stripped else None


__all__ = ["IdentityQuery"]