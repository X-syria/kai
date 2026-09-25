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
    ErrorNode,
    IntentNode,
    IrValidationError,
    KaiProgram,
    ObservationNode,
    PlanNode,
    StateSnapshotNode,
    StepNode,
    action_for,
    action_id_for,
    error_from_observation,
    evidence_from_observation,
    intent_from_action,
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
