"""Application-controlled Tool definitions.

A ``ToolDefinition`` declares everything about a tool *statically*: what it
does, what risk it carries, what capability it maps to, and - critically - how
its output must be verified. The registry is sealed at build time; definitions
can only be added by the application, never by the LLM through arguments.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from kai.tools.execution import ToolExecutor, VerificationContract
from kai.tools.risk import RiskLevel


@dataclass(frozen=True)
class ToolDefinition:
    """Static, application-controlled description of one executable tool."""

    name: str
    description: str
    input_schema: type[BaseModel]
    risk_level: RiskLevel
    capability: str
    executor: ToolExecutor
    verification_contract: VerificationContract
    requires_account: bool = True


__all__ = ["ToolDefinition"]