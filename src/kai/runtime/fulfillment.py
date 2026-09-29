"""Deterministic fulfillment evaluation.

The authority chain this module implements, and the boundary it sits on:

```text
Sealed SuccessCriteria  ->  this evaluator  ->  FulfillmentVerdict
                                                   ->  Task State Machine
```

**What the success criteria are.** ``success_criteria`` in the Task Contract is
*prose*, and it stays prose: the planner reproduces it verbatim, so it is a
descriptive surface for humans and for the plan contract check. It is **not** what
fulfillment is decided on. What is decided on is :class:`SuccessCriteria` — a
typed, frozen, sealed model that says exactly which conditions must be proved,
in one of the domains that exists.

So the two are deliberately separate, and neither can become the other:

```text
success_criteria (prose)          SuccessCriteria (typed)
  description, display,           the authority: what must be
  verbatim plan contract            proved, and in which domain
        --- never read by the evaluator ---   |
                                              v
                          evaluate_calendar_meeting  |  evaluate_gmail_send
```

A model's paraphrase of the criteria cannot reach an evaluator, and a criteria
model cannot choose an evaluator: :class:`SuccessCriteria` names its own
``profile`` and the dispatcher in :mod:`kai.runtime.service` is the only thing
that maps a profile to a function.

**Domain discriminator.** ``FulfillmentProfile`` answers *which question is being
asked*; ``SuccessCriteria`` answers *what must be proved inside it*. They are
separate on purpose and never merged. ``SuccessCriteria`` also carries the profile
it belongs to, so a payload can never be paired with a profile it was not written
for — the model rejects the mismatch instead of the evaluator meeting it later.

**What an evaluator may do:** read the frozen, durable inputs in
:class:`FulfillmentInput` and return a verdict.

**What an evaluator may not do:** transition task state, invoke a provider,
select a branch, read transient runtime state, or consult a model. It cannot
declare ``COMPLETED`` even if every check passes — the verdict it returns is
consumed by the task state machine, which remains the only owner of the
transition. Nothing here performs I/O, and the same inputs always produce the
same verdict, so a resumed run re-derives the same decision.

``FulfillmentVerdict`` is three-valued on purpose. Collapsing "not satisfied" and
"cannot yet tell" into one answer would turn uncertainty into failure, and the
two settle into different lifecycle states.

**Evidence is read as a set, not as a log.** The verdict is a function of the
*relevant evidence set* — never of a record's position in it. The scope of the
two quantifiers is kept deliberately separate, because conflating them is how an
evaluator ends up quietly re-deciding the domain:

* over the **relevant set**, the question is existential — does a qualifying
  meeting satisfying the criteria *exist*?
* over **one meeting**, the criteria are conjunctive — all of them, or it does
  not count.

Neither quantifier is ever applied to the other, and an empty relevant set is an
answer in its own right rather than a vacuous ``all([]) == True``.

**What a witness is, is not decided here.** "Which records are relevant
fulfillment evidence" has one answer per domain, and each is stated in the
module that owns that domain's boundary: :mod:`kai.runtime.evidence` for a
verified, provider-identified ``calendar.create_event`` attempt, and
:mod:`kai.runtime.send_evidence` for a verified, provider-identified
``gmail.send`` attempt. This module imports both filters rather than repeating
either, so the evaluator and the runtime's own readers cannot hold two meanings
of "a meeting was created" or of "a message was sent" — and it is each module's
own identity policy, not a decision taken again per caller, that settles where
that provider's identity is read from. Qualification answers *may this record be
a witness?*; nothing below answers *were the criteria met?*.

**Two domains, and what that does and does not show.** The second-domain
experiment added ``gmail_send`` beside ``calendar_meeting`` and the *shape*
survived: a sealed profile names the question, a typed payload states it, a
witness set is filtered by one stated boundary, the quantifiers are kept in their
own scopes, and the verdict is a pure function of durable state. What did not
    survive unchanged is the substance — the projection is a different type, the
    provider identity is a different field chosen on different grounds, and the
    criteria needed only *one* field here against *two* there. That last one is
    **not** because one domain's verification reaches further than the other's:
    calendar's reaches further — it compares the read-back's attendees and date
    against the request, where gmail's compares recipient and subject — and
    calendar still carries the more criteria. The reason is that calendar's Intent
    asks for a bounded window while its request receives one concrete instant, so
    the sealed range is a condition verification cannot reach: a provider-reported
    slot can fall outside the range the user asked for and still verify. The
    address anchor the two domains share is mechanically the same projection, and
    on the single live producer it is redundant rather than load-bearing, since the
    request is built from the sealed identities and ``verify_event`` already
    compares the read-back's people to that request by equality. The second domain
    is therefore evidence about the *binding* and about
    the *set semantics*. It is not evidence that fulfillment is a general framework,
    and the code is written so that claiming one would require adding code rather
    than rearranging what is here.

**Why there is no expression language here.** ``CalendarMeetingCriteria`` and
``GmailSendCriteria`` are dataclass-shaped sets of typed fields for their
domains, not a DSL. There is no boolean tree to write, no predicate to register,
and nothing a model can author. Two domains sharing a *pattern* is still two
domains: they have no common base, no shared predicate, and no code path that
treats them interchangeably. A third domain would be a further amendment to this
closed vocabulary, exactly as ``FulfillmentProfile`` already is, and not a new
language.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kai.runtime.evidence import QualifyingCreation, qualifying_creations
from kai.runtime.schemas import MeetingWindow
from kai.runtime.send_evidence import QualifyingSend, qualifying_sends
from kai.tasks.schemas import FulfillmentProfile

__all__ = [
    "CALENDAR_MEETING_EVALUATOR_VERSION",
    "CalendarMeetingCriteria",
    "FulfillmentInput",
    "FulfillmentOutcome",
    "FulfillmentVerdict",
    "GMAIL_SEND_EVALUATOR_VERSION",
    "GmailSendCriteria",
    "SuccessCriteria",
    "calendar_meeting_criteria",
    "evaluate_calendar_meeting",
    "evaluate_gmail_send",
    "gmail_send_criteria",
]

#: Stable identity of the calendar meeting evaluator's *semantics*, written as
#: ``<profile>:<semantic version>``.
#:
#: This is a declared constant, deliberately not anything computed. A git SHA
#: would identify a commit rather than a meaning, and would change on every
#: unrelated edit; a timestamp would make re-derivation non-deterministic; a hash
#: of this source would identify a build rather than a semantics, and would move
#: on a pure refactor that changed no meaning. ``v1`` names one thing and one
#: thing only: the evidence-set semantics of *a qualifying creation existing that
#: satisfies all sealed meeting criteria*. It is bumped by hand, and only when
#: that meaning changes.
CALENDAR_MEETING_EVALUATOR_VERSION: Final = "calendar_meeting:v1"

#: Stable identity of the gmail send evaluator's *semantics*, written the same
#: way and for the same reasons as
#: :data:`CALENDAR_MEETING_EVALUATOR_VERSION`: a declared constant naming one
#: meaning, ``v1`` meaning "a qualifying send attempt to the sealed recipients
#: exists", bumped by hand and only when that meaning changes.
#:
#: It is a *separate* identity rather than a shared one, and the separation is
#: the point. ``calendar_meeting:v1`` and ``gmail_send:v1`` are not two versions
#: of one evaluator; if they were, a change to either domain's meaning would
#: force a bump of both, and the recorded identity would claim a coupling that
#: nothing has established. Two names, two meanings, two independently
#: versioned histories.
GMAIL_SEND_EVALUATOR_VERSION: Final = "gmail_send:v1"


class FulfillmentVerdict(StrEnum):
    """The deterministic answer to "was the Intent fulfilled?".

    ``FULFILLED`` is the only value that may permit ``COMPLETED``.
    ``NOT_FULFILLED`` is a decision, not a gap. ``INDETERMINATE`` means the
    evidence available cannot settle the question either way and a human has to
    decide what the task means now.
    """

    FULFILLED = "FULFILLED"
    NOT_FULFILLED = "NOT_FULFILLED"
    INDETERMINATE = "INDETERMINATE"


class CalendarMeetingCriteria(BaseModel):
    """The typed success criteria of the ``calendar_meeting`` domain.

    Exactly the conditions the evaluator has always checked, named as data:

    * ``window`` — the sealed, validated meeting window. Reused as-is rather than
      copied into new date/time fields, because it is already the validated
      object and a second copy is a second thing that can disagree with it.
      ``title`` is carried along but is **not** a success condition: the evaluator
      has never read it, so this model does not promote it into one.
    * ``required_attendees`` — the resolved addresses the created meeting must
      carry. This is the resolved set at its sealed state, not a re-resolution, and
      it is worth being exact about what "resolved" names: the sealed
      ``identities`` row is a join between the model's reading of the goal (a guest
      name) and the identity resolver's output (that guest's address), and this
      field takes the second half. It therefore states *which addresses must be
      present*, not *which people were asked for* — it is blind to how the name was
      rendered, to two names that resolve to one address, and to the ``status`` and
      ``confidence`` that recorded how each address was established.

    The two conjunctive conditions this encodes are the ones the prose states:
    the resolved attendees are among the event's own, and the event falls inside
    the requested window. The third prose criterion — an event was created at all
    — is not a field here: it is answered by the *existence* of a qualifying
    creation in the evidence, which is the evaluator's quantifier rather than a
    parameter of the criteria. Keeping it out is what stops this model from
    growing a redundant flag that could disagree with the evidence.

    Frozen and ``extra="forbid"``, so a sealed criteria cannot be widened, and a
    field that is not part of this domain's meaning cannot be smuggled in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    window: MeetingWindow
    required_attendees: frozenset[str] = Field(default_factory=frozenset)

    def as_criteria(self) -> str:
        """A human-readable rendering, for audit output only.

        Never read back. It exists so a stored outcome can be shown in a sentence
        without anyone opening the durable JSON, and it is a projection of these
        fields, not a source for them.
        """
        window = self.window
        return (
            f"a meeting between {window.start_date} and {window.end_date} "
            f"starting no earlier than {window.earliest_time}, "
            f"with {sorted(self.required_attendees) or 'no required attendee'}"
        )


