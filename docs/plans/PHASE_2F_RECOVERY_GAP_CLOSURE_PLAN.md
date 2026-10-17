# PHASE_2F_RECOVERY_GAP_CLOSURE_PLAN.md — Recovery Gap Closure (PLANNING ONLY)

**Status:** PLANNING ONLY — NO REPOSITORY MODIFICATIONS PERFORMED
**Based on:** PHASE_2E_CONCRETE_RUNTIME_EVIDENCE_REPORT.md, PHASE_2C_RUNTIME_TRACE_EVIDENCE_REPORT.md, PHASE_2D_CONCRETE_RUNTIME_LINKAGE_REPORT.md
**Phase:** Phase 1 — Engineering Proof (`docs/ROADMAP.md`), vertical slice = Schedule-a-Meeting / Calendar

> **Nothing in this document has been implemented.** This is a plan. No production code, no
> tests, and no semantics were changed. Two existing tests were executed read-only to establish
> current facts.

---

## 1. Current Proven State

Proven in Phase 2E from one governed Calendar execution (concrete runtime values, real PostgreSQL):
Task/Plan linkage, `plan_version`/`plan_hash`, `program_id`/fingerprint stability,
`action_id → request_id`, concrete Calendar evidence, `evidence["event_id"]` authority,
Evaluation → Fulfillment, approval flow, TOCTOU re-check. Not re-examined here, per scope.

**New fact established while planning this phase, which changes the starting position:**

> Both Recovery gaps listed as open are, on inspection, **already implemented and already
> covered by existing passing tests.** `tests/test_runtime.py::test_crash_after_create_before_persist_never_duplicates`
> (line 2069, committed in `HEAD`) exercises the crash-before-evidence window and asserts
> `REQUIRES_ATTENTION` with a durable audit triple of `INDETERMINATE`.
> `tests/test_runtime.py::test_a_lost_answer_is_re_derived_identically_after_a_crash` (line 2141)
> proves the verdict is re-derived from the durable log after a crash, not remembered.
>
> Both were run read-only during planning: `130 passed` across the two recovery tests plus
> `tests/test_fulfillment.py` and `tests/test_evidence_semantics.py`.

**This is a correction to the record, not a new claim.** The Phase 2C and Phase 2E verdicts of
`NOT PROVEN` for both gaps were wrong — or rather, they were reached without examining the
recovery tests, because those phases were scoped to the governed live path and explicitly told
not to re-open proven areas. The mechanism was proven structurally and the runtime proof was
assumed absent. It is not absent. Section 8 carries the correction.

What genuinely remains open is **narrower than the gap statement**, and Sections 2 and 3
separate what is already closed from the residue that is not.

---

## 2. Gap A — Crash-before-evidence

### 2.1 Current behavior (read from source, not assumed)

The crash window is bounded by two commits, and the ordering is the whole design:

| # | Site | Effect |
|---|---|---|
| 1 | `src/kai/runtime/service.py:823` `_fence_execution` | **COMMIT** — claim renewed, state as of *before* this step is durable |
| 2 | `src/kai/tools/gateway.py:201` → `src/kai/approvals/service.py:259-275` | single use of the approval claimed by one conditional `UPDATE … WHERE consumed_at IS NULL`, then **`session.commit()` at :274** |
| 3 | `src/kai/tools/gateway.py:210` `tool.executor.execute` | **provider call — the world can change here** |
| 4 | `src/kai/runtime/service.py:841-853` | evidence record built and appended to `row.evidence` (session state, **not yet committed**) |
| 5 | `src/kai/runtime/service.py:975` `_fence_execution` | **COMMIT** — evidence + cursor become durable |

**The decisive fact: the approval is committed as spent (site 2) strictly *before* dispatch
(site 3).** A process death anywhere in the window between 3 and 5 therefore leaves durable
state that is *self-identifying as ambiguous*:

- `Approval.consumed_at` is set, `status = APPROVED` → the single use is gone
- `RuntimeExecution.cursor` is still the **previous** step (`2`) — the create was never recorded
- no evidence row for the create exists
- the provider may or may not hold the event

On resume the runtime re-enters the Gateway for the same step and is refused **before dispatch**:

```
approvals/service.py:226-227   consumed_at is not None  → ApprovalDecision.STALE
tools/gateway.py:161-164       STALE → _denied(..., APPROVAL_STALE)
runtime/service.py:1577-1581   error_code == APPROVAL_STALE
                               AND verification_contract is READ_BACKED
                               → _external_outcome_unestablished() = True
runtime/service.py:844-850     record["external_outcome"] = "UNKNOWN"   (written before the branch)
runtime/service.py:906-930     transition → VERIFYING, record_result("REQUIRES_ATTENTION"),
                               then _settle_verification(...)
runtime/fulfillment.py:618-628 undetermined_creations(evidence) non-empty
                               → FulfillmentVerdict.INDETERMINATE
runtime/service.py:1077-1082   _halt(...) → transition_task(REQUIRES_ATTENTION)
```

Two consequences worth stating plainly, because they are the actual answer to the question asked:

1. **Duplicate creation is prevented by the spent approval, not by provider idempotency.**
   A search of `src/kai/**` for `idempotenc|client_token|Idempotency` returns **no provider-level
   idempotency mechanism**, and Google Calendar `events.insert` offers none to use. The defence is
   entirely KAI-side: the single-use approval is consumed and committed before dispatch, so a
   resumed attempt is refused at the authorization boundary and never reaches the provider.
   This is why the refusal is *independent of provider state* — which is what makes it safe.
