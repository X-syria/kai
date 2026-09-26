# KAI-IR Specification v0

Status: **partially implemented — living specification**.

This document began as a proposal. It is now a specification with a live
implemented subset, maintained by describing the code that exists rather than by
anticipating code that does not. Three terms are used precisely below, and they
are not interchangeable:

- **Implemented** — the primitive exists in `src/kai/ir/` and participates in the
  model as described. It does not mean every theoretical use is supported, and
  it does not mean the production Planner can produce one.
- **Deferred** — specified, but absent from the code. Deferred items are recorded
  as contract, not as a work order (§13).
- **Not applicable to v0** — the capability does not exist in the system at all.

The implemented subset arrived in two commits: `fe18947` (the execution slice)
and `7dae277` (control flow). **§8 is the authoritative control-flow contract**
and is normative; read it before changing anything under `src/kai/ir/`.

This document defines the intermediate representation (IR) that KAI will use to
describe agent intent, plans, actions, observations, and outcomes, so that
reasoning, execution, policy, evidence, and future interfaces (web, desktop
avatar, Telegram, voice) can share one machine-checkable contract.

## 0. Implementation status

| Primitive | Status | Implementation |
| --- | --- | --- |
| `IntentNode` | Implemented | `kai.ir.nodes.IntentNode` |
| `PlanNode` | Implemented | `kai.ir.nodes.PlanNode` |
| `StepNode` | Implemented | `kai.ir.nodes.StepNode` |
| `PolicyConstraintsNode` | Implemented | `kai.ir.nodes.PolicyConstraintsNode` |
| `PermissionNode` | Implemented | `kai.ir.nodes.PermissionNode` |
| `StateSnapshotNode` | Implemented | `kai.ir.nodes.StateSnapshotNode` |
| `ActionNode` | Implemented | `kai.ir.nodes.ActionNode` |
| `ObservationNode` | Implemented | `kai.ir.nodes.ObservationNode` |
| `ErrorNode` | Implemented | `kai.ir.nodes.ErrorNode` (§4.9) |
| `ResultNode` | Implemented | `kai.ir.nodes.ResultNode` |
| `ConditionNode` | Implemented | `kai.ir.nodes.ConditionNode` (§4.4) |
| `ControlNode` | Implemented | `kai.ir.nodes.ControlNode` (§4.15) |
| `ControlEdge` | Implemented | `kai.ir.nodes.ControlEdge` (§4.5) |
| `ApprovalNode` | Deferred | the `Approval` model exists; no IR node (§13) |
| `MemoryOpNode` | Deferred | `MemoryService` exists; no IR node (§13) |

Three rows need their reasoning stated rather than left to inference:

- **`ErrorNode` is implemented as a model, not as dispatch.** It carries the
  error class with its declared response and validates the two against each
  other, but execution does not yet walk error nodes: a failure still resolves
  through `error_code` and the task state machine (§4.9).
- **`ControlNode` was not in the original fourteen.** It is the
  representation-level addition described in §12.2, and it introduced no new
  authority.
- **"Implemented" does not imply reachable.** `ConditionNode` and `ControlNode`
  are fully live: the Planner declares branches, `program_from_plan()` compiles
  them, and the Runtime walks them on every step (§8.9).

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
- Any change to the Tool Gateway, the Task state machine, or the approval flow.
  The one coupling that does exist is narrow and specified in §8.6: the Runtime
  asks the program which step is next, and keeps every other authority.

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
6. **Evidence is append-only.** Every executed *action* produces an observation;
   the full (internal) record is durable and never rewritten, only appended. A
   `ControlNode` that decides or ends the flow executes nothing and produces
   none.
7. **Progressive capability.** v0 targets the capabilities that already exist in
   the system (contacts, calendar, gmail, memory, approval). Primitives for
   future capabilities (Computer Use, browser) are reserved but never fabricate
   a capability that does not exist.

## 3. IR top-level shape

A KAI-IR program (v0) is a directed control graph of `ControlNode` vertices plus
metadata:

```
KaiProgram
  ir_version: "kai-ir/0"
  program_id: <uuid>              # identity of this program (not of a task)
  intent: IntentNode              # entry point, exactly one
  plan: PlanNode                  # the plan this program instantiates
  nodes: [ControlNode]            # control-flow vertices, in declaration order
  edges: [ControlEdge]            # control flow, in declaration order
  constraints: PolicyConstraintsNode
  permissions: [PermissionNode]
  snapshot: StateSnapshotNode?    # progress anchor, bound to one plan version
  observations: [ObservationNode] # append-only log of what actually ran
  fingerprint: <sha256 hex>      # fingerprint of the canonical serialization
```

