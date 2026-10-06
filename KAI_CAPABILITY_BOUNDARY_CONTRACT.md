# KAI Capability Boundary Contract

**Repository:** `C:\Users\khaled\Documents\kai`
**Baseline:** `master` @ `758ad9b` + uncommitted working tree (dirty, 24 modified / 7 untracked)
**Date:** 2026-10-04
**Type:** Design-only. No production code, test, schema, migration, API or provider was modified.

**Evidence basis:** actual source at the baseline above. Every claim below carries a `file:line`.
Test suite at this baseline: **1292 passed, 9 skipped, 5 failed** (§16.4).

---

## 1. Executive Conclusion

### 1.1 The premise of this round is stale, and the finding is structural

This round was commissioned to write boundary contracts for two duplicate capability
pairs. **Those pairs no longer exist in the code at this baseline.** This is not an
interpretation; it is a `git` fact:

| Tree | `POST /api/v1/calendar/events` | `POST /api/v1/gmail/send` |
|---|---|---|
| `HEAD` (`758ad9b`) | `calendar_create_event` → `create_and_verify_event` | `gmail_send` → `send_and_verify` |
| working tree (this baseline) | `calendar_create_event_retired` → `410 Gone` (`api/calendar.py:181-204`) | `gmail_send_retired` → `410 Gone` (`api/gmail.py:177-203`) |

The retirement is **uncommitted**. It exists only in the working tree, so it is not yet
history — but it is the code that runs.

Exhaustive verification that no second production path to either write survives:

- `create_and_verify_event` is called from exactly one production site:
  `tools/adapters/calendar.py:175`. The only other references are the two docstrings
  that describe the retirement (`api/calendar.py:10,185`).
- `send_and_verify` is called from exactly one production site:
  `tools/adapters/gmail.py:159`. Same pattern (`api/gmail.py:10,181`).
- Both adapters are reachable only through `ToolGateway.execute`
  (`tools/gateway.py:210`), and the registry is sealed at build
  (`tools/registry.py:39-45`).

**Therefore: Calendar Create has one entrypoint, and Gmail Send has one entrypoint.**
The "duplicate pair" is not two live paths; it is one live path plus one tombstone that
performs no external call and returns `410` naming the governed replacement.

### 1.2 The duplicate pairs that *do* still exist were never in scope

Three live duplicate pairs remain, and all three are **read-only**:

| Live pair | Path 1 (governed) | Path 2 (direct) |
|---|---|---|
| Calendar availability | tool `calendar.read_slots` | `GET /api/v1/calendar/slots` |
| Gmail mailbox | tools `gmail.search`, `gmail.read` | `GET /api/v1/gmail/search`, `GET /api/v1/gmail/messages/{id}` |
| Contacts | tool `contacts.match` | `GET /api/v1/contacts` |

A fourth, and the most consequential, was never analysed by the prior round: **Task
Contract sealing has two entrypoints with materially different caller authority**
(`POST /runtime/tasks` vs `POST /tasks`). See §13, HA-002.

### 1.3 What this round therefore decides

1. A final class for CAP-001..CAP-026, plus six capabilities the prior matrix did not
   contain (§3).
2. Calendar Create and Gmail Send contracts — written as **single-entrypoint**
   contracts (§5, §6). Not as duplicate-pair contracts.
3. Governance / Evidence / Recovery / Idempotency equivalence rules, stated so they
   remain decidable *after* a future second entrypoint is proposed (§7–§11).
4. Four hidden-authority findings, three of them new and one of them a **prerequisite
   for ever driving `gmail.send`** (§13).
5. **DEC-001, DEC-002, DEC-003: all three ACCEPTED, none reopened, none amended.**
   Their central prescription — unify the governed writes — has already been carried
   out in the working tree (§15).
6. **IMPLEMENTATION GATE: OPEN**, for exactly one slice (§16).

---

## 2. Final Capability Taxonomy

The proposed `CLASS-A..CLASS-F, CLASS-X` taxonomy is **adequate** and is adopted without
amendment. One boundary case was tested against it and resolved rather than answered
with a new class:

> *A production, user-facing, read-only provider capability reachable outside the Task
> Runtime, holding no governed evidence and no approval.* — Does it fit?

It fits **CLASS-B**, provided CLASS-B is read as *"a specific, proven governance
contract"* rather than *"the Core governance contract"*. Its proven contract is
narrow and real: bearer authentication → account resolved by `(tenant_id, user_id)`
only → read-only provider operation → no credential echoed → no evidence, no
fulfillment, no approval. Asserting a separate class for it would imply the contract is
weaker than it is; folding it into CLASS-A would claim governance it does not have.

Final taxonomy, with the classification test that actually decides membership:

| Class | Name | Deciding test | Task envelope | Provider mutation | Governed evidence |
|---|---|---|---|---|---|
| **A** | Core Governed Execution | Reachable only as a stage of a Task driven by `AgentRuntime` | yes | permitted | yes |
| **B** | Governed Capability API | Production entrypoint outside the Task Runtime with a specific, proven governance contract | no | **forbidden** | no (must not claim) |
| **C** | Administrative | Operator action on tenants/principals/credentials, not a user task | no | forbidden | no |
| **D** | Internal Control / Infrastructure | No external entrypoint; or reachable only as an operator/ops surface | no | permitted | no |
| **E** | Test Only | Not production-reachable | no | stub only | no |
| **F** | Legacy / Transitional | Retired entrypoint retained only as a machine-readable refusal naming its governed replacement | no | **none — by construction** | no |
| **X** | Unknown | Classification could not be proven from code | — | — | — |

**`CLASS-X` is used zero times**, and that is a result, not an omission: every
production entrypoint and internal mechanism at this baseline was traced to a
`file:line` and matched a class. `CLASS-X` remains defined for future use.

**`CLASS-E` is used zero times in production.** `tools/stubs.py` is the nearest
candidate and is **not** CLASS-E: `ContractVerificationEngine` is wired in production
and is the verification authority (§12). Only `ApprovalManagerStub`,
`ContentAuthorizerStub` and `IdentityResolverStub` are test doubles.

---

## 3. Final Capability Classification

Legend — `GW` ToolGateway · `TRM` task state machine · `RE` `RuntimeExecution.evidence`
· `CVE` `ContractVerificationEngine` · `n/a` structurally impossible

> **ID continuity note.** CAP-009/010/021/022 keep the exact meanings the prior matrix
> gave them (governed write vs. direct-API write for Calendar Create and Gmail Send),
> so the questions asked of this round are answerable in the asker's terms. The prior
> CAP-004/005 split and CAP-024/025 assignments were re-derived and corrected where the
> code disagrees; §3.2 lists what changed and why.

### 3.1 The 26 capabilities

| Capability | Final Class | Authority | Governance Contract | Side Effect | Evidence | Verification | Recovery | Idempotency | Status |
|---|---|---|---|---|---|---|---|---|---|
| CAP-001 Runtime submit `POST /runtime/tasks` | A | `AgentRuntime.submit` `runtime/service.py:287-315` | sealed slice contract via `build_slice_contract`; `LIVE_SEALABLE_PROFILES` gate `tasks/service.py:114` | internal write | none | schema + profile gate | durable rows | terminal-state idempotent | current |
| CAP-002 Runtime trigger `POST /runtime/tasks/{id}/trigger` | A | `AgentRuntime.trigger`→`_drive` `:321-447` | claim ownership + fence before every external call `:776,823,975` | internal write (+ delegated provider effect) | writes RE | stage gates | claim lease + heartbeat `:1242-1369` | cursor-before-step; completed steps never re-run | current |
| CAP-003 Understand scenario | A | `understand_scenario`, invoked only by `_drive:511` | `extra=forbid` model output; stored, never trusted as decision | model call | stored in `row.scenario` | schema validate on resume `:472` | once per task; resume reads row | once-per-task guard `:496` | current |
| CAP-004 Plan creation | A | `PlannerService.create_plan`, invoked by `_drive:549` | plan versioned + `plan_hash` bound to RE `:1206` | internal write | plan rows | IR validation | resume reuses ACCEPTED plan | supersede-on-replan | current |
| CAP-005 KAI-IR build / seal / traverse | A | `_program_for:1084-1127`, `ir/build.py` | refused-not-repaired on unbuildable `:1096-1099`; capability subset of sealed contract `ir/build.py:311-331` | none (pure) | — | `program_from_plan` validates | derived deterministically per resume | `program_id` uuid5 from task+version | current |
| CAP-006 Tool execution boundary | A | `ToolGateway.execute` `tools/gateway.py:111-274` | 14-step fixed pipeline `:4-39`; sealed registry; `extra=forbid` | delegates | gateway emits `ToolResult` only | step 14 `:215` | caller owns | none of its own | current |
| CAP-007 Calendar availability read | A (tool) **+ B** (direct) | tool via GW; direct `api/calendar.py:103` | tool: GW pipeline. direct: auth + `(tenant,user)` account scope only | read | tool: RE. direct: none | tool: `READ_BACKED`. direct: service parse | tool: cursor. direct: none | reads are replay-safe | current — **live duplicate** |
| CAP-008 Calendar get event (tool only) | A | GW → `adapters/calendar.py:134` | GW pipeline | read | RE | `READ_BACKED` | cursor | replay-safe | current; **no direct route exists** |
| CAP-009 **Calendar Create (governed)** `calendar.create_event` | **A** | GW step 13 → `adapters/calendar.py:160` | GW pipeline + `rule_risk_approval` forces approval (MEDIUM) | **external mutation** | RE via `evidence_from_observation` `runtime/service.py:841` | `READ_AFTER_CREATE` + CVE `stubs.py:202` | UNKNOWN → `VERIFYING` → INDETERMINATE `:889-930` | **single-use approval only**; no provider key | current — **sole live entrypoint** |
| CAP-010 **Gmail Send (governed)** `gmail.send` | **A** | GW step 13 → `adapters/gmail.py:148` | GW pipeline + MEDIUM ⇒ approval; `rule_identity` escalates UNKNOWN | **external mutation** | RE | `READ_AFTER_SEND` + CVE `stubs.py:204` | UNKNOWN → INDETERMINATE → `REQUIRES_ATTENTION` | **single-use approval only**; no provider key | current in GW; **not drivable** (§3.3) |
| CAP-011 Gmail mailbox read | A (tool) **+ B** (direct) | tool via GW; direct `api/gmail.py:98,136` | as CAP-007 | read | tool: RE. direct: none | tool: `READ_BACKED` | tool: cursor. direct: none | replay-safe | current — **live duplicate** |
| CAP-012 Contacts match | A (tool) **+ B** (direct) | tool via GW; direct `api/contacts.py:61` | as CAP-007 | read | tool: RE. direct: none | tool: `READ_BACKED` | tool: cursor. direct: none | replay-safe | current — **live duplicate** |
| CAP-013 Login | C | `api/auth.py:90` | credential check → JWT `jti` | internal write (token) | none | signature + revocation check | n/a | n/a | current |
| CAP-014 Register | C | `api/auth.py:53` | scrypt hash; uniqueness | internal write | none | schema | n/a | n/a | current |
| CAP-015 Logout / token revoke | C | `api/auth.py:131`, `auth/revocation.py` | `jti` denylist | internal write | none | row present | purge job `maintenance/revocation_purge.py` | revoke is idempotent | current |
| CAP-016 Approval decide | A-supporting | `approvals/service.py:467` `decide` | bound to `intent_hash` `:106-117`; `PENDING` only `:495` | lifecycle write | approval row | hash re-verified at consume | lazy expiry `:746` | `(tenant,user,task,tool,intent_hash)` dedup `:314` | current |
| CAP-017 Memory store / retrieve | B | `api/memory.py` | auth + tenant scope; **context-only, never authority** | internal write (KAI DB) | none | schema | n/a | n/a | current |
| CAP-018 Memory search / correct / category toggle | B | `api/memory.py:127,168,233` | as CAP-017 | internal write | none | schema | n/a | n/a | current |
| CAP-019 Google OAuth connect / callback | C | `api/google.py:33,52` | state token; encrypted credential write `google/crypto.py` | internal write (secrets) | none | state match + token exchange | refresh on use | connect re-auth overwrites | current |
| CAP-020 Google account management | C | `api/google.py:133` | tenant/user scope | read | none | scope query | n/a | n/a | current |
| CAP-021 **Calendar Create (direct API)** `POST /calendar/events` | **F** | **nobody — it refuses** | none claimed; body names the governed path `api/calendar.py:194-204` | **none** | none | n/a | n/a | n/a | **retired, tombstoned** |
| CAP-022 **Gmail Send (direct API)** `POST /gmail/send` | **F** | **nobody — it refuses** | as CAP-021 `api/gmail.py:193-203` | **none** | none | n/a | n/a | n/a | **retired, tombstoned** |
| CAP-023 Contacts direct read | B | `api/contacts.py:61` | auth + tenant scope | read | none | service parse | none | replay-safe | current |
| CAP-024 Client task transition / archive | B | `api/tasks.py:110,154` → `tasks/service.py:226` | `CLIENT_TRANSITION_TARGETS = {FAILED, REQUIRES_ATTENTION, RETRY}` `tasks/service.py:66-68`; anything else raises `InvalidActorError` `:299` | lifecycle write | TRM only | TRM `resolve_transition` | TRM | budget-checked | current |
| CAP-025 Claim / lease / fence | D | `runtime/service.py:1242-1414` | claim token + lease + heartbeat; lost claim aborts without writing `:1369` | internal write | — | — | **this *is* the recovery mechanism** | prevents concurrent writers | current |
| CAP-026 Health / metrics probe | D | `api/health.py:12`, `api/metrics.py:22` | unauthenticated, read-only; metrics opt-in | none | none | n/a | n/a | n/a | current |