class GmailSendCriteria(BaseModel):
    """The typed success criteria of the ``gmail_send`` domain.

    One field, and the field count is itself a finding rather than a
    simplification. Compare the two domains:

    **What ``verified=True`` means on both sides, and what it does not.** Each
    domain's provider verification compares the read-back against the **request**
    that produced it, and neither compares it against the Intent.
    :func:`kai.calendar.service.verify_event` compares title, start, end,
    timezone, date, start time, end time and attendees;
    :func:`kai.gmail.service.verify_send` compares the message id, the thread,
    the ``SENT`` label, the sender, the recipient and the subject. So on both
    sides ``verified=True`` means *the provider echoed our request* — a claim
    about the request, and in neither domain a claim about the Intent. Calendar's
    verification is in fact the **broader** of the two, and calendar nonetheless
    carries **more** criteria, so the reach of verification cannot be what
    explains the difference between them.

    **So what a criterion is for is a sealed source to judge the request
    against.** A criterion is stated where there is a durable, sealed value that
    answers something the request's own fields cannot answer on their own: a
    **resolved identity set**, which the log can only *report* rather than
    establish, and a **bounded window**, which the request cannot carry — it
    receives one concrete instant, and a provider-supplied instant can fall
    outside the range the Intent asked for. Where the sealed source and the
    request hold the same value by construction, a criterion would assert only
    that the copy is a copy.

    Note the limit of that, because the rule was once stated more strongly than
    the code supports. :func:`kai.runtime.translator.create_intent_args` fills the
    calendar request's ``attendees`` from the very identities the criteria are
    sealed from, so on that single live producer the request *is* a copy of the
    sealed row, and ``verify_event`` already compares the read-back's attendees to
    that request by set equality — meaning no record that producer can emit fails
    the attendee check while still qualifying as a witness. The field is kept
    because the log is not required to have been written by that producer and
    because the durable ``identities`` column is the authority for an address the
    log's own fields merely report; the window is the field that carries the
    calendar domain's weight against verification.

    Which leaves exactly one condition here, because a send has no range to
    state:

    * ``required_recipients`` — the resolved contacts the sent message must have
      been addressed to. The resolved set at its sealed state, exactly as
      :class:`CalendarMeetingCriteria` carries the resolved attendees.

    **And what licenses the word "recipient" is open, not settled.** The value is
    built from the same sealed identity row the calendar payload reads, through
    the same construction, and this branch does not read the understanding at
    all. But no sealed artifact states that a recipient was wanted:
    :class:`~kai.runtime.schemas.UnderstandingOutput` has no recipient field, and
    the only contract builder in the repository seals ``calendar_meeting`` on
    every task it produces. So the projection is mechanically identical across the
    two domains and semantically unestablished for this one, which is why
    ``gmail_send`` remains a sealed profile rather than a driven one.

    **``subject`` and ``body`` are deliberately not fields.** Each is refused for
    a different, stated reason, and neither is a judgement call — and note that
    neither reason is that the request holds a copy, which is what a former
    version of this docstring claimed:

    * ``subject`` is compared by ``verify_send`` against the request, and **no
      sealed source states it** — not the contract's goal, not the understanding,
      not any durable column. A criterion would have nothing independent to
      compare against, so it would read the request back to itself.
      :class:`CalendarMeetingCriteria` refuses ``title`` for a *different* reason
      that must not be borrowed: a sealed title does exist
      (:attr:`~kai.runtime.schemas.MeetingWindow.title`), the provider already
      compares it against the request, and no sealed prose criterion states it —
      so the ground there is a missing statement, not a missing source.
    * ``body`` is compared by ``verify_send`` not at all, and the Intent carries no
      sealed body of its own either. A criterion no evidence can satisfy and no
      Intent can independently state would make every send unfulfillable, so it is
      not stated.

    Frozen and ``extra="forbid"``, for the same reasons as the calendar payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    required_recipients: frozenset[str] = Field(default_factory=frozenset)

    def as_criteria(self) -> str:
        """A human-readable rendering, for audit output only.

        Never read back, and a projection of the sealed field rather than a
        source for it — the same contract as
        :meth:`CalendarMeetingCriteria.as_criteria`.
        """
        return (
            "a message sent to "
            f"{sorted(self.required_recipients) or 'no required recipient'}"
        )


class SuccessCriteria(BaseModel):
    """The authoritative, sealed statement of what fulfillment must prove.

    One discriminated payload per domain, with the domain named on the record:
    ``profile`` says which question is being asked, and the payload says what must
    be proved inside it. That pairing is checked here rather than assumed by the
    evaluator, so a criteria object can never claim a profile whose payload it
    does not carry.

    It is a *closed* vocabulary of two, mirroring ``FulfillmentProfile`` itself.
    There is no registry, no resolution hook, and no expression tree: a third
    domain would mean a third payload and widening this model deliberately, which
    is a contract amendment and not something a task can bring with it.

    **Two members is not a framework.** The payloads share a shape — a frozen
    model, one accessor, conditions read by one evaluator — and nothing else: no
    common base class, no protocol, no shared predicate, and no code path that
    treats them interchangeably. That is the honest state of the evidence after
    the second-domain experiment, and a base class asserting more than that has
    not been earned by two domains.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: FulfillmentProfile
    calendar_meeting: CalendarMeetingCriteria | None = None
    gmail_send: GmailSendCriteria | None = None

    @model_validator(mode="after")
    def _payload_matches_profile(self) -> Self:
        """A payload that disagrees with the named profile is not a sealed state.

        The check is a closed enumeration rather than a lookup, so that adding a
        domain is a visible edit here instead of a silent acceptance of a
        mismatch. It is written as three refusals, because there are now three
        distinct ways to be wrong and each deserves its own reason: a profile
        with no payload, a profile carrying another domain's payload, and a
        profile that is not in the vocabulary at all.
        """
        named: CalendarMeetingCriteria | GmailSendCriteria | None
        foreign: CalendarMeetingCriteria | GmailSendCriteria | None
        if self.profile == "calendar_meeting":
            named, foreign = self.calendar_meeting, self.gmail_send
        elif self.profile == "gmail_send":
            named, foreign = self.gmail_send, self.calendar_meeting
        else:
            raise ValueError(
                f"success criteria name profile {self.profile!r}, which is not a "
                "sealed fulfillment question"
            )
        if named is None or foreign is not None:
            raise ValueError(
                f"success criteria claim profile {self.profile!r} with no matching payload"
            )
        return self

    def for_calendar_meeting(self) -> CalendarMeetingCriteria:
        """The calendar payload, reachable only when the profile says calendar.

        Typed as returning the payload rather than the wrapper, so an evaluator's
        signature names the domain's conditions and the caller cannot hand it a
        criteria belonging to a different question.
        """
        if self.profile != "calendar_meeting" or self.calendar_meeting is None:
            raise ValueError(f"no calendar criteria in profile {self.profile!r}")
        return self.calendar_meeting

    def for_gmail_send(self) -> GmailSendCriteria:
        """The gmail payload, reachable only when the profile says gmail.

        The second of the two accessors, and there is deliberately no third way
        to reach a payload. Each domain's conditions are named by that domain's
        own accessor, so an evaluator's signature still says which meaning it
        applies, and neither payload can be read by the other's evaluator even by
        accident.
        """
        if self.profile != "gmail_send" or self.gmail_send is None:
            raise ValueError(f"no gmail criteria in profile {self.profile!r}")
        return self.gmail_send


