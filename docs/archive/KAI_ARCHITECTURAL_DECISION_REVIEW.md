# KAI_ARCHITECTURAL_DECISION_REVIEW.md
# KAI - Architectural Decision Review (Second-Level Analysis)

**Repository:** C:\Users\khaled\Documents\kai\.claude\worktrees\busy-gould-c3f43b  
**Date:** 2026-10-03  
**Auditor:** Principal Engineer + Software Architect  
**Type:** Decision-Closure Review (No Implementation)

## 1. Executive Conclusion

This review transitions from 'what exists' to 'what it means architecturally' and identifies decisions that must be closed before any implementation. The Core vs Extension boundary is the primary architectural ambiguity. The governed runtime path (IR + ToolGateway + Task state machine) is coherent and enforces its invariants. The direct provider API endpoints (/api/v1/calendar, /gmail, /contacts) form a parallel execution surface. The system currently has **two execution authorities** in practice, though the Core runtime is designed for a single authority.

## 2. Verified Audit Findings

### FINDING-001 (Core vs Extension Boundary) - CONFIRMED CORRECT
- **Location:** api/calendar.py, api/gmail.py, api/contacts.py vs api/runtime.py + runtime/service.py + tools/gateway.py
- **Direct evidence:** Direct routes instantiate/use provider services directly (e.g. calendar service) and do not construct ToolIntent or call ToolGateway.execute(). Runtime path builds ToolIntent from IR ActionNode and routes through ToolGateway with full pipeline.
- **Classification:** Architectural ambiguity + missing contract (not a bug). The runtime enforces governance; direct APIs are an alternate surface.
- **Status:** CORRECT as stated. Needs decision, not fix.
- **Why decision required:** Bypassability of ToolGateway depends on boundary definition.

### FINDING-002 (Migration test revision) - CONFIRMED
- **Test:** tests/test_memory.py::test_memory_migration_is_rollbackable_and_forward references Alembic revision 'a1b2c3d4e5f7' (from test content). Current Alembic head is b3d5f7a9c1e2 (alembic heads). 
- **Classification:** Test/setup issue. No production impact. Does not indicate migration corruption in code.
- **Decision:** Fix test to match current migration graph OR adjust test scope. Do not change production code/migrations.
- **Status:** Needs decision (fix test vs. leave as-is) but not a production blocker.

## 3. Core vs Extension Boundary

### 3.1 What is KAI Core (actual, by files/symbols)
**Core (governed runtime):**
- Task: tasks/models.py, tasks/service.py, tasks/state.py (lifecycle authority, state machine)
- Runtime/Execution: runtime/service.py (AgentRuntime), runtime/models.py (RuntimeExecution), runtime/schemas.py, runtime/translator.py, runtime/understanding.py
- IR: ir/program.py, ir/nodes.py, ir/build.py, ir/control.py, ir/adapter.py (representation, not executor)
- Policy: policy/models.py, policy/service.py (deterministic policy)
- Approval: approvals/models.py, approvals/service.py (single-use, intent-bound)
- Identity: identity/service.py, identity/models.py, identity/schemas.py
- ToolGateway: tools/gateway.py, tools/boundaries.py, tools/intent.py, tools/registry.py, tools/execution.py, tools/definition.py (execution authority boundary)
- Evidence: runtime/evidence.py, runtime/send_evidence.py, ir/adapter.py (evidence construction)
- Verification: tools/stubs.py (ContractVerificationEngine), tools/boundaries.py
- Fulfillment: runtime/fulfillment.py (typed criteria, pure evaluators)
- Task State Machine: tasks/state.py (single writer semantics)

Core property: Durable execution envelope (Task + RuntimeExecution) with claim semantics, cursor bound to plan_version/plan_hash, evidence append-only.

