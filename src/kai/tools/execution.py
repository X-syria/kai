"""Executor protocol and the raw outcome of running one tool.

An executor (adapter) receives validated arguments plus the trusted context
and returns a ``ToolExecutionOutcome``. The outcome is deliberately
*prescriptive*: it already carries ``verified`` and any mismatches discovered
during the provider exchange, which the verification boundary then republishes
after asserting the contract was honored. The gateway never trusts the raw
outcome by itself; a read contract and the adapter's evidence always
revalidate it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from kai.tools.definition import ToolDefinition

from kai.tools.context import ToolContext


class VerificationContract(StrEnum):
    """How a tool's execution must be verified before the result is trusted.

    Contracts are *declared on the Tool definition* by the application, never
    inferred from arguments, so an LLM cannot relax them.
    """

    READ_BACKED = "read_backed"
    READ_AFTER_CREATE = "read_after_create"
    READ_AFTER_SEND = "read_after_send"
    MANUAL = "manual"


@dataclass(frozen=True)
class VerificationMismatch:
    """A field where the provider reality differs from the expectation."""

    field: str
    expected: str
    actual: str


@dataclass(frozen=True)
class ToolExecutionOutcome:
    """Result of actually running a tool against a provider."""

    ok: bool
    data: Any = None
    verified: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)
    mismatches: tuple[VerificationMismatch, ...] = field(default_factory=tuple)
    error_code: str | None = None


class ToolExecutor(Protocol):
    """Thing that turns validated arguments into a provider exchange."""

    async def execute(
        self,
        *,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolExecutionOutcome: ...


__all__ = [
    "ToolExecutionOutcome",
    "ToolExecutor",
    "VerificationContract",
    "VerificationMismatch",
]