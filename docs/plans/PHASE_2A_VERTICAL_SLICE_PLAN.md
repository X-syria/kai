# PHASE_2A_VERTICAL_SLICE_PLAN.md — Schedule-a-Meeting / Calendar Vertical Slice Inspection Plan

**Purpose:** Planning-only assessment. Determine whether the Schedule-a-Meeting/Calendar vertical slice actually satisfies Phase 1 requirements end-to-end with concrete evidence.

**Status:** PLANNING ONLY — NO REPOSITORY MODIFICATIONS PERFORMED

## 1. Goal and Scope

- Inspect the existing Calendar vertical slice (runtime/governed path) against Phase 1: `Intent → Contract → Planning/KAI-IR → Policy → Approval → Identity → Action → Evidence → Evaluation → Fulfillment → Recovery`.
- Do not assume classes/tests imply completeness. Require evidence per stage.
- Focus on Calendar only. No expansion to Gmail/Contacts/other integrations.
- Governance: respect Core vs Extension boundary; do not reopen closed decisions.

## 2. Path Under Inspection

Full governed chain: `Intent → Contract → Planning/KAI-IR → Policy → Approval → Identity → Action → Evidence → Evaluation → Fulfillment → Recovery`

## 3. Files and Components to Inspect

### Core Runtime/Lifecycle
- `src/kai/runtime/service.py` (AgentRuntime, trigger/execute, transitions, cursor binding to plan_version/plan_hash)
- `src/kai/runtime/understanding.py` (Understanding, scenario validation, extra='forbid')
- `src/kai/runtime/translator.py` (translator/vertical slice contract)
- `src/kai/runtime/schemas.py` (RuntimeExecution/Task schemas)
- `src/kai/runtime/evidence.py` (Evidence qualification rules; must read event_id from evidence['event_id'])
- `src/kai/runtime/fulfillment.py` (Typed criteria, tri-state FULFILLED/NOT_FULFILLED/INDETERMINATE)
- `src/kai/runtime/models.py`, `src/kai/runtime/send_evidence.py`
- `src/kai/tasks/state.py`, `src/kai/tasks/service.py`, `src/kai/tasks/models.py` (state machine, transitions, WAITING_EXTERNAL behavior)

### Planning/KAI-IR
- `src/kai/planner/service.py`, `src/kai/planner/schemas.py`
- `src/kai/ir/program.py`, `src/kai/ir/nodes.py`, `src/kai/ir/build.py`, `src/kai/ir/control.py`, `src/kai/ir/adapter.py`

### Governance/Gateway
- `src/kai/tools/gateway.py` (pipeline: auth, identity assert, reserved args, resolve/validate extra='forbid', policy, content, approval, account guard, **final TOCTOU recheck**, **atomic single-use approval consume**, execute, verify)
- `src/kai/tools/intent.py`, `src/kai/tools/registry.py`, `src/kai/tools/boundaries.py`, `src/kai/tools/definition.py`, `src/kai/tools/execution.py`, `src/kai/tools/adapters/calendar.py`
- `src/kai/policy/service.py`, `src/kai/policy/rules.py`, `src/kai/policy/models.py`
- `src/kai/approvals/` (models/service)
- `src/kai/identity/` (service/models/schemas)

### Calendar/Providers
- `src/kai/calendar/` (client/service) — inspect call paths vs governed vs direct
- `src/kai/api/runtime.py`, `src/kai/api/calendar.py` (to distinguish Governed Core Path vs Direct/Extension)

### Persistence
- `src/kai/db/` models, `alembic/versions/` relevant to runtime/task/plan/cursor/evidence
- Migrations affecting RuntimeExecution, TaskPlan, Task, approvals

### Tests (evidence)
- `tests/test_runtime.py`, `tests/test_kai_ir.py`, `tests/test_task_state.py`, `tests/test_fulfillment.py`, `tests/test_evidence_semantics.py`, `tests/test_calendar.py`, related
- Search for calendar meeting scenario

### Docs (closed decisions)
- Canonical: `docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md`
- Decisions: `docs/archive/KAI_ARCHITECTURAL_DECISION_*.md`, worktree originals (reference)
- Specs: `docs/kai-ir-spec-v0.md`
- Roadmap: `docs/ROADMAP.md`, Governance: `AGENTS.md`

## 4. Expected State Per Stage

### 1) Intent
- Understanding validates with extra='forbid' (intent='schedule_meeting', bounded guests/window)
- Model output treated as data only; stored in RuntimeExecution.scenario
- Evidence: schema validation, rejection on unknown fields

### 2) Contract
- Sealed contract semantics via translator (calendar_meeting profile). Plan acceptance binds plan_version/plan_hash
- Evidence: Task/TaskPlan persisted with versioning; immutable post-creation where applicable

### 3) Planning / KAI-IR
- Planner produces plan; IR built via `program_from_plan()` with deterministic `program_id` (uuid5(task_id+plan_version)), canonical fingerprint
- Cursor bound to plan_version+plan_hash; completed steps never re-executed
- Evidence: TaskPlan rows, IR canonical form/fingerprint

### 4) Policy
- Policy decision allow/deny/require_approval; enforced via ToolGateway pipeline
- Evidence: policy result recorded in context of execution path

### 5) Approval
- Human approval explicit, intent-bound, single-use; consumed atomically (final TOCTOU recheck before consume)
- Evidence: Approval state transitions, single-use enforcement

### 6) Identity
- Identity resolution (context-only hints from memory); cannot be silently substituted; asserted in gateway
- Evidence: identities recorded in RuntimeExecution

