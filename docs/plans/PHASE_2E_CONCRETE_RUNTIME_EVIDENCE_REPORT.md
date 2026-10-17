# PHASE_2E_CONCRETE_RUNTIME_EVIDENCE_REPORT.md — Concrete Runtime Evidence (READ-ONLY)

**Status:** READ-ONLY OBSERVATION — NO REPOSITORY CODE/TEST/MIGRATION MODIFICATIONS PERFORMED
**Based on:** PHASE_2D_CONCRETE_RUNTIME_LINKAGE_PLAN.md, PHASE_2D_CONCRETE_RUNTIME_LINKAGE_REPORT.md, PHASE_2C_RUNTIME_TRACE_EVIDENCE_REPORT.md
**Test executed (unmodified):** `tests/test_runtime.py::test_a_resume_re_derives_the_declared_branch_instead_of_asking_again`

---

## 1. Test Used and How It Was Observed

The Phase 2D report left one residual gap: concrete runtime values for a single governed
Calendar execution could not be extracted by static inspection, because the values are
generated per execution. This phase closes that gap by **executing the existing test
unmodified** and reading its runtime seams from outside the repository.

The test was selected because it is the only existing test that drives a
`calendar.create_event` through the **governed** path (`/api/v1/runtime/tasks/{task_id}/trigger`
→ AgentRuntime → IR → ToolGateway) and forces every governance stage in that one execution:

| Governance stage the test forces | How the test forces it |
|---|---|
| Policy → approval boundary | `MEDIUM` risk policy + `create_calendar_event` ⇒ `REQUIRE_APPROVAL` |
| Approval park/resume | trigger 1 parks on `WAITING_APPROVAL`; trigger 2 takes a new claim |
| TOCTOU re-check | second trigger re-enters the Gateway with an approval already `APPROVED` |
| Approval single use | one approval, consumed exactly once |
| Identity binding | attendee resolved before execution, identity asserted in policy input |
| Contract verification | `READ_AFTER_CREATE` re-read after the insert |
| Durable evidence | evidence rows persisted and read back after both triggers |
| Fulfillment | `calendar_meeting` evaluator runs against the persisted evidence |
| Branch re-derivation | step 3 runs twice, inserts once |

**Observation method (measurement only, zero behavior change):** a pytest plugin located
**outside the repository** at
`C:\Users\khaled\AppData\Local\Temp\opencode\kai_2e\kai_2e_capture.py` wrapped the
production seams and wrote `capture.json` at session end. Wrappers delegate to the original
callables and only read the arguments and results already flowing through them.

**Behavior-neutrality is proven, not assumed:** the same test was run twice —
once with no plugin at all, once with the plugin fully active:

```
no plugin : 1 passed, 2 warnings in 2.96s
with plugin: 1 passed, 2 warnings in 1.92s
```

The test's own assertions (`ok == [False, True]` for the two create rows,
`len(backend.created_events) == 1`, one `ir_program_id`, one `ir_fingerprint`) hold under
instrumentation, so the capture observes the same execution rather than causing a different one.

---

## 2. Concrete Runtime Values

All values below are from **one single capture run**, i.e. one task, one plan, one program,
two triggers, seven evidence rows. They are not assembled across runs.

| Field | Observed Value | Where it was read at runtime |
|---|---|---|
| `task_id` | `67a1d74c-64b6-4456-91ff-e86123965249` | `RuntimeExecution.task_id`; matches `Task.id` and `TaskPlan.task_id` |
| `tenant_id` | `b22fefd6-e685-49d0-ba79-2fa467c6db8b` | `ToolIntent.tenant_id` at `intent_from_action` |
| `user_id` | `d7743f35-e08a-41c7-b03f-cbb54d3e8622` | `ToolIntent.user_id` at `intent_from_action` |
| `account_id` | `9f8de04b-3995-49c1-ac3b-66b3a74fbdc5` | `ToolContext.account_id` at `ToolGateway.execute` |
| `plan_id` | `51aa9f47-2f88-4f99-be49-eed359d29e0e` | `TaskPlan.id` |
| `plan_version` | `1` | `RuntimeExecution.plan_version`, set by `AgentRuntime._bind_plan` |
| `plan_hash` | `bc36a986f4115bb65c5c29f0732d117751edfb394eac32a782bd581baab82a1e` | `RuntimeExecution.plan_hash`, set by `_bind_plan` |
| `ir_program_id` | `301a58cc-b279-5384-b49a-869a59df562d` | `KaiProgram.program_id` at `program_from_plan`; also stamped on every evidence row |
| `ir_fingerprint` | `8bd7fcf5ef51228780a5650fde309f85fa9f52650631c8c85085d29d9b0aff2f` | `KaiProgram.fingerprint`; stamped on every evidence row |
| `action_id` (step 3, index 0) | `runtime-67a1d74c-64b6-4456-91ff-e86123965249-1-3-0` | IR `ActionNode.action_id` from `action_id_for` |
| `request_id` (step 3, index 0) | `runtime-67a1d74c-64b6-4456-91ff-e86123965249-1-3-0` | `ToolIntent.request_id` at `intent_from_action`; `ToolResult.request_id` at the Gateway |
| `approval_id` | `3d257ca9-e702-4467-b27e-bb552d57ccf2` | `Approval.id` |
| `intent_hash` | `5d1e22f4f2a25ac63597b40912bf8d6a4bdf107e2a74f5616727383a9909081d` | `Approval.intent_hash` |
| `evidence["event_id"]` | `created-event-1` | durable evidence row for the successful create, read back from the DB |
| fulfillment verdict | `FULFILLED` | `RuntimeExecution.fulfillment_verdict` after trigger 2 |

