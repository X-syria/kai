"""Unit tests for the explicit Task state machine — no database involved.

The transition map is the single source of truth. These tests pin its shape:
every declared edge is legal, every non-edge is refused, terminal states are
immutable, and unknown states are rejected instead of accepted silently.
"""

from __future__ import annotations

from kai.tasks.state import (
    ALL_STATES,
    COMPLETED,
    CREATED,
    ESCALATE,
    EXECUTING,
    FAILED,
    REQUIRES_ATTENTION,
    RETRY,
    TRANSITIONS,
    VERIFYING,
    apply_retry_budget,
    is_retryable_failure,
    is_terminal,
    resolve_transition,
)

HAPPY_PATH = [
    (CREATED, "UNDERSTANDING"),
    ("UNDERSTANDING", "PLANNING"),
    ("PLANNING", "POLICY_CHECK"),
    ("POLICY_CHECK", "WAITING_APPROVAL"),
    ("POLICY_CHECK", EXECUTING),
    (EXECUTING, "WAITING_APPROVAL"),
    ("WAITING_APPROVAL", EXECUTING),
    ("WAITING_APPROVAL", "POLICY_CHECK"),
    (EXECUTING, "WAITING_EXTERNAL"),
    (EXECUTING, "VERIFYING"),
    ("WAITING_EXTERNAL", EXECUTING),
    (VERIFYING, COMPLETED),
    (VERIFYING, EXECUTING),
]

FAILURE_EDGES = [
    (FAILED, RETRY),
    (FAILED, ESCALATE),
    (FAILED, REQUIRES_ATTENTION),
    (REQUIRES_ATTENTION, RETRY),
    (REQUIRES_ATTENTION, FAILED),
    (RETRY, EXECUTING),
    (RETRY, FAILED),
    (RETRY, ESCALATE),
]


def test_every_declared_edge_is_legal() -> None:
    for current, targets in TRANSITIONS.items():
        for target in targets:
            decision = resolve_transition(current, target)
            assert decision.ok, (current, target)
            assert decision.next_state == target


def test_map_covers_every_state() -> None:
    assert set(TRANSITIONS) == ALL_STATES


def test_happy_path_transitions() -> None:
    for current, target in HAPPY_PATH:
        decision = resolve_transition(current, target)
        assert decision.ok, (current, target)
        assert decision.next_state == target


def test_failure_path_transitions() -> None:
    for current, target in FAILURE_EDGES:
        decision = resolve_transition(current, target)
        assert decision.ok, (current, target)
        assert decision.next_state == target


def test_terminal_states_are_immutable() -> None:
    for terminal in (COMPLETED, ESCALATE):
        assert is_terminal(terminal)
        assert TRANSITIONS[terminal] == frozenset()
        refusals = [s for s in ALL_STATES if resolve_transition(terminal, s).ok]
        assert refusals == []


def test_skipping_states_is_refused() -> None:
    illegal = [
        (CREATED, COMPLETED),            # jump straight to the end
        (CREATED, EXECUTING),            # skip understanding/planning
        ("UNDERSTANDING", "VERIFYING"),  # skip planning/policy/execution
        ("PLANNING", COMPLETED),         # terminal without execution
        ("POLICY_CHECK", "VERIFYING"),   # skip approval/execution
        (EXECUTING, COMPLETED),          # no verification
        ("WAITING_EXTERNAL", "VERIFYING"),  # must resume executing first
    ]
    for current, target in illegal:
        decision = resolve_transition(current, target)
        assert not decision.ok, (current, target)
        assert "illegal transition" in (decision.error or "")


def test_terminal_to_terminal_is_refused() -> None:
    for current, target in ((COMPLETED, ESCALATE), (ESCALATE, COMPLETED)):
        decision = resolve_transition(current, target)
        assert not decision.ok
        assert "terminal" in (decision.error or "")


def test_dead_state_has_no_escape() -> None:
    # COMPLETED has no self-loop and ESCALATE is a dead end.
    assert resolve_transition(COMPLETED, COMPLETED).ok is False
    assert resolve_transition(ESCALATE, ESCALATE).ok is False


def test_unknown_states_are_refused() -> None:
    for current, target in (("NOT_A_STATE", CREATED), (CREATED, "NOT_A_STATE")):
        decision = resolve_transition(current, target)
        assert not decision.ok, (current, target)
        assert "unknown state" in (decision.error or "")


def test_self_transitions_are_not_implicit() -> None:
    for state in ALL_STATES:
        decision = resolve_transition(state, state)
        assert not decision.ok, state


def test_retryable_failure_classification() -> None:
    assert is_retryable_failure(FAILED)
    assert is_retryable_failure(REQUIRES_ATTENTION)
    assert not is_retryable_failure(CREATED)
    assert not is_retryable_failure(EXECUTING)


def test_retry_budget_refuses_when_exhausted() -> None:
    decision = apply_retry_budget(
        current=FAILED, target=RETRY, attempts=2, max_retries=2
    )
    assert not decision.ok
    assert "budget exhausted" in (decision.error or "")


def test_retry_budget_allows_while_remaining() -> None:
    decision = apply_retry_budget(
        current=FAILED, target=RETRY, attempts=1, max_retries=2
    )
    assert decision.ok
    assert decision.next_state == RETRY


def test_retry_budget_does_not_gate_non_retry_transitions() -> None:
    decision = apply_retry_budget(
        current=FAILED, target=ESCALATE, attempts=99, max_retries=2
    )
    assert decision.ok
    assert decision.next_state == ESCALATE


def test_completed_is_terminal_but_not_retryable() -> None:
    assert is_terminal(COMPLETED)
    assert not is_retryable_failure(COMPLETED)
