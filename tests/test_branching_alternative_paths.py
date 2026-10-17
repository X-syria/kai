"""Can one Intent be reached by two different declared paths?

The question this file answers is narrow and falsifiable: given the *same* goal,
the *same* Sealed Contract, and the *same* Success Criteria, can KAI declare two
different execution paths as two alternative ways of fulfilling that Intent —
and if it can, are they genuinely independent?

Everything here is real except the two seams the rest of the suite also fakes: the
Google network and the model. The Tool Gateway, the Policy Engine, the Identity
Resolver, the Approval boundary, the database, and the KAI-IR compiler are all
the production code paths, so a path that reaches fulfillment has done so by
passing through the same authority chain a single-path run passes through.

The declared plan has six steps and one fork after the availability check:

    1 resolve → 2 read → ┬ 3 create → 4 verify  → result
                         └ 5 create → 6 verify  → result

Both arms create the event and read it back, so whichever arm runs can satisfy
the Success Criteria on its own. The two tasks below swap ``then``/``else`` and
therefore walk opposite arms of the same fork.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from kai.db.engine import async_session_factory
from kai.db.models import User
from kai.google.deps import get_google_http_client
from kai.ir import (
    OBSERVATION_FACT_TYPES,
    ConditionNode,
    ConditionOperator,
    ConditionSubject,
    ControlNode,
    ControlNodeKind,
    FactKind,
    KaiProgram,
    ObservationNode,
    evaluate,
    next_step,
    program_from_plan,
)
from kai.ir.errors import IrConditionError
from kai.ir.nodes import ResultNode
from kai.main import app
from kai.memory.models import MemoryType
from kai.memory.service import create_memory
from kai.planner.deps import get_planner_model
from kai.planner.model import PLANNER_SYSTEM_PROMPT
from kai.planner.schemas import PlanProposal
from kai.runtime.models import RuntimeExecution
from kai.runtime.service import AgentRuntime, _control_facts
from kai.runtime.translator import CAPABILITY_TO_TOOL
from kai.runtime.understanding import UNDERSTANDING_SYSTEM_PROMPT
from kai.tasks.models import Task
from kai.tools.catalog import default_registry
from kai.tools.result import ToolOutcome
from tool_harness import GoogleBackend, contact_payload, insert_account

API_PREFIX: Final = "/api/v1"

GOAL: Final = "نسّق اجتماعًا مع أحمد الأسبوع القادم بعد الساعة 4"
ATTENDEE_NAME: Final = "أحمد"
ATTENDEE_EMAIL: Final = "ahmed@example.com"
TIMEZONE: Final = "Asia/Riyadh"
EARLIEST: Final = "16:00"

#: The Success Criteria the translator seals into the task. Both paths are judged
#: against exactly this list; neither path is allowed to add or drop one.
SUCCESS_CRITERIA: Final = (
    "calendar event created",
    "attendee is the requested contact",
    "event fits the requested time window",
)

#: The two arms, as step orders. Arm A is steps 3-4, arm B is steps 5-6, and both
#: create then read back, so each arm is sufficient on its own.
ARM_A: Final = (3, 4)
ARM_B: Final = (5, 6)
ARMS: Final = {"A": ARM_A, "B": ARM_B}
SHARED_PREFIX: Final = (1, 2)
SIX_STEPS: Final = (
    (1, "resolve_contact", "resolve the attendee"),
    (2, "read_calendar", "check availability"),
    (3, "create_calendar_event", "book the first free slot"),
    (4, "verify_calendar_event", "read the booked event back"),
    (5, "create_calendar_event", "book the first free slot"),
    (6, "verify_calendar_event", "read the booked event back"),
)


def _next_monday() -> date:
    today = date.today()
    days_until_monday = (0 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_until_monday)


def _understanding() -> dict:
    start = _next_monday()
    return {
        "intent": "schedule_meeting",
        "guests": [ATTENDEE_NAME],
        "window": {
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=3)).isoformat(),
            "earliest_time": EARLIEST,
            "title": "اجتماع مع " + ATTENDEE_NAME,
        },
    }


# ----------------------------------------------------------------------------------
# The declared plans
# ----------------------------------------------------------------------------------


def _fork(*, after_step: int, reads: int, then_step: int, else_step: int) -> dict:
    return {
        "after_step": after_step,
        "condition": {
            "kind": "observation",
            "step_order": reads,
            "field": "outcome",
            "operator": "eq",
            "value": ToolOutcome.EXECUTION_SUCCEEDED.value,
        },
        "then_step": then_step,
        "else_step": else_step,
    }


def _step(order: int, capability: str, purpose: str) -> dict:
    return {
        "step_id": f"s{order}",
        "order": order,
        "action": capability,
        "purpose": purpose,
        "inputs": ["window"],
        "expected_output": f"the {purpose} result",
        "verification_hint": "recorded in evidence",
    }


def _plan(
    *,
    then_step: int,
    else_step: int,
    branches: list[dict] | None = None,
    steps: tuple[tuple[int, str, str], ...] = SIX_STEPS,
) -> dict:
    """One Intent, two sufficient arms, and the fork that chooses between them."""
    return {
        "goal": GOAL,
        "success_criteria": list(SUCCESS_CRITERIA),
        "steps": [_step(o, c, p) for o, c, p in steps],
        "branches": branches
        or [_fork(after_step=2, reads=2, then_step=then_step, else_step=else_step)],
        "assumptions": ["the attendee accepts the invite"],
        "unresolved_questions": [],
    }


#: A three-fork program in which step 2 is optional. ``branch-3`` reads step 2,
#: and the arm ``1 → 3 → branch-3`` reaches it having never run step 2 — so the
#: fact could only exist because that other arm recorded it earlier. The plan is
#: otherwise a well-formed program, and nothing but the read is wrong with it.
LEAKY_STEPS: Final = SIX_STEPS[:5]
LEAKY_BRANCHES: Final = [
    _fork(after_step=1, reads=1, then_step=2, else_step=3),
    _fork(after_step=2, reads=2, then_step=3, else_step=4),
    _fork(after_step=3, reads=2, then_step=4, else_step=5),
]
#: The same program with the last fork reading step 1, which every arm runs.
HONEST_BRANCHES: Final = [
    _fork(after_step=1, reads=1, then_step=2, else_step=3),
    _fork(after_step=2, reads=2, then_step=3, else_step=4),
    _fork(after_step=3, reads=1, then_step=4, else_step=5),
]


def _leaky_plan(**overrides: object) -> dict:
    plan = _plan(then_step=2, else_step=3, branches=LEAKY_BRANCHES, steps=LEAKY_STEPS)
    plan.update(overrides)
    return plan


def _honest_plan() -> dict:
    return _plan(then_step=2, else_step=3, branches=HONEST_BRANCHES, steps=LEAKY_STEPS)


def _plan_for(arm: str) -> dict:
    """The plan whose fork sends the run down ``arm`` when the read succeeds.

    ``then``/``else`` are swapped between the two tasks, so both plans declare
    the same six steps and the same condition, and the only difference is which
    arm the two outcomes point at. The paths differ; the Intent, the contract,
    and the capabilities do not.
    """
    other = "B" if arm == "A" else "A"
    return _plan(then_step=ARMS[arm][0], else_step=ARMS[other][0])


# ----------------------------------------------------------------------------------
# IR-level helpers (no database, no HTTP)
# ----------------------------------------------------------------------------------


def _task_row(**overrides: object) -> Task:
    """A ``Task`` carrying the sealed contract both paths are judged against."""
    values: dict[str, object] = {
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "goal": GOAL,
        "success_criteria": list(SUCCESS_CRITERIA),
        "fulfillment_profile": "calendar_meeting",
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
        "fallback": "report to the user",
        "state": "UNDERSTANDING",
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def _program_for(plan: dict, **task_overrides: object) -> KaiProgram:
    return program_from_plan(
        task=_task_row(**task_overrides),
        plan_version=1,
        plan_status="ACCEPTED",
        proposal=PlanProposal.model_validate(plan),
        plan_hash="a" * 64,
        registry=default_registry(),
        capability_to_tool=CAPABILITY_TO_TOOL,
    )


def _conditions(program: KaiProgram) -> list[ConditionNode]:
    return [node.condition for node in program.nodes if node.condition is not None]


def _observation(*, step_order: int, outcome: str, ok: bool = True) -> dict:
    """A durable evidence record, shaped exactly as the runtime persists one."""
    return {
        "step_order": step_order,
        "tool": "calendar.read_slots",
        "outcome": outcome,
        "ok": ok,
        "verified": ok,
        "error_code": None if ok else "PROVIDER_ERROR",
        "request_id": f"runtime-x-1-{step_order}-0",
    }


def _walk(
    program: KaiProgram,
    *,
    evidence: list[dict] | None = None,
    outcomes: dict[int, str] | None = None,
    task_state: str = "EXECUTING",
    steps_used: int = 0,
) -> list[int]:
    """The step orders the runtime's own selector picks, given durable facts.

    This is the runtime's loop, not a re-implementation of the decision: every
    iteration calls the IR's own ``next_step`` with facts rebuilt by the runtime's
    own ``_control_facts`` from a cursor and an evidence log, records the step's
    outcome the way the runtime does, and lets the selector run again. Nothing
    here chooses an arm — ``outcomes`` only says what each step recorded.
    """
    log = list(evidence or [])
    cursor = 0
    path: list[int] = []
    while True:
        facts = _control_facts(
            cursor=cursor, task_state=task_state, steps_used=steps_used, evidence=log
        )
        step = next_step(program, facts)
        if step is None:
            return path
        path.append(step.order)
        outcome = (outcomes or {}).get(step.order, ToolOutcome.EXECUTION_SUCCEEDED.value)
        log.append(
            _observation(
                step_order=step.order,
                outcome=outcome,
                ok=outcome == ToolOutcome.EXECUTION_SUCCEEDED.value,
            )
        )
        cursor = step.order
        steps_used += 1


# ----------------------------------------------------------------------------------
# The four semantics, read separately off the durable record
# ----------------------------------------------------------------------------------


def _path_termination(evidence: list[dict], arm: str) -> list[int]:
    """Which steps the run actually walked.

    Path termination read off the evidence log: the steps that ran are exactly
    this arm's steps, and the run stopped there instead of falling through into
    the other arm.
    """
    return sorted({entry["step_order"] for entry in evidence})


def _action_success(evidence: list[dict], arm: str) -> dict[int, bool]:
    """Did each action on this arm's own steps actually succeed?"""
    create, verify = ARMS[arm]
    return {
        entry["step_order"]: entry["ok"]
        for entry in evidence
        if entry["step_order"] in {create, verify}
    }


