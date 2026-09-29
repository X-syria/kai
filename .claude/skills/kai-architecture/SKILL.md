---
name: kai-architecture
description: Reconstruct KAI architecture from the actual implementation - entry points, call graph, data flow, persistence, external calls, state machines, and invariants.
when_to_use: Before major architectural work, subsystem investigations, or whenever the architecture may be stale or uncertain.
user-invocable: true
---

# KAI Architecture: Reverse Engineering

Purpose: derive what KAI **is**, from what KAI **does**. This skill produces an
evidence-backed model of the current system. It does not change the system.

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

No step may be skipped by asserting it. If a step could not be inspected, say
so explicitly.

## Scope

Owns:
- discovering the real structure of a subsystem
- extracting the authority map and the invariant set
- separating what the implementation does from what documentation claims

Does not own:
- changing anything -> `kai-engineering`
- writing or updating documents -> `kai-architecture-docs`
- reasoning about a specific domain's semantics -> the domain skills

## Investigation procedure

### 1. Entry points and callers

Start from externally reachable surfaces: HTTP routers, CLI entry points,
background workers, event handlers. For each, walk its callers upward. An entry
point with no traced caller is dead or a gap.

### 2. Writers and readers

For every persisted concept, list every write site and every read site. A read
with no writer, or a writer with no reader, is a finding. Enumerate all of
them - grep for the model, the column, and the field name, because writes are
routinely spread across services, API layers, and migrations.

### 3. Data flow

Trace a value from its origin to its terminal effect. Note every place it is
transformed, defaulted, coerced, serialized, or re-derived. Pay attention to
values that are computed twice from different sources.

### 4. Persistence

Map models to tables and migrations. Establish which columns are authoritative,
which are derived, and which exist only for querying. Confirm what happens on
schema change and on partial failure.

### 5. External calls

Enumerate every outbound call: provider APIs, OAuth, HTTP clients, model
providers. For each: where credentials come from, where timeouts and retries
live, and what is done on `401`, `429`, and `5xx`.

### 6. State machines

Identify every stateful object and its legal transitions. Record who triggers
each transition, what guards it, and whether a transition can occur twice or
not at all. Illegal and impossible transitions are findings.

### 7. Production seams

Distinguish the code path a real request takes from test-only or stub paths.
A behavior verified only on a stub path is not a production behavior. Record
every place where production code and test code diverge.

### 8. Tests

Treat tests as evidence about intent, not about behavior. For each subsystem,
identify what is asserted, what is only exercised, and what is untested. A
green suite is not proof of an invariant.

## Outputs

### Authority map

For each decision point, name exactly one owner:

| Decision | Owner | File:line | Alternative candidates rejected |
|---|---|---|---|

If two owners are listed, that is an unresolved conflict, not a design.

### Invariant set

State each invariant as a falsifiable claim plus the evidence that holds it:

- the claim
- the code path that enforces it
- the test that would fail if it broke
- the observed behavior when violated

An invariant with no enforcing code path is an aspiration. Label it as such.

## Distinguishing implementation from documentation

For every claim, record one of:

- **Implemented** - traceable to code, with `file:line`.
- **Tested** - implemented and covered by a test that fails without it.
- **Documented only** - appears in a document with no supporting code.
- **Stale** - code contradicts the document.
- **Unknown** - not yet established.

Never collapse "documented only" into "implemented".

## Prohibited

- Refactoring for appearance. This skill is read-only with respect to
  production code.
- Inferring architecture from directory names, module names, or docstrings
  without reading the implementation.
- Accepting a document's account of a subsystem without tracing the code.
- Filling gaps in the model with the design you would have preferred.
- Reporting a subsystem as understood while any of the eight steps above is
  unexamined.