2. **There is no rediscovery or reconciliation path.** Searches for `reconcil|rediscov|orphan`
   across `src/kai/**` find nothing in the runtime. `REQUIRES_ATTENTION` is the deliberate
   terminus: `service.py:1479-1484` states the cursor and evidence are left exactly as they are,
   nothing rewound, nothing re-executed, and *"a human has to decide what the task means now."*
   A human checks Google Calendar directly.

### 2.2 Existing evidence

`tests/test_runtime.py::test_crash_after_create_before_persist_never_duplicates` (line 2069,
committed in `HEAD`) already constructs the crash window by direct durable-state manipulation —
`approval.consumed_at = now`, `row.cursor = 2`, claim aged 30 minutes — then triggers and asserts:

- `task_state == REQUIRES_ATTENTION`
- `"INDETERMINATE" in message`
- durable audit triple `("calendar_meeting", "calendar_meeting:v1", "INDETERMINATE")`
- `backend.created_events == {}` — **no second insert**
- `after[0] == before[0] == 2` — **cursor not rolled back**
- `steps_used` and evidence both `+1` — the blocked attempt is **recorded, not swallowed**
- `not blocked[-1]["ok"]` and `all(not entry["ok"] for entry in blocked)`
- replay → **HTTP 409**, state stays `REQUIRES_ATTENTION`

### 2.3 Unknowns (the honest residue)

The existing test asserts `backend.created_events == {}` — the provider stub is **empty** at
resume. So the test proves *KAI's response to its own lost durable state*; it does not place an
actual orphaned event on the provider side and re-prove the no-duplicate property.

That is the **entire** remaining residue. Stated as a question: *when a real orphan exists
provider-side, is the invariant still that no second event is created?*

The honest analysis says the invariant holds regardless, because the refusal happens at the
approval boundary before the provider is consulted — the test's refusal is unconditional and
does not read provider state. So the residue is a **faithfulness-of-scenario** gap, not a
behaviour gap. It is cheap to close and worth closing precisely because it removes the last
"we asserted this only in the easy direction" caveat.

Not proven and not in scope for Phase 1: whether an orphan can be *automatically* matched back to
its task. See §2.6 Option D.

### 2.4 Proof required

One test, provider-orphan variant. Seed `backend.created_events` with one orphan entry *before*
the resumed trigger, then assert after resume that the orphan is still the only event, that KAI
added nothing, that the durable triple is still `INDETERMINATE`, and that state is still
`REQUIRES_ATTENTION`.

`GoogleBackend.created_events` is a plain public `dict[str, dict]` (`tests/tool_harness.py:135`),
so seeding is a direct assignment. **No harness change and no production change are needed.**

### 2.5 Required change

**Test-only.** One new test, or one extra seeded assertion added to the existing test.
Zero production code. Zero migration. Zero harness change.

### 2.6 Decision

**Option B — minimal test/fault-injection infrastructure is sufficient.**

Not Option A, because a faithful provider-side orphan is the real-world shape of the scenario and
the current test does not construct it; the proof value is small but real, and the cost is a few
lines. Not Option C: no semantic change is required — the design is already correct and already
committed before dispatch. Not Option D: this gap does not block Phase 1 and *should* be closed.

**Explicitly rejected as a sub-option — auto-rediscovery of the orphan.** Building a
reconciliation path (match an orphan event back to its task by window/attendees) is **Option D**:
intentionally out of scope. Justification: it is a *new capability*, forbidden by the Scope Rule;
it would require a `calendar.list_events`/`search` read path plus a matching heuristic, i.e. new
fuzzy-identity logic over provider data; and it would contradict the closed `REQUIRES_ATTENTION`
semantics at `service.py:1481`, which explicitly parks for a human rather than deciding. A human
opening Google Calendar is the intended resolution path and is adequate for Phase 1.

---

## 3. Gap B — UNDETERMINATE Recovery

### 3.1 Current behavior

**Where `INDETERMINATE` is produced:** `src/kai/runtime/fulfillment.py`. It is returned from
exactly two places in the calendar evaluator, both driven by the durable log and never by
`error_code`:

- `:618-628` — if `undetermined_creations(inputs.evidence)` is non-empty, return `INDETERMINATE`
  immediately, before any success condition is considered
- `:647-648` — same check again on the fall-through path

The docstring at `:605-614` is explicit that `INDETERMINATE` is reserved for exactly one thing: a
lost provider answer, and that reading emptiness as `NOT_FULFILLED` is forbidden.

**What produces the undetermined set:** `src/kai/runtime/evidence.py:237-263`
`undetermined_creations` selects records by `record.get("external_outcome") == "UNKNOWN"`
(`:263`) — the module states at `:246` that selection is **never** by `error_code`. It requires
`tool == "calendar.create_event"` (`:262`).

**Who writes the marker:** `src/kai/runtime/service.py:844-850`, and only when
`_external_outcome_unestablished(self.gateway, result)` is true (`:1544-1588`). That predicate
requires the tool's `verification_contract` to not be `READ_BACKED`, and the error code to be
either `is_unresolved_external(...)` (`PROVIDER_UNREACHABLE`, `PROVIDER_SERVER_ERROR`) **or**
`APPROVAL_STALE` handled as a separate clause at `:1579`.

**The layering that makes this sound, and must not be disturbed:**
`src/kai/tools/errors.py:66-72` states `is_unresolved_external` **never** returns True for
`APPROVAL_STALE`, because the gateway consumes the approval before dispatch, so the refusal
provably stopped *this* invocation. The runtime therefore treats `APPROVAL_STALE` as its own
second, explicitly-written conclusion about the *earlier* attempt. Authorization fact and
unresolved-world claim stay two different statements.

