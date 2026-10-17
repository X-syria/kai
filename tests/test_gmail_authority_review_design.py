"""The upstream authority questions for a ``gmail_send``, measured rather than argued.

The three previous rounds each produced a finding and each stopped short of the
question above them:

* round two fixed the *recipient* half — a sealed value must be an address, and it
  must occur literally in the user's goal (``test_sealed_recipient_intent_design.py``);
* round three asked for the whole contract and found the licence is sealed at
  submission, before any intent exists (``test_gmail_intent_contract_design.py``).

This round asks what sits above all of them — **who may author a value KAI sends in
the user's name, where the sealing boundary actually falls, and what happens when
grounding fails or is ambiguous** — and it produces three results, two of which
correct the previous rounds.

**1. The only live send path in the repository is the authenticated client.**
``send_and_verify`` has exactly two call sites: ``api/gmail.py:195`` and
``tools/adapters/gmail.py:159``. The first calls the Gmail service directly — no
``ToolGateway``, no ``PolicyEngine``, no ``ApprovalService``, no ``Task``, no
``RuntimeExecution``, no evidence, no evaluator. Every one of ``to``/``subject``/
``body`` is a literal from the HTTP request body. The runtime has no gmail branch at
all, so the authoring question does not arise until one is added, and this round
adds none.

**2. The approval binds the body and hides it from the approver.** ``_SUMMARY_ARG_FIELDS``
carries ``to`` and ``subject`` and not ``body``; ``canonical_payload`` hashes *every*
validated argument, so the digest moves when the body moves. The binding is
cryptographically sound and the *review* is not: an APPROVE is a decision about text
the decider cannot read. The one constant that documents the intent to redact bodies,
``_SENSITIVE_ARG_FIELDS``, is declared at ``approvals/service.py:63`` and referenced
nowhere. This is what closes question B mechanically — a model-authored body cannot
become an approved body — while leaving open whether the answer is "show it" or
"literal only", which is a governance call, not a code fact.

**3. The identity rule does not need a bigger vocabulary, and round three was wrong
to say it did.** ``_has_conflict`` clause C1 already counts *distinct entities* and
already means "more than one exact entity" by ``CONFLICTING``. But the status decision
at ``identity/service.py:175`` counts *claims*, so one contact matched on both email
and phone — the strongest evidence available, and the one case
``derive_confidence`` rewards — falls to ``UNKNOWN`` with zero confidence while still
carrying the address. ``distinct_ids`` is computed one line below for ``contact_id``.
Every status also collapses onto exactly three ``rule_identity`` outcomes (verify,
escalate, deny), so no new member could carry a new operational meaning without
rewriting that function: ``AMBIGUOUS`` would be a synonym, not a distinction.

A fourth result bounds the severity. ``runtime/service.py:686`` raises before
``:527`` writes, so the live producer never persists a non-positive identity, and the
writer/reader asymmetry in ``gmail_send_criteria`` is a measured inconsistency rather
than an open hole today. That asymmetry is fully specified as a fix and deliberately
**not** applied: this round's decision is DESIGN-ONLY, and mixing a production change
into it would mean choosing a regime this round was told not to pick.

So: no production code changes, ``gmail_send`` stays unlicensed, and what follows pins
the premises a round that settles A, B, D and E has to move on purpose.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final, get_args
from uuid import uuid4

from kai.approvals.service import (
    _SENSITIVE_ARG_FIELDS,
    _SUMMARY_ARG_FIELDS,
    build_request_summary,
    canonical_payload,
    intent_hash,
)
from kai.identity import IdentityQuery, IdentityResolver, IdentityStatus
from kai.identity.service import _has_conflict
from kai.policy import engine as policy_engine
from kai.policy.models import IdentityStatus as PolicyIdentityStatus
from kai.policy.models import PolicyDecisionKind
from kai.policy.rules import rule_identity
from kai.runtime.fulfillment import calendar_meeting_criteria, gmail_send_criteria
from kai.runtime.schemas import MeetingWindow
from kai.runtime.service import AgentRuntime
from kai.tasks.schemas import FulfillmentProfile, TaskContractInput
from kai.tasks.service import LIVE_SEALABLE_PROFILES, create_task
from kai.tools.catalog import default_registry
from kai.tools.gateway import _IDENTITY_TO_POLICY
from kai.tools.risk import RiskLevel
from kai.tools.schemas import GmailSendArgs
from tool_harness import (
    GoogleBackend,
    contact_payload,
    insert_account,
    make_context,
    make_intent,
    make_policy_input,
    make_task,
    make_tenant_user,
    session_scope,
)

SRC: Final = Path(__file__).resolve().parents[1] / "src" / "kai"

OWNER: Final = "owner@corp.example"
ALICE: Final = "alice@example.com"

#: The secret the approver must never be asked to approve blind.
SECRET_BODY: Final = "Internal: the deal closes Friday at 40k."

GMAIL_ARGS: Final[dict[str, Any]] = {
    "to": ALICE,
    "subject": "Quarterly plan",
    "body": SECRET_BODY,
}


def _source(node: Any) -> str:
    return textwrap.dedent(inspect.getsource(node))


def _repo_text(*parts: str) -> str:
    return (SRC.joinpath(*parts)).read_text(encoding="utf-8")


def _gmail_tool() -> Any:
    return default_registry().get("gmail.send")


def _future_window() -> MeetingWindow:
    start = date.today() + timedelta(days=3)
    return MeetingWindow(
        start_date=start,
        end_date=start + timedelta(days=1),
        earliest_time="09:00",
        title="KAI",
    )


# ---------------------------------------------------------------------------
# 1. Approval: bound, but not read
# ---------------------------------------------------------------------------


def test_the_body_is_bound_to_the_approval_and_never_shown_to_the_approver() -> None:
    """The measured asymmetry, which is the mechanical half of gate item B.

    The approver's whole view of a send is one string. It names the recipient and the
    subject. It does not name the body — while the digest that the approval row stores
    covers the body exactly, so a single trailing space invalidates an approval. A
    tamper-proof binding over text nobody read is not informed consent, and that is a
    fact about the code rather than an argument about policy.
    """
    tool = _gmail_tool()
    summary = build_request_summary(tool=tool, arguments=GMAIL_ARGS)

    assert ALICE in summary
    assert GMAIL_ARGS["subject"] in summary
    assert SECRET_BODY not in summary
    assert "40k" not in summary

    payload = canonical_payload(tool=tool, arguments=GMAIL_ARGS, account_id=None)
    assert payload["arguments"]["body"] == SECRET_BODY
    assert set(payload["arguments"]) == {"to", "subject", "body"}

    baseline = intent_hash(tool=tool, arguments=GMAIL_ARGS, account_id=None)
    edited = intent_hash(
        tool=tool, arguments={**GMAIL_ARGS, "body": SECRET_BODY + " "}, account_id=None
    )
    assert baseline != edited, "an invisible edit to the body must break the approval"

    assert baseline != intent_hash(
        tool=tool, arguments=GMAIL_ARGS, account_id=uuid4()
    ), "the account is part of the binding"

    # And the recipient — the field the previous two rounds made load-bearing — is
    # visible. So the gap is specific to the body, not to arguments generally.
    assert "to" in _SUMMARY_ARG_FIELDS
    assert "subject" in _SUMMARY_ARG_FIELDS
    assert "body" not in _SUMMARY_ARG_FIELDS


def test_the_sensitive_field_set_is_declared_and_referenced_nowhere() -> None:
    """The redaction of bodies is documented by a constant that does nothing.

    ``_SENSITIVE_ARG_FIELDS`` names ``body``, ``body_html``, ``content`` and
    ``message`` in a comment that promises they "are never echoed into a
    human-readable summary or any stored/serialized surface". Nothing implements the
    promise: the name appears once in the whole package, at its own declaration. The
    only thing keeping bodies out of summaries today is that ``_SUMMARY_ARG_FIELDS``
    happens not to list them — an omission, not a rule.
    """
    assert "body" in _SENSITIVE_ARG_FIELDS
    assert {"body", "body_html", "content", "message"} == set(_SENSITIVE_ARG_FIELDS)

    service = _repo_text("approvals", "service.py")
    assert service.count("_SENSITIVE_ARG_FIELDS") == 1, (
        "the set is declared and never read, so the redaction it promises is unenforced"
    )
    # And the summary builder reaches only for the allowlist, never for the denylist.
    summary_builder = _source(build_request_summary)
    assert "_SUMMARY_ARG_FIELDS" in summary_builder
    assert "_SENSITIVE_ARG_FIELDS" not in summary_builder


# ---------------------------------------------------------------------------
# 2. The sender, and the only path that actually sends
# ---------------------------------------------------------------------------


def test_the_send_tool_admits_no_sender_field() -> None:
    """The one send value with an unambiguous author is the one the user cannot type.

    ``GmailSendArgs`` is exactly ``{to, subject, body}``, all required. There is no
    ``from``, so the sender cannot be an argument, cannot be a model output, and
    cannot be a policy input. It is read from the connected account — the module
    docstring says so, and the account is resolved by ``(tenant, user)`` before the
    request is built. Provider handshake is therefore the *only* defensible author
    of ``From:``, and the repository already holds that position.
    """
    assert set(GmailSendArgs.model_fields) == {"to", "subject", "body"}
    assert all(field.is_required() for field in GmailSendArgs.model_fields.values())
    assert not {"from", "sender", "account", "account_id"} & set(GmailSendArgs.model_fields)

    api = _repo_text("api", "gmail.py")
    assert "the sender is taken from the stored account, never from the request" in api

    # The sender is read off the account inside the service, not off the request.
    service = _repo_text("gmail", "service.py")
    assert "account.email" in service


def test_the_only_live_send_path_is_the_governed_adapter() -> None:
    """``send_and_verify`` has exactly one call site, and it is the governed one.

    This test previously asserted the defect: ``send_and_verify`` had two call
    sites, the one that worked was ``POST /gmail/send``, and it skipped the
    gateway entirely. The reasoning in the old docstring was that the caller *was*
    the authenticated user acting on their own account, so the runtime's authority
    question was deemed not to apply. That reasoning was the bug — it let an
    external side effect reach the provider without policy, approval, a task,
    durable evidence, an evaluator, or audit, and therefore without anything that
    could later reconstruct or reverse what happened.

    The direct endpoint is now a ``410 Gone`` tombstone. What remains is the
    governed adapter, and with it the whole chain: policy, approval, task,
    evidence, verification, audit.
    """
    service = _repo_text("gmail", "service.py")
    call_sites = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name != "service.py"
        and "await send_and_verify(" in path.read_text(encoding="utf-8")
    )
    assert call_sites == ["tools/adapters/gmail.py"]
    assert "async def send_and_verify" in service

    api_path = SRC / "api" / "gmail.py"
    api = api_path.read_text(encoding="utf-8")

    # Assert on the parsed imports, not on raw text: the read endpoints legitimately
    # import from ``kai.gmail.service``. What must be absent is the send function.
    imported: set[str] = set()
    tree = ast.parse(api)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "kai.gmail.service":
            imported.update(alias.asname or alias.name for alias in node.names)
    assert "send_and_verify" not in imported
    assert imported  # the read path is genuinely still wired

    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "send_and_verify" not in calls

    # The docstrings name what the module no longer reaches, so the substring checks
    # run against the code with every docstring line removed.
    prose: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            prose.update(range(first.lineno, first.end_lineno + 1))
    code = "\n".join(
        line for number, line in enumerate(api.splitlines(), start=1) if number not in prose
    )
    assert "410" in code, "the retired endpoint must still answer Gone"
    for absent in (
        "SendRequest",
        "ToolGateway",
        "PolicyEngine",
        "ApprovalService",
        "RuntimeExecution",
        "from kai.tools.gateway",
        "from kai.policy",
        "from kai.approvals",
    ):
        assert absent not in code, f"the retired send path must not reach {absent}"

    # The tombstone must name the governed path rather than simply refusing, so a
    # caller that reads the failure is told where the write actually lives.
    assert "/runtime/tasks" in code

    # And the agent side is still closed, so no runtime intent can reach the one
    # remaining call site.
    translator = _repo_text("runtime", "translator.py")
    assert "gmail" not in _source_of_intents_for()
    assert "gmail" not in translator.split("SLICE_ALLOWED_TOOLS")[1].split(")")[0]


def _source_of_intents_for() -> str:
    from kai.runtime.service import AgentRuntime as _Runtime

    return _source(_Runtime._intents_for)


# ---------------------------------------------------------------------------
# 3. Identity: the predicate, and why the vocabulary is already big enough
# ---------------------------------------------------------------------------


async def test_the_status_predicate_and_the_conflict_rule_both_count_entities() -> None:
    """The defect this design review found is closed, and must stay closed.

    This test originally asserted the *bug*: "The strongest evidence in the
    system is the evidence that downgrades a verdict." One contact matched on both
    email and phone produced two exact claims, failed ``len(exact_claims) == 1``,
    and fell to ``UNKNOWN`` at zero confidence while the address stayed populated
    on the result. The conflict rule one screen earlier counted distinct entities,
    so the two counters disagreed — and the disagreement landed precisely on the
    corroborated case, which is the case ``derive_confidence`` exists to reward.

    That is now corrected. All three counters read the same set of distinct
    entities, and this test is the regression guard for it: the corroborated case
    must be *at least as trusted* as the single-identifier case, never less.

    The historical finding is kept in the docstring because the correction is
    still latent — no product query carries both identifiers yet — and a latent
    fix is exactly the kind that gets quietly reverted.
    """
    resolve_source = _source(IdentityResolver.resolve)
    assert "elif len({claim.contact.resource_name for claim in exact_claims}) == 1:" in (
        resolve_source
    )
    assert "len(exact_claims) == 1" not in resolve_source
    assert 'distinct_ids = {claim.contact.resource_name for claim in exact_claims}' in _source(
        IdentityResolver._build
    )
    assert "if len({claim.contact.resource_name for claim in exact_claims}) > 1:" in _source(
        _has_conflict
    )

    async with session_scope() as session:
        tenant, user = await make_tenant_user(OWNER)
        account = await insert_account(
            tenant_id=tenant.id, user_id=user.id, email=OWNER, sub="sub-1"
        )
        task = await make_task(tenant_id=tenant.id, user_id=user.id)

        both = contact_payload(
            resource_name="people/1", display_name="Alice", emails=(ALICE,), phones=("+15550100",)
        )

        async def _resolve(query: IdentityQuery) -> Any:
            backend = GoogleBackend()
            backend.contacts_connections = [both]
            return await IdentityResolver().resolve(
                query=query,
                context=make_context(
                    session=session, task=task, backend=backend,
                    account_id=account.id, identity=query,
                ),
            )

        corroborated = await _resolve(IdentityQuery(email=ALICE, phone="+15550100"))
        single = await _resolve(IdentityQuery(email=ALICE))

        # Two identifiers on one contact are no longer labelled less trusted than
        # one. They are the same entity, established twice.
        assert corroborated.status is IdentityStatus.CONTACT_MATCH
        assert corroborated.confidence == single.confidence
        # The address is populated and the verdict agrees with it.
        assert corroborated.email == ALICE
        assert corroborated.contact_id == "people/1"
        # Both exact claims really were made, so this is not a near miss.
        assert len([e for e in corroborated.evidence if e.contact_id is not None]) == 2

        assert single.status is IdentityStatus.CONTACT_MATCH
        assert single.contact_id == "people/1"

        # A second contact holding the same address is still a conflict. Correcting
        # the predicate must not have relaxed C1 into "the claims agree".
        two_holders = GoogleBackend()
        two_holders.contacts_connections = [
            both,
            contact_payload(
                resource_name="people/2", display_name="Alice w", emails=(ALICE,)
            ),
        ]
        contested = await IdentityResolver().resolve(
            query=IdentityQuery(email=ALICE),
            context=make_context(
                session=session, task=task, backend=two_holders,
                account_id=account.id, identity=IdentityQuery(email=ALICE),
            ),
        )
        assert contested.status is IdentityStatus.CONFLICTING
        assert contested.contact_id is None


def test_the_projection_is_total_and_many_to_one_so_a_new_state_cannot_add_an_outcome() -> None:
    """Why a bigger vocabulary could not help: the projection has no room for one.

    ``_IDENTITY_TO_POLICY`` is the whole of the resolver-to-policy relationship, and it
    is a total map from five resolver states onto three policy states, with three of
    them collapsing onto ``VERIFIED``. The gateway subscripts it with no default, so a
    new resolver state would raise rather than slip through — and extending it can only
    ever land on ``VERIFIED``, ``UNKNOWN`` or ``CONFLICTING``, which are the three
    behaviours ``rule_identity`` implements.

    So ``AMBIGUOUS`` is not a missing distinction; it is arithmetic. It would be a
    synonym of "escalate" or of "deny", and the case the previous round wanted it for —
    one contact corroborated on two identifiers — is already positive under the clause
    that counts entities rather than claims. This is the mechanical refutation of
    "the rule needs AMBIGUOUS".
    """
    assert {status.value: projected.value for status, projected in _IDENTITY_TO_POLICY.items()} == {
        "CONTACT_MATCH": "VERIFIED",
        "CHANNEL_VERIFIED": "VERIFIED",
        "PROVIDER_VERIFIED": "VERIFIED",
        "UNKNOWN": "UNKNOWN",
        "CONFLICTING": "CONFLICTING",
    }
    # Total over the resolver's vocabulary, and three-to-one on the positive side.
    assert set(_IDENTITY_TO_POLICY) == set(IdentityStatus)
    assert sorted({p.value for p in _IDENTITY_TO_POLICY.values()}) == [
        "CONFLICTING", "UNKNOWN", "VERIFIED",
    ]
    assert set(_IDENTITY_TO_POLICY.values()) == set(PolicyIdentityStatus)

    # And the three policy states have exactly three behaviours, with identity decided
    # before any approval consideration so a deny can never be masked by one.
    assert policy_engine.PolicyEngine.rule_names() == (
        "binding", "task_state", "allowed_tools", "critical_tool", "risk_policy",
        "deadline", "step_budget", "cost_budget", "identity", "risk_approval",
    )
    names = policy_engine.PolicyEngine.rule_names()
    assert names.index("identity") < names.index("risk_approval")


async def test_the_three_policy_states_have_exactly_three_behaviours_and_no_more() -> None:
    """The collapse is not a claim about the vocabulary; it is a fact about the rule.

    Fed a real task, the real ``gmail.send`` definition and a real intent, each of the
    three projected states produces one of three decisions, and ``CONFLICTING`` denies
    before ``risk_approval`` ever gets a turn. So the resolver's five states already
    have all the authorisation meaning the gateway can express, and a sixth could only
    be a synonym of one of these three.
    """
    async with session_scope():
        tenant, user = await make_tenant_user(OWNER)
        task = await make_task(tenant_id=tenant.id, user_id=user.id, risk_policy="MEDIUM")
        tool = _gmail_tool()
        intent = make_intent("gmail.send", task=task, arguments=dict(GMAIL_ARGS))

        outcomes: set[str] = set()
        for projected in PolicyIdentityStatus:
            decision = rule_identity(
                make_policy_input(
                    task=task, tool=tool, intent=intent,
                    risk_level=RiskLevel.MEDIUM, recipient_identity=projected,
                )
            )
            outcomes.add("ALLOW" if decision is None else str(decision.decision))

        assert outcomes == {
            "ALLOW",
            str(PolicyDecisionKind.REQUIRE_APPROVAL),
            str(PolicyDecisionKind.DENY),
        }
        assert len(outcomes) == 3

        # Below MEDIUM the rule abstains, so the three-way projection is a property of
        # the risky actions — the ones that would send a message.
        assert rule_identity(
            make_policy_input(
                task=task, tool=tool, intent=intent,
                risk_level=RiskLevel.LOW, recipient_identity=PolicyIdentityStatus.UNKNOWN,
            )
        ) is None
        # And with no identity at all the rule stays silent, deferring to the risk rule.
        assert rule_identity(
            make_policy_input(
                task=task, tool=tool, intent=intent,
                risk_level=RiskLevel.MEDIUM, recipient_identity=None,
            )
        ) is None


# ---------------------------------------------------------------------------
# 4. Sealing: five points, no content
# ---------------------------------------------------------------------------


def test_five_sealing_points_exist_and_not_one_of_them_carries_content() -> None:
    """The contract is sealed at submission, and it is sealed about *scope*.

    ``TaskContractInput`` is eleven fields and every one of them is a limit, a name of
    a capability, a risk level, or prose. There is no recipient, no subject and no
    body. So the sharper statement of round three's finding is not "the licence
    precedes the value" — the profile is a *scope* choice, legitimately made from the
    goal — but "no content seal exists in the scope layer at any point". That is what
    makes the sealing boundary a real choice rather than a matter of moving a line.
    """
    assert set(TaskContractInput.model_fields) == {
        "goal", "success_criteria", "fulfillment_profile", "allowed_tools", "risk_policy",
        "deadline", "max_cost", "max_steps", "verification_method", "fallback",
    }
    content_words = {"recipient", "recipients", "to", "subject", "body", "message", "title"}
    assert not content_words & set(TaskContractInput.model_fields)

    # Point 1: scope, at submission, and it is the single write that gates licensing.
    create_source = _source(create_task)
    assert "LIVE_SEALABLE_PROFILES" in create_source
    assert "fulfillment_profile" in create_source

    # Points 2-4: content and plan, later, in the runtime.
    runtime = _repo_text("runtime", "service.py")
    assert "row.scenario = scenario.model_dump(mode=\"json\")" in runtime
    assert "row.identities = identities" in runtime
    assert "plan_version, plan_hash" in runtime or "plan_hash" in runtime

    # Point 5: authorisation, at the gateway, over validated arguments.
    assert "def intent_hash(" in _repo_text("approvals", "service.py")

    # And the sealing gate itself is still closed for this domain. The local
    # ``task_preparation`` profile joined the vocabulary in Stage 2E; gmail did not.
    assert frozenset({"calendar_meeting", "task_preparation"}) == LIVE_SEALABLE_PROFILES
    assert get_args(FulfillmentProfile) == (
        "calendar_meeting",
        "gmail_send",
        "task_preparation",
    )
    assert "gmail_send" not in LIVE_SEALABLE_PROFILES


# ---------------------------------------------------------------------------
# 5. Replay: the writer is strict, the reader is not
# ---------------------------------------------------------------------------


def test_the_positive_status_guard_precedes_the_identities_write() -> None:
    """A non-positive identity cannot be persisted, which bounds the defect's severity.

    The resolver helper raises ``RuntimeFatal`` when a guest's status is outside
    ``_POSITIVE_IDENTITY``, and ``_drive`` turns that into a ``_fail`` return which
    happens *before* the assignment to ``row.identities``. So the live producer never
    leaves a bad row for the readers to misread. The defect measured next is a
    writer/reader inconsistency, not an open hole — and it becomes one the moment a
    second writer exists.
    """
    drive = _source(AgentRuntime._drive)
    resolve_guests = _source(AgentRuntime._resolve_guest_identities)

    assert "if resolved.status not in _POSITIVE_IDENTITY:" in resolve_guests
    assert "raise RuntimeFatal(f\"attendee identity not established: {guest}\")" in resolve_guests

    # The guard fires inside the helper; the write happens in the caller, after the
    # helper's exception has already been turned into a terminal state.
    call = drive.index("identities = await self._resolve_guest_identities(")
    failure = drive.index("return await self._fail(session, task, str(exc))", call)
    write = drive.index("row.identities = identities")
    assert call < failure < write, "the guard must abort before the row is written"

    # The replay guard keys on emptiness, which is safe only because a failed grounding
    # persists nothing. Both halves of that sentence are load-bearing.
    assert drive.index("if not row.identities:") < write


def test_both_criteria_readers_take_the_address_and_discard_the_status() -> None:
    """The measured reader defect, identical in both domains and therefore symmetric.

    Each builder collects ``item["email"]`` and never looks at ``item["status"]``, so a
    row recording CONFLICTING, a row recording UNKNOWN, and a row with no status at
    all each contribute a *required* address — and an empty required set is what the
    evaluators treat as undecidable rather than satisfied. The fix is specified and
    small: require a positive status before taking the address. It is not applied here,
    because this round's decision is DESIGN-ONLY.
    """
    window = _future_window()

    for status in (
        IdentityStatus.CONTACT_MATCH.value,
        IdentityStatus.UNKNOWN.value,
        IdentityStatus.CONFLICTING.value,
        None,
    ):
        identity: dict[str, Any] = {
            "name": "Alice", "status": status, "email": ALICE, "confidence": 0.0,
        }
        gmail = gmail_send_criteria(profile="gmail_send", identities=[identity])
        calendar = calendar_meeting_criteria(
            profile="calendar_meeting", window=window, identities=[identity]
        )
        assert gmail.for_gmail_send().required_recipients == frozenset({ALICE})
        assert calendar.for_calendar_meeting().required_attendees == frozenset({ALICE})

    # The two builders share the comprehension verbatim, so any fix is symmetric by
    # construction and cannot privilege one domain.
    gmail_source = _source(gmail_send_criteria)
    calendar_source = _source(calendar_meeting_criteria)
    collection = 'str(item["email"]).casefold() for item in identities if item.get("email")'
    assert collection in gmail_source
    assert collection in calendar_source
    assert 'item.get("status")' not in gmail_source
    assert 'item.get("status")' not in calendar_source
