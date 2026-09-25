"""The Agent Runtime: one guarded vertical slice over the job system.

The runtime owns fixed orchestration (submit/trigger), the sealed slice
contract, LLM-as-data understanding, deterministic identity and slot logic,
and the resume-safe execution envelope. Execution authority stays with the
Tool Gateway; the runtime only builds intents and reads deterministic results.
"""

from kai.runtime.deps import get_runtime_gateway, get_runtime_service
from kai.runtime.models import RuntimeExecution
from kai.runtime.schemas import (
    GoalSubmission,
    RuntimeTaskResponse,
    SubmitResponse,
    TriggerResponse,
    UnderstandingOutput,
)
from kai.runtime.service import (
    AgentRuntime,
    RuntimeFatal,
    RuntimeNotFoundError,
    RuntimeResumeError,
    TriggerOutcome,
)
from kai.runtime.understanding import UnderstandingFailed, understand_scenario

__all__ = [
    "AgentRuntime",
    "GoalSubmission",
    "RuntimeExecution",
    "RuntimeFatal",
    "RuntimeNotFoundError",
    "RuntimeResumeError",
    "RuntimeTaskResponse",
    "SubmitResponse",
    "TriggerOutcome",
    "TriggerResponse",
    "UnderstandingFailed",
    "UnderstandingOutput",
    "get_runtime_gateway",
    "get_runtime_service",
    "understand_scenario",
]