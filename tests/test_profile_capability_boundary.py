"""The governed profile↔capability boundary, on both sides of the seam.

The generic Task Manager (`POST /api/v1/tasks` → `create_task`) seals a profile
for live execution but does **not** gate `allowed_tools`: callers, and the test
harness, legitimately use capability scope tokens (`gmail`, `finance`, `demo`)
that a profile-level tool/subset gate cannot reason about. The authoritative
``allowed_tools`` semantics belong to ``policy.rules._task_allows``.

Compatibility between a sealed profile and its tool/capability scope is enforced
where a task can actually execute — the governed runtime path:

* ``AgentRuntime.submit`` (the governed live-sealing boundary) refuses to seal an
  executable task whose profile does not licence its ``allowed_tools``; and
* ``AgentRuntime._program_for`` (the runtime's pre-dispatch program gate) refuses
  any task that reached the row by another route before a single step is built.

Both call the one rule, ``runtime.translator.profile_scope_violation``. These
tests prove the positive side (generic creation is unchanged) and the negative
side (a foreign profile↔capability combination cannot be sealed, planned, or
dispatched) plus the mapping's fail-closed edges.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from kai.db.engine import async_session_factory
from kai.planner.models import PLAN_STATUS_ACCEPTED, TaskPlan
from kai.planner.schemas import PlanProposal
from kai.runtime.models import RuntimeExecution
from kai.runtime.schemas import GoalSubmission
from kai.runtime.service import AgentRuntime, RuntimeFatal, RuntimeScopeError
from kai.runtime.translator import (
    PROFILE_ALLOWED_CAPABILITIES,
    PROFILE_ALLOWED_TOOLS,
    SLICE_ALLOWED_TOOLS,
    build_preparation_contract,
    build_slice_contract,
    profile_scope_violation,
)
from kai.tasks.models import Task
from kai.tasks.schemas import TaskContractInput
from kai.tasks.service import count_tasks, create_task
from tool_harness import insert_account, make_gateway, make_task, make_tenant_user

GOAL = "prepare a checklist for the offsite"
CRITERIA = ["a plan was prepared", "information gaps identified"]


# ---------------------------------------------------------------------------
# The rule itself: permit the licensed scope, refuse everything else
# ---------------------------------------------------------------------------


def test_the_rule_accepts_the_licensed_slice_contracts() -> None:
    """Both live-sealable profiles accept exactly the scopes their builders seal."""
    assert profile_scope_violation(
        "calendar_meeting", build_slice_contract(GOAL).allowed_tools
    ) == ()
    assert profile_scope_violation(
        "task_preparation", build_preparation_contract(GOAL).allowed_tools
    ) == ()
    # Capability names are a licensed namespace too, mirroring ``_task_allows``.
    assert profile_scope_violation("calendar_meeting", ["read_calendar"]) == ()
    assert profile_scope_violation("task_preparation", ["prepare_checklist"]) == ()
    assert PROFILE_ALLOWED_TOOLS["calendar_meeting"] == frozenset(SLICE_ALLOWED_TOOLS)


def test_the_rule_refuses_foreign_scopes_and_unknown_profiles() -> None:
    """A token outside the profile, or any token for an unknown profile, is refused."""
    assert profile_scope_violation("calendar_meeting", ["gmail.send"]) == ("gmail.send",)
    assert profile_scope_violation("task_preparation", ["calendar.read_slots"]) == (
        "calendar.read_slots",
    )
    # A capability can never cross profiles: preparation must not read a calendar.
    assert profile_scope_violation("task_preparation", ["read_calendar"]) == (
        "read_calendar",
    )
    # ``gmail_send`` is not a live-sealable profile and has no scope: fail closed.
    assert profile_scope_violation("gmail_send", ["gmail.send"]) == ("gmail.send",)
    # Unknown profile = no scope = everything refused, never an allow-all fallback.
    assert profile_scope_violation("invoice_paid", ["anything"]) == ("anything",)
    assert "gmail" not in PROFILE_ALLOWED_CAPABILITIES["calendar_meeting"]
    assert "gmail" not in PROFILE_ALLOWED_CAPABILITIES["task_preparation"]


# ---------------------------------------------------------------------------
# Positive: generic creation keeps accepting every scope token
# ---------------------------------------------------------------------------


async def test_generic_creation_accepts_capability_scope_tokens() -> None:
    """``create_task``/``make_task`` accept the tokens the suite relies on.

    These are the same shapes the policy, approval, identity, and gateway tests
    use. Nothing at creation checks them against a profile; the profile boundary
    lives on the governed execution path.
    """
    for token in ("gmail", "finance", "demo"):
        tenant, user = await make_tenant_user(f"{token}@corp.example")
        task = await make_task(
            tenant_id=tenant.id, user_id=user.id, allowed_tools=[token]
        )
        assert task.allowed_tools == [token]


async def test_generic_creation_does_not_gate_profile_tool_scope() -> None:
    """The incompatible creation-time gate is gone: ``create_task`` seals the row.

    A ``calendar_meeting`` contract naming ``gmail.send`` persists through the
    generic path. It is the *runtime*, not creation, that refuses to execute it.
    """
    tenant, user = await make_tenant_user("seal-generic@corp.example")
    contract = TaskContractInput(
        goal=GOAL,
        success_criteria=list(CRITERIA),
        fulfillment_profile="calendar_meeting",
        allowed_tools=["gmail.send", "calendar.create_event"],
        risk_policy="MEDIUM",
        max_steps=10,
        verification_method="automatic",
        fallback="ask the user",
    )
    async with async_session_factory() as session:
        task = await create_task(
            session, tenant_id=tenant.id, user_id=user.id, contract=contract
        )
        assert task.allowed_tools == ["gmail.send", "calendar.create_event"]
        assert task.fulfillment_profile == "calendar_meeting"


# ---------------------------------------------------------------------------
# Negative, seam 1: the governed live-sealing boundary (submit)
# ---------------------------------------------------------------------------


async def test_runtime_submit_refuses_an_incompatible_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contract the profile does not licence cannot be sealed as an executable task.

    The runtime builds its contracts itself, so the incompatible one is injected
    at the contract seam to exercise the real ``submit`` guard: no ``Task`` and
    no execution envelope are persisted, and the refusal names the offender.
    """

    def _incompatible(goal: str) -> TaskContractInput:
        return TaskContractInput(
            goal=goal,
            success_criteria=["done"],
            fulfillment_profile="calendar_meeting",
            allowed_tools=["gmail.send"],
            risk_policy="MEDIUM",
            max_steps=10,
            verification_method="automatic",
            fallback="ask the user",
        )

    monkeypatch.setattr("kai.runtime.service.build_slice_contract", _incompatible)

    gateway, _ = make_gateway()
    runtime = AgentRuntime(gateway=gateway, model=_StubModel(None))
    tenant, user = await make_tenant_user("submit-seal@corp.example")
    async with async_session_factory() as session:
        with pytest.raises(RuntimeScopeError) as excinfo:
            await runtime.submit(
                session,
                tenant_id=tenant.id,
                user_id=user.id,
                submission=GoalSubmission(goal="email alice", timezone="UTC"),
            )
        assert "gmail.send" in str(excinfo.value)
        # Nothing was sealed into the tenant and no execution envelope was made.
        assert await count_tasks(session, tenant_id=tenant.id) == 0
        row = await session.scalar(
            select(RuntimeExecution).where(RuntimeExecution.tenant_id == tenant.id)
        )
        assert row is None


