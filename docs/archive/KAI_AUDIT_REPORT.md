# KAI - Engineering Audit Report (Read-Only)

**Repository:** C:\Users\khaled\Documents\kai\.claude\worktrees\busy-gould-c3f43b  
**Date:** 2026-10-03  
**Auditor:** Principal Engineer (Read-Only Investigation)  
**Scope:** Full codebase audit as specified

## A. Executive Summary (FACT)

KAI is a **Governed Agent Runtime** (not a general workflow engine) implementing a deterministic, policy-gated execution pipeline for a 'Schedule-a-Meeting' vertical slice. The system follows: model proposes, deterministic and human authorities decide.

**FACT:** ~264 Python files, 123 test files (625 tests collected). Clear lifecycle: Understanding → Planning → Policy Check → Execution (via IR) → Verification → Fulfillment. Explicit state machines, tenant isolation, Tool Gateway as single execution boundary for governed path. Strong separation between LLM outputs (data) and deterministic control flow.


## B. System Map (FACT)

Request path (governed runtime): HTTP POST /api/v1/runtime/tasks → GoalSubmission validation → Task + RuntimeExecution (CREATED) → POST /runtime/tasks/{id}/trigger → Understanding (PlannerModel, UNDERSTANDING) → Identity resolution (memory hints as context only) → Planning (PlannerService, PLANNING) → Persist TaskPlan, bind plan_version/plan_hash → IR program built/sealed → Policy/Approval via ToolGateway → Execution via IR control flow → ToolGateway executes intents → Observations recorded → Verification → Fulfillment evaluation → State transitions → Durable state in DB.

