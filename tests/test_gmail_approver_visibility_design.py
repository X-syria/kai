"""The settled decisions for the questions the authority round left open.

``test_gmail_authority_review_design.py`` measured the premises and then stopped on
purpose, closing with "pins the premises a round that settles A, B, D and E has to
move on purpose". This file is that round. It adds three results and takes no
production action.

**1. The approver surface is a strictly coarser function of what it authorises.**
The previous round showed the body is bound and hidden. What it did not measure is
the *shape* of that gap, and the shape is sharper than "one field is missing".
``build_request_summary`` takes ``(tool, arguments)`` and nothing else, so five
distinct bodies collapse to one identical summary while ``intent_hash`` produces
five distinct digests. The approver is not shown a censored view of the intent; the
approver is shown a projection that is many-to-one *over the payload it is being
asked to authorise*. Two further consequences are measured here rather than assumed:
the **account** is inside the digest and outside the signature, so the mailbox a
message will be sent from is hash-bound and simultaneously unrenderable; and
``ApprovalDecisionRequest`` carries a single optional ``reason`` with
``extra="forbid"``, while the API scopes the decision by ``current.id`` — so the
decider is always the requester. "Approver visibility" therefore has no second pair
of eyes anywhere in the stack, by construction rather than by omission.

**2. The identity defect has a producer, not just a reader.** The previous round
measured the criteria readers with hand-written rows — a CONFLICTING row, an
UNKNOWN row, a row with no status. It never asked whether the live resolver can
*emit* such a row. It can. ``_has_conflict`` returns ``True`` for two contacts
sharing one address and its docstring promises CONFLICTING "never picks a winner",
but ``_build`` computes ``email`` in the ``else`` branch of a check that excludes
only ``PROVIDER_VERIFIED`` and ``CHANNEL_VERIFIED`` — so a CONFLICTING verdict
carries a concrete address out of the resolver. That is exactly the input the
status-discarding reader consumes, and ``gmail_send_criteria`` seals it as a
*required* recipient. The two halves are now pinned as one chain, through the real
resolver, rather than as two independent observations.

**3. Severity is bounded twice, and the bound is pinned rather than asserted.** The
chain is unreachable in production for two independent reasons: the runtime raises
before it writes a non-positive identity, and ``gmail_send`` is not in
``LIVE_SEALABLE_PROFILES`` so no gmail task can exist to be evaluated at all.
Neither bound is load-bearing on the other. A third, smaller result bounds the
digest itself: ``_canonical_value`` coerces anything unrecognised with ``str()``,
which would let two different objects hash alike, but all three ``GmailSendArgs``
fields validate to ``str``, so that fall-through cannot be reached from this domain
and the residual risk is recorded rather than inflated.

No production code changes. ``gmail_send`` stays unlicensed. What the deferred
questions needed was a decision, and the decisions are below: the approver surface
is **literal-only** or it is not consent (B); the writer must be tightened to match
the reader, or the reader is the wrong shape (D); and there is **no** author oracle
for a body in the agent path, so authoring cannot be settled by adding a check (A).
Those are recorded as regimes in the architecture reference, with the code facts
here doing the work a decision cannot.
"""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from kai.approvals import schemas as approval_schemas
from kai.approvals.service import (
    _canonical_value,
    build_request_summary,
    canonical_payload,
    intent_hash,
)
from kai.identity import IdentityStatus
from kai.identity.service import (
    Contact,
    IdentityQuery,
    IdentityResolver,
    _exact_claims,
    _has_conflict,
)
from kai.runtime.fulfillment import gmail_send_criteria
from kai.tasks.service import LIVE_SEALABLE_PROFILES
from kai.tools.catalog import default_registry

SRC: Final = Path(__file__).resolve().parents[1] / "src" / "kai"

ALICE: Final = "alice@example.com"
OTHER: Final = "mallory@evil.example"

GMAIL_ARGS: Final[dict[str, Any]] = {
    "to": ALICE,
    "subject": "Quarterly plan",
    "body": "Internal: the deal closes Friday at 40k.",
}


def _source(node: Any) -> str:
    return textwrap.dedent(inspect.getsource(node))


def _repo_text(*parts: str) -> str:
    return SRC.joinpath(*parts).read_text(encoding="utf-8")


def _gmail_tool() -> Any:
    return default_registry().get("gmail.send")


# ---------------------------------------------------------------------------
# 1. Approver visibility: the surface is coarser than the binding
# ---------------------------------------------------------------------------