### 3.2 Capabilities present at this baseline that CAP-001..026 did not contain

The prior matrix predates the alerting work and the task/planner/approval-suggestion
routes. Omitting them would make this inventory wrong, so they are added:

| Capability | Final Class | Authority | Governance Contract | Side Effect | Evidence | Verification | Recovery | Idempotency | Status |
|---|---|---|---|---|---|---|---|---|---|
| **CAP-027 Alert delivery webhook** | **D** | `observability/alerting.py:232-261` `deliver` | **none** — no GW, policy, approval or evidence | **external POST** | none — `TaskAlert` row only | none | sweep, at-least-once `alerts/models.py:44` | stable alert `id`, at-least-once | current — **see HA-001** |
| CAP-028 Task plan API `POST/GET /planner/{id}/plan` | B | `api/planner.py:50,91,120` | auth + tenant scope | internal write | none | schema | n/a | n/a | current |
| CAP-029 Task list / get | B | `api/tasks.py:79,90` | auth + tenant scope | read | none | scope query | n/a | n/a | current |
| CAP-030 Approval suggestion list / reject | B | `api/approvals.py:109,124` | auth + tenant scope; cannot *grant* (approve), only reject | internal write | none | status check `:672` | n/a | terminal-state guarded | current |
| CAP-031 Google access-token refresh | D | `google/service.py` `usable_access_token` | refresh under the stored account's own encryption | **external POST** | none | token exchange | refresh-on-use, called inline by every provider service | refresh token is single-use; rotation handled | current — **see HA-003** |
| CAP-032 Direct task creation `POST /tasks` | **B** | `api/tasks.py:59` → `tasks/service.py:100` | caller-supplied `TaskContractInput`; only `fulfillment_profile` gated | internal write | none | schema | n/a | n/a | current — **see HA-002** |

**Total at this baseline: 32 capabilities** (26 preserved + 6 new).

### 3.3 The two statuses that are not "current"

Only three of the 32 are anything other than current, and each is stated precisely:

- **CAP-021 / CAP-022 — retired, tombstoned.** CLASS-F. They perform no provider call,
  so they cannot reintroduce the bypass they were retired to remove. This is asserted by
  construction (`raise HTTPException(410)` is the entire body).
- **CAP-010 — current in the gateway, not drivable.** `gmail.send` is registered in the
  sealed catalog (`tools/catalog.py:97-107`) and the gateway will execute it faithfully.
  But three independent gates make it unreachable from production:
  1. `LIVE_SEALABLE_PROFILES = {"calendar_meeting"}` (`tasks/service.py:78-80`) — a Task
     cannot be sealed with `gmail_send`.
  2. `SLICE_ALLOWED_TOOLS` omits every gmail tool (`runtime/translator.py:29-34`), and
     `allowed_capabilities_from_task` derives capabilities as the **reverse of the
     slice table**, dropping unknown tools and **raising** on an empty result
     (`ir/build.py:321-330`).
  3. `_intents_for` raises `RuntimeFatal` for any tool outside the four slice tools
     (`runtime/service.py:1139-1187`).

  So `gmail.send` today is reachable only from tests that call `gateway.execute`
  directly. It is implemented, governed, verified and evidenced — and undriven.

---

## 4. Duplicate Pair Analysis

### 4.1 Pair A — CAP-009 ↔ CAP-021 (Calendar Create)

#### A. Semantic Identity

**Answer: option 2 — one capability, two interfaces — at `HEAD`; option 1 (one
capability, one interface) at this baseline.**

The proof that they were *the same* capability, not two capabilities sharing a provider
call, is that both executed **the same function on the same account**:

```
HEAD api/calendar.py:calendar_create_event
        └─> calendar.service.create_and_verify_event(session, transport,
                                                     account_id=<resolved>, …)
HEAD tools/adapters/calendar.py:CalendarAdapter._create_event
        └─> calendar.service.create_and_verify_event(session, transport,
                                                     account_id=context.account_id, …)
```

Identical provider operation, identical verification (`verify_event`), identical
account source. The only difference was *what surrounded it*. So they were one
capability with two interfaces — which is exactly the condition under which a single
provider write is reachable through two different governance regimes.

At this baseline the second interface no longer performs the operation. Semantic
identity is therefore no longer in question: **CAP-021 is not Calendar Create; it is a
refusal that names Calendar Create's governed entrypoint.**

#### B. Semantic Equivalence table (evaluated at `HEAD`, where both existed)

| Property | Path 1 (CAP-009, governed) | Path 2 (CAP-021, direct, `HEAD`) | Equivalent? | Required resolution |
|---|---|---|---|---|
| Intent | sealed Task contract + scenario `runtime/service.py:297` | request body `CreateEventBody` | **No** | one intent source |
| Authorization | GW `_authenticate`+`_assert_identity` `gateway.py:117-118` | JWT + tenant query param | **No** | one scope rule |
| Policy | 10-rule engine, twice (`:125`, `:183`) | none | **No** | one policy authority |
| Approval | MEDIUM ⇒ `REQUIRE_APPROVAL` `rules.py:203-212`; single-use | none | **No** | one approval authority |
| Identity | resolver → policy status `gateway.py:352-372` | none | **No** | identity is a governed input |
| Provider Args | validated args → `CreateEventRequest` `adapters/calendar.py:165-173` | `CreateEventBody` → same request | **Yes** | — |
| Side Effect | one provider insert | one provider insert | **Yes (identical)** | this is why it was a duplicate |
| Evidence | append-only RE `:841-852` | none | **No** | one evidence authority |
| Verification | CVE re-asserts `READ_AFTER_CREATE` `stubs.py:202` | `create_and_verify_event` internal | **Partially** — same function, no independent boundary | boundary must be the CVE |
| Audit | RE + task state + safe log `gateway.py:518-537` | route log only | **No** | one audit sink |
| Recovery | UNKNOWN classification + INDETERMINATE `:889-930` | none | **No** | one recovery model |
| Idempotency | single-use approval | none | **No** | one idempotency owner (§11) |
| Task Lifecycle | full TRM | none | **No** | lifecycle participation or none |
| Fulfillment | evaluator over witnesses `fulfillment.py` | none | **No** | fulfillment or none |

**Conclusion:** `same capability / different entrypoint` — proven by identical
`Provider Args` and identical `Side Effect` against a different governance stack. Seven
properties diverged; two matched. That ratio is what made it an architectural defect
rather than a design choice.

#### C. How the resolution was actually achieved

Not by adding governance to the direct path. By **deleting the direct path's ability to
act**: `api/calendar.py:181-204` now raises `410` and names `/runtime/tasks`. Every one
of the seven divergent properties became moot because the path that diverged no longer
reaches a provider. This is the minimal correct resolution and it is already in the
tree — it is DEC-002's prescription executed.

### 4.2 Pair B — CAP-010 ↔ CAP-022 (Gmail Send)

#### A. Semantic Identity

**Same answer as Pair A: one capability, two interfaces at `HEAD`; one capability, one
interface at this baseline.**

```
HEAD api/gmail.py:gmail_send        └─> gmail.service.send_and_verify(...)
HEAD tools/adapters/gmail.py:_send  └─> gmail.service.send_and_verify(...)
```

Identical function, identical account, identical `build_raw_message`, identical
`verify_send`.

#### B. Semantic Equivalence table (evaluated at `HEAD`)

| Property | Path 1 (CAP-010, governed) | Path 2 (CAP-022, direct, `HEAD`) | Equivalent? | Required resolution |
|---|---|---|---|---|
| Intent | sealed contract + scenario | request body `SendBody` | **No** | one intent source |
| Authorization | GW authenticate + identity assert | JWT + `account_id` query param | **No** | one scope rule |
| Policy | engine ×2 | none | **No** | one policy authority |
| Approval | MEDIUM ⇒ approval; single-use | none | **No** | one approval authority |
| Identity | `rule_identity` escalates UNKNOWN `rules.py:176-200` | none | **No** | identity gates a send |
| Provider Args | validated `to/subject/body`; `sender` structurally impossible `schemas.py` | `SendBody` | **Partially** — sender still from account on both | keep sender non-caller-supplied |
| Side Effect | one provider send — **irreversible** | one provider send | **Yes (identical, and worse)** | asymmetry of consequence |
| Evidence | RE + `qualifying_send` | none | **No** | one evidence authority |
| Verification | CVE re-asserts `READ_AFTER_SEND` | service-internal | **Partially** | boundary must be the CVE |
| Audit | RE + safe log | route log | **No** | one audit sink |
| Recovery | UNKNOWN ⇒ INDETERMINATE ⇒ `REQUIRES_ATTENTION` `fulfillment.py:715-735` | none | **No** | one recovery model |
| Idempotency | single-use approval | none | **No** | one idempotency owner |
| Task Lifecycle | full TRM | none | **No** | — |
| Fulfillment | `evaluate_gmail_send` | none | **No** | — |