**Where the transition happens:** `src/kai/runtime/service.py:1077-1082` — `_halt()`, which calls
`transition_task(target=REQUIRES_ATTENTION)`. This is the **only** place in the runtime where a
fulfillment verdict becomes a task state (`:1024-1034`), and `_settle_verification` is the single
settlement path shared by the live path and recovery (`:1016-1022`), with no second settlement
path anywhere.

**Is it deterministic and intentional?** Yes, on both counts. `fulfillment.py:605-614` reserves the
verdict for one meaning; `service.py:1074-1076` states *"INDETERMINATE is not a failure: the run
happened, and the evidence available cannot settle whether the Intent was fulfilled. That is
exactly what REQUIRES_ATTENTION is for."* `undetermined_creations` is a pure selection over the
durable list, so the same rows always yield the same verdict.

### 3.2 Existing evidence

**Gap B is already runtime-proven, by two existing passing tests.**

> **Provenance correction (added after execution).** At the time this plan was drafted, the two
> tests below were described as "existing", which could be misread as *committed*. They are not
> committed. At `HEAD` (`758ad9b`) the committed copy of
> `test_crash_after_create_before_persist_never_duplicates` still asserts the **old** `FAILED` /
> `"STALE"` outcome, and `test_a_lost_answer_is_re_derived_identically_after_a_crash` does not
> exist in the commit at all. Both live in the **uncommitted working tree**, where they
> pre-date Phase 2F. The **production mechanism** they exercise *is* committed at `758ad9b`; the
> tests asserting the intended `REQUIRES_ATTENTION` / `INDETERMINATE` semantics are not.
>
> The conclusion below is therefore correct — Gap B *is* runtime-proven — but the basis is
> working-tree proof, not commit history. All downstream documents state it that way. Phase 2F did
> not author these tests and does not claim credit for them.

`test_crash_after_create_before_persist_never_duplicates:2102-2114` — the live path reaches
`REQUIRES_ATTENTION` with the durable audit triple `INDETERMINATE`, derived from real evidence
rows carrying a real `external_outcome` marker.

`test_a_lost_answer_is_re_derived_identically_after_a_crash:2141` — the recovery path: the task is
put back to `VERIFYING` as if it died between the transition and the settlement, and the stored
triple is **deliberately corrupted to `FULFILLED`** first. Recovery still returns `INDETERMINATE`
(`:2202-2208`), proving the verdict is re-derived from the durable log rather than obeyed, and
`len(backend.created_events) == before_creates` (`:2210`) proves re-deriving does not re-drive the
lost step.

Evaluator-level INDETERMINATE semantics are covered by `tests/test_fulfillment.py` — including
`test_an_unestablished_external_outcome_is_indeterminate_not_not_fulfilled:203`, and the
anti-overreach guards at `test_runtime.py:2275` (`PROVIDER_NOT_CONTACTED` / `PROVIDER_ERROR` must
**not** park) and `:2305` (a read-only tool is never an unknown outcome). Those guards matter as
much as the positive case: they show `INDETERMINATE` is not merely the default.

### 3.3 Unknowns

None material. The producer, the marker writer, the selector, the single settlement path, and the
transition are all located in source and all covered by passing tests on both the live and the
recovery path.

The only thing missing is **the record**: Phase 2C/2E listed this as NOT PROVEN. The correction is
a documentation act, not an engineering act.

### 3.4 Proof required

None. Already proven by execution during planning (`130 passed`).

### 3.5 Required change

**No change.** A report correction only.

### 3.6 Decision

**Option A — existing implementation is sufficient; only the record is wrong.**

Explicitly **not** Option B: adding a fault-injection test here would prove a path that two tests
already exercise on real durable state. It would add maintenance surface for no new evidence.

---

## 4. Proposed Changes

Total production changes required across both gaps: **zero.**

| # | File | Change | Kind | Gap | Justification |
|---|---|---|---|---|---|
| 1 | `tests/test_runtime.py` | Add **one** test: provider-orphan variant of the crash window. Seed `backend.created_events` with one orphan before the resumed trigger; assert the orphan remains the only event, KAI added none, triple still `INDETERMINATE`, state still `REQUIRES_ATTENTION`, replay still 409. | New test only | A | Closes the only real residue (§2.3). `created_events` is a plain dict (`tests/tool_harness.py:135`), so no harness change. |
| 2 | `docs/plans/PHASE_2E_CONCRETE_RUNTIME_EVIDENCE_REPORT.md` | Amend the `NOT PROVEN` verdict for both gaps with a pointer to this plan and to the two existing tests. | Docs | A, B | The current report is wrong on its face. `AGENTS.md`: "Code is Evidence … when they diverge, code wins until reconciled." |
| 3 | `docs/plans/PHASE_2C_RUNTIME_TRACE_EVIDENCE_REPORT.md` | Same correction, since it is the origin of the wrong verdict. | Docs | A, B | Consistency. |

Explicitly **not** proposed: any change under `src/kai/**`, any migration, any harness change, any
new capability. Rejected alternatives are recorded in §2.6 (auto-rediscovery → Option D).

**Ranking** if both gaps were treated as equally open — they are not, so this is advisory only,
ordered by least change → least risk → most proof value → no closed-semantics contact → no scope
growth:

1. **Record correction only** (docs, §4 rows 2-3) — zero code, closes Gap B entirely.
2. **One new test** (§4 row 1) — closes Gap A's residue; test-only, no closed semantics touched.
3. *(rejected)* fault-injection harness for provider failure codes — redundant; the guard tests at
   `test_runtime.py:2275`/`:2305` already pin both directions.
