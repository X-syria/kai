# PHASE_2D_CONCRETE_RUNTIME_LINKAGE_PLAN.md — Concrete Runtime Linkage Extraction (READ-ONLY)

**Status:** PLANNING ONLY — NO REPOSITORY MODIFICATIONS PERFORMED

## Test to use
Use an existing runtime test that exercises governed Calendar path. Candidate: tests/test_runtime.py (runtime lifecycle + planning→IR→execution). Need one concrete test that produces observations/evidence for a calendar action.

## Why this test
Covers Task/RuntimeExecution, TaskPlan, IR build, ToolIntent via adapter (action_id→request_id), evidence/observations — contains linkage.

## Values to extract
- task_id
- plan_version
- plan_hash
- action_id
- request_id
- evidence["event_id"]

## Where to look
- Task/RuntimeExecution/TaskPlan persisted in test context
- IR ActionNode → ToolIntent via ir.adapter.intent_from_action (action_id→request_id)
- Observations/evidence containing event_id

## Linkage proof
All from same execution (same task/plan/program step). Link via adapter relation (request_id derived from action_id), plan identity (task_id+plan_version), evidence tied to execution.

## Constraints
Read-only. Only plan + final report.