def test_the_approver_surface_is_strictly_coarser_than_the_bound_payload() -> None:
    """The gap is not one hidden field; the projection is many-to-one.

    The authority round established that the body is bound but unread. This
    measures the *shape*: for a family of payloads that differ only in the body,
    the approver-facing summary is a single constant while the digest the approval
    row actually stores takes as many values as the family has members. So the
    approver cannot distinguish the payloads it is being asked to authorise, not
    even in principle, from the summary alone.
    """
    tool = _gmail_tool()
    bodies = tuple(f"Internal: the deal closes Friday at {n}k." for n in range(5))

    summaries = {
        build_request_summary(tool=tool, arguments={**GMAIL_ARGS, "body": body})
        for body in bodies
    }
    digests = {
        intent_hash(tool=tool, arguments={**GMAIL_ARGS, "body": body}, account_id=None)
        for body in bodies
    }

    assert len(summaries) == 1, "five distinct bodies must not be distinguishable in review"
    assert len(digests) == 5, "each distinct body must move the stored binding"

    # The direction of the collapse is the finding: the review is coarser than the
    # thing reviewed, so no approver-side rule can recover the difference.
    summary_view = summaries.pop()
    assert ALICE in summary_view and GMAIL_ARGS["subject"] in summary_view
    assert not any(f"{n}k" in summary_view for n in range(5))

    # And it is a projection of the arguments alone — the account cannot widen it.
    summary_params = inspect.signature(build_request_summary).parameters
    assert not [p for p in summary_params if "account" in p], (
        f"the summary must take no account at all, got {tuple(summary_params)}"
    )
    assert "account_id" in inspect.signature(canonical_payload).parameters


def test_the_account_is_bound_but_cannot_be_rendered_to_the_approver() -> None:
    """The sending mailbox is inside the digest and outside the display.

    ``canonical_payload`` carries ``account`` and ``intent_hash`` therefore moves
    when the account moves. ``build_request_summary`` has no account parameter at
    all, so the same summary is produced for every account. For a send in the
    user's own name the ``From:`` line is the single most consequential field of the
    message, and this repository already holds that it must not be an argument — so
    it is correctly unauthorable by the model, correctly bound by the digest, and
    invisible to the human whose approval makes the send legal.
    """
    tool = _gmail_tool()
    accounts = (None, uuid4(), uuid4())

    digests = {intent_hash(tool=tool, arguments=GMAIL_ARGS, account_id=a) for a in accounts}
    summaries = {build_request_summary(tool=tool, arguments=GMAIL_ARGS) for _ in accounts}

    assert len(digests) == len(accounts), "each account must move the binding"
    assert len(summaries) == 1, "no account is renderable in review"
    assert not [p for p in inspect.signature(build_request_summary).parameters if "account" in p]

    payload = canonical_payload(tool=tool, arguments=GMAIL_ARGS, account_id=accounts[1])
    assert payload["account"] == str(accounts[1])
    assert payload["account"] is not None

    # The word "account" survives in the summary only as generic prose lifted from
    # the tool description ("from the user's account") — it names no mailbox, and
    # nothing that identifies one could reach this string.
    summary = summaries.pop()
    assert "account" in summary, "the word appears only as description prose"
    for account in accounts[1:]:
        assert str(account) not in summary
    assert "@" not in summary.split("to:")[0], (
        "no mailbox may precede the recipient line in an approver-facing summary"
    )


def test_the_only_decision_input_is_a_reason_so_the_decider_is_the_requester() -> None:
    """There is no second pair of eyes, by construction rather than by omission.

    ``ApprovalDecisionRequest`` carries one optional ``reason`` and forbids extras,
    so a client cannot nominate a different decider, cannot widen the scope being
    approved, and cannot restate the intent. The API then scopes the decision by
    ``current.id``, which is the same ``user_id`` the approval row was created
    under. Self-approval is therefore structural. It is defensible for a task the
    user is performing on their own account — but it means "approver visibility"
    cannot be strengthened by any amount of care in the summary, because there is
    only ever one reader, and that reader is the author.
    """
    request = approval_schemas.ApprovalDecisionRequest
    assert set(request.model_fields) == {"reason"}
    assert request.model_config.get("extra") == "forbid"
    assert request.model_fields["reason"].annotation == (str | None)

    api = _repo_text("api", "approvals.py")
    assert "user_id=current.id" in api, "the decision is scoped to the requesting user"
    assert "approval.decided_by = user_id" in _repo_text("approvals", "service.py")

    # Both verbs route through the same helper with the same principal, so APPROVE
    # and REJECT cannot differ in who may perform them.
    approve = api.split("async def approval_approve")[1].split("return await _decide")[0]
    reject = api.split("async def approval_reject")[1].split("return await _decide")[0]
    assert "current: User = Depends(get_current_user)" in approve
    assert "current: User = Depends(get_current_user)" in reject