**The consequential asymmetry.** Pair B is strictly worse than Pair A, and the code says
so. A duplicate calendar event is visible, reconcilable and deletable. A duplicate
**send** is delivered to a third party, is not retractable, and is discovered by the
recipient. `api/gmail.py:181-187` states this in the retirement notice itself, and
`runtime/fulfillment.py:715-735` encodes it as an evaluator requirement: a lost answer
must yield `INDETERMINATE`, never `NOT_FULFILLED`, precisely so no blind retry
follows. The retirement docstring names the same reason.

That asymmetry is the reason **Gmail Send must never be given a second entrypoint**, and
it is written into §11 as a constraint rather than left as advice.

#### C. Resolution

`api/gmail.py:177-203` → `410`, naming `/runtime/tasks`. Identical mechanism to Pair A.

### 4.3 What legitimately remains different between the governed and direct *read* paths

This is the part the prior round did not ask, and it must be stated so the read
duplicates are not "fixed" by over-unification (§14.3):

| Property | Governed read (tool) | Direct read (`GET /calendar/slots`) | Must they match? |
|---|---|---|---|
| Authentication | yes | yes | **Yes** |
| Tenant scope | `(tenant_id, user_id)` from context `service.py:1415-1428` | `(current.tenant_id, current.id)` `api/calendar.py:59-70` | **Yes** |
| Account resolution | from task, **no caller-supplied id** | `account_id` query param, then re-scoped | **Yes — and the tool side is strictly stronger** |
| Policy / approval | full GW pipeline | none | **No** — a read has nothing to authorize |
| Evidence | RE record | none | **No** |
| Fulfillment participation | may feed criteria | must never | **No — and must be prevented** |
| Verification | `READ_BACKED` | service parse | **No** |

**No unification is required, and one is forbidden:** the direct read must never be
promoted into the witness set. `qualifying_creation` is scoped to
`MEETING_CREATION_TOOL` (`evidence.py:199`) precisely so a read cannot be mistaken for
the write it observed.

---

## 5. Calendar Create Boundary Contract

```
Intent → Request → Governance → Execution → Provider → Observation
       → Evidence → Verification → Outcome → Fulfilment
```

### 5.1 Contract

**What enters the capability.** One `ToolIntent` whose `tool_name` is
`calendar.create_event` (`catalog.py:66-77`) and whose `task_id`/`tenant_id`/`user_id`
equal the trusted `ToolContext` (`gateway.py:288-296`) — a mismatch is
`REJECTED/CONTEXT_MISMATCH` before anything else runs. The account is **not an input**:
it is derived from the task by `(tenant_id, user_id)` with no caller-supplied selector
(`runtime/service.py:1415-1428`). An argument that tries to name an identity or a
credential is `REJECTED/RESERVED_ARGUMENT` (`gateway.py:298-302`).

**What is trusted.** The context (session, task, tenant, user, account, transport) and
the sealed `Task` row. `PolicyInput` deliberately carries arguments for shape
visibility only and **the engine never reads them** (`gateway.py:329-335`, `rules.py:3-7`).
Cost, step count and identity come from trusted metadata, never from arguments
(`gateway.py:346-350`).

**What is governed.** Ten policy rules in a sealed order (`engine.py:25-38`): binding,
task state (`EXECUTING` only, `rules.py:47`), `allowed_tools`, CRITICAL refusal, risk
cap, deadline, step budget, cost budget, identity, and `rule_risk_approval` — which
forces human approval because the declared risk is MEDIUM (`rules.py:50-52,203-212`).
The gateway evaluates policy **twice**, the second time after every other
authorization check and immediately before the single use is consumed
(`gateway.py:183-195`).

**What executes.** `CalendarAdapter._create_event` (`adapters/calendar.py:160-201`),
reachable only as GW step 13 (`gateway.py:210`). It builds `CreateEventRequest` from
**validated** args (`adapters/calendar.py:165-173`) — the same object the direct
endpoint used to build from a raw body.

**What is observed.** `CalendarEvent` plus a mismatch tuple from
`create_and_verify_event` (`calendar/service.py:188-223`). A read failure that is not
401/403/429 becomes a *mismatch*, not an exception — so "the event exists" and "the
event is what we asked for" stay separate claims.

**What becomes evidence.** One appended record per invocation on `RE`
(`runtime/service.py:841-852`), carrying `tool`, `outcome`, `ok`, `verified`, `args`,
`data`, `evidence`, plus `ir_program_id`/`ir_fingerprint`. Append-only: the runtime
assigns a new list (`row.evidence = list(evidence)`, `:852`) and never rewrites a
prior element.

**What verifies.** Two independent steps, and the second is the one that counts:
1. the adapter's deterministic `verify_event` (`calendar/service.py:222`);
2. `ContractVerificationEngine`, which re-asserts the declared `READ_AFTER_CREATE`
   contract and **refuses `verified`** unless `event_id` is present in the adapter's
   evidence (`tools/stubs.py:177-190, 202-203`).

**What is the outcome.** Six `ToolOutcome` values, mapped by the gateway:

| Outcome | Meaning here | Task effect (`service.py:855-956`) |
|---|---|---|
| `REJECTED` | never dispatched (bad identity, unknown tool, bad args, reserved key) | → `FAILED` |
| `DENIED` | policy/approval refused; provider never reached | → `FAILED` |
| `APPROVAL_REQUIRED` | human decision pending | → `WAITING_APPROVAL`, no result recorded |
| `EXECUTION_SUCCEEDED` | created **and** CVE-verified | → continue; cursor advances `:971-973` |
| `EXECUTION_FAILED` | provider answered and refused, or never contacted | → `FAILED` |
| `REQUIRES_ATTENTION` | dispatch happened, verification failed, **or** the answer was lost | → `VERIFYING`, then settled |

### 5.2 The three identities

- **Calendar identity** — never an input. Derived from the task
  (`service.py:1415-1428`). The governed path is *structurally* unable to aim a write at
  another tenant's calendar; the retired endpoint had to enforce that by hand against a
  query parameter (`tests/test_governed_write_path.py:474-487`).
- **Event identity** — the provider's `event_id`, read **only** from the verification
  `evidence` container and **never** from `data` (`evidence.py:267-291`). The asymmetry
  is deliberate and documented: `data` is the adapter's payload, published by the
  gateway without consulting the contract, so a fallback would let an unvouched id
  re-enter the witness set.
- **Intent identity** — `intent_hash` = SHA-256 over canonical
  `{tool, arguments, account}` (`approvals/service.py:90-117`). An approval authorises
  exactly one execution of exactly that intent.

### 5.3 Idempotency and duplicate protection

| Mechanism | Present? | Evidence |
|---|---|---|
| Provider idempotency key | **No** | no idempotency construct anywhere in `src/` (grep: zero hits) |
| Gateway-level dedupe of a repeated intent | **No** | `gateway.py:111-274` has no memo |
| Single-use approval | **Yes** | atomic CAS `approvals/service.py:259-272` |
| Cursor prevents re-running a completed step | **Yes** | `service.py:764-773`, `:971-973` |
| `WAITING_EXTERNAL` has no return edge | **Yes** | `tasks/state.py:92-110` |

**The load-bearing protection is the single-use approval**, and the code says so itself:
`tasks/state.py:104-109` states the duplicate "is prevented one layer down instead, and
not by this graph at all: the approval behind a write is single-use, so any re-drive is
refused by the gateway before the provider is reached."

**Duplicate protection therefore has exactly one real defence, and it is one layer below
the state machine.** If approval is ever not required — e.g. a LOW-risk write — the
duplicate-prevention property disappears entirely, because no provider key and no
reconciliation exists. This is recorded as **PRQ-2** (§14).

### 5.4 Recovery

`APPROVAL_STALE` is the sharpest case and the code treats it precisely
(`service.py:1544-1588`): the gateway consumes the approval *immediately before*
dispatch, so this invocation provably never reached the provider — but the *earlier*
attempt that held the same single use may have, and left no receipt. The runtime
therefore writes its own conclusion `external_outcome = "UNKNOWN"`
(`service.py:850`, key at `schemas.py:161-162`) and hands the decision to the single
settlement path. `APPROVAL_STALE` is deliberately **excluded** from
`UNRESOLVED_EXTERNAL_CODES` (`tools/errors.py:39-47,53-55`) precisely so a refusal is
never reported as an undecidable world.

Classification of provider answers (`tools/errors.py:26-55`, proven by
`tests/test_governed_write_path.py:59-208`):

| Provider answer | Code | Means |
|---|---|---|
| 4xx | `PROVIDER_ERROR` / typed | **known not applied** |
| never connected | `PROVIDER_NOT_CONTACTED` | **known not applied** |
| dispatched, no answer | `PROVIDER_UNREACHABLE` | **UNKNOWN** |
| 5xx | `PROVIDER_SERVER_ERROR` | **UNKNOWN** |
| readback differs / unreadable | `REQUIRES_ATTENTION` + mismatches | dispatch happened, proof failed |

### 5.5 Fulfilment

`evaluate_calendar_meeting` over the witness set, three-valued
(`fulfillment.py`), selected by the **sealed profile** (`service.py:1591-1652`).
Qualification ≠ fulfilment (`evidence.py:31-35`). The evaluator is pure: it receives a
frozen `FulfillmentInput` that structurally cannot carry a session or a model response
(`fulfillment.py:497-519`), and it performs no transition.

---

## 6. Gmail Send Boundary Contract

Same nine-stage shape. Only the semantics that genuinely differ are stated.

### 6.1 Sender identity — structurally impossible to supply

`SendRequest` is built from exactly three validated arguments — `to`, `subject`, `body`
(`adapters/gmail.py:153-157`). The from-address comes from the stored account
(`gmail/service.py:181`) and is injected into the raw MIME by
`build_raw_message`. There is no `sender` parameter to set. Proven negatively: supplying
one is `REJECTED` and never reaches the provider
(`tests/test_governed_write_path.py:392-393`), and a header injection via a newline in
`subject` is refused before dispatch (`:514-529`).

**Sender identity is therefore not a governance control — it is a type-level
impossibility.** This is stronger than any policy rule and should not be traded away.

### 6.2 Recipient intent

Two distinct things, correctly separated:

- **What the runtime asked for** — the `args` on the evidence record.
- **What the sealed intent named** — `required_recipients`, built from the *persisted,
  never-recomputed* identity rows, case-folded (`fulfillment.py:462-494`).

Fulfilment asks whether a witness's recipients **cover** the sealed set
(`fulfillment.py:666-694`). An empty required set is `NOT_FULFILLED`, never a vacuous
success — the sealed `bool` is what stops `frozenset() <= recipients` from standing in
for "the intent named a recipient and it was this one" (`fulfillment.py:691-694`).

### 6.3 Exact destination