**UUID note (honest limitation):** the row primary keys (`task_id`, `tenant_id`, `user_id`,
`account_id`, `plan_id`, `approval_id`) are generated per run and **differ on every
invocation** — observed across four runs. They are reported as *the values observed in the
capture run cited above*, not as constants. The deterministic values (`plan_version`,
`plan_hash`, `ir_fingerprint`, `action_id` format) were byte-identical across all runs,
because they are derived, not random.

---

## 3. Linkage Proof — One Execution, End to End

Every arrow below is a runtime observation from the same capture, not an inference.

```
Task.id                67a1d74c-…65249
  ├─ TaskPlan.task_id   67a1d74c-…65249   (FK, same task owns the plan)
  └─ RuntimeExecution.task_id 67a1d74c-…65249

Bind:
  TaskPlan(id=51aa9f47-…9e0e, version=1, status=ACCEPTED)
    → _bind_plan → (bound=True, plan_version=1, plan_hash=bc36a986…82a1e)
    → row.plan_version=1, row.plan_hash=bc36a986…82a1e      [observed on the row]

Program:
  program_from_plan(task, plan_version=1, plan_status=ACCEPTED, plan_hash=bc36a986…82a1e)
    → program_id=301a58cc-…562d, fingerprint=8bd7fcf5…0aff2f
    → steps: s1 resolve_contact, s2 read_calendar, s3 create_calendar_event, s4 verify_calendar_event

Action (step 3, index 0):
  action_id_for(task_id, plan_version=1, step_order=3, index=0)
    = "runtime-67a1d74c-…65249-1-3-0"
    → intent_from_action → ToolIntent.request_id = the SAME string   [equal=True]

Governed execution:
  ToolGateway.execute(ToolIntent(request_id=runtime-…-1-3-0), ToolContext(task_id=67a1d74c-…65249))
    → outcome APPROVAL_REQUIRED   (trigger 1, policy REQUIRE_APPROVAL, 1 policy evaluation)

Approval:
  approval_id=3d257ca9-…cdb2, intent_hash=5d1e22f4…081d, status PENDING → APPROVED
    → consumed_at set exactly once
    → approved call re-entered Gateway with 2 policy evaluations (TOCTOU re-check)

Verified execution:
  ToolGateway.execute(same request_id=runtime-…-1-3-0)
    → outcome EXECUTION_SUCCEEDED, ok=True, verified=True
    → verification contract read_after_create, adapter evidence {event_id: created-event-1, mismatch_count: 0}

Durable evidence (7 rows, read back from DB after trigger 2):
  step 3 / create_calendar_event / APPROVAL_REQUIRED  → evidence {}                     → no witness
  step 3 / create_calendar_event / EXECUTION_SUCCEEDED → evidence["event_id"]="created-event-1" → witness
  both rows: request_id == ir_action_id == runtime-…-1-3-0
  all 7 rows: ir_program_id=301a58cc-…562d, ir_fingerprint=8bd7fcf5…0aff2f

Evaluation and settlement:
  evaluate_calendar_meeting(7 persisted records)
    → qualifying_creations → witnesses=["created-event-1"]
    → verdict FULFILLED
  → RuntimeExecution.fulfillment_profile=calendar_meeting,
    evaluator_version=calendar_meeting:v1, fulfillment_verdict=FULFILLED
```

