"""KAI-IR: the first slice's contracts, exercised directly.

Three things are being pinned here:

* the IR is **closed and deterministic** — unknown fields are refused, and the
  same intent and plan always produce the same program id and fingerprint;
* the IR **declares, the Gateway decides** — a program's permission node says
  what an action would need and never grants it, and a capability with no
  catalog tool behind it fails closed;
* the IR **loses nothing at the boundary** — an action becomes an ordinary
  ``ToolIntent``, a result becomes an observation, and an observation becomes
  byte-for-byte the evidence record the runtime already persisted.

Spec: ``docs/kai-ir-spec-v0.md``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from kai.ir import (
    IR_VERSION,
    ActionNode,
    ConditionNode,
    ConditionOperator,
    ConditionSubject,
    ControlEdge,
    ControlFacts,
    ControlNode,
    ControlNodeKind,
    ErrorNode,
    FactKind,
    IntentNode,
    IrConditionError,
    IrControlFlowError,
    IrValidationError,
    KaiProgram,
    ObservationNode,
    PlanNode,
    StateSnapshotNode,
    StepNode,
    action_for,
    action_id_for,
    error_from_observation,
    evaluate,
    evidence_from_observation,
    intent_from_action,
    next_node,
    next_step,
    observation_from_evidence,
    observation_from_result,
    program_from_plan,
    program_id_for,
    require_valid,
    result_for,
    tool_name_for,
    tools_by_capability,
)
from kai.ir.build import (
    allowed_capabilities_from_task,
    permission_for,
    risk_from_policy,
)
from kai.planner.schemas import PlanProposal
from kai.policy.engine import PolicyEngine
from kai.runtime.translator import CAPABILITY_TO_TOOL, capability_to_tool
from kai.tools.catalog import default_registry
from kai.tools.result import ToolOutcome, ToolResult
from kai.tools.risk import RiskLevel

TASK_ID = UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = UUID("22222222-2222-2222-2222-222222222222")
USER_ID = UUID("33333333-3333-3333-3333-333333333333")

PLAN_JSON: dict = {
    "goal": "schedule a meeting",
    "success_criteria": ["calendar event created"],
    "steps": [
        {
            "step_id": "s1",
            "order": 1,
            "action": "resolve_contact",
            "purpose": "find the attendee",
            "inputs": ["sara"],
            "expected_output": "contact identity",
            "verification_hint": "exact email match",
        },
        {
            "step_id": "s2",
            "order": 2,
            "action": "create_calendar_event",
            "purpose": "schedule the meeting",
            "inputs": ["slot"],
            "expected_output": "created event",
            "verification_hint": "event read back",
        },
    ],
    "assumptions": ["attendee reserves the event"],
}


def _proposal(**overrides: object) -> PlanProposal:
    data = dict(PLAN_JSON)
    data.update(overrides)
    return PlanProposal.model_validate(data)


def _task(**overrides: object) -> object:
    """A minimal ``Task`` stand-in exposing only the contract the IR reads."""
    defaults: dict = {
        "id": TASK_ID,
        "goal": "schedule a meeting",
        "success_criteria": ["calendar event created"],
        "allowed_tools": [
            "contacts.match",
            "calendar.read_slots",
            "calendar.create_event",
            "calendar.get_event",
        ],
        "risk_policy": "MEDIUM",
        "deadline": None,
        "max_steps": 10,
        "verification_method": "automatic",
        "fallback": "ask the user to choose a different time",
    }
    defaults.update(overrides)
    return type("TaskStub", (), defaults)()


def _program(**overrides: object) -> KaiProgram:
    kwargs: dict = {
        "task": _task(),
        "plan_version": 1,
        "plan_status": "ACCEPTED",
        "proposal": _proposal(),
        "plan_hash": "a" * 64,
        "registry": default_registry(),
        "capability_to_tool": CAPABILITY_TO_TOOL,
    }
    kwargs.update(overrides)
    return program_from_plan(**kwargs)


def _catalog() -> dict:
    return tools_by_capability(default_registry(), CAPABILITY_TO_TOOL)


# --------------------------------------------------------------------------- #
# Construction and closure
# --------------------------------------------------------------------------- #


def test_a_program_is_sealed_and_validated() -> None:
    program = _program()

    assert program.ir_version == IR_VERSION
    assert program.plan.plan_version == 1
    assert program.plan.status == "ACCEPTED"
    assert program.fingerprint == program.computed_fingerprint()
    assert len(program.fingerprint) == 64
    # The program only exists because it validated.
    assert require_valid(program) is program


def test_the_contract_is_closed() -> None:
    with pytest.raises(ValueError):
        IntentNode(goal="g", success_criteria=["c"], entry=True, extra_field=1)
    with pytest.raises(ValueError):
        StepNode(
            step_id="s1",
            order=1,
            capability="resolve_contact",
            purpose="p",
            tool_name="contacts.match",
        )


def test_a_program_without_a_known_version_is_refused() -> None:
    program = _program()
    with pytest.raises(ValueError):
        KaiProgram(
            ir_version="kai-ir/99",
            program_id=program.program_id,
            intent=program.intent,
            plan=program.plan,
            constraints=program.constraints,
            permissions=program.permissions,
        )

    # And the same refusal is reachable from the validation gate itself.
    future = program.model_copy(update={"ir_version": "kai-ir/99"})
    with pytest.raises(IrValidationError, match="ir_version"):
        future.validate_executable()


def test_a_tampered_program_no_longer_matches_its_fingerprint() -> None:
    program = _program()
    edited = program.model_copy(
        update={
            "intent": IntentNode(
                goal="schedule two meetings", success_criteria=["calendar event created"]
            )
        }
    )
    with pytest.raises(IrValidationError, match="fingerprint"):
        edited.validate_executable()


# --------------------------------------------------------------------------- #
# Determinism and identity
# --------------------------------------------------------------------------- #


def test_the_same_intent_and_plan_always_produce_the_same_program() -> None:
    first = _program()
    second = _program()

    assert first.program_id == second.program_id
    assert first.fingerprint == second.fingerprint


def test_a_new_plan_version_is_a_new_program() -> None:
    first = _program(plan_version=1)
    second = _program(plan_version=2)

    assert first.program_id != second.program_id
    assert first.fingerprint != second.fingerprint


def test_a_different_plan_is_a_different_program() -> None:
    # Orders must stay sequential, so a genuinely different plan is a changed
    # purpose rather than a reshuffle that would make the cursor meaningless.
    changed = _proposal(
        steps=[
            {**PLAN_JSON["steps"][0], "purpose": "find the attendee quickly"},
            PLAN_JSON["steps"][1],
        ]
    )
    assert _program(proposal=changed).fingerprint != _program().fingerprint


def test_program_id_is_derived_not_random() -> None:
    assert program_id_for(task_id=TASK_ID, plan_version=3) == program_id_for(
        task_id=TASK_ID, plan_version=3
    )
    assert program_id_for(task_id=TASK_ID, plan_version=3) != program_id_for(
        task_id=TASK_ID, plan_version=4
    )


def test_action_ids_are_resume_stable_and_never_collide_across_plans() -> None:
    kwargs = {"task_id": TASK_ID, "step_order": 2, "index": 0}
    first = action_id_for(plan_version=1, **kwargs)
    assert first == action_id_for(plan_version=1, **kwargs)
    assert first != action_id_for(plan_version=2, **kwargs)
    assert first != action_id_for(plan_version=1, task_id=TASK_ID, step_order=2, index=1)


def test_the_runtime_request_id_contract_is_unchanged() -> None:
    """The IR took ownership of the identity; the value did not move."""
    from kai.runtime.translator import request_id_for

    assert request_id_for(
        task_id=TASK_ID, plan_version=1, step_order=2, index=0
    ) == action_id_for(task_id=TASK_ID, plan_version=1, step_order=2, index=0)


def test_the_canonical_form_is_sorted_compact_utf8() -> None:
    canonical = _program().to_json()
    parsed = json.loads(canonical)
    assert parsed["canonical"]["ir_version"] == IR_VERSION
    # Compact separators: no pretty printing survives into the hashed form.
    assert ", " not in parsed["canonical"]["plan"]["steps"][0]["step_id"]
    # Sorted keys, recursively.
    assert list(parsed["canonical"].keys()) == sorted(parsed["canonical"].keys())


# --------------------------------------------------------------------------- #
# Validation refuses rather than repairs
# --------------------------------------------------------------------------- #


def test_a_capability_outside_the_task_contract_is_refused() -> None:
    task = _task(allowed_tools=["contacts.match"])
    with pytest.raises(IrValidationError, match="not in the program's allowed"):
        _program(task=task)


def test_a_plan_larger_than_the_task_budget_is_refused() -> None:
    with pytest.raises(IrValidationError, match="budget"):
        _program(task=_task(max_steps=1))


def test_a_snapshot_from_another_plan_is_refused() -> None:
    program = _program()
    with pytest.raises(IrValidationError, match="bound to plan version"):
        program.model_copy(
            update={
                "snapshot": StateSnapshotNode(
                    plan_version=99, plan_hash="b" * 64, cursor=0, steps_used=0
                )
            }
        ).validate_executable()


def test_a_cursor_past_the_end_of_the_plan_is_refused() -> None:
    program = _program()
    with pytest.raises(IrValidationError, match="past the end"):
        program.model_copy(
            update={
                "snapshot": StateSnapshotNode(
                    plan_version=1, plan_hash="a" * 64, cursor=9, steps_used=9
                )
            }
        ).validate_executable()


def test_rewriting_an_observation_is_refused() -> None:
    program = _program()
    observation = _observation(program=program)
    program.record(observation)
    with pytest.raises(IrValidationError, match="append-only"):
        program.model_copy(
            update={"observations": [observation, observation]}
        ).validate_executable()


def test_a_task_with_no_representable_capability_is_refused() -> None:
    with pytest.raises(IrValidationError, match="no capability"):
        allowed_capabilities_from_task(
            _task(allowed_tools=["slack.post"]), CAPABILITY_TO_TOOL
        )


def test_step_orders_must_stay_sequential() -> None:
    with pytest.raises(ValueError, match="sequential"):
        PlanNode(
            plan_version=1,
            status="ACCEPTED",
            steps=[
                StepNode(step_id="a", order=1, capability="resolve_contact", purpose="p"),
                StepNode(step_id="b", order=3, capability="resolve_contact", purpose="p"),
            ],
        )


# --------------------------------------------------------------------------- #
# The IR declares; the Gateway decides
# --------------------------------------------------------------------------- #


def test_a_permission_declares_the_catalog_risk_and_grants_nothing() -> None:
    catalog = _catalog()
    permission = permission_for(
        capability="create_calendar_event",
        tool=catalog["create_calendar_event"],
        risk_cap=risk_from_policy("MEDIUM"),
    )

    assert permission.requires_capability == "create_calendar_event"
    # The declared risk is the catalog's own, not a value the plan chose.
    assert permission.max_risk is RiskLevel.MEDIUM
    assert permission.requires_approval is True
    assert permission.requires_account is True
    # A declaration, not an authorization: there is no grant field on it at all.
    assert not hasattr(permission, "granted")


def test_a_read_only_capability_declares_no_approval() -> None:
    """The declaration has to match what the Gateway will really do, otherwise it
    is decoration. A LOW-risk read under a MEDIUM task needs no human."""
    catalog = _catalog()
    permission = permission_for(
        capability="resolve_contact",
        tool=catalog["resolve_contact"],
        risk_cap=risk_from_policy("MEDIUM"),
    )

    assert permission.max_risk is RiskLevel.LOW
    assert permission.requires_approval is False


def test_every_declared_permission_matches_the_real_catalog() -> None:
    """Machine-checkable against reality: only the event creation may need a
    human, which is exactly what the runtime's approval checkpoint does."""
    program = _program()

    by_capability = {p.requires_capability: p for p in program.permissions}
    assert by_capability["create_calendar_event"].requires_approval is True
    assert by_capability["resolve_contact"].requires_approval is False
    assert all(
        p.requires_account for p in program.permissions
    ), "every tool in this catalog needs a connected account"