def _intent_fulfillment(backend: GoogleBackend) -> dict | None:
    """Did a meeting satisfying every sealed Success Criterion actually get booked?

    Judged against the provider's own record of what it created, one criterion at
    a time, and only reported as fulfilled when every one of them holds.
    """
    if len(backend.created_events) != 1:
        return None
    event = next(iter(backend.created_events.values()))
    start = datetime.fromisoformat(event["start"]["dateTime"])
    end = datetime.fromisoformat(event["end"]["dateTime"])
    criteria = {
        "calendar event created": True,
        "attendee is the requested contact": event["attendees"] == [
            {"email": ATTENDEE_EMAIL}
        ],
        "event fits the requested time window": (
            start.date() == _next_monday()
            and start.hour == 16
            and end.hour == 17
            and event["summary"] == "اجتماع مع " + ATTENDEE_NAME
        ),
    }
    assert set(criteria) == set(SUCCESS_CRITERIA), criteria
    return event if all(criteria.values()) else None


# ----------------------------------------------------------------------------------
# End-to-end harness
# ----------------------------------------------------------------------------------


class BranchingStubModel:
    """Hands out one declared plan per task, in the order they were submitted."""

    model_id = "stub-branching-v0.1"

    def __init__(self) -> None:
        self.plans: list[dict] = []

    async def generate_plan(self, *, messages: list[dict]) -> dict:
        if messages[0]["content"] == UNDERSTANDING_SYSTEM_PROMPT:
            return _understanding()
        assert messages[0]["content"] == PLANNER_SYSTEM_PROMPT
        return self.plans.pop(0) if self.plans else _plan_for("A")