**Observed task/execution progression across the two triggers:**

| | Trigger 1 | Trigger 2 |
|---|---|---|
| `TriggerOutcome` | `WAITING_APPROVAL` | `COMPLETED` |
| `Task.state` | `WAITING_APPROVAL` | `COMPLETED` |
| `RuntimeExecution.cursor` | 2 | 3 |
| `steps_used` | 6 | 7 |
| evidence rows | 6 | 7 |
| fulfillment verdict | *(not yet set)* | `FULFILLED` |
| Approval.status | `PENDING` | `APPROVED`, `consumed_at` set |
| `plan_version` / `plan_hash` | 1 / `bc36a986…` | 1 / `bc36a986…` — unchanged |

The `cursor` and `plan_hash` are **identical across both claims**, which is the runtime
demonstration that the branch was re-derived from the same bound plan rather than a rebuilt one.

---

## 4. Governance Stages — Concrete Runtime Results

| Stage | Observed at runtime | Result |
|---|---|---|
| Policy | `REQUIRE_APPROVAL` / `APPROVAL_REQUIRED_BY_RISK`, risk `MEDIUM`, 10 matched rules (`binding`, `task_state`, `allowed_tools`, `critical_tool`, `risk_policy`, `deadline`, `step_budget`, `cost_budget`, `identity`, `risk_approval`), recipient identity `VERIFIED` | **PROVEN** |
| Identity | 3× `IdentityResolver.resolve` → status `CONTACT_MATCH`, confidence `0.5`, `contact_id=people/ahmed-1`, matched by `contact_email_exact`, source `google_contacts`; persisted on the execution row as `identities=[{name, email: ahmed@example.com, status: CONTACT_MATCH, confidence: 0.5}]` | **PROVEN** |
| Approval lifecycle | `check` returned `REQUIRED` (decided_by `system`, i.e. the policy raised it) then `APPROVED` (decided_by the acting user); `consume` ran once; row transitions `PENDING → APPROVED` with `decided_at` and `consumed_at` both set | **PROVEN** |
| TOCTOU re-check | the approved `create_calendar_event` call ran **2** policy evaluations with identical inputs (`policy_evaluation_count: 2`) versus **1** on the denied call; the second evaluation occurs before `consume` (`src/kai/tools/gateway.py:183` before `:201`) | **PROVEN** |
| Single-use approval | exactly one `consume` call across the whole execution; one provider insert (`len(backend.created_events) == 1`) | **PROVEN** |
| Contract verification | `read_after_create`, `adapter_verified=True`, `boundary_verified=True`, adapter evidence `{event_id: created-event-1, mismatch_count: 0}` republished into the durable `evidence` container | **PROVEN** |
| Fulfillment | `CalendarMeetingCriteria(required_attendees={"ahmed@example.com"}, window 2026-10-12..2026-10-15 earliest 16:00:00)`; verdict `FULFILLED` from 7 records and witness `created-event-1` | **PROVEN** |
| Resume / branch re-derivation | trigger 1 stops at `WAITING_APPROVAL` with `cursor=2`; trigger 2 re-walks the same program (`program_id` and `fingerprint` unchanged) and re-selects step 3; step 3 executes twice, inserts once | **PROVEN** |
| Evidence durability | 7 rows persisted and read back from PostgreSQL after both triggers; every row carries `request_id` == `ir_action_id` and the same program id/fingerprint | **PROVEN** |
| Crash-before-evidence | not exercised in this phase (**NOT PROVEN as of 2E**; closed later by Phase 2F — see §9) | **SUPERSEDED** → see §9 |

---

## 5. `evidence["event_id"]` Authority — Verified, Not Assumed

This is the point the task singled out, so it was checked two independent ways.

**(a) At fulfillment time, on the real persisted records.** The evaluator's own call into
`qualifying_creations` was captured. With the 7 real records present it returned
`witnesses=["created-event-1"]`, and the only qualifying record was the
`EXECUTION_SUCCEEDED`/`ok`/`verified` one — the `APPROVAL_REQUIRED` row with `ok=False` was
correctly rejected. The wrapping of the single read site
`_provider_event_id` (`src/kai/runtime/evidence.py:267`) recorded, for the qualifying record,
`evidence_container_value="created-event-1"`, `data_container_value="created-event-1"`,
`returned="created-event-1"`. So the value fulfillment depends on is demonstrably the one the
verification boundary republished into `evidence`.