def calendar_meeting_criteria(
    *,
    profile: FulfillmentProfile,
    window: MeetingWindow,
    identities: Iterable[Mapping[str, Any]],
) -> SuccessCriteria:
    """Seal the calendar success criteria from already-sealed durable state.

    This is the single place typed criteria are built, and it is pure: the same
    sealed ``window`` and the same sealed ``identities`` always yield the same
    criteria, on a live run and on a recovery alike.

    **Sealing is inherited, not duplicated.** The window is validated and sealed
    when the scenario is persisted, and the identities when the resolution is
    persisted; neither is ever recomputed, and neither is re-derived at evaluation
    time. So the criteria are as immutable as the rows they come from — there is
    no second copy that could drift, and nothing to back-fill for rows that
    predate this model, because every input needed is already durable on the
    execution row. That is why this needs no migration and no new stored field.

    ``required_attendees`` is built with the same normalization the evaluator
    always applied — case-fold, skip anything without an email — so the set is
    identical to the one the previous prose-era code compared against. An
    unsatisfiable set stays unsatisfiable here rather than being rejected:
    ``test_an_unresolved_attendee_is_not_fulfilled`` depends on an empty required
    set yielding ``NOT_FULFILLED``, and refusing it at sealing time would change
    a case that is already decided.
    """
    required = frozenset(
        str(item["email"]).casefold() for item in identities if item.get("email")
    )
    return SuccessCriteria(
        profile=profile,
        calendar_meeting=CalendarMeetingCriteria(
            window=window,
            required_attendees=required,
        ),
    )


