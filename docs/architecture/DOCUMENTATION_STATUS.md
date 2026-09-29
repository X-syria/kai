# KAI — Documentation Status

**Last updated:** 2026-09-27  
**Scope:** record of which document is the reference for what, and what was
kept, archived, or removed. **This file is an index, not an architecture
reference.** The architecture reference is
[`KAI_REFERENCE_ARCHITECTURE_AR.md`](KAI_REFERENCE_ARCHITECTURE_AR.md); it is not
summarised or restated here, and where the two disagree the architecture reference
wins.

## 1. The reference ladder

Read top-down. Each level explains the level above it and must never contradict it.

```text
1. Canonical Architecture / Identity   ->  KAI_REFERENCE_ARCHITECTURE_AR.md
2. Official Roadmap                    ->  (see §2: none in this repository)
3. Implementation Specifications       ->  ../kai-ir-spec-v0.md
4. Experiments / Engineering Reports   ->  (none in this repository)
```

No other document in the repository claims to be "the source of truth" for
KAI's architecture, and none may be introduced without changing this ladder first.

## 2. Pointers, and what each one is authoritative for

| # | Document | Role | Authoritative for | Not authoritative for |
| --- | --- | --- | --- | --- |
| 1 | `docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md` | **Canonical Reference** | KAI identity, component authority boundaries, Core / Required / Extension / Future, non-goals, the freeze point | execution detail, scheduling, priorities |
| 2 | — | Official Roadmap | *no document in this repository currently holds this role* | — |
| 3 | `docs/kai-ir-spec-v0.md` | **Current Design / Supporting Specification** | KAI-IR primitives, validation, canonical form, control-flow contract (§8) | KAI identity, whether KAI-IR may hold authority |
| 4 | — | Experiments / Reports | *none in this repository* | — |
| — | `README.md` | Operational (setup, layout, tests) | how to run the project | architecture, scope, identity |

**The roadmap gap is deliberate and is not filled here.** The canonical
architecture states explicitly that it does not replace an execution roadmap, and
writing one here would be a project-planning decision, not a documentation-hygiene
one. Until a roadmap document exists, it is an open gap, not a dangling reference.

## 3. Full document inventory

Every documentation artifact in the repository, with its classification.

| Document | Classification | Verdict |
| --- | --- | --- |
| `docs/architecture/KAI_REFERENCE_ARCHITECTURE_AR.md` | Canonical Reference | installed 2026-09-27; highest authority on KAI identity |
| `docs/kai-ir-spec-v0.md` | Current Design / Supporting Specification | kept unchanged in substance |
| `README.md` | Current operational doc | kept; one contradictory pointer corrected |
| `docs/architecture/DOCUMENTATION_STATUS.md` | Index (this file) | kept; never a substitute for the architecture reference |

Nothing in the repository is classified as **Historical Record**, **Superseded**,
or **Duplicate** at the file level. There were none to classify: across the entire
Git history only two documentation files have ever existed (`README.md` and
`docs/kai-ir-spec-v0.md`), and neither has been superseded or duplicated.

## 4. Why each existing document was kept

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
the same boundaries. Where it is silent — generic intent fulfillment, the final
role of `ResultNode`, the boundary between representation and runtime semantics —
the canonical reference records the gap as open, which is the correct behaviour
for a living spec.

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

The "Technical & Product Specification v0.5" pointer is the only
dangerous/contradictory documentation found. It was a pointer, not a file, so
there was nothing to delete — only a claim to correct. The schedule-a-meeting
vertical slice it described is still real and is now described by where it
actually lives: `src/kai/runtime/service.py` and the test suite.

Specifically checked and found **absent** from every current document, so no
edit was needed: any claim that KAI is a general Workflow Engine, that KAI-IR is
KAI's historical origin or a source of authority, that `ResultNode` alone decides
fulfillment, that Memory is control-flow authority, that the model can grant
permissions, that branching implies a general workflow language, or that loops and
arbitrary graph execution are part of current KAI.

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
tree. Git history already preserves the full evolution of both existing
documents, and the historical record this task needed is already in Git.

## 7. Removed

**Nothing.** No file was deleted. No documentation file in this repository is
unused, unreferenced, superseded, or duplicated, so no deletion was safe to
perform. Nothing was deleted on the grounds of being old.

If a document is added in future that turns out to be superseded, archive it
under `docs/archive/<area>/` with a one-line header naming what superseded it,
rather than deleting it — the historical record of why the architecture moved is
part of the architecture's history.

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

- every `docs/kai-ir-spec-v0.md` path reference in source and tests still resolves;
- every Markdown link added resolves to a file that exists in the repository;
- `ruff` and `mypy` clean; `pytest` green (see the task report for counts).