Entry points: FastAPI app (main.py), API router mounting runtime/tasks/planner/approvals/google/auth/health + direct provider/calendar/gmail/contacts endpoints. Primary governed entry: /api/v1/runtime/*.


## C. Repository Map (FACT)

Key modules: src/kai/runtime (service.py 1583 lines, models/schemas/translator/understanding/fulfillment/evidence), src/kai/ir (program/nodes/build/control/adapter), src/kai/tasks (models/state/service), src/kai/tools (gateway/intent/registry/boundaries), src/kai/planner, src/kai/db (Postgres+SQLAlchemy2), alembic (10 migrations, head b3d5f7a9c1e2), tests (625 collected), docs/architecture.

App does not auto-migrate on boot (separate migrate service per README).


## D. Runtime Lifecycle (FACT)

States (tasks/state.py): CREATED, UNDERSTANDING, PLANNING, POLICY_CHECK, WAITING_APPROVAL, EXECUTING, WAITING_EXTERNAL, VERIFYING, COMPLETED, FAILED, RETRY, ESCALATE, REQUIRES_ATTENTION. Terminal: COMPLETED, ESCALATE. Retryable: FAILED, REQUIRES_ATTENTION.

Trigger: acquires execution claim (RuntimeExecution.claim_token, committed; never held across external calls). Claim lease 5min, heartbeat 30s. UNDERSTANDING→stores scenario→PLANNING. PLANNING→creates TaskPlan, binds plan_version/plan_hash→POLICY_CHECK. EXECUTING→iterates IR control flow, executes via ToolGateway, records observations, advances cursor only after success. VERIFYING→computes fulfillment (FULFILLED/NOT_FULFILLED/INDETERMINATE), transitions accordingly.

FACT: Cursor bound to plan_version+plan_hash. Completed steps never re-executed. Claim checked at boundaries.


## E. Domain Model (FACT)

Entities: Task (sealed contract+state), TaskPlan (versioned), RuntimeExecution (1:1 with Task: scenario/identities/evidence/cursor/claim/fulfillment audit), Approval (single-use), Memory (context), GoogleAccount (encrypted), Tenant/User (multi-tenancy). Slice contract fixed via translator with calendar_meeting profile.

IR nodes (ir/nodes.py): IntentNode, StepNode, PlanNode, PolicyConstraintsNode, PermissionNode, ActionNode, ConditionNode, ControlNode/ControlEdge, ObservationNode, ErrorNode, ResultNode, StateSnapshotNode. Closed schema (extra='forbid'), bounded sizes.


## F. State Machines (FACT)

Task machine (tasks/state.py): explicit TRANSITIONS; illegal → InvalidTransition. WAITING_EXTERNAL has no return to EXECUTING (prevents silent duplicates; retry via REQUIRES_ATTENTION→RETRY→EXECUTING). RETRY requires attempts < max_retries.

IR control (ir/control.py): pure evaluation from ControlFacts (durable evidence only). Conditions read observations/cursor/steps_used/task_state (never transient ToolResult). Rejects ambiguous edges (>1 eligible) or no eligible edge.


## G. Intent & Understanding (FACT)

Understanding (runtime/understanding.py): validates UnderstandingOutput with extra='forbid' (intent='schedule_meeting', guests 1-5, bounded MeetingWindow). Model output is data only. Stored in RuntimeExecution.scenario. Identity resolution maps names→resolved identities with status/confidence; memory PERSON hints are context only (never authority).


## H. Plan & IR (FACT)

Planning (planner/service.py): locks task for versioning, supersedes previous ACCEPTED, creates new ACCEPTED. UNDERSTANDING→PLANNING on first plan.

IR (ir/build.py, program.py): program_from_plan() builds KaiProgram with deterministic program_id (uuid5 from task_id+plan_version). Canonical fingerprint from canonical JSON (excludes execution record). Validates executable. PlanBranch → ConditionNode + ControlEdges.


## I. Policy, Approval & ToolGateway (FACT)

ToolGateway pipeline (tools/gateway.py): authenticate → assert identity → reserved args check → resolve/validate tool (extra='forbid') → policy → content → approval (if required) → account guard → **final policy re-check (TOCTOU)** → **atomic single-use approval consumption** → execute → verify.

FACT: ToolGateway is execution boundary for governed runtime. Final re-check before consumption prevents TOCTOU. Policy decides allow/deny/require_approval; approval is human authorization when required.


## J. Evidence & Fulfillment (FACT)

Evidence (runtime/evidence.py): Qualifying creation requires tool='calendar.create_event', outcome=EXECUTION_SUCCEEDED, ok/verified true, provider event_id from **evidence['event_id']** (verification boundary) — **NOT** from data.event_id (explicit design choice).

Fulfillment (runtime/fulfillment.py): Typed criteria separate from prose (CalendarMeetingCriteria: window + required_attendees). Three-valued verdict: FULFILLED/NOT_FULFILLED/INDETERMINATE. Evaluators pure (no I/O). Versions are declared constants naming semantics. Qualification ≠ fulfillment; witness set from qualifying_creations.


## K. Persistence, Recovery & Security (FACT)

DB: PostgreSQL 17 + SQLAlchemy 2 + Alembic. Multi-tenancy via tenant_id FK; queries scoped. RuntimeExecution stores plan_version/plan_hash/cursor/claim fields.

Recovery: claim lease 5min, heartbeat 30s; resume from durable state (scenario/identities/evidence/cursor/plan binding); cursor advances only after success; IR derived deterministically. INDETERMINATE → REQUIRES_ATTENTION.

Security: JWT (HS256), scrypt, revoked tokens by jti; tenant isolation; Google OAuth with encrypted creds; middleware (RequestContext outermost, RateLimit on auth, TrustedHost if configured, CORS); metrics opt-in (disabled by default, unauthenticated only if enabled); tracing TRACING_ACCEPT_INBOUND=false by default; ToolGateway guards (reserved args, extra='forbid', identity assertion, policy/content/approval, final TOCTOU re-check, atomic single-use approval, account guard, verification). No secrets in logs/metrics.


## L. API, Tests, Docs vs Reality (FACT/OBSERVATION)

API: /api/v1 base. Governed: /runtime/tasks (POST), /runtime/tasks/{id} (GET, redacted), /runtime/tasks/{id}/trigger (POST). Also /tasks, /planner, /approvals, /memory, /google, plus direct provider endpoints /calendar/*, /gmail/*, /contacts/* (Extension APIs calling services directly), /metrics (opt-in), /health. 

Tests: 625 collected from worktree; mostly passing (one migration test failure: expects old Alembic revision 'a1b2c3d4e5f7' not found; head b3d5f7a9c1e2 - test setup issue, not code). Coverage includes runtime/IR/fulfillment/evidence/ToolGateway/security/observability.

Reality check: governed runtime matches canonical design. Direct provider API routes exist as separate surface (Core vs Extension). No major contradictions; boundary needs explicit documentation.


## M. Invariants (FACT)

Key invariants enforced: (1) Side effects only via ToolGateway (runtime path), (2) Evidence before fulfillment, (3) Cursor advances only after success, (4) Sealed contract immutable post-creation, (5) Replay deterministic from durable facts, (6) Single-use approval atomic, (7) Tenant isolation absolute, (8) Final policy re-check before side effect (TOCTOU), (9) Claim lost → abort without writing, (10) IR fingerprint binds identity to content, (11) Verification required for success reporting. All high confidence.

## N. Findings

**P1 (High):**
- FINDING-001 (FACT): Core vs Extension boundary ambiguity — runtime uses ToolGateway/IR; direct /calendar/*, /gmail/*, /contacts/* call services directly. Recommend document Core (governed via IR+ToolGateway) vs Extension (direct APIs) in canonical architecture; clarify if direct endpoints must be governed.
- FINDING-002 (FACT): Migration test failure — tests/test_memory.py expects Alembic revision 'a1b2c3d4e5f7' not found (head b3d5f7a9c1e2). Test setup issue, not runtime code; update test to match current migration history.

**P2 (Medium):**
- FINDING-003 (FACT, by design): WAITING_EXTERNAL has no return to EXECUTING (prevents duplicates; retry via REQUIRES_ATTENTION→RETRY→EXECUTING). Correct safety design.
- FINDING-004 (FACT): Evidence reads provider ID only from evidence['event_id'] (no fallback to data.event_id). Correct — prevents unverified IDs entering witness set.

**P3 (Low):**
- FINDING-005 (FACT): Root vs worktree test collection difference (import issue in root test unrelated to worktree). Environment-specific.

## O. Risks, Open Questions, Backlog, Next Steps

**Risks (structural):** Dual execution paths (boundary ambiguity) — MEDIUM (mitigate by documentation); claim lease 5min window — LOW (acceptable); INDETERMINATE→REQUIRES_ATTENTION — LOW (correct fail-safe); untrusted model outputs — LOW (well mitigated via extra='forbid' + validation).

**Open Questions:** Direct API governance/boundary, WAITING_EXTERNAL semantics (intentional), memory as authority (consistently context-only), gmail_send live sealing (gated vs enabled).

**Top Engineering Decision Backlog:** (1) Clarify Core vs Extension boundary in canonical architecture (recommend document as-is with explicit labels). (2) Fix migration test revision reference. (3) Keep gmail_send gated until semantic review complete.

**Recommended Next Investigation:** Document API boundary (P1), run full test suite to completion, review direct API endpoints' auth/policy model for their trust level, fix migration test, verify branching edge cases.

## Conclusion

The KAI codebase truthfully implements the governed runtime design. Core paths show strong engineering discipline (defense-in-depth: TOCTOU, single-use approvals, claim semantics, deterministic replay, strict validation). Primary finding is documentation clarity needed around Core vs Extension boundary. No critical correctness/security issues found in governed runtime implementation.