Verification is not "did a send happen" but "did *this* message reach *these* people":
`verify_send` compares `expected_id`, `sender_email`, recipients **and** subject
(`gmail/service.py:207-213`, `:150-163`). A readback naming a different recipient is
`REQUIRES_ATTENTION`, not success (`tests/test_governed_write_path.py:532-548`). The
`SENT` label is required (`gmail/service.py` readback path; proven at
`tests/test_governed_write_path.py:343-357`).

`subject` and `body` are **deliberately not** fulfilment criteria
(`fulfillment.py:302-307`): no sealed artifact states that a particular subject was
wanted. Correct — the recipient is the whole question (`fulfillment.py:470-473`).

### 6.4 Subject / body authority

Arguments are validated against `GmailSendArgs` with `extra=forbid` before dispatch
(`gateway.py:308-316`); malformed input never reaches the provider
(`tests/test_governed_write_path.py:386-404`). A model may propose; only the sealed
contract and the gateway dispose.

### 6.5 Approval binding

`gmail.send` is MEDIUM (`catalog.py:104`), so `rule_risk_approval` requires a human
(`rules.py:50-52,203-212`). The approval binds `{tool, arguments, account}` by SHA-256
(`approvals/service.py:90-117`), and consumption is one atomic conditional UPDATE
re-verifying id, tenant, user, `APPROVED`, `consumed_at IS NULL`, `expires_at > now`
and the hash — all in one statement (`:259-272`). A hash mismatch, an expiry, or a
prior consumption all raise, and **nothing is mutated before every condition holds**.

`rule_identity` adds a second, independent gate: an UNKNOWN recipient identity
escalates to approval and a CONFLICTING one denies outright (`rules.py:176-200`).

### 6.6 Duplicate send

**There is no defence beyond the single-use approval.** No provider idempotency key
exists; Gmail's API offers none that this code uses. Two verified sends of one intent
are **not** treated as duplicates — they are two witnesses, and the existential verdict
is indifferent to the count (`send_evidence.py:251-263`, `fulfillment.py:707-713`).
That is the right call for *fulfilment* and is emphatically **not** a duplicate-send
control. The only thing preventing a second delivery is the approval being spent.

### 6.7 Provider response and the unknown outcome

The distinction gmail forces, and the reason this contract is stricter than calendar's
(`fulfillment.py:715-735`):

| Situation | Verdict | Why |
|---|---|---|
| Provider answered, readback could not confirm | `NOT_FULFILLED` | an answer is not a witness; it is not an unknown |
| Provider never answered (timeout, or spent approval with no receipt) | `INDETERMINATE` | nothing is known |
| A witness satisfies every criterion | `FULFILLED` — **outranks any unknown** (`:758-762`) | proof settles existence |

`INDETERMINATE` routes to `REQUIRES_ATTENTION` (`service.py:889-930`), which is
fail-safe in the strong sense: the message is neither re-sent nor declared undelivered,
and **no blind retry follows**.

### 6.8 Replay

Replaying a send is only safe because replay is not offered. `qualifying_send` is a pure
function of the durable record (`send_evidence.py:211-240`), so *evaluation* replays
deterministically — but *re-execution* is refused, because the approval is spent and
`APPROVAL_STALE` is returned before dispatch (`gateway.py:161-164`). A replayed trigger
reaches the gateway, is refused, and the refusal is recorded as
`external_outcome = UNKNOWN` (`service.py:844-850`) rather than as a fresh attempt.

### 6.9 What is missing for this capability to be drivable

Recorded, not fixed. Driving `gmail.send` in production requires **all** of:

1. `LIVE_SEALABLE_PROFILES` to include `gmail_send` (`tasks/service.py:78-80`) — which
   means a second `UnderstandingOutput` intent and a second capability table
   (`service.py:1608-1617`).
2. A `CAPABILITY_TO_TOOL` entry and an `_intents_for` branch
   (`translator.py:29-34`, `service.py:1129-1187`).
3. **HA-002 closed first** — `rule_allowed_tools` treats an empty `allowed_tools` as
   permitting everything (`rules.py:233-235`), and today only the IR/runtime gates
   prevent that from mattering. Lifting those gates to drive gmail would expose it.
   See §13 and **PRQ-1**.
4. A declared position on whether a send may ever be retried after `INDETERMINATE`.
   This contract's answer is **no, not without a new approval**; that answer must be
   sealed, not assumed.

---

## 7. Multi-Entrypoint Capability Rule

**Rule ME-1.** A capability may have more than one entrypoint. Two entrypoints to the
same external effect are permitted **iff** they are *entangled*: they share one
authority for each governed concern.

**Rule ME-2 (the derivation, and it is not the example given).** The naïve rule is
"same semantic effect ⇒ same governance invariant". That is **wrong**, and the codebase
is the counterexample. Governed and direct reads of the same calendar data are the same
semantic effect and are *deliberately* governed differently (`api/calendar.py:103` vs
`tools/catalog.py:45-56`) — correctly, because a read has no side effect to govern.

The rule that survives the evidence is narrower and about **effect**, not semantics:

```
For every concern C in {authorization, policy, approval, identity,
                        evidence, verification, audit, recovery, idempotency}:

    if the two entrypoints can both produce C's effect,
    then C must have exactly one owner for both.
```

Equivalently: **the question is never "are these the same capability?" but "can these
two paths both reach the point where C is decided?"** For writes, both can. For reads,
neither can — there is nothing to decide. That is why unification is mandatory for
writes and forbidden for reads, and why the same rule yields opposite answers for
CAP-009↔CAP-021 (must unify) and CAP-007↔`GET /calendar/slots` (must not).

**Rule ME-3 (minimum shared contract).** Where a capability legitimately has two
entrypoints, the shared minimum is:

1. one identity source for the acting principal, derived not supplied;
2. one account/credential selector, derived not supplied;
3. one declaration that no governed evidence, no fulfillment and no lifecycle state is
   produced by the non-governed path — and a mechanism that makes the claim checkable;
4. one refusal rule: the non-governed path must be unable to emit a governed artifact.

For the read duplicates, (3) is discharged by `qualifying_creation` being scoped to
`MEETING_CREATION_TOOL` (`evidence.py:199`) — a direct read cannot enter the witness
set even if someone tried to pass its result in.

**Rule ME-4.** A tombstone (`410` naming the governed path) is the *only* sanctioned
form of entrypoint retirement. Deleting the route would give a caller a bare `404` and
no migration instruction; the tombstone is what makes retirement an architectural act
rather than an outage. Both CAP-021 and CAP-022 comply.

---

## 8. Governance Equivalence Rule

**Definition GE-1.** Two paths `P1`, `P2` are **Governance Equivalent** for capability
`K` iff, for every concern `C` that `K` can produce, both paths resolve `C` to the same
authority by the same rule. Equivalence is **per-concern**, not global.

**GE-2 — the concern set is not a subset; it is derived, and it is closed by
construction.** The eight concerns are not an arbitrary list:

| Concern | Is it in scope for `K`? |
|---|---|
| authorization | always |
| policy | iff `K` can produce a decision the system is accountable for |
| approval | iff `K` can produce an irreversible or externally-visible effect |
| identity | iff `K` names a party whose identity is not self-evident from the credential |
| evidence | iff `K` can leave a durable trace that something happened |
| verification | iff `K`'s effect can be falsified by reading the provider back |
| audit | iff `K` must be reconstructable after the fact |
| recovery | iff `K`'s outcome can be unknown |
| idempotency | iff `K` can be executed twice |

A read-only capability with a self-evident counterparty has an **empty** concern set
beyond authorization, and is therefore trivially governance-equivalent to itself across
paths — which is exactly why the read duplicates are not findings.

**GE-3 — measured result at this baseline.**

| Pair | Concerns in scope | Equivalent? |
|---|---|---|
| CAP-009 / CAP-021 | all nine | **Yes — vacuously.** CAP-021 is a `410` and produces none of them. |
| CAP-010 / CAP-022 | all nine | **Yes — vacuously.** Same. |
| CAP-007 / `GET /calendar/slots` | `{authorization}` | **Partially.** Both authenticate and scope to `(tenant, user)`. **Not equivalent** on account selection: the tool path accepts no `account_id`, the direct path accepts one and re-scopes it (`api/calendar.py:59-70`). The tool path is strictly stronger. |
| CAP-011 / gmail direct reads | `{authorization}` | as above |
| CAP-012 / CAP-023 | `{authorization}` | as above |

**GE-4 — the minimum for a capability outside the Task Runtime to remain part of KAI
architecture.** Four requirements, all currently met for the read duplicates:

1. **Authentication** and **(tenant, user) scoping** on every request.
2. **Account selection re-scoped to the caller** — an `account_id` from another tenant
   resolves to nothing.
3. **No governed artifact may be produced** — no evidence, no fulfillment, no lifecycle
   transition.
4. **Read-only** — no provider mutation. This is the load-bearing one: it is what makes
   the empty concern set in GE-2 legitimate rather than an oversight.

**GE-5.** A path that satisfies GE-4 is inside KAI architecture. A path that performs a
mutation and does not satisfy GE-1 for all nine concerns is outside it and must be
removed. There is no third category — a path may not be "mostly governed".

---

## 9. Evidence Equivalence Rule

**Definition EV-1.** Two paths produce **Equivalent Evidence** iff, for every state the
capability can leave the world in, each path's durable record determines the same
answer with the same confidence.

**EV-2 — the three-way test.** Given one record set, decide:

1. **Same kind?** Both must answer *"did this capability succeed at its declared
   effect?"* — not *"did something happen?"*.
2. **Same trigger?** Both must require the same preconditions to admit the record as a
   witness.
3. **Same trusted field?** Both must read the provider identity from the same container.

KAI's answers, and they are specific:

| | Calendar Create | Gmail Send |
|---|---|---|
| Same kind? | yes — "a meeting was created" / "a message was sent" | yes — "the message was sent" |
| Trigger | `tool`, `outcome=EXECUTION_SUCCEEDED`, `ok`, `verified`, `evidence[event_id]` non-empty (`evidence.py:197-207`) | `tool`, `outcome`, `ok`, `verified`, `evidence[message_id]` non-empty (`send_evidence.py:225-235`) |
| Trusted field | `evidence` only, never `data` (`evidence.py:267-291`) | `evidence` only, never `data` (`send_evidence.py:309-319`) |

**EV-3 — so yes: one semantic evidence contract per capability, and it already exists
twice, symmetrically.** `evidence.py` and `send_evidence.py` are deliberately parallel
implementations of one contract, differing only in the tool name and the identity key.
That is the correct shape: one *contract*, two *implementations*, no third reading.

**EV-4 — where they are deliberately NOT equivalent.** Three asymmetries, each
justified in code:

1. **Thread identity.** `gmail.send` publishes `message_id`, `thread_id`, `label_ids`
   and `mismatch_count` (`adapters/gmail.py:173-178`); `calendar.create_event`
   publishes `event_id` and `mismatch_count` (`adapters/calendar.py:191-199`).
   `QualifyingSend` deliberately carries **no** thread (`send_evidence.py:192-196`): a
   thread is a grouping, not an attempt identity, and publishing it invites "some
   conversation exists" to be read as "this intent was delivered".
