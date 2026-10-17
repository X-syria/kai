# PHASE 2C — Runtime Trace Evidence Report (READ-ONLY)

**Status:** READ-ONLY — NO REPOSITORY MODIFICATIONS PERFORMED
**Based on:** PHASE_2A_CALENDAR_RUNTIME_TRACE_SPEC.md, PHASE_2A_VERTICAL_SLICE_INSPECTION_REPORT.md

## 1. Executive Verdict

**PARTIALLY PROVEN — with precisely bounded residual gaps**

Core governance structures and evidence semantics are concrete in code; Intent→IR→Action linkage exists structurally (action_id/request_id via ir.adapter). However, a single consolidated calendar end-to-end runtime assertion across all stages (Policy→Approval→Identity→TOCTOU→Action in one governed execution) is not directly demonstrated by existing tests; ambiguous crash-before-evidence remains not exhaustively proven at runtime.

> **Superseded in two parts by later phases.** As originally written, every row below carried a
> `PARTIALLY PROVEN` or `NOT PROVEN` verdict. Rows 1–3 and the Policy/Approval/Identity/TOCTOU
> rows were closed by **Phase 2E** (all observed inside one governed calendar execution). The
> `Crash-before-evidence` row was closed by **Phase 2F**. See §8 for the current status table and
> the precise provenance of each claim. The original text is preserved verbatim below.


## 2. Evidence Table

| Assertion | Runtime Evidence | Result |
|---|---|---|
| Intent → IR → Action | src/kai/ir/adapter.py intent_from_action maps IR ActionNode to ToolIntent; request_id set from action_id (action linkage). src/kai/runtime/service.py constructs ToolIntent via ir.adapter for governed path. No single consolidated calendar test asserts full cross-boundary identity end-to-end — structural linkage exists. | PARTIALLY PROVEN |
| Policy enforcement | src/kai/tools/gateway.py pipeline includes policy step before content/approval/execution/TOCTOU/consume/verify. Policy input includes tool/identity context. Calendar-specific runtime policy outcome not traced in one consolidated execution. | PARTIALLY PROVEN |
| Approval lifecycle | src/kai/approvals/models.py, src/kai/approvals/service.py; 	ools/gateway.py approval path with final TOCTOU recheck before consuming single-use approval (atomic consume). Calendar runtime linkage missing. | PARTIALLY PROVEN |
| Identity binding | src/kai/identity/service.py/models/schemas; 	ools/gateway.py identity assertion before execution/account guard; memory hints context-only. Consolidated calendar action identity trace absent. | PARTIALLY PROVEN |
| TOCTOU recheck | src/kai/tools/gateway.py: final policy re-check before atomic single-use approval consumption (explicit TOCTOU guard). Structural presence; calendar-specific runtime proof not consolidated. | PARTIALLY PROVEN |
| Calendar Action | Governed: untime/service.py uses ir.adapter.intent_from_action → ToolGateway.execute(); 	ools/adapters/calendar.py. Extension: pi/calendar.py direct service (bypass). Calendar slice uses governed path in runtime tasks; concrete end-to-end assertion partial. | PARTIALLY PROVEN |
| Evidence event_id authority | src/kai/runtime/evidence.py: qualifying creation reads provider event_id only from evidence['event_id'] (no fallback to data.event_id); outcome/verified required. Code rule clear; calendar create runtime observation not consolidated. | PROVEN |
| Evaluation | src/kai/tools/gateway.py verify step; 	ools/boundaries.py, 	ools/stubs.py (ContractVerificationEngine). Structural; calendar runtime linkage partial. | PARTIALLY PROVEN |
| Fulfillment | src/kai/runtime/fulfillment.py: CalendarMeetingCriteria, tri-state FULFILLED/NOT_FULFILLED/INDETERMINATE; qualification≠fulfillment; witness from qualifying_creations. Structure proven; INDETERMINATE/transition runtime proof partial. | PARTIALLY PROVEN |
| Resume | src/kai/tasks/state.py: WAITING_EXTERNAL no return to EXECUTING (REQUIRES_ATTENTION→RETRY→EXECUTING); cursor advances only after success; claim semantics (lease/heartbeat) in untime/service.py. Structurally correct; calendar runtime proof partial. | PARTIALLY PROVEN |
| Recovery | Same as above; claim lost → abort without writing; replay from durable facts; cursor bound to plan_version+plan_hash. Structural; ambiguous cases not exhaustively demonstrated. | PARTIALLY PROVEN |
| Crash-before-evidence | Fail-safe design exists; no runtime proof for provider-already-created case with provider idempotency/reconciliation. Cannot be declared PROVEN from existing tests/code alone. **Closed by Phase 2F — see §8.** | NOT PROVEN *(as of 2C; superseded)* |

