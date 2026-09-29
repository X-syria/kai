---
name: kai-ir
description: Manage KAI-IR - planner to IR translation, conditions, branching, validation, sealing, observations, program identity, fingerprints, and replay.
when_to_use: When changing the IR, planner output handling, condition or branching semantics, validation, sealing, or canonicalization and fingerprinting.
user-invocable: true
---

# KAI-IR

Purpose: keep KAI-IR a stable, validated, canonical representation whose
identity does not depend on how it was produced.

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
- planner output to IR translation
- conditions and branching
- IR validation and sealing
- observations
- program identity, canonical form, and fingerprints
- replay of IR programs

Does not own:
- runtime execution and evidence -> `kai-runtime`
- identity resolution -> `kai-identity`
- approval binding -> `kai-approval`

## Rules

### 1. Do not assume the model proposes executable IR

The planner is a language model. Its output is **untrusted input**. Verify from
the implementation what actually happens:

- what is parsed and what is discarded
- what is validated and what is passed through
- what is canonicalized and what is taken verbatim
- whether validation happens before or after translation
- what the failure modes are on malformed output

Assume nothing about safety here. Determine it.

### 2. Conditions must use the IR's existing vocabulary

A condition must be expressed with the node and operator vocabulary that KAI-IR
already defines. Do not introduce a second DSL, an embedded expression
language, or ad hoc string conditions.

Before adding any condition form, confirm it cannot be expressed with the
existing vocabulary. If it cannot, that is a design question requiring
justification, not an implementation detail to settle locally.

### 3. Branching must preserve five properties

Any change to branching must leave all of these intact:

1. **DAG validity** - the program remains a valid DAG. No cycles, no dangling
   or unreachable nodes introduced by the change.
2. **Canonical form** - a program has one canonical representation.
3. **Program identity** - identity is derived from canonical form, not from
   incidental structure.
4. **Fingerprint stability** - the fingerprint of an unchanged logical program
   does not change across runs, versions, or construction paths.
5. **Replay** - a stored program replays to the same outcome.

### 4. The same logical program must yield the same canonical identity

Two constructions of the same logical program must canonicalize to the same
identity. This is the property that makes fingerprints and replay meaningful.

Explicitly forbidden:

- deriving identity from source ordering, construction path, or insertion order
- including nondeterministic values (timestamps, ids, addresses, object reprs,
  set iteration order) in canonical form
- including fields that are semantically irrelevant to the program's meaning

When adding a field to an IR node, establish whether it belongs in canonical
form. If it does not, including it will change fingerprints of existing stored
programs and break replay.

### 5. Inspect the real validation, not the docstring

Verification rule: **validate against the actual validation implementation.**

- Locate the validation entry point and read what it actually checks.
- Confirm what it does **not** check.
- Confirm when it runs relative to translation, sealing, persistence, and
  execution.
- Confirm that a docstring, a type annotation, or a schema does not merely
  *describe* validation that is not performed.

Do not report a program as validated unless a validation call in the actual path
enforces it.

### 6. Inspect the observation invariant by writer and timing

An observation is durable, but durability is not the same as correctness. For
every observation invariant, establish:

- **Which production code actually writes it.** If only tests write it, the
  invariant is unproven in production.
- **When it is validated** - at construction, at translation, at sealing, at
  persistence, or only at execution. Validation at a stage that does not run
  provides no protection.

An invariant that no production writer maintains is a `KNOWN GAP`, not a
property.

### 7. Negation semantics

Negation affects canonical form, fingerprints, validation, and replay. Do not
change negation semantics without an explicit architectural decision. Verify
current behavior from the implementation first, and confirm the effect on
canonical identity before touching it.

## Prohibited

- Assuming planner output is safe because it parsed.
- Introducing a new condition language.
- Changing branching without checking DAG validity, canonical form, program
  identity, fingerprint stability, and replay.
- Adding a field to canonical form without checking the fingerprint impact on
  already-stored programs.
- Introducing nondeterminism into canonical form.
- Claiming validation occurs without tracing the actual call in the running
  path.
- Claiming an observation invariant holds without identifying a production
  writer.
- Changing negation semantics absent a decision.
