"""What makes a tool-run record a *qualifying send attempt*.

This is the second-domain experiment, and the first thing it has to earn is the
right to exist as its own module. :mod:`kai.runtime.evidence` states the
creation-witness boundary for ``calendar_meeting``; this file states the
send-witness boundary for ``gmail_send``, and the two are deliberately **not**
factored into one.

The tempting move is to notice that the two functions have the same shape — a
tool name, a success outcome, ``ok``, ``verified``, a provider id in
``evidence`` — and merge them behind a rule. That is exactly the move this
experiment exists to avoid taking on faith, so it is not taken. A shared
predicate would assert that "a verified, provider-identified attempt" is one
meaning; whether that is so has to be *shown* per domain, and until it is, the
duplicated shape is the evidence rather than the defect. The two definitions sit
side by side, can be read against each other, and are free to diverge.

```text
raw evidence record
        |  deterministic, offline
        v
  qualifying_send(record)  ->  QualifyingSend | None
        |
        v
  the relevant witness set
        |
        v
  SuccessCriteria -> evaluator -> FulfillmentOutcome
```

**Qualification is not fulfillment.** The question answered here is *may this
record serve as a witness at all?* — never *were the criteria met?* The second
question belongs to :mod:`kai.runtime.fulfillment`, over the set of witnesses
this module produces, and the quantifier over that set is unchanged by anything
in this file.

**What qualification is.** A record qualifies when, and only when, all of the
following hold, each read off the record's own content:

============================  =================================================
``tool``                     ``gmail.send`` — the send tool, and nothing
                              else; ``gmail.search`` and ``gmail.read`` are
                              *readers* of a mailbox and witness nothing having
                              been sent;
``outcome``                  ``EXECUTION_SUCCEEDED``;
``ok``                       true;
``verified``                 true — the verification boundary stood behind the
                              result, not merely the adapter asking for it;
``evidence["message_id"]``   a non-empty string: the provider's identity of the
                              message this attempt delivered.
============================  =================================================

Nothing else is consulted: no session, no task state, no other records, no
provider, no clock, no model. The function is total and pure, so the same record
qualifies the same way on a live run, on a recovery, and on a replay of a
permuted log, forever.

**``evidence.message_id`` is the authority, and ``thread_id`` is not.**
``gmail.send`` publishes *two* provider identities in ``evidence``, and this is
the one place the repository had to choose between them. It chooses
``message_id``, and the reasoning is not that it is the easier field to read:

* ``message_id`` names **one message**, and it is the address the read-after-send
  was actually issued against. :func:`kai.gmail.service.send_and_verify` calls
  ``client.get_message(message_id=sent.message_id)``, so this id is what the
  verification acted upon; :func:`kai.gmail.service.verify_send` then requires
  ``readback.message_id == sent.message_id``, which is an equality between two
  provider answers and not a plausibility check. It is 1:1 with an attempt: one
  ``gmail.send`` yields one accepted message id.
* ``thread_id`` names a **conversation grouping**, and a thread is not an
  attempt. It routinely holds several messages — earlier turns, replies, other
  senders' mail — so its existence says nothing about whether *this* intent was
  delivered. It is also not stable in the way an attempt is: a send that lands in
  an existing thread inherits that thread's id, so two separate sends can share
  one. Admitting it would make a witness fail to identify a witness, which is
  precisely the defect :class:`~kai.runtime.evidence.QualifyingCreation` is
  written to avoid.

The repository's own verification model already agrees, and this module follows
it rather than restating a preference:
:func:`kai.tools.stubs._missing_contract_evidence` gates a ``READ_AFTER_SEND``
claim on ``message_id`` alone, and :func:`kai.gmail.service.verify_send` treats
``thread_id`` as a presence check ("non-empty") rather than a value to compare.
The boundary that decides what counts as proved names ``message_id``; this
module reads the same field for the same reason.

**``evidence`` is the authority; ``data`` is not.** A durable record carries the
provider's answer in two places, and they are not the same claim:

* ``data`` is the adapter's **serialized provider payload** — what
  ``GmailAdapter._send`` produced from the read-back message. The Gateway passes
  it through untouched, in every outcome, without consulting the tool's declared
  :class:`~kai.tools.execution.VerificationContract`. It is a claim about *what
  the provider said*.
* ``evidence`` is what the **verification boundary republishes**:
  :meth:`kai.tools.stubs.ContractVerificationEngine.verify` re-asserts the
  declared contract and only then carries the adapter's evidence forward under
  ``{"contract": ..., **execution.evidence}``.

Only the second is gated on the contract, so only the second can stand behind a
fulfillment verdict, and there is **no fallback** to ``data`` — the same refusal
:mod:`kai.runtime.evidence` makes for ``event_id``, for the same reason.

**A stronger refusal than calendar's, and it is structural rather than
editorial.** In ``GmailAdapter._send`` the evidence dictionary is populated only
when the read-back returned a message: on a failed read the adapter emits
``evidence={}`` and ``verified=False``, and on the success path it emits a
``message_id``. So an unverified send cannot produce a provider identity even by
accident — there is nothing in the record to read one from, whether from
``evidence`` or from ``data`` (``data`` is ``None`` in the same branch). The
no-fallback rule is therefore not merely a decision here; it has no gap to
close. It is still written as an explicit refusal, because the day an adapter
starts serializing an id into ``data`` on the failure path, the refusal is what
has to already be in place.

**No attempt identity.** A :class:`QualifyingSend` carries the provider's message
identity and the arguments the attempt was made with, and nothing else: no
``step_order``, no ``request_id``, no ordinal, and deliberately no
``thread_id``. A witness is identified by what it witnesses, not by where it sat
in a log or which conversation it joined; an identifier with no reader is a
second thing that can disagree with the durable row it claims to name.

**No persistence.** A :class:`QualifyingSend` is an in-memory typed projection
derived deterministically from evidence that is already durable, in the same way
:class:`~kai.runtime.evidence.QualifyingCreation` is. Storing it would add a
second copy that can only drift, a migration, and a backfill question.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from kai.runtime.schemas import EXTERNAL_OUTCOME, EXTERNAL_OUTCOME_UNKNOWN
from kai.tools.result import ToolOutcome

__all__ = [
    "MESSAGE_SEND_TOOL",
    "QualifyingSend",
    "qualifying_send",
    "qualifying_sends",
    "undetermined_sends",
]

#: The one tool whose successful, verified, provider-identified attempts are what
#: ``gmail_send`` fulfillment is asked about. Named here for the reason
#: ``kai.runtime.evidence.MEETING_CREATION_TOOL`` is named there: so the
#: evaluator and the runtime cannot spell the domain's tool differently.
#:
#: ``gmail.search`` and ``gmail.read`` are excluded on purpose. They read a
#: mailbox, and a mailbox that *already contains* the message is not a witness
#: that this run sent it — the distinction is the same one that keeps
#: ``calendar.get_event`` out of the creation set.
MESSAGE_SEND_TOOL: Final = "gmail.send"

#: The key the provider's own identity of the delivered message is published
#: under. Qualified with its container for the same reason as
#: ``kai.runtime.evidence._PROVIDER_EVENT_ID``: the same key also appears in the
#: serialized payload, and only the copy inside ``evidence`` is authoritative.
_PROVIDER_MESSAGE_ID: Final = "message_id"

#: Where that identity is read from, and the only place. See the module
#: docstring: nothing falls back to the serialized ``data`` payload.
_PROVIDER_IDENTITY_CONTAINER: Final = "evidence"


@dataclass(frozen=True)
class QualifyingSend:
    """One verified, provider-identified attempt to send a message.

    The whole of "a qualifying send attempt", and nothing else:

    * ``provider_message_id`` — the identity the provider gave the message this
      attempt delivered, read out of the record's verification evidence. Its
      presence is what separates *a message was sent* from *a send was
      attempted*.
    * ``arguments`` — what the attempt was made **with**: the recipient, subject
      and body the run asked to send. This is the send being witnessed, so it is
      what the sealed criteria are compared against; it is the request, not the
      provider's read-back of it, which is why it is ``arguments`` and not
      ``data``.

    Frozen, and the arguments are held as a read-only copy, so a witness cannot
    be edited after qualification and cannot be changed from behind by mutating
    the record it was built from. It is deliberately **not** hashable, for the
    reason :class:`~kai.runtime.evidence.QualifyingCreation` is not: it carries
    the attempt's arguments, and equality alone is what a reader of a witness set
    needs.

    **It carries no attempt identity and no thread.** Neither ``step_order`` nor
    ``thread_id`` is here, and the second omission is a semantic one rather than
    a trimming one — a thread is a grouping of messages, not an identity of an
    attempt, and publishing it on the witness would be the first step toward
    treating "some conversation exists" as "this intent was delivered".
    """

    provider_message_id: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        # ``frozen=True`` forbids reassignment through the normal path, so the
        # one-time normalization of the arguments goes through ``object`` — the
        # copy is taken here, once, and never after.
        object.__setattr__(
            self, "arguments", MappingProxyType(dict(self.arguments))
        )


def qualifying_send(record: Any) -> QualifyingSend | None:
    """This record read as a qualifying send attempt, or ``None``.

    A **total, pure function of the record's own content**: the same record
    always yields the same answer, in the same process or another, on a live run
    or a recovery, whatever else is in the log and wherever this record sits in
    it. There is no I/O here, no session, no clock, no model, and no other
    record, so nothing can make a witness appear or disappear.

    ``None`` is an answer, not a failure — it means *this record is not evidence
    that a message was sent*, which is true of a failed attempt, a read-only
    gmail tool, an unverified result, a success the provider never identified,
    and of anything that is not a record at all. Qualification never raises.
    """
    if not isinstance(record, Mapping):
        return None
    if record.get("tool") != MESSAGE_SEND_TOOL:
        return None
    if record.get("outcome") != ToolOutcome.EXECUTION_SUCCEEDED.value:
        return None
    if not record.get("ok") or not record.get("verified"):
        return None
    message_id = _provider_message_id(record)
    if message_id is None:
        return None
    arguments = record.get("args")
    return QualifyingSend(
        provider_message_id=message_id,
        arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
    )


def qualifying_sends(evidence: Iterable[Any]) -> tuple[QualifyingSend, ...]:
    """The relevant witness set: every qualifying send in the log.

    A filter, never a truncation: the **whole** set is returned, in the order the
    log happens to hold. It is a set for the evaluator's purposes and nothing
    chooses a representative out of it, so no caller can let the log's order
    decide an answer.

    Duplicate suppression is deliberately **absent**, and is not a correctness
    matter here. A resumed run can legitimately record the same send twice;
    because the verdict is existential, a repeat is neither counted twice nor
    dropped, so a deduplicating pass would buy nothing but a cost.

    This matters more for a send than for a creation, and it is worth being
    explicit about why the verdict stays the same. Two *distinct* sends of the
    same intent are not duplicates at all — they are two witnesses, and the
    existential question is satisfied by either. De-duplicating by message id
    would additionally be wrong in the one case where ids are not the point: the
    question "was this intent fulfilled" is about existence, so collapsing
    witnesses cannot change the answer, and an implementation that did collapse
    them would be asserting that the *count* matters. It does not.
    """
    return tuple(
        witness
        for witness in (qualifying_send(record) for record in evidence)
        if witness is not None
    )


def undetermined_sends(evidence: Iterable[Any]) -> tuple[Mapping[str, Any], ...]:
    """The attempts to send whose outcome nobody ever recorded.

    The complement of :func:`qualifying_sends`, and deliberately the same shape:
    a filter that returns the **whole** set of records that say *this send may or
    may not have gone out*, in log order, with nothing truncated.

    **These records are not witnesses and are never converted into one.** A
    missing provider message id is not a message that was not sent; it is a
    message whose existence this run cannot vouch for. Keeping them as their own
    set, rather than folding them into the witness set with a flag, is what stops
    an unknown from being counted as evidence either way.

    Scoped to :data:`MESSAGE_SEND_TOOL` for the same reason
    :func:`qualifying_send` is: a read that timed out says nothing about whether
    a send happened, and this evaluator is not asked about reads. Which error
    **Selection is by ``external_outcome``, never by ``error_code``.** The runtime
    writes that field as its own conclusion after deciding that a side-effecting
    run ended without a knowable outcome; ``error_code`` reports why the gateway
    returned what it returned, which is a different question. In particular
    ``APPROVAL_STALE`` is an authorization refusal and does not appear here: the
    gateway consumes an approval immediately before dispatch, so that refusal
    provably stopped the invocation before the provider was reached.

    Scoped to :data:`MESSAGE_SEND_TOOL` for the same reason
    :func:`qualifying_send` is: a read KAI could not complete says nothing about
    whether a send happened, and this evaluator is not asked about reads.
    """
    return tuple(
        record
        for record in evidence
        if isinstance(record, Mapping)
        and record.get("tool") == MESSAGE_SEND_TOOL
        and record.get(EXTERNAL_OUTCOME) == EXTERNAL_OUTCOME_UNKNOWN
    )


def _provider_message_id(record: Mapping[str, Any]) -> str | None:
    """The provider's identity for the delivered message, or ``None``.

    Read from ``evidence`` and only from ``evidence`` — the copy the verification
    boundary republished after re-asserting the tool's ``READ_AFTER_SEND``
    contract. The serialized ``data`` payload is what the provider said, not what
    the boundary vouched for, and there is no fallback to it.

    A non-string or empty value is ``None`` rather than coerced. A provider
    identity that is not a usable string is not an identity, and inventing one
    from ``42`` or ``" "`` is exactly the guess this module exists to refuse.

    A blank value is refused but never rewritten: the id is returned exactly as
    the provider spelled it, because a *changed* id would be a different address.
    """
    container = record.get(_PROVIDER_IDENTITY_CONTAINER)
    if not isinstance(container, Mapping):
        return None
    message_id = container.get(_PROVIDER_MESSAGE_ID)
    if isinstance(message_id, str) and message_id.strip():
        return message_id
    return None