def gmail_send_criteria(
    *,
    profile: FulfillmentProfile,
    identities: Iterable[Mapping[str, Any]],
) -> SuccessCriteria:
    """Seal the gmail success criteria from already-sealed durable state.

    The second-domain counterpart of :func:`calendar_meeting_criteria`, and the
    reason it can be this short is worth stating plainly: **the sealed identity
    rows are the only input this domain needs.** There is no meeting window, no
    date range, no time-of-day bound, and — unlike the calendar criteria — no
    second field competing for attention. The recipient is the whole question.

    The same durable rows, and the same normalization, as the calendar path: the
    identities were resolved and persisted before any tool ran, are never
    recomputed, and are normalized case-folded here with identities that resolved
    no email skipped. So this adds **no new persistence and no migration** — the
    experiment reused an existing sealed row rather than inventing a storage
    shape for it, which is itself part of what the experiment was testing.

    An unsatisfiable set stays unsatisfiable here rather than being rejected, for
    the reason the calendar path keeps it: an empty required set is a legitimate
    sealed value that the evaluator already decides as ``NOT_FULFILLED``, and
    refusing it at sealing time would convert a decided verdict into a failed
    run.
    """
    required = frozenset(
        str(item["email"]).casefold() for item in identities if item.get("email")
    )
    return SuccessCriteria(
        profile=profile,
        gmail_send=GmailSendCriteria(required_recipients=required),
    )


