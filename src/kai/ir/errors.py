"""KAI-IR error taxonomy and validation failure type.

The classes and responses are the v0 set from ``docs/kai-ir-spec-v0.md`` §6. A
failure in the IR is never an unstructured string: the class determines the
declared response, and ``error_code`` is always a stable application string,
never a raw provider or exception message.
"""

from __future__ import annotations

from enum import StrEnum

from kai.tools.result import ToolOutcome


class ErrorClass(StrEnum):
    """v0 error classification; the class decides the declared response."""

    PERMANENT = "PERMANENT"
    TRANSIENT = "TRANSIENT"
    POLICY = "POLICY"
    APPROVAL = "APPROVAL"
    IDENTITY = "IDENTITY"
    BUDGET = "BUDGET"
    INTERNAL = "INTERNAL"


class ErrorResponse(StrEnum):
    """What a program declares it will do about a failure it anticipated."""

    STOP = "STOP"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"
    ATTENTION = "ATTENTION"


#: The declared response for each class, per spec §6. Kept as data so the
#: mapping is inspectable and testable rather than scattered in branches.
RESPONSE_BY_CLASS: dict[ErrorClass, ErrorResponse] = {
    ErrorClass.PERMANENT: ErrorResponse.STOP,
    ErrorClass.TRANSIENT: ErrorResponse.RETRY,
    ErrorClass.POLICY: ErrorResponse.STOP,
    ErrorClass.APPROVAL: ErrorResponse.ATTENTION,
    ErrorClass.IDENTITY: ErrorResponse.STOP,
    ErrorClass.BUDGET: ErrorResponse.STOP,
    ErrorClass.INTERNAL: ErrorResponse.ESCALATE,
}

#: Which class a gateway outcome belongs to. The Gateway's ``ToolOutcome`` is
#: the system's own vocabulary; this is the only place the two are related, so
#: adding an outcome has to be a deliberate, visible decision here.
CLASS_BY_OUTCOME: dict[ToolOutcome, ErrorClass] = {
    ToolOutcome.EXECUTION_SUCCEEDED: ErrorClass.TRANSIENT,
    ToolOutcome.EXECUTION_FAILED: ErrorClass.PERMANENT,
    ToolOutcome.WAITING_EXTERNAL: ErrorClass.TRANSIENT,
    ToolOutcome.REQUIRES_ATTENTION: ErrorClass.INTERNAL,
    ToolOutcome.DENIED: ErrorClass.POLICY,
    ToolOutcome.APPROVAL_REQUIRED: ErrorClass.APPROVAL,
    ToolOutcome.REJECTED: ErrorClass.APPROVAL,
}


class IrValidationError(ValueError):
    """Raised when a program is not a valid, executable KAI-IR program.

    Deliberately a *build-time* failure: nothing partially valid is ever
    returned to a caller, because spec §2.1 makes validated IR the only thing
    that may be executed.
    """


__all__ = [
    "CLASS_BY_OUTCOME",
    "RESPONSE_BY_CLASS",
    "ErrorClass",
    "ErrorResponse",
    "IrValidationError",
]