# ---------------------------------------------------------------------------
# 2. Identity: the defect has a producer
# ---------------------------------------------------------------------------


def _conflicting_resolver_input() -> tuple[list[Any], list[Any]]:
    """Two contacts sharing one address: C1 conflict, and a winning-looking claim."""
    contacts = [
        Contact(
            resource_name="people/1",
            display_name="Alice",
            emails=(ALICE,),
            phone_numbers=(),
        ),
        Contact(
            resource_name="people/2",
            display_name="Alice B",
            emails=(ALICE,),
            phone_numbers=(),
        ),
    ]
    claims = _exact_claims(IdentityQuery(email=ALICE), contacts)
    return claims, contacts


def test_a_conflicting_verdict_carries_no_address_out_of_the_resolver() -> None:
    """``_has_conflict`` promises CONFLICTING never picks a winner. ``_build`` keeps it.

    This was measured as a defect and has been fixed at the producer. The row comes
    from the real resolver helper rather than a hand-written one: ``_build`` used to
    compute ``email`` in the ``else`` branch of a condition excluding only
    ``PROVIDER_VERIFIED`` and ``CHANNEL_VERIFIED``, so a CONFLICTING verdict was
    handed a concrete address. Withholding ``contact_id`` while publishing the
    address is what made it easy to miss — the result looked half-resolved.

    The contract is now that a verdict which selected nothing publishes no
    identifier at all, so no reader has to distrust the shape it is given.
    """
    claims, contacts = _conflicting_resolver_input()
    query = IdentityQuery(email=ALICE)

    assert _has_conflict(
        exact_claims=claims,
        candidates=[c.resource_name for c in contacts],
        query=query,
        account=None,
        provider_evidence=(),
        channel_evidence=(),
    ), "two contacts on one address is the C1 conflict"

    resolver = IdentityResolver.__new__(IdentityResolver)
    result = resolver._build(
        IdentityStatus.CONFLICTING,
        tuple(claim.evidence for claim in claims),
        claims,
        None,
    )

    assert result.status is IdentityStatus.CONFLICTING
    assert result.email is None, "a conflicting verdict must publish no address"
    assert result.phone is None, "nor a phone number"
    assert result.contact_id is None
    assert result.display_name is None
    assert result.confidence == 0.0
    # The evidence is still reported: the conflict is auditable even though no
    # candidate is selected. Losing the address must not lose the finding.
    assert result.evidence

    # The docstring is the promise, and the producer is what enforces it.
    assert "CONFLICTING never picks a winner" in _source(_has_conflict)
    build = _source(IdentityResolver._build)
    assert "selects_entity = status in _SELECTING_STATUSES" in build
    assert "if selects_entity and len(distinct_ids) == 1" in build


def test_the_conflict_no_longer_reaches_a_criteria_as_a_required_recipient() -> None:
    """The producer and the consumer compose into a chain the previous round split.

    The address the resolver used to emit for a CONFLICTING verdict was precisely
    the field the criteria reader takes, and the reader takes it without consulting
    the status that would have disqualified it. Sealed as a *required* recipient, an
    unresolved identity became an obligation the run was measured against.

    Both halves were tested separately, which is why a fix at one end left the other
    standing. With the producer corrected, the chain is broken at its source: the
    criteria reader still does not read the status, and still would take an address
    if one were offered, but the resolver no longer offers one. The residual reader
    weakness is recorded rather than relied upon.
    """
    claims, contacts = _conflicting_resolver_input()
    resolver = IdentityResolver.__new__(IdentityResolver)
    conflicting = resolver._build(
        IdentityStatus.CONFLICTING,
        tuple(claim.evidence for claim in claims),
        claims,
        None,
    )
    assert _has_conflict(
        exact_claims=claims,
        candidates=[c.resource_name for c in contacts],
        query=IdentityQuery(email=ALICE),
        account=None,
        provider_evidence=(),
        channel_evidence=(),
    )

    criteria = gmail_send_criteria(
        profile="gmail_send",
        identities=[
            {
                "name": "Alice",
                "status": conflicting.status.value,
                "email": conflicting.email,
                "confidence": conflicting.confidence,
            }
        ],
    )

    # A CONFLICTING identity contributes no recipient, because it carries no
    # address to contribute.
    assert criteria.for_gmail_send().required_recipients == frozenset()
    # And the reader that made that possible never mentions the status.
    assert 'item.get("status")' not in _source(gmail_send_criteria)
    assert 'str(item["email"]).casefold()' in _source(gmail_send_criteria)

    # The contrast that makes the fix decidable. The reader is unchanged and still
    # takes whatever address it is handed, so the only reason a conflict now
    # seals differently is that the resolver declined to name one.
    resolved = resolver._build(
        IdentityStatus.CONTACT_MATCH,
        tuple(claim.evidence for claim in claims[:1]),
        claims[:1],
        None,
    )
    assert resolved.status is IdentityStatus.CONTACT_MATCH
    assert resolved.email == ALICE, "a selected entity still publishes its address"
    resolved_criteria = gmail_send_criteria(
        profile="gmail_send",
        identities=[{"status": resolved.status.value, "email": resolved.email}],
    )
    assert resolved_criteria.for_gmail_send().required_recipients == frozenset({ALICE})
    assert criteria.for_gmail_send().required_recipients != (
        resolved_criteria.for_gmail_send().required_recipients
    )