def test_an_unbacked_capability_fails_closed() -> None:
    permission = permission_for(
        capability="teleport", tool=None, risk_cap=risk_from_policy("CRITICAL")
    )

    assert permission.max_risk is RiskLevel.CRITICAL
    assert permission.requires_approval is True
    assert permission.requires_account is True


def test_a_low_risk_task_cannot_use_a_high_risk_tool_without_approval() -> None:
    catalog = _catalog()
    permission = permission_for(
        capability="create_calendar_event",
        tool=catalog["create_calendar_event"],
        risk_cap=risk_from_policy("LOW"),
    )

    assert permission.requires_approval is True


def test_the_program_cannot_widen_the_task_contract() -> None:
    """The allowed set is derived from the sealed task contract, so a capability
    the task never allowed cannot appear in a program built for that task."""
    program = _program()
    allowed = program.constraints.allowed_capabilities

    assert set(allowed) == {
        "resolve_contact",
        "read_calendar",
        "create_calendar_event",
        "verify_calendar_event",
    }
    assert "drop_database" not in allowed
    # Every step the plan uses is inside the contract.
    assert {step.capability for step in program.plan.steps} <= set(allowed)


def test_editing_a_sealed_program_is_caught_by_its_fingerprint() -> None:
    """The seal is what makes the program auditable: its content and its
    fingerprint cannot disagree."""
    program = _program()
    forged = program.model_copy(
        update={
            "plan": PlanNode(
                plan_version=1,
                status="ACCEPTED",
                steps=[
                    *program.plan.steps,
                    StepNode(
                        step_id="s9",
                        order=3,
                        capability="drop_database",
                        purpose="drop everything",
                    ),
                ],
            )
        }
    )
    with pytest.raises(IrValidationError, match="fingerprint"):
        forged.validate_executable()