The model deliberately splits into two halves. `nodes` and `edges` are the
**static control structure**: what the program is willing to do, and in what
order, given facts it has not seen yet. `snapshot` and `observations` are the
**runtime record**: what happened when it ran. They are separate fields, and
they are treated differently by the fingerprint (§8.11).

Note that `IntentNode.entry` and the control graph's entry node are two
different things. `IntentNode.entry` marks the program as a whole as the IR's
single entry point, and is validated to be true. The *control* entry — where a
walk through `nodes` and `edges` starts — is derived from the graph and is never
declared (§8.2).

The canonical serialization is JSON with sorted keys, compact separators, and
UTF-8 (the same normalization the runtime uses for `plan_fingerprint`).
`fingerprint` is the content hash used for plan binding, change detection, and
audit correlation. It covers `ir_version`, `intent`, `plan`, `nodes`, `edges`,
`constraints`, and `permissions`; it deliberately excludes `snapshot`,
`observations`, and `fingerprint` itself.

## 4. The primitives

The v0 primitive set is closed and ordered: the fourteen from the original
proposal, plus `ControlNode` (§4.15), which the implementation added. Each is
listed with its role and its status; §0 is the summary table, and §13 lists what
is deferred.

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

A deterministic comparison over facts that already exist, used to choose a
branch. The condition is one flat object, not an expression:

- `condition_id`: stable identifier for the condition itself.
- `subject`: the single fact being read, as a `ConditionSubject`.
- `operator`: one of `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`.
- `value`: the literal operand — a `bool`, `int`, `str`, or a list of one of
  those.
- `negate`: `bool`, default `false` (§4.4.3).

#### 4.4.1 The fact vocabulary is closed

`subject.kind` is one of exactly four `FactKind` values, and each names one
scalar:

| `kind` | Type | Reads |
| --- | --- | --- |
| `observation` | depends on `field` | the last observation recorded for `step_order` |
| `cursor` | `int` | `RuntimeExecution.cursor` |
| `steps_used` | `int` | `RuntimeExecution.steps_used` |
| `task_state` | `str` | the task's current state |

An `observation` subject must additionally name both `step_order` and `field`,
where `field` is one of `outcome` (str), `ok` (bool), `verified` (bool), or
`error_code` (str). The other three kinds must name no step and no field: a
condition reads one fact, so there is no path expression to write.

This is an allow-list of scalars, not a path into a provider payload. A condition
cannot read an environment variable, call a tool, or ask the model, because the
vocabulary has nowhere to put any of those. A step-scoped observation subject
reads the **last** observation recorded for that step, which is that step's
outcome: a step runs its intents in order and stops on the first unsuccessful
one.

#### 4.4.2 Operands are type-checked against the fact at build time

The operator and the operand are validated against the referenced fact when the
program is built, not when a condition is evaluated. `verified == true` is
valid; `verified == "yes"` is refused with a validation error, rather than
becoming a condition that silently never matches. In detail:

- the operand's type must be exactly the fact's type — `bool` is not `int`;
- `in` and `not_in` require a non-empty, single-typed list whose members match
  the fact's type; every other operator requires a single scalar;
- `gt`, `gte`, `lt`, and `lte` are refused for `str` facts, where ordering has
  no defined meaning.

This is a deliberate property of the IR, not a convenience. Because the operand
type is settled before a program can be sealed, evaluation is a single
comparison that can only fail by asking about a fact that does not exist yet —
which is also refused rather than guessed (§8.5).

The condition language is therefore closed and data-only. There is no Python
expression, no scripting, no `eval`, no whitelisted expression evaluator, and no
model-generated executable logic. A condition is a comparison of a durable fact
against a literal, and nothing more.

#### 4.4.3 Negation is a flag, not a nested expression

Negation is `negate: bool` — not a `not` operator, and not a nested subtree. A
predicate and its complement are one flat object differing by a single boolean,
so the complement of "observation of step 2 has `ok == true`" is that same
object with `negate: true`. There is no second predicate object to declare, and
no expression tree to canonicalize. `ConditionNode.is_negation_of()` names that
relation structurally: same subject, same comparison, opposite polarity.

The consequence is what a two-way branch looks like — two edges whose guards
differ only in `negate`, which keeps a branch inspectable as a pair of opposites
without either side being a nested expression. No branch is *proven* exhaustive
by this in v0; see §8.14.

Supported: yes — implemented. The Runtime evaluates conditions on every step
(§8.6), and the vocabulary above is exactly `FactKind` and
`OBSERVATION_FACT_TYPES` in `src/kai/ir/nodes.py`.

