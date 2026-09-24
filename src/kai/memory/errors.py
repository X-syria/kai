"""Memory error vocabulary.

Every error is short, non-sensitive, and safe to surface to a caller; none ever
carries memory content, secrets, or one tenant's scope into another context.
"""

from __future__ import annotations


class MemoryError(ValueError):
    """Base class for all Memory Service errors."""


class MemoryNotFoundError(MemoryError):
    """No memory resolves for the requested ``(tenant, user, id)`` tuple.

    A foreign-tenant or foreign-user memory is deliberately indistinguishable
    from one that never existed, exactly like the other scoped stores in KAI.
    """


class MemoryValidationError(MemoryError):
    """A field was out of range or a required value was missing.

    Values outside ``[0, 1]`` (confidence, importance, recurrence,
    future_relevance) are rejected here — never silently clamped.
    """


class InvalidMemoryTransition(MemoryError):
    """An operation was attempted on a memory in a state that forbids it.

    Covers editing/superseding/deleting a SUPERSEDED or DELETED memory where the
    lifecycle does not allow it.
    """


__all__ = [
    "InvalidMemoryTransition",
    "MemoryError",
    "MemoryNotFoundError",
    "MemoryValidationError",
]