"""The boundary between KAI-IR and the Tool Gateway.

Three conversions, and nothing else:

* an ``ActionNode`` becomes a ``ToolIntent`` for the Gateway to judge;
* a ``ToolResult`` becomes an ``ObservationNode``;
* an ``ObservationNode`` becomes the legacy durable evidence record the runtime
  already persists, byte-for-byte, plus additive IR correlation.

Two rules hold at this boundary:

**The IR declares, the Gateway decides.** :func:`intent_from_action` produces an
ordinary ``ToolIntent`` and the same ``ToolGateway.execute`` runs it, so policy,
content, approval, account, and verification are applied exactly as they were
before the IR existed. An ``ActionNode``'s permission node is a requirement
saying what the action *would need*; nothing here grants it, and a mismatch
between the declaration and the Gateway's verdict is resolved in the Gateway's
favour (spec §2.5).

**The IR owns identity, not the transport.** ``action_id`` *is* the Gateway's
request id — the same deterministic value the runtime already derived, so
evidence, approval intent hashes, and request correlation are unchanged — and it
is built only from the task, plan version, step, and position (spec §5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from kai.ir.build import action_id_for
from kai.ir.errors import (
    CLASS_BY_OUTCOME,
    RESPONSE_BY_CLASS,
    ErrorClass,
    IrValidationError,
)
from kai.ir.nodes import ActionNode, ErrorNode, ObservationNode, ResultNode, StepNode
from kai.ir.program import KaiProgram
from kai.tools.intent import ToolIntent
from kai.tools.result import ToolOutcome, ToolResult
from kai.tools.risk import RiskLevel


def action_for(
    *,
    step: StepNode,
    index: int,
    capability: str,
    arguments: dict[str, Any],
    task_id: UUID,
    plan_version: int,
    risk_hint: RiskLevel | None = None,
) -> ActionNode:
    """Build the action for one tool invocation of one step.

    Built here, at execution time, rather than at plan time, because the
    arguments depend on what earlier observations established — the chosen slot,
    the verified attendee. The identity is still fully deterministic.
    """
    return ActionNode(
        action_id=action_id_for(
            task_id=task_id,
            plan_version=plan_version,
            step_order=step.order,
            index=index,
        ),
        capability=capability,
        arguments=dict(arguments),
        risk_hint=risk_hint,
    )


def tool_name_for(action: ActionNode, capability_to_tool: Any) -> str:
    """Resolve the action's capability to a registered tool name, or refuse.

    The mapping is injected, never stored on the action: which tool backs a
    capability is the Gateway's business, and an action that names a tool
    directly would be a program asking for a tool instead of a capability
    (spec §2.4).
    """
    tool_name = capability_to_tool(action.capability)
    if not tool_name:
        raise IrValidationError(
            f"no registered tool backs capability {action.capability!r}"
        )
    return tool_name


def intent_from_action(
    action: ActionNode,
    *,
    tool_name: str,
    task_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
) -> ToolIntent:
    """The ``ToolIntent`` the Gateway is asked to judge and execute.

    ``request_id`` is the action's own id: one identity for the invocation, so
    the IR's log and the Gateway's approval records cannot drift apart.
    """
    return ToolIntent(
        tool_name=tool_name,
        arguments=dict(action.arguments),
        task_id=task_id,
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=action.action_id,
    )


def observation_from_result(
    *,
    action: ActionNode,
    step_order: int,
    result: ToolResult,
    recorded_at: datetime | None = None,
) -> ObservationNode:
    """The durable record of what one action actually did.

    Carries the internal evidence — arguments, provider payload, verification
    evidence — because resume and verification need it. None of this is
    published; the public projection stays a separate redacted layer (spec §7).
    """
    return ObservationNode(
        action_id=action.action_id,
        step_order=step_order,
        outcome=result.outcome,
        ok=bool(result.ok),
        verified=bool(result.verified),
        error_code=result.error_code,
        message=result.message,
        evidence=dict(result.evidence or {}),
        data=result.data,
        arguments=dict(action.arguments),
        request_id=result.request_id or action.action_id,
        recorded_at=recorded_at,
    )


def error_from_observation(observation: ObservationNode) -> ErrorNode | None:
    """The typed failure an observation represents, with its declared response.

    A success is not an error, so a successful observation yields ``None``. The
    class comes from the Gateway's own outcome and the response from the spec's
    table, so a failure is never reported as a bare string.
    """
    if observation.outcome.value == "EXECUTION_SUCCEEDED" and observation.ok:
        return None
    error_class: ErrorClass = CLASS_BY_OUTCOME[observation.outcome]
    return ErrorNode(
        error_code=observation.error_code or f"{observation.outcome.value.lower()}",
        error_class=error_class,
        response=RESPONSE_BY_CLASS[error_class],
    )


def result_for(
    *,
    result: str,
    reason: str,
    observations: list[ObservationNode],
) -> ResultNode:
    """How the program ended, and which observations decided it."""
    return ResultNode(
        result=result,
        reason=reason,
        final_observation_ids=[item.action_id for item in observations],
    )


def evidence_from_observation(
    observation: ObservationNode, *, tool_name: str
) -> dict[str, Any]:
    """The durable evidence record for an observation, in the runtime's shape.

    Reproduces exactly the record the runtime persists today — same keys, same
    values, same JSON-safe coercion — so resume, the slot/event readers, and the
    success check keep working on IR-produced records without knowing the IR
    exists. The three ``ir_*`` keys are additive and internal: the public
    projection is an explicit allow-list, so they cannot widen what is published.
    """
    return {
        "step_order": observation.step_order,
        "tool": tool_name,
        "args": _json_safe(dict(observation.arguments)),
        "outcome": observation.outcome.value,
        "ok": bool(observation.ok),
        "verified": bool(observation.verified),
        "error_code": observation.error_code,
        "message": observation.message,
        "data": _json_safe(observation.data),
        "evidence": _json_safe(dict(observation.evidence)),
        "request_id": observation.request_id,
        "ir_action_id": observation.action_id,
    }


def observation_from_evidence(
    record: dict[str, Any], *, step_order: int | None = None
) -> ObservationNode | None:
    """Rebuild an observation from a persisted evidence record, or ``None``.

    The reverse direction, used when the runtime reads its own durable log back:
    a record this cannot make sense of is refused rather than half-restored, so
    a schema drift can never produce a plausible-looking observation.
    """
    outcome_value = record.get("outcome")
    if not isinstance(outcome_value, str):
        return None
    try:
        outcome = ToolOutcome(outcome_value)
    except ValueError:
        return None
    order = record.get("step_order") if step_order is None else step_order
    if not isinstance(order, int):
        return None
    arguments = record.get("args")
    evidence = record.get("evidence")
    return ObservationNode(
        action_id=str(record.get("ir_action_id") or record.get("request_id") or ""),
        step_order=order,
        outcome=outcome,
        ok=bool(record.get("ok")),
        verified=bool(record.get("verified")),
        error_code=record.get("error_code"),
        message=str(record.get("message") or ""),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
        data=record.get("data"),
        arguments=dict(arguments) if isinstance(arguments, dict) else {},
        request_id=str(record.get("request_id") or ""),
    )


def attach(program: KaiProgram, observation: ObservationNode) -> ObservationNode:
    """Append one observation to the program's log and return it."""
    return program.record(observation)