### 5. `ControlEdge` — directed transition

One way from one control node to the next, plus the guard that permits it.

- `from_node`, `to_node`: node ids, each of which must name a declared
  `ControlNode`.
- `when`: an optional `ConditionNode`. Absent means unconditional.

An edge with no `when` is unconditional. An action node has exactly one
outgoing edge, and that edge is unconditional; a condition node may declare
several guarded edges, but all of its guards must read the same fact and no two
may be the same condition. Two unconditional edges leaving one condition node
are not refused at build time — they are ambiguous rather than contradictory —
and §8.4 refuses them when the runtime finds two eligible edges.

A self-loop (`from_node == to_node`) is refused at construction: a self-loop is a
one-node cycle, and cycles are refused outright (§8.13). The same pair of
endpoints may not be declared twice.

Edges belong to the **static IR program**, not to the runtime record. An edge
says "if this guard holds, control may pass this way"; it never records that
control *did* pass that way. Which edge was taken is not stored anywhere — it is
re-derived from durable facts on every walk (§8.12), so nothing about execution
leaks into the hashed structure (§8.11).

Supported: yes — implemented, and walked by the Runtime on every step. See §8.

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

`result` is restricted to the states the task state machine can actually report
as terminal, so a program can never declare an outcome the machine cannot
represent.

Supported: yes — this maps to the runtime's terminal outcomes and the task
state machine's terminal states.

### 9. `ErrorNode` — a structured, typed failure

A failure the program anticipated, with a classification and a declared
response (retry, escalate, or stop). v0 requires error classes to be explicit
so a failure is never an unstructured string.

- `error_code` (stable application string), `error_class` (see §6), `response`
  (STOP | RETRY | ESCALATE | ATTENTION).

Supported: yes as a model, not as dispatch. The class/response pairing from §6
is implemented and validated against each other (`PERMANENT` may not claim
`RETRY`), but execution does not yet walk error nodes: a failure still resolves
through `error_code` and the task state machine. Dispatching on the declared
response is deferred (§13).

### 10. `MemoryOpNode` — read or write memory

An explicit memory operation scoped to the tenant/user, used as context
(never authority) or as a durable note.

- `op` (READ | WRITE), `memory_type`, `query`/`content`, `scope` (tenant+user
  enforced by the system, never by the IR).

Supported: deferred — `MemoryService` (retrieve / create) exists and the runtime
already uses it for context, but there is no `MemoryOpNode` in the IR and no
step is dispatched to one (§13).

### 11. `PermissionNode` — a declared permission requirement

Declares that an action needs a capability, a risk ceiling, an account, or
human approval. It is a *requirement declaration*, enforced by policy and
approval, and never a grant.

- `requires_capability`, `max_risk`, `requires_account`, `requires_approval`.

Supported: yes — this is the existing risk policy, approval manager, and
`requires_account` on `ToolDefinition`, now expressed as a node rather than an
implicit side effect of running the action. Declaring it still grants nothing
(§2.5, §8.8).

### 12. `ApprovalNode` — a human-in-the-loop checkpoint

An explicit request for human approval, bound to the specific action and
arguments it gates. Approving one node must not implicitly approve another.

- `gates` (the ActionNode(s) it guards), `single_use: true`, `state`
  (PENDING | APPROVED | DENIED | REJECTED | EXPIRED | CONSUMED).

Supported: deferred — the `Approval` model and its single-use consume already
exist, and are already the mechanism that prevents duplicate execution after a
lost receipt. What is deferred is the IR node: approvals are requested by the
Gateway during execution, not named in advance by a plan (§13).

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
refusal when the bound plan no longer matches. Note that this is the *plan
binding* anchor, not the program's position in its control flow; that is derived
(§8.12, §12.4).

### 15. `ControlNode` — a vertex of the control-flow graph

Added by the implementation; §12.2 explains why. A `ControlNode` is a static
vertex with a discriminated `kind`:

| `kind` | Must carry | Means |
| --- | --- | --- |
| `action` | `step_order` | the plan step that runs here |
| `condition` | `condition` | a decision point |
| `result` | `result` | how the flow ends |

Each kind must carry its own payload and none of the others', so a node cannot be
an action and a condition at once.

A `ControlNode` executes nothing. It is a position in the declared graph, and it
is deliberately *not* the `ActionNode` its step eventually produces: an
`ActionNode`'s concrete arguments depend on what earlier observations
established, so it is built at execution time (§5). Naming the step rather than
copying one is what keeps runtime-filled data out of the hashed control
structure (§8.11).

