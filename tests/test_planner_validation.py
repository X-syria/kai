"""Deterministic planner validation — pure unit tests, no database.

These exercise the two gates a raw model output must pass before it can ever
become a persisted plan: Pydantic shape validation (including
``extra="forbid"``, which rejects any permission/tool/risk field the model
tries to smuggle into the structure) and :func:`~kai.planner.schemas.domain_errors`
contract-fidelity checks. Nothing here talks to the network.
"""

from __future__ import annotations

import re

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
        "fulfillment_profile": "calendar_meeting",
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


def test_a_plan_cannot_name_its_own_fulfillment_profile() -> None:
    """The evaluator is bound by the sealed contract, not chosen by the model.

    ``fulfillment_profile`` is the field that says which evaluator answers a
    task's ``success_criteria``. A plan that could set it would be choosing the
    standard it is later judged against, so the field is absent from the
    proposal schema and ``extra="forbid"`` refuses it.
    """
    with pytest.raises(ValidationError) as exc_info:
        PlanProposal.model_validate(_plan(fulfillment_profile="calendar_meeting"))
    assert "fulfillment_profile" in str(exc_info.value)


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


def test_inputs_may_be_an_array_of_strings() -> None:
    """The declared representation: ``inputs`` is a JSON array of strings."""
    step = _plan()["steps"][0] | {"inputs": ["calendar_id", "window"]}
    proposal = PlanProposal.model_validate(_plan(steps=[step]))
    assert proposal.steps[0].inputs == ["calendar_id", "window"]


def test_inputs_may_be_empty() -> None:
    """An empty array is valid: the step declares no inputs."""
    step = _plan()["steps"][0] | {"inputs": []}
    proposal = PlanProposal.model_validate(_plan(steps=[step]))
    assert proposal.steps[0].inputs == []


def test_inputs_scalar_string_rejected() -> None:
    """A scalar string is not the declared list type (the Phase 2D.4 mismatch)."""
    step = _plan()["steps"][0] | {"inputs": "calendar_id"}
    with pytest.raises(ValidationError) as exc_info:
        PlanProposal.model_validate(_plan(steps=[step]))
    errors = exc_info.value.errors()
    assert any(
        error["loc"] == ("steps", 0, "inputs") and error["type"] == "list_type"
        for error in errors
    ), errors


def test_inputs_non_array_object_rejected() -> None:
    """Any non-array value is rejected, never coerced into a one-item list."""
    step = _plan()["steps"][0] | {"inputs": {"calendar_id": True}}
    with pytest.raises(ValidationError) as exc_info:
        PlanProposal.model_validate(_plan(steps=[step]))
    errors = exc_info.value.errors()
    assert any(
        error["loc"] == ("steps", 0, "inputs") and error["type"] == "list_type"
        for error in errors
    ), errors


def test_prompt_documents_inputs_contract() -> None:
    """The prompt states the ``inputs`` type the schema actually enforces.

    A regression guard for prompt documentation, not proof that a model will
    comply; the executable guarantee stays the schema validation above.
    """
    from kai.planner.model import PLANNER_SYSTEM_PROMPT

    assert "a JSON array of strings" in PLANNER_SYSTEM_PROMPT
    assert "empty array []" in PLANNER_SYSTEM_PROMPT


def _prompt_capability_vocabulary() -> set[str]:
    """The capability ids the prompt enumerates, parsed from its own text."""
    from kai.planner.model import PLANNER_SYSTEM_PROMPT

    match = re.search(
        r"capability ids \(and nothing else\):\s*\[([^\]]+)\]",
        PLANNER_SYSTEM_PROMPT,
    )
    assert match is not None, "prompt does not enumerate a capability vocabulary"
    return {token.strip() for token in match.group(1).split(",") if token.strip()}


def test_prompt_enumerates_the_canonical_capability_vocabulary() -> None:
    """The prompt lists exactly the capabilities the domain validator allows.

    Detects drift in both directions: a capability added to
    ``ABSTRACT_CAPABILITIES`` without updating the prompt, or a stale id kept
    in the prompt after the canonical set changes.
    """
    from kai.planner.service import ABSTRACT_CAPABILITIES

    assert _prompt_capability_vocabulary() == set(ABSTRACT_CAPABILITIES)


def test_prompt_forbids_invented_capabilities() -> None:
    """The prompt tells the model not to invent capability names."""
    from kai.planner.model import PLANNER_SYSTEM_PROMPT

    assert "Never invent synonyms" in PLANNER_SYSTEM_PROMPT


