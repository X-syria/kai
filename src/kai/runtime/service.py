"""The Agent Runtime: orchestrates one vertical slice end to end.

``AgentRuntime`` owns the fixed pipeline of the slice:

    submit -> CREATED
    trigger: CREATED -> UNDERSTANDING  (one PlannerModel call -> scenario)
             -> resolve attendee identities (memory hint + Identity Resolver)
             -> PLANNING   (existing PlannerService.create_plan)
             -> POLICY_CHECK -> EXECUTING
             -> WAITING_APPROVAL  (a mid-flight approval checkpoint)
             -> execute plan steps as ToolIntents through the Tool Gateway
             -> VERIFYING -> COMPLETED  (or FAILED / REQUIRES_ATTENTION)

The Task Manager machine is the only writer of ``Task.state``: every step above
goes through :func:`kai.tasks.service.transition_task`, so the task itself
carries the flow (including a real ``WAITING_APPROVAL`` pause) instead of the
runtime reporting a state the task never entered.

Guarantees kept here:

* Every tool execution goes through the Tool Gateway (policy, content,
  approval, verification, account). The runtime never calls the calendar,
  contacts, or any adapter directly.
* All LLM output is data. Understanding and planning are gated on task state so
  a resume never re-runs the model; persistence (``RuntimeExecution``) carries
  scenario + identities + evidence + cursor for deterministic, LLM-free resume.
* The cursor only advances past a step whose intents all succeeded, and
  completed steps are never re-executed — a stopped approval checkpoint or a
  transient network failure can never cause a duplicate calendar insert.
* The cursor counts the steps of exactly one plan: it is bound to a plan
  version and that version's content hash. A plan re-proposed after execution
  started is refused, never silently resumed against the old cursor, and the
  refusal rewinds nothing and re-executes nothing.
* One trigger owns one execution at a time. The per-task claim
  (``RuntimeExecution.claim_token``) is claimed and released in short committed
  transactions and is *never* held across an external provider call, so a
  concurrent or duplicate resume writes nothing at all and can never overwrite
  the owner's result; a claim abandoned by a crashed worker expires.
* A live owner is never replaced. While it works, a heartbeat renews its lease
  on its own short transaction, so a slow model or provider call cannot let the
  lease lapse and hand the task to a second worker; and every step boundary and
  side effect re-proves ownership, so a worker that *did* lose the claim aborts
  before it acts on the task or records anything about it.
* Failure is deterministic: unknown identity, unsupported capability, no free
  slot, a denied/expired approval, or an unverified execution all move the task
  through the existing Task Manager machine.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kai.approvals.models import Approval, ApprovalStatus
from kai.db.engine import async_session_factory
from kai.db.models import GoogleAccount
from kai.identity.models import IdentityStatus
from kai.identity.schemas import IdentityQuery
from kai.identity.service import IdentityResolver
from kai.logging import get_logger
from kai.memory.models import MemoryType
from kai.memory.service import retrieve_memory
from kai.planner.model import PlannerModel
from kai.planner.models import TaskPlan
from kai.planner.schemas import PlanProposal
from kai.planner.service import (
    ModelUnavailableError,
    PlannerService,
    PlanNotAllowedError,
    PlanNotFoundError,
    PlanRejectedError,
)
from kai.runtime.models import RuntimeExecution
from kai.runtime.schemas import GoalSubmission, UnderstandingOutput
from kai.runtime.translator import (
    build_slice_contract,
    candidate_days,
    capability_to_tool,
    content_category_for,
    create_intent_args,
    memory_email_hint,
    parse_free_intervals,
    pick_slot,
    plan_fingerprint,
    read_intent_args,
    request_id_for,
)
from kai.runtime.understanding import UnderstandingFailed, understand_scenario
from kai.tasks.models import Task
from kai.tasks.service import TaskNotFoundError, create_task, get_task, transition_task
from kai.tasks.state import (
    CREATED,
    EXECUTING,
    FAILED,
    PLANNING,
    POLICY_CHECK,
    REQUIRES_ATTENTION,
    UNDERSTANDING,
    VERIFYING,
    WAITING_APPROVAL,
    WAITING_EXTERNAL,
    is_retryable_failure,
    is_terminal,
)
from kai.tools.context import ToolContext
from kai.tools.gateway import ToolGateway
from kai.tools.intent import ToolIntent
from kai.tools.result import ToolOutcome, ToolResult

_RESUMABLE_STATES: frozenset[str] = frozenset(
    {
        CREATED,
        UNDERSTANDING,
        PLANNING,
        POLICY_CHECK,
        EXECUTING,
        WAITING_APPROVAL,
        VERIFYING,
    }
)

#: States where the runtime is blocked on somebody else, so triggering the task
#: again would achieve nothing. ``WAITING_APPROVAL`` is not one of them: an
#: approval that is already settled leaves the task able to move on.
_WAITING_ON_OTHERS: frozenset[str] = frozenset({WAITING_EXTERNAL})

#: How long a trigger's execution claim stays valid before another trigger may
#: take it over. A live trigger finishes in seconds; a claim this old belongs
#: to a worker that died mid-flight, and leaving it forever would strand the
#: task. A live worker never reaches this age: :meth:`AgentRuntime._heartbeat`
#: renews the lease while it works, and this is only the crash-detection window.
_CLAIM_LEASE: Final[timedelta] = timedelta(minutes=5)

#: How often the owner renews its claim lease, on its own session. Comfortably
#: inside :data:`_CLAIM_LEASE` so a few slow or failed renewals cannot expire a
#: live worker's claim, and rare enough to be a rounding error on the database.
_CLAIM_HEARTBEAT_SECONDS: Final[float] = 30.0

_POSITIVE_IDENTITY: frozenset[IdentityStatus] = frozenset(
    {
        IdentityStatus.CONTACT_MATCH,
        IdentityStatus.PROVIDER_VERIFIED,
        IdentityStatus.CHANNEL_VERIFIED,
    }
)


class RuntimeNotFoundError(ValueError):
    """No task resolves for the requested ``(tenant, user, id)``."""


class RuntimeResumeError(ValueError):
    """The task state does not permit a runtime trigger."""


class RuntimeFatal(ValueError):
    """A deterministic stop condition the runtime translates into a failure."""


class ClaimLostError(Exception):
    """The execution claim moved to another worker while this one was running.

    Raised at a fence: the caller has proved it is no longer the owner, so it
    must abandon the trigger without writing anything. It is deliberately not a
    :class:`RuntimeFatal` — a lost claim is never this task's failure to record.
    """


def next_action_for(state: str, *, approval_ids: Sequence[str] = ()) -> str:
    """What the caller can usefully do about a task sitting in ``state``.

    One mapping, derived from the Task Manager machine instead of a second
    hand-written list of "finished" states that could drift away from it:

    * a pending approval outranks everything - a human is the bottleneck;
    * a terminal or failed task is ``DONE``, because the runtime will not touch
      it again and only the caller (retry, escalate) can;
    * a task waiting on a third party is ``WAIT``, because triggering it again
      would achieve nothing;
    * everything else is ``EXECUTE``, which is a call the runtime can honour.
    """
    if approval_ids:
        return "APPROVE"
    if is_terminal(state) or is_retryable_failure(state):
        return "DONE"
    if state in _WAITING_ON_OTHERS:
        return "WAIT"
    return "EXECUTE"


@dataclass(frozen=True)
class TriggerOutcome:
    """What one trigger produced, for the API surface."""

    task_state: str
    outcome: str
    message: str
    approval_ids: list[str]
    steps_used: int


class AgentRuntime:
    """Coordinates the slice pipeline for one task, per trigger."""

    def __init__(
        self,
        *,
        gateway: ToolGateway,
        model: PlannerModel,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        logger: structlog.BoundLogger | None = None,
    ) -> None:
        self.gateway = gateway
        self.model = model
        # The heartbeat needs a session of its own: renewing on the owner's
        # session would queue behind whatever long call the owner is inside.
        self._session_factory = session_factory or async_session_factory
        self._log = logger or get_logger("kai.runtime.service")

    # ------------------------------------------------------------------ #
    # Submit
    # ------------------------------------------------------------------ #

    async def submit(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        submission: GoalSubmission,
    ) -> Task:
        """Create a ``CREATED`` task with the sealed slice contract and its
        execution envelope (timezone validated at the schema boundary)."""
        contract = build_slice_contract(submission.goal)
        task = await create_task(
            session, tenant_id=tenant_id, user_id=user_id, contract=contract
        )
        execution = RuntimeExecution(
            task_id=task.id,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            timezone=submission.timezone.strip(),
            scenario=None,
            identities=[],
            evidence=[],
            cursor=0,
            steps_used=0,
        )
        session.add(execution)
        await session.commit()
        await session.refresh(execution)
        return task

    # ------------------------------------------------------------------ #
    # Trigger
    # ------------------------------------------------------------------ #

    async def trigger(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        task_id: UUID,
        transport: Any,
    ) -> TriggerOutcome:
        """Drive the task as far as it can go in one call.

        Stages are gated on the task's current state; a terminal task is
        idempotent, a failed task refuses a trigger, and a resumed task never
        re-runs the model or re-executes completed steps.

        Exactly one trigger may own a task's execution at a time. Ownership is
        the ``RuntimeExecution`` claim: it is taken in one short committed
        transaction, it is *not* a held transaction or row lock while the
        provider is called, and it is released in a ``finally``. A caller that
        cannot take it writes nothing and returns ``RESUME_IN_PROGRESS``, which
        is what makes a concurrent/duplicate resume deterministic instead of a
        second writer racing the owner into another final state.

        Ownership is kept alive for that whole run by a background heartbeat on
        its own session, and re-proved at every step boundary and before every
        tool call. So the lease only ever lapses for a worker that really died:
        a live slow owner is never taken over, and an owner that *was* taken
        over returns ``CLAIM_LOST`` having written nothing.
        """
        try:
            task = await get_task(
                session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
            )
        except TaskNotFoundError as exc:
            raise RuntimeNotFoundError(str(exc)) from exc

        if is_terminal(task.state):
            return TriggerOutcome(
                task.state,
                "ALREADY_TERMINAL",
                "task already reached a terminal state",
                [],
                0,
            )
        if task.state not in _RESUMABLE_STATES:
            raise RuntimeResumeError(
                f"task state {task.state} is not resumable by the agent runtime"
            )

        row = await self._load_execution(session, task_id=task.id, tenant_id=tenant_id)
        if row is None:
            raise RuntimeResumeError(
                "task was not submitted through the agent runtime"
            )

        claim = await self._claim_execution(
            session, task_id=task.id, tenant_id=tenant_id
        )
        if claim is None:
            return TriggerOutcome(
                task.state,
                "RESUME_IN_PROGRESS",
                "another trigger already owns this task's execution",
                [],
                row.steps_used,
            )
        # Captured before the ``finally`` rollback expires the ORM instances.
        owned_task_id = task.id
        # Renewed in the background for as long as this trigger runs, so a live
        # but slow worker keeps its lease instead of being replaced mid-flight.
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(
                task_id=owned_task_id,
                tenant_id=tenant_id,
                claim=claim,
                stop=heartbeat_stop,
            )
        )
        state_at_start = task.state
        try:
            # The claim is a bulk UPDATE, so the identity-mapped row must be
            # re-read before the pipeline trusts its cursor/evidence. The task
            # is re-read too: a caller that queued behind the owner can only
            # take the claim after the owner finished, and then the persisted
            # state is the only truth.
            await session.refresh(row)
            await session.refresh(task)
            if is_terminal(task.state):
                return TriggerOutcome(
                    task.state,
                    "ALREADY_TERMINAL",
                    "task already reached a terminal state",
                    [],
                    row.steps_used,
                )
            if task.state not in _RESUMABLE_STATES:
                raise RuntimeResumeError(
                    f"task state {task.state} is not resumable by the agent runtime"
                )
            return await self._drive(
                session, task=task, row=row, transport=transport, claim=claim
            )
        except ClaimLostError:
            self._log.warning(
                "execution claim lost to another worker",
                task_id=str(owned_task_id),
                tenant_id=str(tenant_id),
            )
            return await self._lost_claim_outcome(
                task_id=owned_task_id,
                tenant_id=tenant_id,
                fallback_state=state_at_start,
            )
        finally:
            # Discard anything an aborted trigger left uncommitted, stop
            # renewing, then hand ownership back in its own short transaction.
            heartbeat_stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await session.rollback()
            await self._release_execution(
                session, task_id=owned_task_id, tenant_id=tenant_id, claim=claim
            )

    async def _drive(
        self,
        session: AsyncSession,
        *,
        task: Task,
        row: RuntimeExecution,
        transport: Any,
        claim: UUID,
    ) -> TriggerOutcome:
        """Run the owned pipeline for one task (see :meth:`trigger`).

        Every stage below opens with a fence: the first write of the stage, and
        the external call it wraps, only ever happen while this worker is still
        the owner.
        """
        await self._fence_execution(
            session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
        )
        account = await self._resolve_account(session, task)
        if account is None:
            return await self._fail(
                session, task, "no connected google account for this tenant"
            )

        scenario = (
            UnderstandingOutput.model_validate(row.scenario)
            if row.scenario is not None
            else None
        )

        # A task that died between the VERIFYING transition and its outcome is
        # finished here rather than re-run. The verdict is a pure function of
        # the scenario, the identities, and the evidence, all of which are
        # already durable, so the crash window costs one decision and never a
        # re-execution, a re-plan, or a second model call.
        if task.state == VERIFYING:
            if scenario is None:
                return await self._fail(
                    session, task, "cannot verify: no understanding on record"
                )
            return await self._settle_verification(
                session, task=task, row=row, scenario=scenario, claim=claim
            )

        # Stage 1 - understanding (CREATED / UNDERSTANDING only).
        if task.state in {CREATED, UNDERSTANDING}:
            await self._fence_execution(
                session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
            )
            if task.state == CREATED:
                await transition_task(
                    session,
                    task_id=task.id,
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                    target=UNDERSTANDING,
                )
                await session.refresh(task)
            notes = await self._memory_notes(session, task)
            try:
                scenario = await understand_scenario(
                    self.model,
                    goal=task.goal,
                    timezone=row.timezone,
                    memory_notes=notes,
                )
            except UnderstandingFailed as exc:
                return await self._fail(session, task, str(exc))
            row.scenario = scenario.model_dump(mode="json")
            await self._fence_execution(
                session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
            )

        assert scenario is not None

        # Stage 2 - identity, exactly once per task.
        if not row.identities:
            await self._fence_execution(
                session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
            )
            try:
                identities = await self._resolve_guest_identities(
                    session, task, scenario=scenario, account=account, transport=transport
                )
            except RuntimeFatal as exc:
                return await self._fail(session, task, str(exc))
            row.identities = identities
            await self._fence_execution(
                session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
            )

        # Stage 3 - planning (UNDERSTANDING/PLANNING only; resume reuses the row).
        service = PlannerService(self.model)
        try:
            if task.state == UNDERSTANDING:
                await self._fence_execution(
                    session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
                )
                await service.create_plan(
                    session,
                    task_id=task.id,
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                )
                await session.refresh(task)
            plan = await service.get_plan(
                session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
            )
        except PlanNotAllowedError as exc:
            return await self._fail(session, task, f"planning refused: {exc}")
        except PlanRejectedError as exc:
            # PlannerService already moved the task to FAILED itself.
            return TriggerOutcome("FAILED", "FAILED", str(exc), [], row.steps_used)
        except ModelUnavailableError as exc:
            return TriggerOutcome("FAILED", "FAILED", str(exc), [], row.steps_used)
        except PlanNotFoundError as exc:
            return await self._fail(session, task, str(exc))

        # Stage 3b - bind the cursor to the plan it is counting on. The cursor
        # and the persisted scenario/evidence describe *these* steps; a plan
        # that changed underneath them can no longer be resumed from it, and
        # guessing would either skip the new plan's steps or re-run finished
        # ones (a duplicate calendar insert).
        bound, plan_version = await self._bind_plan(session, row, plan, claim)
        if not bound:
            return await self._halt(
                session,
                task,
                _plan_diverged(row.plan_version, plan.version),
            )

        # Stage 4 - promote through the machine. A task parked in
        # WAITING_APPROVAL whose approval is still pending is a no-op: it stays
        # in that real state instead of bouncing through EXECUTING.
        if task.state in {PLANNING, WAITING_APPROVAL, POLICY_CHECK}:
            await self._fence_execution(
                session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
            )
        if task.state == PLANNING:
            await transition_task(
                session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                target=POLICY_CHECK,
            )
            await session.refresh(task)
        if task.state == WAITING_APPROVAL:
            pending = await self.pending_approval_ids(session, task)
            if pending:
                return TriggerOutcome(
                    WAITING_APPROVAL,
                    WAITING_APPROVAL,
                    "human approval is still pending",
                    pending,
                    row.steps_used,
                )
            await transition_task(
                session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                target=EXECUTING,
            )
        if task.state == POLICY_CHECK:
            await transition_task(
                session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                target=EXECUTING,
            )
        await session.refresh(task)

        # Stage 5 - execute (resume-safe).
        proposal = PlanProposal.model_validate(plan.plan_json)
        return await self._execute(
            session,
            task=task,
            row=row,
            scenario=scenario,
            proposal=proposal,
            plan_version=plan_version,
            transport=transport,
            account=account,
            claim=claim,
        )

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    async def _memory_notes(self, session: AsyncSession, task: Task) -> list[str]:
        """Memory as context for the understanding stage — never instructions."""
        scored = await retrieve_memory(
            session,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            memory_types={MemoryType.PERSON, MemoryType.PREFERENCE},
            limit=10,
        )
        return [
            f"{item.entry.memory_type}: {item.entry.content}" for item in scored
        ]

    async def _resolve_guest_identities(
        self,
        session: AsyncSession,
        task: Task,
        *,
        scenario: UnderstandingOutput,
        account: GoogleAccount,
        transport: Any,
    ) -> list[dict[str, Any]]:
        """Establish each attendee through exact identifiers plus live evidence.

        A name alone is never enough; the memory email hint is only a hint. A
        guest that does not resolve to a positive identity fails the whole task
        before any tool runs.
        """
        resolver = IdentityResolver()
        results: list[dict[str, Any]] = []
        for guest in scenario.guests:
            email = await memory_email_hint(
                session,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                name=guest,
            )
            query = IdentityQuery(email=email) if email else IdentityQuery(name=guest)
            context = ToolContext(
                session=session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                account_id=account.id,
                transport=transport,
                identity=query,
            )
            resolved = await resolver.resolve(query=query, context=context)
            if resolved.status not in _POSITIVE_IDENTITY:
                raise RuntimeFatal(f"attendee identity not established: {guest}")
            results.append(
                {
                    "name": guest,
                    "email": resolved.email or email or "",
                    "status": resolved.status.value,
                    "confidence": resolved.confidence,
                }
            )
        return results

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    async def _execute(
        self,
        session: AsyncSession,
        *,
        task: Task,
        row: RuntimeExecution,
        scenario: UnderstandingOutput,
        proposal: PlanProposal,
        plan_version: int,
        transport: Any,
        account: GoogleAccount,
        claim: UUID,
    ) -> TriggerOutcome:
        evidence: list[dict[str, Any]] = list(row.evidence)
        identities: list[dict[str, Any]] = list(row.identities)
        cursor = row.cursor
        steps_used = row.steps_used

        collected = _read_slot_items(evidence)
        created_event_id = _created_event_id(evidence)

        try:
            for step in proposal.steps:
                if step.order <= cursor:
                    # Completed on an earlier trigger. Never re-run: re-running
                    # a finished calendar insert would create a duplicate event.
                    continue
                await self._fence_execution(
                    session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
                )
                intents = self._intents_for(
                    step.action,
                    scenario=scenario,
                    identities=identities,
                    collected=collected,
                    created_event_id=created_event_id,
                    timezone=row.timezone,
                )
                for index, (tool_name, args, identity) in enumerate(intents):
                    steps_used += 1
                    intent = ToolIntent(
                        tool_name=tool_name,
                        arguments=args,
                        task_id=task.id,
                        tenant_id=task.tenant_id,
                        user_id=task.user_id,
                        request_id=request_id_for(
                            task_id=task.id,
                            plan_version=plan_version,
                            step_order=step.order,
                            index=index,
                        ),
                    )
                    context = ToolContext(
                        session=session,
                        task_id=task.id,
                        tenant_id=task.tenant_id,
                        user_id=task.user_id,
                        account_id=account.id,
                        transport=transport,
                        metadata={
                            "steps_used": steps_used,
                            "content_category": content_category_for(tool_name),
                        },
                        identity=identity,
                    )
                    # Last gate before the one call that can change the world:
                    # a worker that lost the claim never reaches the provider.
                    await self._fence_execution(
                        session,
                        task_id=task.id,
                        tenant_id=task.tenant_id,
                        claim=claim,
                    )
                    result = await self.gateway.execute(intent, context)
                    evidence.append(_record(step.order, tool_name, args, result))
                    row.evidence = list(evidence)
                    row.steps_used = steps_used

                    if result.outcome is ToolOutcome.APPROVAL_REQUIRED:
                        await self._fence_execution(
                            session,
                            task_id=task.id,
                            tenant_id=task.tenant_id,
                            claim=claim,
                        )
                        # The task really parks in WAITING_APPROVAL; the runtime
                        # never reports a state the task did not enter.
                        await session.refresh(task)
                        if task.state != WAITING_APPROVAL:
                            await transition_task(
                                session,
                                task_id=task.id,
                                tenant_id=task.tenant_id,
                                user_id=task.user_id,
                                target=WAITING_APPROVAL,
                            )
                        pending = await self.pending_approval_ids(session, task)
                        return TriggerOutcome(
                            WAITING_APPROVAL,
                            "WAITING_APPROVAL",
                            "human approval required to create the calendar event",
                            pending,
                            steps_used,
                        )
                    if (
                        result.outcome is not ToolOutcome.EXECUTION_SUCCEEDED
                        or not result.ok
                    ):
                        message = result.message or f"tool {tool_name} did not succeed"
                        target = (
                            "REQUIRES_ATTENTION"
                            if result.outcome is ToolOutcome.REQUIRES_ATTENTION
                            else "FAILED"
                        )
                        await self._fence_execution(
                            session,
                            task_id=task.id,
                            tenant_id=task.tenant_id,
                            claim=claim,
                        )
                        await transition_task(
                            session,
                            task_id=task.id,
                            tenant_id=task.tenant_id,
                            user_id=task.user_id,
                            target=target,
                            fail_reason=message,
                        )
                        return TriggerOutcome(target, target, message, [], steps_used)

                    if tool_name == "calendar.read_slots":
                        collected.extend(
                            _read_slot_items([_record(step.order, tool_name, args, result)])
                        )
                    elif tool_name == "calendar.create_event":
                        event_id = (result.evidence or {}).get("event_id")
                        if event_id:
                            created_event_id = event_id

                row.cursor = step.order
                row.evidence = list(evidence)
                row.steps_used = steps_used
                await self._fence_execution(
                    session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
                )

            await self._fence_execution(
                session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
            )
            await transition_task(
                session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                target=VERIFYING,
            )
            return await self._settle_verification(
                session, task=task, row=row, scenario=scenario, claim=claim
            )
        except RuntimeFatal as exc:
            return await self._fail(session, task, str(exc))

    async def _settle_verification(
        self,
        session: AsyncSession,
        *,
        task: Task,
        row: RuntimeExecution,
        scenario: UnderstandingOutput,
        claim: UUID,
    ) -> TriggerOutcome:
        """Give a task in ``VERIFYING`` the verdict its evidence already implies.

        Shared by the live execution path and by the recovery path, so a task
        that crashed a moment ago and a task that ran cleanly are decided by
        exactly the same code, on exactly the same inputs.
        """
        await self._fence_execution(
            session, task_id=task.id, tenant_id=task.tenant_id, claim=claim
        )
        if _fulfilled(scenario, list(row.identities), list(row.evidence)):
            await transition_task(
                session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                target="COMPLETED",
            )
            return TriggerOutcome(
                "COMPLETED",
                "COMPLETED",
                "meeting scheduled and verified",
                [],
                row.steps_used,
            )
        return await self._fail(
            session,
            task,
            "verified execution did not satisfy the task success criteria",
            steps_used=row.steps_used,
        )

    def _intents_for(
        self,
        action: str,
        *,
        scenario: UnderstandingOutput,
        identities: list[dict[str, Any]],
        collected: list[tuple[datetime, datetime]],
        created_event_id: str | None,
        timezone: str,
    ) -> list[tuple[str, dict[str, Any], IdentityQuery | None]]:
        tool_name = capability_to_tool(action)
        if tool_name is None:
            raise RuntimeFatal(
                f"capability not supported by the agent runtime slice: {action}"
            )

        if tool_name == "contacts.match":
            query = scenario.guests[0] if scenario.guests else scenario.window.title
            return [(tool_name, {"query": query}, None)]

        if tool_name == "calendar.read_slots":
            days = candidate_days(scenario.window)
            if not days:
                raise RuntimeFatal("no candidate days in the requested window")
            return [
                (
                    tool_name,
                    read_intent_args(
                        day=day, timezone=timezone, earliest_time=scenario.window.earliest_time
                    ),
                    None,
                )
                for day in days
            ]

        if tool_name == "calendar.create_event":
            slot = pick_slot(collected)
            if slot is None:
                raise RuntimeFatal("no free one-hour slot in the requested window")
            emails = [item["email"] for item in identities if item.get("email")]
            if not emails:
                raise RuntimeFatal("no verified attendee address")
            args = create_intent_args(
                slot=slot,
                title=scenario.window.title,
                timezone=timezone,
                attendee_emails=emails,
            )
            queried_identity = IdentityQuery(email=emails[0])
            return [(tool_name, args, queried_identity)]

        if tool_name == "calendar.get_event":
            if not created_event_id:
                raise RuntimeFatal("cannot verify: no created event id on record")
            return [(tool_name, {"event_id": created_event_id}, None)]

        raise RuntimeFatal(
            f"capability not supported by the agent runtime slice: {action}"
        )

    # ------------------------------------------------------------------ #
    # Support
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _load_execution(
        session: AsyncSession, *, task_id: UUID, tenant_id: UUID
    ) -> RuntimeExecution | None:
        result = await session.execute(
            select(RuntimeExecution).where(
                RuntimeExecution.task_id == task_id,
                RuntimeExecution.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _bind_plan(
        session: AsyncSession, row: RuntimeExecution, plan: TaskPlan, claim: UUID
    ) -> tuple[bool, int]:
        """Bind the saved cursor to the plan version it counts, or refuse.

        Returns ``(bound, plan_version)``. ``bound`` is False only when the row
        is already counting a *different* plan and has work on record: the
        cursor then describes steps that no longer exist, and reusing it would
        silently skip the current plan's steps or re-execute finished ones. In
        that case the trigger must stop instead of guessing.

        Re-binding is allowed only while nothing has run yet (no cursor
        progress, no evidence, no step spent), which is the one case where the
        saved state cannot contradict a different plan.
        """
        version = plan.version
        digest = plan_fingerprint(plan.plan_json)
        if row.plan_version == version and row.plan_hash == digest:
            return True, version
        if row.cursor > 0 or row.evidence or row.steps_used > 0:
            return False, version
        row.plan_version = version
        row.plan_hash = digest
        await AgentRuntime._fence_execution(
            session,
            task_id=row.task_id,
            tenant_id=row.tenant_id,
            claim=claim,
        )
        return True, version

    @staticmethod
    async def _claim_execution(
        session: AsyncSession, *, task_id: UUID, tenant_id: UUID
    ) -> UUID | None:
        """Take the per-task execution claim, or return ``None`` if taken.

        One conditional UPDATE decides the winner: a second caller's statement
        blocks on the row lock, then re-reads the committed row and matches
        nothing, so exactly one caller ever owns the execution. The statement is
        committed immediately — nothing is held while the provider is called —
        and a claim older than ``_CLAIM_LEASE`` is treated as abandoned by a
        crashed worker and may be taken over.
        """
        token = uuid4()
        now = datetime.now(UTC)
        result = await session.execute(
            update(RuntimeExecution)
            .where(
                RuntimeExecution.task_id == task_id,
                RuntimeExecution.tenant_id == tenant_id,
                or_(
                    RuntimeExecution.claim_token.is_(None),
                    RuntimeExecution.claimed_at < now - _CLAIM_LEASE,
                ),
            )
            .values(claim_token=token, claimed_at=now)
            .returning(RuntimeExecution.task_id)
        )
        claimed = result.scalar_one_or_none() is not None
        await session.commit()
        return token if claimed else None

    @staticmethod
    async def _renew_claim(
        session: AsyncSession, *, task_id: UUID, tenant_id: UUID, claim: UUID
    ) -> bool:
        """Renew the lease if and only if ``claim`` still owns the row.

        One conditional statement decides it, so the answer is never stale: it
        matches the row only while the stored token is this worker's, and
        matching rows get a fresh ``claimed_at``. The caller commits.
        """
        result = await session.execute(
            update(RuntimeExecution)
            .where(
                RuntimeExecution.task_id == task_id,
                RuntimeExecution.tenant_id == tenant_id,
                RuntimeExecution.claim_token == claim,
            )
            .values(claimed_at=datetime.now(UTC))
            .returning(RuntimeExecution.task_id)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def _fence_execution(
        session: AsyncSession, *, task_id: UUID, tenant_id: UUID, claim: UUID
    ) -> None:
        """Prove this worker still owns the claim, or abandon the trigger.

        The renewal is committed, so a fence that passes both proves ownership
        and buys a fresh :data:`_CLAIM_LEASE` before the caller's next external
        call or write — nobody else can take the claim in that window. A fence
        that fails rolls back first, discarding everything this worker had
        pending, and then raises: by then its cursor, evidence, and state are
        known stale, and the new owner is the only writer that counts.
        """
        if await AgentRuntime._renew_claim(
            session, task_id=task_id, tenant_id=tenant_id, claim=claim
        ):
            await session.commit()
            return
        await session.rollback()
        raise ClaimLostError("the execution claim is no longer owned by this trigger")

    async def _heartbeat(
        self,
        *,
        task_id: UUID,
        tenant_id: UUID,
        claim: UUID,
        stop: asyncio.Event,
    ) -> None:
        """Keep the owner's claim lease fresh for as long as it works.

        Renews on its own short-lived session, never on the owner's, so a slow
        model or a slow provider call cannot serialize the renewal behind the
        very work it is protecting — which is the whole point: the lease has to
        stay young while the owner is inside a call, not only between them.

        Stops on ``stop``, and gives up for good the moment a renewal matches
        nothing: from then on this worker is not the executor, and the owner's
        next fence is what stops it before it writes anything.
        """
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_CLAIM_HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                async with self._session_factory() as beat:
                    if await self._renew_claim(
                        beat, task_id=task_id, tenant_id=tenant_id, claim=claim
                    ):
                        await beat.commit()
                    else:
                        await beat.rollback()
                        self._log.warning(
                            "execution claim heartbeat lost the claim",
                            task_id=str(task_id),
                            tenant_id=str(tenant_id),
                        )
                        return
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError as exc:
                # A renewal that never reached the database proves nothing, and
                # the lease is still valid for the rest of its window. Say so
                # and let the next tick try again rather than dropping the
                # heartbeat on a transient connection failure.
                self._log.warning(
                    "execution claim heartbeat could not renew the lease",
                    task_id=str(task_id),
                    tenant_id=str(tenant_id),
                    error=str(exc),
                )

    async def _lost_claim_outcome(
        self, *, task_id: UUID, tenant_id: UUID, fallback_state: str
    ) -> TriggerOutcome:
        """Report where the task actually is, not what this worker last saw.

        Read through a fresh session because this worker's own session is being
        abandoned: anything uncommitted in it is stale by definition, and the
        owner now driving the task is the only one whose state counts.
        """
        async with self._session_factory() as probe:
            state = await probe.scalar(
                select(Task.state).where(
                    Task.id == task_id, Task.tenant_id == tenant_id
                )
            )
            steps_used = await probe.scalar(
                select(RuntimeExecution.steps_used).where(
                    RuntimeExecution.task_id == task_id,
                    RuntimeExecution.tenant_id == tenant_id,
                )
            )
        return TriggerOutcome(
            state or fallback_state,
            "CLAIM_LOST",
            "this trigger lost the execution claim to another worker; it ran no "
            "step and wrote no state",
            [],
            int(steps_used or 0),
        )

    @staticmethod
    async def _release_execution(
        session: AsyncSession, *, task_id: UUID, tenant_id: UUID, claim: UUID
    ) -> None:
        """Hand the claim back; only the owner can release it."""
        await session.execute(
            update(RuntimeExecution)
            .where(
                RuntimeExecution.task_id == task_id,
                RuntimeExecution.tenant_id == tenant_id,
                RuntimeExecution.claim_token == claim,
            )
            .values(claim_token=None, claimed_at=None)
        )
        await session.commit()

    @staticmethod
    async def _resolve_account(
        session: AsyncSession, task: Task
    ) -> GoogleAccount | None:
        result = await session.execute(
            select(GoogleAccount)
            .where(
                GoogleAccount.tenant_id == task.tenant_id,
                GoogleAccount.user_id == task.user_id,
            )
            .order_by(GoogleAccount.created_at, GoogleAccount.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def pending_approval_ids(
        session: AsyncSession, task: Task
    ) -> list[str]:
        result = await session.execute(
            select(Approval).where(
                Approval.task_id == task.id,
                Approval.tenant_id == task.tenant_id,
                Approval.status == ApprovalStatus.PENDING.value,
            )
        )
        return [str(approval.id) for approval in result.scalars().all()]

    @staticmethod
    async def execution_row(
        session: AsyncSession, *, task_id: UUID, tenant_id: UUID
    ) -> RuntimeExecution | None:
        return await AgentRuntime._load_execution(
            session, task_id=task_id, tenant_id=tenant_id
        )

    @classmethod
    async def _fail(
        cls,
        session: AsyncSession,
        task: Task,
        reason: str,
        *,
        steps_used: int = 0,
    ) -> TriggerOutcome:
        await transition_task(
            session,
            task_id=task.id,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            target=FAILED,
            fail_reason=reason,
        )
        return TriggerOutcome("FAILED", "FAILED", reason, [], steps_used)

    @classmethod
    async def _halt(
        cls,
        session: AsyncSession,
        task: Task,
        reason: str,
    ) -> TriggerOutcome:
        """Stop a task that has run, but cannot continue on its own.

        ``REQUIRES_ATTENTION`` and not ``FAILED``: the work already happened, so
        a human has to decide what the task means now. The saved cursor and its
        evidence are left exactly as they are — nothing is rewound, nothing is
        re-executed.
        """
        await transition_task(
            session,
            task_id=task.id,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            target=REQUIRES_ATTENTION,
            fail_reason=reason,
        )
        return TriggerOutcome("REQUIRES_ATTENTION", "REQUIRES_ATTENTION", reason, [], 0)


def _plan_diverged(bound_version: int | None, current_version: int) -> str:
    """Deterministic, non-sensitive reason for a refused resume."""
    return (
        f"plan changed after execution started (saved cursor is bound to plan v"
        f"{bound_version}, current plan is v{current_version}); the saved cursor "
        "was not reused and no step was executed"
    )


def _record(
    step_order: int,
    tool_name: str,
    args: dict[str, Any],
    result: ToolResult,
) -> dict[str, Any]:
    """One durable, JSON-safe evidence entry for a tool invocation."""
    return {
        "step_order": step_order,
        "tool": tool_name,
        "args": _json_safe(args),
        "outcome": result.outcome.value,
        "ok": bool(result.ok),
        "verified": bool(result.verified),
        "error_code": result.error_code,
        "message": result.message,
        "data": _json_safe(result.data),
        "evidence": _json_safe(dict(result.evidence)),
        "request_id": result.request_id,
    }


def _json_safe(value: Any) -> Any:
    """Coerce a provider payload to JSON-safe values, dropping what is not."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return None