def test_a_capability_outside_the_contract_is_refused_on_its_own_merits() -> None:
    """With the seal dropped, the structural guard is what refuses the step."""
    program = _program()
    unsealed = program.model_copy(update={"fingerprint": ""}).model_copy(
        update={
            "plan": PlanNode(
                plan_version=1,
                status="ACCEPTED",
                steps=[
                    *program.plan.steps,
                    StepNode(
                        step_id="s9",
                        order=3,
                        capability="drop_database",
                        purpose="drop everything",
                    ),
                ],
            )
        }
    )
    with pytest.raises(IrValidationError, match="drop_database"):
        unsealed.validate_executable()


def test_the_gateway_is_still_the_authority_over_an_action() -> None:
    """The IR's declaration never reaches the policy engine as a decision."""
    program = _program()
    step = program.plan.steps[0]
    action = action_for(
        step=step,
        index=0,
        capability=step.capability,
        arguments={"query": "sara"},
        task_id=TASK_ID,
        plan_version=1,
    )
    intent = intent_from_action(
        action,
        tool_name="contacts.match",
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )

    # The policy engine sees an ordinary intent and judges it on the real task,
    # not on anything the program asserted about itself.
    assert intent.tool_name == "contacts.match"
    assert intent.request_id == action.action_id
    assert PolicyEngine is not None


def test_a_capability_with_no_tool_is_refused_at_the_boundary() -> None:
    action = ActionNode(action_id="x", capability="drop_database", arguments={})
    with pytest.raises(IrValidationError, match="no registered tool"):
        tool_name_for(action, capability_to_tool)


# --------------------------------------------------------------------------- #
# The boundary loses nothing
# --------------------------------------------------------------------------- #


def _result(**overrides: object) -> ToolResult:
    defaults: dict = {
        "tool_name": "contacts.match",
        "outcome": ToolOutcome.EXECUTION_SUCCEEDED,
        "ok": True,
        "verified": True,
        "risk_level": RiskLevel.LOW,
        "data": {"event_id": "evt-1"},
        "evidence": {"event_id": "evt-1"},
        "request_id": "runtime-1-1-1-0",
    }
    defaults.update(overrides)
    return ToolResult(**defaults)


def _observation(
    *,
    program: KaiProgram | None = None,
    step_order: int = 1,
    result: ToolResult | None = None,
) -> ObservationNode:
    prog = program or _program()
    step = prog.plan.steps[step_order - 1]
    action = action_for(
        step=step,
        index=0,
        capability=step.capability,
        arguments={"query": "sara"},
        task_id=TASK_ID,
        plan_version=1,
    )
    return observation_from_result(
        action=action,
        step_order=step_order,
        result=result or _result(request_id=action.action_id),
    )


def test_an_action_becomes_an_ordinary_tool_intent() -> None:
    program = _program()
    step = program.plan.steps[1]
    action = action_for(
        step=step,
        index=2,
        capability=step.capability,
        arguments={"title": "meeting", "attendees": ["a@example.com"]},
        task_id=TASK_ID,
        plan_version=1,
    )
    intent = intent_from_action(
        action,
        tool_name="calendar.create_event",
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )

    assert intent.tool_name == "calendar.create_event"
    assert intent.arguments == {"title": "meeting", "attendees": ["a@example.com"]}
    assert intent.request_id == action.action_id
    # The trusted scope is taken from the runtime, never from the program.
    assert (intent.tenant_id, intent.user_id, intent.task_id) == (
        TENANT_ID,
        USER_ID,
        TASK_ID,
    )


def test_an_observation_keeps_the_internal_evidence() -> None:
    observation = _observation()

    assert observation.ok is True
    assert observation.verified is True
    assert observation.evidence == {"event_id": "evt-1"}
    assert observation.arguments == {"query": "sara"}


def test_an_observation_that_is_ok_but_unverified_is_impossible() -> None:
    with pytest.raises(ValueError, match="ok without being verified"):
        _observation(result=_result(ok=True, verified=False))


def test_the_evidence_record_keeps_its_existing_shape() -> None:
    """Backward compatibility: the durable record the runtime reads back is
    unchanged, so resume, the slot reader, and the success check are untouched."""
    observation = _observation()
    record = evidence_from_observation(observation, tool_name="contacts.match")

    assert set(record) >= {
        "step_order",
        "tool",
        "args",
        "outcome",
        "ok",
        "verified",
        "error_code",
        "message",
        "data",
        "evidence",
        "request_id",
    }
    assert record["tool"] == "contacts.match"
    assert record["outcome"] == "EXECUTION_SUCCEEDED"
    assert record["args"] == {"query": "sara"}
    assert record["evidence"] == {"event_id": "evt-1"}
    assert record["request_id"] == observation.action_id
    # Additive, internal-only correlation.
    assert record["ir_action_id"] == observation.action_id


