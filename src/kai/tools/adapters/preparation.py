"""Task preparation tool adapter (deterministic, side-effect-free)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kai.tools.adapters.base import AdapterBase
from kai.tools.context import ToolContext
from kai.tools.execution import ToolExecutionOutcome
from kai.tools.schemas import PrepareTaskArgs


class PreparationAdapter(AdapterBase):
    """Executes the local `task.prepare` tool."""

    async def execute(
        self,
        *,
        tool: Any,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolExecutionOutcome:
        if tool.name != "task.prepare":
            raise self.unimplemented(tool.name)
        self.record("task.prepare")
        args = PrepareTaskArgs.model_validate(arguments)
        objective = args.objective.strip()

        data: dict[str, Any] = {
            "objective": objective,
            "steps": [
                {
                    "order": 1,
                    "title": "Clarify task scope",
                    "description": "Confirm the stated objective and known constraints.",
                },
                {
                    "order": 2,
                    "title": "Identify required inputs",
                    "description": "List concrete information needed to proceed.",
                },
                {
                    "order": 3,
                    "title": "Draft acceptance criteria",
                    "description": "Write a small, testable checklist of success criteria.",
                },
            ],
            "missing_information": [
                "any deadline or time window",
                "stakeholder or recipient details",
                "preferred format or location",
            ],
        }
        evidence: dict[str, Any] = {
            "objective_provided": True,
            "objective": objective,
            "step_count": len(data["steps"]),
            "missing_count": len(data["missing_information"]),
        }
        return ToolExecutionOutcome(
            ok=True,
            verified=True,
            data=data,
            evidence=evidence,
        )


__all__ = ["PreparationAdapter"]