### 7) Action
- Side effects only via ToolGateway on governed path. ToolIntent from IR ActionNode (action_id/request_id linkage). Account guard applied
- Evidence: ToolIntent construction, request linkage

### 8) Evidence
- Observations recorded after ToolResult; qualification requires tool='calendar.create_event', outcome=EXECUTION_SUCCEEDED, ok/verified true, provider event_id from `evidence['event_id']` only
- Append-only; durable
- Evidence: observation/evidence records

### 9) Evaluation
- Verification via ContractVerificationEngine/stubs as applicable; verification required for success reporting
- Evidence: verification outcome

### 10) Fulfillment
- Typed criteria (CalendarMeetingCriteria), tri-state: FULFILLED/NOT_FULFILLED/INDETERMINATE. Evaluators pure. Qualification≠fulfillment
- Evidence: fulfillment verdict, witness set from qualifying_creations

### 11) Recovery
- Claim semantics: lease 5min, heartbeat 30s; claim lost → abort without writing. Cursor advances only after success. WAITING_EXTERNAL no return to EXECUTING (REQUIRES_ATTENTION→RETRY). INDETERMINATE→REQUIRES_ATTENTION. Replay from durable facts
- Evidence: claim_token/claimed_at/heartbeat, cursor state, resume behavior

## 5. Proof Requirements Per Stage (Evidence to Collect)

For each stage, collect:
- **Implementation evidence:** specific symbols/files/lines (file_path:line refs)
- **Runtime evidence:** how it appears in Task/RuntimeExecution/observations
- **Test evidence:** relevant test names + what they prove/not prove
- **Persistence evidence:** DB fields/tables (Task/TaskPlan/RuntimeExecution/observations/evidence/approvals)
- **Missing evidence:** concrete gaps

Classify: **PROVEN / PARTIALLY PROVEN / NOT PROVEN** per stage. Do not use PROVEN without explicit evidence.

## 6. Potential Gaps to Prove (not assume)

- Calendar slice end-to-end path: is create event exercised through governed path (IR→ToolGateway) vs direct API in typical flows?
- Contract binding to fulfillment profile for calendar
- Evidence qualification strictly uses `evidence['event_id']` in practice
- Cursor bound to plan_version+plan_hash and never re-executes completed steps
- Approval single-use + TOCTOU final recheck actually enforced on calendar action
- WAITING_EXTERNAL/RETRY semantics match state machine
- Recovery: ambiguous crash-before-evidence behavior and provider idempotency constraints
- Dual paths: identify which parts of Calendar surface use Governed Core vs Extension (explicit classification per this slice)

## 7. Risks, Dependencies, Closed Decisions

**Risks:** over-reading tests, conflating unit vs runtime proof, assuming direct API carries same guarantees, test-only vs prod paths.

**Dependencies:** existing governance docs, DEC-001/002/003 boundary contract, canonical architecture.

**Closed decisions (do not reopen):** Fulfillment semantics, Calendar meeting semantics, live sealing licensing, Google OAuth refresh-token lifecycle, CLI-01, TASK-01 planner version allocation race. Treat as current; record contradictions only if real evidence exists.

## 8. Out of Scope

Gmail/Contacts, Computer Use, Multi-Agent, Agent marketplace, Plugin ecosystem, many integrations, platform/cloud rewrite, microservices, K8s, event-driven rewrite, KAI-IR/ToolGateway rewrite, DB architecture change, broad refactors. Only blockers for Calendar slice.

## 9. Execution Steps (Order)

1. **Inventory tests**: list calendar/runtime/fulfillment/evidence/ir/task_state tests; map to stages
2. **Trace call paths**: runtime trigger→planning→IR→gateway→calendar adapter→calendar service; compare vs `api/calendar.py` direct path
3. **Inspect schemas/models**: confirm fields (plan_version/plan_hash/cursor, scenario/identities/evidence, fulfillment audit triple)
4. **Inspect state machine**: transitions (WAITING_EXTERNAL, RETRY, REQUIRES_ATTENTION, VERIFYING)
5. **Inspect evidence/fulfillment**: qualification + tri-state + witness set
6. **Inspect gateway pipeline**: TOCTOU recheck + single-use approval consume + verification boundary
7. **Map each stage to concrete evidence** (files/lines + test/runtime/persistence)
8. **Classify per stage**: PROVEN/PARTIALLY_PROVEN/NOT-PROVEN + missing evidence
9. **Document findings + blockers + unknowns** (no modifications to repo)
10. **Finalize plan assessment** and produce report per §11

## 10. Verification Requirements (post-inspection, still no modifications)

- Each claim backed by concrete references (file_path:line where practical)
- Evidence lists implementation/runtime/test/persistence separately
- Explicit classification per stage with justification
- No speculation without labeling as "unknown/unproven"
- Respect boundary: distinguish Core vs Extension for Calendar

## 11. Report Structure (Final Output)

### Plan Created
Path: `docs/plans/PHASE_2A_VERTICAL_SLICE_PLAN.md`

### Scope
Calendar/Schedule-a-Meeting governed slice vs Phase 1 chain

### Out of Scope
As §8

### Key Unknowns
List concrete unknowns (not guesses)

### Expected Evidence
Per-stage required evidence + what constitutes PROVEN

### Potential Blockers
Blockers only if preventing completion of slice; no fixes proposed

### Execution Status
**PLANNING ONLY — NO REPOSITORY MODIFICATIONS PERFORMED**

### Stage-by-Stage Assessment (to be filled by inspection)
Table: Stage | Implementation Evidence | Runtime Evidence | Test Evidence | Persistence Evidence | Missing Evidence | Status (PROVEN/PARTIALLY/NOT)