2. **Scope of the tool filter.** `MESSAGE_SEND_TOOL` excludes `gmail.search` and
   `gmail.read` **on purpose** (`send_evidence.py:152-155`): a mailbox that already
   contains the message is not a witness that this run sent it. The same reasoning
   keeps `calendar.get_event` out of the creation set.
3. **The `UNKNOWN` complement.** Both keep undetermined attempts as a **separate set**
   (`evidence.py:237-264`, `send_evidence.py:272-306`), never a flag on a witness,
   selected by `external_outcome` and never by `error_code`.

**EV-5.** A direct API path produces **no** governed evidence and must never be
converted into any. This is GE-4(3) restated for evidence, and it is enforced by the
tool-scoping in EV-4(2) rather than by convention.

**EV-6 — no duplicate suppression, on purpose.** Both witness sets are filters, never
truncations, and deliberately do not de-duplicate (`evidence.py:222-228`,
`send_evidence.py:251-263`). A resumed run may legitimately record the same creation
twice; the verdict is existential, so a repeat is neither double-counted nor dropped.
An implementation that collapsed them would be asserting that the *count* matters. It
does not.

---

## 10. Recovery Equivalence Rule

**Definition RC-1.** Two paths to one capability are **Recovery Equivalent** iff, for
every external outcome, both paths reach the *same* conclusion about whether the effect
occurred, and neither permits a re-execution that the other would refuse.

**RC-2 — the six cases, evaluated at `HEAD` (where two paths existed):**

| Case | Governed path | Direct path (`HEAD`) | Equivalent? |
|---|---|---|---|
| **timeout / no answer** | `PROVIDER_UNREACHABLE` ⇒ `external_outcome=UNKNOWN` ⇒ INDETERMINATE ⇒ `REQUIRES_ATTENTION` | none — the caller's HTTP request simply failed | **No** |
| **lost response after commit** | as above; the provider may have applied it and KAI says so | same — and KAI never learns it | **No** |
| **provider success + local crash before evidence** | on resume: no witness ⇒ INDETERMINATE, *not* COMPLETED | crash is invisible; the caller may retry freely | **No** |
| **retry** | refused — approval spent ⇒ `APPROVAL_STALE` (`gateway.py:161-164`) | unconstrained | **No** |
| **reconciliation** | none implemented; INDETERMINATE parks for a human | none | **Both none** |
| **duplicate prevention** | single-use approval only | none | **No** |

**RC-3 — the answer to "must recovery semantics be equal?" is: not as models, but as
*conclusions*.** The two paths need not share a mechanism. They must not be able to
reach *different answers about the same event*. At `HEAD` they could: the direct path
could report "failed" and be retried into a duplicate, while the governed path reported
"unknown" and parked. One provider call, two contradictory conclusions, no shared
authority to adjudicate. **That divergence is the actual defect** — not the absence of a
recovery model on the direct path.

**RC-4 — post-retirement.** RC-3 is now satisfied for both pairs *vacuously*: there is
one recovery model and one path. The rule's value is prospective — it is what forbids
re-adding a second path.

**RC-5 — the remaining recovery gap, stated honestly.** Recovery is fail-safe against
false completion and **not** duplicate-safe independent of the provider. Specifically:

- Fail-safe: no auto-`COMPLETED`; `INDETERMINATE` ⇒ `REQUIRES_ATTENTION`; no auto-return
  from `WAITING_EXTERNAL` (`tasks/state.py:92-110`); lost claim aborts without writing
  (`service.py:1369`).
- Not duplicate-safe: `crash-after-dispatch-before-evidence` + `explicit RETRY` can
  re-issue the write. The state graph permits the route
  `WAITING_EXTERNAL → REQUIRES_ATTENTION → RETRY → EXECUTING` **by design**
  (`tasks/state.py:101-104`), and the only thing stopping the re-dispatch is the spent
  approval.

This is a **documented, deliberate trade**, recorded at `tasks/state.py:104-109` and
`send_evidence.py:251-263`. It is not a defect to be fixed in this round; it is
**PRQ-2** (§14).

---

## 11. Idempotency Boundary

### 11.1 The correct level: the **execution contract**, and only there

Four candidate levels were tested against the evidence:

| Candidate | Verdict |
|---|---|
| **The provider adapter** | **No.** An adapter cannot dedupe: it holds no durable state and does not know whether it is being re-driven. `create_and_verify_event` is called once per dispatch and has no memory (`calendar/service.py:188-223`). |
| **The entrypoint** | **No.** Entrypoints are the thing being unified away. An entrypoint-scoped idempotency property would be trivially bypassed by using the other entrypoint — which is exactly the CAP-009↔CAP-021 defect. |
| **The capability** | **Partially.** Right *scope*, insufficient *mechanism*: a capability has no durable store of its own. |
| **The execution contract** | **Yes.** This is where the durable facts live (`RE.evidence`, `TaskPlan`, `Approval`) and where a re-drive can be recognised. |

**Decision ID-1.** Idempotency is a property of **the execution contract** — concretely,
of the pair *(sealed Task intent, single-use approval, cursor)* — and of nothing below
it. The adapter is the **last** layer at which idempotency may be implemented, never the
first, because a provider key that is not bound to the intent hash would authorise a
different execution than the one that was approved.

### 11.2 Can two paths to one external effect have different idempotency semantics?

**Yes — and that is only safe when the non-governed path cannot re-dispatch.**

At `HEAD`, CAP-009 and CAP-021 could: the governed path's re-drive was refused by a
spent approval; the direct path had no such refusal, so a client retry produced a
second event. Two different idempotency semantics over one external effect is
**categorically unsafe** whenever both paths are live, because idempotency is a property
of the *effect*, and the caller chooses the path.

**The boundary that makes divergence safe** (ID-2) is a disjunction, and only the first
clause is acceptable:

- ❌ *"the direct path is read-only"* — false for a write.
- ✅ **"at most one live path can dispatch the effect, and the non-dispatchable path is
  structurally incapable of dispatching"** — CAP-021/CAP-022 satisfy this by returning
  `410` (`api/calendar.py:194`, `api/gmail.py:193`). Not by policy: by having no code
  path to the provider.

This is why the retirement was implemented as a tombstone rather than a policy flag.

### 11.3 The honest current state

| Property | Status | Evidence |
|---|---|---|
| Idempotency key generated and sent to the provider | **Absent** | zero hits for `idempot*`/`dedup`/`reconcil` in `src/` except approval dedup and alert dedup |
| `request_id` used as a dedupe key | **Absent** | generated per invocation (`gateway.py:536`), never compared |
| Duplicate dispatch prevented by approval single-use | **Present** | `approvals/service.py:259-272` |
| Duplicate *fulfilment* prevented | **Not attempted, by design** | existential verdict (`evidence.py:222-228`) |
| Reconciliation of an unknown outcome | **Absent** | no query-then-decide path exists |

**ID-3.** KAI currently has **exactly one** duplicate-dispatch defence, it lives in the
approval record, and it disappears the moment a write is permitted without approval.
That sentence is the single most important thing in this section, and it is
**PRQ-2**.

---

## 12. Final Authority Matrix

| Concern | Authoritative Owner | Enforcing site | Rejected alternatives |
|---|---|---|---|
| **User Intent** | the caller's goal text, sealed once | `tasks/service.py:100-140`; `build_slice_contract` `translator.py:53-90` | planner/model proposals (`planner/model.py:51` forbids permission fields) |
| **Task Contract** | the `Task` row; written once, no update path | `tasks/models.py`, `create_task` | IR nodes (`ir/` is derived); `TaskPlan` (versioned progress, not authority) |
| **Policy** | `PolicyEngine` + sealed rule sequence | `engine.py:25-38`; invoked `gateway.py:125,183` | the gateway's own ordering; content authorizer (advisory input to policy, not a substitute) |
| **Approval** | `ApprovalService` on the `Approval` row | `approvals/service.py:169,230` | the API decision endpoint alone (cannot execute); a boolean on the task |
| **Identity** | `IdentityResolver` → status → policy | `gateway.py:352-372`; `rules.py:176-200` | memory hints (context only); message content |
| **Capability Authorization** | sealed registry **∧** `rule_allowed_tools` **∧** IR capability subset | `registry.py:39-45`; `rules.py:85-97`; `ir/build.py:311-331` | the runtime's capability table alone (outside the gateway it protects nothing) |
| **Provider Execution** | the adapter, reached only as GW step 13 | `gateway.py:210`; `adapters/*.py` | the API layer (the retired defect); the IR (no I/O) |
| **Evidence** | `RE.evidence`, append-only, one record per invocation | `runtime/service.py:841-852`; `evidence.py` / `send_evidence.py` | `ToolResult.data` (never verified); a separate evidence table |
| **Verification** | `ContractVerificationEngine`, re-asserting the declared contract | `stubs.py:155-196`; `gateway.py:215` | the adapter's own `verified` flag (it is a *claim*; CVE is the *vouching*) |
| **Fulfillment** | pure evaluators, selected by the **sealed** profile | `fulfillment.py`; `service.py:1591-1679` | evidence qualification (`evidence.py:31-35`); the task state |
| **Task Lifecycle** | `resolve_transition` via `transition_task` — single writer | `tasks/state.py:131`; `tasks/service.py:226` | any model, planner or IR node; the gateway |
| **Recovery** | claim + lease + fence, then the state machine | `runtime/service.py:1242-1414`; `tasks/state.py` | provider retry; a generic reconciliation service (none exists) |
| **Idempotency** | **no dedicated owner.** Emergent from *approval single-use* + *cursor*; provider idempotency **absent** | `approvals/service.py:259-272`; `service.py:764-773` | a provider idempotency key (**does not exist**); the state graph (`tasks/state.py:104-109` says so explicitly) |
| **Audit** | `RE.evidence` + task state + safe structured log | `service.py:841-852`; `gateway.py:518-537` | arguments or tokens in logs (excluded by construction) |

### 12.1 Do the duplicate pairs break this matrix?

**No.** Verified concern by concern:

| Concern | Broken by CAP-009↔021 or CAP-010↔022? |
|---|---|
| User Intent | No — one source (`build_slice_contract`) |
| Task Contract | No — one row, one sealing gate |
| Policy | No — one engine, one owner |
| Approval | No — one service, one CAS |
| Identity | No — one resolver |
| Capability Authorization | No — one sealed registry + one rule |
| Provider Execution | No — one adapter, one caller |
| Evidence | No — one append-only log, one boundary per domain |
| Verification | No — one CVE for both paths |
| Fulfillment | No — one profile → one evaluator |
| Task Lifecycle | No — one machine |
| Recovery | No — one claim owner |
| Idempotency | No — one approval record |
| Audit | No — one sink |

**At `HEAD` this matrix had exactly two duplicated owners — Provider Execution and
Recovery — and both were reachable without the other twelve.** That is the whole finding,
stated as a matrix property: a duplicated *effect* owner beneath a single *governance*
owner is always a defect, because nothing upstream can constrain it.

---

## 13. Hidden Authority Findings

