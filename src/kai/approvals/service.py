"""The real Approval Manager: request -> human decision -> one exact execution.

Pipeline guaranteed here (matches the tool gateway boundary):

* ``check`` is lazy/deterministic: expiration is evaluated at read/evaluation
  time, never only by a background worker, so an expired approval can never be
  treated as approved.
* Exact intent binding: an approval is identified by a SHA-256 of a canonical
  payload (tool name + normalized arguments + account). Any change to a
  security-relevant argument produces a different hash, so an old approval can
  never authorize a changed intent.
* Single use: an APPROVED approval may authorize exactly one execution. The
  next ``check`` for the same exact intent after consumption returns STALE and
  forces a fresh request + fresh human decision.
* Immutability: once terminal (APPROVED / DENIED / EXPIRED) an approval cannot
  be decided again. The only path forward is a brand-new approval.
* Tenant/user isolation: every read and decision is scoped by the trusted
  context (``tenant_id`` + ``user_id``), never by client-supplied values.

Nothing here stores secrets: no tokens, no authorization headers, no message
bodies, no credentials.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kai.approvals.errors import (
    ApprovalNotFoundError,
    ApprovalSuggestionNotFoundError,
    ExpiredApprovalError,
    InvalidApprovalTransition,
    InvalidSuggestionTransition,
)
from kai.approvals.models import Approval, ApprovalRuleSuggestion, ApprovalStatus
from kai.config import get_settings
from kai.tasks.models import Task
from kai.tasks.service import InvalidTransition, transition_task
from kai.tasks.state import WAITING_APPROVAL
from kai.tools.boundaries import (
    ApprovalDecision,
    ApprovalDecisionRecord,
)

if TYPE_CHECKING:
    from kai.tools.context import ToolContext
    from kai.tools.definition import ToolDefinition
    from kai.tools.intent import ToolIntent

#: Argument fields that are never echoed into a human-readable summary or any
#: stored/serialized surface (message bodies, content).
_SENSITIVE_ARG_FIELDS: frozenset[str] = frozenset(
    {"body", "body_html", "content", "message"}
)

#: Keys surfaced in the request summary (safe, non-secret argument fields).
#: A tuple preserves deterministic order in the human-readable preview.
_SUMMARY_ARG_FIELDS: tuple[str, ...] = (
    "to",
    "subject",
    "title",
    "date",
    "start_time",
    "end_time",
    "timezone",
    "attendees",
    "query",
    "recipient",
)


class ApprovalAction(StrEnum):
    """The only two human decisions an approval accepts."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


def canonical_payload(
    *, tool: ToolDefinition, arguments: Mapping[str, Any], account_id: UUID | None
) -> dict[str, Any]:
    """Deterministic canonical payload that defines the *exact* intent.

    Built from the *validated* arguments (post-schema) plus the account the
    action would run through. Dict keys are recursively sorted so two intents
    that normalize to the same validated values hash identically.
    """
    return {
        "tool": tool.name,
        "arguments": _canonical_value(dict(arguments)),
        "account": str(account_id) if account_id is not None else None,
    }


def intent_hash(
    *, tool: ToolDefinition, arguments: Mapping[str, Any], account_id: UUID | None
) -> str:
    """SHA-256 hex digest of :func:`canonical_payload` (the intent binding)."""
    payload = canonical_payload(tool=tool, arguments=arguments, account_id=account_id)
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_request_summary(
    *, tool: ToolDefinition, arguments: Mapping[str, Any]
) -> str:
    """Safe, human-readable preview for the approver.

    Only whitelisted, non-sensitive argument fields are shown; message bodies,
    tokens, and credentials are never included.
    """
    action = tool.description.split(".")[0].strip()
    lines = [action]
    for key in _SUMMARY_ARG_FIELDS:
        if key not in arguments:
            continue
        value = arguments[key]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if isinstance(value, (datetime, date, time)):
            value = value.isoformat()
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


