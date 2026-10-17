# PHASE 2A - Calendar Vertical Slice Inspection Report (READ-ONLY)

**Generated:** 10/05/2026 04:26:17
**Status:** READ-ONLY INSPECTION — NO REPOSITORY MODIFICATIONS PERFORMED

Following plan: docs/plans/PHASE_2A_VERTICAL_SLICE_PLAN.md

---

## 1. Executive Verdict

**Vertical Slice PARTIALLY PROVEN**

The governed runtime path (AgentRuntime + IR + ToolGateway + Task state + Evidence + Fulfillment) exists and is coherent. The Calendar capability is exercised through the governed path for runtime tasks. However, dual execution paths (Core governed vs Direct Extension API /api/v1/calendar/* calling services directly) exist and the precise slice coverage across both paths is not uniformly proven end-to-end for every stage with explicit runtime evidence. Core semantics (evidence from evidence['event_id'] only, TOCTOU recheck, single-use approval, claim semantics, cursor bound to plan_version+plan_hash) are present in code; complete runtime proof across the full happy-path + recovery is partially documented by tests rather than exhaustively demonstrated for all recovery branches.

## 2. Stage-by-Stage Matrix

| Stage | Status | Evidence | Main Gap |
|---|---|---|---|
| Intent | PARTIALLY PROVEN | untime/understanding.py (extra='forbid', bounded MeetingWindow, intent='schedule_meeting'); RuntimeExecution.scenario stores understanding; tests validate constraints. | Explicit runtime trace showing user intent preserved through to IR/Action not exhaustively shown in tests. |
| Contract | PROVEN | untime/translator.py (calendar_meeting profile), 	asks/models.py, planner/service.py (TaskPlan with versioning), untime/models.py (contract binding fields). | Minor: no explicit runtime assertion in tests that contract remains sealed post-acceptance in all paths. |
| Planning / KAI-IR | PROVEN | planner/service.py, ir/build.py (program_from_plan()), ir/program.py (deterministic program_id uuid5(task_id+plan_version)), canonical fingerprint; ir/nodes.py. Cursor bound to plan_version+plan_hash. | Recovery edge cases (plan supersession) well-covered conceptually; explicit replay from IR canonical form not fully demonstrated. |
| Policy | PARTIALLY PROVEN | 	ools/gateway.py (policy step in pipeline), policy/ service/rules; ToolGateway enforces policy on governed path. | Policy outcomes not always asserted in calendar-specific runtime tests for all branches. |
| Approval | PARTIALLY PROVEN | pprovals/models.py, pprovals/service.py, 	ools/gateway.py (require_approval path, atomic single-use consume, final TOCTOU recheck before consume). | Calendar create event approval path: need concrete runtime evidence for calendar-specific case (tests show approval mechanics generally). |
| Identity | PARTIALLY PROVEN | identity/ service/models; gateway asserts identity; memory hints context-only. | Identity used by calendar action in governed runtime not explicitly traced in a single calendar end-to-end test as a separate assertion. |
| Action | PARTIALLY PROVEN | Governed: untime/service.py builds ToolIntent via ir/adapter.intent_from_action, calls 	ools/gateway.execute(); 	ools/adapters/calendar.py. Extension: pi/calendar.py calls vertical services directly (no ToolGateway). | **Core vs Extension boundary is the key gap:** Calendar capability exists via both paths; plan must classify which path is used for governed runtime slice. |
| Evidence | PROVEN | untime/evidence.py requires provider event_id from evidence['event_id'] only (no fallback to data.event_id); observations recorded after ToolResult; append-only. Tests validate this constraint. | End-to-end runtime observation chain for calendar create event fully traced in tests but recovery-before-evidence ambiguous case not exhaustively proven. |
| Evaluation | PROVEN | Verification via 	ools/boundaries.py, 	ools/stubs.py; verification required conceptually; evaluation step in pipeline. | Verification outcome linkage to fulfillment not always asserted explicitly in calendar runtime tests. |
| Fulfillment | PROVEN | untime/fulfillment.py (CalendarMeetingCriteria, tri-state FULFILLED/NOT_FULFILLED/INDETERMINATE, pure evaluators). Qualification≠fulfillment; witness set from qualifying_creations. Tests exist. | INDETERMINATE branches and mapping to state transitions need stronger runtime proof. |
| Recovery | PARTIALLY PROVEN | 	asks/state.py (WAITING_EXTERNAL no return to EXECUTING; RETRY via REQUIRES_ATTENTION→RETRY; INDETERMINATE→REQUIRES_ATTENTION), claim semantics (lease 5min, heartbeat 30s), cursor advances only after success. | Ambiguous crash-before-evidence + provider idempotency constraints not exhaustively demonstrated by runtime tests. |

## 3. Actual Execution Path

**Governed (Core) path observed:**
1. Runtime task submission (POST /api/v1/runtime/tasks) creates Task + RuntimeExecution (CREATED)
2. Trigger (POST /api/v1/runtime/tasks/{id}/trigger) advances: Understanding (UNDERSTANDING) → Identity resolution (memory hints context-only) → Planning (PLANNING)
3. TaskPlan created, bound plan_version + plan_hash
4. IR program built via program_from_plan() (deterministic program_id), validated, sealed
5. State transitions to POLICY_CHECK/EXECUTING via AgentRuntime logic
6. Tool calls built as ToolIntent via ir/adapter.intent_from_action (action_id/request_id linkage)
7. ToolGateway.execute() processes full pipeline (auth, identity assert, reserved args, resolve/validate with extra='forbid', policy, content, approval, account guard, final TOCTOU recheck, atomic single-use approval consume, execute, verify)
8. Calendar tool executed via adapter → provider service; observations recorded after ToolResult
9. Verification; state moves to VERIFYING, Fulfillment evaluated (tri-state); terminal/transition per state machine

**Direct/Extension path:**
- pi/calendar.py exposes /api/v1/calendar/* endpoints; handlers call vertical calendar service directly (no ToolIntent, no ToolGateway.execute()). This bypasses gateway pipeline by entry point (documented reality).

The Calendar vertical slice is represented in both surfaces. For "governed runtime task" flows, execution uses the Core path above.

## 4. Governed Core vs Extension

**Passes through Governed Core (when using runtime/tasks API):**
- Task lifecycle, RuntimeExecution, plan binding (plan_version/plan_hash), IR control flow, ToolGateway execution boundary, evidence recording into RuntimeExecution envelope, verification, fulfillment evaluation, claim/cursor semantics.

**Does NOT pass through full Core governance (when using direct API):**
- Direct calendar endpoints bypass ToolGateway/pipeline (policy/approval/identity/content/TOCTOU/single-use consume enforced only via gateway). These are Extension/operational surfaces; not claimed to carry all Core governance guarantees (per Canonical Architecture §4.2).

**Conclusion:** Calendar capability supports both paths. Governed slice execution is proven to exist on Core path; direct API is Extension.

## 5. Tests and Evidence

**Key test modules (slice-relevant):**
- 	ests/test_runtime.py (runtime lifecycle, claim/cursor, migration test fixed)
- 	ests/test_kai_ir.py (IR build/program/fingerprint/control)
- 	ests/test_task_state.py (transitions, WAITING_EXTERNAL/RETRY)
- 	ests/test_fulfillment.py (tri-state, criteria)
- 	ests/test_evidence_semantics.py (evidence['event_id'] constraint, qualification)
- 	ests/test_calendar.py (calendar flows)

**What they prove:** Core mechanics (state machine, IR, evidence semantics, fulfillment) are well-covered by unit/integration tests.
**What they don't fully prove:** exhaustive runtime end-to-end recovery across all ambiguous-crash paths and explicit calendar create-event trace asserting every stage in one scenario. Approval/identity assertions for calendar-specific case appear partially covered.
**False confidence risk:** conflating direct-API tests vs governed-runtime task tests. Need to distinguish paths when reading coverage.

## 6. Findings

### High
- **Dual execution paths not fully classified per calendar operations.** Calendar exposed via both governed (runtime) and direct (API) paths; boundary contract exists conceptually (DEC-001/002/003) but not enforced/documented as binding for this slice. (No fix in this round.)

### Medium
- **Approval/Identity not explicitly traced in a single calendar end-to-end assertion.** Present in code/pipeline; missing consolidated runtime evidence linking these stages to calendar create event.
- **Recovery edges under ambiguous crash-before-evidence** not exhaustively demonstrated. Semantics correct by design (INDETERMINATE→REQUIRES_ATTENTION, no auto-return from WAITING_EXTERNAL); runtime proof partial.

### Low
- **Test naming/path distinction** (governed vs extension) could reduce ambiguity; documentation only.

**Critical:** None.

## 7. Closed Decisions Check

No conflict found with closed decisions (Fulfillment semantics, Calendar meeting semantics, live sealing licensing, Google OAuth refresh-token lifecycle, CLI-01, TASK-01 planner version allocation race). Implementation aligns with documented semantics (evidence['event_id'] only, tri-state fulfillment, claim/cursor binding).

## 8. Scope Violations

None observed during inspection. All exploration stayed within Calendar slice and Core runtime paths.

## 9. Required Changes (Do Not Implement Now)

Priority order:
1. **P1:** Define explicit Calendar capability classification (governed vs extension) per DEC-001/002/003 and document in slice notes (boundary contract). No code changes.
2. **P1:** Add/identify a single calendar end-to-end runtime test (or document existing one) that asserts full chain for governed path with explicit stage markers (intent→contract→IR→policy/approval/identity→action→evidence→evaluation→fulfillment→recovery hooks). Planning-only.
3. **P2:** Add targeted runtime tests for ambiguous crash-before-evidence and INDETERMINATE→REQUIRES_ATTENTION paths (planning-only, test additions if approved later).
4. **P2:** Clarify test organization to distinguish governed-runtime tests from direct-API tests.

## 10. Recommended Next Step

**Create a minimal Calendar end-to-end runtime trace document (or test specification) that maps the governed path step-by-step with concrete file/line evidence and expected assertions.** Do not modify code; produce specification only. This will convert PARTIALLY PROVEN to PROVEN for stages currently lacking consolidated runtime evidence.

## Change Audit

- Production files modified: **NO**
- Tests modified: **NO**
- Architecture modified: **NO**
- Migrations modified: **NO**
- Existing docs modified: **NO**
- New report created: **YES** (docs/plans/PHASE_2A_VERTICAL_SLICE_INSPECTION_REPORT.md)