def record_error(
    program: KaiProgram, observation: ObservationNode
) -> ErrorNode | None:
    """Derive the typed failure an observation represents and keep it on the program.

    The single place ``error_from_observation`` is called from execution, so the
    IR's own error taxonomy is applied to every recorded action rather than
    leaving failures as bare strings in Runtime code.

    A success yields ``None`` and records nothing. This is bookkeeping only: the
    returned error is not read by any control-flow decision, and the observation
    itself is unaffected, so a caller that ignores the return value changes
    nothing about what runs.
    """
    error = error_from_observation(observation)
    if error is not None:
        program.record_error(error)
    return error


def record_result(
    program: KaiProgram, *, result: str, reason: str
) -> ResultNode:
    """Note how the program ended, from the observations it recorded.

    ``final_observation_ids`` therefore names the actions *this attempt* ran. The
    program's observation log holds one attempt, because a program is rebuilt per
    trigger; the complete, gap-free history stays in the ``RuntimeExecution``
    evidence log, which is what an audit reads.

    ``result`` must be a reportable result state, so a task parked in
    ``WAITING_APPROVAL`` cannot be recorded — a paused run has not ended, and
    inventing a result for it would assert something untrue.
    """
    return program.record_result(
        result_for(result=result, reason=reason, observations=program.observations)
    )


def _json_safe(value: Any) -> Any:
    """Coerce a provider payload to JSON-safe values, dropping what is not."""
    import json

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return None


__all__ = [
    "action_for",
    "attach",
    "error_from_observation",
    "evidence_from_observation",
    "intent_from_action",
    "observation_from_evidence",
    "observation_from_result",
    "record_error",
    "record_result",
    "result_for",
    "tool_name_for",
]