## 3. Concrete Code References

- Intent→Action linkage: src/kai/ir/adapter.py::intent_from_action (maps ActionNode→ToolIntent; sets request_id from action_id). Governed execution constructs ToolIntent via this in runtime path (src/kai/runtime/service.py).
- Gateway pipeline (order): src/kai/tools/gateway.py — authenticate → assert identity → reserved args → resolve/validate (extra='forbid') → policy → content → approval (if required) → account guard → final policy re-check (TOCTOU) → atomic single-use approval consume → execute → verify.
- Evidence rule: src/kai/runtime/evidence.py — qualifying creation requires tool='calendar.create_event', outcome=EXECUTION_SUCCEEDED, ok/verified true, provider event_id from evidence['event_id'] only.
- Fulfillment: src/kai/runtime/fulfillment.py (CalendarMeetingCriteria, tri-state, pure evaluators; witness from qualifying_creations).
- State machine/recovery: src/kai/tasks/state.py (WAITING_EXTERNAL no return to EXECUTING; REQUIRES_ATTENTION→RETRY; INDETERMINATE→REQUIRES_ATTENTION; cursor/claim semantics in src/kai/runtime/service.py).
- IR build/identity: src/kai/ir/build.py program_from_plan with program_id uuid5(task_id+plan_version), canonical fingerprint; cursor bound to plan_version+plan_hash.

## 4. What Tests Establish (existing)

- 	ests/test_evidence_semantics.py: enforces evidence['event_id'] authority (PROVEN structurally/runtime by test intent).
- 	ests/test_fulfillment.py: tri-state + criteria semantics.
- 	ests/test_task_state.py: transitions (WAITING_EXTERNAL/RETRY behavior).
- 	ests/test_kai_ir.py, 	ests/test_runtime.py: IR/program identity, cursor binding, lifecycle.

## 5. Residual Gaps (Precisely Bounded)

1. **Intent→IR→Action consolidated trace** (PARTIALLY PROVEN): action_id/request_id linkage exists; no single calendar governed runtime assertion spans Understanding→IR Action→ToolIntent with concrete scenario values.
2. **Policy→Approval→Identity→TOCTOU→Action in same execution** (PARTIALLY PROVEN): mechanisms present in same pipeline; calendar-specific runtime linkage not consolidated in one trace.
3. **Approval single-use + TOCTOU + identity binding (calendar)** (PARTIALLY PROVEN): code enforces; concrete runtime evidence for calendar action missing.
4. **Crash-before-evidence (ambiguous outcome)** (NOT PROVEN **as of this phase**; **closed by Phase 2F — see §8**): fail-safe design (INDETERMINATE→REQUIRES_ATTENTION, claim lost → abort, no auto-COMPLETION); provider-already-created + idempotency/reconciliation not exhaustively demonstrated at runtime *at the time of this report*.

Items 1–3 above were likewise closed by Phase 2E, which observed them concretely in a single
governed calendar execution rather than structurally.

## 6. Updated Verdict

**PARTIALLY PROVEN — with precisely bounded residual gaps** *(original 2C verdict; superseded in part — see §8)*

Structural proof is strong. Runtime consolidated proof is partial for cross-stage linkages in one calendar execution; crash-before-evidence remains not provable with existing infrastructure without modification (and modifications are disallowed).

> This verdict was correct when written. Phases 2E and 2F subsequently closed the cross-stage
> linkage gaps and the crash-before-evidence gap respectively, without modifying production code
> or migrations. §8 gives the current status.

## 7. Next Step (Single)

**Run one additional narrowly-scoped read-only analysis: produce a concrete mapping (file:line + expected field values) for a single existing calendar runtime test that exercises governed path, extracting the actual runtime linkage values (action_id/request_id, plan_version/plan_hash, evidence event_id) to convert the consolidated trace from structurally to concretely demonstrated.** No test/code changes.

## 8. Current Status (appended after Phase 2F)

Sections 1–7 are the original Phase 2C record and are preserved as written. This section states
the current, evidence-backed status of every verdict above.