# ---------------------------------------------------------------------------
# Negative, seam 2: the pre-dispatch gate (a task that reached the row)
# ---------------------------------------------------------------------------


async def test_task_preparation_cannot_execute_a_foreign_capability() -> None:
    """A preparation profile cannot be driven into a calendar capability.

    The task is persisted directly (bypassing ``submit``) with a
    ``task_preparation`` profile but a calendar scope, then driven. It is refused
    at the runtime's program gate before any step is built or dispatched, and it
    fails through the Task Manager machine rather than running.
    """
    gateway, _ = make_gateway()
    model = _StubModel(_plan(action="read_calendar"))
    runtime = AgentRuntime(gateway=gateway, model=model)

    tenant, user = await make_tenant_user("prep-calendar@corp.example")
    contract = TaskContractInput(
        goal=GOAL,
        success_criteria=list(CRITERIA),
        fulfillment_profile="task_preparation",
        allowed_tools=["calendar.read_slots"],
        risk_policy="LOW",
        max_steps=2,
        verification_method="automatic",
        fallback="provide the missing details",
    )
    async with async_session_factory() as session:
        task = await create_task(
            session, tenant_id=tenant.id, user_id=user.id, contract=contract
        )
        task_id = task.id
        row = RuntimeExecution(
            task_id=task_id,
            tenant_id=tenant.id,
            user_id=user.id,
            timezone="UTC",
            scenario=None,
            identities=[],
            evidence=[],
            cursor=0,
            steps_used=0,
        )
        session.add(row)
        await session.commit()

        outcome = await runtime.trigger(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task_id,
            transport=None,
        )

        assert outcome.task_state == "FAILED"
        assert outcome.outcome == "FAILED"
        assert "does not permit" in outcome.message
        # The offender is named as the *capability* the contract derived, not the
        # tool token: the check is profile↔capability, the profile's own vocabulary.
        assert "read_calendar" in outcome.message

        persisted = await session.scalar(select(Task).where(Task.id == task_id))
        assert persisted is not None and persisted.state == "FAILED"
        refreshed = await session.scalar(
            select(RuntimeExecution).where(RuntimeExecution.task_id == task_id)
        )
        assert refreshed is not None
        # No step was dispatched: nothing was invoked and nothing was recorded.
        assert refreshed.evidence == []
        assert refreshed.steps_used == 0


