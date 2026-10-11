"""Pure classification of task transitions into operational-alert kinds.

This module is the alerting *contract* and nothing else: which state-machine
edges owe an external alert and of which kind. It has no transport, no HTTP, no
metrics registry, and no dependency on the dispatcher. That is the whole reason
it exists as its own module.

The durable outbox writes the intent to notify inside the task-state transaction
(see :mod:`kai.alerts.service` and the enqueue seam in
:mod:`kai.tasks.service`). Making that write depend on the module that actually
delivers the alert - which builds an ``httpx`` client and serialises the wire
body - would mean the transition path imports a socket library to record a
database row. Keeping the classification here keeps the atomic enqueue free of
the delivery machinery, and it is the single definition of the contract, so the
delivery path consumes these kinds rather than re-deriving them.

Why the two-kinds decision is load-bearing is argued in
:mod:`kai.observability.alerting`; this module only owns the decision itself.
"""

from __future__ import annotations

from typing import Final

#: Payload discriminator. Bumped only if a field changes meaning; a receiver
#: written against ``v1`` can keep parsing.
ALERT_SCHEMA: Final[str] = "kai.task_failure_alert.v1"

#: The two external alert kinds. A failure and the resolution of that same
#: failure are separate notifications because they ask the operator to do
#: opposite things; one combined "state changed" alert would be actionable
#: neither way.
ALERT_KIND_FAILURE: Final[str] = "task_failure"
ALERT_KIND_RESOLUTION: Final[str] = "task_failure_resolved"


def _unresolved_states() -> frozenset[str]:
    from kai.tasks.state import (
        ESCALATE,
        FAILED,
        REQUIRES_ATTENTION,
        RETRY,
    )

    return frozenset({FAILED, REQUIRES_ATTENTION, RETRY, ESCALATE})


#: States in which a task's failure is unresolved. ``RETRY`` belongs here: a
#: retry is only reachable from a failure and exists only to re-attempt it, so
#: leaving the set on the way to ``RETRY`` would report a resolution that did not
#: happen and then re-open an incident nobody had fixed. ``ESCALATE`` is here for
#: the same reason - escalation hands a failure to a human, it does not resolve
#: it. Resolved from :mod:`kai.tasks.state` rather than re-spelled, so the two
#: cannot drift.
OPEN_FAILURE_STATES: Final[frozenset[str]] = _unresolved_states()


def classify_transition(from_state: str, to_state: str) -> str | None:
    """Return the alert kind for this edge, or ``None`` if it is not an alert.

    ``None`` is the common case and is not an error: most transitions move a task
    along the happy path, and a failure state that merely changes label
    (``FAILED -> REQUIRES_ATTENTION``) is the same incident, not a new one.

    An edge naming a state that does not exist is silent rather than resolved.
    ``ESCALATE`` is in the open set, so an unrecognised target would otherwise be
    read as "the failure is over" - the one conclusion this contract must never
    reach by accident. The state machine refuses unknown states outright; this
    mirrors that rather than inferring health from a value it cannot place.
    """
    from kai.tasks.state import ALL_STATES

    if from_state not in ALL_STATES or to_state not in ALL_STATES:
        return None
    was_open = from_state in OPEN_FAILURE_STATES
    is_open = to_state in OPEN_FAILURE_STATES
    if not was_open and is_open:
        return ALERT_KIND_FAILURE
    if was_open and not is_open:
        return ALERT_KIND_RESOLUTION
    return None


__all__ = [
    "ALERT_KIND_FAILURE",
    "ALERT_KIND_RESOLUTION",
    "ALERT_SCHEMA",
    "OPEN_FAILURE_STATES",
    "classify_transition",
]
