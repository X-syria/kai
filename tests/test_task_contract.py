"""Pydantic + domain validation for the Task Contract.

The schema-level tests are database-free. The two live-sealing license tests at
the end exercise the real ``create_task`` boundary (the single write to
``Task.fulfillment_profile``) against Postgres, so they need the same shared
database the rest of the runtime suite uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from kai.tasks.schemas import StateTransitionRequest, TaskContractInput
from kai.tasks.service import (
    UnlicensedFulfillmentError,
    count_tasks,
    create_task,
)
from tool_harness import make_tenant_user, session_scope

VALID = {
    "goal": "Book a flight to Berlin",
    "success_criteria": ["booking reference exists"],
    "fulfillment_profile": "calendar_meeting",
    "allowed_tools": ["calendar", "gmail"],
    "risk_policy": "LOW",
    "max_cost": 500.0,
    "max_steps": 20,
    "verification_method": "automatic",
    "fallback": "ask the user",
}


def _contract(**overrides: object) -> TaskContractInput:
    data = {**VALID, **overrides}
    return TaskContractInput.model_validate(data)


def test_valid_contract() -> None:
    contract = _contract()
    assert contract.goal == "Book a flight to Berlin"


def test_goal_is_required() -> None:
    with pytest.raises(ValidationError):
        TaskContractInput.model_validate({k: v for k, v in VALID.items() if k != "goal"})


def test_goal_cannot_be_blank() -> None:
    with pytest.raises(ValidationError):
        _contract(goal="   ")


def test_fallback_is_required_and_not_blank() -> None:
    with pytest.raises(ValidationError):
        _contract(fallback="   ")
    with pytest.raises(ValidationError):
        TaskContractInput.model_validate(
            {k: v for k, v in VALID.items() if k != "fallback"}
        )


def test_max_steps_bounds() -> None:
    with pytest.raises(ValidationError):
        _contract(max_steps=0)
    with pytest.raises(ValidationError):
        _contract(max_steps=-10)
    with pytest.raises(ValidationError):
        _contract(max_steps=1001)
    assert _contract(max_steps=1000).max_steps == 1000


def test_max_cost_bounds() -> None:
    with pytest.raises(ValidationError):
        _contract(max_cost=-1)
    assert _contract(max_cost=0).max_cost == 0


def test_contract_structure_deadline_must_be_timezone_aware() -> None:
    naive = datetime(2026, 6, 24, 10, 0)
    with pytest.raises(ValidationError):
        _contract(deadline=naive)
    aware = datetime(2026, 6, 24, 10, 0, tzinfo=UTC)
    assert _contract(deadline=aware).deadline == aware


def test_contract_structure_success_criteria_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        _contract(success_criteria=[])
    with pytest.raises(ValidationError):
        _contract(success_criteria=[""])
    with pytest.raises(ValidationError):
        _contract(success_criteria=["  "])


def test_high_risk_tasks_capped_max_steps() -> None:
    assert _contract(risk_policy="HIGH", max_steps=100).max_steps == 100
    with pytest.raises(ValidationError):
        _contract(risk_policy="HIGH", max_steps=101)
    with pytest.raises(ValidationError):
        _contract(risk_policy="CRITICAL", max_steps=500)
    assert _contract(risk_policy="LOW", max_steps=1000).max_steps == 1000


def test_unknown_risk_policy_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _contract(risk_policy="WHATEVER")


def test_unknown_verification_method_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _contract(verification_method="telepathy")


def test_allowed_tools_are_sanitised() -> None:
    contract = _contract(allowed_tools=[" calendar ", "", "  gmail  "])
    assert contract.allowed_tools == ["calendar", "gmail"]


def test_success_criteria_entry_length_cap() -> None:
    with pytest.raises(ValidationError):
        _contract(success_criteria=["x" * (2000 + 1)])


def test_tool_count_cap() -> None:
    with pytest.raises(ValidationError):
        _contract(allowed_tools=[f"tool-{i}" for i in range(51)])


def test_goal_length_cap() -> None:
    with pytest.raises(ValidationError):
        _contract(goal="x" * (1000 + 1))


def test_state_transition_request_rejects_unknown_state() -> None:
    with pytest.raises(ValidationError):
        StateTransitionRequest(target_state="NOT_A_STATE")
    assert StateTransitionRequest(target_state="EXECUTING").target_state == "EXECUTING"


def test_deadline_is_a_future_boundary_not_required() -> None:
    past = datetime.now(UTC) - timedelta(days=30)
    # A past deadline is structurally fine; the scheduler enforces it later.
    assert _contract(deadline=past).deadline == past


# --- live-sealing license gate (create_task is the single seal boundary) -----


async def test_calendar_profile_is_accepted_by_the_sealing_gate() -> None:
    """``calendar_meeting`` still seals through the real ``create_task`` path.

    The reference profile carries an evaluator-decided sealed-intent anchor (the
    enforced window), so it must remain licensable. This exercises the actual
    persistence boundary, not the constant.
    """
    tenant, user = await make_tenant_user("owner@corp.example")
    async with session_scope() as session:
        task = await create_task(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            contract=_contract(),
        )
        assert task.fulfillment_profile == "calendar_meeting"


async def test_gmail_profile_is_rejected_by_the_sealing_gate() -> None:
    """``gmail_send`` cannot be sealed: no evaluator-decided user-intent source.

    Every ``gmail_send`` criterion value is derived from execution-time
    resolution, so the profile is refused at the license boundary — before any
    ``Task`` row is persisted — and the error names the rejected profile.
    """
    tenant, user = await make_tenant_user("owner@corp.example")
    async with session_scope() as session:
        with pytest.raises(UnlicensedFulfillmentError) as excinfo:
            await create_task(
                session,
                tenant_id=tenant.id,
                user_id=user.id,
                contract=_contract(fulfillment_profile="gmail_send"),
            )
        assert "gmail_send" in str(excinfo.value)
        # The gate fires before persistence: nothing was sealed into the tenant.
        assert await count_tasks(session, tenant_id=tenant.id) == 0