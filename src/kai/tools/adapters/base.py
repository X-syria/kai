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
from typing import Final
from uuid import UUID

import httpx

from kai.tools.context import ToolContext
from kai.tools.errors import (
    ACCOUNT_REQUIRED,
    NOT_IMPLEMENTED,
    PROVIDER_ERROR,
    PROVIDER_NOT_CONTACTED,
    PROVIDER_SERVER_ERROR,
    PROVIDER_UNREACHABLE,
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


#: Transport failures after which **no connection was ever established**, so the
#: request provably never left this process.
#:
#: The claim is narrow and is stated as narrowly as it can be: httpx raises
#: these when it could not obtain a usable connection to the provider endpoint,
#: which means no request bytes were written to a socket carrying them. It is a
#: statement about this client's socket, not about every hop in between \u2014 it
#: assumes, correctly for a direct TLS call to a Google API, that no
#: intermediary is buffering a request on our behalf.
#:
#: :data:`~kai.tools.errors.PROVIDER_NOT_CONTACTED` is therefore a *known*
#: outcome: not an unknown one. Reporting it as unresolved would be inventing
#: doubt about an operation that provably did not happen.
_NOT_CONTACTED: Final = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)

#: Transport failures **after** a connection existed and bytes were being sent.
#:
#: Here the request may or may not have reached the provider and been applied,
#: and nothing in this process can tell. A read timeout, a reset mid-response, or
#: a server that hung up after acting are all indistinguishable from the client
#: side, which is exactly why they are unresolved rather than failed.
_DISPATCHED_UNRESOLVED: Final = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.CloseError,
    httpx.RemoteProtocolError,
)


def provider_failure(
    exception: Exception,
    *,
    codes: Mapping[type[Exception], str],
    default: str = PROVIDER_ERROR,
) -> ToolExecutionError:
    """Classify an integration exception into a stable adapter error code.

    Callers list their most specific provider errors first; ``GoogleApiError``
    (the common base) is always a reasonable fallback.

    **The classification is by what can be established about the request, not by
    which exception was raised.** Three cases are separated, because collapsing
    any two of them asserts something this process cannot know:

    1. *Never contacted* \u2014 no connection was established, so nothing was sent.
       :data:`~kai.tools.errors.PROVIDER_NOT_CONTACTED`, a known outcome.
    2. *Dispatched, answer unresolved* \u2014 the request went out and no usable
       answer establishing the outcome came back: no complete response, or a
       success reply whose body cannot be read as the accepted result, or a
       server error that does not say whether the operation was applied.
       :data:`PROVIDER_UNREACHABLE` / :data:`PROVIDER_SERVER_ERROR`.
    3. *Answered* \u2014 the provider replied and the reply is conclusive; a 4xx is
       the provider rejecting the request, which for a write is positive evidence
       that it did not take effect. The caller's own codes, unchanged.

    A generic :class:`httpx.TransportError` with no more specific type is treated
    as case 2 rather than case 1: httpx names the sub-type precisely when it can,
    and assuming the request stayed home would be the unsafe direction to be
    wrong in.
    """
    if isinstance(exception, _NOT_CONTACTED):
        return ToolExecutionError(
            str(exception), error_code=PROVIDER_NOT_CONTACTED
        )
    if isinstance(exception, (*_DISPATCHED_UNRESOLVED, httpx.TransportError)):
        return ToolExecutionError(
            str(exception), error_code=PROVIDER_UNREACHABLE
        )
    # A provider error that carries a 5xx was answered, but the answer does not
    # establish whether a side effect was applied. Read from the status the client
    # attached, never parsed back out of the message text.
    status = getattr(exception, "status_code", None)
    if isinstance(status, int) and status >= 500:
        return ToolExecutionError(
            str(exception), error_code=PROVIDER_SERVER_ERROR
        )
    for exc_type, error_code in codes.items():
        if isinstance(exception, exc_type):
            return ToolExecutionError(str(exception), error_code=error_code)
    return ToolExecutionError(str(exception), error_code=default)


__all__ = ["AdapterBase", "provider_failure"]