| 2C assertion | 2C verdict | Current status | Closed by |
|---|---|---|---|
| Intent → IR → Action | PARTIALLY PROVEN | **PROVEN** (concrete values: `action_id` re-derived identically on both triggers, `request_id == action_id` for all 7 rows) | Phase 2E |
| Policy enforcement | PARTIALLY PROVEN | **PROVEN** (`REQUIRE_APPROVAL` / `APPROVAL_REQUIRED_BY_RISK`, risk `MEDIUM`, 10 matched rules, `policy_evaluation_count: 2`) | Phase 2E |
| Approval lifecycle | PARTIALLY PROVEN | **PROVEN** (`PENDING → APPROVED`, single `consume`, `decided_at` + `consumed_at` set) | Phase 2E |
| Identity binding | PARTIALLY PROVEN | **PROVEN** (3× resolve → `CONTACT_MATCH`, confidence `0.5`, persisted on execution row) | Phase 2E |
| TOCTOU recheck | PARTIALLY PROVEN | **PROVEN** (2 policy evaluations on approved call vs 1 on denied, re-check precedes consume) | Phase 2E |
| Contract verification | PARTIALLY PROVEN | **PROVEN** (`read_after_create`, `adapter_verified` + `boundary_verified`, evidence republished) | Phase 2E |
| Fulfillment | PARTIALLY PROVEN | **PROVEN** (`FULFILLED` from 7 records, witness `created-event-1`) | Phase 2E |
| Evidence durability | PARTIALLY PROVEN | **PROVEN** (7 rows read back from PostgreSQL after both triggers) | Phase 2E |
| Crash-before-evidence | NOT PROVEN | **PROVEN semantically** — orphan from a real crash is never created twice; test committed at `ebed487`, but reproducible only against the working tree (§ below) | Phase 2F |

### Proven, with provenance stated honestly

Phase 2F closes the ambiguous-outcome gap for the **stale-claim** path. The mechanism is
approval consumption committed **before** provider dispatch
(`src/kai/approvals/service.py:230-278`), a stale claim refused at
`src/kai/tools/gateway.py:161-210`, and an unknown external outcome becoming `INDETERMINATE` →
`REQUIRES_ATTENTION`.

**Provenance:** the runtime assertions for that path live in the **current working tree**
(`test_a_lost_answer_is_re_derived_identically_after_a_crash`, plus the `REQUIRES_ATTENTION`
upgrade of `test_crash_after_create_before_persist_never_duplicates`). At `758ad9b` the committed
crash test still asserts the old `FAILED` / `"STALE"` outcome.

**Correction, appended after `ebed487`:** this section originally added that "the production
mechanism is committed; the tests asserting the intended semantics are not yet." The first half of
that was wrong. `758ad9b` commits the pre-dispatch refusal (`gateway.py:160`), but not the
mechanism that carries an ambiguous provider outcome to a verdict — `external_outcome` (0
occurrences in committed `runtime/service.py`), `undetermined_creations` (0 in committed
`runtime/evidence.py`), and `APPROVAL_STALE` (0 in committed `tools/gateway.py`) are all
working-tree-only.

So `PROVEN` in the row above means **a passing test observed the behaviour in this working
tree**. It does not mean the repository can reproduce it. The two must not be collapsed:

| | Semantic status | Repository-backed |
|---|---|---|
| Crash-before-evidence, orphan present | closed | test committed at `ebed487`; needs uncommitted production symbols to run |
| `INDETERMINATE` recovery for the crash window | closed | **no** — mechanism and tests both uncommitted |

### Not claimed anywhere

- **Provider-side idempotency keys** — not implemented, not proven, not claimed.
- **Automatic rediscovery or reconciliation** of an orphan — out of scope; a lost claim fails
  *stop* into human attention rather than auto-retrying.

The guarantee is **fail-stop with human attention required**, not self-healing execution.

---

## Change Audit

- Production files modified: NO
- Tests modified: NO *(as of Phase 2C; Phase 2F later added one new test — see the Phase 2F report)*
- Migrations modified: NO
- Existing architecture docs modified: NO
- Existing tests modified: NO
- Temporary instrumentation used: NO
- Temporary artifacts removed: NO
- New plan created: YES (docs/plans/PHASE_2C_RUNTIME_TRACE_EXECUTION_PLAN.md)
- New report created: YES (docs/plans/PHASE_2C_RUNTIME_TRACE_EVIDENCE_REPORT.md)