**Limitation, stated plainly:** in this test both containers happen to hold the same string,
so this observation alone cannot *discriminate* the two containers. Discrimination was
therefore established separately.

**(b) Differential probe on the real production functions.** Because the single governed
execution cannot separate the containers, the three production functions were called
directly with conflicting inputs. This is a differential probe on production code, clearly
labelled as such — it is not a runtime capture of a governed execution.

| Record shape | `_provider_event_id` | witness from `qualifying_creation` | `_created_event_id` |
|---|---|---|---|
| `evidence={event_id: evt1}`, `data={event_id: evt1}` | `evt1` | `evt1` | `evt1` |
| `evidence={event_id: evt1}`, `data={event_id: evt2}` | `evt1` | `evt1` | `evt1` |
| `evidence={}`, `data={event_id: evt1}` | `None` | `None` | `None` |
| `evidence={event_id: evt1}`, no `data` | `evt1` | `evt1` | `evt1` |
| `evidence={}`, `data={event_id: 42}` | `None` | `None` | `None` |
| `evidence={event_id: 42}`, `data={event_id: evt1}` | `None` | `None` | `None` |
| `evidence={event_id: " "}`, `data={event_id: evt1}` | `None` | `None` | `None` |

Conclusions that hold: when `evidence` and `data` disagree, **`evidence` wins and `data` is
ignored**; when `evidence` carries no usable string, **no witness is produced even though
`data` holds one**; a non-string or blank identity yields `None` rather than being coerced.
There is **no `data` fallback anywhere on the witness path**, so `data["event_id"]` cannot be
used as a substitute anywhere in qualification, the evaluator, or `_created_event_id`.

Structural backing: `_PROVIDER_IDENTITY_CONTAINER: Final = "evidence"`
(`src/kai/runtime/evidence.py:139`) and the single read at `:285`; the rationale is documented
in the docstring at `:267-284`.

---

## 6. Identity Derivation Re-Derived From Observed Values

Each observed identity was recomputed from its documented formula and matched exactly, which
distinguishes "a value existed" from "the value is the one the code claims to derive".

| Identity | Formula in code | Recomputed | Match |
|---|---|---|---|
| `action_id` (all 7 rows) | `f"runtime-{task_id}-{plan_version}-{step_order}-{index}"` — `src/kai/ir/build.py:83` | identical for all 7 | ✅ |
| `program_id` | `uuid5(KAI_PROGRAM_NAMESPACE, f"{task_id}:{plan_version}")` — `src/kai/ir/build.py:92` | `301a58cc-b279-5384-b49a-869a59df562d` | ✅ |
| `plan_hash` | `plan_fingerprint(plan.plan_json)` — `src/kai/runtime/service.py:1226` | recomputed from the stored `plan_json` = `bc36a986…82a1e`, equal to the value stored on `RuntimeExecution` | ✅ |
| `request_id` | `ToolIntent.request_id = action.action_id` — `src/kai/ir/adapter.py:110` | `equal=True` for all 7 actions | ✅ |

The `action_id` formula matching on all 7 rows also demonstrates the property the formula
exists for: the same (task, plan_version, step, index) re-derives the **identical** id on the
resumed trigger, which is why the denied and the approved create rows are correlatable to the
same logical action.

---

## 7. Result

**PROVEN — concretely, from one governed Calendar execution.**

The Phase 2D residual gap ("concrete value extraction") is closed. The Phase 2C cross-stage
consolidation is closed as well: `Policy → Approval → Identity → TOCTOU → Contract Verification
→ Action → Evidence → Evaluation → Fulfillment` were all observed **inside the same single
execution of the same task/plan/program**, which is exactly what Phase 2C could not previously
demonstrate. Evidence authority (`evidence["event_id"]`, no `data` fallback) is proven both at
fulfillment time on real records and by differential probe.

**As of this phase: `crash-before-evidence` remains NOT PROVEN.** No runtime infrastructure
exists to inject a provider-side create that succeeds while the process dies before evidence is
written, and creating that infrastructure would require modifying production or tests, which
this phase forbids. At the time of writing it was an open gap, not a passed item. **This verdict
is superseded by Phase 2F — see §9.**

**Still NOT demonstrated by this evidence (honest scope limit):**
- the ambiguous-`UNDETERMINATE` / `REQUIRES_ATTENTION` recovery transition at runtime;
- idempotent provider-side re-creation after a lost claim;
- approval replay/expiry rejection at runtime.

