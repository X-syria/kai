---
name: kai-state-integrity
description: Protect the working tree - record a baseline, snapshot before mutating, and restore temporary changes to their exact original bytes without destructive Git commands.
when_to_use: Before and during any task that edits, experiments on, or temporarily modifies files in this repository. Mandatory for mutation testing and any throwaway edit.
user-invocable: true
---

# KAI State Integrity

This skill exists to make it impossible for an agent to destroy work it did
not create. In this repository the working tree frequently contains changes
that are not in any commit and exist nowhere else. Those changes are the user's
work.

## Core principle

> **Anything that existed in the working tree before the task began is
> USER-OWNED BASELINE. It is never a byproduct. It is never yours to discard.**

## Scope

Owns:
- recording the pre-task repository state
- snapshotting files before modifying them
- restoring temporary mutations to their exact original bytes
- the prohibition on destructive Git commands

Does not own:
- what a change should contain -> `kai-engineering`
- how to prove a change is correct -> `kai-test-and-mutation`
- architecture or domain questions

## Mandatory cycle

```
baseline
-> snapshot
-> mutation
-> test
-> exact rollback
-> checksum verification
```

A task is not finished until checksums match again.

## Step 1 - Baseline

Run before touching anything:

```bash
git status --porcelain
git diff --stat
git diff --name-only
git diff --cached --stat
```

Record the output. Every line that appears here is a user-owned change you must
preserve.

## Step 2 - Snapshot

Before editing a file, record its exact bytes so the original can be restored:

```bash
sha256sum path/to/file.py
cp path/to/file.py /path/outside/repo/file.py.bak
```

Store snapshots outside the repository working tree.

## Step 3 - Mutation

Make the smallest possible change. Keep the edit scoped to the files listed as
owned by the current skill.

## Step 4 - Test

Run the tests that prove the change. Follow `kai-test-and-mutation` for test
design. Do not skip verification because "the change is obviously safe".

## Step 5 - Exact rollback

Restore the snapshot:

```bash
cp /path/outside/repo/file.py.bak path/to/file.py
```

The file must return to the same bytes it had in step 2. Semantically
"equivalent" rewrites are not a rollback.

## Step 6 - Checksum verification

```bash
sha256sum path/to/file.py
```

The digest must equal the one recorded in step 2. Then re-run
`git status --porcelain` and confirm the result matches the step 1 baseline,
plus the intended changes only.

## Prohibited commands

These are forbidden without an explicit, unambiguous instruction from the user
given in the current conversation. They are never a legitimate recovery step:

```
git reset --hard
git restore .
git checkout -- .
git clean -fd
git clean -fdx
```

Also prohibited:

- `git stash`, `git checkout <branch>` on a dirty tree, or any other operation
  that relocates uncommitted work without a traceable restore path.
- Reverting a file to `HEAD` when that file has user modifications. `HEAD` is
  not the user's state; the recorded snapshot is.
- Amending, rebasing, or rewriting history.
- Deleting a file that existed before the task began.

## Rules

1. Never use Git to return a file that contains user changes. Restore from the
   snapshot, not from `HEAD`.
2. A temporary mutation must end at the same bytes it started from.
3. If a rollback cannot be verified by checksum, report it as unverified. Do
   not claim success.
4. If baseline state is already dirty in the files you must touch, stop and
   ask the user before editing.
5. Removing your own temporary files is allowed; removing pre-existing files
   is not.