@dataclass(frozen=True)
class FulfillmentInput:
    """Everything a fulfillment evaluator is allowed to read.

    A frozen record rather than the runtime's own objects, so it is structurally
    impossible to hand an evaluator a session, a task, or a live model
    response. Every field is durable state that survives a crash:

    * ``criteria`` — the sealed :class:`SuccessCriteria`: what must be proved, in
      which domain. It carries no session and no model output, and it was built
      from the persisted window and identities, not from a live re-reading of
      anything.
    * ``evidence`` — the append-only durable tool-run log.

    The resolved identities and the raw scenario are deliberately *absent*: the
    evaluator no longer receives them to infer conditions from. They are what the
    criteria were built from, once, and passing them alongside would invite the
    inference this model exists to remove.
    """

    criteria: SuccessCriteria
    evidence: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class FulfillmentOutcome:
    """The whole of one evaluation, as a single self-consistent record.

    The three fields travel together because they are one fact, not three: the
    profile names the evaluator, the version says *which semantics of that
    evaluator* ran, and the verdict is what those semantics concluded. Keeping
    them in one value is what stops an audit record from pairing one
    evaluator's answer with another's identity.

    **This is an audit artifact, never an authority.** The record may be persisted
    so a reader can ask "what was concluded, and by what", and it is expected to
    be re-derivable from the durable evidence at any time. It is never consulted
    to decide anything: a re-derivation that disagrees with it is the
    re-derivation that is right, because the authority is the evaluator applied
    to the evidence, and this only describes that having happened.
    """

    profile: FulfillmentProfile
    version: str
    verdict: FulfillmentVerdict


