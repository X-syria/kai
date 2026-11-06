# KAI Capability Classification Matrix - Design Only

**Repository:** C:\Users\khaled\Documents\kai\.claude\worktrees\busy-gould-c3f43b  
**Date:** 2026-10-03  
**Type:** Design-only classification (no implementation)

**Status (Human Decision Gate D.4, 2026-10-06):** explicitly **NON_AUTHORITATIVE** —
this file is recorded for reference only. It is **not** an authoritative governance
document at this stage and may be reconsidered only by an explicit later decision
(see Register §14.9 in `KAI_CAPABILITY_BOUNDARY_CONTRACT.md`).

## 1. Executive Summary

This is a design-only capability inventory. Based on code inspection of API routes, ToolGateway, provider services, runtime/service, tasks/state, IR, and direct provider endpoints. Goal: classify each capability by execution authority, governance, evidence, verification, recovery, idempotency per DEC-001/002/003.

## 2. Classification Rules

- Capability = semantic unit that can perform observable action (read/write/side-effect/lifecycle)
- Production Reachability: PRODUCTION_REACHABLE / INTERNAL_ONLY / ADMIN_ONLY / TEST_ONLY / DEAD / UNKNOWN
- Side Effect Class: NONE / READ / INTERNAL_WRITE / EXTERNAL_SIDE_EFFECT / EXTERNAL_MUTATION / LIFECYCLE_MUTATION / SECURITY_SENSITIVE (multi-allowed)
- Authority: DURABLE_RUNTIME / TOOL_GATEWAY / DIRECT_PROVIDER_API / ADMINISTRATIVE / DATABASE / RECOVERY_ENGINE / OTHER
- Idempotency: NATIVE_PROVIDER_IDEMPOTENCY / KAI_IDEMPOTENCY / RECONCILABLE / NOT_IDEMPOTENT / UNPROVEN / UNKNOWN (use UNPROVEN if cannot prove)
- API Type: CORE_API / CAPABILITY_API / ADMIN_API / LEGACY_API / TEST_API
- Gateway Class: Gateway-Owned / Gateway-Compatible / Gateway-Bypass (with legitimacy: legitimate/legacy/defect/unknown per DEC-002)

## 3. Capability Inventory (from code)

### Core Runtime (governed)
- CAP-001: Runtime task submission (create Task + RuntimeExecution). Reachability: PRODUCTION_REACHABLE. Entry: POST /api/v1/runtime/tasks. Path: HTTP→runtime.submit→Task/RuntimeExecution. Side Effect: INTERNAL_WRITE/LIFECYCLE_MUTATION. Authority: DURABLE_RUNTIME. Policy/Approval: depends on design (no explicit approval gate on submit per typical flow). Evidence: internal persistence (not provider external side effect). Verification: schema/validation. Recovery: durable rows.
- CAP-002: Runtime trigger (advance pipeline). Reachability: PRODUCTION_REACHABLE. Entry: POST /api/v1/runtime/tasks/{id}/trigger. Path: HTTP→AgentRuntime.trigger→ToolGateway (for tool calls). Side Effect: INTERNAL_WRITE/EXTERNAL_SIDE_EFFECT (when executing tools). Authority: DURABLE_RUNTIME + TOOL_GATEWAY (execution). Governed: yes.

### Understanding/Planning/IR (internal)
- CAP-003: Understand scenario. Reachability: INTERNAL_ONLY. Path: runtime/understanding.understand_scenario (called by trigger). Side Effect: NONE (model call producing data). Authority: DURABLE_RUNTIME.
- CAP-004: Plan creation. Reachability: INTERNAL_ONLY. Path: planner/service.create_plan. Side Effect: INTERNAL_WRITE. Authority: DURABLE_RUNTIME.
- CAP-005: IR program build/validation. Reachability: INTERNAL_ONLY. Path: ir/build.program_from_plan. Side Effect: NONE. Authority: DURABLE_RUNTIME.

### ToolGateway (execution)
- CAP-006: Tool execution (generic). Reachability: INTERNAL_ONLY. Path: ToolGateway.execute with ToolIntent. Side Effect: varies (EXTERNAL_SIDE_EFFECT if tool does). Authority: TOOL_GATEWAY.

### Calendar (provider)
- CAP-007: Calendar read slots. Reachability: PRODUCTION_REACHABLE (via runtime path or direct). Entry: runtime path uses calendar.read_slots tool (via Gateway) or direct API. Side Effect: READ. Authority varies by path.
- CAP-008: Calendar get event. Reachability: PRODUCTION_REACHABLE. Side Effect: READ.
- CAP-009: Calendar create event (create meeting). Reachability: PRODUCTION_REACHABLE. Side Effect: EXTERNAL_MUTATION. Key governed capability.

