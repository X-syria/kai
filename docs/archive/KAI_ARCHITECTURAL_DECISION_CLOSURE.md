# KAI - Architectural Decision Closure (DEC-001, DEC-002, DEC-003)

**Repository:** C:\Users\khaled\Documents\kai\.claude\worktrees\busy-gould-c3f43b  
**Date:** 2026-10-03  
**Auditor:** Principal Engineer + Software Architect  
**Type:** Decision-Closure (No Implementation)

## Executive Decision Summary

The re-verification confirms: Core (IR + ToolGateway + Runtime + Task state machine) is coherent and enforces its invariants; direct provider API endpoints (/api/v1/calendar, /gmail, /contacts) form a parallel execution surface with different enforcement. The system has **two execution authorities in practice**. Recovery is fail-safe against false completion but not guaranteed duplicate-safe independent of provider idempotency (risk documented).

**DEC-001**: ACCEPTED WITH CONDITIONS — Define explicit Core vs Extension boundary using Hybrid Model D (governed writes affecting Task fulfillment/completion must go through Core; operational reads/writes with no Task lifecycle correlation may go through Extension) with documented constraints.

**DEC-002**: ACCEPTED WITH CONDITIONS — ToolGateway non-bypassability is conditional by classification (per DEC-001). Governed side effects are non-bypassable via ToolGateway; Extension operations are allowed via direct path only if they do not participate in Task lifecycle and boundary is explicit.

**DEC-003**: ACCEPTED WITH CONDITIONS — Evidence/Task participation is capability-classified. Actions that can affect Task fulfillment/completion must produce governed evidence in RuntimeExecution envelope via Core. Purely operational actions with no Task correlation may be evidence-free in governed sense (Extension) with clear semantics.

**Safe to modify production code?** NO — DEC-001-DEC-003 closed as decisions with conditions; implementation must follow the classification/contracting per the decision records. The first implementation slice is: define capability classification (governed vs operational) for calendar/gmail/contacts endpoints and document the boundary contract (no code enforcement changes yet — documentation/contract definition first).
## 1. Decision Premises

From existing reports:
- **DEC-001** (from review): Core vs Extension boundary definition — dual execution paths exist (ToolGateway enforced only on runtime path). Prefer Hybrid D: classify governed writes (need Task/lifecycle/evidence/approval) must go through Core; operational direct endpoints treated as Extension with documented constraints.
- **DEC-002**: ToolGateway non-bypassability — conditional by classification (per DEC-001). Governed side effects non-bypassable via ToolGateway; Extension ops allowed via direct path only if they do not participate in Task lifecycle and boundary documented.
- **DEC-003**: Evidence/Task participation for direct APIs — classify by capability. If action can affect Task fulfillment/completion, must produce governed evidence via Core. If purely operational with no Task correlation, Extension allowed without governed envelope.

Verification (FACT level): runtime/service.py constructs ToolIntent and calls gateway.execute(); api/calendar.py, api/gmail.py, api/contacts.py call provider services directly (no ToolGateway). RuntimeExecution + Task form durable envelope for Core. Evidence qualification centralized in runtime/evidence.py reads from evidence['event_id']. Recovery is fail-safe (INDETERMINATE→REQUIRES_ATTENTION, no auto-return from WAITING_EXTERNAL, claim prevents overwrite) but duplicate prevention depends on provider idempotency in ambiguous-crash-before-evidence cases.
## 2. DEC-001 — Core vs Extension Boundary Definition

### Problem
The system has two execution paths with different enforcement: governed runtime (IR+ToolGateway+Runtime+Task) vs direct provider API endpoints. This creates ambiguity about execution authority and whether ToolGateway is global.

### Current Reality (FACT)
- Path A (governed): runtime/service.py builds ToolIntent via ir/adapter and calls gateway.execute() with full pipeline (policy/content/approval/account/TOCTOU recheck/single-use consume/verify). Creates/uses Task + RuntimeExecution.
- Path B (direct): api/calendar.py, api/gmail.py, api/contacts.py call vertical provider services directly. No ToolIntent, no ToolGateway, no Task/RuntimeExecution in typical handlers.