All three remain undemonstrated **by this phase's evidence**. The first is closed separately in
Phase 2F (§9); the second and third remain open and are *not* claimed anywhere.

---

## 8. Next Step (Single)

Close `crash-before-evidence` **without** touching production code, tests, or migrations — by
exhaustively demonstrating what the existing code already does when `external_outcome` is
`UNKNOWN`: read `undetermined_creations` (`src/kai/runtime/evidence.py:237`) together with the
existing `WAITING_EXTERNAL` / `REQUIRES_ATTENTION` transitions and state, on the same
calendar execution, to establish precisely how much of the ambiguous case is *already*
structurally fail-safe and exactly which part remains unprovable without new infrastructure.
Report the proven/unprovable split; do not build the missing infrastructure in this phase.

> **Outcome:** executed as Phase 2F. The proven/unprovable split is recorded in §9.

---

## 9. Phase 2F Addendum — `crash-before-evidence` Closed

This section is a later correction appended to the Phase 2E record. Sections 1–8 above are left
as originally written so the Phase 2E verdict is not silently rewritten.

### Gap A — scenario-faithfulness gap: **CLOSED**

The residual concern was that no test reproduced the *real* crash window: a provider create that
succeeds while the process dies **before** evidence is persisted, leaving an orphan on the
provider side. The earlier orphan scenario was rejected as unfaithful because the runtime never
actually reached the provider.

Closed by one new test, `tests/test_runtime.py::test_an_orphan_left_by_a_crash_is_never_created_a_second_time`:

- the runtime is driven through approval consume with `approval.consumed_at` set, `row.cursor = 2`,
  and the worker's claim abandoned — the real ordering, because
  `src/kai/approvals/service.py:230-278` commits consumption **before** dispatch and
  `src/kai/tools/gateway.py:161-210` re-checks and then dispatches;
- the crash window is then simulated at the harness/provider boundary, so the create genuinely
  reaches the backend and a genuine orphan exists;
- resume yields `REQUIRES_ATTENTION` with `INDETERMINATE` and no second dispatch.

### Gap B — stale-claim semantics: **PROVEN IN THE CURRENT WORKING TREE (not in commit history)**

The pre-dispatch approval refusal is proven by the working-tree test
`tests/test_runtime.py::test_a_lost_answer_is_re_derived_identically_after_a_crash`, and by the
working-tree upgrade of `test_crash_after_create_before_persist_never_duplicates` to
`REQUIRES_ATTENTION` / `INDETERMINATE`.

**Provenance, stated precisely and not glossed:** at `HEAD` (`758ad9b`) the committed copy of
`test_crash_after_create_before_persist_never_duplicates` still asserts the old `FAILED` /
`"STALE"` outcome. The `REQUIRES_ATTENTION` / `INDETERMINATE` assertions and the recovery
re-derivation test exist only as **uncommitted working-tree modifications** that pre-date Phase
2F. Phase 2F did not author them. This report therefore claims working-tree proof, and explicitly
does **not** claim committed-test proof.

**Correction, appended after `ebed487`:** this section originally went on to say that "the
production mechanism they exercise *is* committed at `758ad9b`". That was wrong, and it mattered.
`758ad9b` commits the *pre-dispatch refusal* (`gateway.py:160`) and `INDETERMINATE` as a verdict,
but **not** the mechanism that carries an ambiguous provider outcome to that verdict:

| Symbol | `758ad9b` | Working tree |
|---|---|---|
| `external_outcome` in `src/kai/runtime/service.py` | **0 occurrences** | present |
| `undetermined_creations` in `src/kai/runtime/evidence.py` | **0 occurrences** | present |
| `APPROVAL_STALE` in `src/kai/tools/gateway.py` | **0 occurrences** | present |

So the production half of Gap B is working-tree-only as well, not just the tests. Gap B is
semantically closed and has no repository-backed proof at all.

### What is proven, and what is not

| Claim | Semantic status | Repository provenance |
|---|---|---|
| An orphan from a real crash is never created a second time | **PROVEN** | test committed at `ebed487`; **reproducible only against the working tree** — the `external_outcome` / `undetermined_creations` symbols it asserts are uncommitted |
| Stale claim is refused **before** provider dispatch (`APPROVAL_STALE`) | **PROVEN** | refusal committed at `758ad9b` (`gateway.py:160`); `APPROVAL_STALE` code uncommitted |
| Unknown external outcome becomes `INDETERMINATE` → `REQUIRES_ATTENTION` | **PROVEN** | **working-tree only** — mechanism and tests both uncommitted |
| Provider-side **idempotency keys** | **NOT PROVEN — and not claimed** | n/a |
| Automatic **rediscovery / reconciliation** of an orphan | **OUT OF SCOPE by decision** — deliberately excluded, not merely unproven | n/a |
| Automatic recovery of the orphan without human attention | **NOT PROVEN — and not claimed** | n/a |

