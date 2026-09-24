"""Execution context: trusted connection/host details for a tool run.

Everything on ``ToolContext`` is supplied by the *system* (the caller
authenticates the task, locates the tenant/user/account, and provides a
transport). Everything the tool receives from outside goes through
``ToolIntent.arguments`` and stays untrusted through schema validation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from kai.identity.schemas import IdentityQuery


@dataclass
class ToolContext:
    """Trusted inputs the gateway and executors may rely on."""

    session: AsyncSession
    task_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    account_id: uuid.UUID | None = None
    transport: httpx.AsyncClient | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: An explicit identity to resolve via the Identity Resolver. The query
    #: carries *identifiers* (data); tenant/user/account scope is always the
    #: context itself, never the query.
    identity: IdentityQuery | None = None

    @property
    def has_account(self) -> bool:
        return self.account_id is not None


__all__ = ["ToolContext"]