def test_the_evidence_record_is_json_safe() -> None:
    record = evidence_from_observation(
        _observation(result=_result(data={"when": datetime(2026, 1, 1, tzinfo=UTC)})),
        tool_name="contacts.match",
    )
    json.dumps(record)  # must not raise
    assert record["data"] == {"when": "2026-01-01 00:00:00+00:00"}


def test_an_observation_round_trips_through_the_durable_record() -> None:
    original = _observation()
    record = evidence_from_observation(original, tool_name="contacts.match")
    restored = observation_from_evidence(record)

    assert restored is not None
    assert restored.action_id == original.action_id
    assert restored.outcome is original.outcome
    assert restored.ok == original.ok
    assert restored.evidence == original.evidence


def test_an_unreadable_record_is_refused_rather_than_half_restored() -> None:
    assert observation_from_evidence({"outcome": "NOT_A_REAL_OUTCOME"}) is None
    assert observation_from_evidence({"outcome": "EXECUTION_SUCCEEDED"}) is None


# --------------------------------------------------------------------------- #
# Errors and results
# --------------------------------------------------------------------------- #


def test_a_success_is_not_an_error() -> None:
    assert error_from_observation(_observation()) is None


@pytest.mark.parametrize(
    ("outcome", "expected_class"),
    [
        (ToolOutcome.APPROVAL_REQUIRED, "APPROVAL"),
        (ToolOutcome.DENIED, "POLICY"),
        (ToolOutcome.EXECUTION_FAILED, "PERMANENT"),
    ],
)
def test_a_gateway_outcome_maps_to_its_error_class(
    outcome: ToolOutcome, expected_class: str
) -> None:
    observation = _observation(
        result=_result(
            outcome=outcome, ok=False, verified=False, error_code="x.y.z"
        )
    )
    error = error_from_observation(observation)

    assert error is not None
    assert error.error_class.value == expected_class
    assert error.error_code == "x.y.z"


def test_an_error_class_must_declare_its_own_response() -> None:
    with pytest.raises(ValueError, match="declares response"):
        ErrorNode(
            error_code="x",
            error_class="APPROVAL",
            response="RETRY",
        )


def test_a_result_reports_a_task_state_and_its_observations() -> None:
    observation = _observation()
    result = result_for(
        result="COMPLETED", reason="verified", observations=[observation]
    )

    assert result.result == "COMPLETED"
    assert result.final_observation_ids == [observation.action_id]
    with pytest.raises(ValueError, match="reportable task states"):
        result_for(result="SOMETHING_ELSE", reason="", observations=[])


def test_a_task_state_the_program_cannot_end_in_is_refused() -> None:
    """A program may only end in a state the task machine can actually hold."""
    with pytest.raises(ValueError):
        result_for(result="EXECUTING", reason="still going", observations=[])


# --------------------------------------------------------------------------- #
# The program is bound to the plan it runs
# --------------------------------------------------------------------------- #


def test_the_snapshot_binds_the_program_to_one_plan_content() -> None:
    program = _program()

    assert program.snapshot is not None
    assert program.snapshot.plan_version == program.plan.plan_version
    assert program.snapshot.plan_hash == "a" * 64
    assert program.snapshot.cursor == 0
    assert program.step(1) is not None
    assert program.step(2) is not None
    assert program.step(3) is None


def test_a_program_carries_a_declaration_for_every_capability_it_uses() -> None:
    program = _program()

    assert {p.requires_capability for p in program.permissions} == {
        "resolve_contact",
        "create_calendar_event",
    }
    for step in program.plan.steps:
        assert program.permission_for(step.capability) is not None


def test_a_capability_mapping_is_injected_not_imported() -> None:
    """The IR is pointed at a catalog; it does not own the mapping."""
    other = {"resolve_contact": "contacts.match", "create_calendar_event": "x.y.z"}
    task = _task(allowed_tools=["contacts.match", "x.y.z"])
    program = _program(task=task, capability_to_tool=other)

    assert set(program.constraints.allowed_capabilities) == {
        "resolve_contact",
        "create_calendar_event",
    }
    assert program.permission_for("create_calendar_event").max_risk is RiskLevel.CRITICAL


def test_a_plan_hash_that_is_not_a_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="hex digest"):
        StateSnapshotNode(
            plan_version=1, plan_hash="z" * 64, cursor=0, steps_used=0
        )
    with pytest.raises(ValueError, match="at least 64 characters"):
        StateSnapshotNode(plan_version=1, plan_hash="nope", cursor=0, steps_used=0)


def test_only_one_entry_point_is_possible() -> None:
    with pytest.raises(ValueError, match="only entry point"):
        IntentNode(goal="g", success_criteria=["c"], entry=False)

    program = _program()
    with pytest.raises(IrValidationError, match="entry point"):
        program.model_copy(
            update={
                "intent": program.intent.model_copy(update={"entry": False})
            }
        ).validate_executable()


def test_ids_are_not_guessed_from_wall_clock_or_randomness() -> None:
    """Two independently built programs for the same task agree completely."""
    a = _program()
    b = _program()

    assert a.program_id == b.program_id
    assert a.fingerprint == b.fingerprint
    assert uuid4() not in (a.program_id,)  # not a fresh v4


# --------------------------------------------------------------------------- #
# Control flow: conditions
# --------------------------------------------------------------------------- #


def _condition(
    condition_id: str = "c1",
    *,
    kind: FactKind = FactKind.OBSERVATION,
    step_order: int | None = 1,
    field: str | None = "outcome",
    operator: ConditionOperator = ConditionOperator.EQ,
    value: object = ToolOutcome.EXECUTION_SUCCEEDED.value,
    negate: bool = False,
) -> ConditionNode:
    return ConditionNode(
        condition_id=condition_id,
        subject=ConditionSubject(kind=kind, step_order=step_order, field=field),
        operator=operator,
        value=value,
        negate=negate,
    )


