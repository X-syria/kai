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
    WAITING_EXTERNAL,
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
    ("WAITING_EXTERNAL", "REQUIRES_ATTENTION"),
    (VERIFYING, COMPLETED),
    (VERIFYING, "EXECUTING"),
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
        ("WAITING_EXTERNAL", "VERIFYING"),  # no route forward to verification
    ]
    for current, target in illegal:
        decision = resolve_transition(current, target)
        assert not decision.ok, (current, target)
        assert "illegal transition" in (decision.error or "")


def test_waiting_external_does_not_resume_by_re_executing() -> None:
    """The edge whose only meaning was "wait, then drive the step again" is gone.

    ``WAITING_EXTERNAL -> EXECUTING`` existed by symmetry with
    ``EXECUTING -> WAITING_EXTERNAL``, and it said precisely one thing: resume by
    re-driving the step that is waiting. For a task whose provider answer was
    lost, that is the duplicate this machine exists to prevent, and no caller
    needs it \u2014 nothing enters this state at all.

    **Graph reachability is deliberately not what is asserted here, because it is
    not what the architecture guarantees.** ``WAITING_EXTERNAL`` still reaches
    ``EXECUTING`` by way of ``REQUIRES_ATTENTION -> RETRY -> EXECUTING``, and
    that is intended: a retry is an explicit decision to try again, taken by a
    caller rather than by this state machine.

    What actually prevents the duplicate is one layer down and is not a graph
    property: the approval behind a write is single-use, so a re-drive is
    *refused by the gateway* with ``APPROVAL_STALE`` and cannot reach the
    provider. A test that pretended the state graph made re-execution impossible
    would be asserting a protection that does not exist, and would therefore
    pass for the wrong reason.
    """
    assert EXECUTING not in TRANSITIONS[WAITING_EXTERNAL]
    assert resolve_transition(WAITING_EXTERNAL, EXECUTING).ok is False
    # The two exits that remain both stop the task instead of resuming it.
    assert TRANSITIONS[WAITING_EXTERNAL] == frozenset(
        {FAILED, REQUIRES_ATTENTION}
    )


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