#: The relevant evidence set is not selected here. *What qualifies as a creation
#: witness* is stated once, in :mod:`kai.runtime.evidence`, and imported above —
#: so this evaluator and the runtime's own event-id reader cannot drift into two
#: meanings of "a meeting was created".


def _meeting_satisfies_criteria(
    created: QualifyingCreation,
    *,
    criteria: CalendarMeetingCriteria,
) -> bool:
    """Does *this one* created meeting satisfy every sealed Success Criterion?

    The conjunction lives here, and its scope is one meeting. The conditions are
    read off the typed criteria rather than inferred: the resolved attendees must
    be among the event's own, and the event must fall inside the requested window.
    Every check is a decidable comparison over durable scalars, and an unparseable
    argument is a failed criterion rather than an error, so this cannot raise.

    The witness is a :class:`~kai.runtime.evidence.QualifyingCreation`, so what is
    compared is the creation's own arguments — already known to be a verified,
    provider-identified attempt — rather than a raw log record re-inspected here.
    """
    args = created.arguments
    attendees = {
        str(address).casefold() for address in (args.get("attendees") or [])
    }
    required = criteria.required_attendees
    if not required or not attendees or not required <= attendees:
        return False

    try:
        day = date.fromisoformat(args["date"])
        start_time = time.fromisoformat(args["start_time"])
    except (KeyError, TypeError, ValueError):
        return False

    window = criteria.window
    if not (window.start_date <= day <= window.end_date):
        return False
    return bool(start_time >= window.earliest_time)


