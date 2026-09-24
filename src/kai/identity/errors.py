"""Identity Resolver error vocabulary."""

from __future__ import annotations


class IdentityError(ValueError):
    """Base error for the Identity Resolver domain."""


class IdentityResolutionError(IdentityError):
    """Raised when the resolver cannot produce a verdict (misconfiguration).

    Ordinary "no evidence" inputs are never errors: they resolve to
    ``UNKNOWN``. This is reserved for truly unresolvable resolver state.
    """


__all__ = ["IdentityError", "IdentityResolutionError"]