async def test_a_prepare_task_still_completes() -> None:
    """The approved ``task_preparation`` behaviour is preserved end to end."""
    gateway, _ = make_gateway()
    model = _StubModel(_plan(action="prepare_checklist"))
    runtime = AgentRuntime(gateway=gateway, model=model)

    tenant, user = await make_tenant_user("prep-ok@corp.example")
    async with async_session_factory() as session:
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
        assert outcome.task_state == "COMPLETED", outcome.message


# ---------------------------------------------------------------------------
# Fail-closed on an unlicensed profile, at the program gate (seam 2)
# ---------------------------------------------------------------------------
#
# An unknown/non-live-sealable profile has no entry in the execution registry,
# so ``PROFILE_ALLOWED_CAPABILITIES.get(profile)`` is ``None``. The gate must
# treat that as "no licence" and refuse, never as "no restriction" and fall
# through to the capability subset check — that check is derived from
# ``task.allowed_tools`` and is not profile-scoped, so a representable
# capability would otherwise let the step reach ``gateway.execute`` and the
# refusal would only arrive later, from the fulfillment evaluator.


def test_program_for_refuses_an_unlicensed_profile() -> None:
    """An unlicensed profile is refused even with a representable capability.

    ``allowed_capabilities_from_task`` succeeds here (the contract names a tool
    this slice can execute), which is exactly the case the old
    ``if licensed is not None`` skipped. The refusal must come from the profile,
    not from the capability subset check.
    """
    task = _in_memory_task(
        profile="invoice_paid", allowed_tools=["calendar.read_slots"]
    )
    with pytest.raises(RuntimeFatal) as excinfo:
        _program_for(task, action="read_calendar")
    message = str(excinfo.value)
    assert "invoice_paid" in message
    assert "not licensed" in message
    assert "does not permit" not in message


def test_program_for_still_builds_for_both_licensed_profiles() -> None:
    """Regression: the fail-closed change must not refuse licensed profiles."""
    calendar = _in_memory_task(
        profile="calendar_meeting", allowed_tools=SLICE_ALLOWED_TOOLS
    )
    calendar_program = _program_for(calendar, action="read_calendar")
    assert calendar_program.program_id is not None

    preparation = _in_memory_task(
        profile="task_preparation", allowed_tools=["task.prepare"]
    )
    preparation_program = _program_for(preparation, action="prepare_checklist")
    assert preparation_program.program_id is not None


