"""The tool intent: a validated, system-trusted request to run one tool.

An intent is the *only* thing an upper layer (task planner / agent runtime)
may hand to the gateway. Any data that is not explicitly modeled here is
rejected with ``extra="forbid"``. Identity fields (task_id/tenant_id/user_id)
must match the trusted ``ToolContext``; the tool's ``arguments`` are treated
as untrusted and cannot carry reserved identity/credential keys.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Keys that are always managed by the system and never accepted as tool
#: arguments, regardless of which tool is being invoked.
RESERVED_ARGUMENTS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "user_id",
        "task_id",
        "account_id",
        "access_token",
        "refresh_token",
        "credentials",
        "credential",
        "token",
        "authorization",
        "headers",
    }
)


class ToolIntent(BaseModel):
    """A structured request to execute exactly one registered tool."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    request_id: str = Field(default="", max_length=128)

    def argument_keys(self) -> Mapping[str, Any]:
        return self.arguments


__all__ = ["RESERVED_ARGUMENTS", "ToolIntent"]