---
name: kai-engineering
description: Engineering discipline for all KAI implementation work - minimal semantic change, explicit authority, preserved invariants, and evidence-based decisions.
when_to_use: Any change to KAI source, tests, migrations, or configuration. Load this alongside a domain skill when one applies.
user-invocable: true
---

# KAI Engineering Discipline

KAI is a **trusted execution substrate for personal AI agents**. Code in this
repository takes real actions on a user's behalf. Every change is therefore
judged by what it permits, not only by how clean it looks.

## Shared base rule (binding on every KAI skill)

**Actual code is the primary source of truth.**

Use this cognitive order before making any architectural claim or change:

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

Architecture documentation records intent. It never overrides observed
implementation. If a document and the code disagree, that disagreement is a
finding to classify, not a licence to edit code (see `kai-architecture-docs`).

## Scope

Owns:
- change discipline for any KAI implementation work
- what counts as sufficient evidence for a decision
- the invariants that must survive every change

Does not own:
- repository and working-tree protection -> `kai-state-integrity`
- reverse engineering the architecture -> `kai-architecture`
- reconciling documentation with code -> `kai-architecture-docs`
- test design and mutation analysis method -> `kai-test-and-mutation`
- domain semantics -> `kai-google`, `kai-approval`, `kai-identity`,
  `kai-runtime`, `kai-ir`

## Change discipline

1. **Minimal semantic change.** Fix the proven defect. Nothing more.
2. **No unrelated refactoring**, even where the surrounding code is clearly
   worse than you would have written it.
3. **No speculative abstractions.** Do not add a layer, registry, protocol, or
   framework before a concrete caller needs it.
4. **Explicit authority.** Name the component that owns the decision before
   changing it. If two components can answer the same question, either the
   design is wrong or you have not found the seam. Determine which before
   editing.

## Invariants that must survive every change

- **Tenant isolation** - no query, cache, or credential path may cross tenant
  boundaries. Scope at the query, not at the caller.
- **Approval boundaries** - approval gates a canonical intent and is consumed
  before execution. Never bypass, weaken, or reorder it.
- **Identity semantics** - the entity is the semantic unit; identifiers are
  attributes of it.
- **Durable evidence** - execution outcome is persisted state, not log output.
- **Replay semantics** - a replay reproduces an outcome; it never grants new
  authority.
- **Execution authority** - the Gateway remains the only path to a provider.

If a required change appears to violate one of these, the change is wrong until
proven otherwise. Do not resolve the tension by updating the invariant.

## Evidence standard

A decision is evidence-based when it cites at least one of:

- an implementation path (`path/to/file.py:123`)
- a test that fails before the change and passes after
- a recorded architectural decision

Absence of evidence is not evidence of absence. If you cannot cite, say so and
stop. Do not infer architecture from naming, folder layout, type hints, or
docstrings.

## Prohibited

- Changing code to match a document.
- Widening a change to "clean it up while we are here".
- Adding dependencies, configuration, or migrations without a proven need.
- Asserting an architectural fact not traceable to code, tests, or a decision.
- Editing Git history or discarding working-tree state (see
  `kai-state-integrity`).
- Reopening a closed question without new evidence (see `kai-approval` for the
  worked example).

## Definition of done

- The proven defect is fixed, and only that defect.
- Each invariant above is confirmed intact at a production seam.
- A test proves the behavior at the seam that matters, not at a helper.
- Any documentation claim relied on along the way has been checked against
  code.
