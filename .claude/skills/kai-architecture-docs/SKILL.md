---
name: kai-architecture-docs
description: Update architecture documentation from the actual code - classify CURRENT, PLANNED, OPEN DECISION, and KNOWN GAP, and reconcile doc/code conflicts without editing code to match docs.
when_to_use: When writing or updating architecture documents, or when documentation and implementation appear to disagree.
user-invocable: true
---

# KAI Architecture Documentation

Purpose: bring architecture documentation into agreement with the actual
implementation. The direction of correction is always **code -> docs**, never
**docs -> code**.

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

This skill is the terminal step of that order. It may only be reached after the
architecture truth model exists - see `kai-architecture` for producing it.

## Scope

Owns:
- writing and updating architecture documents
- classifying every architectural claim by status
- reconciling documentation against implementation

Does not own:
- discovering the architecture -> `kai-architecture`
- deciding what to build -> `kai-engineering`
- domain behavior semantics -> the domain skills

## Mandatory order

```
actual code
-> architecture truth model
-> compare docs
-> update docs
```

Do not begin editing a document before the truth model for the area in question
exists. Do not infer the truth model by reading the document you are about to
update.

## Status labels - every claim gets exactly one

| Label | Meaning |
|---|---|
| `CURRENT` | Implemented in code and traceable to `file:line`, optionally covered by tests. |
| `PLANNED` | Intended, not implemented. Must reference the decision or issue that establishes the intent. |
| `OPEN DECISION` | Genuinely undecided. Both options remain live; the document must present them as unresolved. |
| `KNOWN GAP` | The code is wrong or incomplete relative to intent. Must describe observed behavior and the gap, not the desired fix. |

Never write an unimplemented design in the present tense. Never write an
implemented behavior as future work.

## When documentation conflicts with code

**Do not change code merely to match the document.** A document is not a spec
that outranks the implementation; it is a record that may be stale.

Classify the conflict explicitly, and record it as one of:

- **stale docs** - the document describes an implementation that no longer
  exists. Fix the document.
- **stale code** - the implementation diverges from a settled, still-valid
  decision. Do not fix the code here. Escalate as a decision item for
  `kai-engineering`.
- **undocumented behavior** - the code does something no document mentions.
  Document it as `CURRENT` if it is intentional; otherwise raise it as
  `KNOWN GAP`.
- **ambiguous intent** - the code is compatible with several readings. Record
  as `OPEN DECISION` with the options enumerated.
- **open decision** - the document records an unresolved choice and the code
  has meanwhile picked one. Either revert to `OPEN DECISION` explicitly or
  promote to `CURRENT` only after the decision is settled.

## Traceability

Every significant architectural claim must be traceable to exactly one of:

- implementation (a `file:line` reference)
- a test (a test that would fail if the claim were false)
- a recorded decision (the document or issue where the choice was settled)

A claim with no traceability is an opinion. Either cite it or delete it.

## Prohibited

- Editing production code to satisfy a document.
- Copying an existing document's claims forward as `CURRENT` without
  re-verifying them against code.
- Collapsing `OPEN DECISION` into `CURRENT` because the code currently behaves
  one way. What the code does today is not the same as what was decided.
- Marking something `PLANNED` without naming the intent it comes from.
- Silently deleting a conflicting passage instead of classifying it.
- Writing architecture prose that a reader could not verify against the
  repository.