class ApprovalService:
    """Tenant/user-scoped implementation behind the gateway approval boundary."""

    def __init__(
        self,
        *,
        ttl: timedelta | None = None,
        fatigue_threshold: int | None = None,
        fatigue_window: timedelta | None = None,
        fatigue_cooldown: timedelta | None = None,
    ) -> None:
        settings = get_settings()
        self._ttl = ttl or timedelta(minutes=settings.approval_ttl_minutes)
        self._fatigue_threshold = (
            fatigue_threshold or settings.approval_fatigue_threshold
        )
        self._fatigue_window = fatigue_window or timedelta(
            days=settings.approval_fatigue_window_days
        )
        self._fatigue_cooldown = fatigue_cooldown or timedelta(
            days=settings.approval_fatigue_cooldown_days
        )

    # ------------------------------------------------------------------ #
    # Gateway boundary
    # ------------------------------------------------------------------ #

    async def check(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
        now: datetime | None = None,
    ) -> ApprovalDecisionRecord:
        """Evaluate, lazily-expire, and answer for one exact intent.

        Returns a single decision the gateway can act on: REQUIRED (pending,
        stop), DENIED, EXPIRED, STALE (already used), or APPROVED (execution
        may proceed as soon as the gateway re-checks policy and consumes the
        single use).

        Evaluation is against the *latest* matching approval; a settled or
        consumed approval is never silently replaced here, so a denial or an
        exhausted single use stays observable and can never flip back into
        PENDING. A genuinely new request is only created when no approval
        exists for the exact intent yet.
        """
        current = _as_utc(now)
        digest = intent_hash(
            tool=tool,
            arguments=arguments,
            account_id=context.account_id,
        )
        existing = await self._find_intent_request(
            session=context.session,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            task_id=context.task_id,
            tool_name=tool.name,
            intent_hash=digest,
            now=current,
        )

        if existing is None:
            approval = await self.request(
                session=context.session,
                intent=intent,
                context=context,
                tool=tool,
                task=task,
                arguments=arguments,
                now=current,
            )
            return self._record(ApprovalDecision.REQUIRED, approval)
        if existing.status == ApprovalStatus.PENDING.value:
            return self._record(ApprovalDecision.REQUIRED, existing)
        if existing.status == ApprovalStatus.DENIED.value:
            return self._record(ApprovalDecision.DENIED, existing)
        if existing.status == ApprovalStatus.EXPIRED.value:
            return self._record(ApprovalDecision.EXPIRED, existing)
        # APPROVED: single use — at most one execution may proceed.
        if existing.consumed_at is not None:
            return self._record(ApprovalDecision.STALE, existing)
        return self._record(ApprovalDecision.APPROVED, existing)

    async def consume(
        self,
        *,
        approval_id: UUID,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
        now: datetime | None = None,
    ) -> None:
        """Claim the single use of an APPROVED approval, atomically.

        The claim is a single conditional UPDATE whose ``WHERE`` clause
        re-verifies, in one statement, the decision scope, the exact intent
        (hash), the APPROVED status, that it was not already consumed, and that
        it has not expired. With PostgreSQL this is an atomic compare-and-set:
        two concurrent attempts for the *same* approval cannot both win — the
        first ``UPDATE ... WHERE consumed_at IS NULL`` claims the row, the
        second matches zero rows and raises. No check in this method can leave
        the row unconsumed on the "we won" path, and nothing is mutated before
        *all* authorization conditions hold.
        """
        current = _as_utc(now)
        expected = intent_hash(
            tool=tool,
            arguments=arguments,
            account_id=context.account_id,
        )
        result = await context.session.execute(
            update(Approval)
            .where(
                Approval.id == approval_id,
                Approval.tenant_id == context.tenant_id,
                Approval.user_id == context.user_id,
                Approval.status == ApprovalStatus.APPROVED.value,
                Approval.consumed_at.is_(None),
                Approval.expires_at > current,
                Approval.intent_hash == expected,
            )
            .values(consumed_at=current)
            .returning(Approval.id)
        )
        if result.first() is not None:
            await context.session.commit()
            return
        # The atomic claim failed; end the statement's transaction and read the
        # row back to explain *why* without risking a stale identity-map image.
        await context.session.commit()
        approval = await self._load(
            session=context.session,
            approval_id=approval_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        if approval.intent_hash != expected:
            raise ApprovalNotFoundError("approval does not match the execution intent")
        if approval.expires_at <= current:
            raise ExpiredApprovalError("approval expired before it could be used")
        if approval.status != ApprovalStatus.APPROVED.value:
            raise InvalidApprovalTransition(
                f"approval is {approval.status}; only an APPROVED approval can be consumed"
            )
        raise InvalidApprovalTransition(
            "approval was already consumed by another execution"
        )

    # ------------------------------------------------------------------ #
    # Request / read
    # ------------------------------------------------------------------ #

    async def request(
        self,
        *,
        session: AsyncSession,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
        now: datetime | None = None,
    ) -> Approval:
        """Create or return the pending approval for the exact intent.

        Deterministic deduplication: the same ``(tenant, user, task, tool,
        intent_hash)`` still PENDING reuses the existing approval. A settled or
        consumed approval is never reused for a new request — a *different*
        intent or a *settled* intent yields a new approval.
        """
        current = _as_utc(now)
        digest = intent_hash(
            tool=tool,
            arguments=arguments,
            account_id=context.account_id,
        )
        existing = await self._find_intent_request(
            session=session,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            task_id=context.task_id,
            tool_name=tool.name,
            intent_hash=digest,
            now=current,
        )
        if existing is not None and existing.status == ApprovalStatus.PENDING.value:
            return existing
        if (
            existing is not None
            and existing.status == ApprovalStatus.APPROVED.value
            and existing.consumed_at is None
            and existing.expires_at > current
        ):
            # Reuse of the exact approved-unused intent; decided() already
            # moved the task forward, so nothing to do here.
            return existing

        summary = build_request_summary(tool=tool, arguments=arguments)
        recipient_class = _metadata_str(context, "recipient_class")
        approval = Approval(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            task_id=context.task_id,
            tool_name=tool.name,
            risk_level=tool.risk_level.value,
            status=ApprovalStatus.PENDING.value,
            recipient_class=recipient_class,
            intent_hash=digest,
            request_summary=summary,
            expires_at=current + self._ttl,
        )
        session.add(approval)
        try:
            await session.commit()
        except IntegrityError:
            # A concurrent request created the same pending intent; reuse it.
            await session.rollback()
            existing = await self._find_intent_request(
                session=session,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                task_id=context.task_id,
                tool_name=tool.name,
                intent_hash=digest,
                now=current,
            )
            assert existing is not None
            return existing
        await session.refresh(approval)
        # Task state reflects "waiting on a human" whenever the machine allows;
        # the machine is authoritative — an EXECUTING task that cannot move back
        # simply stays EXECUTING and is still blocked by the PENDING decision.
        await self._best_effort_transition(
            session=session,
            task_id=context.task_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            target=WAITING_APPROVAL,
        )
        return approval

    async def _find_intent_request(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        user_id: UUID,
        task_id: UUID,
        tool_name: str,
        intent_hash: str,
        now: datetime,
    ) -> Approval | None:
        stmt = (
            select(Approval)
            .where(
                Approval.tenant_id == tenant_id,
                Approval.user_id == user_id,
                Approval.task_id == task_id,
                Approval.tool_name == tool_name,
                Approval.intent_hash == intent_hash,
            )
            .order_by(Approval.created_at.desc())
        )
        result = await session.execute(stmt)
        approval = result.scalars().first()
        if approval is None:
            return None
        return await self._maybe_expire(session, approval, now)

    async def get(
        self,
        *,
        session: AsyncSession,
        approval_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        now: datetime | None = None,
    ) -> Approval:
        """Fetch one approval scoped to the trusted ``(tenant, user)``.

        A foreign-tenant or foreign-user approval resolves the same as one that
        never existed: :class:`ApprovalNotFoundError`.
        """
        current = _as_utc(now)
        approval = await self._load(session, approval_id, tenant_id, user_id)
        return await self._maybe_expire(session, approval, current)

    async def list_approvals(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        user_id: UUID,
        status: str | None = None,
        now: datetime | None = None,
    ) -> list[Approval]:
        """List approvals for one user, newest first (lazy-expiring each)."""
        current = _as_utc(now)
        stmt = (
            select(Approval)
            .where(Approval.tenant_id == tenant_id, Approval.user_id == user_id)
            .order_by(Approval.created_at.desc())
        )
        if status is not None:
            stmt = stmt.where(Approval.status == status)
        result = await session.execute(stmt)
        approvals = result.scalars().all()
        refreshed: list[Approval] = []
        for approval in approvals:
            refreshed.append(await self._maybe_expire(session, approval, current))
        if refreshed:
            await session.commit()
        return refreshed

    # ------------------------------------------------------------------ #
    # Human decision
    # ------------------------------------------------------------------ #

    async def decide(
        self,
        *,
        session: AsyncSession,
        approval_id: UUID,
        decision: ApprovalAction,
        tenant_id: UUID,
        user_id: UUID,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Approval:
        """Record a human APPROVE/REJECT on one scoped approval.

        Terminal approvals are immutable: DENIED/EXPIRED/APPROVED can never be
        re-decided; changing direction requires a brand-new approval.

        The row is locked for the whole check-decide-write. An approve racing a
        reject is not a harmless last-writer-wins: without the lock both read
        PENDING, both pass the immutability check, and both answer 200 with
        their own answer - while the approval the gateway later reads is
        whichever one happened to commit last. The loser blocks, re-reads, and
        is refused as the immutability check requires.
        """
        current = _as_utc(now)
        approval = await self._load_for_update(
            session, approval_id, tenant_id, user_id
        )
        approval = await self._maybe_expire(session, approval, current)
        if approval.status != ApprovalStatus.PENDING.value:
            if approval.status == ApprovalStatus.EXPIRED.value:
                await session.commit()
                raise ExpiredApprovalError(
                    "approval expired before a decision could be recorded"
                )
            raise InvalidApprovalTransition(
                f"approval is already {approval.status}; terminal approvals are immutable"
            )

        status = (
            ApprovalStatus.APPROVED.value
            if decision is ApprovalAction.APPROVE
            else ApprovalStatus.DENIED.value
        )
        approval.status = status
        approval.decided_at = current
        approval.decided_by = user_id
        approval.decision_reason = _clean(reason)
        await session.commit()
        await session.refresh(approval)

        if decision is ApprovalAction.APPROVE:
            await self._best_effort_transition(
                session=session,
                task_id=approval.task_id,
                tenant_id=approval.tenant_id,
                user_id=approval.user_id,
                target="EXECUTING",
            )
            await self._suggest_rule_if_fatigue(
                session=session,
                tenant_id=approval.tenant_id,
                user_id=approval.user_id,
                tool_name=approval.tool_name,
                risk_level=approval.risk_level,
                recipient_class=approval.recipient_class,
                approved_at=current,
            )
        else:
            await self._best_effort_transition(
                session=session,
                task_id=approval.task_id,
                tenant_id=approval.tenant_id,
                user_id=approval.user_id,
                target="FAILED",
                fail_reason="approval rejected by the user",
            )
        return approval

    # ------------------------------------------------------------------ #
    # Approval fatigue (non-authorizing suggestion)
    # ------------------------------------------------------------------ #

    async def _suggest_rule_if_fatigue(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        user_id: UUID,
        tool_name: str,
        risk_level: str,
        recipient_class: str,
        approved_at: datetime,
    ) -> None:
        """Offer ONE non-authorizing rule suggestion per scope when recent
        approvals cross the threshold. Rejection starts the cooldown.

        Never a permission: the suggestion holds no grant, the gateway never
        consults it, and it can never override Policy.
        """
        recipient_class = recipient_class or ""

        since = approved_at - self._fatigue_window
        count = await _count_approved(
            session=session,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            risk_level=risk_level,
            recipient_class=recipient_class,
            since=since,
        )
        if count < self._fatigue_threshold:
            return

        offered = await session.execute(
            select(ApprovalRuleSuggestion).where(
                ApprovalRuleSuggestion.tenant_id == tenant_id,
                ApprovalRuleSuggestion.user_id == user_id,
                ApprovalRuleSuggestion.tool_name == tool_name,
                ApprovalRuleSuggestion.risk_level == risk_level,
                ApprovalRuleSuggestion.recipient_class == recipient_class,
                ApprovalRuleSuggestion.status == "OFFERED",
                ApprovalRuleSuggestion.expiration > approved_at,
            )
        )
        if offered.scalars().first() is not None:
            return

        recent_rejection = await session.execute(
            select(ApprovalRuleSuggestion).where(
                ApprovalRuleSuggestion.tenant_id == tenant_id,
                ApprovalRuleSuggestion.user_id == user_id,
                ApprovalRuleSuggestion.tool_name == tool_name,
                ApprovalRuleSuggestion.risk_level == risk_level,
                ApprovalRuleSuggestion.recipient_class == recipient_class,
                ApprovalRuleSuggestion.status == "REJECTED",
            )
        )
        latest_rejected = recent_rejection.scalars().first()
        if (
            latest_rejected is not None
            and latest_rejected.rejected_at is not None
            and latest_rejected.rejected_at + self._fatigue_cooldown > approved_at
        ):
            return  # cooldown still running — do not re-offer.

        suggestion = ApprovalRuleSuggestion(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            risk_level=risk_level,
            recipient_class=recipient_class,
            allowed_tools=[tool_name],
            allowed_recipients=[],
            risk_ceiling=risk_level,
            expiration=approved_at
            + timedelta(days=get_settings().approval_suggestion_expiration_days),
            status="OFFERED",
        )
        session.add(suggestion)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # a concurrent offer won already

    async def list_suggestions(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        user_id: UUID,
    ) -> list[ApprovalRuleSuggestion]:
        result = await session.execute(
            select(ApprovalRuleSuggestion)
            .where(
                ApprovalRuleSuggestion.tenant_id == tenant_id,
                ApprovalRuleSuggestion.user_id == user_id,
            )
            .order_by(ApprovalRuleSuggestion.created_at.desc())
        )
        return list(result.scalars().all())

    async def reject_suggestion(
        self,
        *,
        session: AsyncSession,
        suggestion_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRuleSuggestion:
        current = _as_utc(now)
        result = await session.execute(
            select(ApprovalRuleSuggestion).where(
                ApprovalRuleSuggestion.id == suggestion_id,
                ApprovalRuleSuggestion.tenant_id == tenant_id,
                ApprovalRuleSuggestion.user_id == user_id,
            )
        )
        suggestion = result.scalar_one_or_none()
        if suggestion is None:
            raise ApprovalSuggestionNotFoundError(
                "suggestion not found for this tenant/user"
            )
        if suggestion.status != "OFFERED":
            raise InvalidSuggestionTransition(
                f"suggestion is already {suggestion.status}"
            )
        suggestion.status = "REJECTED"
        suggestion.rejected_at = current
        suggestion.reject_reason = _clean(reason)
        await session.commit()
        await session.refresh(suggestion)
        return suggestion

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _record(decision: ApprovalDecision, approval: Approval) -> ApprovalDecisionRecord:
        """Build the gateway boundary verdict for one approval row."""
        return ApprovalDecisionRecord(
            decision=decision,
            reason=f"approval {decision.value.lower()}",
            decided_by=(
                str(approval.decided_by) if approval.decided_by is not None else "system"
            ),
            approval_id=approval.id,
        )

    async def _load(
        self,
        session: AsyncSession,
        approval_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> Approval:
        result = await session.execute(
            select(Approval).where(
                Approval.id == approval_id,
                Approval.tenant_id == tenant_id,
                Approval.user_id == user_id,
            )
        )
        approval = result.scalar_one_or_none()
        if approval is None:
            raise ApprovalNotFoundError("approval not found for tenant")
        return approval

    async def _load_for_update(
        self,
        session: AsyncSession,
        approval_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> Approval:
        """Lock the approval row and read it as it is right now.

        The deciding session may have read this approval earlier in the same
        request, so ``populate_existing`` makes sure the lock hands back the
        row's real state rather than the session's cached image of it.
        """
        result = await session.execute(
            select(Approval)
            .where(
                Approval.id == approval_id,
                Approval.tenant_id == tenant_id,
                Approval.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        approval = result.scalar_one_or_none()
        if approval is None:
            raise ApprovalNotFoundError("approval not found for tenant")
        return approval

    async def _maybe_expire(
        self, session: AsyncSession, approval: Approval, now: datetime
    ) -> Approval:
        """Lazy, deterministic expiration: PENDING/APPROVED past ``expires_at``
        become EXPIRED at read/evaluation time."""
        if (
            approval.status
            in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
            and approval.expires_at <= now
        ):
            approval.status = ApprovalStatus.EXPIRED.value
            approval.decided_at = now
            approval.decision_reason = "approval expired"
        return approval

    async def _best_effort_transition(
        self,
        *,
        session: AsyncSession,
        task_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        target: str,
        fail_reason: str | None = None,
    ) -> None:
        """Ask the Task Manager's machine; never overrule it.

        The machine is the single source of truth for legal transitions; if a
        transition is not legal from the current state we leave the task
        untouched instead of inventing a new one.
        """
        with suppress(InvalidTransition):
            await transition_task(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                target=target,
                fail_reason=fail_reason,
            )


async def _count_approved(
    *,
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    tool_name: str,
    risk_level: str,
    recipient_class: str,
    since: datetime,
) -> int:
    from sqlalchemy import func

    result = await session.execute(
        select(func.count(Approval.id)).where(
            Approval.tenant_id == tenant_id,
            Approval.user_id == user_id,
            Approval.tool_name == tool_name,
            Approval.risk_level == risk_level,
            Approval.recipient_class == recipient_class,
            Approval.status == ApprovalStatus.APPROVED.value,
            Approval.decided_at >= since,
        )
    )
    return int(result.scalar_one())


def _canonical_value(value: Any) -> Any:
    """Normalize a value for stable hashing (recursive, deterministic)."""
    if isinstance(value, dict):
        return {
            key: _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _clean(reason: str | None) -> str | None:
    if reason is None:
        return None
    stripped = " ".join(reason.split()).strip()
    return stripped if stripped else None


def _as_utc(now: datetime | None) -> datetime:
    if now is not None:
        return now
    return datetime.now(UTC)


def _metadata_str(context: ToolContext, key: str) -> str:
    value = context.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()[:64]
    return ""


__all__ = [
    "ApprovalAction",
    "ApprovalService",
    "build_request_summary",
    "canonical_payload",
    "intent_hash",
]