4. *(rejected)* provider idempotency key — impossible on this API and unnecessary; the defence is
   the pre-dispatch committed single use.
5. *(rejected)* automatic orphan reconciliation — Option D, new capability, contradicts closed
   `REQUIRES_ATTENTION` semantics.

---

## 5. Files That Must Not Change

**Closed decisions this plan preserves and must not disturb** (per instruction §6):
fulfillment semantics · Calendar meeting semantics · live sealing licensing · Google OAuth
refresh-token lifecycle · CLI-01 · TASK-01 planner version allocation race.

| Path | Why frozen |
|---|---|
| `src/kai/runtime/fulfillment.py` | Closed fulfillment semantics. Owns the `INDETERMINATE` reservation (`:605-614`) and `CalendarMeetingCriteria`. |
| `src/kai/runtime/evidence.py` | `evidence["event_id"]` authority, proven in 2E. `undetermined_creations` selects by `external_outcome`, never `error_code` (`:246`). |
| `src/kai/tools/gateway.py` | Approval-before-dispatch ordering (`:201` before `:210`) and the TOCTOU re-check (`:183` before `:201`) — both proven in 2E. |
| `src/kai/tools/errors.py` | The two-layer `APPROVAL_STALE` distinction (`:66-72`). Collapsing it would make an authorization refusal readable as an undecidable world. |
| `src/kai/approvals/service.py` | Atomic single-use consume, committed pre-dispatch (`:259-275`). This *is* the crash-before-evidence defence. |
| `src/kai/runtime/service.py` | Single settlement path (`:1016-1022`, `:1024-1034`), `_halt` semantics (`:1479-1484`), fence-commit ordering (`:823`/`:975`). |
| `src/kai/runtime/schemas.py` | `external_outcome` publication boundary and the `ir_fingerprint`-only redaction rule (`:178-184`). |
| `src/kai/tasks/state.py` | Lifecycle transitions. |
| `src/kai/planner/**` | TASK-01. |
| `tests/tool_harness.py` | Deterministic provider stub. `created_events` is already directly seedable, so no change is needed — which is exactly why this file is frozen. |
| `tests/conftest.py` | Real-PostgreSQL harness; the evidence base for every Phase 2 claim. |

---

## 6. Verification

### Gap B — already verified during planning

```
pytest tests/test_runtime.py::test_crash_after_create_before_persist_never_duplicates \
       tests/test_runtime.py::test_a_lost_answer_is_re_derived_identically_after_a_crash \
       tests/test_fulfillment.py tests/test_evidence_semantics.py -q
→ 130 passed, 4 warnings in 24.34s
```

No further execution required. Only the record correction (§4 rows 2-3).

### Gap A — required after the change

1. `pytest tests/test_runtime.py::test_crash_after_create_before_persist_never_duplicates -v`
   — the existing test must still pass unchanged. It is the regression guard for the pre-dispatch
   ordering; a new test must never be bought by weakening it.
2. Run the **new** provider-orphan test; require it to pass.
3. `pytest tests/test_runtime.py tests/test_fulfillment.py tests/test_evidence_semantics.py tests/test_governed_write_path.py tests/test_task_state.py -q`
   — full Recovery + evidence + governance surface green.
4. `ruff check` and `mypy` on the touched test file, per `AGENTS.md` validation policy.
5. Re-run the full suite before concluding; a Recovery change that is test-only should not move
   any other result.
6. Assert **no** diff under `src/kai/**` and **no** new migration: `git status --porcelain`.
   The working tree was already dirty at baseline (56 entries, 35 files, +2240/-864) and that
   baseline is user-owned and must be unchanged apart from the intended additions.

### Proof that must be reported, not assumed

The new test must assert the orphan is *still the only event after resume*, not merely that
KAI's attempt was denied. Asserting only the denial would re-prove what §2.2 already proves and
leave the residue open.

---

## 7. Risks

