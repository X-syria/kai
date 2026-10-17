"""Identity semantic integrity and approval binding, measured end to end.

The previous round (``test_gmail_authority_review_design.py``) measured two things
and stopped short of both:

* the status predicate at ``identity/service.py`` counts *claims* while the conflict
  rule one screen earlier counts *entities*, so one contact corroborated on two
  identifiers is labelled ``UNKNOWN``;
* ``gmail_send_criteria`` takes ``item["email"]`` and never reads ``item["status"]``,
  so a non-positive row would still contribute a required recipient.

It called the second one "a measured inconsistency rather than an open hole today"
and specified a fix — require a positive status before taking the address — then
deliberately did not apply it, because applying it would have meant choosing a regime
the round was told not to pick.

**This round picks it, from the repository rather than from taste.** The fix belongs
at the writer, and the writer is already there. Three independent pieces of evidence
say so, and the third is a test the repository already ships:

1. **There is exactly one write.** Across all of ``src/`` and all of ``alembic/`` the
   persisted ``identities`` column is assigned in precisely one statement,
   ``runtime/service.py:527``, and the ``RuntimeExecution`` row is constructed with
   ``identities=[]``. The migration declares the column and writes no data. So
   "which rows exist" is not a question the readers have to answer defensively — it
   is a question with one writer.
2. **That one write is guarded.** ``_resolve_guest_identities`` raises ``RuntimeFatal``
   unless ``resolved.status`` is in ``_POSITIVE_IDENTITY``, and the ``except RuntimeFatal``
   that turns it into a terminal ``_fail`` runs *before* the assignment. A non-positive
   identity cannot be persisted, by construction rather than by convention.
3. **The readers are deliberately projections, and the repository says so.**
   ``test_intent_provenance.py::test_how_an_identity_was_established_is_not_part_of_the_criteria``
   asserts that ``status`` and ``confidence`` must *not* reach the criteria, with the
   docstring "the runtime refuses to run at all unless every guest resolved to a
   positive identity, so by the time a verdict is possible the establishing evidence
   has already done its work". A dozen further tests in ``test_fulfillment.py`` and
   ``test_gmail_fulfillment.py`` pass rows with **no** ``status`` key at all and
   require the address to be sealed. Adding a status filter to the criteria would not
   merely be unrequested — it would contradict the architecture the suite encodes and
   break those tests.

So the reader asymmetry is **correct code in the wrong-looking place**, and the previous
round's proposed fix is the wrong remedy. The genuine residual risk is not a missing
check in three readers; it is that a single statement is load-bearing for a safety
property, with nothing checking that it stays that way. This round pins that one
statement instead of duplicating the rule four times.

The approval half closes the same way, and for the same reason — by enumeration rather
than by assertion. ``_SUMMARY_ARG_FIELDS`` covers all ten of ``calendar.create_event``'s
arguments, and ``calendar.create_event`` is the only MEDIUM tool in the live slice, so
**every argument that can reach an approver on a live path is visible to that approver**.
Two other live tools hide arguments (``day_start``/``day_end``, ``event_id``) and are
therefore never escalated. ``gmail.send`` hides ``body``, is MEDIUM, and is not in the
slice. The invariant holds today as a fact about the field whitelist crossing the risk
threshold — which is not the same as a structural guarantee, and is exactly what breaks
the day Gmail is licensed.

Two things are therefore *not* settled, and this round does not settle them:

* the claim-versus-entity predicate is a genuine semantic defect with a one-line
  remedy, and choosing it is a decision about corroboration (below);
* a body bound into a digest that the approver cannot read is a governance question —
  "show it" versus "literal only" — with no code fact to settle it.

Hence DESIGN-ONLY: no production change, no lifecycle or content change, and
``gmail_send`` stays unlicensed.
"""

from __future__ import annotations

