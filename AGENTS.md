# AGENTS.md — Project Governance & Agent Instructions

## Project Direction (Current)

KAI currently follows this sequencing:

Engineering Proof → Product Proof → Market Validation → Expansion after traction

KAI is a calculated technical bet worth continuing, but not yet a commercially proven company. The goal is to prove KAI deserves to be built commercially, not to build KAI's entire future at once.

## Strategic Phases

See `docs/ROADMAP.md` for the authoritative phase definitions and sequencing.

## Scope Rule

No feature expansion without a concrete reason tied to the current phase. Do not expand capabilities for hypothetical future needs. Expansion only after traction.

## Architecture Authority

The Canonical Architecture (`docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md`) is the highest authority for KAI identity and component boundaries. It defines Core vs Extension vs Future and non-negotiable principles.

## Roadmap Authority

`docs/ROADMAP.md` defines the current strategic phase and sequencing. It is the official source of truth for strategic direction.

## Core vs Extension Boundary

- **Governed Core Path**: AgentRuntime → ToolGateway → Policy/Approval/Identity → Execution → Evidence → Evaluation → Fulfillment → Recovery. This is KAI's governed execution surface.
- **Direct/Operational Extension Paths**: Certain API surfaces may call provider services directly. Existence of an Extension path does not imply it carries all guarantees of the Governed Core. Governance guarantees apply to the Core path as defined by the canonical architecture and boundary contract.

## AI-assisted Engineering Policy

AI-generated code is not trusted automatically. All meaningful changes require appropriate validation, as applicable:
- Tests, static analysis, type checking (mypy), linting (ruff)
- Security review, concurrency review, database review
- Integration testing, failure testing, load testing (when relevant)
- Staging validation, monitoring, rollback readiness

## Change Discipline

- Do not perform broad refactors without clear evidence and necessity tied to current phase.
- Do not delete production code without concrete evidence, full dependency analysis, and a clear justification (document path, purpose, references, dependencies, risk).
- Do not introduce duplicate architecture or competing abstractions.
- Do not change closed/frozen semantics without explicit evidence the existing decision is incorrect.
- Prefer minimal, safe changes. If uncertain, document and stop.

## Code is Evidence

The repository's current code, tests, and migrations are the source of truth. Documentation must reflect reality; when they diverge, code wins until reconciled with evidence.