`result` must be one of the task machine's reportable terminal states, so the IR
can never invent an outcome of its own. A result node is terminal: it has no
outgoing edge (§8.3).

Supported: yes — implemented. The Runtime walks these nodes and returns the
`StepNode` belonging to the first eligible `action` node (§8.6).

## 5. Determinism, identity, and plan binding

- **Action identity.** Each `ActionNode` has a deterministic `action_id`
  derived from stable inputs (task, plan version, step, position) — never from
  wall-clock time or randomness. The canonical form is
  `runtime-{task_id}-{plan_version}-{step_order}-{index}`, where `index` is the
  intent's position within its step. The same action therefore re-issues the
  same id on every resume, and different plan versions never collide. Branching
  does not participate in it: control flow routes to a step, and the branch
  taken changes when a step runs, not what it is (§8.10).
- **Control structure is part of identity.** `nodes` and `edges` are inside the
  fingerprint, so a program that can take a different branch is a different
  program. Runtime observations are outside it (§8.11).
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

## 8. Execution model and the KAI-IR Control-Flow Contract

```
IntentNode
   -> PlanNode (StepNode*)
        -> ControlEdge / ConditionNode  (branching)
             -> ActionNode  (via Tool Gateway: policy -> approval -> execute -> verify)
                  -> ObservationNode
                       -> ResultNode / ErrorNode
```

The Tool Gateway remains the only path from an `ActionNode` to a provider. IR
does not call adapters directly and does not embed credentials, endpoints, or
transport details.

### KAI-IR Control-Flow Contract

Everything in §8.1–§8.14 is **normative**. It is the contract any future
implementation of KAI-IR control flow must obey, and it is written to be checked
against code rather than read aspirationally. Where the current implementation
does not yet enforce something, that is recorded as deferred (§13) instead of
being written as though it were true.

#### 8.1 Static control structure vs runtime execution state

Two things are easily confused here, and the model keeps them in separate fields:

| | Static control structure | Runtime execution state |
| --- | --- | --- |
| Fields | `nodes`, `edges` | `observations`, `snapshot` |
| Answers | what may run next, given facts | what has already happened |
| In the fingerprint | yes | no |
| Changes when the program runs | no | yes |

The graph says a step *may* run next. Whether it *did*, and with what result, is
only ever in the runtime record. No part of the graph is a mutable cursor, and
nothing in the graph is written to during execution.

#### 8.2 Entry is derived, never declared

```
entry = the unique node in `nodes` with no incoming edge
```

If the number of such nodes is not exactly one, the graph is invalid. The entry
is computed from `edges` on every walk; it is not a field, and there is no way to
declare one.

This is deliberate. A separately declared entry could disagree with the graph's
own edges, and the program would then have two sources of truth for where it
starts — one the author wrote and one the structure implies. Deriving the entry
makes that state unrepresentable rather than merely discouraged.

This is distinct from `IntentNode.entry` (§3): that field marks the program as
the IR's single entry point, while the control entry is where a walk through the
graph begins.

#### 8.3 Graph validation contract

A program is executable only if its control flow satisfies all of the following.
Each check exists because, without it, some program would be executable while
having no defined next step.

- **Unique node ids.** No two control nodes share a `node_id`; no two conditions
  share a `condition_id`.
- **Valid node types.** Every `kind` is one of `action`, `condition`, `result`,
  and carries only its own payload (§4.15).
- **No dangling endpoints.** Every `from_node` and `to_node` names a declared
  node.
- **No self-loops, no duplicate edges.** `from_node != to_node`, and a given
  pair of endpoints is declared once.
- **Exactly one entry** (§8.2).
- **Complete reachability.** Every declared node is reachable from the entry. A
  node nothing can reach is dead structure, and a program carrying it is
  declaring a path it cannot take.
- **Acyclicity** (§8.13).
- **No ambiguous branch declaration.** Within a condition node: all of its
  guarded edges read the *same* fact — a compound condition is a chain of
  condition nodes, not one node with two subjects — and no two edges carry the
  same condition. Note where this check stops: two *unconditional* edges leaving
  one condition node are not refused here, because they are ambiguous rather
  than contradictory, and ambiguity is only decidable with the facts in hand.
  §8.4 refuses them.
- **One continuation per action node.** An action node has exactly one outgoing
  edge, and that edge is unguarded. A step's continuation is declared once; a
  branch after a step is expressed by a following condition node, not by
  guarding the step's own edge.
- **Result nodes are terminal.** A result node has no outgoing edge.
- **Every plan step appears exactly once.** The step orders carried by action
  nodes equal the step orders in the plan. A step the graph cannot account for,
  or accounts for twice, is refused.
