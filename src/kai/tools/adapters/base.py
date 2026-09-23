"""Shared machinery for provider adapters.

Adapters are the *only* layer allowed to touch provider integrations. Each
adapter reuses the existing tenant-scoped integration service
(``kai.contacts.service``, ``kai.calendar.service``, ``kai.gmail.service``)
exactly as the API vertical slices do: a provider round-trip here is the same
code path as the public endpoints. The gateway adds the boundaries; the
adapter adds serialization and deterministic error classification.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import httpx

from kai.tools.context import ToolContext
from kai.tools.errors import (
    ACCOUNT_REQUIRED,
    NOT_IMPLEMENTED,
    TRANSPORT_REQUIRED,
    ToolContextError,
    ToolExecutionError,
)


class AdapterBase:
    """Tracks adapter invocations for deterministic, observable tests."""

    def __init__(self) -> None:
        self._calls: list[str] = []

    def record(self, name: str) -> None:
        self._calls.append(name)

    @property
    def calls(self) -> list[str]:
        return list(self._calls)

    def require_transport(self, context: ToolContext) -> httpx.AsyncClient:
        if context.transport is None:
            raise ToolContextError(
                "a Google transport is required in the execution context",
                error_code=TRANSPORT_REQUIRED,
            )
        return context.transport

    def require_account(self, context: ToolContext) -> UUID:
        """Return the account id, refusing tools that require one.

        The gateway already refuses ``requires_account`` tools without an
        account, so this is a defensive narrowing for the type checker and
        miswired gateways.
        """
        if context.account_id is None:
            raise ToolContextError(
                "this tool requires a connected google account",
                error_code=ACCOUNT_REQUIRED,
            )
        return context.account_id

    def unimplemented(self, tool_name: str) -> ToolExecutionError:
        return ToolExecutionError(
            f"adapter {type(self).__name__} does not implement tool {tool_name!r}",
            error_code=NOT_IMPLEMENTED,
        )


def provider_failure(
    exception: Exception,
    *,
    codes: Mapping[type[Exception], str],
    default: str = "PROVIDER_ERROR",
) -> ToolExecutionError:
    """Classify an integration exception into a stable adapter error code.

    Callers list their most specific provider errors first; ``GoogleApiError``
    (the common base) is always a reasonable fallback.
    """
    for exc_type, error_code in codes.items():
        if isinstance(exception, exc_type):
            return ToolExecutionError(str(exception), error_code=error_code)
    return ToolExecutionError(str(exception), error_code=default)


__all__ = ["AdapterBase", "provider_failure"]