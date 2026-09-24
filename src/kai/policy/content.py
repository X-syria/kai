"""Deterministic content authorization boundary.

This is *not* an LLM content classifier. ``ScheduleContentAuthorizer`` maps a
content category supplied by the trusted execution context to one of three
verdicts (allow / escalate / block) using the application's closed table from
:mod:`kai.policy.schemas`. When the category is missing or unknown the verdict
is escalation — the boundary never guesses, so it can never silently let
something through.

The message body (``arguments``) is deliberately never read: a message cannot
talk itself in or out of a category. That responsibility belongs to the trusted
context producer (identity/planner layer), not to text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from kai.policy.schemas import CATEGORY_MODE, ContentCategory, ContentMode

if TYPE_CHECKING:
    from kai.tasks.models import Task
    from kai.tools.boundaries import ContentAuthorization
    from kai.tools.context import ToolContext
    from kai.tools.definition import ToolDefinition
    from kai.tools.intent import ToolIntent

#: Metadata key the trusted context uses to carry the content category.
CONTENT_CATEGORY_KEY: str = "content_category"


class ScheduleContentAuthorizer:
    """Decides the *content* verdict from a trusted category, deterministically.

    Structural equivalent of the ``ContentAuthorizer`` boundary; callers may
    wire it directly into the gateway.
    """

    def __init__(self) -> None:
        self.calls: list[ContentAuthorization] = []

    async def authorize(
        self,
        *,
        intent: ToolIntent,
        context: ToolContext,
        tool: ToolDefinition,
        task: Task,
        arguments: Mapping[str, Any],
    ) -> ContentAuthorization:
        category = ContentCategory.from_value(
            context.metadata.get(CONTENT_CATEGORY_KEY)
        )
        decision = self._decide(category)
        self.calls.append(decision)
        return decision

    def _decide(self, category: ContentCategory | None) -> ContentAuthorization:
        from kai.tools.boundaries import ContentAuthorization

        if category is None:
            return ContentAuthorization(
                allowed=False,
                reason="content category is unknown; escalate for human review",
                escalate=True,
            )
        mode = CATEGORY_MODE.get(category)
        if mode is ContentMode.ALLOW:
            return ContentAuthorization(
                allowed=True, reason=f"content category {category.value} is allowed"
            )
        if mode is ContentMode.ESCALATE:
            return ContentAuthorization(
                allowed=False,
                reason=f"content category {category.value} requires human escalation",
                escalate=True,
            )
        return ContentAuthorization(
            allowed=False, reason=f"content category {category.value} is blocked"
        )


__all__ = ["CONTENT_CATEGORY_KEY", "ScheduleContentAuthorizer"]