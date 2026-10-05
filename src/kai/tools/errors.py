"""Errors raised by the Tool Gateway pipeline.

Every error maps to a deterministic ``ToolOutcome`` + ``error_code`` inside
``kai.tools.gateway.ToolGateway`` so a rejected invocation is still returned
as a structured tool result rather than an uncontrolled exception.
"""

from __future__ import annotations

from typing import Final

TASK_UNRESOLVED: Final = "TASK_UNRESOLVED"
CONTEXT_MISMATCH: Final = "CONTEXT_MISMATCH"
ACCOUNT_REQUIRED: Final = "ACCOUNT_REQUIRED"
TRANSPORT_REQUIRED: Final = "TRANSPORT_REQUIRED"
UNKNOWN_TOOL: Final = "UNKNOWN_TOOL"
RESERVED_ARGUMENT: Final = "RESERVED_ARGUMENT"
VALIDATION_ERROR: Final = "VALIDATION_ERROR"
NOT_IMPLEMENTED: Final = "NOT_IMPLEMENTED"
APPROVAL_STALE: Final = "APPROVAL_STALE"
PROVIDER_ERROR: Final = "PROVIDER_ERROR"
PROVIDER_UNREACHABLE: Final = "PROVIDER_UNREACHABLE"
PROVIDER_NOT_CONTACTED: Final = "PROVIDER_NOT_CONTACTED"
PROVIDER_SERVER_ERROR: Final = "PROVIDER_SERVER_ERROR"

#: Codes meaning **the provider's answer does not establish whether a side
#: effect took place**.
#:
#: These are an *execution-layer* statement. Each one was produced after the
#: request was on the wire, and each is a case where the response cannot settle
#: what the provider did:
#:
#: * :data:`PROVIDER_UNREACHABLE` — the request was dispatched and no response
#:   ever completed. The provider may have applied it, or never received it.
#: * :data:`PROVIDER_SERVER_ERROR` — the provider answered, but with a 5xx. A
#:   server error is a statement about the response, not about whether the write
#:   was committed before the error was raised.
#:
#: **What is deliberately not in this set:**
#:
#: * :data:`PROVIDER_NOT_CONTACTED` — no connection was ever established, so the
#:   request provably never left this process. That is a *known* outcome, and the
#:   opposite of an unknown.
#: * :data:`PROVIDER_ERROR` — a 4xx. The provider received the request and
#:   rejected it, which for a write is positive evidence that it did not apply.
#: * :data:`APPROVAL_STALE` — see below. It is an authorization fact and belongs
#:   to a different layer entirely.
#:
#: Membership here says nothing about whether the tool is a write. Whether an
#: unresolved answer *matters* depends on whether the operation changes the
#: world, which is the caller's fact to supply from the tool definition's
#: ``verification_contract``.
UNRESOLVED_EXTERNAL_CODES: Final[frozenset[str]] = frozenset(
    {PROVIDER_UNREACHABLE, PROVIDER_SERVER_ERROR}
)


def is_unresolved_external(error_code: str | None) -> bool:
    """Did the provider's answer fail to settle what happened on its side?

    A pure test of the code alone, so the same record answers the same way in
    the runtime, in a fulfillment evaluator, and on a recovery in another
    process. ``None`` is ``False``: a run that recorded no error recorded no
    unresolved answer.

    **This never returns True for :data:`APPROVAL_STALE`.** That code reports
    that an invocation was refused at the authorization boundary, and the
    gateway consumes an approval immediately *before* dispatch, so a refusal
    provably stops that invocation before the provider is reached. Folding it in
    here would make "this invocation was not permitted" and "the world is in an
    unknown state" the same statement, and would let a policy refusal be
    reported as an undecidable external outcome.
    """
    return error_code is not None and error_code in UNRESOLVED_EXTERNAL_CODES


__all__ = [
    "ACCOUNT_REQUIRED",
    "APPROVAL_STALE",
    "CONTEXT_MISMATCH",
    "NOT_IMPLEMENTED",
    "PROVIDER_ERROR",
    "PROVIDER_NOT_CONTACTED",
    "PROVIDER_SERVER_ERROR",
    "PROVIDER_UNREACHABLE",
    "UNRESOLVED_EXTERNAL_CODES",
    "is_unresolved_external",
    "RESERVED_ARGUMENT",
    "TASK_UNRESOLVED",
    "TRANSPORT_REQUIRED",
    "UNKNOWN_TOOL",
    "VALIDATION_ERROR",
    "DuplicateToolError",
    "ToolContextError",
    "ToolContextMismatchError",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistryLockedError",
    "ToolReservedArgumentError",
    "ToolValidationError",
]


class ToolError(RuntimeError):
    """Base class for gateway-specific failures."""


class ToolNotFoundError(ToolError, KeyError):
    """Requested tool_name does not exist in the registry."""


class DuplicateToolError(ToolError, ValueError):
    """A tool with the same name is already registered."""


class ToolRegistryLockedError(ToolError):
    """Registration attempted after the registry was sealed."""


class ToolValidationError(ToolError, ValueError):
    """Arguments failed schema validation against the tool input schema."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class ToolReservedArgumentError(ToolError):
    """Arguments include a reserved system key (tenant_id, tokens, ...)."""


class ToolContextError(ToolError):
    """The execution context is unusable (task unresolved, missing transport)."""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        self.error_code = error_code
        super().__init__(message)


class ToolContextMismatchError(ToolError):
    """Intent identity fields disagree with the trusted execution context."""


class ToolExecutionError(ToolError, ValueError):
    """An executor (adapter) failed while talking to a provider.

    ``error_code`` carries a stable provider/error classification and never
    includes request payloads, headers, or credentials.
    """

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        self.error_code = error_code
        super().__init__(message)