Six capabilities were re-examined for the ability to grant permission, bypass approval,
mutate lifecycle, declare fulfilment, create authoritative evidence, or select a
provider effect outside the matrix above. Three findings.

### HA-001 — Alert delivery is an ungoverned external side effect on the transition path

**Severity: Medium. Type: second external-effect authority.**

`tasks/service.py:356` awaits `alerting.notify_task_transition(...)` **inline inside
`transition_task`**. That reaches `deliver` (`observability/alerting.py:232-261`), which
issues `httpx.post` to a configured URL.

Against §12, `deliver`:
- selects a **provider effect** (an outbound HTTP call) — outside the matrix;
- creates **no governed evidence** — the `TaskAlert` row is an operational record, not
  an `RE` record;
- is covered by **no** policy, approval, identity or verification;
- has its **own** idempotency model — at-least-once with a stable `id`
  (`alerts/models.py:42-44`), which is correct and is *not* the KAI model.

It **cannot** grant permission, bypass approval, mutate lifecycle, or declare
fulfilment — the four most dangerous capabilities. It is nonetheless a second authority
for *external effect*, and it is the only one remaining.

**Mitigating facts, which are why this is Medium and not High:** it is
application-configured (not caller-controlled); `deliver` never raises, so it cannot
corrupt a transition (`alerting.py:237-239`); the URL is redacted to scheme+host in logs
(`:202-215`); and the payload is task metadata, not task content.

**Status: recorded, not fixed. → PRQ-4.**

### HA-002 — Task Contract sealing has two entrypoints with different caller authority

**Severity: High (architectural). Type: second authority for Task Contract.**

| | `POST /runtime/tasks` (CAP-001) | `POST /tasks` (CAP-032) |
|---|---|---|
| Contract source | `build_slice_contract(goal)` — **application-determined** | caller's `TaskContractInput` — **caller-determined** |
| `allowed_tools` | `SLICE_ALLOWED_TOOLS`, 4 tools | caller-supplied, **default `[]`** |
| `risk_policy` | `MEDIUM` | caller-supplied, **default `LOW`** |
| `fulfillment_profile` | `calendar_meeting` | caller-supplied, **gated** by `LIVE_SEALABLE_PROFILES` |
| Reaches the runtime? | yes, via `/runtime/tasks/{id}/trigger` | **yes, via the same trigger** |

Both land in the same `Task` table and are driven by the same `AgentRuntime`. So the
**Task Contract** row in §12 has two producers with materially different authority, and
neither is bounded by the other.

**I searched for an exploitable bypass and did not find one.** Specifically:

- `risk_policy="CRITICAL"` does **not** bypass approval — `rule_risk_approval` reads the
  **tool's** declared risk, not the task cap (`rules.py:203-212`), so MEDIUM always
  escalates. The cap only escalates further (`RISK_POLICY_CAP`).
- `allowed_tools=[]` does **not** widen the executable set — `allowed_capabilities_from_task`
  raises `IrValidationError` on an empty result (`ir/build.py:327-330`), and
  `_program_for` converts that to `RuntimeFatal` (`service.py:1126-1127`).
- `fulfillment_profile="gmail_send"` is refused at the single write
  (`tasks/service.py:114-118`).

**But the defence-in-depth is inverted.** `rule_allowed_tools` — the *gateway's own*
allowlist rule — treats an empty list as **permit everything** (`rules.py:233-235`). The
only thing preventing a caller-created, empty-allowlist task from reaching any
registered tool is two layers **outside** the gateway. A gateway that is correct in
isolation is not what is protecting this; a stack of upstream gates is.

This is latent today and becomes live the moment anyone tries to drive `gmail.send`,
because driving it requires relaxing exactly those gates (§6.9).

**Status: recorded, not fixed. → PRQ-1 (blocking for §6.9).**

### HA-003 — Token refresh is an external side effect inside a provider call

**Severity: Low. Type: internal effect, correctly scoped.**

`usable_access_token` (`google/service.py`) performs a refresh `POST` to the token
endpoint, invoked inline by every provider service before the real call
(`calendar/service.py:204`, `gmail/service.py:182`). It is therefore an outbound
external effect on the CAP-009/CAP-010 path that appears in no contract.

It is **not** a defect: the refresh credential belongs to the same account, is
encrypted at rest (`google/crypto.py`), is a protocol obligation rather than a business
effect, and cannot be aimed elsewhere — the account is task-derived. Recorded so that a
future "all external effects pass the gateway" sweep does not treat it as a bypass.

**Status: documented, no action.**

### 13.1 Checked and found clean

| Candidate | Finding |
|---|---|
| `POST /tasks/{id}/transition` (CAP-024) | cannot declare fulfilment — `CLIENT_TRANSITION_TARGETS` excludes `COMPLETED`, `VERIFYING` and every success state; anything else raises `InvalidActorError` (`tasks/service.py:66-68, 299-302`) |
| `POST /approvals/{id}/approve` (CAP-016) | cannot approve itself out of band: binds to `intent_hash`, transitions only from `PENDING` (`approvals/service.py:495-502`) |
| Planner / model output | cannot set `allowed_tools`, `risk_policy`, permissions or model config (`planner/model.py:51`); IR nodes cannot decide policy, approval or fulfilment |
| Memory | context-only; a PERSON hint never upgrades identity status |
| Direct read APIs (CAP-007/011/012/023) | cannot create governed evidence — the witness filters are tool-scoped (`evidence.py:199`, `send_evidence.py:227`) |
| `tools/stubs.py` | `ContractVerificationEngine` is **production**, not a test double; the other three stubs are CLASS-E |

---

## 14. Implementation Prerequisites

Deliberately split to prevent over-engineering. Each pair gets all three headings.

### 14.1 Calendar Create (CAP-009 / CAP-021)

**Must be unified before implementation** — nothing remains. Provider Args, Side
Effect, Verification and Intent already resolve to one authority; the seven divergent
concerns were closed by retiring the second path. Any *new* Calendar Create surface must
satisfy GE-1 for all nine concerns or not exist.

**May remain path-specific:**
- the runtime's *sequencing* choice of which created event to read back —
  `_created_event_id` takes the earliest qualifying creation
  (`runtime/service.py:1682-1704`) and is explicitly a sequencing decision that cannot
  reach the verdict;
- the *content* of the calendar criteria (window + required attendees), which is
  domain-specific and correctly so;
- operational concern placement: the governed path's `_external_outcome_unestablished`
  runs at the runtime layer (`service.py:1544`), not inside the gateway.

**Must NOT be unified:**
- the witness filter and the evaluator — they are different questions
  (`evidence.py:31-35`);
- `QualifyingCreation` and `QualifyingSend` — parallel, not merged. Merging them would
  force the thread-identity omission (`send_evidence.py:192-196`) onto creations, or
  invent a shared base carrying fields neither domain needs;
- the three-valued verdict into two values, for either domain.

### 14.2 Gmail Send (CAP-010 / CAP-022)

**Must be unified before implementation** (= before `gmail.send` is ever driven):
1. **HA-002 closed** — `rule_allowed_tools` made fail-closed on an empty allowlist.
2. A declared, sealed position on retry-after-`INDETERMINATE` (§6.9.4).
3. `gmail_send` added to `LIVE_SEALABLE_PROFILES` with the understanding intent and
   capability table that implies (`service.py:1608-1617`).

**May remain path-specific:** all read concerns (§4.3); the gmail criteria's content
(recipients only — `fulfillment.py:470-473`); `GmailSendCriteria`'s deliberate omission
of `subject`/`body` (`fulfillment.py:302-307`).

**Must NOT be unified:**
- **sender derivation with anything caller-supplied** — keep it a type-level
  impossibility (§6.1);
- `evaluate_gmail_send`'s `INDETERMINATE` branch into `NOT_FULFILLED`
  (`fulfillment.py:720-727`) — collapsing them is what produces a blind double send;
- the "proof outranks an unknown" precedence (`:758-762`) — dropping it would park
  tasks that are provably done.

### 14.3 The three live read duplicates (CAP-007/011/012 + direct)

**Must be unified:** nothing. GE-2 gives them a one-concern set.

**May remain path-specific:** everything, including verification and account selection.

**Must NOT be unified:**
- the direct read into the witness set (EV-4.2);
- the direct read into task lifecycle or fulfillment (GE-4.3);
- any account-selection change that would re-introduce a caller-supplied `account_id`
  on the *governed* path — that path is stronger without it and must stay so.

### 14.4 Prerequisite register

| ID | Prerequisite | Severity | Blocks |
|---|---|---|---|
| **PRQ-1** | Make `rule_allowed_tools` fail-closed on an empty `allowed_tools` (HA-002) | High | **any** drive of `gmail.send`; any future catalog addition |
| **PRQ-2** | Declare the idempotency position: single-use approval is the *only* duplicate-dispatch defence, and it vanishes for approval-free writes (ID-3) | High | any write at LOW risk; any retry-after-UNKNOWN policy |
| **PRQ-3** | Seal the retry-after-`INDETERMINATE` answer for sends | Medium | `gmail.send` drive |
| **PRQ-4** | Decide whether alert delivery belongs inside the governed boundary or is declared out-of-scope external effect (HA-001) | Medium | any "single external-effect authority" invariant |
| **PRQ-5** | Reconcile the 5 failing tests (all one root cause: untracked `alembic/versions/a1b2c3d4e5f7_add_alerts.py`, head `a1b2c3d4e5f7`, plus the `alerts` table). Tests assert frozen values `REVISION_HEAD == d4e5f6a7b8c9` (`tests/test_tasks.py:991`), 12 migrations (`tests/test_identity_single_gate_lock.py:503,996`), and a table-comment set without `alerts` (`tests/test_schema_metadata_parity.py:109`) | Low | nothing architectural; **it is the same class as the prior round's FINDING-002 and has recurred** |

### 14.5 PRQ-4 / HA-001 — decided

Appended under the same convention as §10 and §15: the §14.4 register rows above are the record
of what was open when, and are left exactly as written. The closure of **PRQ-4** is registered
here rather than by rewriting its row, because rewriting the row would erase the fact that the
question was open at all.

```text
PRQ-4 / HA-001
Decision: OUT-OF-GOVERNED-EXECUTION
Status: ACCEPTED / DECIDED

Alert delivery is an operational observability effect, not a governed
business side effect. It is emitted after durable task-state commit and
cannot participate in Policy, Approval, Identity, Evidence, Verification,
Fulfillment, or lifecycle authority.
```

**Basis.** `src/kai/tasks/service.py:320` is the only assignment to `task.state` in `src/`, so the
alert seam is the sole notification origin — a property of notification *completeness*, not of
business authority. `transition_task` commits first (`tasks/service.py:342`) and alerts after
(`:356`), discarding both return values (`:356-357`). `deliver` swallows every exception
(`observability/alerting.py:248-259`) and `notify_task_transition` never raises. The payload is a
fixed ten-field tuple carrying no goal, `last_error`, provider text or webhook URL
(`alerting.py:31-39`, `:74-85`, `:202-215`). `src/kai/alerts/models.py:20-22` self-declares the row
operational and states that `external_outcome` and the fulfillment verdict live on the execution
row, written only from evidence. Alert loss is never business loss, which is why the finding is
Medium rather than High.

