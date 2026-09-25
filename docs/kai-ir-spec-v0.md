# KAI-IR Specification v0

Status: **proposal / documentation only**. No compiler, runner, tables, endpoints,
parser, or runtime code implements anything in this document at the time of
writing (commit `b12e96d` is the current code baseline).

This document defines the intermediate representation (IR) that KAI will use to
describe agent intent, plans, actions, observations, and outcomes, so that
reasoning, execution, policy, evidence, and future interfaces (web, desktop
avatar, Telegram, voice) can share one machine-checkable contract.

## 1. Scope and non-goals

In scope for v0:

- A structural, serializable graph representation of "what the agent intends to
  do, why, under which permissions, with which results".
- Determinism rules that make a plan replayable, resumable, and auditable.
- A clear separation between *proposal* (Planner output) and *execution*
  (Runtime-driven), without prescribing how either is implemented.

Out of scope for v0:

- Compiling IR to a provider-specific call sequence.
- Optimizing, scheduling, or parallelizing IR nodes.
- Any new tool, provider, interface, or policy semantics.
- Any change to the Tool Gateway, Task state machine, approval flow, or runtime.

## 2. Design principles

1. **Data, not instructions.** IR is a validated data structure. A model may
   *propose* IR; only validated IR may be *executed*. The IR never carries
   executable code, prompts-as-code, or credentials.
2. **The contract is closed.** Every node and field is allow-listed. Unknown
   fields are rejected, never ignored (mirrors the existing `extra="forbid"`
   schemas).
3. **Deterministic identity.** A given intent always has the same identity and
   fingerprint. The same semantic IR always serializes to the same bytes and
   the same hash.
4. **Capability, not tool.** IR references abstract capabilities; mapping a
   capability to a concrete tool is the Gateway's concern, never the model's.
5. **Permission is data, not trust.** Permissions and constraints travel *with*
   the IR as declared constraints, but are only *enforced* by the policy engine
   and approval manager. Declaring a permission never grants it.
6. **Evidence is append-only.** Every executed node produces an observation; the
   full (internal) record is durable and never rewritten, only appended.
7. **Progressive capability.** v0 targets the capabilities that already exist in
   the system (contacts, calendar, gmail, memory, approval). Primitives for
   future capabilities (Computer Use, browser) are reserved but never fabricate
   a capability that does not exist.

## 3. IR top-level shape

A KAI-IR program (v0) is a directed graph of nodes plus metadata:

```
KaiProgram
  ir_version: "kai-ir/0"
  program_id: <uuid>              # identity of this program (not of a task)
  intent: IntentNode              # entry point, exactly one
  plan: PlanNode                  # the plan this program instantiates
  nodes: [Node]                   # remaining nodes, in declaration order
  edges: [ControlEdge]            # control/data flow
  constraints: PolicyConstraintsNode
  permissions: [PermissionNode]
  fingerprint: <sha256 hex>      # fingerprint of the canonical serialization
```

The canonical serialization is JSON with sorted keys, compact separators, and
UTF-8 (the same normalization the runtime uses for `plan_fingerprint`).
`fingerprint` is the content hash used for plan binding, change detection, and
audit correlation.

## 4. The fourteen primitives

The v0 primitive set is closed and ordered. Each primitive is listed with its
role and whether the current architecture already supports it.

### 1. `IntentNode` — entry point

What the user asked for, in structured form, plus the success criteria that
define "done". It carries no permissions and no tools.

- `goal`: the user's goal text.
- `success_criteria`: ordered list of verifiable conditions.
- `entry: true` (only the IntentNode is an entry).

Supported: yes — this is the current `Task.goal` + `success_criteria` contract
and the runtime `UnderstandingOutput` scenario.

### 2. `PlanNode` — the plan being executed

An ordered, bounded sequence of steps with a declared plan version. This is the
node that execution binds to.

- `plan_version`: integer, monotonic per task.
- `steps`: ordered list of `StepNode` references.
- `status`: `DRAFT | ACCEPTED | SUPERSEDED | EXECUTED`.

Supported: yes — this is the existing `TaskPlan` (version, status) and the
runtime's `plan_version` / `plan_hash` binding.

### 3. `StepNode` — one unit of work

A single ordered step within a plan: a capability call with inputs and an
expected output. A step is the unit the runtime cursor counts.

- `step_id`, `order`, `capability`, `inputs`, `expected_output`,
  `verification_hint`.

Supported: yes — this is the existing `PlanStep` shape and the runtime's
cursor/step accounting.

### 4. `ConditionNode` — a guard on control flow

