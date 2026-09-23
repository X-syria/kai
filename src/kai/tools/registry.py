"""The Tool Registry: the deterministic catalog of executable tools.

Design rules enforced here:

* Tools are registered by the **application** at startup only. The registry
  is sealed after ``build()`` and refuses any further registration, so a
  tool name can never be invented at runtime from LLM input.
* Names must be stable (``^[a-z0-9][a-z0-9._-]*$``); this is the grammar an
  upper layer (planner) is allowed to emit. Everything else is refused with
  ``ToolNotFoundError`` at execution time.
* Duplicate names are a programming error and raise ``DuplicateToolError``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import MappingProxyType
from typing import Any

from kai.tools.definition import ToolDefinition
from kai.tools.errors import (
    DuplicateToolError,
    ToolNotFoundError,
    ToolRegistryLockedError,
)

_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class ToolRegistry:
    """Immutable-after-build lookup of ToolDefinitions by name."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._locked = False

    @classmethod
    def build(cls, definitions: Iterable[ToolDefinition]) -> ToolRegistry:
        """Create a sealed registry from the application's catalog."""
        registry = cls()
        for definition in definitions:
            registry.register(definition)
        registry.lock()
        return registry

    def lock(self) -> None:
        """Seal the registry so no more tools can be registered."""
        self._locked = True

    @property
    def is_locked(self) -> bool:
        return self._locked

    def register(self, definition: ToolDefinition) -> None:
        """Add a tool *only before* the registry is sealed."""
        if self._locked:
            raise ToolRegistryLockedError(
                f"registry is sealed; cannot register tool {definition.name!r}"
            )
        name = definition.name
        if _NAME_RE.fullmatch(name) is None:
            raise ValueError(
                f"invalid tool name {name!r}: allowed pattern is "
                "lowercase alphanumerics, '.', '_', '-' (see registry docs)"
            )
        if name in self._tools:
            raise DuplicateToolError(f"tool {name!r} is already registered")
        if not definition.description.strip():
            raise ValueError(f"tool {name!r} must have a non-empty description")
        self._tools[name] = definition

    def get(self, name: str) -> ToolDefinition:
        """Return a tool by name, refusing unknown/variated names."""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(
                f"unknown tool {name!r}; tools are application-registered only"
            ) from exc

    def get_if_present(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def contains(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def definitions(self) -> MappingProxyType[str, ToolDefinition]:
        return MappingProxyType(dict(self._tools))

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Any:
        return iter(self.names())


__all__ = ["ToolRegistry"]