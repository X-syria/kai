"""Deterministic planner validation — pure unit tests, no database.

These exercise the two gates a raw model output must pass before it can ever
become a persisted plan: Pydantic shape validation (including
``extra="forbid"``, which rejects any permission/tool/risk field the model
tries to smuggle into the structure) and :func:`~kai.planner.schemas.domain_errors`
contract-fidelity checks. Nothing here talks to the network.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kai.planner.schemas import PlanProposal, domain_errors, plan_json
from kai.tasks.models import Task

GOAL = "Schedule a team meeting"
CRITERIA = ["invitees confirmed", "slot chosen", "calendar event created"]


def _task(**overrides: object) -> Task:
    params: dict[str, object] = {
        "goal": GOAL,
        "success_criteria": list(CRITERIA),
        "max_steps": 5,
    }
    params.update(overrides)
    return Task(**params)  # type: ignore[call-arg]


def _plan(**overrides: object) -> dict:
    data: dict[str, object] = {
        "goal": GOAL,
        "success_criteria": list(CRITERIA),
        "steps": [
            {
                "step_id": "s1",
                "order": 1,
                "action": "read_calendar",
                "purpose": "find availability",
            },
            {
                "step_id": "s2",
                "order": 2,
                "action": "propose_times",
                "purpose": "offer candidate slots",
            },
            {
                "step_id": "s3",
                "order": 3,
                "action": "create_calendar_event",
                "purpose": "book the slot",
            },
        ],
        "assumptions": ["all invitees use the same calendar provider"],
        "unresolved_questions": ["what is the meeting duration?"],
    }
    data.update(overrides)
    return data


CAPABILITIES = {
    "resolve_contact",
    "read_calendar",
    "propose_times",
    "send_message",
    "create_calendar_event",
    "verify_calendar_event",
}


# --- shape validation ------------------------------------------------------

def test_valid_plan_passes_shape_and_contract() -> None:
    proposal = PlanProposal.model_validate(_plan())
    task = _task()
    assert domain_errors(proposal, task=task, allowed_capabilities=CAPABILITIES) == []


def test_missing_goal_rejected() -> None:
    data = _plan()
    del data["goal"]
    with pytest.raises(ValidationError):
        PlanProposal.model_validate(data)


def test_empty_success_criteria_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanProposal.model_validate(_plan(success_criteria=[]))


def test_extra_top_level_fields_rejected() -> None:
    # The structural guarantee: permission/tool/risk/model fields are not part
    # of the schema, and ``extra="forbid"`` rejects them outright.
    with pytest.raises(ValidationError) as exc_info:
        PlanProposal.model_validate(_plan(risk_policy="CRITICAL"))
    assert "risk_policy" in str(exc_info.value)

    with pytest.raises(ValidationError):
        PlanProposal.model_validate(
            _plan(permissions=["delete-emails"], allowed_tools=["admin"])
        )


def test_step_without_action_rejected() -> None:
    step = _plan()["steps"][0].copy()
    del step["action"]
    with pytest.raises(ValidationError):
        PlanProposal.model_validate(_plan(steps=[step]))


def test_step_without_purpose_rejected() -> None:
    step = _plan()["steps"][0].copy()
    del step["purpose"]
    with pytest.raises(ValidationError):
        PlanProposal.model_validate(_plan(steps=[step]))


# --- contract fidelity -----------------------------------------------------

def test_goal_rewrite_rejected() -> None:
    proposal = PlanProposal.model_validate(_plan(goal="Book a flight to Berlin"))
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("goal" in e for e in errors)


def test_criteria_invented_rejected() -> None:
    proposal = PlanProposal.model_validate(
        _plan(success_criteria=[*CRITERIA, "refund issued"])
    )
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("success criteria" in e for e in errors)


def test_criteria_dropped_rejected() -> None:
    proposal = PlanProposal.model_validate(_plan(success_criteria=CRITERIA[:1]))
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("success criteria" in e for e in errors)


def test_criteria_paraphrase_rejected() -> None:
    proposal = PlanProposal.model_validate(
        _plan(success_criteria=["invitees CONFIRMED", "slot chosen"])
    )
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("success criteria" in e for e in errors)


# --- steps: budget / ids / order / vocabulary ------------------------------

def test_duplicate_step_ids_rejected() -> None:
    steps = _plan()["steps"]
    steps[1] = dict(steps[1], step_id=steps[0]["step_id"])
    proposal = PlanProposal.model_validate(_plan(steps=steps))
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("duplicate step_id" in e for e in errors)


def test_non_sequential_orders_rejected() -> None:
    steps = _plan()["steps"]
    steps[1] = dict(steps[1], order=9)
    proposal = PlanProposal.model_validate(_plan(steps=steps))
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("sequential" in e for e in errors)


def test_out_of_order_positions_normalised_by_declared_order() -> None:
    # Steps delivered in positions [2,1,3] but with consistent order fields get
    # canonicalised to order 1..n and pass. The model's declared order wins.
    base = _plan()["steps"]
    by_id = {s["step_id"]: s for s in base}
    shuffled = [dict(by_id["s2"]), dict(by_id["s1"]), dict(by_id["s3"])]
    proposal = PlanProposal.model_validate(_plan(steps=shuffled))
    assert domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES) == []


def test_order_gap_rejected() -> None:
    # A single step whose order starts at 3 can never form a 1..n sequence.
    single = dict(_plan()["steps"][0], order=3)
    proposal = PlanProposal.model_validate(_plan(steps=[single]))
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("sequential" in e for e in errors)


def test_duplicate_orders_rejected() -> None:
    steps = _plan()["steps"]
    steps[1] = dict(steps[1], order=steps[0]["order"])
    proposal = PlanProposal.model_validate(_plan(steps=steps))
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("sequential" in e for e in errors)


def test_over_budget_rejected() -> None:
    template = _plan()["steps"][0]
    five = [
        dict(template, step_id=f"s{i}", order=i) for i in range(1, 6)
    ]
    proposal = PlanProposal.model_validate(_plan(steps=five))
    task = _task(max_steps=4)
    errors = domain_errors(proposal, task=task, allowed_capabilities=CAPABILITIES)
    assert any("max_steps" in e for e in errors)


def test_capability_outside_vocabulary_rejected() -> None:
    step = _plan()["steps"][0].copy()
    step["action"] = "delete_all_emails"
    proposal = PlanProposal.model_validate(_plan(steps=[step]))
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("capabilit" in e for e in errors)


def test_unresolved_question_not_silently_assumed() -> None:
    # An item may live in assumptions OR unresolved_questions, never both.
    proposal = PlanProposal.model_validate(
        _plan(
            assumptions=["what is the meeting duration?"],
            unresolved_questions=["what is the meeting duration?"],
        )
    )
    errors = domain_errors(proposal, task=_task(), allowed_capabilities=CAPABILITIES)
    assert any("duplicated" in e for e in errors)


# --- serialization ---------------------------------------------------------

def test_plan_json_roundtrips() -> None:
    proposal = PlanProposal.model_validate(_plan())
    reloaded = PlanProposal.model_validate(plan_json(proposal))
    assert reloaded == proposal


def test_plan_json_never_includes_contract_overrides() -> None:
    proposal = PlanProposal.model_validate(_plan())
    serialized = plan_json(proposal)
    assert "permissions" not in serialized
    assert "risk_policy" not in serialized
    assert "allowed_tools" not in serialized
    assert "model" not in serialized
    assert "execution" not in serialized