---
name: kai-runtime
description: Manage KAI runtime execution - Gateway authority, policy pipeline ordering, verification, fulfillment, durable evidence, retry, resume, and replay.
when_to_use: When changing runtime execution flow, Gateway stages, verification or fulfillment, evidence persistence, or retry and replay behavior.
user-invocable: true
---

# KAI Runtime

Purpose: keep the Gateway the single execution authority, preserve the pipeline
order that makes approval and policy meaningful, and keep execution state
durable and honest.

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
- `Runtime` execution lifecycle and the Gateway's role
- policy evaluation within the execution pipeline
- ordering constraints between stages
- verification and fulfillment as distinct concerns
- evidence as durable execution state
- retry, resume, and replay semantics

Does not own:
- approval hashing and approver visibility -> `kai-approval`
- identity resolution -> `kai-identity`
- provider credentials -> `kai-google`
- program and condition semantics -> `kai-ir`

## Gateway is the execution authority

The Gateway is the only sanctioned path from an approved intent to a provider
call.

- **No bypass.** Do not add a shortcut, an internal helper call, a direct
  client invocation, or an alternate route that reaches a provider without
  passing through the Gateway pipeline. Such a path removes policy, approval,
  and evidence by construction.
- When you need new behavior, add it **as a stage within the pipeline**, so it
  inherits the same guarantees.

## Pipeline ordering - do not simplify without evidence

```
validation
-> policy/content
-> identity
-> approval check
-> TOCTOU re-evaluation
-> DENY
-> approval consume
-> execute
-> verify
-> evidence
```

Each stage exists for a reason. Notably:

- **TOCTOU re-evaluation sits between approval check and approval consume.**
  The world may have changed between a human approving and the system acting.
  Re-evaluation is what closes that window. Moving consumption earlier or
  later changes the safety property.
- **DENY precedes approval consume.** An approval is consumed only when the
  operation will actually proceed. Consuming on denial wastes the approval and
  changes retry behavior.
- **Consume precedes execute.** Consuming after execution permits a
  double-execution race.
- **Evidence follows execution** and is persisted as state.

Do not collapse stages, merge adjacent ones, or reorder them to simplify code.
If a stage appears redundant, prove it redundant with evidence from the code and
tests first.

## Verification is not fulfillment

Keep these separate. They answer related questions but are different facts.

| | Question | Nature |
|---|---|---|
| Verification | did the operation execute as intended? | an assessment of the attempt |
| Fulfillment | did the user's goal actually get achieved? | an assessment of the outcome |

They can disagree: a send can be verified as correctly executed while the
fulfillment audit finds the message undelivered or the recipient mismatched.
Do not unify them merely because both answer "did it work?". Unifying them
destroys the ability to report a successful execution that failed its goal.

## Evidence is durable state

- Evidence is **durable execution state**, not log output. It is persisted and
  queryable, and later decisions may depend on it.
- Never downgrade evidence to a log line, a metrics counter, or a transient
  in-memory record.
- Never write evidence that claims an outcome not actually observed.
- Evidence must survive process restart; a replay that cannot read prior
  evidence is broken.

## Retry, resume, replay

- **Retry** re-attempts a failed operation. It must not duplicate a completed
  side effect, and it must not skip guards that the first attempt passed.
- **Resume** continues an interrupted execution from its persisted cursor
  without re-executing completed stages.
- **Replay** reproduces a prior outcome.

**Replay does not raise authority.** A replayed execution is not a fresh
authorization. It does not create a new approval, does not re-run policy to
permit something previously denied, and does not escalate. Treat replay output
as a reproduction, clearly labeled as such.

## Prohibited

- Bypassing the Gateway to reach a provider.
- Reordering or collapsing pipeline stages without evidence.
- Consuming an approval on a denial path.
- Consuming an approval after execution.
- Merging verification and fulfillment.
- Treating evidence as logs.
- Letting retry or replay grant authority not present in the original
  execution.
- Fabricating or inferring evidence for an operation that was not observed.