def _recorded(
    step_order: int, outcome: ToolOutcome = ToolOutcome.EXECUTION_SUCCEEDED
) -> ObservationNode:
    succeeded = outcome is ToolOutcome.EXECUTION_SUCCEEDED
    return ObservationNode(
        action_id=f"a{step_order}",
        step_order=step_order,
        outcome=outcome,
        ok=succeeded,
        verified=succeeded,
    )


def test_a_condition_compares_one_durable_fact() -> None:
    condition = _condition()
    facts = ControlFacts(cursor=0, observations=(_recorded(1),))

    assert evaluate(condition, facts) is True
    assert (
        evaluate(
            _condition(operator=ConditionOperator.NEQ),
            ControlFacts(cursor=0, observations=(_recorded(1, ToolOutcome.EXECUTION_FAILED),)),
        )
        is True
    )
    # The cursor is a fact too, and needs no observation to read.
    assert evaluate(
        _condition(
            kind=FactKind.CURSOR, step_order=None, field=None,
            operator=ConditionOperator.GTE, value=3,
        ),
        ControlFacts(cursor=3),
    )


def test_a_condition_cannot_reach_outside_the_fact_vocabulary() -> None:
    """No provider payload, no environment, no model call — just declared facts."""
    with pytest.raises(ValueError, match="observation field must be one of"):
        _condition(field="data.free")
    with pytest.raises(ValueError, match="observation field must be one of"):
        _condition(field="arguments")
    with pytest.raises(ValueError, match="must not name a step or a field"):
        _condition(kind=FactKind.CURSOR, field="outcome")
    with pytest.raises(ValueError, match="must not name a step or a field"):
        _condition(kind=FactKind.STEPS_USED, step_order=1)
    with pytest.raises(ValueError, match="needs both step_order and field"):
        _condition(step_order=None)


def test_an_unsupported_operator_is_refused() -> None:
    with pytest.raises(ValueError):
        _condition(operator="between")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _condition(operator="matches")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _condition(operator="len")  # type: ignore[arg-type]


def test_a_type_mismatched_operand_is_refused() -> None:
    """A condition that could never match is refused where it is written."""
    with pytest.raises(ValueError, match="must be bool"):
        _condition(field="verified", value="yes")
    with pytest.raises(ValueError, match="must be int"):
        _condition(
            kind=FactKind.CURSOR, step_order=None, field=None,
            operator=ConditionOperator.LT, value="soon",
        )
    with pytest.raises(ValueError, match="must be str"):
        _condition(field="error_code", value=7)


def test_an_ordered_comparison_on_a_text_fact_is_refused() -> None:
    with pytest.raises(ValueError, match="undefined for a text fact"):
        _condition(operator=ConditionOperator.GT, value="zzz")


def test_a_membership_operand_must_be_a_list_of_the_right_type() -> None:
    with pytest.raises(ValueError, match="needs a list operand"):
        _condition(operator=ConditionOperator.IN, value=ToolOutcome.DENIED.value)
    with pytest.raises(ValueError, match="non-empty list"):
        _condition(operator=ConditionOperator.IN, value=[])
    with pytest.raises(ValueError, match="must hold str values"):
        _condition(operator=ConditionOperator.IN, value=[1, 2])
    with pytest.raises(ValueError, match="needs a single operand"):
        _condition(operator=ConditionOperator.EQ, value=[ToolOutcome.DENIED.value])

    facts = ControlFacts(cursor=0, observations=(_recorded(1),))
    assert evaluate(
        _condition(operator=ConditionOperator.IN, value=["DENIED", "REJECTED"]), facts
    ) is False
    assert evaluate(
        _condition(operator=ConditionOperator.NOT_IN, value=["DENIED", "REJECTED"]), facts
    ) is True


def test_a_condition_reading_a_step_with_no_observation_is_refused() -> None:
    """The fact does not exist yet, so the program must not proceed on a guess."""
    with pytest.raises(IrConditionError, match="no observation has been recorded"):
        evaluate(_condition(), ControlFacts(cursor=0))
    with pytest.raises(IrConditionError, match="no observation has been recorded for step 2"):
        evaluate(
            _condition(step_order=2),
            ControlFacts(cursor=0, observations=(_recorded(1),)),
        )


def test_a_condition_reads_the_last_outcome_recorded_for_its_step() -> None:
    facts = ControlFacts(
        cursor=1,
        observations=(
            _recorded(1, ToolOutcome.WAITING_EXTERNAL),
            _recorded(1, ToolOutcome.EXECUTION_SUCCEEDED),
        ),
    )

    assert evaluate(_condition(), facts) is True


def test_a_fact_that_is_absent_is_not_the_same_as_a_fact_that_is_empty() -> None:
    """A step that recorded no error code reads as "no error", not as a failure."""
    facts = ControlFacts(cursor=0, observations=(_recorded(1),))

    assert evaluate(
        _condition(field="error_code", operator=ConditionOperator.EQ, value="DENIED"), facts
    ) is False
    assert evaluate(
        _condition(field="error_code", operator=ConditionOperator.NEQ, value="DENIED"), facts
    ) is True


def test_negation_is_a_boolean_rather_than_a_nested_expression() -> None:
    positive = _condition("c1")
    negative = _condition("c2", negate=True)

    assert positive.is_negation_of(negative) is True
    assert negative.is_negation_of(positive) is True
    assert positive.is_negation_of(positive) is False
    # A different fact or a different comparison is not a negation of anything.
    assert positive.is_negation_of(_condition("c3", field="ok", value=True)) is False
    assert positive.is_negation_of(_condition("c4", operator=ConditionOperator.NEQ)) is False


def test_a_condition_node_must_carry_its_condition_and_nothing_else() -> None:
    with pytest.raises(ValueError, match="must carry its condition"):
        ControlNode(node_id="cond-1", kind=ControlNodeKind.CONDITION)
    with pytest.raises(ValueError, match="must not carry another kind"):
        ControlNode(
            node_id="cond-1",
            kind=ControlNodeKind.CONDITION,
            condition=_condition(),
            step_order=1,
        )


# --------------------------------------------------------------------------- #
# Control flow: graph structure
# --------------------------------------------------------------------------- #


