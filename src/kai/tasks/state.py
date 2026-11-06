"""Explicit Task state machine (application-level, no LLM involvement).

The machine is the single source of truth for legal transitions. Every
transition is decided here; nothing else — no Planner, no LLM — may move a
task's state. Attempting an illegal transition raises :class:`InvalidTransition`
instead of being accepted silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, cast

# Lifecycle states — the exact KAI v0.5 vocabulary.
CREATED = "CREATED"
UNDERSTANDING = "UNDERSTANDING"
PLANNING = "PLANNING"
POLICY_CHECK = "POLICY_CHECK"
WAITING_APPROVAL = "WAITING_APPROVAL"
EXECUTING = "EXECUTING"
WAITING_EXTERNAL = "WAITING_EXTERNAL"
VERIFYING = "VERIFYING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
RETRY = "RETRY"
ESCALATE = "ESCALATE"
REQUIRES_ATTENTION = "REQUIRES_ATTENTION"

TaskState = Literal[
    "CREATED",
    "UNDERSTANDING",
    "PLANNING",
    "POLICY_CHECK",
    "WAITING_APPROVAL",
    "EXECUTING",
    "WAITING_EXTERNAL",
    "VERIFYING",
    "COMPLETED",
    "FAILED",
    "RETRY",
    "ESCALATE",
    "REQUIRES_ATTENTION",
]

ALL_STATES: Final[frozenset[str]] = frozenset(
    {
        CREATED,
        UNDERSTANDING,
        PLANNING,
        POLICY_CHECK,
        WAITING_APPROVAL,
        EXECUTING,
        WAITING_EXTERNAL,
        VERIFYING,
        COMPLETED,
        FAILED,
        RETRY,
        ESCALATE,
        REQUIRES_ATTENTION,
    }
)

_TERMINAL: Final[frozenset[str]] = frozenset({COMPLETED, ESCALATE})
# A failure only restarts when the retry budget still allows it.
FAILURE_STATES_THAT_RETRY: Final[frozenset[str]] = frozenset(
    {FAILED, REQUIRES_ATTENTION}
)

TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    # --- happy path (KAI v0.5 main line) -----------------------------------
    CREATED: frozenset({UNDERSTANDING, FAILED, REQUIRES_ATTENTION}),
    UNDERSTANDING: frozenset({PLANNING, FAILED, REQUIRES_ATTENTION}),
    PLANNING: frozenset({POLICY_CHECK, UNDERSTANDING, FAILED, REQUIRES_ATTENTION}),
    POLICY_CHECK: frozenset(
        {WAITING_APPROVAL, EXECUTING, PLANNING, FAILED, REQUIRES_ATTENTION}
    ),
    WAITING_APPROVAL: frozenset(
        {POLICY_CHECK, EXECUTING, FAILED, REQUIRES_ATTENTION}
    ),
    # An execution that stops mid-flight for a human decision parks in
    # WAITING_APPROVAL; without this edge the only honest machine state for a
    # paused approval would be to keep pretending the task is EXECUTING.
    EXECUTING: frozenset(
        {
            WAITING_APPROVAL,
            WAITING_EXTERNAL,
            VERIFYING,
            FAILED,
            REQUIRES_ATTENTION,
        }
    ),
    # ``WAITING_EXTERNAL`` deliberately has no edge back to ``EXECUTING``.
    #
    # ``EXECUTING -> WAITING_EXTERNAL`` was the natural way to park a task whose
    # outcome depends on something outside this process, and the return edge was
    # then written by symmetry. That symmetry is the problem: the return edge
    # said exactly one thing \u2014 resume by re-driving the step that is waiting \u2014
    # which for a write whose provider answer was lost is the duplicate this
    # machine exists to prevent. Nothing can wait here today either, so the edge
    # had no reader and only the wrong meaning.
    #
    # What this does *not* do is make ``EXECUTING`` unreachable from here:
    # ``WAITING_EXTERNAL -> REQUIRES_ATTENTION -> RETRY -> EXECUTING`` is a legal
    # route, and it is meant to be. A retry is a deliberate decision by a caller,
    # not something this machine may do on its own. The duplicate is prevented
    # one layer down instead, and not by this graph at all: the approval behind a
    # write is single-use, so any re-drive is refused by the gateway before the
    # provider is reached. A transition table is not where that is enforced, and
    # claiming otherwise here would document a protection that does not exist.
    WAITING_EXTERNAL: frozenset({FAILED, REQUIRES_ATTENTION}),
    VERIFYING: frozenset({COMPLETED, EXECUTING, FAILED, REQUIRES_ATTENTION}),
    COMPLETED: frozenset(),
    # --- failure path (KAI v0.5) -------------------------------------------
    FAILED: frozenset({RETRY, ESCALATE, REQUIRES_ATTENTION}),
    REQUIRES_ATTENTION: frozenset({RETRY, FAILED}),
    RETRY: frozenset({EXECUTING, FAILED, ESCALATE}),
    ESCALATE: frozenset(),
}


def is_terminal(state: str) -> bool:
    """A terminal task can never change state again."""
    return state in _TERMINAL


def is_retryable_failure(state: str) -> bool:
    """True for failure states that may re-enter the lifecycle via ``RETRY``."""
    return state in FAILURE_STATES_THAT_RETRY


def resolve_transition(current: str, target: str) -> TransitionDecision:
    """Ask the machine whether ``current -> target`` is allowed.

    This is the only path that may change a task's state; see
    :func:`kai.tasks.service.transition_task`. Unknown states and illegal
    transitions are refused, never accepted silently.
    """
    if current not in ALL_STATES:
        return TransitionDecision(ok=False, error=f"unknown state: {current!r}")
    if target not in ALL_STATES:
        return TransitionDecision(ok=False, error=f"unknown state: {target!r}")
    if is_terminal(current):
        return TransitionDecision(
            ok=False,
            error=f"terminal state {current} cannot transition",
        )
    allowed = TRANSITIONS[current]
    if target not in allowed:
        return TransitionDecision(
            ok=False,
            error=f"illegal transition {current} -> {target}",
        )
    return TransitionDecision(ok=True, next_state=cast(TaskState, target))


def apply_retry_budget(
    *,
    current: str,
    target: str,
    attempts: int,
    max_retries: int,
) -> TransitionDecision:
    """Enforce ``max_retries`` against the failure -> retry path.

    Re-entering the lifecycle through ``RETRY`` (from either failure state) is
    only allowed while the budget remains; once exhausted, retry is refused and
    the task must escalate instead.
    """
    if target != RETRY or current not in FAILURE_STATES_THAT_RETRY:
        return TransitionDecision(ok=True, next_state=cast(TaskState, target))
    if attempts >= max_retries:
        return TransitionDecision(
            ok=False,
            error=f"retry budget exhausted ({attempts}/{max_retries}); escalate instead",
        )
    return TransitionDecision(ok=True, next_state=cast(TaskState, target))


@dataclass(frozen=True)
class TransitionDecision:
    """Result of a transition attempt.

    ``ok`` is True only when the transition is legal and the retry budget
    permits it. ``error`` is always a short, non-sensitive message intended to
    be returned to the caller; it never contains task content or secrets.
    """

    ok: bool
    next_state: TaskState | None = None
    error: str | None = None


class InvalidTransition(ValueError):
    """Raised when a state change is refused by the machine."""


__all__ = [
    "CREATED",
    "UNDERSTANDING",
    "PLANNING",
    "POLICY_CHECK",
    "WAITING_APPROVAL",
    "EXECUTING",
    "WAITING_EXTERNAL",
    "VERIFYING",
    "COMPLETED",
    "FAILED",
    "RETRY",
    "ESCALATE",
    "REQUIRES_ATTENTION",
    "TaskState",
    "ALL_STATES",
    "TRANSITIONS",
    "TransitionDecision",
    "InvalidTransition",
    "is_terminal",
    "is_retryable_failure",
    "resolve_transition",
    "apply_retry_budget",
]