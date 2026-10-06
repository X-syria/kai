# KAI — Documentation Status

**Last updated:** 2026-10-06  
**Scope:** record of which document is the reference for what, and what was
kept, archived, or removed. **This file is an index, not an architecture
reference.** The architecture reference is
[`KAI_REFERENCE_ARCHITECTURE_AR.md`](KAI_REFERENCE_ARCHITECTURE_AR.md); it is not
summarised or restated here, and where the two disagree the architecture reference
wins.

> **Governance ruling applied (Human Decision Gate 2026-10-06):** `AR@HEAD` is the
> canonical copy; the working-tree copy was encoding corruption only (BOM / CRLF /
> cp1256 mojibake) and has been restored, byte-identical, with no text edit.
> `docs/ROADMAP.md` is the **Official Roadmap**; any older passage in this file that
> says otherwise is stale documentation, not competing authority. See Register
> §14.9 in `KAI_CAPABILITY_BOUNDARY_CONTRACT.md`.

## 1. The reference ladder

Read top-down. Each level explains the level above it and must never contradict it.

```text
1. Canonical Architecture / Identity   ->  KAI_REFERENCE_ARCHITECTURE_AR.md
2. Official Roadmap                    ->  docs/ROADMAP.md
3. Implementation Specifications       ->  ../kai-ir-spec-v0.md
4. Supporting Architecture Docs        ->  KAI_CAPABILITY_CLASSIFICATION_MATRIX.md (explicitly NON_AUTHORITATIVE)
                                           + KAI_CAPABILITY_BOUNDARY_CONTRACT.md (register of decisions)
5. Governance                          ->  AGENTS.md
6. Experiments / Engineering Reports   ->  (see full inventory; none authoritative)
```

No other document in the repository claims to be "the source of truth" for
KAI's architecture, and none may be introduced without changing this ladder first.
Documents that record governance or decisions (AGENTS.md, the Capability Boundary
Contract Register) sit below the canonical reference and the official roadmap; they
never override either.

## 2. Pointers, and what each one is authoritative for

| # | Document | Role | Authoritative for | Not authoritative for |
| --- | --- | --- | --- | --- |
| 1 | `docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md` | **Canonical Reference** | KAI identity, component authority boundaries, Core / Required / Extension / Future, non-goals, the freeze point | execution detail, scheduling, priorities |
| 2 | `docs/ROADMAP.md` | **Official Roadmap** | strategic phases and sequencing (Phase 1–4), the expansion-after-traction rule | feature lists |
| 3 | `docs/kai-ir-spec-v0.md` | **Current Design / Supporting Specification** | KAI-IR primitives, validation, canonical form, control-flow contract (§8) | KAI identity, whether KAI-IR may hold authority |
| 4 | `KAI_CAPABILITY_BOUNDARY_CONTRACT.md` | Register of capability-classification and promotion decisions | recorded decisions and their authorizations | decision-making itself (see AGENTS.md / AR) |
| — | `docs/architecture/KAI_CAPABILITY_CLASSIFICATION_MATRIX.md` | **Explicitly NON_AUTHORITATIVE** (D.4 2026-10-06) | nothing at this stage | all of it — design-only inventory, may be reconsidered by an explicit later decision |
| — | `README.md` | Operational (setup, layout, tests) | how to run the project | architecture, scope, identity |

The old "the roadmap gap is deliberate" claim in earlier revisions of this file is
**stale** and is withdrawn here: `docs/ROADMAP.md` exists and is the official
roadmap (D.3). The canonical architecture never replaced the execution roadmap;
the roadmap document now does that.

## 3. Full document inventory

Every documentation artifact in the repository, with its classification.

| Document | Classification | Verdict |
| --- | --- | --- |
| `docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md` | Canonical Reference | canonical at `HEAD`; restored from encoding corruption 2026-10-06 (no text edit) |
| `docs/ROADMAP.md` | Official Roadmap | official per D.3 (2026-10-06) |
| `AGENTS.md` | Governance | tracked-authoritative per D.4 (2026-10-06) |
| `KAI_CAPABILITY_BOUNDARY_CONTRACT.md` | Governance / Register | tracked-authoritative per D.4 (2026-10-06); §14.7 closed, §14.9 records the gate rulings |
| `docs/kai-ir-spec-v0.md` | Current Design / Supporting Specification | kept unchanged in substance |
| `docs/architecture/KAI_CAPABILITY_CLASSIFICATION_MATRIX.md` | Design-only inventory | **non-authoritative** per D.4 (2026-10-06); not promoted |
| `docs/architecture/DOCUMENTATION_STATUS.md` | Index (this file) | kept; reconciled 2026-10-06 |
| `README.md` | Current operational doc | kept; one contradictory pointer corrected |
| `docs/ALERTING.md`, `docs/OBSERVABILITY.md` | Operational reference | kept (tracked) |
| `docs/PRODUCTION-SECURITY-01-REPORT.md`, `docs/PRODUCTION-OBSERVABILITY-01-REPORT.md`, `docs/PRODUCTION-RELEASE-01-REPORT.md`, `docs/FINAL-PRODUCTION-GO-LIVE-01-REPORT.md` | Engineering reports | kept (tracked) |
| `docs/plans/PHASE_2F_RECOVERY_GAP_CLOSURE_PLAN.md` | Engineering plan (phase 2F) | kept (tracked) |
| `docs/archive/` | Historical/archived material | kept (untracked) |
| `docs/EXTERNAL-ALERT-DESTINATION-01-REPORT.md`, `docs/SECURITY_BASELINE.md`, `docs/plans/PHASE_2A_*`, `docs/plans/PHASE_2C_*`, `docs/plans/PHASE_2D_*`, `docs/plans/PHASE_2E_*` | Working-tree reports and plans | present (untracked); not authoritative |

