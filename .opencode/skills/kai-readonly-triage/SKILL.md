---
name: kai-readonly-triage
description: Investigate KAI test failures, regressions, and repository state without modifying files or assuming failures are pre-existing.
compatibility: opencode
metadata:
  opencode/slash: "true"
---

# KAI Read-Only Triage

Use this skill for failing tests, unexpected diffs, release blockers, regressions, and uncertain repository state.

## Strict default: read-only

Do not edit source, tests, configs, lockfiles, or docs during triage. Do not stage, commit, push, stash, reset, clean, restore, run migrations, or touch production services. Ask for explicit authorization before any fix round.

## Investigation sequence

1. Capture `git status --short`, branch/HEAD, `git diff --stat`, and `git diff --check`.
2. Inspect `git diff HEAD -- <relevant paths>` and the current versions of changed code and fixtures.
3. Run the smallest relevant test selection first; record the exact command, exit code, and complete failure summary.
4. Cluster failures by common symptom, stack frame, fixture/contract assumption, and changed boundary. Provide representative examples for each cluster.
5. Establish failure provenance. Do not label a failure pre-existing merely because a test uses a legacy alias or shorthand. Compare against a verified baseline or historical source before making that claim.
6. Check whether tests encode an old contract, whether fixtures are outdated, or whether enforcement introduced a genuine regression. Keep these explanations separate until evidenced.
7. Report findings, confidence, missing evidence, and a proposed minimal next step. Do not implement the fix as part of diagnosis.

## Safety constraints

- No `git reset --hard`, `git clean`, forced checkout, deletion, or migration commands.
- No changes to working-tree files to make tests pass.
- Do not suppress, skip, or weaken tests solely to restore a green suite.
- Never infer that a passing targeted suite proves full-suite health.