A deterministic predicate over observations/state that chooses a branch. v0
restricts conditions to values already available at runtime (state, cursor,
observation outcomes). Arbitrary code in a condition is not allowed.

- `predicate`: a closed, whitelisted predicate language (e.g. `observation.outcome == EXECUTION_SUCCEEDED`, `cursor >= k`).

Supported: partially — the runtime currently has no branching; the task state
machine provides the only branching. A condition language is a new abstraction.

### 5. `ControlEdge` — directed transition

An explicit edge between nodes. The graph must be acyclic in v0.

- `from`, `to`, `when` (optional `ConditionNode` reference).

Supported: partially — the task state machine (`TRANSITIONS`) is the current
control-flow model; IR edges would be a second, finer-grained graph. They must
be kept consistent with the task state machine, not replace it blindly.

### 6. `ActionNode` — a capability invocation

The concrete thing to execute: an abstract capability plus validated arguments.
It is *not* a provider call; the Tool Gateway resolves capability → tool.

- `action_id` (deterministic), `capability`, `arguments`, `risk_hint` (declared, not authoritative).

Supported: yes — this is the current `ToolIntent` (abstract in, validated
arguments) and the runtime's `capability_to_tool` translation.

### 7. `ObservationNode` — a recorded result

The durable, internal record of one executed `ActionNode`: outcome, evidence,
verdict, and the request identity. Observations are append-only.

- `action_id`, `outcome` (ToolOutcome), `ok`, `verified`, `error_code`,
  `evidence` (full, internal), `request_id`, `recorded_at`.

Supported: yes — this is the existing runtime `evidence` list and the Gateway's
`ToolResult`.

### 8. `ResultNode` — the program's aggregate outcome

The single node describing how the program ended: completed, failed, or
requires attention, with the reason.

- `result` (COMPLETED | FAILED | REQUIRES_ATTENTION | ...), `reason`, `final_observation_ids`.

Supported: yes — this maps to the runtime's terminal outcomes and the task
state machine's terminal states.

### 9. `ErrorNode` — a structured, typed failure

A failure the program anticipated, with a classification and a declared
response (retry, escalate, or stop). v0 requires error classes to be explicit
so a failure is never an unstructured string.

- `error_code` (stable application string), `class` (see §6), `response`
  (STOP | RETRY | ESCALATE | ATTENTION).

Supported: partially — the system already has `error_code` on `ToolResult` and
distinct task failure states, but does not yet have a first-class structured
error taxonomy or a declared per-error response in the plan.

### 10. `MemoryOpNode` — read or write memory

An explicit memory operation scoped to the tenant/user, used as context
(never authority) or as a durable note.

- `op` (READ | WRITE), `memory_type`, `query`/`content`, `scope` (tenant+user
  enforced by the system, never by the IR).

Supported: yes — this is the existing `MemoryService` (retrieve / create), which
the runtime already uses for context.

### 11. `PermissionNode` — a declared permission requirement

Declares that an action needs a capability, a risk ceiling, an account, or
human approval. It is a *requirement declaration*, enforced by policy and
approval, and never a grant.

- `requires_capability`, `max_risk`, `requires_account`, `requires_approval`.

Supported: yes — this is the existing risk policy, approval manager, and
`requires_account` on `ToolDefinition`. The gap is expressing it as a node
rather than an implicit side effect of running the action.

### 12. `ApprovalNode` — a human-in-the-loop checkpoint

An explicit request for human approval, bound to the specific action and
arguments it gates. Approving one node must not implicitly approve another.

- `gates` (the ActionNode(s) it guards), `single_use: true`, `state`
  (PENDING | APPROVED | DENIED | REJECTED | EXPIRED | CONSUMED).

Supported: yes — this is the existing `Approval` model and its single-use
consume, which is already the mechanism that prevents duplicate execution after
a lost receipt.

### 13. `PolicyConstraintsNode` — the global rule envelope

The program-wide constraints that policy will evaluate: allowed capabilities,
risk ceiling, budget, deadline, and content rules.

- `allowed_capabilities`, `max_risk`, `budget`, `deadline`, `content_rules`.

Supported: yes — this is the current task contract (`allowed_tools`,
`risk_policy`, `max_steps`) and the policy engine's rules.

### 14. `StateSnapshotNode` — an explicit, versioned progress anchor

A bindable snapshot of "where the program is": the plan it is bound to, its
content hash, the cursor, and steps used. Execution may resume only from a
snapshot that matches the current plan.

- `plan_version`, `plan_hash`, `cursor`, `steps_used`, `bound_at`.

