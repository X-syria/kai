"""KAI Tool Gateway (v0.5 slice).

Separates Tool *Intent* from Tool *Execution*. An upper layer (e.g. the task
planner / a future agent runtime) hands the gateway a validated
``ToolIntent``; the gateway authenticates the task identity, resolves the
application-registered tool, validates arguments, runs the policy / content /
approval boundaries, executes through a provider adapter, and re-asserts the
tool's declared verification contract before producing one ``ToolResult``.

Guarantees:

* The tool catalog is application-controlled and sealed at build time
  (:mod:`kai.tools.catalog`); an unknown or variated tool name is refused.
* Arguments are untrusted; reserved identity/credential keys are structurally
  impossible and re-checked by the gateway.
* Execution happens only through registered adapters over the existing
  tenant-scoped services, never through arbitrary functions or raw HTTP.
* A result is ``verified`` only when the tool's declared verification contract
  held. Execution success without verification yields ``REQUIRES_ATTENTION``.
"""

from kai.tools.boundaries import (
    ApprovalDecision,
    ApprovalDecisionRecord,
    ApprovalManager,
    ContentAuthorization,
    ContentAuthorizer,
    IdentityResolver,
    PolicyDecision,
    PolicyEngine,
    VerificationEngine,
    VerificationOutcome,
)
from kai.tools.catalog import default_definitions, default_registry
from kai.tools.context import ToolContext
from kai.tools.definition import ToolDefinition
from kai.tools.execution import (
    ToolExecutionOutcome,
    ToolExecutor,
    VerificationContract,
    VerificationMismatch,
)
from kai.tools.gateway import ToolGateway
from kai.tools.intent import RESERVED_ARGUMENTS, ToolIntent
from kai.tools.registry import ToolRegistry
from kai.tools.result import ToolOutcome, ToolResult
from kai.tools.risk import RiskLevel
from kai.tools.stubs import (
    ApprovalManagerStub,
    ContentAuthorizerStub,
    ContractVerificationEngine,
    IdentityResolverStub,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalDecisionRecord",
    "ApprovalManager",
    "ApprovalManagerStub",
    "ContentAuthorization",
    "ContentAuthorizer",
    "ContentAuthorizerStub",
    "ContractVerificationEngine",
    "IdentityResolver",
    "IdentityResolverStub",
    "PolicyDecision",
    "PolicyEngine",
    "RESERVED_ARGUMENTS",
    "RiskLevel",
    "ToolContext",
    "ToolDefinition",
    "ToolExecutionOutcome",
    "ToolExecutor",
    "ToolGateway",
    "ToolIntent",
    "ToolOutcome",
    "ToolRegistry",
    "ToolResult",
    "VerificationContract",
    "VerificationEngine",
    "VerificationMismatch",
    "VerificationOutcome",
    "default_definitions",
    "default_registry",
]