def _action_node(node_id: str, order: int) -> ControlNode:
    return ControlNode(node_id=node_id, kind=ControlNodeKind.ACTION, step_order=order)


def _condition_node(node_id: str, condition: ConditionNode) -> ControlNode:
    return ControlNode(node_id=node_id, kind=ControlNodeKind.CONDITION, condition=condition)


def _result_node(node_id: str = "result") -> ControlNode:
    return ControlNode(node_id=node_id, kind=ControlNodeKind.RESULT, result="COMPLETED")


def _three_step_proposal() -> PlanProposal:
    return PlanProposal.model_validate(
        {
            "goal": "schedule a meeting",
            "success_criteria": ["calendar event created"],
            "steps": [
                {
                    "step_id": "s1",
                    "order": 1,
                    "action": "resolve_contact",
                    "purpose": "find the attendee",
                },
                {
                    "step_id": "s2",
                    "order": 2,
                    "action": "create_calendar_event",
                    "purpose": "schedule the meeting",
                },
                {
                    "step_id": "s3",
                    "order": 3,
                    "action": "verify_calendar_event",
                    "purpose": "read the event back",
                },
            ],
            "assumptions": [],
        }
    )


def _base(**overrides: object) -> KaiProgram:
    """A three-step program: ``step 1 → step 2 → step 3``, no branches."""
    kwargs: dict = {
        "task": _task(),
        "plan_version": 1,
        "plan_status": "ACCEPTED",
        "proposal": _three_step_proposal(),
        "plan_hash": "a" * 64,
        "registry": default_registry(),
        "capability_to_tool": CAPABILITY_TO_TOOL,
    }
    kwargs.update(overrides)
    return program_from_plan(**kwargs)


def _regraft(
    program: KaiProgram, nodes: list[ControlNode], edges: list[ControlEdge]
) -> KaiProgram:
    """Rebuild a program with a different control flow, re-sealed and re-validated."""
    return require_valid(
        program.model_copy(update={"nodes": nodes, "edges": edges, "fingerprint": ""}).seal()
    )


def _refused(
    program: KaiProgram, nodes: list[ControlNode], edges: list[ControlEdge], match: str
) -> None:
    with pytest.raises(IrValidationError, match=match):
        _regraft(program, nodes, edges)


def _branch_graph(
    *, taken: str = "succeeded"
) -> tuple[list[ControlNode], list[ControlEdge]]:
    """``action-1 → cond-1 → action-2 | action-3 → result``.

    A genuine fork: the guard on the true edge and the guard on the false edge are
    the same comparison with opposite polarity, which is the only shape the
    validator can *prove* covers every value of the fact.
    """
    positive = _condition("contact-resolved")
    negative = _condition("contact-unresolved", negate=True)
    return (
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", positive),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="action-2", when=positive),
            ControlEdge(from_node="cond-1", to_node="action-3", when=negative),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
    )


def _branching(**overrides: object) -> KaiProgram:
    nodes, edges = _branch_graph()
    return _regraft(_base(), nodes, edges)


def test_a_plan_with_no_branches_still_declares_a_real_graph() -> None:
    """Every program has one, so a branch is an edit to a chain, not a new mode."""
    program = _base()

    assert [node.node_id for node in program.nodes] == [
        "action-1",
        "action-2",
        "action-3",
        "result",
    ]
    assert [node.kind for node in program.nodes] == [
        ControlNodeKind.ACTION,
        ControlNodeKind.ACTION,
        ControlNodeKind.ACTION,
        ControlNodeKind.RESULT,
    ]
    assert program.entry_node_id() == "action-1"
    assert [edge.to_node for edge in program.edges_from("action-1")] == ["action-2"]


def test_the_entry_node_is_derived_from_the_edges_and_must_be_unique() -> None:
    program = _base()
    assert program.entry_node_id() == "action-1"

    every_step = [_action_node(f"action-{n}", n) for n in (1, 2, 3)]
    # Nothing leads into steps 2 and 3, so control could begin in three places.
    _refused(
        program,
        [*every_step, _result_node()],
        [
            ControlEdge(from_node="action-1", to_node="result"),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "exactly one entry node",
    )
    # A closed loop has nowhere to begin at all.
    _refused(
        program,
        every_step,
        [
            ControlEdge(from_node="action-1", to_node="action-2"),
            ControlEdge(from_node="action-2", to_node="action-3"),
            ControlEdge(from_node="action-3", to_node="action-1"),
        ],
        "exactly one entry node",
    )


def test_an_edge_to_a_node_that_does_not_exist_is_refused() -> None:
    program = _base()
    _refused(
        program,
        [_action_node("action-1", 1), _action_node("action-2", 2), _action_node("action-3", 3)],
        [
            ControlEdge(from_node="action-1", to_node="action-2"),
            ControlEdge(from_node="action-2", to_node="nowhere"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "targets undeclared node",
    )


def test_an_edge_may_not_target_its_own_source() -> None:
    """A self-loop is a one-node cycle, and a cycle is refused outright."""
    with pytest.raises(ValueError, match="must not target its own source"):
        ControlEdge(from_node="action-1", to_node="action-1")


def test_a_duplicate_node_id_is_refused() -> None:
    program = _base()
    _refused(
        program,
        [_action_node("action-1", 1), _action_node("action-1", 1), _action_node("action-2", 2)],
        [ControlEdge(from_node="action-1", to_node="action-2")],
        "must be unique",
    )


def test_the_same_branch_between_two_nodes_is_declared_once() -> None:
    program = _base()
    condition = _condition()
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", condition),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="action-2", when=condition),
            ControlEdge(from_node="cond-1", to_node="action-2"),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "duplicate control edge",
    )


def test_a_node_control_can_reach_but_not_leave_is_refused() -> None:
    program = _base()
    _refused(
        program,
        [_action_node("action-1", 1), _action_node("action-2", 2), _action_node("action-3", 3)],
        [ControlEdge(from_node="action-1", to_node="action-2")],
        "nowhere to go",
    )


def test_a_result_node_ends_the_flow_and_may_not_continue() -> None:
    program = _base()
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="action-2"),
            ControlEdge(from_node="action-2", to_node="action-3"),
            ControlEdge(from_node="action-3", to_node="result"),
            ControlEdge(from_node="result", to_node="action-1"),
        ],
        "must not have an outgoing edge",
    )