#### 14.5.1 Intended invariants

Stated as contract so a later change cannot break them silently. Not implemented by this decision;
items 4, 5 and 7 are already true and already test-covered.

1. Alert delivery is not ToolGateway business execution.
2. Alert delivery is not Policy / Approval / Identity authority.
3. Alert delivery is not fulfillment evidence.
4. Alert failure must not fabricate business failure.
5. Alert failure must not convert successful execution into `FAILED`.
6. Alert delivery must not authorize or trigger a provider mutation.
7. Observability failure must not become a second lifecycle authority.
8. Any future alert retry policy must not alter business semantics.
9. `Alert` remains CLASS-D operational data; acknowledgement/resolution must never gate settlement.
10. Off-by-default alert configuration must remain metric/operational only and must not gate
    execution.

#### 14.5.2 Known gap

```text
Known gap:
A process failure after durable task-state commit but before alert delivery
may lose the notification. This is an observability/delivery reliability
gap, not a business-execution correctness failure.
```

The loss is unrecoverable by re-raise rather than merely unlikely: `FAILED -> FAILED` is not a legal
edge, so nothing re-fires it. This is already recorded honestly in `docs/ALERTING.md` ("a real gap,
not a solved problem"), and no durable outbox is authorised by this decision. The `alerts` table and
its migration exist, but nothing in `src/` inserts or reads an `Alert` row, and the operator
lifecycle columns are written by nobody; the outbox remains unimplemented.

### 14.6 Transport classification in `provider_failure` — decided

Filed under the same convention as §14.5: the §14.4 register rows above are the record of what was
open when and are left exactly as written. This closure is registered as a new subsection rather
than by editing any row or any architecture document. No `DECISION AMENDMENT` is issued, because
nothing previously recorded was wrong.

```text
Transport classification / provider failure codes
Site: src/kai/tools/adapters/base.py (provider_failure)
Decision: ALIGN PRODUCER TO ESTABLISHED TAXONOMY
Status: ACCEPTED / DECIDED

The implementation is bringing the producer into alignment with the
already-established taxonomy and recovery semantics in
`src/kai/tools/errors.py` and `src/kai/runtime/service.py`.
It does not create a new rule.
```

The accepted model is:

```text
connection never established
    -> PROVIDER_NOT_CONTACTED
    -> known non-application
    -> normal retry remains permitted

request dispatched / outcome unavailable
    -> PROVIDER_UNREACHABLE
    -> unknown external outcome
    -> no automatic redispatch

HTTP 5xx / equivalent server-side uncertainty
    -> PROVIDER_SERVER_ERROR
    -> unknown external outcome
    -> no automatic redispatch

HTTP 4xx / explicit provider refusal
    -> known answer / failure
    -> not INDETERMINATE
```

**Basis.** The taxonomy is not new and is not owned by this change: `errors.py` is clean at this
baseline and already declares `PROVIDER_NOT_CONTACTED` (`errors.py:23`, documented at `:41` as "no
connection was ever established"), `PROVIDER_UNREACHABLE` (`:22`), `PROVIDER_SERVER_ERROR` (`:24`),
`UNRESOLVED_EXTERNAL_CODES = {PROVIDER_UNREACHABLE, PROVIDER_SERVER_ERROR}` (`:53-54`) and
`is_unresolved_external` (`:58`). `runtime/service.py` already depends on that taxonomy:
`_external_outcome_unestablished` (`service.py:1544`) cites `is_unresolved_external` directly
(`:1554`), and the parking path writes `external_outcome = UNKNOWN` at `:844-850` and `:889`.

The gap this change closes is that **nothing in `src/` ever produced those two codes.** An
exhaustive search for `PROVIDER_UNREACHABLE` / `PROVIDER_SERVER_ERROR` outside `errors.py` returns
no hits, so at this baseline `is_unresolved_external()` can only ever be reached via
`APPROVAL_STALE`, which the same function explicitly distinguishes as "an authorization fact, not
an outcome" (`service.py:1559-1560`). The producer was therefore inconsistent with the taxonomy the
runtime had already been written against.

The recovery semantics it restores are equally pre-existing: `FAILED -> RETRY` is a legal edge
(`tasks/state.py:114`), and `apply_retry_budget` (`state.py:156`, driven from
`tasks/service.py:310`) re-dispatches on it. At the same time `errors.py:44-45` defines a 4xx as
"positive evidence that it did not apply" — so collapsing a dispatched-but-unknown transport
failure into the caller's default `PROVIDER_ERROR` (`base.py`, the pre-change default) records an
ambiguous outcome as known-not-applied, sends the task to `FAILED`, and re-arms the budgeted retry
blindly. The three-way split is what makes the pre-existing `errors.py` contract and the
pre-existing `service.py` parking path reachable at all.

Callers are unchanged: `calendar.py:107,152,187`, `gmail.py:114,140,171`, `contacts.py:67` all still
pass `codes=_PROVIDER_CODES`, and the `default` moves from the literal `"PROVIDER_ERROR"` to the
identical constant `errors.PROVIDER_ERROR`. No caller, signature, gateway rule, policy rule,
approval rule, evidence record, migration or schema is touched by this decision.

**Known cost, accepted with the decision.** Classifying dispatched-unknown failures as unresolved
moves them out of budgeted retry and into `REQUIRES_ATTENTION`. That is the intended direction
(a human or a later explicit policy decides, the process does not redispatch blind), but it raises
the expected `REQUIRES_ATTENTION` volume against the providers for as long as transport failures
occur. Rejected explicitly: treating a bare `httpx.TransportError` as "never contacted" — httpx
names the specific subtype when it can, so the unqualified type is the unsafe direction to be
wrong in.

**Promotion coupling.** This decision does not stand alone as a one-file unit. The runtime fields
that publish its consequence (`external_outcome`, plus `ir_fingerprint`) are declared in
`src/kai/runtime/schemas.py` and proven by `tests/test_runtime.py`, so the coherent promotion unit
is the three together. `tests/test_governed_write_path.py` is the direct proof of the
classification itself. See §14.8.

### 14.7 Identity identifier gating for `CONFLICTING` / `UNKNOWN` — blocked

Registered as a blocked item only. **No decision is taken here on the substance**, and specifically
no position is taken on whether the Canonical Architecture is wrong. The architecture document is
**not** amended by this entry.

```text
Identity identifier gating (identity/service.py _SELECTING_STATUSES)
Decision: NONE — NO DECISION TAKEN
Status: BLOCKED pending explicit authority reconciliation
Action: EXCLUDED from the current promotion

The working-tree identity change is blocked pending explicit authority
reconciliation because it contradicts documented Canonical Architecture
behaviour for CONFLICTING results.
```

**What is excluded.** `src/kai/identity/service.py`, `tests/test_identity_grounding_methodology.py`,
`tests/test_identity_approval_binding_design.py` and `tests/test_gmail_approver_visibility_design.py`.
This is treated as a separate identity-semantics decision, not as part of any other promotion unit.
The working-tree change is left in place: it is not reverted, modified or accepted.

**Why it is blocked rather than rejected.** The working tree adds `_SELECTING_STATUSES`
(`identity/service.py:61`, gates at `:267`, `:271`, `:287`, `:296`) so that `UNKNOWN` and
`CONFLICTING` resolutions hide *every* candidate identifier. The Canonical Architecture documents
the opposite for `CONFLICTING`: its identity-resolution table (AR `18050e4`, L1368 and L1369) shows
`CONFLICTING` reported with `email` present and `contact_id` absent, and AR L1375 shows the same for
a bound-account conflict; AR L1381 states that `CONFLICTING` stays independent of match count
altogether. AR's own flagged defect list names a different row — an `UNKNOWN` resolution reported
with a match — whose stated remedy is a single-distinct-identifier gate, which the pre-change code
already implements (`identity/service.py:251-252`). So code and architecture disagree, and this
register does not have authority to decide which side is correct. That is what "pending explicit
authority reconciliation" means.

The change is also not confined to resolver output shape: it flips the assertions the pre-existing
tests pin, `result.email == ALICE` for a `CONFLICTING` verdict to `result.email is None`, and
`for_gmail_send().required_recipients == frozenset({ALICE})` to `frozenset()` — a downstream effect
on **approval recipient-intent criteria**. That is a further reason the ruling belongs to an
authority decision rather than to this promotion.

**Authority caveat — recorded as required.** This reconciliation cannot be *final* while the
authoritative architecture document is itself disputed in the working tree:
`docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md` is double-encoded and BOM-corrupted in the
working copy, so the line references above are quoted from the clean `HEAD` version
(`18050e4`). The corruption is a separate document-integrity issue and is **not** repaired,
regenerated or restored by this promotion. Any ruling on the identity change should be made against
the corrected AR text, and should be recorded here as §14.7 closure when issued.

**§14.7 closure — issued by the human decision gate on 2026-10-06.**

The authority caveat is resolved first, in the order the gate required. `D.2 = CONFIRM` recorded
the clean `HEAD` copy of the Canonical Architecture as canonical and classified the working-tree
copy as discardable encoding corruption; the working-tree copy was restored byte-identical to
`HEAD` (BOM/CRLF/cp1256 mojibake only, no text edit). Every reference in this entry therefore
quotes the clean canonical text. `D.3 = CONFIRM` and the `D.4` tracking decisions are recorded in
§14.9.

```text
Identity identifier gating (identity/service.py _SELECTING_STATUSES)
Decision: AUTHORIZED (D.1 = AUTHORIZE)
Status: CLOSED
Action: PROMOTED AS STANDALONE UNIT (identity unit only)
Promotion executed: commit c8dd269 `feat(identity): promote the _build writer-tightening for
                     non-selecting verdicts` — 4 files, +200/-47 (service.py + 3 test files).
Working tree left as-is for everything else; the documentation/register unit updates in this
file, DOCUMENTATION_STATUS.md and the AR/matrix restorations remain UNCOMMITTED pending a
separate explicit commit instruction.

Semantic contract authorized:
- CONFLICTING publishes no email / phone / contact_id / display_name in IdentityResult.
- Reachable UNKNOWN is unchanged.
- Confirm positive statuses (CONTACT_MATCH / PROVIDER_VERIFIED / CHANNEL_VERIFIED) are unchanged.
- No Gateway change, no Identity-architecture change, no FactKind/capability-vocabulary growth.
```

Recorded scope boundary (not authorized, not touched): `gmail INDETERMINATE` behaviour in
`src/kai/runtime/fulfillment.py` / `src/kai/runtime/send_evidence.py` remains
`UNAUTHORIZED — SEPARATE PROMOTION UNIT`. Unknown downstream dependency: contract applies to
`IdentityResult` even though the live persistence path is currently guarded by
`runtime/service.py:696` (reachability is not the authorization basis).

Known limit recorded with the closure (not fixed, out of scope): `IdentityResult.evidence`
still carries `contact_id` inside each `IdentityEvidence` under `CONFLICTING`; the authorized
contract gates the top-level identifier fields only, and `evidence` is preserved so conflict stays
auditable.

Validation evidence: identity-unit tests + full suite + `ruff`/`mypy` — recorded in §14.7.1 below.

#### 14.7.1 Validation evidence (recorded 2026-10-06)

```text
Identity unit tests: 72 passed
  (tests/test_identity.py, test_identity_grounding_methodology.py,
   test_identity_approval_binding_design.py, test_identity_single_gate_lock.py,
   test_gmail_approver_visibility_design.py)
Full suite: 1302 passed, 9 skipped, 1 failed
  sole failure: tests/test_runtime.py::test_migration_is_rollbackable_and_forward
  - DB head 'a1b2c3d4e5f7' (excluded untracked alerts migration) vs frozen
    REVISION_HEAD 'd4e5f6a7b8c9'. Pre-existing artifact of the EXCLUDED alerts
    cluster (Register §14.8.1 / §16.4), not identity-related; identity adds no
    migration and touches no schema.
ruff: all checks passed (identity-unit files)
mypy: no issues found in 133 source files
```

---

### 14.8 Promotion boundary decided with §14.6

Not a prerequisite row and not a new PRQ: recorded so the boundary is auditable.

```text
Promotion unit: 14 files (13 tracked-modified + 1 untracked)
Decision: AUTHORISE AS ONE COHERENT UNIT
Status: ACCEPTED / DECIDED
Promotion itself: NOT YET DONE — validation first, no staging or commit
```

P0's proof requires `tests/test_tool_security.py`, and that same file carries the PRQ-1 proof;
PRQ-1's production semantics require its harness and consequence-test changes. Splitting that file
would mean editing it solely to manufacture a smaller boundary, which is not a reason. The
identity unit, the alerts cluster, `tasks/state.py`, the client refactors, the AR repair and the
unrelated documentation cleanup all remain excluded. See `docs/plans/PHASE_2F_RECOVERY_GAP_CLOSURE_PLAN.md`
§11 for the P0/P1 evidence these depend on.

#### 14.8.1 Validation result (recorded as evidence, not as a decision)

The authorised 14-file boundary was validated by building a throwaway tree outside the repository
that is byte-proven equal to `HEAD` plus exactly those 14 files, and running the suite against it.
**The 14 files are not self-sufficient.** Two gaps were found, each by experiment and each
reproducible:

1. `tests/test_governed_write_path.py` (promoted) constructs `GoogleApiError(..., status_code=…)`
   and `GoogleNotFoundError(..., status_code=…)`. At `HEAD` those constructors take no keyword
   argument, so 5 of its tests fail. The keyword exists only in the working-tree
   `src/kai/calendar/client.py`. This is not merely a test convenience: `provider_failure` reads
   `getattr(exception, "status_code", None)` (`base.py:153`) and **nothing at `HEAD` attaches that
   attribute**, so without the client changes the third case of the §14.6 model — *HTTP 5xx →
   unknown outcome* — is unreachable in production for every Google API error. `gmail/client.py`
   and `contacts/client.py` are in the same position (0 `status_code=` sites at `HEAD`, 6 and 5 in
   the working tree) and are required for the same reason on their adapters.
2. `src/kai/api/gmail.py` (promoted) retires the direct send endpoint, but `HEAD`'s
   `tests/test_gmail_authority_review_design.py` still asserts that endpoint exists
   (`call_sites == ["api/gmail.py", "tools/adapters/gmail.py"]`). Without promoting that test,
   2 tests fail against the promoted API.

With those four files added the isolated tree is `HEAD` + exactly **18 files** (17 tracked-modified
+ 1 untracked), proven by content comparison, and the isolated result is **5 failed, 1229 passed,
9 skipped**. All 5 remaining failures were separately reproduced at *pure* `HEAD` with no unit files
present: they are the shared PostgreSQL database sitting at the excluded alerts revision
`a1b2c3d4e5f7` while the on-disk migration set is `HEAD`'s 12 — i.e. an artefact of the excluded
alerts cluster and the shared test database, not of this unit.

The 14-file authorization above is therefore recorded as issued, and this subsection records what
the evidence showed. The boundary decision required to proceed is **14 vs 18** and is left to the
human decision-maker; nothing has been staged or committed either way.

---

### 14.9 Human decision gate — governance rulings (D.2 / D.3 / D.4) — 2026-10-06

Recorded as issued, verbatim from the human decision gate. These rulings are governance decisions,
not implementation decisions; they change no production code.

```text
D.2 = CONFIRM
  docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md@HEAD is CANONICAL.
  The working-tree copy is encoding corruption only (cp1256 mojibake + BOM + CRLF),
  discardable without semantic loss. RESTORE only: no AR text edit.

D.3 = CONFIRM
  docs/ROADMAP.md is the OFFICIAL ROADMAP.
  Any stale text in docs/architecture/DOCUMENTATION_STATUS.md that contradicts it is
  stale documentation, not competing authority.

D.4 tracking
  AGENTS.md                                = TRACKED_AUTHORITATIVE
  docs/ROADMAP.md                          = TRACKED_AUTHORITATIVE
  KAI_CAPABILITY_BOUNDARY_CONTRACT.md      = TRACKED_AUTHORITATIVE
  docs/architecture/KAI_CAPABILITY_CLASSIFICATION_MATRIX.md = NON_AUTHORITATIVE

Effect of D.4:
- the three TRACKED_AUTHORITATIVE files are registered for tracking as authoritative
  governance documents;
- the Classification Matrix is NOT an authoritative governance document at this stage;
  it is recorded as explicitly NON_AUTHORITATIVE.
```

Consequences recorded here for future rounds:

- Any subsequent internal contradiction inside `DOCUMENTATION_STATUS.md` that pits an old passage
  against `docs/ROADMAP.md` is to be classified **stale docs** (per the documentation discipline:
  fix the document, never treat it as competing authority) and corrected inside an authorized
  documentation round — never used to weaken the roadmap.
- The Classification Matrix may be re-considered only by an explicit later decision; it is not
  promoted in this round.
- Tracking registration (index) for the three TRACKED_AUTHORITATIVE files is part of the
  documentation promotion unit; commitment/release of the unit remains a separate step.

---

## 15. DEC-001 / DEC-002 / DEC-003 Validation

| Decision | Prior status | Status now | Basis |
|---|---|---|---|
| **DEC-001** Core vs Extension boundary | ACCEPTED WITH CONDITIONS (Hybrid Model D) | **ACCEPTED AS-IS** | Its prescription — governed writes affecting fulfilment go through Core, operational ops may stay outside with documented constraints — has been *executed*, not merely agreed. Both governed writes are now single-entrypoint. The hybrid remains the right *rule* precisely because it produced the right *answer* here: it classified both writes as governed and both direct writes as illegitimate. |
| **DEC-002** ToolGateway non-bypassability | ACCEPTED WITH CONDITIONS (conditional by class) | **ACCEPTED AS-IS** | The condition is now **satisfied for every classified capability**: CAP-009 and CAP-010 have no bypass, CAP-021/022 cannot dispatch, the read classes have nothing to bypass (GE-2), and the gateway is non-bypassable for all seven catalog tools (`gateway.py:210` is the sole production dispatch site). |
| **DEC-003** Evidence / Task participation | ACCEPTED WITH CONDITIONS (classify by capability) | **ACCEPTED AS-IS** | The classification it demanded is delivered: §3 assigns every capability a class, and evidence participation follows mechanically — CLASS-A participates, CLASS-B must not, CLASS-F produces none, CLASS-D produces operational records only. |

**No amendment is required, so none is issued.** No `DECISION AMENDMENT` section
appears in this document, and that is a deliberate absence: an amendment would imply a
decision was wrong, and all three were not merely correct but *load-bearing* — each one
predicted the resolution that the code then adopted.

**One forward-looking caveat, recorded as an observation and not as an amendment:**
DEC-001/002/003 were validated against a *write* duplicate. The three **read**
duplicates (§4.3) and the two **contract-sealing** entrypoints (HA-002) were outside
their scope. They do not contradict the decisions — the reads satisfy GE-4 and the
sealing split is bounded by three upstream gates — but they are surface area the
decisions never named. **PRQ-1** is the work that closes it.

---

## 16. IMPLEMENTATION GATE

# OPEN FOR DESIGN-TO-IMPLEMENTATION

### 16.1 Why open, and why only one slice

The gate's stated precondition was *"define the capability classification matrix and
document the boundary contract"*. Both are delivered (§3, §5, §6), so the gate opens.

It opens for **one** slice, chosen because it is the only remaining item that is both
**blocking** and **bounded**:

> ### THE ONE SLICE
> **Make `rule_allowed_tools` fail-closed on an empty `allowed_tools`, and add the
> boundary test that pins it.**
>
> Site: `src/kai/policy/rules.py:226-243` (`_task_allows`), plus
> `tests/test_tool_security.py`.
>
> Semantics: an empty `allowed_tools` currently returns `True` — "a contract without a
> capability fixture permits any application-registered tool"
> (`rules.py:228-235`). Change it to refuse. This makes the gateway's own allowlist
> rule agree with `allowed_capabilities_from_task`, which already refuses the empty case
> (`ir/build.py:327-330`).
>
> Why this one: it is **PRQ-1**, it is the documented prerequisite for driving
> `gmail.send` (§6.9), and it is the single place where the current system relies on
> gates *outside* the gateway to protect a rule *inside* it (HA-002). It changes no
> behaviour on the current slice, because no live task has an empty `allowed_tools`
> (`build_slice_contract` always seals four).
>
> Blast radius: one pure function, one rule in a sealed sequence, one test. It adds no
> state, no migration, no schema, no API surface, and no provider change.

### 16.2 What is explicitly *not* authorised by opening the gate

- `gmail.send` is **not** authorised to be driven. That needs PRQ-1, PRQ-2 **and** PRQ-3.
- No second entrypoint for any write. GE-1/ME-2 forbid it; a proposal must show
  single-owner for all nine concerns.
- No provider idempotency infrastructure (PRQ-2 is a *decision*, not an implementation).
- No change to HA-001 (PRQ-4).
- No merge, refactor, rename, or "cleanup" of `evidence.py` / `send_evidence.py`.
- No test, schema, migration or provider edits beyond PRQ-5's mechanical reconciliation.

### 16.3 Rejected alternative: OPEN for both pairs

Considered and **rejected**. The gate could have opened for the duplicate-pair work
instead. It should not, because that work is **already done in the working tree** — the
retirement of CAP-021/CAP-022 is exactly the first implementation slice the prior round
named, and it is complete. Opening the gate for it again would authorise re-litigating a
finished change.

### 16.4 Baseline evidence

Suite at this baseline: **1292 passed, 9 skipped, 5 failed.**

All 5 failures share one root cause and are **unrelated to governance** — the untracked
migration `alembic/versions/a1b2c3d4e5f7_add_alerts.py` (head `a1b2c3d4e5f7`) and the
`alerts` table have outrun three tests' frozen expectations (PRQ-5). Nothing in the
gateway, evidence, approval, fulfillment or lifecycle path fails.

The dirty working tree is itself the reason the prior round's matrix is stale: it was
written against `1f63fb4`, three feature commits and 24 modified files behind this
baseline.