**Git-history remark (corrected 2026-10-06).** An earlier revision claimed that
"across the entire Git history only two documentation files have ever existed".
That was true in 2026-09-27 and is no longer true: Git history now contains the
README, the IR spec, both architecture references, DOCUMENTATION_STATUS, the
observability/alerting docs, and the production reports (latest addition
`docs/plans/PHASE_2F_RECOVERY_GAP_CLOSURE_PLAN.md`, commit `ebed487`). This index
is authoritative about the current tree, not about history.

Nothing in the repository is classified as **Superseded** or **Duplicate** at the
file level at HEAD.

## 4. Why the key documents are kept

### `docs/kai-ir-spec-v0.md` — kept

It is a live implementation specification, not a superseded draft. It has hard
dependencies that would break if it moved or was renumbered:

- cited by path in `src/kai/ir/__init__.py:17` and `src/kai/ir/errors.py:3`;
- cited by path in `tests/test_kai_ir.py:14`;
- cited by **section number** in 34 docstring lines across 8 modules
  (`src/kai/ir/{adapter,build,control,errors,nodes,program}.py`,
  `src/kai/planner/schemas.py`, `src/kai/runtime/service.py` — `spec §2.1`,
  `§2.4`, `§2.5`, `§4.4`, `§5`, `§6`, `§7`, `§7.1`, `§8.3`, `§8.9`, `§8.14`,
  `§8.15`, `§10`, `§11`, `§16`, …). Its own §12 note records that these numbers
  are stable by convention, so sections were **not** renumbered; the authority
  note was added above §0 instead.

Its content is consistent with the canonical reference, not in conflict with it:
§8.7 (Runtime authority), §8.8 (Tool Gateway authority), §8.5 (condition
evaluation grants nothing), and §4.4.2 (declarations are not grants) all restate
the same boundaries.

### `README.md` — kept

Required by tooling: `pyproject.toml` declares `readme = "README.md"` and the
`Dockerfile` does `COPY pyproject.toml README.md ./`. Deleting or moving it would
break the build.

## 5. Changes made to remove ambiguity

| Change | Reason |
| --- | --- |
| Installed the canonical reference at `docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md` | the approved reference existed only outside the repository, so nothing in the tree pointed to it |
| Added a §0 status block to the canonical reference | it must be legible as canonical on its own, without a reader inferring it from context |
| Added a scope-of-authority note to `docs/kai-ir-spec-v0.md` | a specification in a `docs/` tree can be misread as the project's identity; it now states that it refines the reference architecture and does not define KAI's identity |
| Replaced the README's `Source of truth: Technical & Product Specification v0.5` with the reference ladder | that document is **not in this repository and never was** in Git history, and asserting it as the source of truth contradicted the canonical reference |
| Reconciled this file (2026-10-06): registered `docs/ROADMAP.md` as Official Roadmap, corrected the stale "roadmap gap" and "two documents in history" passages, listed the governance documents and their D.4 verdicts, marked the Classification Matrix non-authoritative | recorded governance rulings D.2/D.3/D.4 from the Human Decision Gate (Register §14.9) |

**Corrected after code review:** the earlier version of this list also claimed
that no document said the Tool Gateway can be bypassed. That claim was about the
*agent* path only, and as a system-wide statement it did not match the code. The
Tool Gateway is mandatory for agent execution — `AgentRuntime._execute` is the only
production caller of `ToolGateway.execute` — but direct vertical API endpoints
(`api/calendar.py`, `api/gmail.py`, `api/contacts.py`) call the calendar / gmail /
contacts services directly, without traversing the gateway pipeline and therefore
without Policy, Approval, Identity Resolver, Content Authorizer, the
reserved-argument guard, or `ContractVerificationEngine`. The canonical reference
§4.2 now states this. The two paths are **not** unified by this change, and no
security judgement is made about them here.

## 6. Archived

**Nothing.** There was no file whose historical value outweighed its cost in the
tree, and Git history preserves the evolution of the tracked documents. Historical
working-tree material lives untracked in `docs/archive/`.

## 7. Removed

**Nothing.** No file was deleted. If a document is added in future that turns out
to be superseded, archive it under `docs/archive/<area>/` with a one-line header
naming what superseded it, rather than deleting it.

## 8. Still-historical material, and what not to read it as

- **Git history before `4d9c057`** predates the KAI-IR specification. It shows
  the SaaS-foundation origin of the repository (tenancy, Google APIs,
  authentication, persistence, task lifecycle) but not KAI's distinctive
  architecture. It is history, not a current design reference.
- **`src/kai/**` docstrings carrying "v0.5"** (`tasks/state.py`, `tasks/schemas.py`,
  `planner/schemas.py`, `tools/__init__.py`, `approvals/__init__.py`,
  `identity/service.py`, `config.py`) refer to the *slice* version of the
  schedule-a-meeting vocabulary. They are not a claim about project identity and
  must not be cited as one.
- The canonical reference's own **closing appendix** (الأصل المنتجي / الأصل
  المعماري) is a historical statement about how the project reached its current
  shape. It is not a design proposal and not a source of current requirements.

## 9. Verification for this change

Documentation-only. No production code, IR primitive, branching semantics,
success/fulfillment semantics, database schema, or API behaviour was touched.

- `docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md` restored byte-identical to
  `HEAD` (blob `b6b279b…`, hash-object verified);
- every `docs/kai-ir-spec-v0.md` path reference in source and tests still resolves;
- every Markdown link added resolves to a file that exists in the repository;
- identity promotion is a separate unit (see Register §14.7 closure);
- the `gmail INDETERMINATE` change remains `UNAUTHORIZED — SEPARATE PROMOTION UNIT`;
- `ruff` and `mypy` clean (see the promotion report for counts).