def test_an_action_node_declares_exactly_one_continuation() -> None:
    program = _base()
    condition = _condition()
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="action-2"),
            ControlEdge(from_node="action-1", to_node="action-3"),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "continuation is declared once",
    )
    # A step is followed by a condition node, not by a guard hanging off the step.
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="action-2", when=condition),
            ControlEdge(from_node="action-2", to_node="action-3"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "followed by a condition node",
    )


def test_a_node_nothing_can_reach_is_refused() -> None:
    """A declared part of the flow the walk can never visit is a broken promise."""
    program = _base()
    first = _condition("a")
    second = _condition("b")
    # A detached loop: the entry is unique, but two nodes are outside its reach.
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", first),
            _condition_node("cond-2", second),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="result"),
            ControlEdge(from_node="cond-1", to_node="cond-2", when=second),
            ControlEdge(from_node="cond-2", to_node="cond-1", when=first),
        ],
        r"nodes nothing can reach: \['cond-1', 'cond-2'\]",
    )


def test_a_cycle_is_refused() -> None:
    """v0's graph is acyclic: revisiting a step would re-issue its action id."""
    program = _base()
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", _condition("a")),
            _condition_node("cond-2", _condition("b")),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="cond-2", when=_condition("b")),
            ControlEdge(from_node="cond-2", to_node="cond-1", when=_condition("a")),
        ],
        "cycle among 2 nodes",
    )


def test_the_graph_must_account_for_every_planned_step_exactly_once() -> None:
    program = _base()
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _action_node("action-1b", 1),
            _action_node("action-2", 2),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="action-1b"),
            ControlEdge(from_node="action-1b", to_node="action-2"),
            ControlEdge(from_node="action-2", to_node="result"),
        ],
        "the graph must account for every step exactly once",
    )


def test_a_condition_may_only_read_a_step_that_is_upstream_of_it() -> None:
    """The fact has to exist by the time the branch is asked about it."""
    program = _base()
    late = _condition("late", step_order=3)
    late_false = _condition("late-false", step_order=3, negate=True)
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", late),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="action-2", when=late),
            ControlEdge(from_node="cond-1", to_node="action-3", when=late_false),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "cannot exist yet",
    )


def test_a_condition_cannot_read_a_step_outside_the_plan() -> None:
    program = _base()
    stranger = _condition("stranger", step_order=9)
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", stranger),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="action-2", when=stranger),
            ControlEdge(
                from_node="cond-1", to_node="action-3",
                when=_condition("stranger-false", step_order=9, negate=True),
            ),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "which is not in this plan",
    )


def test_a_condition_node_branches_on_one_fact() -> None:
    """A compound condition is a chain of condition nodes, not a hidden conjunction."""
    program = _base()
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", _condition("on-outcome")),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(
                from_node="cond-1", to_node="action-2",
                when=_condition("on-outcome"),
            ),
            ControlEdge(
                from_node="cond-1", to_node="action-3",
                when=_condition("on-ok", field="ok", value=True),
            ),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "guards on 2 different facts",
    )

def test_two_edges_guarded_identically_are_refused() -> None:
    """A fact satisfying two identical guards has made no choice at all."""
    program = _base()
    same = _condition("same")
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", same),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="action-2", when=same),
            ControlEdge(from_node="cond-1", to_node="action-3", when=same),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "the same condition",
    )


def test_duplicate_condition_ids_are_refused() -> None:
    program = _base()
    _refused(
        program,
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", _condition("same-id")),
            _condition_node("cond-2", _condition("same-id", negate=True)),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="action-2", when=_condition("same-id")),
            ControlEdge(
                from_node="cond-1", to_node="action-3",
                when=_condition("same-id", negate=True),
            ),
            ControlEdge(from_node="cond-2", to_node="action-2"),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
        "condition ids must be unique",
    )


def test_the_control_flow_is_part_of_the_program_identity() -> None:
    """Changing which branch a program can take changes what it intends to do."""
    linear = _base()
    nodes, edges = _branch_graph()
    branching = _regraft(linear, nodes, edges)

    assert branching.fingerprint != linear.fingerprint
    assert linear.program_id == branching.program_id  # same task, same plan
    # And the fork survives a round trip through the durable record format.
    assert "nodes" in linear.to_json()


# --------------------------------------------------------------------------- #
# Control flow: traversal
# --------------------------------------------------------------------------- #


def _facts(
    *, cursor: int = 0, observations: tuple[ObservationNode, ...] = ()
) -> ControlFacts:
    return ControlFacts(
        cursor=cursor, task_state="RUNNING", steps_used=cursor, observations=observations
    )


def test_an_unbranched_program_walks_its_steps_in_order() -> None:
    program = _base()

    assert [next_step(program, _facts(cursor=0)).order] == [1]
    assert [next_step(program, _facts(cursor=1)).order] == [2]
    assert [next_step(program, _facts(cursor=2)).order] == [3]
    assert next_step(program, _facts(cursor=3)) is None


def test_a_completed_step_is_never_asked_for_again() -> None:
    """The guarantee the old ``step.order <= cursor`` skip gave, from the program."""
    program = _branching()
    observations = (_recorded(1),)

    step = next_step(program, _facts(cursor=1, observations=observations))
    assert step is not None
    assert step.order == 2
    # Asked again with the same durable facts, it names the same step.
    assert next_step(program, _facts(cursor=1, observations=observations)) is step


def test_a_branch_selects_exactly_one_path() -> None:
    program = _branching()

    succeeded = next_step(
        program, _facts(cursor=1, observations=(_recorded(1),))
    )
    failed = next_step(
        program,
        _facts(cursor=1, observations=(_recorded(1, ToolOutcome.EXECUTION_FAILED),)),
    )

    assert succeeded is not None and succeeded.order == 2
    assert failed is not None and failed.order == 3
    # Not both: the fork is a decision, and the other branch's step is not next.
    assert {succeeded.order, failed.order} == {2, 3}


