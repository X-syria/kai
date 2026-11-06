# PHASE_2C_RUNTIME_TRACE_EXECUTION_PLAN.md — Runtime Trace Execution (READ-ONLY)

**Status:** PLANNING ONLY — NO REPOSITORY MODIFICATIONS PERFORMED

## Goal
Execute the governed Calendar runtime trace to produce concrete evidence for remaining gaps identified in PHASE_2A_CALENDAR_RUNTIME_TRACE_SPEC.md, moving from PARTIALLY PROVEN toward PROVEN where feasible.

## Scenarios
Use smallest realistic Calendar Schedule-a-Meeting scenario via governed runtime path (POST /api/v1/runtime/tasks -> POST /api/v1/runtime/tasks/{id}/trigger). Use existing test harnesses/mocks/stubs where possible.

## Existing Tests/Harnesses
- tests/test_runtime.py (runtime lifecycle, claims, cursor, transitions)
- tests/test_kai_ir.py (IR/program/fingerprint)
- tests/test_task_state.py (state transitions)
- tests/test_fulfillment.py (CalendarMeetingCriteria, tri-state)
- tests/test_evidence_semantics.py (evidence['event_id'] authority)
- tests/test_calendar.py (calendar flows)

## Provider/Mock Infrastructure
- tools/stubs.py (ContractVerificationEngine, in-memory executors) — test harness only
- Existing test fixtures in tests/ (no changes)

## Evidence Required Per Assertion
See Table C in trace spec. Focus: Intent→IR→Action linkage (action_id/request_id), Policy→Approval→Identity→TOCTOU in same execution, Evidence uses evidence['event_id'] only, Fulfillment uses qualified evidence, Approval single-use/resume if applicable, Recovery semantics.

## What Can Be Proven
Structural/linkage observables via existing tests (e.g. action_id/request_id via ir/adapter, evidence rule, state machine). Some runtime linkages visible by reading test behavior and source.

## What Cannot Be Proven With These Tools
Exhaustive "crash-after-provider-creation-before-evidence" runtime proof without modifying harness; provider idempotency reconciliation not fully exercised. Direct Extension equivalence is not provable (and not required). Some consolidated cross-stage assertions may only be structurally demonstrable.

## Ephemeral Instrumentation
None planned. Use read-only inspection: grep/read source, collect concrete file paths/lines, analyze existing test behaviors. No temporary code changes.

## What Will Not Be Modified
Production code, tests, migrations, architecture, existing docs, config, schemas, APIs, dependencies. Only this plan + final report created if needed? Final report will be created at end; this is the only new plan.

## Steps
1. Read existing trace spec and gaps
2. Inspect key code paths with concrete references (runtime/service, ir/adapter, tools/gateway, approvals, identity, runtime/evidence, tasks/state, runtime/fulfillment)
3. Map existing tests to assertions (which tests prove what)
4. Document concrete evidence for each assertion (or mark NOT PROVEN with exact missing proof)
5. Produce final report with table + verdict