These last two are different verdicts and must stay different. Idempotency keys are an
unimplemented *capability*; nobody has decided against them. Automatic rediscovery was
considered and **rejected on purpose**: it is a new capability, and it would contradict the
`REQUIRES_ATTENTION` semantics precisely because they park for a human who cannot be replaced by
a query. Recovery cannot distinguish "the crashed attempt created the event" from "it did not", so
rediscovery would be guessing.

"Semantically proven" here means a passing test observes the behaviour in the working tree. It
does **not** mean a reader who clones this repository can reproduce it. Only one of the three
proven rows is fully reproducible from committed code alone, and even that one requires the
uncommitted symbols to run.

The guarantee is **approval pre-dispatch + fail-stop into human attention**. It is *not*
provider idempotency and *not* automatic reconciliation.

### 2F Change Audit

- Production files modified: **NO**
- Migrations modified: **NO**
- Test harness (`tests/tool_harness.py`) modified: **NO**
- Tests modified: **YES — exactly one new test added**, appended at EOF of
  `tests/test_runtime.py`; no existing test was edited, reordered, reformatted, or weakened.
- User-owned uncommitted edits in `tests/test_runtime.py` preserved byte-for-byte: **YES** —
  pre-edit SHA256 `4D3D392EC9351B65DB8B666FB72CD9B9C34BA918E89F457DCD32A7128E84E70B`;
  `difflib.SequenceMatcher` over before/after yields opcodes `['equal', 'insert']` (pure append).
- Validation: targeted recovery tests **3 passed**; `tests/test_runtime.py` **80 passed**;
  `ruff check tests/test_runtime.py` **All checks passed**; `mypy` error count unchanged at
  **23 before and 23 after**, with **zero** errors in the added region.
- `ruff format` is **not** a configured gate (`pyproject.toml` defines only `[tool.ruff.lint]`)
  and was deliberately not applied: it would rewrite pre-existing user code across 41 hunks.
- Pre-existing unrelated failures in `tests/test_governed_write_path.py` (**18 failed, 40 passed**)
  confirmed **not** caused by this work: identical 18 failures reproduce with `tests/test_runtime.py`
  restored to its pre-edit bytes, and that module does not import `test_runtime`.
- Negative control (external, outside the repo, deleted after use): on a test that genuinely
  creates an event, `next_event_id` advanced `1 → 2` with exactly one create POST. This proves
  the new test's `next_event_id == 1` assertion is load-bearing, not vacuously true.

---

## Change Audit

- Production files modified: **NO**
- Tests modified: **NO**
- Existing tests executed (unmodified): **YES** — `tests/test_runtime.py::test_a_resume_re_derives_the_declared_branch_instead_of_asking_again`, 4 runs, all passing (1 without instrumentation, 3 with)
- Migrations modified: **NO**
- Existing architecture docs modified: **NO**
- Existing plans modified: **NO** — `PHASE_2D_CONCRETE_RUNTIME_LINKAGE_PLAN.md` judged sufficient and left untouched
- New specification created: **NO**
- New report created: **YES** — `docs/plans/PHASE_2E_CONCRETE_RUNTIME_EVIDENCE_REPORT.md` (this file)
- Temporary instrumentation used: **YES** — external pytest plugin, **outside the repository**: `C:\Users\khaled\AppData\Local\Temp\opencode\kai_2e\kai_2e_capture.py`. Wrappers delegate to the original callables; behavior-neutrality demonstrated by identical passing runs with and without it.
- Temporary probe scripts used: **YES** — also outside the repository: `probe_event_id_authority.py`, `probe_identity_derivation.py`
- Temporary artifacts location: **OUTSIDE REPOSITORY** — `C:\Users\khaled\AppData\Local\Temp\opencode\kai_2e\` (`capture.json` + 3 scripts). No repository file was written at any point.
- Working-tree integrity: verified no `src/**.py` or `tests/**.py` file was modified during this phase; the 35-file / 2240-insertion dirty tree pre-existed this work and was left untouched.