### Evidence
- runtime/service.py: AgentRuntime._execute uses gateway.execute()
- tools/gateway.py: full pipeline implementation
- ir/adapter.py: intent_from_action produces ToolIntent
- api/calendar.py, api/gmail.py, api/contacts.py: direct service calls (by inspection of mounted routes)

### Architectural Question
Should there be one execution authority (all side effects through Core) or an explicit Core/Extension split with clear contracts?

### Options
A. All external side effects via Runtime/ToolGateway (single authority)
B. Direct APIs honor same contracts (shared enforcement)
C. Explicit Core/Extension with different trust models
D. Hybrid: governed writes affecting Task lifecycle/fulfillment via Core; operational ops with no Task correlation via Extension (with constraints)

### Option Analysis (key dimensions)
- Correctness/Security: A strongest; D preserves safety for governed actions
- Recovery/Idempotency: A unified; D allows different models per surface but needs clear boundaries
- Complexity/Backwards compat: A higher breaking risk; D pragmatic
- Auditability/Tenancy/Observability: A unified; D acceptable if tagged and contracts explicit

### Recommendation
**ACCEPTED WITH CONDITIONS — Option D (Hybrid)**. Classify by capability: governed writes that can affect Task fulfillment/completion must go through Core (producing governed evidence in RuntimeExecution). Operational reads/writes with no Task lifecycle correlation may go through Extension, only if they do not participate in governed execution and boundary is explicitly documented with constraints.

### Why (engineering)
Preserves safety where it matters (governed execution) without forcing immediate breaking changes to direct API surface; makes boundary explicit and enforceable conceptually.

### Consequences
+ Explicit boundary, reduces ambiguity
+ Protects governed invariants
- Requires capability classification
- Must prevent cross-contamination

### Explicit Non-Goals
Not forcing all operations through Runtime; not removing direct APIs without decision; not adding primitives.

### Required Invariants
INV-001 (governed path via ToolGateway), INV-002-INV-020 for Core; Extension must not claim governed semantics without producing governed artifacts.

### Implementation Scope (later, not now)
Define capability classification matrix for calendar/gmail/contacts endpoints; document boundary in architecture docs; identify cross-contamination points.

### Required Tests (post-impl)
Tests asserting governed capabilities only available via Core path; boundary tests preventing governed semantics from Extension.

### Migration/Compatibility
No migration required; API surface unchanged by decision; possible future deprecation if needed, but not implied.

### Dependencies
None (foundational). Subsequent: DEC-002 depends on this; DEC-003 depends on this.

### Decision Status
**ACCEPTED WITH CONDITIONS**
## 3. DEC-002 — ToolGateway Non-Bypassability

### Problem
ToolGateway is non-bypassable on governed path but bypassed by direct API endpoints. Need clear, enforceable rule.

### Current Reality (FACT)
Direct routes (api/calendar/gmail/contacts) do not call gateway.execute(); Core does. This is code-level bypass of gateway pipeline for those entry points.

### Evidence
tools/gateway.py pipeline (steps 1-14), runtime/service.py calls gateway.execute(), direct API files call services directly.

### Architectural Question
Should ToolGateway be globally non-bypassable, or conditionally by classification?

### Options
- Globally non-bypassable (all external side effects)
- Conditionally non-bypassable by path classification (per DEC-001)
- Accept explicit bypasses with contracts

### Option Analysis
Global: strongest correctness/security but breaks direct API UX, high complexity to reconcile. Conditional: matches hybrid model, enforceable per classification. Accepting bypasses without contracts is ambiguous.

### Recommendation
**ACCEPTED WITH CONDITIONS — Conditional by classification (per DEC-001)**. Governed side effects are non-bypassable via ToolGateway. Extension operations may use direct path only if they do not participate in Task lifecycle, do not affect Task fulfillment/completion, and the boundary is explicitly documented. Any operation classified as 'governed' must go through Core (no bypass).

### Why
Avoids over-constraining operational endpoints while enforcing governance where invariants matter.

### Consequences
+ Governance preserved for governed actions
- Requires classification discipline
- Boundary must remain explicit

### Explicit Non-Goals
Blindly routing everything through Gateway; removing Extension endpoints.

### Required Invariants
INV-001 enforced for governed path; Extension path must not claim governed invariants (INV-008/INV-010/INV-011/INV-012).