import inspect
import re
import textwrap
import typing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from kai.approvals.service import (
    _SENSITIVE_ARG_FIELDS,
    _SUMMARY_ARG_FIELDS,
    ApprovalService,
    build_request_summary,
    canonical_payload,
    intent_hash,
)
from kai.identity import IdentityQuery, IdentityResolver, IdentityStatus
from kai.runtime.fulfillment import calendar_meeting_criteria, gmail_send_criteria
from kai.runtime.schemas import MeetingWindow
from kai.runtime.service import _POSITIVE_IDENTITY, AgentRuntime, _fulfillment_outcome
from kai.runtime.translator import SLICE_ALLOWED_TOOLS
from kai.tasks.schemas import FulfillmentProfile
from kai.tasks.service import LIVE_SEALABLE_PROFILES
from kai.tools.catalog import default_registry
from tool_harness import (
    GoogleBackend,
    contact_payload,
    insert_account,
    make_context,
    make_task,
    make_tenant_user,
    session_scope,
)

REPO: Final = Path(__file__).resolve().parents[1]
SRC: Final = REPO / "src" / "kai"

OWNER: Final = "owner@corp.example"
ALICE: Final = "alice@example.com"
SECRET_BODY: Final = "Internal: the deal closes Friday at 40k."

GMAIL_ARGS: Final[dict[str, Any]] = {
    "to": ALICE,
    "subject": "Quarterly plan",
    "body": SECRET_BODY,
}


def _source(node: Any) -> str:
    return textwrap.dedent(inspect.getsource(node))


def _python_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.exists():
            out.extend(sorted(root.rglob("*.py")))
    return out


def _future_window() -> MeetingWindow:
    start = date.today() + timedelta(days=3)
    return MeetingWindow(
        start_date=start,
        end_date=start + timedelta(days=1),
        earliest_time="09:00",
        title="KAI",
    )


# ---------------------------------------------------------------------------
# 1. The identity invariant: one writer, guarded, and the readers are projections
# ---------------------------------------------------------------------------


def test_the_persisted_identity_column_has_exactly_one_writer_anywhere_in_the_repository() -> None:
    """Gate item 1, proved by exhaustion rather than by inspection.

    Every assignment to a ``.identities`` attribute in ``src/`` and ``alembic/`` is
    collected by a scanner, not by a grep of one file. There is one, it is in
    ``AgentRuntime._drive``, and it is the assignment the guard precedes. The
    ``RuntimeExecution`` row is created with an empty list, and the migration only
    declares the column — so no seed, fixture, migration, or recovery path can
    introduce a second writer, and "which identity rows exist" has a single answer.
    """
    files = _python_files(SRC, REPO / "alembic")
    assert files, "the scan found no source files, so it proved nothing"

    writes = [
        f"{p.relative_to(REPO).as_posix()}:{n}: {line.strip()}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\.identities\s*=(?!=)", line)
    ]
    assert writes == [
        f"src/kai/runtime/service.py:{_line_of('row.identities = identities')}: "
        "row.identities = identities"
    ], f"expected exactly one writer, found {writes}"

    # The write is the guarded one, and the guard aborts before it.
    drive = _source(AgentRuntime._drive)
    call = drive.index("identities = await self._resolve_guest_identities(")
    failure = drive.index("return await self._fail(session, task, str(exc))", call)
    write = drive.index("row.identities = identities")
    assert call < failure < write

    resolve_guests = _source(AgentRuntime._resolve_guest_identities)
    assert "if resolved.status not in _POSITIVE_IDENTITY:" in resolve_guests
    assert 'raise RuntimeFatal(f"attendee identity not established: {guest}")' in resolve_guests

    # The row starts empty, so a failed grounding leaves nothing to inherit.
    service_text = (SRC / "runtime" / "service.py").read_text(encoding="utf-8")
    assert service_text.count("identities=[],") == 1
    assert "RuntimeExecution(" in service_text

    # And the migration declares the column without seeding it.
    migration = (
        REPO / "alembic" / "versions" / "e9f8a7b6c5d4_add_runtime_executions.py"
    ).read_text(encoding="utf-8")
    assert "'identities'," in migration
    assert "INSERT" not in migration and "UPDATE" not in migration


def _line_of(needle: str) -> int:
    service_text = (SRC / "runtime" / "service.py").read_text(encoding="utf-8")
    for n, line in enumerate(service_text.splitlines(), 1):
        if line.strip() == needle:
            return n
    raise AssertionError(f"{needle!r} not found")