def _read_slot_items(
    evidence: list[dict[str, Any]],
) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    for entry in evidence:
        if entry.get("tool") != "calendar.read_slots":
            continue
        if entry.get("outcome") != "EXECUTION_SUCCEEDED" or not entry.get("ok"):
            continue
        intervals.extend(parse_free_intervals((entry.get("data") or {}).get("free")))
    return intervals


def _created_event_id(evidence: list[dict[str, Any]]) -> str | None:
    for entry in evidence:
        if entry.get("tool") != "calendar.create_event":
            continue
        if entry.get("outcome") != "EXECUTION_SUCCEEDED" or not entry.get("verified"):
            continue
        from_provider = (entry.get("evidence") or {}).get("event_id")
        if isinstance(from_provider, str) and from_provider:
            return from_provider
        from_data = (entry.get("data") or {}).get("event_id")
        if isinstance(from_data, str) and from_data:
            return from_data
    return None


def _fulfilled(
    scenario: UnderstandingOutput,
    identities: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> bool:
    """Deterministic check of the slice's three success criteria."""
    created = [
        entry
        for entry in evidence
        if entry.get("tool") == "calendar.create_event"
        and entry.get("outcome") == "EXECUTION_SUCCEEDED"
        and entry.get("ok")
        and entry.get("verified")
        and (entry.get("evidence") or {}).get("event_id")
    ]
    if not created:
        return False

    args = created[0].get("args") or {}
    attendees = {
        str(address).casefold() for address in (args.get("attendees") or [])
    }
    resolved = {str(item["email"]).casefold() for item in identities if item.get("email")}
    if not resolved or not attendees or not resolved <= attendees:
        return False

    try:
        day = date.fromisoformat(args["date"])
        start_time = time.fromisoformat(args["start_time"])
    except (KeyError, TypeError, ValueError):
        return False

    window = scenario.window
    if not (window.start_date <= day <= window.end_date):
        return False
    return start_time >= window.earliest_time


__all__ = [
    "AgentRuntime",
    "ClaimLostError",
    "RuntimeFatal",
    "RuntimeNotFoundError",
    "RuntimeResumeError",
    "TriggerOutcome",
]
