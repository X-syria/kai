# PHASE_2D_CONCRETE_RUNTIME_LINKAGE_REPORT.md — Concrete Runtime Linkage Extraction (READ-ONLY)

**Status:** READ-ONLY — NO REPOSITORY MODIFICATIONS PERFORMED

## 1. Test Used
Candidate inspected: tests/test_runtime.py::test_a_resume_re_derives_the_declared_branch_instead_of_asking_again
- Contains calendar.create_event (evidence shows creates list filtered by tool == 'calendar.create_event')
- Exercises runtime task lifecycle (submit→trigger), plan with branches, execution with approvals, evidence recording
- Uses governed runtime path (runtime API) not direct extension API

Reason: exercises governed path and produces calendar.create_event observations.

## 2. Concrete Runtime Values (structural/observed)
| Field | Actual Value | Source |
|---|---|---|
| task_id | NOT OBSERVABLE from static inspection (generated per test) | RuntimeExecution/Task created at runtime by test helpers (_submit) |
| plan_version | NOT OBSERVABLE statically | TaskPlan.plan_version (incremented on plan creation) — set during PLANNING |
| plan_hash | NOT OBSERVABLE statically | TaskPlan.plan_hash (bound to plan content) |
| action_id | NOT OBSERVABLE statically | IR ActionNode.action_id (generated when building IR) |
| request_id | NOT OBSERVABLE statically | ToolIntent.request_id = action_id (via ir.adapter.intent_from_action) — structural relation only, concrete value not observable without execution |
| evidence event_id | NOT OBSERVABLE statically | Evidence entry in observations contains event_id from evidence['event_id'] per runtime/evidence.py; concrete value comes from provider stub in test |

## 3. Linkage Proof
**Structural linkage (PROVEN by code):**
- IR program_id: uuid5(task_id + plan_version) — src/kai/ir/build.py (program_from_plan). Deterministic binding of program to (task_id, plan_version).
- Action→ToolIntent: src/kai/ir/adapter.py::intent_from_action sets ToolIntent.request_id = action_id. Direct structural linkage.
- Cursor bound to plan_version+plan_hash — 	asks/runtime models/semantics (enforced in runtime). Prevents re-execution across versions.
- Evidence authoritative: src/kai/runtime/evidence.py requires provider event_id from evidence['event_id'] only. Fulfillment consumes qualifying creations.

**Runtime linkage across same execution:**
Cannot be extracted as concrete values from static inspection of this test alone without observing the actual runtime objects/rows. The test produces evidence with ir_program_id and ir_fingerprint (asserted: len({e['ir_program_id'] for e in evidence})==1), confirming same program/execution — but concrete IDs not statically enumerable here.

## 4. Result
**PARTIALLY PROVEN**

Structural linkages (action_id↔request_id, program bound to task_id+plan_version, cursor bound to plan_version+plan_hash, evidence uses evidence['event_id']) are PROVEN by code. Concrete runtime values for a single calendar execution are **NOT OBSERVABLE** via static inspection of existing tests; the linkage across all concrete values in the same execution instance is structurally established but not concretely extracted as runtime values without executing/observing the test.

## 5. Remaining Gap
Concrete runtime values (task_id, plan_version, plan_hash, action_id, request_id, evidence.event_id) for the same calendar action in one governed execution are not extractable from static repository inspection. To make this PROVEN, we need to observe the actual values from a single execution (e.g. via test run with debug capture) — but test modifications/execution instrumentation beyond read-only inspection are disallowed per rules. The gap is: concrete value extraction, not structural correctness.

## 6. Recommended Next Step (single)
Do not modify tests or add instrumentation. Declare this portion as PARTIALLY PROVEN (structural linkage proven, concrete values not observable via static read-only inspection). Move to next smallest step: confirm calendar vertical slice verdict remains PARTIALLY PROVEN with precisely bounded residual gap (concrete value extraction not feasible under read-only constraint).

## Change Audit
- Production modified: NO
- Tests modified: NO
- Migrations modified: NO
- Existing docs modified: NO
- New plan: YES (PHASE_2D_CONCRETE_RUNTIME_LINKAGE_PLAN.md)
- New report: YES (PHASE_2D_CONCRETE_RUNTIME_LINKAGE_REPORT.md)
- Temporary instrumentation: NO