def test_all_three_execution_readers_are_projections_of_that_one_row() -> None:
    """The three consumers the previous round flagged, and where each one gets its data.

    There are two read points and three consumers, and they are all in this module.
    ``_drive`` reads the sealed row once at ``service.py:718`` and threads the list to
    ``_intents_for``, which turns it into the ``attendee_emails`` that reach
    ``calendar.create_event``. ``_settle_verification`` reads it again at
    ``service.py:983`` and threads it to ``_fulfillment_outcome``, which builds both
    criteria from it. Nothing between the column and the consumer re-derives or
    re-validates a status, which is the shape of the invariant: one gate, three
    consumers.
    """
    drive = _source(AgentRuntime._drive)
    assert "if not row.identities:" in drive
    assert "row.identities = identities" in drive

    execute = _source(AgentRuntime._execute)
    assert execute.count("list(row.identities)") == 1
    assert "identities=identities," in execute  # into _intents_for

    settle = _source(AgentRuntime._settle_verification)
    assert settle.count("list(row.identities)") == 1
    assert "identities=identities" in _source(_fulfillment_outcome)

    # The consumers themselves.
    intents_for = _source(AgentRuntime._intents_for)
    assert "attendee_emails" in intents_for
    assert 'emails = [item["email"] for item in identities if item.get("email")]' in intents_for

    outcome = _source(_fulfillment_outcome)
    assert outcome.count("identities=identities,") == 2
    assert "calendar_meeting_criteria(" in outcome
    assert "gmail_send_criteria(" in outcome

    # None of the five re-checks the status. That is the design, and the next test
    # is the repository's own statement of it.
    for source in (drive, execute, settle, intents_for, outcome):
        assert "_POSITIVE_IDENTITY" not in source


def test_the_repository_already_states_that_status_must_not_reach_the_criteria() -> None:
    """The load-bearing citation, and the reason the previous round's fix is withdrawn.

    ``test_intent_provenance.py`` asserts that for every positive status and every
    confidence the criteria JSON is unchanged, and its docstring gives the reason: the
    runtime refuses to run at all unless every guest resolved positively, so by the
    time a verdict is possible the establishing evidence has already done its work.
    Carrying ``status`` into the criteria "would state a condition the evaluator could
    never see".

    That is the architecture. A reader-side status filter would not merely be unasked
    for; it would assert a condition the evaluator provably cannot observe, and the
    suite pins that it must not. The gate belongs at the writer, where it already is.
    """
    provenance = (REPO / "tests" / "test_intent_provenance.py").read_text(encoding="utf-8")
    assert (
        "def test_how_an_identity_was_established_is_not_part_of_the_criteria() -> None:"
        in provenance
    )
    assert "The runtime refuses to run at all unless every guest resolved" in provenance
    assert "status`` and ``confidence`` are a gate, not a success condition" in provenance

    # And the same convention is load-bearing in the fulfilment suites, which pass
    # rows with no status at all and require the address to be sealed.
    fulfillment = (REPO / "tests" / "test_fulfillment.py").read_text(encoding="utf-8")
    assert 'identities=[{"email": "ahmed@example.com"}],' in fulfillment
    assert 'identities=[{"name": "nobody resolved"}]' in fulfillment

    # So the two builders stay pure address projections. A required set is a fact
    # about the sealed row, not a second verdict on identity.
    for builder in (calendar_meeting_criteria, gmail_send_criteria):
        source = _source(builder)
        assert 'item.get("status")' not in source
        assert 'str(item["email"]).casefold() for item in identities if item.get("email")' in source


# ---------------------------------------------------------------------------
# 2. What the invariant does and does not promise
# ---------------------------------------------------------------------------


