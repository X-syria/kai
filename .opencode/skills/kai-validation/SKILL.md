---
name: kai-validation
description: Validate KAI changes with focused tests, full regression checks, static analysis, migration checks, and evidence-based reporting.
compatibility: opencode
metadata:
  opencode/slash: "true"
---

# KAI Validation Protocol

Use after an explicitly authorized implementation, or when asked to design its verification plan.

## Before running checks

- Read `AGENTS.md`, relevant architecture/contract docs, and the tests adjacent to the changed code.
- Check `git status --short` and record the pre-existing diff. Do not overwrite unrelated user work.
- Identify the exact invariants and expected behaviors that the change is supposed to preserve.

## Verification order

1. Run focused tests for the changed behavior and its negative/edge cases.
2. Run the relevant broader regression tests for adjacent domains and lifecycle behavior.
3. Run repository-standard lint and type checks (for example, Ruff and Mypy where configured).
4. Run the full test suite when feasible; otherwise explicitly state it was not run and why.
5. If schema or database behavior changed, inspect migration heads/status and migration tests. Never apply or downgrade a real database without explicit authorization.
6. Run `git diff --check` and review the final diff for scope creep, secrets, generated artifacts, and accidental doc/config edits.

## Evidence requirements

- Report exact commands and their exit status.
- Report actual pass/fail/skip counts from output; never estimate them.
- Separate tests that were run from tests that were merely planned.
- A clean lint/type check does not prove semantic correctness.
- A targeted pass does not mean the full suite passed.
- An implementation does not establish production readiness by itself.
- Keep known gaps and untested paths visible in the final report.