- **Conditions read only facts that can exist where they are read.** `cursor`,
  `steps_used`, and `task_state` are always available. An `observation` subject
  names a step in this plan, and the reading node must be downstream of that
  step's action node.

The last rule deserves precision, because the obvious weaker version is wrong. It
is not enough that the referenced step is upstream on *some* path to the
condition. The check is a **backward search from the reading node** for the
producing action node, which asks whether *every* path arriving at the condition
passed through that step. A condition also reachable by a path that skips the
step could evaluate a fact that does not exist on that path, so the honest form
of the rule is the stronger one.

At run time the same rule is enforced again from the other direction: reading a
step that has no observation is refused, never satisfied by a record that happens
to be nearby (§8.5).

#### 8.4 Branch selection contract

For the current node under the current facts:

```
0 eligible edges  -> control-flow failure
1 eligible edge   -> follow it
>1 eligible edges -> control-flow failure
```

An edge is eligible when it has no `when`, or when its `when` evaluates true. On
ambiguity or absence, the runtime must never:

- take the first edge;
- take the first matching branch;
- fall back to a default branch;
- silently stop when nothing is eligible.

Every one of those would make the path taken a function of declaration order or
of a heuristic rather than of what happened, and a program whose behaviour
depends on which of two true guards the reader preferred is not deterministic.
Both failure cases stop the run — the Runtime fails the task with a control-flow
error — and neither is retried, because a graph that does not match the facts
will not match them on a second attempt.

#### 8.5 Condition evaluation contract

Evaluating a condition is a **pure decision over already-available facts**. It:

- is deterministic — the same condition and the same facts always give the same
  answer;
- performs exactly one comparison, against a literal operand whose type was
  already checked at build time (§4.4.2);
- reads only the facts it is given, and only from the closed vocabulary in
  §4.4.1;
- calls no model and no tool;
- performs no external I/O;
- mutates no state, Runtime or otherwise;
- grants no permission;
- performs no task state transition.

It follows that evaluating a condition *cannot acquire a capability or cause an
action*: it compares a durable fact against a literal, and that is the whole of
it. A fact that does not exist — an observation for a step that has not run — is
a control-flow failure, never a default, never `None`, and never a false-y
stand-in that would let a branch pass by accident.

#### 8.6 The control-flow execution loop

```
IR program
    ↓
durable cursor + evidence + step count + task state
    ↓
ControlFacts
    ↓
next_step()
    ↓
Runtime executes the returned step
    ↓
Tool Gateway (policy -> approval -> execute -> verify)
    ↓
ToolResult -> Observation appended to the evidence log
    ↓
rebuild ControlFacts from durable state
    ↓
repeat
```

`next_step()` returns a **`StepNode`, or `None` when the flow is finished** —
nothing more. It does not return, and must not be extended to return:

- a `ToolIntent` or an `ActionNode`;
- a permission, an approval, or a policy decision;
- a task state transition;
- a Gateway request;
- a model decision or a plan revision.

The Runtime asks the program which step is next, and then does all of that
itself, in its own order, under its own authority. Keeping the answer to "which
step" is precisely what lets the IR decide *order* without ever deciding
*effect*.

The Runtime is fail-closed around this: a condition error or a control-flow
error ends the task rather than falling through to the next step, because a
program that cannot be walked has no next step.

#### 8.7 Runtime authority

KAI-IR control flow owns exactly one question: given the facts, which step is
eligible next. It owns nothing else. The Runtime remains authoritative for:

- task lifecycle and the task state machine;
- durable execution claims, leases, and fencing;
- persistence of the cursor, step count, and evidence;
- evidence durability and the append-only log;
- task state transitions;
- recovery authority, including what a resume is allowed to assume.

The IR has no way to express any of these and no way to override them. A program
whose flow is finished has stated that no step remains; it has not completed the
task, and only the Runtime decides that.

#### 8.8 Tool Gateway authority

Control flow does not bypass, wrap, or replace the Tool Gateway. The Gateway
remains authoritative for capability authorization, policy, approvals, account
selection, credentials, verification, and the actual tool execution. Therefore:

- a condition evaluating true grants nothing;
- a `ControlEdge` being selected grants nothing;
- an `ActionNode` executes nothing by existing;
- a branch is not a way around policy, and a step is not a permission.

A branch may only decide *which declared step runs next*. Every step it selects
is then executed exactly as it would have been on the linear path: through the
same Gateway, under the same policy checks, with the same approval handling.

#### 8.9 Planner boundary

