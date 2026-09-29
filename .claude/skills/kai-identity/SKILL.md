---
name: kai-identity
description: Manage KAI identity resolution - IdentityQuery, claims, entities, IdentityStatus, confidence, conflict, policy projection, persistence, and replay semantics.
when_to_use: When changing identity resolution, status or confidence derivation, entity merging, identity persistence, or how identity results reach policy.
user-invocable: true
---

# KAI Identity

Purpose: preserve the rule that **the entity is the semantic unit**. Identity
resolution determines what a piece of text refers to; it must not invent
ambiguity, dissolve multiplicity, or grant authority.

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
- `IdentityQuery` handling
- claims and evidence about referents
- entity formation and the entity/identifier relationship
- `IdentityStatus` and confidence derivation
- conflict detection
- policy projection of identity results
- identity persistence and replay behavior

Does not own:
- the overall Gateway pipeline -> `kai-runtime`
- approval of the resulting intent -> `kai-approval`
- IR condition vocabulary -> `kai-ir`

## Core rule: semantic unit = entity

This is the foundation of the domain.

- **Multiple identifiers for the same entity do not mean multiple identities.**
  An email address, a phone number, and a display name that all resolve to one
  person are one identity with several attributes. Treating them as three
  identities inflates ambiguity and breaks user trust.

- **Multiple distinct entities are `CONFLICTING`.** When resolution genuinely
  cannot decide between different referents, that is a conflict, and the status
  must say so. Do not average them away, do not pick the plurality silently, and
  do not merge them to reduce noise.

## Rules

1. **Respect `derive_confidence`.** Confidence is derived from evidence by the
   existing implementation. Do not hand-set it, override it, or reimplement it.
   If it appears wrong, establish that from the code and the tests first.

2. **Do not add a new `IdentityStatus` without an architectural decision.** The
   status set is a closed contract consumed by policy, API schemas, persistence
   and tests. Introducing a value is an architectural change, not a local fix.

3. **Preserve the single-writer invariant for `identities`.** Exactly one
   component writes identity rows. A second writer, a bulk upsert, or a
   read-path write path breaks the invariant. All other components read.

4. **Preserve `call < failure < write` in runtime identity persistence.** The
   call to the identity system must occur before any failure is raised, and any
   write happens strictly after. A write that happens before the call, or a
   failure raised before the call, changes observable semantics.

5. **Replay must not raise authority.** A replayed identity result is a
   reproduction of a prior resolution. It does not gain new authority, does not
   bypass a conflict, and does not upgrade a prior low-confidence result.

## Investigating identity behavior

- Trace `IdentityQuery` from construction to consumption: who builds it, what
  fields matter, and where it is filtered or defaulted.
- List every writer of `identities` and every reader. Confirm the single-writer
  invariant holds in practice, not just by naming convention.
- Establish where claims become entities and where entities become a status.
- Identify the policy projection: what identity information crosses into policy
  decisions, and what does not.
- Check tenant scoping at the query, not at the caller.

## Prohibited

- Splitting one entity into multiple identities because it has several
  identifiers.
- Merging distinct entities to avoid a `CONFLICTING` status.
- Introducing a new `IdentityStatus` value ad hoc.
- Hand-setting or post-hoc adjusting confidence.
- Adding a second writer for `identities`.
- Reordering so that a write precedes the call, or a failure precedes it.
- Letting replay elevate authority.