### Implementation Scope
Classification matrix; boundary documentation. No enforcement code change implied now.

### Required Tests
Tests verifying governed capabilities cannot bypass ToolGateway (post-impl).

### Migration/Compatibility
No breaking change implied by decision.

### Dependencies
Depends on DEC-001. Subsequent affects DEC-003.

### Decision Status
**ACCEPTED WITH CONDITIONS**

### Bypass Analysis (per-type)
- Architectural bypass (violates Core boundary): any write classified as governed using direct path — not allowed under this decision.
- Legitimate capability (Extension): operational ops with no Task correlation — allowed if classified correctly.
- Test-only: no production impact.
- Administrative: treat per classification; may differ but must respect same constraints if they affect governed state.
## 4. DEC-003 — Evidence/Task Participation for Direct APIs

### Problem
Direct APIs produce no RuntimeExecution/evidence in governed form, creating semantic drift between paths. Need rule for when governed envelope is required.

### Current Reality (FACT)
Direct paths do not create Task/RuntimeExecution or write governed evidence via ir.adapter.evidence_from_observation. Evidence semantics centralized in runtime/evidence.py apply to Core.

### Evidence
runtime/evidence.py (qualification reads from evidence['event_id']), runtime/models.py (RuntimeExecution), direct API handlers call services directly.

### Architectural Question
When must an action produce governed evidence and participate in Task lifecycle?

### Options
- Require direct APIs to write governed evidence
- Allow evidence-free with different semantics
- Forbid for write actions that affect fulfillment

### Option Analysis
Requiring all to write governed evidence forces them into Core (conflicts with extension). Evidence-free with different semantics risks drift. Classify by capability is cleanest.

### Recommendation
**ACCEPTED WITH CONDITIONS — Classify by capability (per DEC-001).** If action can affect Task fulfillment/completion, it must go through Core and produce governed evidence in RuntimeExecution envelope. Purely operational actions with no Task correlation may be evidence-free in governed sense (Extension) with clear semantics and must not claim governed artifacts.

### Why
Prevents semantic drift where it matters; preserves operational flexibility.

### Consequences
+ Clear envelope boundary
- Requires classification discipline

### Explicit Non-Goals
Forcing all paths into single envelope.

### Required Invariants
INV-007, INV-008, INV-010 enforced for Core; Extension must not conflate with governed fulfillment.

### Implementation Scope
Classification rules + documentation of envelope boundary.

### Required Tests
Post-impl tests verifying governed actions produce evidence; no governed semantics leakage from Extension.

### Migration/Compatibility
No change implied.

### Dependencies
DEC-001, DEC-002.

### Decision Status
**ACCEPTED WITH CONDITIONS**
## 5. Execution Authority Model

### Authority Comparison

| Concern | Authority A (Path A - Governed/Core) | Authority B (Path B - Extension/Direct) |
|---|---|---|
| **Authentication** | FastAPI deps (auth) + context binding | FastAPI deps (auth) + context binding |
| **Authorization** | Tenant scoping + ToolGateway policy/content/approval gates + identity assertion | Route-level auth/tenant scoping (per handlers); no gateway policy/approval pipeline by default |
| **Policy** | PolicyEngine via ToolGateway (evaluated, final TOCTOU recheck) | Not enforced via gateway; enforcement depends on service/handler (if any) |
| **Approval** | ApprovalManager via ToolGateway (single-use, intent-bound) | Not via gateway boundary |
| **Identity** | Context↔intent assertion, resolved identities sealed in RuntimeExecution | Handler/service-level; no governed identity sealing into RuntimeExecution envelope |
| **Execution state** | Task state machine (single writer); RuntimeExecution (cursor bound to plan_version/plan_hash, claim lease/heartbeat) | No Task/RuntimeExecution lifecycle |
| **Evidence** | Append-only in RuntimeExecution.evidence; qualification centralized (reads from evidence['event_id']); via ir.adapter | No governed evidence envelope by default |
| **Verification** | VerificationEngine via gateway (contract verification) | Provider/service-level verification varies; not via governed gateway contract path |
| **Recovery** | Durable state-based (resume from facts); claim prevents overwrite; fail-safe (INDETERMINATE→REQUIRES_ATTENTION); no auto-return from WAITING_EXTERNAL | Provider-level recovery/idempotency if implemented; no runtime envelope recovery |
| **Idempotency** | Runtime-level (cursor prevents re-execution of completed steps) + claim semantics; provider idempotency still relevant on ambiguous outcomes | Provider-dependent |
| **Audit** | Unified via RuntimeExecution evidence + task state + spans/metrics | Route/service-level logging; not unified into governed audit envelope |
| **Task lifecycle** | Full (CREATED→...→COMPLETED/FAILED/REQUIRES_ATTENTION/ESCALATE) | None |

