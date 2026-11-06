"""The local ``task_preparation`` slice: a governed first task without Google.

This is the Stage 2E experiment's regression surface. It proves, against the
real service layers and the real database, that a human-style preparation request

    ``GoalSubmission(intent="prepare_task")``
      -> a sealed contract allowing exactly ``task.prepare``
      -> a persisted plan whose only step is ``prepare_checklist``
      -> one local, side-effect-free, verified tool run
      -> a truthful ``COMPLETED``

traverses the *same* governed lifecycle every other profile uses, while naming
no Google account, no calendar, and no guest. It also pins the fail-closed edges:
a preparation with no qualifying evidence is never ``FULFILLED``, and a plan for
this profile cannot acquire a capability the sealed contract does not name.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from kai.db.engine import async_session_factory
from kai.runtime.fulfillment import (
    FulfillmentInput,
    FulfillmentVerdict,
    SuccessCriteria,
    evaluate_task_preparation,
    task_preparation_criteria,
)
from kai.runtime.models import RuntimeExecution
from kai.runtime.schemas import GoalSubmission, UnderstandingOutput
from kai.runtime.service import AgentRuntime
from kai.runtime.translator import build_preparation_contract
from kai.tools.adapters.preparation import PreparationAdapter
from kai.tools.catalog import default_registry
from kai.tools.context import ToolContext
from tool_harness import make_gateway, make_tenant_user

GOAL = "prepare a checklist for the offsite"


def _plan(goal: str = GOAL) -> dict[str, Any]:
    """A valid plan for the preparation contract: one local step, nothing else."""
    return {
        "goal": goal,
        "success_criteria": ["a plan was prepared", "information gaps identified"],
        "steps": [
            {
                "step_id": "p1",
                "order": 1,
                "action": "prepare_checklist",
                "purpose": "produce a local checklist for the task",
                "inputs": ["goal"],
                "expected_output": "a checklist and its gaps",
                "verification_hint": "the checklist is non-empty",
            }
        ],
        "assumptions": [],
        "unresolved_questions": [],
    }


class _StubModel:
    """The one model call this profile makes is the planner's, and nothing else."""

    model_id = "stub-preparation-v0"

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[list[dict[str, Any]]] = []

    async def generate_plan(self, *, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(messages)
        return self.response


def test_build_preparation_contract_is_sealed_and_minimal() -> None:
    contract = build_preparation_contract(GOAL)
    assert contract.goal == GOAL
    assert contract.allowed_tools == ["task.prepare"]
    assert contract.fulfillment_profile == "task_preparation"
    assert contract.risk_policy == "LOW"
    assert contract.max_steps == 2
    assert contract.success_criteria == [
        "a plan was prepared",
        "information gaps identified",
    ]


async def test_the_adapter_is_local_side_effect_free_and_deterministic() -> None:
    adapter = PreparationAdapter()
    async with async_session_factory() as session:
        context = ToolContext(
            session=session,
            task_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            account_id=None,
            transport=None,
            metadata={"content_category": "prepare_task"},
        )
        tool = default_registry().get("task.prepare")
        outcome = await adapter.execute(
            tool=tool, arguments={"objective": GOAL}, context=context
        )
        again = await adapter.execute(
            tool=tool, arguments={"objective": GOAL}, context=context
        )
    assert outcome.ok and outcome.verified
    assert outcome.data == again.data
    assert outcome.evidence["step_count"] >= 1
    assert outcome.evidence["objective"] == GOAL
    assert adapter.calls == ["task.prepare", "task.prepare"]


async def test_prepare_task_completes_without_a_google_account() -> None:
    gateway, _ = make_gateway()
    model = _StubModel(_plan())
    runtime = AgentRuntime(gateway=gateway, model=model)

    async with async_session_factory() as session:
        tenant, user = await make_tenant_user("planner@corp.example")
        task = await runtime.submit(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            submission=GoalSubmission(goal=GOAL, timezone="UTC", intent="prepare_task"),
        )
        task_id = task.id
        outcome = await runtime.trigger(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task_id,
            transport=None,
        )

        assert outcome.task_state == "COMPLETED", outcome.message
        assert outcome.outcome == "COMPLETED"
        assert len(model.calls) == 1, "planning is the only model call"

        row = (
            await session.execute(
                select(RuntimeExecution).where(RuntimeExecution.task_id == task_id)
            )
        ).scalar_one()
        # No understanding was sealed for a local preparation, and no guest was
        # resolved; the run is decided from the goal and the local evidence alone.
        assert row.scenario is None
        assert row.identities == []
        entry = row.evidence[-1]
        assert entry["tool"] == "task.prepare"
        assert entry["ok"] is True and entry["verified"] is True
        assert entry["args"] == {"objective": GOAL}
        assert row.fulfillment_profile == "task_preparation"
        assert row.fulfillment_verdict == "FULFILLED"


def test_preparation_is_not_fulfilled_without_qualifying_evidence() -> None:
    criteria = task_preparation_criteria(profile="task_preparation", objective=GOAL)
    assert isinstance(criteria, SuccessCriteria)

    def verdict(records: list[Any]) -> FulfillmentVerdict:
        return evaluate_task_preparation(
            FulfillmentInput(criteria=criteria, evidence=tuple(records))
        )

    good = {
        "tool": "task.prepare",
        "outcome": "EXECUTION_SUCCEEDED",
        "ok": True,
        "verified": True,
        "args": {"objective": GOAL},
        "evidence": {"step_count": 3},
    }
    assert verdict([good]) is FulfillmentVerdict.FULFILLED
    # A different objective is a different intent, even with identical evidence.
    assert (
        verdict([{**good, "args": {"objective": "prepare a budget"}}])
        is FulfillmentVerdict.NOT_FULFILLED
    )
    # Unverified, failed, malformed, and unrelated records never satisfy it.
    assert verdict([{**good, "verified": False}]) is FulfillmentVerdict.NOT_FULFILLED
    assert verdict([{**good, "ok": False}]) is FulfillmentVerdict.NOT_FULFILLED
    assert (
        verdict([{**good, "outcome": "EXECUTION_FAILED"}])
        is FulfillmentVerdict.NOT_FULFILLED
    )
    assert (
        verdict([{**good, "evidence": {"step_count": 0}}])
        is FulfillmentVerdict.NOT_FULFILLED
    )
    assert (
        verdict([{**good, "tool": "calendar.create_event"}])
        is FulfillmentVerdict.NOT_FULFILLED
    )
    assert verdict([]) is FulfillmentVerdict.NOT_FULFILLED


async def test_a_prepare_task_cannot_acquire_another_capability() -> None:
    gateway, _ = make_gateway()
    model = _StubModel(
        {
            **_plan(),
            "steps": [
                {
                    "step_id": "x1",
                    "order": 1,
                    "action": "read_calendar",
                    "purpose": "peek at the calendar",
                    "inputs": ["calendar_id"],
                    "expected_output": "free slots",
                    "verification_hint": "non-empty",
                }
            ],
        }
    )
    runtime = AgentRuntime(gateway=gateway, model=model)

    async with async_session_factory() as session:
        tenant, user = await make_tenant_user("planner2@corp.example")
        task = await runtime.submit(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            submission=GoalSubmission(goal=GOAL, timezone="UTC", intent="prepare_task"),
        )
        outcome = await runtime.trigger(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
            transport=None,
        )
    # The sealed contract allows exactly ``task.prepare``; a plan that reaches
    # for another capability is refused, and the task fails rather than running.
    assert outcome.task_state == "FAILED"


async def test_a_google_profile_still_fails_closed_without_an_account() -> None:
    """The local profile's provider-free path widens nothing: Google still refuses."""
    gateway, _ = make_gateway()
    model = _StubModel(_plan())
    runtime = AgentRuntime(gateway=gateway, model=model)

    async with async_session_factory() as session:
        tenant, user = await make_tenant_user("no-account@corp.example")
        task = await runtime.submit(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            submission=GoalSubmission(goal="schedule a meeting with alice", timezone="UTC"),
        )
        outcome = await runtime.trigger(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
            transport=None,
        )
    assert outcome.task_state == "FAILED"
    assert "google account" in outcome.message
    assert model.calls == [], "the account gate closes before any model call"


def test_the_understanding_schema_is_unaffected_by_this_slice() -> None:
    """The preparation profile adds no field to the calendar understanding."""
    assert set(UnderstandingOutput.model_fields) == {"intent", "guests", "window"}