def test_prompt_requires_exact_criteria_copy() -> None:
    """The prompt requires the task's criteria be copied, not rewritten.

    Prompt-text only: it does not assert a model will always comply; the
    executable guarantee stays ``domain_errors``.
    """
    from kai.planner.model import PLANNER_SYSTEM_PROMPT

    assert "success_criteria MUST be copied exactly" in PLANNER_SYSTEM_PROMPT
    for phrase in ("paraphrase", "same order", "unresolved_questions"):
        assert phrase in PLANNER_SYSTEM_PROMPT


def test_task_datablocks_supply_the_authoritative_criteria() -> None:
    """The task's own criteria reach the model verbatim (never re-derived)."""
    from kai.planner.service import _task_datablocks

    rendered = _task_datablocks(_task())
    for criterion in CRITERIA:
        assert criterion in rendered


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


# --- branches --------------------------------------------------------------

def _branch(**overrides: object) -> dict:
    """A declared fork after step 1, on the outcome step 1 recorded."""
    declaration: dict = {
        "after_step": 1,
        "condition": {
            "kind": "observation",
            "step_order": 1,
            "field": "outcome",
            "operator": "eq",
            "value": "EXECUTION_SUCCEEDED",
        },
        "then_step": 2,
        "else_step": 3,
    }
    declaration.update(overrides)
    return declaration


def _errors(branches: list[dict], **overrides: object) -> list[str]:
    proposal = PlanProposal.model_validate(_plan(branches=branches, **overrides))
    return domain_errors(
        proposal, task=_task(), allowed_capabilities=CAPABILITIES
    )


def test_a_plan_may_declare_a_branch_and_a_plan_may_not_declare_one() -> None:
    assert _errors([]) == []
    assert _errors([_branch()]) == []


def test_branches_are_absent_from_the_plan_when_the_model_declares_none() -> None:
    """An absent key and an empty list are the same plan, not two of them."""
    without = PlanProposal.model_validate(_plan())
    stated = _plan(branches=[])
    stated["branches"] = []
    with_empty = PlanProposal.model_validate(stated)

    assert without.branches == with_empty.branches == []
    assert plan_json(without) == plan_json(with_empty)


def test_a_plan_with_a_branch_still_roundtrips_through_the_stored_contract() -> None:
    proposal = PlanProposal.model_validate(_plan(branches=[_branch()]))

    reloaded = PlanProposal.model_validate(plan_json(proposal))

    assert reloaded == proposal
    assert plan_json(reloaded)["branches"] == [
        {
            "after_step": 1,
            "condition": {
                "kind": "observation",
                "step_order": 1,
                "field": "outcome",
                "operator": "eq",
                "value": "EXECUTION_SUCCEEDED",
                "negate": False,
            },
            "then_step": 2,
            "else_step": 3,
        }
    ]


@pytest.mark.parametrize(
    "branches, refused",
    [
        pytest.param(
            [_branch(then_step=3, else_step=3)],
            "then_step and else_step are the same step",
            id="both-outcomes-lead-to-one-step",
        ),
        pytest.param(
            [_branch(then_step=2, else_step=9)],
            "else_step 9 is not a step in this plan",
            id="an-outcome-names-a-step-that-does-not-exist",
        ),
        pytest.param(
            [_branch(then_step=3, else_step=4)],
            "must be step 2",
            id="neither-outcome-continues-the-chain",
        ),
        pytest.param(
            [_branch(), _branch(then_step=2, else_step=4)],
            "a step carries only one branch",
            id="one-step-cannot-carry-two-forks",
        ),
    ],
)
def test_a_declaration_that_is_not_a_graph_is_refused_at_the_planner_gate(
    branches: list[dict], refused: str
) -> None:
    """Refused when the plan is offered, not discovered while a task is running."""
    errors = _errors(branches)

    assert any(refused in error for error in errors), errors


def test_a_condition_the_ir_cannot_represent_is_refused_at_the_planner_gate() -> None:
    errors = _errors(
        [
            _branch(
                condition={
                    "kind": "observation",
                    "step_order": 1,
                    "field": "outcome",
                    "operator": "eq",
                    "value": True,
                }
            )
        ]
    )

    assert any("operand must be str" in error for error in errors), errors
    assert all(error.startswith("branch after step 1") for error in errors), errors


def test_a_branch_cannot_carry_a_permission_or_a_loop() -> None:
    """The plan still has no way to express permission, risk, or repetition."""
    for smuggled in (
        {"loop": True},
        {"goto": 3},
        {"requires_approval": True},
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PlanProposal.model_validate(_plan(branches=[_branch(**smuggled)]))


def test_branches_are_bounded() -> None:
    """A branch per step at most, so a plan cannot smuggle a graph by volume."""
    with pytest.raises(ValidationError):
        PlanProposal.model_validate(
            _plan(branches=[_branch(after_step=1) for _ in range(51)])
        )