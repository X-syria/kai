---
name: kai-fulfillment-semantics
description: Review KAI fulfillment evaluators and evidence semantics without accidentally generalizing guarantees across capability domains.
compatibility: opencode
metadata:
  opencode/slash: "true"
---

# KAI Fulfillment Semantics

Use when working on fulfillment evaluation, evidence qualification, retry/recovery, or SuccessCriteria.

## Start with current source of truth

Read the current sealed contract/SuccessCriteria representation, evaluator dispatch, evidence qualification functions, recovery path, decision records, and tests. Historical notes can help locate the design, but current repository code and authoritative docs must be checked before relying on them.

## Semantic review checklist

- State the proposition that makes fulfillment true, false, or indeterminate for the specific domain. Do not assume one domain's semantics apply to another.
- Verify that a single qualifying evidence witness satisfies all required criteria within that witness; do not accidentally combine partial criteria across unrelated witnesses.
- Check qualification conditions such as correct tool/domain, successful execution status, explicit success flags, verification status, and required non-empty evidence fields.
- Check ordering and duplicate invariance where the contract requires them; include permutations and duplicate evidence in tests.
- Check empty evidence, irrelevant evidence, multiple candidates, malformed evidence, and mixed valid/invalid sibling witnesses.
- Treat persisted outcomes as audit/recovery inputs only to the extent the current design specifies. Verify whether recovery re-derives the result from durable evidence; do not assume a stored verdict is authoritative.
- Audit every criterion against the event/effect actually evidenced. Check temporal boundaries, durations, identities/recipients, and any projection from sealed intent to runtime evidence.
- Do not claim generalization from two domains based only on shared code shape. Require domain-specific semantic justification, explicit dispatch, and tests for the new domain.
- Preserve an explicit evaluator version and decision semantics when the architecture requires versioned evaluation.

## Change protocol

Before proposing implementation, write down the formal expected predicate in plain language, map each criterion to a source of evidence, list invalid/ambiguous cases, and name the tests that establish each invariant. If a criterion's semantic source is unclear, report the gap rather than inventing one.