### Analysis
Both have auth; enforcement diverges after auth. Core has single-writer lifecycle + governed envelope; Extension does not.

### Conclusion on Intent
1. **Intended?** Core design clearly intended single authority for governed execution. Direct endpoints exist as operational surface — ambiguous by design documentation.
2. **Temporary?** Not necessarily. Can be permanent if boundary explicit.
3. **Architectural defect?** No defect in Core; boundary ambiguity is the issue (addressed by DEC-001).
4. **Capability split?** Yes — this is a capability split (governed vs operational). Best modeled as Core vs Extension with explicit contracts (Model D).
## 6. ToolGateway Authority Model

### Bypass Inventory (by code inspection)
- **Entrypoint**: POST /api/v1/calendar/* (api/calendar.py routes) — **caller**: HTTP clients; **provider**: calendar provider; **user-reachable**: yes (authenticated); **production-reachable**: yes if mounted; **executes side effect**: for write ops yes; **bypasses**: ToolGateway pipeline, Task/RuntimeExecution envelope, governed evidence; **intentional?** Unclear from code — operational convenience.
- **Entrypoint**: /api/v1/gmail/* (api/gmail.py) — same as above; bypasses governed pipeline.
- **Entrypoint**: /api/v1/contacts/* (api/contacts.py) — mostly read-oriented; bypasses gateway.
- **Test-only paths**: tools/stubs.py — **not production reachable** as external entry; legitimate for testing.

### Classification
- **Architectural bypass** (violates Core boundary if operation is governed): any write action classified as governed (affects Task fulfillment/completion) using direct path → violates DEC-002 under classification.
- **Legitimate capability (Extension)**: operations with no Task correlation → acceptable if classified correctly.
- **Test-only**: ignore for production architecture.
- **Administrative**: case-by-case per classification.

### Rule (from DEC-002)
ToolGateway is non-bypassable for **governed side effects**. Extension operations allowed only if they do not participate in Task lifecycle and boundary documented. Rule must be **unambiguous and enforceable** conceptually; enforcement mechanism is post-impl — decision is the rule itself.

### Enforcement Model (conceptual)
Classification matrix (governed vs operational) per endpoint/capability. Governed capabilities are only exposed via Core paths; Extension paths do not expose governed capabilities. This is enforceable by design/routing, not by adding broad abstractions now.
## 7. External Outcome Model

### Ambiguous Outcome Cases
`	ext
KAI
 ↓
Provider request
 ↓
Provider executes effect
 ↓
network failure / timeout / lost response
 ↓
KAI does not know if effect happened
 ↓
process recovery
`

### Classification
- **Known Failure**: provider guarantees effect did not happen (e.g. request rejected before execution, idempotent failure)
- **Known Success**: provider guarantees effect happened (response received with provider id)
- **Unknown Outcome**: cannot prove either (lost response/timeout/crash-before-evidence)

### Current Behavior (verified)
- Core records evidence after ToolResult; cursor advances only after success. 
- Claim lost → abort without writing (prevents overwrite). 
- WAITING_EXTERNAL has no auto-return to EXECUTING (explicit RETRY via REQUIRES_ATTENTION→RETRY→EXECUTING).
- Fulfillment three-valued: FULFILLED/NOT_FULFILLED/INDETERMINATE. INDETERMINATE maps to REQUIRES_ATTENTION (not FAILED) — **fail-safe**.

### What KAI does at UNKNOWN OUTCOME
- Does **not** auto-complete to COMPLETED (uses INDETERMINATE→REQUIRES_ATTENTION path)
- Does **not** automatically re-execute from WAITING_EXTERNAL
- Requires explicit decision path (RETRY from REQUIRES_ATTENTION) if retry permitted
- Aborts without overwriting on claim loss

### What KAI does NOT guarantee
- **Duplicate prevention independent of provider**: on crash-before-evidence, retry may re-issue request if provider has no idempotency and no reconciliation. Runtime cursor prevents re-executing completed steps (persisted); if never persisted, no such guard.

**Conclusion:** fail-safe against false completion. **Not duplicate-safe independent of provider idempotency** in ambiguous-crash-before-evidence case. This is documented as risk.
## 8. INDETERMINATE Semantics

INDETERMINATE is a distinct fulfillment verdict (not collapsed to failure). Full semantics:

- **Who enters it?** Fulfillment evaluator when qualifying evidence set exists but cannot settle criteria conclusively, or when evidence insufficient to determine fulfillment (per runtime/fulfillment.py). Also when recovery cannot prove state.
- **Who exits it?** Task remains in REQUIRES_ATTENTION (state machine). Exit via explicit external action (client transitions allowed: FAILED/REQUIRES_ATTENTION/RETRY per CLIENT_TRANSITION_TARGETS in tasks/service.py). No automatic exit.
- **Allows retry?** Via REQUIRES_ATTENTION → RETRY path (if budget allows). Retry is explicit decision, not automatic.
- **Allows user retry?** Yes, if task in non-terminal state and within retry budget semantics (client may request RETRY from REQUIRES_ATTENTION).
- **Stops task?** Yes — moves to REQUIRES_ATTENTION (not terminal like COMPLETED/ESCALATE). Task is blocked pending human/explicit decision.
- **Can fulfillment be decided?** Only after new evidence or reconciliation/explicit decision; evaluator re-runs on evidence set.
- **May execute same effect again?** On explicit RETRY, execution may re-attempt. Duplicate possible if provider lacks idempotency and prior effect occurred but evidence never persisted.
- **Reconciliation?** None implemented generically. Relies on provider query/reconciliation if available, or human decision.
- **User-visible**: Task state REQUIRES_ATTENTION; last_error/next_action reflect blocked state.
- **Audit trail**: Stored in fulfillment_verdict/evidence; state transitions recorded.

**Rule:** INDETERMINATE means "cannot prove fulfillment" — fail-safe (never auto-completes). It does **not** mean "safe to retry without consequence" — consequence depends on provider idempotency.
## 9. Provider Idempotency Model

### Classification by provider capability
- **Provider with native idempotency**: can safely retry with same key; KAI may leverage if key available. Keying strategy not generically implemented in ToolGateway.
- **Provider without native idempotency**: retry after ambiguous outcome risks duplicate. KAI cannot guarantee prevention without reconciliation.
- **Provider with query/reconciliation API**: can attempt to determine state before/after; not generically implemented.
- **Provider without reconciliation**: only safe options: mark INDETERMINATE and require human intervention; or treat as non-retryable in ambiguous cases.

### What KAI can guarantee now
- Runtime: cursor prevents re-execution of completed steps (persisted evidence exists). Claim prevents concurrent writers.
- Recovery: fail-safe (no false COMPLETED). Explicit RETRY path only.

### What KAI cannot guarantee now
- Duplicate prevention on crash-before-evidence when provider lacks idempotency and no reconciliation.

### Policy stance (by decision)
Document risk (DEC-004 in review). Do **not** add generic idempotency infrastructure now unless required by boundary decision. The ambiguous outcome is handled safely (fail-safe) for the vertical slice; provider idempotency is an external constraint. Any governed capability where duplicates are unacceptable must be evaluated against provider capabilities.

### Required (conceptual)
For governed capabilities (per DEC-001/003), the implementation phase must consider: does provider support idempotency keys? Is there reconciliation? If no, is INDETERMINATE+human intervention the required behavior? These are constraints, not code to add now.
## 10. Cross-Decision Invariants

Resulting invariants from DEC-001-DEC-003:

| ID | Invariant | Evidence | Enforcement (post-impl) | Test (required) | Status |
|---|---|---|---|---|---|
| XINV-001 | Governed side effects affecting Task fulfillment/completion only via Core (ToolGateway). Extension paths must not expose them. | DEC-001/002; runtime/service.py calls gateway.execute(); direct APIs bypass | Capability classification + routing/design enforcement | Tests asserting governed ops require Core path | DEFINED (decision) |
| XINV-002 | ToolGateway is non-bypassable for governed side effects; bypass allowed only for classified Extension ops with no Task lifecycle. | DEC-002; gateway pipeline exists | Classification rules | Tests for non-bypassability of governed ops | DEFINED |
| XINV-003 | Actions affecting Task fulfillment/completion produce governed evidence in RuntimeExecution envelope via Core. | DEC-003; runtime/evidence.py, RuntimeExecution model | Envelope enforcement on Core | Tests verifying governed actions produce evidence | DEFINED |
| XINV-004 | Extension operations do not claim governed semantics (no governed evidence/Task completion). | DEC-001/003 | Boundary contract | Tests preventing semantics leakage | DEFINED |
| XINV-005 | Ambiguous outcomes are fail-safe (never auto-COMPLETED); INDETERMINATE→REQUIRES_ATTENTION with explicit retry path. | Current behavior verified; external outcome model | Existing state machine (no change required) | Recovery tests for ambiguous outcomes | CONFIRMED (existing) |
| XINV-006 | Duplicate prevention for ambiguous-crash-before-evidence depends on provider idempotency/reconciliation; risk is documented. | Idempotency model | Documented constraint (no forced abstraction now) | Tests documenting behavior per provider constraints | DEFINED |

Also retain existing Core invariants (INV-001-INV-020) for governed path.
## 11. Decision Dependency Graph

`	ext
DEC-001 (Core vs Extension boundary definition)
   ↓
DEC-002 (ToolGateway non-bypassability by classification)
   ↓
DEC-003 (Evidence/Task participation rules per path/capability)
`

**Explanation:** DEC-001 establishes trust model and what 'governed' means (foundational). DEC-002 operationalizes non-bypassability based on that classification. DEC-003 defines when governed envelope/evidence is required, depending on boundary rules from DEC-001 and enforcement stance from DEC-002.

## 12. Rejected Alternatives

- **A (all via Runtime/ToolGateway)**: Rejected as immediate choice due to breaking risk to existing direct API surface and operational flexibility; could be end-state but not necessary now.
- **B (shared contracts only)**: Insufficient without explicit boundary classification; risks ambiguous enforcement.
- **C (explicit split without hybrid classification)**: Less precise than D for operational writes.

**Chosen:** D with conditions.

## 13. Out of Scope

- Gmail design, Computer Use, Desktop Agent, loops, generalized workflows, richer IR, new provider integrations
- Global enforcement code changes to remove all bypasses now
- Adding IR primitives, states, fields, interfaces, provider wrappers unless directly required by decision closure (none are)
- Broad idempotency infrastructure — deferred pending boundary clarity

## 14. IMPLEMENTATION GATE

**Safe to modify production code?** **NO**

**Why:** Decisions are closed as ACCEPTED WITH CONDITIONS but require capability classification and boundary contract definition first. Enforcement code changes risk architectural drift without that groundwork.

**Minimal first implementation slice (only first step):**
1. **Define capability classification matrix** for calendar/gmail/contacts endpoints (governed vs operational). For each endpoint/capability, determine: (a) affects Task fulfillment/completion? (b) participates in Task lifecycle? (c) requires governed evidence? Document results and rationale. **No code modifications** — this is a design artifact to operationalize DEC-001-DEC-003.

All other work is deferred until this classification is complete and reviewed.

## 15. Final Status Summary

- **DEC-001:** ACCEPTED WITH CONDITIONS (Hybrid Model D)
- **DEC-002:** ACCEPTED WITH CONDITIONS (conditional by classification)
- **DEC-003:** ACCEPTED WITH CONDITIONS (capability-classified envelope)

**Key 5 resulting invariants:** XINV-001 (governed side effects via Core only), XINV-002 (ToolGateway non-bypassable for governed ops), XINV-003 (governed actions produce evidence in RuntimeExecution), XINV-004 (no governed semantics leakage from Extension), XINV-005 (ambiguous outcomes fail-safe). 

**KAI readiness for implementation:** **Not yet ready** to modify production code. First step is capability classification matrix (design only). Implementation gate is clear.
