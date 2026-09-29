"""What makes a tool-run record a *qualifying creation attempt*.

The runtime persists one append-only record per tool invocation on
``RuntimeExecution.evidence`` (built by :func:`kai.ir.adapter.evidence_from_observation`).
Two readers need to answer a question about those records, and until this module
existed they answered it with two different definitions:

* :func:`kai.runtime.fulfillment.evaluate_calendar_meeting` asks *"is there
  evidence here that the Intent was fulfilled?"* and is therefore deciding
  something;
* :func:`kai.runtime.service._created_event_id` asks *"which created meeting may
  this run read back?"* and is therefore addressing a follow-up call.

A second reading of "a meeting was created" is a semantic boundary problem, not
an ordering one: the same record could be a witness to one reader and invisible
to the other. So the meaning is stated once, here, and both read it from here.

```text
raw evidence record
        |  deterministic, offline
        v
  qualifying_creation(record)  ->  QualifyingCreation | None
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

============================  ==================================================
``tool``                      ``calendar.create_event`` — the creation tool, and
                              nothing else;
``outcome``                   ``EXECUTION_SUCCEEDED``;
``ok``                        true;
``verified``                  true — the verification boundary stood behind the
                              result, not merely the adapter asking for it;
``evidence["event_id"]``      a non-empty string: a provider identity.
============================  ==================================================

A failed attempt, another tool, an unverified result, a success the provider
never identified, and a record that is not even a mapping are all ``None``. They
are evidence that an attempt was *made* — not evidence about whether anything was
fulfilled. Nothing else is consulted: no session, no task state, no other
records, no provider, no clock, no model. The function is total and pure, so the
same record qualifies the same way on a live run, on a recovery, and on a replay
of a permuted log, forever.

**``evidence.event_id`` is the authority; ``data.event_id`` is not.** This is
the one asymmetry the repository had, and it is decided here rather than left
implicit. A durable record carries the created event in two places, and they are
not the same claim:

* ``data`` is the adapter's **serialized provider payload** — what the provider
  returned, as content. The Gateway passes it through untouched, in every
  outcome, without consulting the tool's declared
  :class:`~kai.tools.execution.VerificationContract`. It is a claim about *what
  the provider said*.
* ``evidence`` is what the **verification boundary republishes**:
  :meth:`kai.tools.stubs.ContractVerificationEngine.verify` re-asserts the
  declared contract and only then carries the adapter's evidence forward under
  ``{"contract": ..., **execution.evidence}``. For a create tool the contract is
  ``READ_AFTER_CREATE``, and the engine refuses to report ``verified`` unless the
  adapter produced an ``event_id`` for the read-back to have used
  (:func:`kai.tools.stubs._missing_contract_evidence`). It is a claim about *what
  was proved*.

Only the second one is gated on the contract, so only the second one can stand
behind a fulfillment verdict. A record carrying an ``event_id`` in ``data`` alone
therefore proves that a payload was serialized and nothing more: the read-back
either never confirmed it, or the boundary declined to vouch for it. Reading
``data`` as a fallback would let an unverified id enter the witness set through
the back door, and the ``data`` field is never verified by construction — so
there is no fallback here, on purpose, and not merely for convenience. Both
values happen to be equal in the one adapter that writes them today; the decision
is about which one is *load-bearing*, and only ``evidence`` is.

**Two fields, and no more.** A :class:`QualifyingCreation` carries the provider
identity and the arguments the attempt was made with, because those are exactly
what the two readers need: the evaluator compares the arguments against the
sealed criteria, and the runtime addresses the provider by the id. There is
deliberately no attempt identity on it — no ``step_order``, no ``request_id``, no
``ir_action_id``. Nothing reads one, a witness is identified by what it
witnesses rather than by where it sat in a log, and a field added only so a
record could be traced is a field that can disagree with the durable row it
claims to describe.

**No persistence.** A :class:`QualifyingCreation` is an in-memory typed
projection derived deterministically from evidence that is already durable, in
the same way :func:`kai.runtime.fulfillment.calendar_meeting_criteria` is derived
from the sealed rows. Storing it would add a second copy that can only drift, a
migration, and a backfill question about rows written before this meaning was
stated. There is nothing to add and nothing to guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from kai.tools.result import ToolOutcome

__all__ = [
    "MEETING_CREATION_TOOL",
    "QualifyingCreation",
    "qualifying_creation",
    "qualifying_creations",
]

#: The one tool whose successful, verified, provider-identified attempts are what
#: ``calendar_meeting`` fulfillment is asked about. Named here so the filter reads
#: as a decision about the domain rather than as a string comparison buried in a
#: check, and so the evaluator and the runtime cannot name it differently.
MEETING_CREATION_TOOL: Final = "calendar.create_event"

#: The key the provider's own identity of the created object is published under.
#:
#: Deliberately qualified with its container: the same key appears in ``data``,
#: and the whole point of :func:`_provider_event_id` is that only the copy inside
#: ``evidence`` is authoritative. See the module docstring.
_PROVIDER_EVENT_ID: Final = "event_id"

#: Where that identity is read from, and the only place. Nothing falls back to
#: ``data``: a payload the verification boundary never vouched for is not a
#: provider identity, and treating it as one would be inventing evidence.
_PROVIDER_IDENTITY_CONTAINER: Final = "evidence"


@dataclass(frozen=True)
class QualifyingCreation:
    """One verified, provider-identified attempt to create a meeting.

    The whole of "a qualifying creation attempt", and nothing else:

    * ``provider_event_id`` — the identity the provider gave the created event,
      read out of the record's verification evidence. Its presence is what
      separates *an event was created* from *an attempt was made*, and it is the
      address the runtime reads the event back by.
    * ``arguments`` — what the attempt was made **with**: the attendees and the
      slot the run asked for. This is the meeting being witnessed, so it is what
      the sealed criteria are compared against; it is the request, not the
      provider's read-back of it, which is why it is ``args`` and not ``data``.

    Frozen, and the arguments are held as a read-only copy, so a witness cannot be
    edited after qualification and cannot be changed from behind by mutating the
    record it was built from. It is deliberately **not** hashable, because it
    carries the attempt's arguments: a content hash over a mapping has nowhere to
    be defined without freezing the arguments into a second representation, and
    equality alone is what the reader of a witness set needs.

    **It carries no attempt identity.** No ``step_order``, no ``request_id``, no
    ``ir_action_id``, no ordinal. A witness is not a position in a log: the
    evaluator iterates witnesses existentially and reads none of this, the runtime
    reads only the id, and an identifier with no reader would be a second thing
    that could disagree with the durable record it claims to name.
    """

    provider_event_id: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        # ``frozen=True`` forbids reassignment through the normal path, so the
        # one-time normalization of the arguments goes through ``object`` — the
        # copy is taken here, once, and never after.
        object.__setattr__(
            self, "arguments", MappingProxyType(dict(self.arguments))
        )


def qualifying_creation(record: Any) -> QualifyingCreation | None:
    """This record read as a qualifying creation attempt, or ``None``.

    A **total, pure function of the record's own content**: the same record
    always yields the same answer, in the same process or another, on a live run
    or a recovery, whatever else is in the log and wherever this record sits in
    it. There is no I/O here, no session, no clock, no model, and no other
    record, so nothing can make a witness appear or disappear.

    ``None`` is an answer, not a failure — it means *this record is not evidence
    that a meeting was created*, which is true of a failed attempt, another
    tool, an unverified result, a success the provider never identified, and of
    anything that is not a record at all. Qualification never raises.
    """
    if not isinstance(record, Mapping):
        return None
    if record.get("tool") != MEETING_CREATION_TOOL:
        return None
    if record.get("outcome") != ToolOutcome.EXECUTION_SUCCEEDED.value:
        return None
    if not record.get("ok") or not record.get("verified"):
        return None
    event_id = _provider_event_id(record)
    if event_id is None:
        return None
    arguments = record.get("args")
    return QualifyingCreation(
        provider_event_id=event_id,
        arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
    )


def qualifying_creations(evidence: Iterable[Any]) -> tuple[QualifyingCreation, ...]:
    """The relevant witness set: every qualifying creation in the log.

    A filter, never a truncation: the **whole** set is returned, in the order the
    log happens to hold. It is a set for the evaluator's purposes and nothing
    chooses a representative out of it, so no caller can let the log's order
    decide an answer.

    Duplicate suppression is deliberately **absent**, and is not a correctness
    matter here. A resumed run can legitimately record the same creation twice;
    because the verdict is existential, a repeat is neither counted twice nor
    dropped, so a deduplicating pass would buy nothing but a cost. If a caller
    ever wants a unique set for storage or reporting, that is a separate
    question from what a witness *means*.
    """
    return tuple(
        witness
        for witness in (qualifying_creation(record) for record in evidence)
        if witness is not None
    )


def _provider_event_id(record: Mapping[str, Any]) -> str | None:
    """The provider's identity for the created event, or ``None``.

    Read from ``evidence`` and only from ``evidence`` — the copy the verification
    boundary republished after re-asserting the tool's ``READ_AFTER_CREATE``
    contract. ``data`` is the adapter's serialized provider payload, which the
    Gateway publishes without consulting that contract, and which is therefore
    not a standing claim that anything was created; a fallback to it would let an
    identity the boundary declined to vouch for re-enter the witness set.

    A non-string or empty value is ``None`` rather than coerced. A provider
    identity that is not a usable string is not an identity, and inventing one
    from ``42`` or ``" "`` is exactly the guess this module exists to refuse.

    A blank value is refused but never rewritten: the id is returned exactly as
    the provider spelled it, because a *changed* id would be a different address
    to read the event back by.
    """
    container = record.get(_PROVIDER_IDENTITY_CONTAINER)
    if not isinstance(container, Mapping):
        return None
    event_id = container.get(_PROVIDER_EVENT_ID)
    if isinstance(event_id, str) and event_id.strip():
        return event_id
    return None