Supported: yes — this is exactly the current `RuntimeExecution`
(`plan_version`, `plan_hash`, `cursor`, `steps_used`) plus `_bind_plan`'s
refusal when the bound plan no longer matches.

## 5. Determinism, identity, and plan binding

- **Action identity.** Each `ActionNode` has a deterministic `action_id`
  derived from stable inputs (task, plan version, step, position) — never from
  wall-clock time or randomness. The same action therefore re-issues the same
  id on every resume, and different plan versions never collide.
- **Plan binding.** A `StateSnapshotNode` is bound to one `PlanNode` by
  `plan_version` + `plan_hash` (the content fingerprint). If the current plan
  no longer matches the bound plan, execution must stop and surface
  `REQUIRES_ATTENTION` — it must not rewind, guess, or execute.
- **Re-plan.** A new plan is only adoptable before any step has run. After the
  first step, a different plan is refused; the runtime never silently resumes a
  new plan against an old cursor.
- **Crash safety.** The claim/lease that serializes execution is committed
  before any external call and is never held as a lock across a provider call.

## 6. Error taxonomy (v0)

v0 classifies errors into a small, stable set. The class determines the
declared response of an `ErrorNode`:

```
PERMANENT   -> STOP (task FAILED)
TRANSIENT   -> RETRY (bounded by retry budget)
POLICY      -> STOP (denied; nothing was executed)
APPROVAL    -> STOP or ATTENTION (denied/rejected/expired)
IDENTITY    -> STOP (recipient not provably resolved)
BUDGET      -> STOP (step/cost budget exhausted)
INTERNAL    -> ESCALATE or STOP
```

`error_code` is always a stable application string, never a raw provider or
exception message. Raw provider text may be kept in *internal* evidence but
must not appear in the public projection.

## 7. Evidence model

Three strictly separated layers:

1. **Internal evidence (durable, append-only).** Full action record: arguments,
   provider payload, verification evidence, request id, timestamps. Needed for
   resume and verification. Never published directly.
2. **Public projection (redacted, read-only).** What an interface may show:
   what ran, which tool/step, the outcome, ok/verified, stable error code,
   request id. No arguments, no provider payload, no unmasked personal data.
3. **Audit/event data.** Correlatable identifiers (program id, plan version,
   action id, request id) for tracing across layers, without leaking payloads.

## 8. Execution model (v0, target)

```
IntentNode
   -> PlanNode (StepNode*)
        -> ControlEdge / ConditionNode  (branching; new in v0)
             -> ActionNode  (via Tool Gateway: policy -> approval -> execute -> verify)
                  -> ObservationNode
                       -> ResultNode / ErrorNode
```

The Tool Gateway remains the only path from an `ActionNode` to a provider. IR
does not call adapters directly and does not embed credentials, endpoints, or
transport details.

## 9. Compatibility with the current architecture

- The closed `IntentNode`/`PlanNode`/`StepNode`/`ActionNode`/`ObservationNode`/
  `PolicyConstraintsNode`/`PermissionNode`/`ApprovalNode`/`StateSnapshotNode`/
  `ResultNode` set maps directly onto schemas that already exist.
- `ConditionNode` and `ControlEdge` introduce a branching concept the current
  runtime does not have; the task state machine is the current (coarser) control
  flow and must remain authoritative for task state.
- `ErrorNode` formalizes error classes that today are implicit in
  `error_code` + task state.
- The Tool Gateway is unaffected: IR references capabilities, and the Gateway
  already owns capability → tool, policy, approval, and verification.
- A Computer Use tool, when it exists, is just another `ActionNode` capability
  behind the same Gateway; it does not require IR or Gateway redesign.

## 10. Versioning

- `ir_version` is a string (`"kai-ir/0"`). Breaking changes bump the minor
  segment; additive, backward-compatible changes bump the patch segment.
- A node is only valid for versions declared in this spec. Unknown node types
  are rejected at validation, matching the project's `extra="forbid"` posture.

## 11. Open questions (deliberately unresolved in v0)

- Should a `ConditionNode` predicate language be data-only, or may it reference
  a whitelisted expression evaluator? (v0: data-only.)
- Should `MemoryOpNode` writes be allowed inside a plan, or only as explicit
  post-conditions? (v0: allowed but always tenant/user-scoped and auditable.)
- What is the canonical serialization for graph ordering, so two structurally
  identical graphs always hash identically? (v0: declaration order of nodes and
  edges is part of the canonical form.)
- How does a future Computer Use session identity attach to an `ActionNode`
  without leaking it? (Unresolved; deferred.)