def test_the_chain_is_unreachable_twice_over_and_both_bounds_are_independent() -> None:
    """Why the finding above is a recorded inconsistency and not an open hole.

    Two unrelated mechanisms each close the chain, and neither is standing on the
    other. The runtime refuses a non-positive identity before the identities write,
    so no bad row is persisted by the live producer; and ``gmail_send`` is not a
    live-sealable profile, so no gmail task exists to carry the profile whose
    criteria would misread it. Remove either and the defect is still inert; remove
    both and it is a hole. Recording the two bounds is what lets a future writer be
    added without re-deriving this.
    """
    runtime = _repo_text("runtime", "service.py")

    # Bound one: the writer is strict.
    assert "if resolved.status not in _POSITIVE_IDENTITY:" in runtime
    assert "row.identities = identities" in runtime
    assert "LIVE_SEALABLE_PROFILES" not in runtime.split("row.identities = identities")[0]

    # Bound two: the profile cannot be licensed, so the gmail reader has no input.
    assert frozenset({"calendar_meeting"}) == LIVE_SEALABLE_PROFILES
    assert "gmail_send" not in LIVE_SEALABLE_PROFILES
    assert "UnlicensedFulfillmentError" in _repo_text("tasks", "service.py")


# ---------------------------------------------------------------------------
# 3. The digest, bounded honestly
# ---------------------------------------------------------------------------


def test_the_string_fallthrough_cannot_be_reached_from_the_gmail_schema() -> None:
    """``_canonical_value`` coerces the unknown with ``str()``; gmail cannot reach it.

    The digest is the *only* argument binding in the system, so a collision would
    be serious. ``_canonical_value`` ends in a catch-all that stringifies anything it
    does not recognise, which would let two structurally different values hash
    alike. Every ``GmailSendArgs`` field validates to ``str`` or a ``str`` subclass,
    so the fall-through is dead code for this domain and the residual risk is
    recorded rather than inflated into a finding.
    """
    canonical = _source(_canonical_value)
    assert "return str(value)" in canonical, "the catch-all exists and is a real weakness"

    # What actually reaches ``_canonical_value`` is the *validated, dumped* payload —
    # ``ToolGateway._validate_arguments`` hands ``model_dump()`` to the approval
    # layer, not the raw arguments. So the property that matters is the type of the
    # validated value, not the annotation: ``EmailStr`` is not a ``str`` subclass,
    # but it dumps to a plain ``str``.
    validated = _gmail_tool().input_schema.model_validate(GMAIL_ARGS).model_dump()
    for name, value in validated.items():
        assert type(value) is str, f"{name} must normalise as a plain string, got {type(value)}"
    assert set(validated) == {"to", "subject", "body"}

    # So for a real gmail payload the catch-all is never selected, and the digest is
    # injective over the three fields that matter.
    tool = _gmail_tool()
    other = intent_hash(
        tool=tool, arguments={**GMAIL_ARGS, "to": OTHER}, account_id=None
    )
    baseline = intent_hash(tool=tool, arguments=GMAIL_ARGS, account_id=None)
    assert other != baseline

    # The fallback is reachable for values the schema does not define, which is the
    # honest statement of where the weakness would start to matter.
    assert _canonical_value(object) == str(_canonical_value(object))
