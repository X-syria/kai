---
name: kai-test-and-mutation
description: Tests and mutation analysis that prove real invariants - production-seam coverage, rejection of tautological tests, and killed mutants for critical invariants.
when_to_use: When writing or reviewing tests, when validating a fix, or when asked whether a behavior is genuinely verified.
user-invocable: true
---

# KAI Test and Mutation Discipline

Purpose: ensure tests prove **invariants**, not implementation details. A test
that passes while the system is broken is worse than no test, because it
converts an open risk into a false assurance.

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

A test is evidence only about the behavior it actually reaches on the actual
code path.

## Scope

Owns:
- how tests are chosen and written
- production-seam coverage
- mutation analysis for critical invariants
- judging whether a proof is real

Does not own:
- what to change in production -> `kai-engineering`
- what the behavior should be (domain) -> the domain skills
- repository safety during mutation runs -> `kai-state-integrity`

## Rules

### 1. Prefer production-seam tests

Test through the same entry point, middleware, dependency injection, and
pipeline that production uses. A test that calls an internal helper directly
proves only that the helper behaves as the test expects.

### 2. A helper test is not evidence of production behavior

Calling a private function and asserting its return value tells you nothing
about whether the Gateway, middleware, or router actually invokes it that way.
Do not cite helper tests as proof of a production claim. If only a helper is
reachable from the test environment, record that as a coverage limitation
rather than as evidence.

### 3. Reject tautological tests

A test is tautological when it restates the implementation and therefore passes
regardless of correctness:

- asserting a value against the same function that produced it
- asserting a hash equals a hash computed by the code under test
- asserting a mock was called exactly as the test itself invoked it
- asserting a constant against itself
- asserting a serialized form round-trips through the same serializer

The test must be able to fail for a real reason. Before accepting any test,
ask: **what change to the code would make this test fail?** If the answer is
none that matters, the test proves nothing.

### 4. Mutation testing for critical invariants

For each critical invariant, mutate the enforcing code and confirm the test
suite catches it. A critical invariant with zero surviving-mutant evidence is
not verified.

Follow the mutation cycle in `kai-state-integrity`: snapshot, mutate, test,
restore to exact original bytes, verify by checksum.

### 5. Killed mutation vs collateral failure

A mutant is **killed** when a test fails that is *specifically sensitive to the
mutated behavior*.

A mutant may instead produce a **collateral failure** - an unrelated test that
fails for incidental reasons (import error, shared fixture, unrelated
assertion, cascade from a broken setup). This is not a kill. It tells you the
suite is brittle, not that the invariant is protected.

Every claimed kill must name the specific test that failed and why that test
targets the mutated behavior. If you cannot name it, the mutant survived.

### 6. Cover the behavior that actually matters

Where relevant to the change, add explicit coverage for:

- **failure** - errors propagate as typed failures, not as silent success
- **retry** - retries do not duplicate side effects or bypass guards
- **replay** - replay reproduces the outcome without granting new authority
- **authorization** - unauthorized paths are refused, not merely logged
- **tenant isolation** - cross-tenant access is impossible at the query level

## Closed hypotheses - do not reopen without new evidence

**M8 - previously hypothesized, then disproven.** An earlier hypothesis
claimed the Gateway rebuilds `arguments` after approval consumption, which
would mean execution could differ from what was approved.

This was disproven. The current code carries the **same** `arguments` through
the sequence:

```
check -> consume -> execute
```

Because the arguments are not reconstructed between approval consumption and
execution, the hypothesized defect does not exist in the current code.

Therefore:

- Do not describe M8 as a current bug.
- Do not "fix" M8.
- Do not write a test asserting a defect that was disproven.
- Reopen it only if new evidence shows arguments are rebuilt or substituted
  somewhere in that path - and cite that code.

This is a general principle: a disconfirmed hypothesis stays closed. Record it
as closed and move on rather than re-deriving it from memory.

## Prohibited

- Reporting a mutant as killed on the strength of a collateral failure.
- Treating a passing helper test as verification of production behavior.
- Writing a test whose failure mode is unrelated to the claim it supports.
- Adding tests that pass before and after the change (they constrain nothing).
- Deleting or weakening an existing test to make a change pass.
- Running mutation analysis without restoring the original bytes (see
  `kai-state-integrity`).