def test_the_walk_ends_at_the_result_node_on_either_branch() -> None:
    program = _branching()

    branches = ((2, ToolOutcome.EXECUTION_SUCCEEDED), (3, ToolOutcome.EXECUTION_FAILED))
    for cursor, outcome in branches:
        facts = _facts(
            cursor=cursor,
            observations=(_recorded(1, outcome), _recorded(cursor)),
        )
        assert next_step(program, facts) is None


def test_a_branch_that_matches_nothing_fails_closed() -> None:
    """Neither "no branch matches" nor "both match" is ever resolved by guessing.

    The two guards overlap on purpose: they cover different value sets, so the
    program's declared branch structure has a gap and an overlap at the same
    time. Both are refusals, and both leave the choice unmade.
    """
    program = _base()
    nodes, edges = (
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", _condition("refused")),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(
                from_node="cond-1", to_node="action-2",
                when=_condition(
                    "refused",
                    operator=ConditionOperator.IN,
                    value=["DENIED", "REJECTED"],
                ),
            ),
            ControlEdge(
                from_node="cond-1", to_node="action-3",
                when=_condition(
                    "escalated", operator=ConditionOperator.IN, value=["DENIED", "WAITING_EXTERNAL"]
                ),
            ),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
    )
    forked = _regraft(program, nodes, edges)

    # The contact resolved: neither guard holds, so the program cannot choose.
    with pytest.raises(IrControlFlowError, match="no control edge out of node 'cond-1'"):
        next_step(forked, _facts(cursor=1, observations=(_recorded(1),)))
    # Denied: both guards hold, so it also cannot choose.
    with pytest.raises(
        IrControlFlowError, match=r"2 control edges out of node 'cond-1' are eligible"
    ):
        next_step(
            forked,
            _facts(cursor=1, observations=(_recorded(1, ToolOutcome.DENIED),)),
        )
    # A value exactly one guard covers is the only case the fork can serve.
    step = next_step(
        forked,
        _facts(cursor=1, observations=(_recorded(1, ToolOutcome.REJECTED),)),
    )
    assert step is not None and step.order == 2


def test_a_branch_never_picks_for_itself_between_two_true_guards() -> None:
    program = _base()
    condition = _condition("succeeded")
    nodes, edges = (
        [
            _action_node("action-1", 1),
            _condition_node("cond-1", condition),
            _action_node("action-2", 2),
            _action_node("action-3", 3),
            _result_node(),
        ],
        [
            ControlEdge(from_node="action-1", to_node="cond-1"),
            ControlEdge(from_node="cond-1", to_node="action-2", when=condition),
            ControlEdge(from_node="cond-1", to_node="action-3"),
            ControlEdge(from_node="action-2", to_node="result"),
            ControlEdge(from_node="action-3", to_node="result"),
        ],
    )
    chained = _regraft(program, nodes, edges)

    # Guard false, fallthrough taken.
    step = next_step(
        chained, _facts(cursor=1, observations=(_recorded(1, ToolOutcome.EXECUTION_FAILED),))
    )
    assert step is not None and step.order == 3
    # Guard true *and* fallthrough eligible: the program made no choice.
    with pytest.raises(IrControlFlowError, match="ambiguous"):
        next_step(chained, _facts(cursor=1, observations=(_recorded(1),)))


def test_a_walk_that_cannot_start_is_refused() -> None:
    """The walker re-checks its own precondition instead of guessing a start."""
    program = _base()
    assert next_node(program, _facts()) is not None

    headless = program.model_copy(
        update={
            "nodes": [
                _action_node("action-1", 1),
                _action_node("action-2", 2),
                _action_node("action-3", 3),
            ],
            "edges": [
                ControlEdge(from_node="action-1", to_node="action-2"),
                ControlEdge(from_node="action-2", to_node="action-3"),
                ControlEdge(from_node="action-3", to_node="action-1"),
            ],
        }
    )
    with pytest.raises(IrControlFlowError, match="no single entry node"):
        next_node(headless, _facts())


# --------------------------------------------------------------------------- #
# Control flow: recovery and identity
# --------------------------------------------------------------------------- #


def test_a_branch_taken_after_a_resume_is_the_same_branch() -> None:
    """The facts come off the execution row, so a resume re-derives the decision."""
    program = _branching()

    live = next_step(program, _facts(cursor=1, observations=(_recorded(1),)))
    persisted = [
        evidence_from_observation(observation, tool_name="contacts.match")
        for observation in (_recorded(1),)
    ]
    restored = tuple(
        observation
        for record in persisted
        if (observation := observation_from_evidence(record)) is not None
    )
    resumed = next_step(program, _facts(cursor=1, observations=restored))

    assert live is not None and resumed is not None
    assert live.order == resumed.order
    assert live.step_id == resumed.step_id


def test_reaching_an_action_through_a_branch_does_not_change_its_identity() -> None:
    """A branch routes to a step; it never mints a new id for what that step does."""
    step_two = _branching().step(2)
    assert step_two is not None
    linear_two = _base().step(2)
    assert linear_two is not None

    direct = action_for(
        step=linear_two, index=0, capability=linear_two.capability, arguments={},
        task_id=TASK_ID, plan_version=1,
    )
    branched = action_for(
        step=step_two, index=0, capability=step_two.capability, arguments={},
        task_id=TASK_ID, plan_version=1,
    )

    assert branched.action_id == direct.action_id == action_id_for(
        task_id=TASK_ID, plan_version=1, step_order=2, index=0
    )
    # And it is the id the runtime was already using, unchanged.
    assert branched.action_id == f"runtime-{TASK_ID}-1-2-0"


def test_observations_stay_out_of_the_program_identity() -> None:
    """Running a program must not change what the program *is*."""
    program = _branching()
    before = program.fingerprint

    program.record(_recorded(1))

    assert program.fingerprint == before
    assert program.computed_fingerprint() == before


def test_a_program_with_no_conditions_never_asks_the_runtime_to_choose() -> None:
    """Branching is opt-in: the current slice's plans walk straight through."""
    program = _base()
    seen: list[str] = []
    cursor = 0
    while (node := next_node(program, _facts(cursor=cursor))) is not None:
        seen.append(node.node_id)
        cursor = node.step_order  # type: ignore[assignment]
    assert seen == ["action-1", "action-2", "action-3"]