def evaluate_calendar_meeting(inputs: FulfillmentInput) -> FulfillmentVerdict:
    """Was a meeting satisfying every sealed Success Criterion created?

    The conditions are read from :class:`CalendarMeetingCriteria`, so the meaning
    being applied is a value that can be read, compared, and sealed — not prose
    that this function knows the reading of. Nothing here parses text.

    The verdict is a function of the relevant evidence *set*. Where a single
    qualifying creation exists the answer is that creation's — identical to
    reading it off the log — and where several exist the question is whether one
    of them satisfies the criteria, not whether the log happened to put a
    satisfying one first.

    **The set is imported, not re-derived.** :func:`qualifying_creations` is the
    one qualification boundary, shared with the runtime's own event-id reader, so
    "a qualifying witness" cannot mean one thing to this evaluator and another to
    the code that decides which created event to read back.

    This evaluator never returns ``INDETERMINATE``: every check is a decidable
    comparison over durable scalars, and the runtime only enters ``VERIFYING``
    once every action has succeeded, so on this domain the question always has
    an answer. The value is implemented, routed, and reachable from the state
    machine — no live path on this domain produces it, which is a property of the
    domain rather than a gap in the binding.
    """
    qualifying = qualifying_creations(inputs.evidence)

    # The empty set is an answer, not a gap. No verified meeting was ever
    # created, so the "an event was created" condition is known to be false. That
    # is ``NOT_FULFILLED``, never the empty ``all()`` reading as vacuous truth,
    # and never ``INDETERMINATE``: nothing here is undecidable.
    if not qualifying:
        return FulfillmentVerdict.NOT_FULFILLED

    criteria = inputs.criteria.for_calendar_meeting()

    # Existential over the relevant set: one qualifying creation that satisfies
    # every criterion is enough, because the question is whether such a meeting
    # *exists*. This is the same reading the repository already applies when it
    # checks fulfillment — "a meeting satisfying every sealed Success Criterion
    # exists" — and it is what makes the verdict a function of the set:
    # existence survives permutation, ignores a record that appears twice, and
    # needs no representative to be chosen, so no order is consulted anywhere.
    for created in qualifying:
        if _meeting_satisfies_criteria(created, criteria=criteria):
            return FulfillmentVerdict.FULFILLED
    return FulfillmentVerdict.NOT_FULFILLED


#: The send-witness set is not selected here either. What qualifies as a send
#: witness is stated once, in :mod:`kai.runtime.send_evidence`, and imported
#: above. It is deliberately a *different* module from the creation boundary
#: rather than a shared one — see that module's docstring.


