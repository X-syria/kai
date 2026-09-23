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

__all__ = [
    "ACCOUNT_REQUIRED",
    "CONTEXT_MISMATCH",
    "NOT_IMPLEMENTED",
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