def test_the_positive_set_is_three_states_and_the_vocabulary_need_not_grow() -> None:
    """Gate item 2, closed: one state is wrong, three is what exists, five is all.

    The positive set is ``CONTACT_MATCH``, ``PROVIDER_VERIFIED`` and
    ``CHANNEL_VERIFIED``; ``UNKNOWN`` and ``CONFLICTING`` are the two negatives. So
    "exactly one state" does not describe the system — three states establish
    identity, and they differ in provenance rather than in authority. Adding
    ``AMBIGUOUS`` would not help: the resolver-to-policy projection is total onto three
    states with three of the five collapsing onto ``VERIFIED``, so any new member
    would land on ``verify``, ``escalate`` or ``deny`` and be a synonym.
    """
    assert frozenset(_POSITIVE_IDENTITY) == frozenset(
        {
            IdentityStatus.CONTACT_MATCH,
            IdentityStatus.PROVIDER_VERIFIED,
            IdentityStatus.CHANNEL_VERIFIED,
        }
    )
    assert set(IdentityStatus) - frozenset(_POSITIVE_IDENTITY) == {
        IdentityStatus.UNKNOWN,
        IdentityStatus.CONFLICTING,
    }
    assert not hasattr(IdentityStatus, "RESOLVED")
    assert not hasattr(IdentityStatus, "AMBIGUOUS")
    assert not hasattr(IdentityStatus, "UNAUTHORIZED")


async def test_same_entity_corroboration_is_grounded_and_distinct_entities_are_not() -> None:
    """Gate items 3 and 4, resolved: the predicate counts entities, as the rule does.

    The question this round was told to stop on has been answered by measurement.
    One contact matched on *both* email and phone is the strongest contact evidence
    available — ``derive_confidence`` rewards corroboration, and the address stays
    populated — yet it failed ``len(exact_claims) == 1`` and fell to ``UNKNOWN`` at
    zero confidence while ``contact_id`` named the entity. Two *different* contacts
    matched one per identifier correctly produced ``CONFLICTING``, because the
    conflict rule counted distinct entities.

    The two counters disagreed, and the disagreement landed on the corroborated
    case. The answer is not a new positive state: the repository's own vocabulary
    says ``CONTACT_MATCH`` is "a trusted identifier (exact email or exact phone)
    appears in the tenant's Google Contacts", which two identifiers on one contact
    satisfy twice over. So the predicate now reads the same set C1 reads, and
    ``status`` can no longer contradict ``contact_id`` on the same result object.
    """
    resolve_source = _source(IdentityResolver.resolve)
    assert "elif len({claim.contact.resource_name for claim in exact_claims}) == 1:" in (
        resolve_source
    )
    assert "len(exact_claims) == 1" not in resolve_source
    assert "distinct_ids = {claim.contact.resource_name for claim in exact_claims}" in _source(
        IdentityResolver._build
    )

    async with session_scope() as session:
        tenant, user = await make_tenant_user(OWNER)
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email=OWNER, sub="sub-1"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)

        one_contact = contact_payload(
            resource_name="people/1", display_name="Alice", emails=(ALICE,), phones=("+15550100",)
        )
        by_email = contact_payload(
            resource_name="people/1", display_name="Alice", emails=(ALICE,), phones=()
        )
        by_phone = contact_payload(
            resource_name="people/2", display_name="Bob", emails=(), phones=("+15550100",)
        )

        async def _resolve(query: IdentityQuery, contacts: list[Any]) -> Any:
            backend = GoogleBackend()
            backend.contacts_connections = contacts
            return await IdentityResolver().resolve(
                query=query,
                context=make_context(
                    session=session,
                    task=task,
                    backend=backend,
                    account_id=account.id,
                    identity=query,
                ),
            )

        both = IdentityQuery(email=ALICE, phone="+15550100")
        corroborated = await _resolve(both, [one_contact])
        distinct = await _resolve(both, [by_email, by_phone])
        single = await _resolve(IdentityQuery(email=ALICE), [one_contact])

        # Case C: one entity, two exact claims. Grounded, and no less trusted than
        # one claim against the same contact.
        assert corroborated.status is IdentityStatus.CONTACT_MATCH
        assert corroborated.email == ALICE
        assert corroborated.contact_id == "people/1"
        assert corroborated.confidence == single.confidence
        # Two exact claims really were made, so this is the corroborated case and
        # not merely a single match.
        assert len([e for e in corroborated.evidence if e.contact_id is not None]) == 2

        # Case B: two entities. Still CONFLICTING, and still not a recipient — the
        # correction did not relax the conflict rule. It also tightened the
        # shape: a verdict that names no entity carries no address either, so
        # a conflict cannot leak outward through a required recipient.
        assert distinct.status is IdentityStatus.CONFLICTING
        assert distinct.email is None
        assert distinct.phone is None
        assert distinct.contact_id is None

        # And one claim on one entity remains positive, which is why the criteria
        # builders can be pure projections.
        assert single.status is IdentityStatus.CONTACT_MATCH
        assert single.contact_id == "people/1"

        # The asymmetry the whole methodology rests on, stated in one comparison.
        assert corroborated.status is not distinct.status