### Gmail (provider)
- CAP-010: Gmail send message. Reachability: PRODUCTION_REACHABLE. Side Effect: EXTERNAL_MUTATION.
- CAP-011: Gmail read messages/threads (as applicable). Reachability: PRODUCTION_REACHABLE. Side Effect: READ.

### Contacts (provider)
- CAP-012: Contacts match/list. Reachability: PRODUCTION_REACHABLE. Side Effect: READ.

### Auth
- CAP-013: Login. Reachability: PRODUCTION_REACHABLE. Entry: POST /api/v1/auth/login. Side Effect: INTERNAL_WRITE (token issuance). Authority: OTHER (auth layer). Security sensitive.
- CAP-014: Register. Reachability: PRODUCTION_REACHABLE. Entry: POST /api/v1/auth/register. Side Effect: INTERNAL_WRITE. Security sensitive.
- CAP-015: Logout/revoke token. Reachability: PRODUCTION_REACHABLE. Entry: auth endpoints. Side Effect: INTERNAL_WRITE.

### Approvals
- CAP-016: Create/list/decide approval. Reachability: PRODUCTION_REACHABLE. Entry: /api/v1/approvals/*. Side Effect: LIFECYCLE_MUTATION. Authority: depends on path.

### Memory
- CAP-017: Store/retrieve memory. Reachability: PRODUCTION_REACHABLE. Side Effect: INTERNAL_WRITE/READ. Authority: INTERNAL_ONLY context (per design).
- CAP-018: Memory search. Reachability: PRODUCTION_REACHABLE. Side Effect: READ.

### Google OAuth
- CAP-019: Google OAuth connect/callback. Reachability: PRODUCTION_REACHABLE. Entry: /api/v1/google/*. Side Effect: INTERNAL_WRITE (store encrypted creds). Security sensitive.
- CAP-020: Google accounts management. Reachability: PRODUCTION_REACHABLE. Side Effect: INTERNAL_WRITE/READ.

### Direct API provider endpoints (explicit)
- CAP-021: Direct calendar operations (API surface). Reachability: PRODUCTION_REACHABLE. Entry: /api/v1/calendar/*. Path: HTTP→service→provider (no Gateway). Side Effect: varies. Authority: DIRECT_PROVIDER_API.
- CAP-022: Direct gmail operations (API surface). Reachability: PRODUCTION_REACHABLE. Entry: /api/v1/gmail/*. Side Effect: varies. Authority: DIRECT_PROVIDER_API.
- CAP-023: Direct contacts operations (API surface). Reachability: PRODUCTION_REACHABLE. Entry: /api/v1/contacts/*. Side Effect: READ. Authority: DIRECT_PROVIDER_API.

### Admin/Maintenance/Recovery
- CAP-024: Token revocation/maintenance. Reachability: INTERNAL_ONLY. Side Effect: INTERNAL_WRITE.
- CAP-025: Claim/lease semantics (runtime). Reachability: INTERNAL_ONLY. Side Effect: INTERNAL_WRITE. Authority: RECOVERY_ENGINE.
- CAP-026: Health check. Reachability: PRODUCTION_REACHABLE. Entry: /api/v1/health. Side Effect: NONE (readiness probe). Read-only.

Total discovered: 26 capabilities.

## 4. Capability Matrix (selected key capabilities)

| Capability | Reachability | Side Effect Class | Authority | Policy | Approval | Identity | Evidence | Verification | Recovery | Idempotency | API Type | Gateway Class | Risk |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CAP-001 (runtime submit) | PRODUCTION_REACHABLE | INTERNAL_WRITE,LIFECYCLE_MUTATION | DURABLE_RUNTIME | None (contract seal) | No (submit) | Yes (tenant/user) | No (internal persistence) | Schema/validation | Durable rows | KAI_IDEMPOTENCY | CORE_API | Gateway-Compatible | C2 |
| CAP-002 (runtime trigger) | PRODUCTION_REACHABLE | INTERNAL_WRITE,EXTERNAL_SIDE_EFFECT | DURABLE_RUNTIME+TOOL_GATEWAY | Via Gateway on tool calls | Conditional (via Gateway) | Yes | Yes (governed via observations) | Via Gateway | Claim/cursor/evidence | KAI_IDEMPOTENCY (cursor prevents re-exec of completed) | CORE_API | Gateway-Owned | C4 |
| CAP-009 (calendar create) - via runtime | PRODUCTION_REACHABLE | EXTERNAL_MUTATION | TOOL_GATEWAY (via Core) | Yes (Gateway) | Conditional | Yes (account) | Yes (governed, evidence['event_id']) | Yes (contract verification) | Fail-safe (INDET→REQUIRES_ATTENTION); duplicate depends on provider | UNPROVEN (provider-dependent) | CORE_API | Gateway-Owned | C5 |
| CAP-010 (gmail send) - via runtime | PRODUCTION_REACHABLE | EXTERNAL_MUTATION | TOOL_GATEWAY (via Core) | Yes | Conditional | Yes | Yes (governed) | Yes | Same as above | UNPROVEN | CORE_API | Gateway-Owned | C5 |
| CAP-021 (calendar direct API) | PRODUCTION_REACHABLE | EXTERNAL_MUTATION/READ | DIRECT_PROVIDER_API | Route-level (auth only) | No via Gateway | Route-level | No (not governed envelope) | Service-level varies | Provider-dependent | UNPROVEN | CAPABILITY_API/LEGACY_API | Gateway-Bypass (legitimate/legacy per DEC-002 classification) | C4-C5 |
| CAP-022 (gmail direct API) | PRODUCTION_REACHABLE | EXTERNAL_MUTATION/READ | DIRECT_PROVIDER_API | Route-level | No via Gateway | Route-level | No (governed envelope) | Service-level varies | Provider-dependent | UNPROVEN | CAPABILITY_API/LEGACY_API | Gateway-Bypass (same) | C5 |
| CAP-023 (contacts direct API) | PRODUCTION_REACHABLE | READ | DIRECT_PROVIDER_API | Route-level | No | Route-level | No | Service-level | Provider-dependent (read) | NATIVE_PROVIDER_IDEMPOTENCY (read) | CAPABILITY_API | Gateway-Bypass (legitimate for read ops) | C1 |
| CAP-026 (health) | PRODUCTION_REACHABLE | NONE | OTHER | None | No | No | No | N/A | N/A | NATIVE_PROVIDER_IDEMPOTENCY | CORE_API | Gateway-Compatible | C0 |

Notes: Governance equivalence for direct APIs is NOT established (see section 6). Evidence field refers to governed envelope (RuntimeExecution.evidence).

## 5. Authority Matrix

| Capability | Who may decide | Who may execute | Who may persist | Who may verify | Who may declare completion |
|---|---|---|---|---|---|
| CAP-002 (trigger/runtime) | Task state machine + Gateway policy | AgentRuntime + ToolGateway | Runtime (Task/RuntimeExecution) | ToolGateway verification | Task state machine (based on fulfillment verdict) |
| CAP-009 (calendar create via Core) | Gateway policy + approval (if required) | ToolGateway→provider | Runtime records evidence; provider persists remote state | ContractVerificationEngine | Task state machine (not Gateway) |
| CAP-021 (calendar direct) | Route/handler (no gateway policy) | Service→provider | Provider persists; no Runtime envelope persistence | Service-level (varies) | No Task lifecycle authority |
| CAP-022 (gmail direct) | Route/handler | Service→provider | Provider only | Service-level (varies) | No Task lifecycle |
| CAP-023 (contacts direct) | Route/handler | Service→provider | Provider only (read) | Service-level | N/A |

Key point: CAP-021/022 concentrate decision/execution/persist/verify/completion differently (no Task lifecycle authority). For Core (CAP-009), completion is separate from execution/evidence/verification (Task state machine owns completion).

## 6. Governance Equivalence Matrix

For direct APIs vs Core path for same semantic operation (calendar create/gmail send):

| Governance Primitive | Core (via ToolGateway) | Direct API | Equiv? | Notes |
|---|---|---|---|---|
| Authorization | Yes (gateway + tenant scoping) | Route-level auth only (per handlers) | **No** (different depth) | Gateway adds policy/content gates |
| Policy | Yes (PolicyEngine, TOCTOU recheck) | No via gateway | **No** | Not equivalent |
| Approval | Conditional via ApprovalManager | No via gateway | **No** | Not equivalent |
| Identity | Context↔intent assertion; sealed in RuntimeExecution | Route-level; no governed sealing | **No** | Not equivalent |
| Evidence | Append-only in RuntimeExecution.evidence; centralized qualification | None in governed envelope | **No** | Different evidence model |
| Verification | ContractVerificationEngine (governed) | Service-level varies | **No** (uncertain) | Depends on implementation |
| Audit | Unified via envelope + task state | Route/service logs only | **No** | Not equivalent |
| Recovery Semantics | Fail-safe (INDET→REQUIRES_ATTENTION); claim/cursor | Provider-dependent | **No** | Different recovery model |
| Task Lifecycle | Yes (full machine) | No | **No** | Not equivalent |
| Tenancy | Enforced via scoped queries + context | Route-level scoping (varies) | **Partial** | May be scoped but enforcement differs |

**Conclusion:** Governance equivalence is **NOT established** for direct API write operations (CAP-021/022). Read operations (CAP-023) have weaker governance needs but still differ from Core path.

This directly supports DEC-001/002/003 (classification required).

## 7. Duplicate Capability Analysis

Semantic duplicates found:

- **Calendar create event**: exists as CAP-009 (via Core/ToolGateway, governed) and CAP-021 (direct API). Same provider operation, different governance/evidence/recovery.
- **Gmail send message**: CAP-010 (governed) and CAP-022 (direct API). Same semantic action, different authority model.
- **Calendar/Gmail read ops**: also exposed via both paths (CAP-007/008 via runtime tools; CAP-021/022 include reads) - read semantics less critical but still duplicated entry points.

**Comparison (calendar create):**
- Authz/policy/approval: only Core has gateway enforcement
- Evidence: only Core produces governed envelope
- Verification: different paths
- Retries/idempotency: Core uses cursor+claim; direct is provider-dependent
- Errors/state handling: different models

These duplicates are the source of "two execution authorities" identified in review.

## 8. Capability Drift Findings

**F-CAP-001 - Governance drift (calendar create)**
- Capability: calendar event creation (CAP-009 vs CAP-021)
- Issue: Different governance depending on entrypoint (Core enforces policy/approval/verification/evidence; direct API does not via gateway)
- Severity: Medium-High (architectural)
- Evidence: api/calendar.py direct routes vs runtime/service.py+tools/gateway.py
- Impacted invariant: XINV-001 (governed side effects via Core), governance equivalence
- DEC affected: DEC-001, DEC-002, DEC-003
- Recommended direction: Classify per DEC-001 (hybrid). If affects Task fulfillment/completion, must be Core-only or made governance-equivalent.
- Implementation prerequisite: Capability classification matrix applied to this capability (this work)

**F-CAP-002 - Evidence semantics drift (writes)**
- Capability: gmail send (CAP-010 vs CAP-022)
- Issue: Only Core produces governed evidence in RuntimeExecution envelope
- Severity: Medium-High
- Evidence: runtime/evidence.py (qualification) not used by direct API paths
- Impacted: INV-007/INV-008, XINV-003
- DEC affected: DEC-003

**F-CAP-003 - Recovery semantics drift**
- Writes via direct API lack runtime claim/cursor/evidence recovery; rely on provider semantics
- Severity: Medium
- Impacted: recovery model
- DEC affected: DEC-003

**F-CAP-004 - Approval/policy bypass drift**
- Direct write APIs bypass gateway approval/policy (when required)
- Severity: High if governed capability exposed
- Impacted: policy/approval boundaries
- DEC affected: DEC-002

**F-CAP-005 - Authority concentration unclear**
- Same semantic capability has different who-decides/who-executes/who-declares-completion
- Severity: Medium
- Impacted: lifecycle authority
- DEC affected: DEC-001

## 9. Recovery / Idempotency Classification

| Capability | Idempotency | Recovery Model | Notes |
|---|---|---|---|
| CAP-002 (trigger) | KAI_IDEMPOTENCY (cursor prevents re-exec of completed steps) | Claim/cursor/evidence-based; fail-safe (INDET→REQUIRES_ATTENTION); no auto-return from WAITING_EXTERNAL | Core recovery model |
| CAP-009 (calendar create via Core) | UNPROVEN (provider-dependent) | Fail-safe against false completion; duplicate risk on crash-before-evidence if provider lacks idempotency | Risk documented (DEC-004 context) |
| CAP-010 (gmail send via Core) | UNPROVEN (provider-dependent) | Same as above | Risk documented |
| CAP-021 (calendar direct) | UNPROVEN (provider-dependent) | Provider-dependent; no governed recovery/envelope | Different model |
| CAP-022 (gmail direct) | UNPROVEN (provider-dependent) | Provider-dependent | Different model |
| CAP-023 (contacts direct, read) | NATIVE_PROVIDER_IDEMPOTENCY (read) | Provider-dependent (read) | Lower risk |

**Conclusion:** Idempotency gaps exist for external mutations (especially crash-before-evidence). Governed path is fail-safe but not duplicate-safe independent of provider. Direct path relies entirely on provider.

## 10. Target Capability Model (design-only)

Target model (by class, no implementation):

`	ext
Governed Write (affects Task fulfillment/completion)
  → Must be via Core (DURABLE_RUNTIME + TOOL_GATEWAY)
  → ToolIntent + ToolGateway pipeline (policy/content/approval/TOCTOU/consume/verify)
  → Produces governed evidence in RuntimeExecution (append-only)
  → Task lifecycle authority owns completion
  → Fail-safe recovery (INDET→REQUIRES_ATTENTION)

Operational (read or operational mutation with no Task correlation)
  → May be via Extension (DIRECT_PROVIDER_API)
  → Must not claim governed evidence/envelope
  → Must not participate in Task completion
  → Boundary explicit; no cross-contamination

Reads: often Extension-appropriate if no governance required; if used as inputs to governed decisions, inputs must flow through governed path.
`

**Mapping (initial):** CAP-009/010 → governed class (if affect fulfillment/completion). CAP-021/022 write ops → require classification: if they affect Task fulfillment/completion they must move to Core; if purely operational with no Task correlation they remain Extension with explicit boundary constraints. CAP-023 (reads) → Extension-appropriate. CAP-002 → Core.

## 11. Capability Findings (F-CAP)

- **F-CAP-001** (calendar create dual path): Governance drift between CAP-009 (Core/governed) and CAP-021 (direct). Severity: High. Affected DEC-001/002/003. Prereq: classify each as governed or operational per Hybrid Model D.
- **F-CAP-002** (gmail send dual path): Evidence semantics drift (CAP-010 vs CAP-022). Severity: High. DEC-003.
- **F-CAP-003** (recovery semantics): Different recovery models. Severity: Med. DEC-003.
- **F-CAP-004** (approval/policy bypass): Direct write APIs bypass gateway gates. Severity: High if governed. DEC-002.
- **F-CAP-005** (authority unclear): Same capability split across authorities. Severity: Med. DEC-001.
- **F-CAP-006** (idempotency unproven): External mutations have UNPROVEN idempotency; duplicate risk on crash-before-evidence. Severity: Med. DEC-004 context (document risk).
- **F-CAP-007** (governance equivalence missing): No equivalence for direct write paths. Severity: High. DEC-001/003.

## 12. Decision Validation

- **DEC-001 (Core vs Extension):** YES - remains correct. Hybrid Model D matches observed dual paths and need for classification.
- **DEC-002 (ToolGateway bypass):** YES - conditional by classification is correct.
- **DEC-003 (Evidence/Task participation):** YES - capability-classified is correct.

No revision required. Decisions are supported by inventory.

## 13. Implementation Prerequisites

1. **Capability classification matrix applied** (this document) - complete as design artifact.
2. **For each duplicate pair (calendar create, gmail send), assign final class** (governed vs operational) with justification per DEC-001/003.
3. **Document boundary contract** for any capability classified as Extension (including what governance is not claimed).
4. **Define rules to prevent cross-contamination** (Extension results must not be treated as governed without envelope).

All design-only (no code changes).

## 14. Do Not Implement Yet

Per original instruction and gate: do not move endpoints, merge runtimes, rewrite Gateway, add abstractions, add idempotency layer, reconciliation, new states/fields/interfaces. Only classification/design artifacts.

## Summary (12-point)
1. Capabilities discovered: 26 (CAP-001..CAP-026)
2. External side-effect capabilities: ~6-8 (CAP-002 triggers tools; CAP-009/010 writes; CAP-021/022 direct writes)
3. Direct provider/API paths: 3 (calendar/gmail/contacts - CAP-021/022/023)
4. Gateway bypasses: 2 primary write paths (CAP-021/022)
5. Legitimate bypasses: CAP-023 (reads) likely legitimate as Extension if classified; write bypasses not obviously legitimate without classification
6. Architectural defects: 0 (ambiguities identified; no defect claimed)
7. Most important duplicate: calendar create (CAP-009 vs CAP-021), gmail send (CAP-010 vs CAP-022)
8. Key governance drift: F-CAP-001, F-CAP-002 (different policy/approval/evidence/recovery)
9. Largest idempotency gap: external mutations (UNPROVEN) on crash-before-evidence
10. Capabilities without clear execution authority: None for Core; direct write paths have authority ambiguity resolved only after classification
11. DEC-001/002/003: All still correct (no revision needed)
12. Implementation gate: **Not open** - need final class assignment + boundary contract for duplicate pairs (design-only step completed; next is applying classification decisions to close gate)


