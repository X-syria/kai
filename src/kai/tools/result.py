"""The single, structured result type produced by the Tool Gateway.

``ToolResult`` is the *only* way a tool invocation leaves the gateway. It is
JSON-safe except for ``data``, which carries provider payloads (kept
credential-free); the gateway guarantees ``ok``/``verified``/``outcome`` are
coherent: an ``EXECUTION_SUCCEEDED`` result is always ``ok and verified``, an
``EXECUTION_FAILED`` result never is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kai.tools.execution import VerificationMismatch
from kai.tools.risk import RiskLevel


class ToolOutcome(StrEnum):
    """Terminal outcome classification, shared with task states by intent."""

    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    REQUIRES_ATTENTION = "REQUIRES_ATTENTION"
    DENIED = "DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ToolResult:
    """Structured, deterministic answer for exactly one tool invocation."""

    tool_name: str
    outcome: ToolOutcome
    ok: bool
    verified: bool
    risk_level: RiskLevel
    data: Any = None
    error_code: str | None = None
    message: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    mismatches: tuple[VerificationMismatch, ...] = field(default_factory=tuple)
    request_id: str = ""


__all__ = ["ToolOutcome", "ToolResult"]