def test_replay_reuses_the_sealed_row_and_cannot_upgrade_its_authority() -> None:
    """Gate item 5: no code path turns a failed identity into a valid recipient.

    ``_drive`` re-resolves identity only while the row is *empty*. A sealed row is
    reused verbatim on replay — the resolver is not re-run, so its status cannot be
    re-decided into a positive one — and a failed grounding never wrote anything, so
    the empty row re-resolves from scratch rather than inheriting partial authority.

    The only way a non-positive status can reach a reader is an out-of-band write to
    the JSON column, by a different version of this code or by hand. That is why the
    single writer is the thing to protect, and why duplicating the rule in three
    readers would not have closed the path anyway — it would only have added three
    places to forget.
    """
    drive = _source(AgentRuntime._drive)
    guard = drive.index("if not row.identities:")
    write = drive.index("row.identities = identities")
    assert guard < write
    assert drive.count("if not row.identities:") == 1
    assert "if row.identities:" not in drive

    # On the replay path the sealed list is read, not recomputed: both read points are
    # `list(row.identities)` and neither calls the resolver.
    assert _source(AgentRuntime._execute).count("list(row.identities)") == 1
    assert _source(AgentRuntime._settle_verification).count("list(row.identities)") == 1
    assert "resolver.resolve(" not in _source(AgentRuntime._settle_verification)
    assert "resolver.resolve(" not in _source(_fulfillment_outcome)

    # And a row that never resolved is re-derived from the resolver, which is the
    # only component that can assign a status.
    resolve_guests = _source(AgentRuntime._resolve_guest_identities)
    assert resolve_guests.count("resolved = await resolver.resolve(") == 1
    assert '"status": resolved.status.value' in resolve_guests


def test_a_populated_email_never_reaches_a_criteria_or_an_intent_without_a_positive_row() -> None:
    """Gate item 1 again, from the reader's side: the email alone decides nothing.

    The builders take the address and nothing else, and the intent builder takes the
    same list. So an email is a *payload* of a positive row, never a source of
    authority in its own right. The corollary is the fragility: because the readers
    carry no status, the entire property rests on the single assignment at
    ``service.py:527`` staying guarded.
    """
    window = _future_window()
    positive: dict[str, Any] = {
        "name": "Alice",
        "email": ALICE,
        "status": IdentityStatus.CONTACT_MATCH.value,
        "confidence": 1.0,
    }
    # Status and confidence are invisible to the criteria, by design.
    for status in sorted(_POSITIVE_IDENTITY, key=lambda s: s.value):
        for confidence in (0.0, 0.5, 1.0):
            variant = {**positive, "status": status.value, "confidence": confidence}
            assert (
                calendar_meeting_criteria(
                    profile="calendar_meeting", window=window, identities=[variant]
                ).for_calendar_meeting().required_attendees
                == frozenset({ALICE})
            )
            assert (
                gmail_send_criteria(profile="gmail_send", identities=[variant])
                .for_gmail_send()
                .required_recipients
                == frozenset({ALICE})
            )

    # An address with no row behind it still reaches the criteria — which is exactly
    # why the guard cannot live here, and exactly what the writer prevents upstream.
    bare: dict[str, Any] = {"name": "Alice", "email": ALICE}
    assert (
        gmail_send_criteria(profile="gmail_send", identities=[bare])
        .for_gmail_send()
        .required_recipients
        == frozenset({ALICE})
    )


# ---------------------------------------------------------------------------
# 3. Approval binding: what is hashed, what is shown, and what can change
# ---------------------------------------------------------------------------