The production Planner emits a `PlanProposal` whose `branches` field is an
**optional** array of two-way forks, and `program_from_plan()` builds the
corresponding control graph from it:

```
Planner
  ↓
PlanProposal { steps[1..n], branches[] }        branches[] omitted → no forks
  ↓
KAI-IR control graph
```

The Planner is asked for a fork only when the step that comes next genuinely
depends on a fact that is already known. It does not describe a graph, and it
cannot: the declaration has four fields and no vocabulary for anything else.

**A declared branch** (`PlanBranch`) is:

| Field | Meaning |
| --- | --- |
| `after_step` | the step the fork follows, by `order` |
| `condition` | one `PlanCondition` (§4.4's vocabulary, unchanged) |
| `then_step` | the step the condition being *true* leads to |
| `else_step` | the step the condition being *false* leads to |

`condition` carries `kind`, `operator`, `value`, `negate`, and — for
`kind: "observation"` — `step_order` and `field`. These are the same closed sets
a `ConditionNode` accepts, used directly: **the Planner gets no looser condition
language than the IR has**, and a declaration the IR cannot represent is refused
by `ConditionNode`'s own validation rather than translated into something wider.

**The partition rule.** Exactly one of `then_step`/`else_step` must be
`after_step + 1`; the other must be a later step, and those are the only two that
may run. This is not a style rule. A fork that skipped nothing is not a fork, and
a fork that skipped a step in the middle would leave that step on no path at all
— dead structure, which §8.3 refuses. Requiring one target to be the natural
continuation is exactly the condition under which every declared step stays on
exactly one acyclic path.

**Translation** (`graph_from_plan()`) is therefore mechanical and adds nothing:

- a plan with no branches gets `linear_graph(steps)` — the identical graph, and
  the identical `program_id` and fingerprint, it has always produced;
- each branch becomes one `ControlNode` of kind `condition` carrying the
  declared `ConditionNode`, reached from `action-{after_step}` by an
  unconditional edge;
- its two outgoing edges carry the declared condition and **the same condition
  negated** — the only two-way shape §8.4 can prove covers every value of the
  fact. The Planner names where each outcome *goes*; the predicate decides
  nothing more than that;
- a step whose successor is a branch target ends its chain there and runs into a
  `result` node, so each path has exactly one end.

**Two gates, one rule set.** A declaration that cannot become a graph is refused
where it is offered: `domain_errors()` calls `branch_errors()` and compiles each
condition, so a malformed branch is rejected when the plan is *accepted* rather
than surfacing as an unexecutable program mid-task. The program is then built and
validated anyway (§8.3, §8.5), because the same rule failing twice is cheaper
than a plan that was accepted and could not run. There is no weaker
Planner-specific check to drift out of step with the IR's own.

**What the Planner still cannot say.** No `else`/`end`/`goto`/`loop`, no free-form
graph, no cycles (§8.13), and no way to express permission, risk, capability, or
tool choice: those remain absent from `PlanBranch` by construction, exactly as
they are absent from every other part of the plan.

**Honest limit of this slice.** The Runtime stops a run at the first step whose
outcome is not `EXECUTION_SUCCEEDED`, so a branch that would read a *failed*
step is not reachable in production today — a plan that declares one is valid and
simply never fires. The facts that do vary mid-run are `cursor`, `steps_used`,
and `task_state`; the observation facts are constant on every path that reaches a
fork, so today a declared branch selects *which planned steps run* rather than
reacting to a failure. Both polarities of a branch are exercised at program level
in `tests/test_kai_ir.py`; the end-to-end tests in `tests/test_runtime.py` prove
the production path end to end.

#### 8.10 Action identity under branching

Control flow routes to a **plan step**. It does not define, redefine, or
participate in action identity. The canonical action id is:

```
runtime-{task_id}-{plan_version}-{step_order}-{index}
```

where `index` is the intent's position within its step. It derives from the task,
the plan version, the step, and the position — never from the clock, a random
source, or the branch taken.

The invariant: **branch selection must not change the identity of the action.**
Which edge is followed decides *when* a step runs, not *what it is*. That
preserves:

- **evidence correlation**, because an observation still correlates to the same
  action id the Gateway already used;
- **Gateway request identity**, including a request already in flight or already
  recorded;
- **approval intent identity**, so an approval continues to cover exactly the
  action it was granted for;
- **recovery semantics**, because a resume re-derives the same id for the same
  step.

#### 8.11 Fingerprinting the control structure

The static control structure is inside the fingerprint. The canonical form is:

```
ir_version, intent, plan, nodes, edges, constraints, permissions
```

Declaration order is part of the canonical form: nodes and edges are emitted as
declared and are not re-sorted, so reordering them changes the fingerprint.

The runtime record is outside it. Excluded: `observations`, `snapshot`,
`fingerprint` itself, and every transient execution value — timestamps, claim or
worker identity, the current cursor, task state. A program is identified by what
it intends to do, not by what happened when it ran, so a plan that ran twice, or
resumed once, hashes identically to the plan that has not run at all.

The invariant:

```
same declared IR program + same canonical structure = same fingerprint
```

regardless of runtime observations.

This is also why a control-flow vertex references a step rather than copying a
step's arguments (§4.15): arguments filled in during execution would otherwise
make a program's identity depend on the run that produced them.

#### 8.12 Recovery

The control position is **derived**, on every attempt, from durable state:

```
ControlFacts(cursor, task_state, steps_used, observations)
    ← RuntimeExecution row + the persisted evidence log
```

A walk always starts from the derived entry (§8.2) and re-evaluates every guard
from those facts; it never trusts a remembered position. The principle is:

> resume derives from durable state

not

> resume trusts a separately persisted IR position.

Two properties follow. A resume cannot inherit a stale position, because there
is no position to inherit. And the branch taken after a resume is the branch
those same facts would have selected before the crash, so a crash cannot
silently change the program's path.

No new storage was added for this and none is specified: the existing
`RuntimeExecution` row and the evidence log already carry every fact the
vocabulary in §4.4.1 can name. Re-deriving the walk on every step is affordable
because the walk is bounded by the node count (§8.13).

#### 8.13 Acyclicity

The control graph is acyclic, and a cycle is refused at validation. The
contract-level reasons:

- **Bounded traversal.** Every walk terminates within the node count, so "what is
  next?" is always answerable in bounded work.
- **Deterministic termination.** A cyclic graph may simply never reach a result
  node, and "never finishes" is not a state a task can be in.
- **Action identity stays meaningful.** Revisiting a step re-issues its action
  id — the same id the Gateway already used — so the program's identity would
  stop meaning *exactly once*, taking evidence correlation and approval
  single-use down with it.
- **Existing recovery assumptions hold.** Resume reasoning assumes the program
  advances.

Loop semantics are not designed here. Any future loop form (a counted `for`, a
bounded retry) is future work, and would have to be specified together with how
it prevents a re-entered step from re-issuing an action id.

#### 8.14 Branch exhaustiveness

Static validation does **not** prove that a set of guards partitions the domain
of a fact. Deciding that is a decision procedure, not a structural check, and v0
declines to build one: there is no SAT solver, no domain-partitioning engine,
and no `is_negation_of`-based exhaustiveness proof in the validator.

The `negate` flag (§4.4.3) keeps a predicate and its complement comparable
structurally, so the shape such a proof would need is expressible — but nothing
today consumes that to accept or reject a program.

Exhaustiveness is instead enforced at run time by §8.4, where the facts are
known:

- zero eligible branches → failure;
- one eligible branch → continue;
- more than one eligible branch → failure.

This is a deliberate division of labour: static validation refuses what it can
prove is structurally wrong, and run-time selection refuses everything else,
where the facts exist. A program whose guards do not cover a case it can actually
reach therefore fails when it reaches that case, not at build time.

## 9. Compatibility with the current architecture

- The closed `IntentNode`/`PlanNode`/`StepNode`/`ActionNode`/`ObservationNode`/
  `PolicyConstraintsNode`/`PermissionNode`/`StateSnapshotNode`/`ResultNode` set
  maps directly onto schemas that already exist. `ApprovalNode` and
  `MemoryOpNode` remain deferred as IR nodes, though the underlying system
  capabilities they would name already exist (§13).
- `ConditionNode`, `ControlNode`, and `ControlEdge` are implemented, declared by
  the Planner (§8.9), compiled by `program_from_plan()`, and walked by the
  Runtime. Branching is a *second* control-flow model, at step granularity, and it
  does not replace the task state machine: that machine remains authoritative for
  task state (§8.7).
- `ErrorNode` is implemented as a validated model, while the error classes
  execution still derives from `error_code` + task state.
- The Tool Gateway is unaffected: IR references capabilities, and the Gateway
  already owns capability → tool, policy, approval, and verification.
- A Computer Use tool, when it exists, is just another `ActionNode` capability
  behind the same Gateway; it does not require IR or Gateway redesign.

## 10. Versioning

- `ir_version` is a string (`"kai-ir/0"`). Breaking changes bump the minor
  segment; additive, backward-compatible changes bump the patch segment.
- A node is only valid for versions declared in this spec. Unknown node types
  are rejected at validation, matching the project's `extra="forbid"` posture.

## 11. Open questions

Two of these are now settled by the implementation; they are marked **Resolved**
and point at the normative text rather than being deleted.

- Should a `ConditionNode` predicate language be data-only, or may it reference
  a whitelisted expression evaluator? **Resolved: data-only**, and now normative
  rather than a preference — the closed fact vocabulary, the flat `negate` flag,
  and build-time operand checking are specified in §4.4 and §8.5.
- Should `MemoryOpNode` writes be allowed inside a plan, or only as explicit
  post-conditions? (v0: allowed but always tenant/user-scoped and auditable.
  The primitive itself is deferred — §13.)
- What is the canonical serialization for graph ordering, so two structurally
  identical graphs always hash identically? (v0: declaration order of nodes and
  edges is part of the canonical form. Implemented, and specified in §8.11.)
- How does a future Computer Use session identity attach to an `ActionNode`
  without leaking it? (Unresolved; deferred.)

## 12. Divergences from the original v0 proposal

This document started as a proposal, and parts of the code still cite it by
section number. Where the implementation settled a question differently from the
proposal, the implemented behaviour is now the specification, and the difference
is recorded here rather than quietly overwritten.

Note for editors: because source docstrings cite this document by number, the
existing section numbers are stable. New material is appended (§12, §13) or added
as a subsection (§8, §4.4, §4.15) rather than inserted, and renumbering would
silently invalidate those citations.

### 12.1 `ConditionNode`: one `predicate` became four fields

The proposal described a `predicate` — "a closed, whitelisted predicate
language", with examples. The implementation models a condition as four explicit
fields, `subject`, `operator`, `value`, `negate`, with the fact vocabulary
closed by a `FactKind` enum and the comparison closed by a `ConditionOperator`
enum.

The change is from prose describing a language to fields describing one
comparison, which is what makes the language inspectable, type-checkable at build
time, and hashable without interpretation. The intent is unchanged: data only,
no code (§4.4).

### 12.2 `ControlNode` was added

`ControlNode` is a fifteenth primitive that the proposal did not have; the
proposal described control flow entirely in terms of `ControlEdge` between the
existing node types.

It exists at the representation level because an edge list alone cannot say what
kind of thing an endpoint is, and several guarantees in §8.3 are statements about
vertex kinds: that a result node is terminal, that an action node has exactly
one unguarded continuation, and that a condition node's guards read a single
fact. Each of those needs a typed vertex to attach to.

Adding it introduced no new authority. A `ControlNode` executes nothing and
grants nothing; it is a position in a graph, and §8.7 and §8.8 are unchanged by
its existence.

### 12.3 `ErrorNode.class` is `error_class`

`class` is a reserved word in Python, so the field is named `error_class`. The
class/response table in §6 is unchanged.

### 12.4 `StateSnapshotNode` is the plan anchor, not the program position

The proposal's `StateSnapshotNode` reads like the place a program's position
lives. It is not. It is the bindable plan anchor (`plan_version` + `plan_hash`),
validated against the plan the program instantiates. Control position is derived
from the Runtime execution row, and the snapshot is not part of the fingerprint
(§8.12).

## 13. Deferred items

Specified, not implemented. Recorded as contract so that a future change knows
what it must satisfy. None of these is a work order, and none should be read as a
commitment to a particular design.

| Item | Why it is deferred | What exists today |
| --- | --- | --- |
| `ApprovalNode` (§4.12) | approvals are requested by the Gateway during execution, not named in advance by a plan | the `Approval` model and its single-use consume |
| `MemoryOpNode` (§4.10) | memory is context for a step, not a step of its own | `MemoryService` (retrieve / create) |
| A branch on a *failed* step (§8.9) | the Runtime stops a run at the first non-successful step, so such a branch can never fire | branches on `cursor` / `steps_used` / `task_state` and on the outcomes of steps that succeeded |
| Exhaustive guard partition validation (§8.14) | it is a decision procedure, not a structural check | run-time "exactly one eligible edge" (§8.4) |
| Cycles / loops (§8.13) | a cycle breaks action-id exactly-once and current recovery assumptions | acyclicity enforcement |
| Execution-time dispatch of `ErrorNode` (§4.9) | error routing is still the task state machine's | the validated `ErrorNode` model |
| Computer Use session identity on an `ActionNode` (§11) | the capability does not exist in the system | nothing — deliberately not fabricated |

Until one of these is implemented, the corresponding guarantee in this document
holds exactly as far as §8 states, and no further.
