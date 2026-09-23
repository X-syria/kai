"""FastAPI plumbing for the Planner's model dependency (stub-injectable)."""

from __future__ import annotations

from kai.config import get_settings
from kai.planner.model import OpenAICompatiblePlannerModel, PlannerModel


def get_planner_model() -> PlannerModel:
    """Build the app's single fixed planner model from settings."""
    settings = get_settings()
    return OpenAICompatiblePlannerModel(
        model=settings.planner_model,
        url=settings.planner_model_url,
        api_key=settings.planner_model_api_key,
        timeout=settings.planner_model_timeout_seconds,
    )