def test_the_approval_binds_the_action_the_account_and_nothing_else() -> None:
    """Gate items 7, 8 and 9: the bound object is narrower than the task.

    ``canonical_payload`` is exactly three keys — tool, canonicalised arguments,
    account. So the token authorises **one invocation of one tool with one argument
    object on one account**. It does not bind the Task, the Contract, the Plan, the
    Evidence or the deadline: those are separate objects with their own hashes, and
    the approval row carries ``task_id`` as a reference rather than as a snapshot. The
    approver is deciding about an action, and the action is what is immutable.
    """
    tool = default_registry().get("gmail.send")
    payload = canonical_payload(tool=tool, arguments=GMAIL_ARGS, account_id=None)
    assert set(payload) == {"tool", "arguments", "account"}
    assert payload["tool"] == "gmail.send"
    assert set(payload["arguments"]) == {"to", "subject", "body"}

    baseline = intent_hash(tool=tool, arguments=GMAIL_ARGS, account_id=None)
    account = uuid4()
    assert baseline != intent_hash(tool=tool, arguments=GMAIL_ARGS, account_id=account)
    assert baseline != intent_hash(
        tool=tool, arguments={**GMAIL_ARGS, "body": SECRET_BODY + " "}, account_id=None
    )
    assert baseline != intent_hash(
        tool=tool, arguments={**GMAIL_ARGS, "subject": "Q4"}, account_id=None
    )

    # The digest is deterministic, so recovery recomputes the identical value.
    assert intent_hash(tool=tool, arguments=GMAIL_ARGS, account_id=None) == baseline

    # And consumption re-derives it from the live arguments and compares, so a changed
    # action cannot spend the approval.
    consume = _source(ApprovalService.consume)
    assert "expected = intent_hash(" in consume
    assert "Approval.intent_hash == expected" in consume
    assert 'raise ApprovalNotFoundError("approval does not match the execution intent")' in consume
    # Atomic single-use: one conditional UPDATE, consumed_at is part of the WHERE.
    assert "Approval.consumed_at.is_(None)" in consume
    assert "Approval.status == ApprovalStatus.APPROVED.value" in consume


def test_the_summary_and_the_digest_are_built_from_the_same_argument_mapping() -> None:
    """What the approver sees is a projection of the thing that was hashed — no more.

    ``build_request_summary`` and ``canonical_payload`` are both called from
    ``request()`` with the same validated ``arguments``, so the summary cannot drift
    from the digest, and every field it does show is inside the digest. The fields it
    omits are still hashed; only the human view is narrower.
    """
    tool = default_registry().get("gmail.send")
    summary = build_request_summary(tool=tool, arguments=GMAIL_ARGS)
    assert ALICE in summary and GMAIL_ARGS["subject"] in summary
    assert SECRET_BODY not in summary and "40k" not in summary

    payload = canonical_payload(tool=tool, arguments=GMAIL_ARGS, account_id=None)
    shown = {line.split(":", 1)[0] for line in summary.splitlines()[1:]}
    assert shown <= set(payload["arguments"])

    # The redaction intent is declared, and the whitelist is disjoint from it, so the
    # two constants cannot contradict each other.
    assert frozenset({"body", "body_html", "content", "message"}) == _SENSITIVE_ARG_FIELDS
    assert set(_SUMMARY_ARG_FIELDS).isdisjoint(_SENSITIVE_ARG_FIELDS)