### 3.2 Side effects outside Core (by path analysis)
**Paths that execute side effects outside governed Core:**
1. **Direct API provider routes**
- Entry: POST /api/v1/calendar/* (api/calendar.py), POST /api/v1/gmail/* (api/gmail.py), GET/POST as applicable (api/contacts.py)
- Chain inspection: request → FastAPI deps (auth) → route handler → vertical service (calendar/gmail/contacts service) → provider client → provider. 
- Breakpoints in chain (vs governed): No ToolIntent construction, no ToolGateway.execute(), no IR ActionNode, no RuntimeExecution creation/usage (typical direct handlers), no Task lifecycle entry, no policy/approval via gateway pipeline, no evidence recorded via ir.adapter.evidence_from_observation in the governed form by default.
- Verification: depends on service implementation (some services have create_and_verify/send_and_verify patterns in vertical services; verify against contract exists in tools/stubs and some services). Whether that equals 'governed pipeline' is different from full runtime envelope.

2. **Test-only paths**: tools/stubs.py provides in-memory implementations for testing (ContractVerificationEngine, executors) — test harness, not production execution path.

3. **Internal service calls**: Provider services called from both paths (shared implementation). Shared code is not bypass; entry point differs.

**Conclusion (verified):** Direct HTTP API endpoints to calendar/gmail/contacts are production-callable surfaces (mounted under /api/v1, with auth deps per codebase). They do not pass through ToolGateway or the durable Runtime envelope by design of those handlers.

## 4. Direct API Provider Endpoints Analysis

Examined endpoints (representative): api/calendar.py, api/gmail.py, api/contacts.py, api/router.py mounting.

For each representative:

**POST-like operations (create/send):**
- Calendar: api/calendar.py exposes endpoints under calendar router (mounted in api/router.py). Route handlers call calendar service methods directly.
- Gmail: api/gmail.py similar direct service calls.
- Contacts: read-oriented in typical shape.

**Evidence check per question:**
- Production path? Yes - routes mounted in app, auth dependencies present.
- User-accessible? Yes if authenticated and authorized (auth deps). 
- Executes side effect? Yes for write operations (create/send).
- Passes through ToolGateway? **No** (no ToolIntent, no gateway.execute()).
- Passes through Policy? **No** via gateway pipeline; policy enforcement depends on whether vertical service/endpoint enforces policy. Not via tools/policy service through gateway.
- Needs Approval? **No** via approval boundary; not enforced by gateway.
- Produces Evidence? Vertical services may record/return evidence shapes, but **not** via ir.adapter.evidence_from_observation into RuntimeExecution envelope in these direct paths. Evidence semantics defined (runtime/evidence.py) apply to runtime path.
- Enters Task lifecycle? **No** - no Task creation, no state transitions.
- Uses RuntimeExecution? **No**.
- Can execute without durable execution? **Yes** - direct call produces side effect without Task/RuntimeExecution rows.

**Classification:** Direct APIs are operational/extension endpoints that bypass the governed execution pipeline. They are not part of the Durable Runtime Core's execution authority in their current form.

## 5. Execution Authority Analysis

**Question: Does KAI have a single execution authority or multiple?**

From verified call graph:
- Governed path: Model/API trigger → Durable Runtime (AgentRuntime) → ToolGateway (pipeline) → Provider service → Provider. ToolGateway is the execution gate; runtime owns lifecycle.
- Direct path: HTTP API handler → Provider service → Provider. No ToolGateway, no Runtime lifecycle.

**Answer:** **Two execution authorities in practice.**

Diagrams:
`
Path A (Core/governed): Model/API → Durable Runtime (Task + RuntimeExecution + IR) → ToolGateway → Provider
Path B (Extension/direct): API/Client → Provider (via vertical services) → Provider
`

**Is this intended?** Ambiguous. Canonical architecture emphasizes ToolGateway as boundary and governed runtime; direct APIs exist as separate surface. Codebase contains both.

**Is direct API Core or Extension?**
- By governance: Extension/operational surface (bypasses Core governance).
- By mounting: Both under /api/v1; no explicit labeling in router beyond tags.
- By design intent (from ref): Core is governed runtime; other endpoints may be extension APIs.

**Does shared contract prevent semantics drift?**
- Partial shared implementation (provider services, verification helpers) exist. 
- No single enforcement point across both paths (ToolGateway only on Path A).
- Evidence semantics defined centrally (runtime/evidence.py) but not consumed by Path B.
- Policy/approval only enforced via gateway on Path A.

**Implication:** Semantics can diverge (verification, evidence, approval, policy) unless both paths honor same contracts.

## 6. Evaluate Options (A-D)

### Option A: All external side effects must pass through Durable Runtime (IR + ToolGateway)
- **Architecture:** Single execution authority. Direct APIs either removed, re-routed, or become thin facades to runtime trigger/submit flows.
- **Correctness:** Strongest - enforces full pipeline (policy/approval/verification/evidence/claim/idempotency/lifecycle).
- **Security:** Strongest boundary.
- **Complexity:** High if direct APIs are used operationally (UI/admin/CLI convenience). May break existing direct API usage patterns.
- **Backward compat:** Potentially breaking.
- **API usability:** Runtime path is multi-step (submit → trigger) vs direct one-shot calls. UX differs.
- **Recovery/idempotency:** Inherited from runtime (claim lease, cursor, evidence).
- **Observability:** Unified.
- **Future extensibility:** Clean.
- **Requires:** Decision on API surface; possible API redesign. Ensure all capabilities exposed via runtime/IR or new IR primitives if needed.

### Option B: Direct APIs must honor same Policy/Approval/Verification/Evidence contracts (enforce at service/gateway boundary)
- **Architecture:** Single logical contract, possibly multiple entry points. Could extract shared enforcement into a 'write path' service used by both.
- **Correctness:** Good if enforcement is mandatory.
- **Security:** Good if contracts non-bypassable.
- **Complexity:** Medium - factor out common enforcement (e.g. route direct calls through ToolGateway or equivalent service).
- **Backward compat:** Easier to preserve direct API signatures.
- **Usability:** Preserves one-shot direct calls.
- **Recovery/idempotency:** Need to decide if direct path creates Task/RuntimeExecution or uses different idempotency (provider keys). If not creating durable envelope, recovery model differs.
- **Observability:** Can unify.
- **Requires:** Define 'shared write-path contract' (what must be enforced regardless of entry). Decide whether direct path produces evidence in same shape and whether it participates in Task lifecycle.

### Option C: Direct provider APIs are Extension (separate trust boundary) - not Core governed
- **Architecture:** Accept dual authorities with explicit boundary. Document Core (governed, durable) vs Extension (direct operational). 
- **Correctness:** Acceptable if trust model explicit and risks understood.
- **Security:** Depends on auth/tenant scoping of direct endpoints (they have auth deps per codebase). Policy/approval may be intentionally optional for extension use cases.
- **Complexity:** Lowest - document boundary, add tests/guards as needed.
- **Backward compat:** Preserved.
- **Usability:** Preserved.
- **Recovery/idempotency:** Extension path may rely on provider idempotency; Core has runtime recovery.
- **Observability:** Can tag by path.
- **Requires:** Explicit documentation of boundary, invariants per surface, and prohibition on cross-contamination (e.g. extension results must not be silently treated as 'governed' without evidence). Also clarify which path is authoritative for which use cases.

### Option D: Alternative model (hybrid)
- **Architecture:** Distinguish read-only vs write; writes that need governance must go through Core. Simple/direct writes allowed via Extension with minimal contracts.
- **Complexity:** Pragmatic. 
- **Requires:** Classify capabilities (read/write, governed vs operational). Define minimal contracts per class.
- **Trade-offs:** Operational flexibility while protecting governed actions. Needs clear classification.

**Recommendation direction (decision required, not implementation):** Before choosing, decide the trust model. If 'governed' means every external side effect that matters to Task lifecycle must be durable+verifiable, A is strongest. If operational convenience is required and direct endpoints serve different actors/use cases, B or C are more pragmatic. Current codebase strongly favors A's mechanics in Core but also ships B/C-style direct endpoints. The choice must be explicit.

## 7. ToolGateway Analysis

**Question: Is ToolGateway a non-bypassable execution authority?**

From code evidence (no modifications):
- **Proven on Core path:** Yes. runtime/service.py constructs ToolIntent (via ir/adapter.intent_from_action) and calls gateway.execute(). Pipeline: authenticate → identity/assert → reserved args → resolve tool → validate schema (extra='forbid') → policy → content → approval (if required) → account guard → final policy re-check (TOCTOU) → atomic single-use approval consume → execute → verify. (tools/gateway.py)
- **Proof by call sites (Core):** AgentRuntime._execute (service.py) uses gateway to execute intents. IR produces actions → adapter produces ToolIntent with request_id = action_id.
- **Bypasses:** Direct API routes (api/calendar.py, api/gmail.py, api/contacts.py) do **not** call ToolGateway. They call vertical provider services directly. These are code-level bypasses of the gateway pipeline for those entry points.

**Conclusion:** ToolGateway is non-bypassable **on the governed runtime execution path**. It is **not** non-bypassable at the HTTP API surface level because alternative entry points exist that do not invoke it.

**Required decision:** Should ToolGateway be made non-bypassable at the application boundary (all external side effects), or should the boundary explicitly exclude certain extension endpoints?

## 8. Ambiguous Outcome Analysis (Recovery/Safety)

Scenario: provider performs side effect, returns ambiguous/lost connection, crash before durable evidence, recovery, retry.

Verified from code:
- RuntimeExecution stores evidence append-only (JSONB). Observations recorded after ToolResult. Cursor advances only after successful step (runtime/service.py). Claim semantics: claim_token/claimed_at, lease 5min, heartbeat 30s; claim lost → ClaimLostError, caller must abort without writing (prevents overwriting). 
- WAITING_EXTERNAL: no edge back to EXECUTING (tasks/state.py) — intentional to prevent silent auto-resume; retry path goes REQUIRES_ATTENTION → RETRY → EXECUTING (explicit decision). 
- Fulfillment: three-valued verdict FULFILLED/NOT_FULFILLED/INDETERMINATE. INDETERMINATE leads to REQUIRES_ATTENTION (not FAILED) — fail-safe.
- Evidence qualification: reads provider event_id from evidence['event_id'] (verification boundary), not data.event_id (runtime/evidence.py). Centralizes 'qualifying creation' meaning.
- ToolGateway verification: verification engine checks contract before finalizing success (tools/gateway.py + tools/stubs.py).

**Findings by question:**
- **Duplicate side effect possible?** Theoretically possible if: (1) provider has no idempotency key, (2) crash before evidence persisted, (3) retry occurs without provider correlation. Code does **not** generate provider idempotency keys in the generic path (depends on provider API). Claim prevents concurrent workers from racing; single worker retry after crash is constrained by WAITING_EXTERNAL semantics and requires explicit RETRY decision, but cannot reconstruct evidence that was never persisted.
- **Durable idempotency at runtime level?** Runtime tracks cursor/plan binding and evidence; completed steps never re-executed. For a step that already produced evidence (persisted), retry behavior depends on state. If evidence not persisted (crashed before commit), runtime has no record — must infer.
- **Provider idempotency key?** **Not evident as a generic mechanism** in core codebase (provider clients take requests as-is). No central idempotency key generation/passing visible in ToolGateway/intent. Implementation is provider-specific if present.
- **Reconciliation?** None implemented. No read-back reconciliation beyond verification step at execution time.
- **INDETERMINATE reachable?** Yes (fulfillment returns INDETERMINATE in cases where evidence set cannot settle).
- **WAITING_EXTERNAL reachable?** States exist; transitions allow moving to WAITING_EXTERNAL from EXECUTING (tasks/state.py shows EXECUTING can go to WAITING_EXTERNAL). But transition into WAITING_EXTERNAL depends on runtime logic; state exists. 
- **Retry distinguishes failed vs unknown?** Yes: FAILED vs REQUIRES_ATTENTION are distinct states (both retryable under conditions); INDETERMINATE maps to REQUIRES_ATTENTION (not FAILED). Retry budget applies to failure→retry paths.

**Code path (actual):** Crash after provider success but before persisting RuntimeExecution.evidence → on resume, runtime has no qualifying evidence → fulfillment likely INDETERMINATE/NOT_FULFILLED depending on evidence set → moves to REQUIRES_ATTENTION (fail-safe) rather than auto-completing. This prevents false COMPLETED but does **not** prevent duplicate side effect on manual RETRY if provider call is re-issued without idempotency key.

**Conclusion:** Recovery is **safe against false completion** (fail-safe). It is **not fully safe against duplicate side effects** in the ambiguous-crash-before-evidence case without provider-level idempotency keys. This is a known gap in many systems; design choices (WAITING_EXTERNAL, INDETERMINATE→REQUIRES_ATTENTION, claim) reduce risk but do not eliminate it.

## 9. Evidence/Fulfillment Boundaries

Verify separation: Provider Success ≠ Execution Success ≠ Evidence ≠ Fulfillment ≠ Task Completion.

**From code (verified):**
- **Provider Success:** What provider returned (adapter result). Distinct from verification.
- **Execution Success:** ToolResult.ok/outcome as determined after execution; gateway produces ToolResult. Separated from verification (gateway verifies after execution).
- **Evidence:** Stored in RuntimeExecution.evidence (append-only). Built via ir.adapter.evidence_from_observation() from ObservationNode. Contains tool, outcome, ok, verified, args, data, evidence (provider evidence), request_id, ir_action_id. Qualification (e.g. qualifying_creation) filters evidence set — separate from raw evidence.
- **Fulfillment:** Typed criteria (CalendarMeetingCriteria) evaluated by pure evaluators over qualifying evidence set → FulfillmentVerdict (FULFILLED/NOT_FULFILLED/INDETERMINATE). runtime/fulfillment.py is pure; does not transition state.
- **Task Completion:** State transition to COMPLETED only via Task state machine after fulfillment decision (runtime/service.py decides transition based on verdict). Task state is owned by machine.

**Mixing check:** No obvious mixing found. Each boundary is explicit:
- ir/adapter.py separates observation construction from gateway result
- runtime/evidence.py defines qualification separately from fulfillment
- runtime/fulfillment.py is pure evaluator (no state changes)
- runtime/service.py applies verdict to state transitions

**Conclusion:** Boundaries are well-separated in Core. The direct API paths do not create these artifacts.

## 10. KAI-IR Boundary

Verify IR constraints:
- Does not execute provider? **Yes.** IR defines nodes (ActionNode, etc.) and control flow (control.py is pure evaluation/traversal). Adapter produces ToolIntent for gateway; no provider I/O in ir/*.
- Does not decide policy? **Yes.** PolicyConstraintsNode is declaration only; PolicyEngine (policy/service.py) decides. Gateway evaluates policy.
- Does not decide approval? **Yes.** PermissionNode is requirement declaration; ApprovalManager decides at gateway boundary.
- Does not own credentials? **Yes.** No credentials in IR nodes/schemas.
- Does not decide fulfillment? **Yes.** IR has ResultNode and observations; fulfillment is separate (runtime/fulfillment.py) using evidence.
- Does not decide task completion? **Yes.** ResultNode records outcome conceptually; actual state transitions done by Task state machine in runtime/service.py based on fulfillment/verdict.

**Current IR scope:** Representation/contract (intent, plan, control flow, constraints, permissions, observations, errors, result, fingerprint). Pure data model (extra='forbid').

**Question: Does current IR need new primitives?**

From audit: Current slice is linear+branching (calendar_meeting). IR already supports conditions/control edges (ir/control.py, ir/nodes.py). MemoryOpNode and ApprovalNode are explicitly deferred (comment in ir/nodes.py). Adding primitives is not necessary for current vertical slice.

**Conclusion:** IR adds representation only, not a second runtime. It is a derived, deterministic description consumed by runtime; execution authority remains with ToolGateway + Runtime. No new primitives required by current design.

## 11. Semantic Contract

**Must not change without explicit architectural decision:**

`	ext
Intent Authority = Task Contract (sealed at creation). goal/success_criteria/fulfillment_profile/allowed_tools/risk_policy/etc stored on Task; immutable post-creation. (tasks/models.py, tasks/service.py)
Execution Authority = ToolGateway (non-bypassable on governed path). Single gate for side effects with full pipeline. (tools/gateway.py)
Provider Authority = External provider systems; verification must prove provider state matches request.
Evidence Authority = RuntimeExecution.evidence (append-only). Qualification centralized in runtime/evidence.py (reads from evidence['event_id']). (runtime/models.py, runtime/evidence.py)
Fulfillment Authority = Typed evaluators over qualifying evidence set (pure). FulfillmentVerdict is three-valued; does not transition state. (runtime/fulfillment.py)
Task Lifecycle Authority = Task state machine (tasks/state.py). Only machine may transition states; external client transitions restricted to CLIENT_TRANSITION_TARGETS (FAILED, REQUIRES_ATTENTION, RETRY).
Memory Authority = Context only (never authority). Identity resolution treats memory hints as hints. (memory/service.py, identity resolution)
IR Authority = Derived representation (plan+contract+progress). Deterministic program_id/fingerprint via canonical JSON; excludes execution record. No I/O, no policy/approval/credentials. (ir/program.py, ir/build.py)
Runtime Envelope Authority = (Task + RuntimeExecution) with claim lease/heartbeat, cursor bound to plan_version+plan_hash. Resume from durable state only. (runtime/service.py, runtime/models.py)
Identity Authority = Resolved identities with status/confidence; sealed into RuntimeExecution. (identity/service.py)
`

**Allowed:** Extend within vertical slice if contracts preserved. Share provider service implementations across paths only if enforcement contracts are identical.

**Forbidden:** Making IR decide policy/approval/fulfillment/completion; bypassing ToolGateway on governed side effects without replacing its enforcement; conflating Provider Success with Task Completion; treating Memory as control-flow authority; mutating sealed Task Contract post-creation; auto-returning from WAITING_EXTERNAL without explicit RETRY decision.

## 12. Architectural Invariants

Top invariants (evidence-based):

| ID | Invariant | Evidence | Enforcement | Test | Current Status |
|---|---|---|---|---|---|
| INV-001 | Side effects on governed path only via ToolGateway | runtime/service.py calls gateway.execute() with ToolIntent from ir/adapter | ToolGateway pipeline | gateway/tools tests | PROVEN |
| INV-002 | Sealed Task Contract immutable post-creation | tasks/service.py create_task sets fields; no update path modifies contract | Model/service design | contract tests | PROVEN |
| INV-003 | Task state transitions only via machine | tasks/service.py transition_task uses resolve_transition; raises InvalidTransition if illegal | tasks/state.py | state tests | PROVEN |
| INV-004 | Cursor advances only after successful step; completed steps never re-executed | runtime/service.py execution logic | Runtime logic + durable cursor | runtime tests | PROVEN |
| INV-005 | Cursor bound to plan_version+plan_hash | RuntimeExecution has plan_version, plan_hash; binding logic in runtime | RuntimeEnvelope | runtime tests | PROVEN |
| INV-006 | Replay deterministic from durable facts | ir/control.py reads facts from observations/evidence; IR derived from same inputs | Pure functions + durable state | branching/runtime tests | PROVEN |
| INV-007 | Evidence append-only; qualification centralized | runtime/evidence.py, RuntimeExecution.evidence | Adapter + model | evidence semantics tests | PROVEN |
| INV-008 | Provider identity from evidence['event_id'] (not data) | runtime/evidence.py qualification | Centralized filter | evidence tests | PROVEN |
| INV-009 | Fulfillment is pure (no state transitions) | runtime/fulfillment.py takes evidence, returns verdict | Design | fulfillment tests | PROVEN |
| INV-010 | Completion only after fulfillment decision applied via state machine | runtime/service.py maps verdict to state | Runtime logic | runtime/fulfillment tests | PROVEN |
| INV-011 | Single-use approval consumed atomically | gateway step 12 + approval manager consume | Atomic check | approval tests | PROVEN |
| INV-012 | Final policy re-check before consume/execute (TOCTOU) | tools/gateway.py step 11 | Gateway pipeline | gateway tests | PROVEN |
| INV-013 | Claim lost → abort without writing | ClaimLostError raised; caller abandons | runtime/service.py | claim semantics tests | PROVEN |
| INV-014 | Tenant isolation enforced at scope | queries scoped by tenant_id (+user_id) | service layer | auth/multi-tenant tests | PROVEN |
| INV-015 | IR fingerprint binds identity to content (excludes execution record) | ir/program.py canonical_form excludes observations/errors/result/snapshot/fingerprint | seal/validate | ir tests | PROVEN |
| INV-016 | Verification required for success reporting | gateway verifies after execute; result.finalize uses verification | gateway pipeline | verification tests | PROVEN |
| INV-017 | Memory is context-only (never authority) | identity resolution treats memory hints as hints | design | identity tests | PROVEN |
| INV-018 | IR has no I/O/credentials/policy decisions | ir/* pure data/model | design | ir tests | PROVEN |
| INV-019 | WAITING_EXTERNAL requires explicit RETRY (no auto-return) | tasks/state.py transitions | state machine | state tests | PROVEN |
| INV-020 | Three-valued fulfillment verdict (INDETERMINATE distinct) | FulfillmentVerdict enum; maps to REQUIRES_ATTENTION | design | fulfillment tests | PROVEN |

Notes: INV-001 applies to governed path. Direct API paths do not currently satisfy INV-001/INV-008/INV-010/INV-011/INV-012 in the same way (enforcement differs). That is the boundary point.

## 13. P0/P1 Verification (against original findings)

**FINDING-001 (P1): Core vs Extension boundary ambiguity** — **CONFIRMED as architectural ambiguity**, not bug. Evidence verified (direct routes bypass ToolGateway; runtime path enforces). Classification: missing contract + architectural ambiguity. Needs decision (Options A-D). Not a production blocker if boundary is explicitly defined; becomes blocker if semantics must be unified.

**FINDING-002 (P1): Migration test revision mismatch** — **CONFIRMED test/setup issue**. Expected 'a1b2c3d4e5f7' vs head b3d5f7a9c2? (head is b3d5f7a9c1e2). No production impact. Decision: fix test or adjust. Not production blocker.

**P0:** None identified. Core governed path is coherent.

**Separation:** Production blockers: none from current analysis. Architectural improvements: boundary decision (FINDING-001). Technical debt: none critical. Future features: adding IR primitives not needed now.

## 14. Decision Table

| Decision | Current State | Options | Recommended Direction | Why | Must Decide Before |
|---|---|---|---|---|---|
| DEC-001: Core vs Extension boundary definition | Dual execution paths exist (ToolGateway enforced only on runtime path) | A: All external side effects via Runtime/ToolGateway; B: Direct APIs honor same contracts (extract shared enforcement); C: Explicit Core/Extension with different trust models; D: Hybrid (governed writes via Core, ops via Extension) | **C or D** (explicit boundary) with clear contract. Prefer **D**: classify governed writes (need Task/lifecycle/evidence/approval) must go through Core; operational direct endpoints treated as Extension with documented constraints. | Preserves safety for governed actions while maintaining operational flexibility. Matches current split without forcing immediate breaking API changes. | Any change to write-path enforcement, any expansion of governed capabilities, or production hardening decisions. |
| DEC-002: ToolGateway non-bypassability | Non-bypassable on governed path only; bypassable at HTTP surface via direct APIs | Make globally non-bypassable; or make conditionally non-bypassable by path classification; or accept explicit bypasses with contracts | **Conditional by classification** (per DEC-001). Governed side effects: non-bypassable via ToolGateway. Extension ops: may use direct path only if they do not participate in Task lifecycle and boundary documented. | Avoids over-constraining operational endpoints; enforces governance where it matters. | Enforcing single execution authority; refactoring direct APIs. |
| DEC-003: Evidence/Task participation for direct APIs | Direct APIs produce no RuntimeExecution/evidence in governed form | Require direct APIs to write governed evidence; or allow them to be evidence-free with different semantics; or forbid them for write actions that affect fulfillment | **Classify by capability** (D). If action can affect Task fulfillment/completion, must go through Core (producing governed evidence). If purely operational (no Task correlation), Extension allowed without governed envelope. | Prevents semantic drift between paths while being practical. | Any write operation that could be part of governed flow. |
| DEC-004: Provider idempotency | No generic provider idempotency key mechanism visible in ToolGateway | Add generic idempotency key generation (action_id/request_id) to ToolGateway and pass to providers; or provider-specific; or rely on runtime cursor+evidence only | **Decide now or defer with documented risk.** Minimum: ensure action_id/request_id is available to providers when useful; evaluate provider capabilities. Do **not** implement broad change yet — document risk (duplicate side effects on crash-before-evidence) and decide if acceptable for vertical slice. | Ambiguous outcome analysis shows risk exists but design is fail-safe against false completion. | Before expanding to providers with weaker idempotency or higher risk. |
| DEC-005: Migration test fix | tests/test_memory.py references revision 'a1b2c3d4e5f7' not found (head b3f43b?) actual head b3d5f7a9c1e2 | Fix test to use correct revision; or skip/update test; or remove if stale | **Fix test** to match current migration graph. Test-only change, no production impact. | Keeps test suite consistent. | CI/test hygiene (can be done anytime, separate from architecture). |
| DEC-006: Gmail_send live sealing | Evaluator exists; LIVE_SEALABLE_PROFILES may restrict | Add gmail_send to live sealable profiles; or keep gated pending review | **Keep gated** until semantic review complete (matches safety-first). | Avoids broadening surface before contract review. | If enabling gmail_send in live runtime. |

## 15. Decision Dependencies

Dependency order (A must be decided before B if B depends on A):

`	ext
DEC-001 (Core vs Extension boundary definition)
    ↓
DEC-002 (ToolGateway non-bypassability by classification)
    ↓
DEC-003 (Evidence/Task participation rules per path/capability)
    ↓
DEC-004 (Provider idempotency policy based on boundary rules)

DEC-005 (migration test) is independent (test hygiene).
DEC-006 (gmail_send sealing) is independent feature gate.
`

**Explanation:**
- DEC-001 sets the trust model and what 'governed' means. This is foundational.
- DEC-002 operationalizes non-bypassability based on classification from DEC-001.
- DEC-003 determines when evidence/Task envelope is required — depends on boundary rules.
- DEC-004's necessity/scope depends on whether paths that can cause side effects without durable evidence are allowed (outcome of DEC-001/DEC-003).

**Dependency rule:** Do not implement enforcement changes (especially around bypasses) until DEC-001-DEC-003 are closed. DEC-004 can be decided with documented risk but full implementation scope depends on boundary.

## 16. Do Not Implement Yet

Additions should be deferred until architectural decisions are closed:

- **New IR primitives** (beyond current linear+branching). Current IR is sufficient for slice; adding primitives without clarifying IR Authority (section 11) risks turning IR into second runtime. Defer.
- **Generalized workflow engine features** (loops, parallel branches beyond DAG assumptions, dynamic capability registration). Would change control semantics and potentially IR's representation-only role.
- **Expanding governed surface beyond calendar_meeting** (e.g. gmail_send to live sealing) — defer pending DEC-006/semantic review.
- **Enforcing global ToolGateway bypass removal** — defer until DEC-001-DEC-002 closed (could break direct API usage).
- **Provider idempotency infrastructure changes** — defer until DEC-004 scope determined (can document risk now).
- **Major API surface reshaping** — defer until boundary decision (DEC-001).
- **Computer Use/Desktop agents/other verticals** — out of scope for current slice; would require contract review.

Rationale: These risk architectural drift without first resolving Core vs Extension boundary and execution authority.

## 17. Final Engineering Position

**Direct answers to the required questions:**

1. **Is the current execution architecture coherent?** Partially. The Core governed path (IR + ToolGateway + Runtime + Task state machine) is coherent and internally consistent. The presence of parallel direct API paths creates incoherence at the application boundary until the Core/Extension boundary is explicitly defined.

2. **Does KAI have a single execution authority or more?** **More than one in practice.** Path A (governed): ToolGateway + Runtime as execution/lifecycle authority. Path B (direct): provider services called directly with no gateway — effectively a second execution authority. The design intends single authority for governed flows.

3. **Is ToolGateway the boundary?** It is the boundary **on the governed runtime path** (non-bypassable there). It is **not** the global application boundary because direct API routes do not invoke it. Proof: runtime/service.py calls gateway.execute(); api/calendar.py/gmail.py/contacts.py do not.

4. **Is Runtime the lifecycle authority only?** Runtime (AgentRuntime) + Task state machine together form the lifecycle authority for governed tasks. Task state machine is the single writer of Task.state; Runtime orchestrates transitions based on durable evidence and fulfillment. For non-task execution (direct APIs), there is no Task lifecycle — so authority scope is path-dependent.

5. **Are Evidence semantics coherent?** Yes, within Core. Centralized in runtime/evidence.py (qualification reads from evidence['event_id'], not data). The direct API paths do not produce governed evidence in the same envelope, so coherence is limited to Core.

6. **Is Fulfillment semantics complete or vertical-specific?** Vertical-specific (calendar_meeting) but well-separated (typed criteria separate from prose; pure evaluators; three-valued verdict). The separation is general; the concrete criteria/domain are slice-specific. Semantics are sound for the vertical slice.

7. **Is recovery safe in ambiguous outcomes?** **Fail-safe but not duplicate-safe.** Prevents false COMPLETED (INDETERMINATE→REQUIRES_ATTENTION, WAITING_EXTERNAL with no auto-return, claim prevents concurrent overwrite). Does not guarantee preventing duplicate side effects on crash-before-evidence without provider-level idempotency keys. Documented risk.

8. **Does API layer respect Core boundary?** No, not consistently. Direct provider endpoints bypass ToolGateway/Core governance. The runtime API (/runtime/*) does respect Core boundary.

9. **Does KAI-IR add representation only or is it becoming a second runtime?** Representation only. IR is pure data (no I/O, no credentials, no policy/approval/fulfillment decisions). Control flow is deterministic evaluation over durable facts. No execution/executor inside IR — execution remains with ToolGateway+Runtime.

10. **What is the minimal set of decisions to close before touching production code again?** **DEC-001, DEC-002, DEC-003** (in that order). These close Core vs Extension boundary, non-bypassability, and when governed evidence/Task participation is required. DEC-004 should be decided or explicitly risk-accepted. DEC-005/DEC-006 are non-blocking for architecture.

**Summary (15-point):**
1. **Correct:** Core governed path (IR+ToolGateway+Runtime+Task machine) is coherent and enforces invariants.
2. **Correct:** Evidence qualification centralized, reads from evidence['event_id'] (not data); boundaries well-separated in Core.
3. **Correct:** Fulfillment is pure, three-valued (INDETERMINATE distinct); recovery is fail-safe.
4. **Correct:** IR is representation-only (no I/O/credentials/policy/approval/fulfillment decisions).
5. **Correct:** WAITING_EXTERNAL has no auto-return (explicit RETRY); claim semantics prevent overwrite on concurrency/crash.
6. **Correct:** Sealed contract immutable; tenant isolation; TOCTOU re-check; single-use approvals.
7. **Incomplete:** System has two execution authorities in practice (governed runtime vs direct provider APIs).
8. **Incomplete:** ToolGateway is global boundary only on governed path, not at HTTP surface.
9. **Incomplete:** Direct APIs bypass governance and do not produce governed evidence/Task envelope.
10. **Needs decision:** Core vs Extension boundary (DEC-001) — foundational.
11. **Needs decision:** When ToolGateway is non-bypassable (DEC-002), conditional on classification.
12. **Needs decision:** When governed evidence/Task participation required (DEC-003) to prevent semantic drift.
13. **Risk known:** Ambiguous-crash-before-evidence can allow duplicate side effects without provider idempotency (DEC-004).
14. **Do not touch:** IR primitives expansion, global bypass removal, broad API reshaping until boundary decisions closed.
15. **Non-blocking:** Migration test fix (DEC-005), gmail_send sealing (DEC-006) are separate from architectural closure.