def _send_satisfies_criteria(
    sent: QualifyingSend,
    *,
    criteria: GmailSendCriteria,
) -> bool:
    """Does *this one* sent message satisfy every sealed Success Criterion?

    The conjunction lives here, and its scope is one send. The single condition is
    the sealed recipient set: the address this message went to must include every
    resolved contact the Intent named.

    The witness is a :class:`~kai.runtime.send_evidence.QualifyingSend`, so what
    is compared is the send's own arguments — already known to be a verified,
    provider-identified attempt — rather than a raw log record re-inspected here.

    **Note what is absent, deliberately.** The recipient, the subject and the
    ``SENT`` label are all compared by
    :func:`kai.gmail.service.verify_send` against the request, and
    ``verified=True`` already vouches for them. Re-checking them here would
    compare the request against itself. The one thing that check *cannot* do is
    know whether the address matches the Intent, because the address only becomes
    meaningful when compared against the sealed resolution — which is this line.

    An unparseable argument is a failed criterion rather than an error, so this
    cannot raise. A send addressed to nobody, to a non-string, or to a party the
    Intent never resolved is ``False`` — and, like the calendar path, an empty
    required set is a failed criterion rather than a vacuous success.
    """
    raw_to = sent.arguments.get("to")
    if not isinstance(raw_to, str) or not raw_to:
        return False
    recipients = frozenset({raw_to.casefold()})
    required = criteria.required_recipients
    # An empty required set is a failed criterion, not a vacuous success: the
    # sealed ``bool`` is what keeps ``frozenset() <= recipients`` from standing
    # in for "the Intent named a recipient and it was this one".
    return bool(required) and required <= recipients


def evaluate_gmail_send(inputs: FulfillmentInput) -> FulfillmentVerdict:
    """Was a message sent to the sealed recipients, and verified by the provider?

    The second-domain experiment. It asks the same *shape* of question as
    :func:`evaluate_calendar_meeting` — "does a qualifying witness satisfying
    every sealed criterion exist?" — and the experiment's result is that the
    shape survived the change of domain. What did not survive unchanged is the
    content of the criteria, the projection, and the provider identity, and none
    of the three is hidden here.

    The verdict is a function of the relevant evidence *set*. Where a single
    qualifying send exists the answer is that send's — identical to reading it
    off the log — and where several exist the question is whether one of them
    satisfies the criteria, not whether the log happened to put a satisfying one
    first. Two verified sends of the same intent are **not** a contradiction and
    **not** a duplicate: they are two witnesses, and existential satisfaction
    makes the question indifferent to how many there are.

    **This evaluator never returns ``INDETERMINATE``, and the reason is a
    property of the domain rather than an omission.** Every check is a decidable
    comparison over durable scalars, so there is nothing undecidable here — the
    same structural argument as the calendar evaluator.

    The interesting case is the one that *looks* like it needs the third value: a
    send the provider accepted but whose read-after-send failed. The adapter then
    records ``verified=False`` with no ``message_id`` in ``evidence``, so the
    record is not a witness, and the verdict is ``NOT_FULFILLED`` — even though a
    message may in fact have been delivered. That is correct under the question
    actually being asked, which is "does durable verified evidence establish
    that the intent was fulfilled", and it is the same answer the calendar domain
    gives when a created event cannot be read back.

    It is recorded here rather than resolved, because gmail makes the stakes
    visible in a way calendar does not: a retry after an unverified send may
    deliver the message twice, and that irreversibility is a real argument for a
    future "cannot tell" that is distinct from "not fulfilled". Designing that
    value is **deferred** — it needs a decision about retry policy and about
    whether a human is asked, not a third enum member — and inventing it here
    would be a state the runtime cannot yet route meaningfully. The enum keeps
    three values; this domain produces two.
    """
    qualifying = qualifying_sends(inputs.evidence)

    # The empty set is an answer, not a gap: no verified message was ever sent, so
    # "a message was sent to the Intent's recipient" is known to be false. That is
    # ``NOT_FULFILLED``, never the empty ``all()`` reading as vacuous truth.
    if not qualifying:
        return FulfillmentVerdict.NOT_FULFILLED

    criteria = inputs.criteria.for_gmail_send()

    # Existential over the relevant set, for the same reason and with the same
    # scope discipline as the calendar evaluator: the two quantifiers are kept
    # apart, the criteria are conjunctive over one witness, and neither is
    # applied to the other.
    for sent in qualifying:
        if _send_satisfies_criteria(sent, criteria=criteria):
            return FulfillmentVerdict.FULFILLED
    return FulfillmentVerdict.NOT_FULFILLED