async def test_an_unlicensed_profile_cannot_dispatch_a_step() -> None:
    """A persisted row with an unknown profile is refused before any dispatch.

    The row bypasses ``submit`` (whose live-sealing gate and ``create_task``'s
    ``LIVE_SEALABLE_PROFILES`` gate would both refuse it) and is written straight
    to the tables: an unknown profile with a representable capability, a
    resumable plan already accepted, and a scenario/identity snapshot so the
    earlier stages are not what stops it. Driving it must fail at the program
    gate — no step dispatched, so no evidence recorded and ``steps_used`` stays
    zero — rather than run the step and only then let the fulfillment evaluator
    refuse the unknown profile.
    """
    gateway, _ = make_gateway()
    runtime = AgentRuntime(gateway=gateway, model=_StubModel(None))

    tenant, user = await make_tenant_user("unlicensed-profile@corp.example")
    async with async_session_factory() as session:
        account = await insert_account(
            tenant_id=tenant.id,
            user_id=user.id,
            email="owner@corp.example",
        )
        assert account.id is not None

        task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            state="EXECUTING",
            goal=GOAL,
            success_criteria=list(CRITERIA),
            fulfillment_profile="invoice_paid",
            allowed_tools=["calendar.read_slots"],
            risk_policy="LOW",
            max_steps=10,
            verification_method="automatic",
            fallback="ask the user",
        )
        session.add(task)
        await session.flush()

        session.add(
            TaskPlan(
                tenant_id=tenant.id,
                task_id=task.id,
                version=1,
                status=PLAN_STATUS_ACCEPTED,
                plan_json=_plan(action="read_calendar"),
                model_id="stub-unlicensed-profile-v0",
            )
        )
        session.add(
            RuntimeExecution(
                task_id=task.id,
                tenant_id=tenant.id,
                user_id=user.id,
                timezone="UTC",
                scenario=_scenario(),
                identities=[
                    {
                        "name": "Alice",
                        "email": "alice@example.com",
                        "status": "RESOLVED",
                        "confidence": 0.9,
                    }
                ],
                evidence=[],
                cursor=0,
                steps_used=0,
            )
        )
        await session.commit()
        task_id = task.id

        outcome = await runtime.trigger(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task_id,
            transport=None,
        )

        assert outcome.task_state == "FAILED"
        assert outcome.outcome == "FAILED"
        assert "invoice_paid" in outcome.message
        assert "not licensed" in outcome.message
        # The refusal must be the profile gate's, not the later evaluator's.
        assert "no fulfillment evaluator" not in outcome.message

        persisted = await session.scalar(select(Task).where(Task.id == task_id))
        assert persisted is not None and persisted.state == "FAILED"

        refreshed = await session.scalar(
            select(RuntimeExecution).where(RuntimeExecution.task_id == task_id)
        )
        assert refreshed is not None
        assert refreshed.evidence == []
        assert refreshed.steps_used == 0


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _plan(*, action: str) -> dict:
    return {
        "goal": GOAL,
        "success_criteria": list(CRITERIA),
        "steps": [
            {
                "step_id": "p1",
                "order": 1,
                "action": action,
                "purpose": "one local step",
                "inputs": ["goal"],
                "expected_output": "a result",
                "verification_hint": "non-empty",
            }
        ],
        "assumptions": [],
        "unresolved_questions": [],
    }


class _StubModel:
    """A planner model that returns one fixed plan; used as a labelled stub."""

    model_id = "stub-profile-boundary-v0"

    def __init__(self, response: dict | None) -> None:
        self.response = response
        self.calls: list[list[dict]] = []

    async def generate_plan(self, *, messages: list[dict]) -> dict:
        self.calls.append(messages)
        assert self.response is not None, "this test should never reach planning"
        return self.response


def _in_memory_task(*, profile: str, allowed_tools: list[str]) -> Task:
    """A transient ``Task`` with an explicit id; enough for ``_program_for``."""
    return Task(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        state="EXECUTING",
        goal=GOAL,
        success_criteria=list(CRITERIA),
        fulfillment_profile=profile,
        allowed_tools=list(allowed_tools),
        risk_policy="LOW",
        max_steps=10,
        verification_method="automatic",
        fallback="ask the user",
    )


def _program_for(task: Task, *, action: str):
    """Run the real program gate for one transient task and one-step plan."""
    gateway, _ = make_gateway()
    runtime = AgentRuntime(gateway=gateway, model=_StubModel(None))
    proposal = PlanProposal.model_validate(_plan(action=action))
    return runtime._program_for(
        task,
        proposal=proposal,
        plan_status=PLAN_STATUS_ACCEPTED,
        plan_version=1,
        plan_hash="0" * 64,
        row=RuntimeExecution(
            task_id=task.id,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            timezone="UTC",
            scenario=None,
            identities=[],
            evidence=[],
            cursor=0,
            steps_used=0,
        ),
    )


def _scenario() -> dict:
    """A valid persisted understanding snapshot (not a model call).

    The window spans a full week so it always contains at least one workday: the
    pre-fix defect path has to get all the way to ``gateway.execute`` for the
    test to demonstrate what the fix removes, rather than stopping earlier on an
    empty candidate-day set.
    """
    start = date.today() + timedelta(days=1)
    return {
        "intent": "schedule_meeting",
        "guests": ["Alice"],
        "window": {
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=7)).isoformat(),
            "earliest_time": "09:00",
            "title": "Sync",
        },
    }