| Sev | Risk | Basis | Mitigation |
|---|---|---|---|
| **Critical** | Editing `tests/test_runtime.py` on a tree where that file already carries uncommitted user changes (`git status` shows ` M tests/test_runtime.py`), risking overwrite of user work | Working tree dirty at baseline; `kai-state-integrity` | Snapshot exact bytes + `sha256` before any edit; append only; verify checksum and `git status` after. If the intended insertion point overlaps user changes, stop and ask. |
| **High** | Treating this plan's "already proven" as licence to skip re-verification later | Phase 2C/2E already made the opposite error by assuming absence | §6 step 1 re-runs the existing tests; the record correction cites test names and line numbers, not conclusions. |
| **High** | The new orphan test passes for the wrong reason (e.g. denied before ever consulting the stub), so it proves nothing about provider state | The refusal is unconditional, so a weak assertion would pass trivially | Assert the orphan is still present and still the *only* entry after resume — i.e. assert on `backend.created_events` content, not on KAI's refusal code. |
| **Medium** | Scope creep into reconciliation: "the orphan exists, so KAI should find it" | Tempting but a new capability | §2.6 records it as Option D with reasons. `REQUIRES_ATTENTION` semantics are closed. |
| **Medium** | Mistaking "no provider idempotency" for a defect | `events.insert` offers none; the pre-dispatch committed single use is the defence | §2.1 documents the ordering with line numbers. Do not propose an idempotency key later. |
| **Medium** | Amending Phase 2E could be read as rewriting evidence to fit a conclusion | The reports are evidence artifacts | Correction is additive: names the tests and lines, states the prior verdict was wrong, changes no observed runtime value. |
| **Low** | A future refactor reorders commit and dispatch, silently reopening the crash window | Nothing structurally prevents it | §6 step 1 is the standing regression guard; the ordering is asserted by the existing test's `consumed_at` setup. |
| **Low** | Temp artifacts from Phase 2E left in `%TEMP%` | Outside repo, no effect on the tree | Optionally delete `%TEMP%\opencode\kai_2e\`; not required, and not a repo change. |

---

## 8. Final Recommendation

**One step, and it is a correction plus one test:**

> **Execute the record correction (§4 rows 2-3) and add the single provider-orphan test (§4 row 1) in one working session, then verify per §6.**

**The answer to the question this plan was asked to answer — proof gap or system gap:**

> **It is a proof gap, not a system gap.** Not one line of Recovery semantics needs to change.
>
> *(Outcome of executing this plan: see §9 — both gaps closed, one test added, zero production
> changes, `test_runtime.py` 80 passed.)*
>
> - **Gap B (`UNDETERMINATE → REQUIRES_ATTENTION`) is already PROVEN — in the working tree.**
>   Two passing tests drive the real durable path — live and recovery — to a durable audit triple
>   of `INDETERMINATE` and a `REQUIRES_ATTENTION` state, including the case where the stored
>   verdict is deliberately corrupted first so recovery must re-derive rather than obey. Only the
>   record was wrong. **These tests are uncommitted working-tree changes; they are not in `HEAD`
>   (`758ad9b`), where the crash test still asserts the old `FAILED` / `"STALE"` outcome.**
> - **Gap A (crash-before-evidence) is PROVEN on KAI's side and correct by design.** The
>   single-use approval is consumed and **committed before dispatch**
>   (`approvals/service.py:274` before `gateway.py:210`), so a death in the window leaves durable
>   state that is self-identifying as ambiguous; the resumed attempt is refused at the
>   authorization boundary, never reaches the provider, does not rewind the cursor, records the
>   blocked attempt, settles `INDETERMINATE`, parks, and refuses replay with 409. The duplicate
>   defence is the pre-dispatch committed single use — there is no provider idempotency key
>   because none is needed and none is available.
> - **The only real residue is scenario faithfulness:** the existing test resumes against an
>   *empty* provider stub. One test-only addition seeds one orphaned event and re-asserts the
>   no-duplicate invariant. Cost: a few lines in one file. Production changes: **zero**.
> - **Automatic rediscovery of an orphan is Option D — intentionally out of scope.** It is a new
>   capability, forbidden by the Scope Rule, and it would contradict the closed `REQUIRES_ATTENTION`
>   semantics that park precisely so a human decides. A human opening Google Calendar is the
>   intended and adequate resolution path for Phase 1.

The Phase 1 Calendar vertical slice can be declared closed on Recovery once §6 is green. Nothing
in this plan touches Core semantics, closed decisions, or KAI's scope.

---

## 9. Execution Result

The recommendation above was executed. Record of what actually happened, including the places
reality differed from this plan.

### Added

Exactly one test, appended at EOF of `tests/test_runtime.py`:
`test_an_orphan_left_by_a_crash_is_never_created_a_second_time`.

The plan anticipated an overlap with the user's uncommitted edits and said to stop and ask. That
happened: the intended insertion point sat inside a user hunk (`new lines 2134–2376`). On the
user's `استمر` the append was moved to EOF, outside every user hunk. `difflib.SequenceMatcher`
over the before/after bytes returns opcodes `['equal', 'insert']` — a pure append, so every
pre-existing byte survives.

The plan specified asserting on `backend.created_events` content. It also specified proving no
re-dispatch. `GoogleBackend.next_event_id` was used for the latter, asserted `== 1` before and
after recovery, and a negative control confirmed that detector is load-bearing: on a test that
genuinely creates an event, `next_event_id` advances `1 → 2` with exactly one create POST.

### Changed

Documentation only, and additively — no Phase 2C/2E observation was rewritten:

| File | Correction |
|---|---|
| `PHASE_2C_RUNTIME_TRACE_EVIDENCE_REPORT.md` | Added §8 with current status per assertion; marked the crash row and the §6 verdict as superseded in place |
| `PHASE_2E_CONCRETE_RUNTIME_EVIDENCE_REPORT.md` | Marked `crash-before-evidence` superseded; added §9 recording Gap A closure, Gap B working-tree provenance, and the not-claimed list |
| `PHASE_2F_RECOVERY_GAP_CLOSURE_PLAN.md` | This section, plus a provenance correction in §3.2 and §8 |

### Not changed

`src/kai/**` — zero. Migrations — zero. `tests/tool_harness.py` — zero. No existing test was
edited, reordered, reformatted, or weakened. `ruff format` was deliberately **not** run: it is not
a configured gate (`pyproject.toml` defines only `[tool.ruff.lint]`) and it would rewrite
pre-existing user code across 41 hunks.

### Verification

| Gate | Result |
|---|---|
| Targeted recovery tests | **3 passed** |
| `tests/test_runtime.py` | **80 passed** |
| `ruff check tests/test_runtime.py` | **All checks passed** |
| `mypy tests/test_runtime.py` | **23 errors before, 23 after** — zero in the added region |
| `git diff --check -- tests/test_runtime.py` | clean |
| Tracked diffstat before vs after | `41 files` both; `tests/test_runtime.py` `344 → 453`; **+109 lines, no other file changed** |
| Byte preservation | `SequenceMatcher` `['equal', 'insert']`; pre-edit SHA256 `4D3D392E…4E84E70B` |

One finding worth recording: `tests/test_governed_write_path.py` has **18 pre-existing failures**
(`DENIED` where `EXECUTION_FAILED` is expected). These are **not** from this work — the identical
18 failures reproduce with `tests/test_runtime.py` restored to its pre-edit bytes, and that module
does not import `test_runtime`. It is flagged here rather than fixed, because fixing it is outside
Phase 2F scope and would touch semantics this phase must not change.

### Result

| Gap | Status |
|---|---|
| Gap A — crash-before-evidence | **CLOSED** |
| Gap B — `UNDETERMINATE` recovery | **CLOSED** (working-tree proof; not yet committed) |
| Provider idempotency keys | **NOT PROVEN — not claimed** |
| Automatic orphan rediscovery | **OUT OF SCOPE by decision (Option D)** |

The Gap B line above was accurate when §9 was written. §10 supersedes it.

---

## 10. Post-Commit Provenance Reconciliation

Appended after the Phase 2F commit. Sections 1–9 are the pre-commit record and are left as
written. Nothing above is retroactively edited, because doing so would erase the distinction
between what was known when and what the repository turned out to contain.

### 10.1 The commit

`ebed4871482ca1bba93502a52c1ca993d312118c` (`test(runtime): close phase 2F recovery proof gap`),
parent `758ad9b`. Exactly two paths:

| Path | Change |
|---|---|
| `tests/test_runtime.py` | +110 / −1, one hunk, one added test |
| `docs/plans/PHASE_2F_RECOVERY_GAP_CLOSURE_PLAN.md` | new file (this document) |

No `src/**`, no `alembic/**`, no `tests/tool_harness.py`, no migrations. The 41 other tracked
files carrying the user's uncommitted work were left unstaged and are still unstaged.

### 10.2 Correction: the orphan test is not reproducible from the repository alone

§9 and the commit message record the orphan test as repository-backed proof. The *test* is
committed. The *semantics it asserts* are not, and this changes what the commit actually
establishes on its own.

At `758ad9b` the three production symbols the orphan test depends on do not exist:

| Symbol | `758ad9b` | Working tree |
|---|---|---|
| `external_outcome` in `src/kai/runtime/service.py` | **0 occurrences** | present |
| `undetermined_creations` in `src/kai/runtime/evidence.py` | **0 occurrences** | present |
| `APPROVAL_STALE` in `src/kai/tools/gateway.py` | **0 occurrences** | present |

The committed crash test at `758ad9b` — unchanged by `ebed487` — asserts the older outcome:

```
assert resumed["task_state"] == "FAILED", resumed
assert "STALE" in resumed["message"]
```

whereas the orphan test committed in `ebed487` asserts `REQUIRES_ATTENTION` and a durable
`INDETERMINATE` audit triple. Those two expectations cannot both hold against one tree.

So `ebed487` records the proof, but does not by itself reproduce it. Checked out on its own it
would fail; it passes only in combination with the uncommitted production semantics. The honest
formulation is:

> The orphan-present proof is **committed as a test**, and **reproducible only against the
> working tree**, because the production symbols it asserts are still uncommitted.

### 10.3 What is committed at `758ad9b`

Not everything about recovery is working-tree-only. `758ad9b` already contains:

1. Single-use approval consumed **and committed before dispatch** —
   `src/kai/approvals/service.py`, unmodified (`git status` clean).
2. Refusal of a stale claim at the authorization boundary — `src/kai/tools/gateway.py:160`.
   The refusal is committed; only its `APPROVAL_STALE` error code is not.
3. `INDETERMINATE` as a settlement verdict reaching `REQUIRES_ATTENTION`, proven by committed
   tests that inject the verdict directly:
   `test_an_indeterminate_verdict_stops_for_a_human_not_a_failure`,
   `test_an_indeterminate_run_records_its_own_verdict`.
4. The crash window itself, asserted to its older outcome: no duplicate provider event, cursor
   unchanged, blocked attempt recorded not-ok, replay refused `409`.

### 10.4 What remains working-tree-only

| Item | Kind |
|---|---|
| `APPROVAL_STALE` error code | production |
| `external_outcome` marker + `undetermined_creations` selection | production |
| Stale claim → `UNKNOWN` → `INDETERMINATE` → `REQUIRES_ATTENTION` for the crash window | production |
| `test_crash_after_create_before_persist_never_duplicates` upgraded to `REQUIRES_ATTENTION` + durable `INDETERMINATE` | test |
| `test_a_lost_answer_is_re_derived_identically_after_a_crash` | test |
| Seven `INDETERMINATE` anti-overreach guards | test |
| `test_the_published_evidence_names_the_sealed_plan_that_produced_it` | test |

The `external_outcome` mechanism is the load-bearing one: without it there is no path from an
ambiguous provider outcome to `INDETERMINATE`, so the stale-claim settlement differs between the
committed tree and the working tree.

### 10.5 Canonical recovery status

| Area | Status |
|---|---|
| Crash-before-evidence **implementation** | **CLOSED** — pre-dispatch refusal committed at `758ad9b` |
| Crash-before-evidence **proof with orphan present** | **CLOSED** — test committed at `ebed487`; reproducible only against the working tree (§10.2) |
| `INDETERMINATE` recovery **implementation** | **CLOSED semantically**; the `UNKNOWN` → `INDETERMINATE` → `REQUIRES_ATTENTION` path for the crash window is working-tree-only |
| `INDETERMINATE` recovery **semantic proof** | **CLOSED at working-tree level; repository-backed proof pending** |
| Provider idempotency | **NOT PROVEN** — no idempotency key exists; not claimed |
| Automatic orphan rediscovery | **OUT OF SCOPE** — deliberate, not a deferral |

The guarantee actually demonstrated is narrow and should not be widened in the retelling:

> An ambiguous external outcome causes **fail-stop into human attention** rather than blind
> redispatch. It does not reconcile, self-heal, or prevent provider-side duplicates by any other
> means. A human opening the provider is the intended resolution path.

Parking in `REQUIRES_ATTENTION` rather than auto-retrying is the designed outcome, not a missing
feature. Recovery cannot distinguish "the crashed attempt created the event" from "it did not",
so a retry would be a coin flip and a rediscovery query would be a new capability.

### 10.6 Not decided here

Whether to commit the Gap B semantic tests, the production symbols in §10.4, and the untracked
Phase 2C/2E reports is a separate decision. This document records the state; it does not resolve
it.

---

## 11. P0 / P1 Milestone Record

Appended after §10, under the same convention: nothing above is retroactively edited. Sections 1–10
remain the record of what was known when, including the §10.5 statuses that this section supersedes
on the two axes it now closes.

### 11.1 Recorded status

```text
P0 — public execution boundary:        CLOSED / PROVEN
P1 — Calendar ambiguity/recovery:      CLOSED / PROVEN
Gmail evidence semantics:              PROVEN
Gmail runtime recovery execution:     DEFERRED / OUT OF SLICE
```

Stated with the precision the Gmail line requires, because the two are not the same claim:

```text
Gmail evidence/fulfillment semantics: PROVEN
Gmail governed execution + ambiguous-outcome recovery: UNEXERCISED / OUT OF CURRENT SLICE
```

The Gmail result is a **deliberate scope boundary, not a defect**. Gmail runtime recovery is
recorded as **not exercised**. It is explicitly *not* claimed as proven, and it is explicitly *not*
derived from the Calendar proof: the Calendar evidence says nothing about a send, and inferring a
send result from it would be vacuous reasoning of exactly the kind this record refuses.

`src/kai/**` for this milestone: **zero**. No production change was made or was required. No
recovery abstraction, idempotency framework, or state-machine machinery was added. The Calendar
slice remains Calendar-only: Gmail was **not** added to capabilities, allowed tools, planner/tool
routing, fulfillment profile, runtime dispatch, or the public execution surface.

HEAD at the time of this record: `18050e4` (`fix(recovery): make ambiguous outcome semantics
repository-backed`).

### 11.2 P0 — public execution boundary, CLOSED / PROVEN

Method was boundary enumeration, not assertion. 19 public write routes exist; the only two with
provider capability are `POST /api/v1/calendar/events` and `POST /api/v1/gmail/send`, both retired
`410` with zero provider calls. `POST /api/v1/tasks` followed by `/trigger` is refused `409`
("task was not submitted through the agent runtime") with zero provider calls. Each external write
service has exactly one caller, the adapter (`test_each_external_write_has_exactly_one_caller_and_it_is_the_adapter`),
and `ToolGateway.execute` has exactly one production caller, `src/kai/runtime/service.py:829`,
after the claim fence.

Positive control was a real governed run over the in-process public HTTP boundary: the first trigger
parks at `WAITING_APPROVAL` with no provider write, the approval is taken, and the second trigger —
a resume rather than a second pass — reaches `COMPLETED` with exactly one create POST. The provider
is reached only after approval, and only through the Gateway. Boundary tests:
`tests/test_calendar.py:307`, `tests/test_calendar.py:342`, `tests/test_gmail.py:326`,
`tests/test_gmail.py:364`.

### 11.3 P1 — Calendar ambiguity/recovery, CLOSED / PROVEN

**The critical window is proven by an injected crash, not by a reconstructed database state.**
`test_crash_after_create_before_persist_never_duplicates` and its orphan sibling set up the
post-crash rows directly. The new test instead kills the process at the first durable write after
the provider answered, then reads whatever the database was actually left holding.

The injection seam is deliberately the crudest one available — process death at the first
`AsyncSession.commit` after the provider's answer — because evidence durability rides on *whichever
commit comes next*, not on a commit of its own. That was established by a failed first attempt:
injecting before the `VERIFYING` transition still left the create evidence durable, because
`AgentRuntime._fence_execution` (`src/kai/runtime/service.py:1296`) commits the claim renewal at
`service.py:1311`, and `transition_task` commits at `src/kai/tasks/service.py:342`.

What the test establishes:

| Claim | Assertion |
|---|---|
| the side effect happened | `backend.next_event_id == 2` — exactly one create POST |
| KAI durably knows nothing of it | execution envelope byte-identical to pre-crash |
| absence of evidence is not failure | task state `EXECUTING` — neither `FAILED` nor `COMPLETED` |
| recovery is honest | `REQUIRES_ATTENTION`, audit triple `INDETERMINATE` |
| recovery does not duplicate | `next_event_id` still `== 2`; still exactly one event |
| the refusal is durable | replay still `409`, still no dispatch |

**`WAITING_EXTERNAL` is proven unreachable.** No `transition_task(..., target=WAITING_EXTERNAL)`,
no direct `task.state` assignment, and `ToolOutcome.WAITING_EXTERNAL` is declared at
`src/kai/tools/result.py:26` and referenced nowhere else in `src/` — no adapter produces it. It is
also deliberately absent from `UNRESOLVED_EXTERNAL_CODES` (`src/kai/tools/errors.py:53`), which
holds only `PROVIDER_UNREACHABLE` and `PROVIDER_SERVER_ERROR`, and `is_unresolved_external` is
documented never to admit a refusal code. The `WAITING_EXTERNAL -> EXECUTING` edge is already
absent at `src/kai/tasks/state.py:110` — a **working-tree** removal; HEAD still carries
`{EXECUTING, FAILED, REQUIRES_ATTENTION}`. Because the state is unreachable, that removal changes no
reachable behaviour.

**Duplicate prevention is layered, and not in the state graph.** Adding `WAITING_EXTERNAL` to
`_RESUMABLE_STATES` does **not** produce a valid governed path: the run is refused by
`rule_task_state` (`src/kai/policy/rules.py:70`, which admits only `_EXECUTION_STATES`) and the task
ends `FAILED` rather than re-dispatching. Two guards were removed *together* before a duplicate
appeared, and the create POST genuinely re-dispatched. The single-use approval
(`src/kai/approvals/service.py:266`) and the policy task-state rule are what prevent redispatch;
the transition table is not, and claiming otherwise would document a protection that does not exist.

### 11.4 Proof quality: not tautological

Every invariant above was checked against mutants, and each mutant was rolled back to verified
bytes.

| Mutant | Outcome |
|---|---|
| `_external_outcome_unestablished` → `False` | killed — the unknown collapses to `FAILED` |
| drop `consumed_at.is_(None)` from the approval CAS | **survived** |
| treat `ApprovalDecision.STALE` as approved | killed — reports `FAILED` |
| remove **both** approval guards | killed — genuine duplicate: `next_event_id == 3` |
| `WAITING_EXTERNAL` added to `_RESUMABLE_STATES` | killed — refused by policy, task ends `FAILED` |
| `CAPABILITY_TO_TOOL` routes a capability to `gmail.send` | killed — refused before dispatch |
| a send branch added to `_build_steps` | killed — **a real Gmail mutation reached the provider** |

The surviving mutant is informative rather than a gap: the CAS is a backstop, not the primary
guard, because `check` refuses at `src/kai/tools/gateway.py:161` before `consume` is reached. The
last mutant is the reason the Gmail boundary below is stated as *unexercised* and not as *proven*:
the seal was broken far enough to dispatch a real send, and the test caught it at the provider.

### 11.5 Gmail: what is proven and what is not

`gmail.send` **is** a registered tool with a `READ_AFTER_SEND` contract, so the Gateway would
classify a lost answer from it as an unresolved external outcome exactly as it does for
`calendar.create_event`. `src/kai/runtime/send_evidence.py` implements the send-witness boundary:
`message_id` is the identity (not `thread_id`, and with **no fallback** to `data`), qualification is
a total pure function of the record's own content, and the evaluator returns `INDETERMINATE` for a
lost answer rather than `NOT_FULFILLED`. Recipient identity does affect qualification —
`_send_satisfies_criteria` compares `to` against the sealed resolution, while subject/body/`SENT` are
deliberately not re-checked because `verified=True` already vouches for them. An empty required set
is a failed criterion, not a vacuous success.

None of it is reachable from the governed runtime. At least six independent seals: the capability
vocabulary has no gmail entry; `SLICE_ALLOWED_TOOLS` excludes the tool so `rule_allowed_tools`
denies it; `SLICE_CAPABILITIES` excludes the capability so the planner cannot plan it;
`SLICE_FULFILLMENT_PROFILE` seals `calendar_meeting`; `src/kai/runtime/service.py:1107` validates
every proposed action against the task's capabilities and refuses at `service.py:1111` before the
program is built; and `_build_steps` has a per-action allowlist with no send branch.

So the honest split is:

| Gmail axis | Status |
|---|---|
| evidence / fulfillment semantics | **PROVEN** |
| governed execution dispatch | **DEFERRED — out of current slice, by design** |
| ambiguous-outcome recovery, end to end | **UNEXERCISED** |

`gmail.send` requires PRQ-1, PRQ-2 **and** PRQ-3 before it may be driven (§16.2). Enabling Gmail to
satisfy the P1 proof matrix would be adding a capability to close an artificial gap, and is not
authorised. The next architectural task does not come from this workstream.

### 11.6 Verification

| Gate | Result |
|---|---|
| Full suite | **1303 passed, 9 skipped, 522 warnings** — zero failures, zero errors |
| `ruff check .` | **All checks passed** |
| `mypy` | **Success: no issues found in 133 source files** |
| `alembic check` | **No new upgrade operations detected** |
| Focused (runtime + gmail + governed write path + task state + policy + security) | **291 passed** |
| Byte preservation of every mutated file | SHA-256 verified, 3 files |
| Working tree after the round | **identical to the pre-round baseline** (67 entries) |

Added, working-tree-only and **not committed**:

| Test | Axis |
|---|---|
| `test_a_crash_after_the_provider_answered_never_lets_the_write_happen_twice` | P1 Calendar critical window |
| `test_waiting_external_is_inert_even_for_a_reclaimable_stale_claim` | P1 `WAITING_EXTERNAL` |
| `test_the_governed_runtime_cannot_reach_a_send_however_it_is_widened` | P1 Gmail boundary |

Per §10.2, these are committed as neither test nor symbol: **they are reproducible only against the
working tree**, because the production semantics they assert remain uncommitted. They do not by
themselves establish what the repository contains on checkout.

### 11.7 Not decided here

Whether to commit these three tests, the `src/kai/tasks/state.py` edge removal, or the §10.4
production symbols remains a separate decision. This record states the milestone; it does not
resolve the commit question, and it resolves no Gmail scope question.
