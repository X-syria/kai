"""PlannerService — the deterministic pipeline of this phase.

    Task (in UNDERSTANDING/PLANNING)
      → build prompt from the Task Contract (user content is DATA, not code)
      → PlannerModel.generate_plan  (raw structured JSON)
      → Pydantic shape validation   (extra fields forbidden)
      → domain_errors               (contract fidelity + budget + ordering)
      → persist a versioned TaskPlan   (old accepted plan → SUPERSEDED)
      → TaskManager.transition → PLANNING

Nothing here executes tools, changes permissions, or touches risk policy. Every
gate leaves the model's output as *data*; an invalid plan is never persisted
and never promoted — instead the task moves to ``FAILED`` through the existing
Task Manager machine.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kai.planner.model import PLANNER_SYSTEM_PROMPT, ModelOutputError, PlannerModel
from kai.planner.models import (
    PLAN_STATUS_ACCEPTED,
    PLAN_STATUS_SUPERSEDED,
    TaskPlan,
)
from kai.planner.schemas import PlanProposal, domain_errors, plan_json
from kai.tasks.models import Task
from kai.tasks.service import get_task, transition_task
from kai.tasks.state import PLANNING, UNDERSTANDING

# Abstract capabilities this vertical slice may plan with. These are labels
# for the Planner's proposal stage, NOT tool calls — nothing executes them.
ABSTRACT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "resolve_contact",
        "read_calendar",
        "propose_times",
        "send_message",
        "create_calendar_event",
        "verify_calendar_event",
    }
)

_PLANNABLE_STATES: frozenset[str] = frozenset({UNDERSTANDING, PLANNING})


class PlanNotAllowedError(ValueError):
    """Raised when the task state does not permit planning."""


class PlanRejectedError(ValueError):
    """Raised when the model output failed structured/domain validation."""


class ModelUnavailableError(ValueError):
    """Raised when the model could not produce output at all."""


class PlanNotFoundError(ValueError):
    """Raised when no persisted plan exists for the requested scope/version."""


def _task_datablocks(task: Task) -> str:
    """Render the Task Contract as delimited data blocks (never instructions)."""
    criteria = "\n".join(f"- {item}" for item in task.success_criteria)
    tools = ", ".join(task.allowed_tools) if task.allowed_tools else "(none)"
    deadline = task.deadline.isoformat() if task.deadline else "none"
    return (
        f"<goal>{task.goal}</goal>\n"
        f"<success_criteria>\n{criteria}\n</success_criteria>\n"
        f"<allowed_tools>{tools}</allowed_tools>\n"
        f"<risk_policy>{task.risk_policy}</risk_policy>\n"
        f"<deadline>{deadline}</deadline>\n"
        f"<max_cost>{task.max_cost}</max_cost>\n"
        f"<max_steps>{task.max_steps}</max_steps>\n"
        f"<verification_method>{task.verification_method}</verification_method>\n"
        f"<fallback>{task.fallback}</fallback>"
    )


class PlannerService:
    def __init__(self, model: PlannerModel) -> None:
        self.model = model

    async def create_plan(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None,
    ) -> TaskPlan:
        task = await get_task(
            session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
        )
        if task.state not in _PLANNABLE_STATES:
            raise PlanNotAllowedError(
                f"planning requires a task in UNDERSTANDING/PLANNING, got {task.state}"
            )

        raw = await self._generate(
            task, session=session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
        )
        proposal = await self._validate(
            task, raw, session=session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
        )
        plan = await self._persist(session, task, proposal)

        if task.state == UNDERSTANDING:
            await transition_task(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                user_id=user_id,
                target=PLANNING,
            )
        return plan

    # --- pipeline gates ----------------------------------------------------

    async def _generate(
        self,
        task: Task,
        *,
        session: AsyncSession,
        task_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None,
    ) -> dict:
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": _task_datablocks(task)},
        ]
        try:
            return await self.model.generate_plan(messages=messages)
        except ModelOutputError as exc:
            await self._fail(
                session, task_id=task_id, tenant_id=tenant_id, user_id=user_id,
                reason="planner model unavailable",
            )
            raise ModelUnavailableError(str(exc)) from exc

    async def _validate(
        self,
        task: Task,
        raw: dict,
        *,
        session: AsyncSession,
        task_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None,
    ) -> PlanProposal:
        try:
            proposal = PlanProposal.model_validate(raw)
        except ValidationError as exc:
            await self._fail(
                session, task_id=task_id, tenant_id=tenant_id, user_id=user_id,
                reason="plan failed structured validation",
            )
            raise PlanRejectedError("plan failed structured validation") from exc

        problems = domain_errors(
            proposal, task=task, allowed_capabilities=ABSTRACT_CAPABILITIES
        )
        if problems:
            await self._fail(
                session, task_id=task_id, tenant_id=tenant_id, user_id=user_id,
                reason=problems[0],
            )
            raise PlanRejectedError(problems[0])
        return proposal

    async def _fail(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None,
        reason: str,
    ) -> None:
        await transition_task(
            session,
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            target="FAILED",
            fail_reason=reason,
        )

    async def _persist(
        self, session: AsyncSession, task: Task, proposal: PlanProposal
    ) -> TaskPlan:
        result = await session.execute(
            select(func.max(TaskPlan.version)).where(
                TaskPlan.tenant_id == task.tenant_id,
                TaskPlan.task_id == task.id,
                TaskPlan.status == PLAN_STATUS_ACCEPTED,
            )
        )
        previous_version = result.scalar_one()
        version = (previous_version or 0) + 1

        await session.execute(
            update(TaskPlan)
            .where(
                TaskPlan.tenant_id == task.tenant_id,
                TaskPlan.task_id == task.id,
                TaskPlan.status == PLAN_STATUS_ACCEPTED,
            )
            .values(status=PLAN_STATUS_SUPERSEDED)
        )
        plan = TaskPlan(
            tenant_id=task.tenant_id,
            task_id=task.id,
            version=version,
            status=PLAN_STATUS_ACCEPTED,
            plan_json=plan_json(proposal),
            model_id=self.model.model_id,
        )
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
        return plan

    # --- retrieval --------------------------------------------------------

    async def get_plan(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None,
        version: int | None = None,
    ) -> TaskPlan:
        task = await get_task(
            session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
        )
        stmt = select(TaskPlan).where(
            TaskPlan.tenant_id == tenant_id, TaskPlan.task_id == task.id
        )
        if version is not None:
            # Audit path: fetch one exact version regardless of current status.
            stmt = stmt.where(TaskPlan.version == version)
        else:
            stmt = stmt.where(TaskPlan.status == PLAN_STATUS_ACCEPTED)
        stmt = stmt.order_by(TaskPlan.version.desc()).limit(1)
        plan = (await session.execute(stmt)).scalar_one_or_none()
        if plan is None:
            raise PlanNotFoundError("no accepted plan exists for this task")
        return plan

    async def list_plans(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None,
    ) -> list[TaskPlan]:
        task = await get_task(
            session, task_id=task_id, tenant_id=tenant_id, user_id=user_id
        )
        result = await session.execute(
            select(TaskPlan)
            .where(TaskPlan.tenant_id == tenant_id, TaskPlan.task_id == task.id)
            .order_by(TaskPlan.version.desc())
        )
        return list(result.scalars().all())