def test_no_live_approval_eligible_tool_has_an_argument_the_approver_cannot_see() -> None:
    """Gate item 6, settled for the live slice by enumeration.

    Two live tools hide arguments from the summary — ``calendar.read_slots`` hides
    ``day_start``/``day_end`` and ``calendar.get_event`` hides ``event_id`` — and both
    are LOW risk, so neither is ever escalated to a human. ``calendar.create_event`` is
    the only MEDIUM tool in the slice and hides nothing, so on every live path every
    argument that reaches an approver is visible to that approver.

    ``gmail.send`` is MEDIUM and hides ``body``. It is not in the slice, so it cannot
    reach this gateway; that is the only reason the invariant holds, and it is a fact
    about the license rather than a property of the mechanism.
    """
    registry = default_registry()
    hidden_by_tool = {
        name: sorted(set(registry.get(name).input_schema.model_fields) - set(_SUMMARY_ARG_FIELDS))
        for name in SLICE_ALLOWED_TOOLS
    }
    assert hidden_by_tool == {
        "contacts.match": [],
        "calendar.read_slots": ["day_end", "day_start"],
        "calendar.get_event": ["event_id"],
        "calendar.create_event": [],
    }

    escalated = [n for n in SLICE_ALLOWED_TOOLS if registry.get(n).risk_level.value == "MEDIUM"]
    assert escalated == ["calendar.create_event"]
    assert hidden_by_tool["calendar.create_event"] == [], (
        "the only tool a human approves on a live path must have nothing hidden"
    )

    # The two tools that do hide arguments cannot reach a human: LOW risk is never
    # escalated, so a hidden argument there cannot authorise anything.
    for name in ("calendar.read_slots", "calendar.get_event"):
        assert registry.get(name).risk_level.value == "LOW"
        assert hidden_by_tool[name], "the hiding only matters if the tool is escalated"

    # The gap is real, and it is the reason this invariant is not structural.
    assert registry.get("gmail.send").risk_level.value == "MEDIUM"
    assert hidden_by_tool.get("gmail.send", ["body"]) == ["body"]
    assert "gmail.send" not in SLICE_ALLOWED_TOOLS


def test_the_content_authorizer_cannot_stand_in_for_human_visibility() -> None:
    """A passing content gate is not an approver having read anything.

    ``ScheduleContentAuthorizer`` decides on a ``content_category`` string taken from
    ``context.metadata`` and never touches the body. ``content_category_for`` derives
    that string from the tool *name* alone and returns only ``confirm_meeting`` or
    ``propose_time``. So the content check is a constant on the live path and cannot
    notice a body; it is a different question from whether the approver saw one.
    """
    from kai.runtime.translator import content_category_for as derive

    categories = {derive(name) for name in list(SLICE_ALLOWED_TOOLS) + ["gmail.send"]}
    assert categories <= {"confirm_meeting", "propose_time"}

    # The module says so itself: the body is deliberately never read, because a
    # message cannot argue its own category.
    source = (SRC / "policy" / "content.py").read_text(encoding="utf-8")
    assert "is deliberately never read" in source
    for forbidden in ('arguments["body"]', 'arguments.get("body")', '"body" in arguments'):
        assert forbidden not in source, f"the authorizer must not read message content: {forbidden}"

    # And the metadata it consults is populated from the tool name, not from the args.
    service_text = (SRC / "runtime" / "service.py").read_text(encoding="utf-8")
    assert '"content_category": content_category_for(tool_name)' in service_text


# ---------------------------------------------------------------------------
# 4. Licensing: unchanged, and the reason this round changes nothing
# ---------------------------------------------------------------------------


def test_gmail_send_stays_unlicensed_and_the_decision_is_design_only() -> None:
    """The boundary this round is not allowed to move.

    ``LIVE_SEALABLE_PROFILES`` now holds ``calendar_meeting`` and the local,
    provider-free ``task_preparation`` (added in Stage 2E); ``gmail_send`` still
    exists as a fulfilment profile and a tool definition and nowhere as a live path.
    The two defects found this round — the claim predicate and the unread body — are
    left in place, because settling either is a decision, and a round told to stop
    rather than choose stops.
    """
    assert frozenset(LIVE_SEALABLE_PROFILES) == frozenset(
        {"calendar_meeting", "task_preparation"}
    )
    assert typing.get_args(FulfillmentProfile) == (
        "calendar_meeting",
        "gmail_send",
        "task_preparation",
    )
    assert "gmail_send" not in LIVE_SEALABLE_PROFILES

    # No gmail branch was added to the runtime's intent builder.
    intents_for = _source(AgentRuntime._intents_for)
    assert "gmail" not in intents_for

    # And the body is still both hashed and hidden: bound, unread, undecided.
    tool = default_registry().get("gmail.send")
    assert SECRET_BODY in canonical_payload(
        tool=tool, arguments=GMAIL_ARGS, account_id=None
    )["arguments"]["body"]
    assert SECRET_BODY not in build_request_summary(tool=tool, arguments=GMAIL_ARGS)