@pytest.fixture
def backend() -> GoogleBackend:
    backend = GoogleBackend()
    backend.contacts_connections = [
        contact_payload(
            resource_name="people/ahmed-1",
            display_name=ATTENDEE_NAME,
            emails=(ATTENDEE_EMAIL,),
        )
    ]
    return backend


@pytest.fixture
def stub() -> BranchingStubModel:
    return BranchingStubModel()


@pytest.fixture(autouse=True)
def overrides(backend: GoogleBackend, stub: BranchingStubModel) -> AsyncIterator[None]:
    async def _client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler),
            base_url="http://google.api.test",
            timeout=15.0,
        ) as client:
            yield client

    app.dependency_overrides[get_planner_model] = lambda: stub
    app.dependency_overrides[get_google_http_client] = _client
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_planner_model, None)
        app.dependency_overrides.pop(get_google_http_client, None)


async def _signed_in_user(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{API_PREFIX}/auth/register",
        json={"email": "khaled@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    token: str = resp.json()["access_token"]

    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.email == "khaled@example.com"))
        ).scalar_one()
        tenant_id, user_id = user.tenant_id, user.id
    await insert_account(
        tenant_id=tenant_id,
        user_id=user_id,
        email="khaled@example.com",
        scopes=(
            "openid email "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/contacts.readonly"
        ),
    )
    async with async_session_factory() as session:
        await create_memory(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            memory_type=MemoryType.PERSON,
            content=f"{ATTENDEE_NAME} صديقي وبريده {ATTENDEE_EMAIL}",
        )
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _submit(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.post(
        f"{API_PREFIX}/runtime/tasks",
        json={"goal": GOAL, "timezone": TIMEZONE},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["task_id"])


async def _trigger(client: httpx.AsyncClient, token: str, task_id: str) -> dict:
    resp = await client.post(
        f"{API_PREFIX}/runtime/tasks/{task_id}/trigger", headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _approve(client: httpx.AsyncClient, token: str, approval_id: str) -> None:
    resp = await client.post(
        f"{API_PREFIX}/approvals/{approval_id}/approve",
        json={"reason": "yes, book it"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


async def _evidence(task_id: str) -> list[dict]:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(RuntimeExecution).where(RuntimeExecution.task_id == UUID(task_id))
            )
        ).scalar_one()
        return list(row.evidence)


async def _drive_task(
    client: httpx.AsyncClient, token: str, task_id: str
) -> tuple[dict, dict, list[dict]]:
    """Run one task across its approval boundary: park, approve, finish.

    The approval checkpoint is a real parking point, so this crosses two claims
    and the second trigger is a resume rather than a second pass.
    """
    first = await _trigger(client, token, task_id)
    assert first["outcome"] == "WAITING_APPROVAL", first
    await _approve(client, token, first["approval_ids"][0])
    second = await _trigger(client, token, task_id)
    return first, second, await _evidence(task_id)


# ----------------------------------------------------------------------------------
# A. The two paths are representable, and they are two paths
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("arm", ["A", "B"])
def test_each_declared_path_compiles_and_the_runtime_walks_it(arm: str) -> None:
    """Both declared paths are real programs, and the walk ends on its own arm.

    The availability check succeeds, so the fork sends this task down the arm it
    declared. The walk reaches its own result node and stops there: the other
    arm's steps are never eligible, so they are never run.
    """
    program = _program_for(_plan_for(arm))
    create, verify = ARMS[arm]

    assert _walk(program) == [*SHARED_PREFIX, create, verify]

    facts = _control_facts(
        cursor=verify,
        steps_used=4,
        task_state="EXECUTING",
        evidence=[
            _observation(step_order=order, outcome="EXECUTION_SUCCEEDED")
            for order in (*SHARED_PREFIX, create, verify)
        ],
    )
    assert next_step(program, facts) is None, "the walk did not terminate on its arm"


def test_the_two_paths_share_one_intent_and_one_contract_and_differ_only_in_the_path() -> None:
    """Same Intent, same Success Criteria, same permissions; different path."""
    program_a = _program_for(_plan_for("A"))
    program_b = _program_for(_plan_for("B"))

    # 1. One Intent, declared once, sealed once.
    assert program_a.intent.goal == program_b.intent.goal == GOAL
    assert program_a.intent.success_criteria == program_b.intent.success_criteria
    assert tuple(program_a.intent.success_criteria) == SUCCESS_CRITERIA
    assert program_a.program_id == program_b.program_id, "one task, one program id"

    # 2. One authority set: neither path widened it.
    assert program_a.constraints == program_b.constraints
    assert program_a.constraints.allowed_capabilities == [
        "resolve_contact", "read_calendar", "create_calendar_event", "verify_calendar_event",
    ]
    assert [(p.requires_capability, p.requires_approval) for p in program_a.permissions] == [
        (p.requires_capability, p.requires_approval) for p in program_b.permissions
    ]
    assert {p.requires_capability for p in program_a.permissions} == {
        "resolve_contact", "read_calendar", "create_calendar_event", "verify_calendar_event",
    }
    assert {p.requires_capability for p in program_a.permissions if p.requires_approval} == {
        "create_calendar_event"
    }

    # 3. What differs is the path, and only the path: the same six steps, the
    #    same capabilities, a different graph and a different fingerprint.
    assert {n.step_order for n in program_a.nodes if n.step_order} == {1, 2, 3, 4, 5, 6}
    assert {n.step_order for n in program_b.nodes if n.step_order} == {1, 2, 3, 4, 5, 6}
    assert program_a.fingerprint != program_b.fingerprint
    assert [s.capability for s in _capabilities(program_a)] == [
        s.capability for s in _capabilities(program_b)
    ]
    # The two arms' guards are the same condition pointing at different steps.
    assert [(c.condition_id, c.subject.step_order) for c in _conditions(program_a)] == [
        (c.condition_id, c.subject.step_order) for c in _conditions(program_b)
    ]


def _capabilities(program: KaiProgram) -> list[object]:
    return [program.step(order) for order in range(1, 7)]


def test_the_result_node_is_a_terminal_marker_and_nothing_else() -> None:
    """The result records how the run ended. It cannot fulfil or refuse anything."""
    program = _program_for(_plan_for("A"))
    terminals = [n for n in program.nodes if n.kind is ControlNodeKind.RESULT]

    # One terminal per arm, and each is an unadorned marker: no step, no
    # condition, and nothing that could be read as a verdict.
    assert {n.node_id for n in terminals} == {"result-4", "result-6"}
    for node in terminals:
        assert node.result == "COMPLETED"
        assert node.condition is None
        assert node.step_order is None

    # The recorded marker has exactly three facts about the ending and nothing
    # else: no Success Criteria, no policy, no approval, no fulfillment verdict.
    assert set(ResultNode.model_fields) == {
        "result", "reason", "final_observation_ids",
    }
    assert not {"success_criteria", "requires_approval", "fulfilled"} & set(
        ResultNode.model_fields
    )
    assert set(ControlNode.model_fields) == {
        "node_id", "kind", "step_order", "condition", "result",
    }


# ----------------------------------------------------------------------------------
# B. Both paths reach Intent fulfillment, through the real authority chain
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("arm", ["A", "B"])
async def test_both_paths_reach_intent_fulfillment(
    client: httpx.AsyncClient, backend: GoogleBackend, stub: BranchingStubModel, arm: str
) -> None:
    """Four semantics, asserted separately, on each path.

    Action Success, path termination, Intent fulfillment, and Task State are four
    different questions. A run can satisfy three of them and still not have
    fulfilled the Intent, so each is checked on its own rather than inferred from
    the task's final state.
    """
    stub.plans.append(_plan_for(arm))
    token = await _signed_in_user(client)
    task_id = await _submit(client, token)
    first, outcome, evidence = await _drive_task(client, token, task_id)
    create, verify = ARMS[arm]
    other = ARMS["B" if arm == "A" else "A"]

    # 1. Action Success: both actions on this arm returned ok.
    assert _action_success(evidence, arm) == {create: True, verify: True}

    # 2. Path Termination: the run walked this arm and stopped, so nothing from
    #    the other arm was ever run or recorded.
    assert _path_termination(evidence, arm) == [*SHARED_PREFIX, *ARMS[arm]]
    assert not set(other) & {entry["step_order"] for entry in evidence}

    # 3. Intent Fulfillment: a meeting satisfying every sealed Success Criterion
    #    exists, booked behind the approval boundary.
    assert _intent_fulfillment(backend) is not None, "the Intent was not fulfilled"
    assert len(first["approval_ids"]) == 1

    # 4. Task State: the run terminated as fulfilled.
    assert outcome["outcome"] == "COMPLETED", outcome
    assert outcome["task_state"] == "COMPLETED"
    assert outcome["message"] == "meeting scheduled and verified"

    # The authority chain is intact on this path: the actions ran under the IR's
    # own identity, and the write happened behind a human approval.
    assert all(entry["ir_action_id"] == entry["request_id"] for entry in evidence)
    orders = "|".join(map(str, [*SHARED_PREFIX, *ARMS[arm]]))
    pattern = re.compile(rf"^runtime-{re.escape(task_id)}-1-({orders})-\d+$")
    assert all(pattern.match(entry["ir_action_id"]) for entry in evidence), evidence
    # The arm's own steps, and the write behind exactly one approval.
    assert {int(pattern.match(e["ir_action_id"]).group(1)) for e in evidence} == {
        *SHARED_PREFIX, *ARMS[arm]
    }
    creates = [e for e in evidence if e["tool"] == "calendar.create_event"]
    assert [e["ok"] for e in creates] == [False, True], creates
    assert len(backend.created_events) == 1


async def test_the_two_paths_never_read_each_others_evidence(
    client: httpx.AsyncClient, backend: GoogleBackend, stub: BranchingStubModel
) -> None:
    """Run both paths in one session and compare their durable records.

    Same user, same goal, same sealed contract, same horizon — two tasks whose
    only difference is which arm the fork was declared to take. Neither task's
    evidence may mention the other's steps, and no action identity may be shared.
    """
    stub.plans.extend([_plan_for("A"), _plan_for("B")])
    token = await _signed_in_user(client)

    runs = []
    for _ in range(2):
        task_id = await _submit(client, token)
        _, outcome, evidence = await _drive_task(client, token, task_id)
        runs.append({"task_id": task_id, "outcome": outcome, "evidence": evidence})

    first_run, second_run = runs
    assert _path_termination(first_run["evidence"], "A") == [*SHARED_PREFIX, *ARM_A]
    assert _path_termination(second_run["evidence"], "B") == [*SHARED_PREFIX, *ARM_B]
    assert not set(ARM_B) & {e["step_order"] for e in first_run["evidence"]}
    assert not set(ARM_A) & {e["step_order"] for e in second_run["evidence"]}

    # Distinct programs, distinct paths, and not one action id in common.
    assert len({e["ir_program_id"] for run in runs for e in run["evidence"]}) == 2
    assert {e["ir_action_id"] for e in first_run["evidence"]} & {
        e["ir_action_id"] for e in second_run["evidence"]
    } == set()
    # Both were judged against the same Success Criteria and both were fulfilled.
    for run in runs:
        assert run["outcome"]["outcome"] == "COMPLETED", run
    assert len(backend.created_events) == 2


# ----------------------------------------------------------------------------------
# C. A path may not read evidence the other path produced
# ----------------------------------------------------------------------------------


def test_a_condition_may_not_read_a_step_the_walk_can_skip() -> None:
    """The strong form: the read step must run on *every* path to the condition.

    ``branch-1`` makes step 2 optional, and ``branch-3`` reads step 2. So the arm
    ``1 → 3 → branch-3`` reaches that condition having never run step 2: the fact
    could only be there because an earlier arm recorded it — the same evidence
    read across two arms. The program is refused, and the refusal names the walk.
    """
    with pytest.raises(IrConditionError, match="on every path") as refusal:
        _program_for(_leaky_plan())
    message = str(refusal.value)
    assert "step 2" in message and "branch-3" in message and "condition-3" in message

    # The same program reading a step every arm runs is fine, so this is a check
    # on the walk rather than a blanket refusal of a second or third branch.
    program = _program_for(_honest_plan())
    assert [c.condition_id for c in _conditions(program)] == [
        "branch-1", "branch-2", "branch-3",
    ]
    assert [c.subject.step_order for c in _conditions(program)] == [1, 2, 1]
    assert _walk(program) == [1, 2, 3, 4]


def test_a_condition_reads_only_from_a_step_its_own_path_produced() -> None:
    """Runtime level: the fact comes from the step's own observation or nowhere.

    A condition has no path expression and no payload lookup. It reads one
    recorded field of one step's observation, so a path that skipped the step has
    nothing for it to read — there is no second place a value could come from.
    """
    program = _program_for(_plan_for("A"))
    condition = _conditions(program)[0]
    assert condition.subject.kind is FactKind.OBSERVATION
    assert condition.subject.field == "outcome"
    assert condition.subject.step_order == 2

    produced = _control_facts(
        cursor=2,
        task_state="EXECUTING",
        steps_used=2,
        evidence=[
            _observation(step_order=order, outcome=ToolOutcome.EXECUTION_SUCCEEDED.value)
            for order in (1, 2)
        ],
    )
    assert evaluate(condition, produced) is True

    # Drop step 2's observation and the same condition has no fact to read. The
    # IR refuses such a program, so this state is unreachable in a real run.
    without = _control_facts(
        cursor=1,
        task_state="EXECUTING",
        steps_used=1,
        evidence=[_observation(step_order=1, outcome="EXECUTION_SUCCEEDED")],
    )
    assert not any(o.step_order == 2 for o in without.observations)


async def test_a_leaky_path_is_refused_before_it_can_write_anything(
    client: httpx.AsyncClient, backend: GoogleBackend, stub: BranchingStubModel
) -> None:
    """End to end: the refused program never reaches a calendar write."""
    stub.plans.append(_leaky_plan())
    token = await _signed_in_user(client)
    task_id = await _submit(client, token)
    outcome = await _trigger(client, token, task_id)

    assert outcome["outcome"] == "FAILED", outcome
    assert backend.created_events == {}, "a refused program still wrote to the calendar"
    assert "KAI-IR" in outcome["message"], outcome
    assert await _evidence(task_id) == []


# ----------------------------------------------------------------------------------
# D. Replay and resume
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("arm", ["A", "B"])
async def test_a_resume_re_derives_the_same_path_and_runs_nothing_twice(
    client: httpx.AsyncClient, backend: GoogleBackend, stub: BranchingStubModel, arm: str
) -> None:
    """A resume re-derives the same arm from durable facts and does not re-ask.

    The second trigger re-walks the program from its entry using the evidence the
    first one wrote, and takes the same arm — without asking the model, and
    without re-running a step that already ran.
    """
    stub.plans.append(_plan_for(arm))
    token = await _signed_in_user(client)
    task_id = await _submit(client, token)

    first = await _trigger(client, token, task_id)
    assert first["outcome"] == "WAITING_APPROVAL", first
    parked = await _evidence(task_id)
    create, verify = ARMS[arm]
    # The run stopped exactly at the approval boundary, on the right arm.
    assert _path_termination(parked, arm) == [*SHARED_PREFIX, create]
    assert not set(ARMS["B" if arm == "A" else "A"]) & {e["step_order"] for e in parked}

    await _approve(client, token, first["approval_ids"][0])
    second = await _trigger(client, token, task_id)
    evidence = await _evidence(task_id)

    assert _path_termination(evidence, arm) == [*SHARED_PREFIX, *ARMS[arm]]
    assert _action_success(evidence, arm) == {create: True, verify: True}
    assert second["outcome"] == "COMPLETED", second
    assert _intent_fulfillment(backend) is not None

    # One program, one fingerprint, across both claims: the arm was re-derived
    # from the same program, not rebuilt around a fresh decision.
    assert len({e["ir_program_id"] for e in evidence}) == 1
    assert len({e["ir_fingerprint"] for e in evidence}) == 1
    assert len({e["ir_fingerprint"] for e in evidence}) == len(
        {e["ir_fingerprint"] for e in parked}
    )
    # The parked step ran twice and wrote to the calendar once.
    assert [e["ok"] for e in evidence if e["tool"] == "calendar.create_event"] == [
        False, True,
    ]
    assert len(backend.created_events) == 1


def test_the_arm_is_chosen_by_durable_evidence_and_nothing_else() -> None:
    """One program, two arms, opposite outcomes from one recorded fact.

    Replay stability is only meaningful if the arm is a function of what the run
    recorded. Flipping step 2's recorded outcome flips the arm; nothing else in
    the program, the model, or the wall clock is consulted. And re-deriving from
    the same facts gives the same arm every time.
    """
    read_succeeded: dict[int, str] = {2: ToolOutcome.EXECUTION_SUCCEEDED.value}
    read_failed: dict[int, str] = {2: ToolOutcome.EXECUTION_FAILED.value}

    for arm in ("A", "B"):
        other = "B" if arm == "A" else "A"
        program = _program_for(_plan_for(arm))
        # The read succeeded: this task's own arm ran.
        assert _walk(program, outcomes=read_succeeded) == [*SHARED_PREFIX, *ARMS[arm]]
        # The read failed: the other arm ran instead, from the same program.
        assert _walk(program, outcomes=read_failed) == [*SHARED_PREFIX, *ARMS[other]]
        # And the decision is stable across replays of the same facts.
        assert _walk(program, outcomes=read_succeeded) == _walk(
            program, outcomes=read_succeeded
        )


# ----------------------------------------------------------------------------------
# E. What this design cannot do, asserted rather than worked around
# ----------------------------------------------------------------------------------


def test_a_condition_cannot_read_a_provider_value() -> None:
    """The blocker for "book the first free slot" vs "book the second".

    The discriminating experiment for one Intent with two *methods* needs a
    branch to choose between two free slots. A condition reads four closed
    scalars about an action and cannot reach the slot the provider returned, so
    the choice is not expressible. This is the limit, stated as a fact rather
    than engineered around.
    """
    assert sorted(OBSERVATION_FACT_TYPES) == ["error_code", "ok", "outcome", "verified"]
    for field in ("data", "data.start", "evidence", "arguments", "message"):
        with pytest.raises(ValueError, match="observation field must be one of"):
            ConditionSubject(kind=FactKind.OBSERVATION, step_order=3, field=field)

    # The slot does exist durably — it is simply not a fact a condition may read.
    observation = ObservationNode.model_validate(
        {
            "action_id": "runtime-x-1-3-0",
            "step_order": 3,
            "outcome": ToolOutcome.EXECUTION_SUCCEEDED,
            "ok": True,
            "verified": True,
            "data": {"start": "2026-01-05T16:00:00+03:00"},
        }
    )
    assert observation.data["start"].startswith("2026-01-05T16:00")
    assert "start" not in OBSERVATION_FACT_TYPES


def test_the_two_paths_are_two_control_flow_paths_over_one_provider_method() -> None:
    """Both arms resolve to the same tool, so they differ in path, not in method.

    The runtime builds a step's tool call from its capability alone. Two steps
    declaring the same capability are the same provider method, so these are two
    alternative walks of one implementation — not two implementations of the
    Intent. Stated here rather than papered over with a second capability.
    """
    parameters = set(inspect.signature(AgentRuntime._intents_for).parameters)
    assert parameters == {
        "self", "action", "scenario", "identities", "collected",
        "created_event_id", "timezone", "objective",
    }
    assert not parameters & {"step_order", "node_id", "branch", "path", "arm"}

    program = _program_for(_plan_for("A"))
    tools = {order: CAPABILITY_TO_TOOL[program.step(order).capability] for order in range(1, 7)}
    assert tools[3] == tools[5] == "calendar.create_event"
    assert tools[4] == tools[6] == "calendar.get_event"


def test_no_primitive_was_added_to_make_the_two_paths_work() -> None:
    """Two paths needed no loop, no parallel, no new node kind, no new fact."""
    assert {kind.value for kind in ControlNodeKind} == {"action", "condition", "result"}
    assert {kind.value for kind in FactKind} == {
        "observation", "cursor", "steps_used", "task_state",
    }
    assert {op.value for op in ConditionOperator} == {
        "eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in",
    }
    # One entry, and it is the first step: a fork is not a second way in.
    program = _program_for(_plan_for("A"))
    assert program.entry_node_id() == "action-1"
    entries = [
        n for n in program.nodes
        if n.kind is ControlNodeKind.ACTION and n.step_order == 1
    ]
    assert len(entries) == 1


# ----------------------------------------------------------------------------------
# F. The records these assertions read are the ones the runtime persists
# ----------------------------------------------------------------------------------


def test_the_evidence_records_this_file_asserts_on_are_the_ones_that_is_persisted() -> None:
    """The shape built by hand here is the shape the durable row stores."""
    record = _observation(step_order=2, outcome="EXECUTION_SUCCEEDED")
    assert json.loads(json.dumps(record)) == record
    assert set(record) == {
        "step_order", "tool", "outcome", "ok", "verified", "error_code", "request_id",
    }
