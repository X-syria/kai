---
name: kai-approval
description: Manage KAI approvals - canonical intent, intent hash, approval consumption, TOCTOU, approver visibility, and execution binding.
when_to_use: When changing approval flows, intent hashing, Gateway ordering, approver-facing payloads, or anything between approval and execution.
user-invocable: true
---

# KAI Approval and Execution Binding

Purpose: keep the chain **canonical intent -> approval -> consumption ->
execution** intact and honest about what each part actually guarantees.

## Shared base rule (binding on every KAI skill)

**Actual code is the primary source of truth.**

```
actual code
-> call graph
-> data flow
-> authority
-> state/lifecycle
-> invariants
-> tests
-> persistence
-> Git history when needed
-> architecture truth
-> documentation reconciliation
```

## Scope

Owns:
- canonical intent definition and hashing
- approval creation, evaluation, and consumption
- TOCTOU re-evaluation
- approver-visible projections
- binding between what was approved and what is executed

Does not own:
- Gateway pipeline mechanics beyond ordering constraints -> `kai-runtime`
- identity resolution behind the intent -> `kai-identity`
- Gmail message licensing -> `kai-google`

## Concepts that must stay distinct

Keep these four separate. Conflating any of them is how approvals become
meaningless:

| Concept | What it is |
|---|---|
| canonical payload | the normalized, full intent being authorized |
| digest | a hash **over** the canonical payload |
| approver-visible projection | the subset and rendering a human actually sees and decides on |
| executed arguments | the values actually passed to the provider at execution time |

A change to one does not imply a change to the others. Verify each independently.

## Rules

1. **Approval must bind to the canonical intent.** An approval authorizes a
   specific canonical payload. If the payload changes, the approval no longer
   applies - recompute and require a new one.

2. **A SHA-256 intent hash is not a digital signature.** It is a digest over
   data. It provides integrity against accidental or naive change and
   tamper-evidence for stored data. It does **not** provide authenticity,
   non-repudiation, or proof of who approved. Never describe it as a
   cryptographic signature, a signature, a certificate, or proof of approval
   authorship. Use the words "digest", "hash", or "content hash".

3. **Preserve Gateway ordering.** The approval check, the TOCTOU
   re-evaluation, and the approval consumption occur in a fixed order relative
   to policy, identity, and execution. Do not reorder them, and do not move
   consumption earlier or later without evidence that the change is safe. The
   full pipeline ordering is specified in `kai-runtime`.

4. **Hash coverage is not human visibility.** A digest covering the canonical
   payload says nothing about whether the approver could see and understand the
   operation. These are independent properties. A proposal may be perfectly
   bound by hash and still opaque to the person approving it. Do not report
   hash binding as evidence of adequate review.

5. **Do not treat a matching hash as sufficient authorization.** Hash equality
   proves the payload is unchanged. Authorization requires that a valid
   approval exists, is unconsumed, is unexpired, belongs to the right principal
   and tenant, and covers the current state of the world.

## Closed hypothesis - M8

An earlier hypothesis stated that the Gateway rebuilds `arguments` after
approval consumption, which would allow execution to diverge from what was
approved.

**This hypothesis was disproven.** The current code carries the same
`arguments` through:

```
check -> consume -> execute
```

The arguments are not reconstructed between consumption and execution, so the
hypothesized divergence does not exist in the current code.

- Do not describe M8 as a current bug.
- Do not reopen M8 without new evidence citing code that rebuilds or
  substitutes arguments in that path.
- Do not redesign the Gateway pipeline on the basis of M8.

## Adjacent but separate: Gmail approver visibility

The Gmail body-verification gap - the extent to which the approver-visible
projection reveals the actual message body - is an **independent** concern. It
is tracked and addressed on its own terms.

This skill must not be used as justification to redesign Gmail licensing,
message construction, or body rendering. Raise visibility questions in the
documented gap; do not expand scope from here.

## Prohibited

- Calling a hash a signature.
- Treating hash equality as proof of approval.
- Adding a second hashing, signing, or confirmation layer without a proven
  need (see `kai-engineering`).
- Changing the approver-visible projection without checking what tests assert
  about it.
- Reordering Gateway stages as part of an approval fix.
- Reopening M8 absent new evidence.
