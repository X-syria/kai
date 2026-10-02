# FINAL-PRODUCTION-GO-LIVE-01

## Verdict

**DEPLOYMENT-READY — EXTERNAL ALERT DESTINATION REQUIRED**

The release candidate is internally sound and every control we can exercise without
your infrastructure has been exercised against real infrastructure. It is **not**
`PRODUCTION-READY`, for one reason and one reason only:

> No Alertmanager, receiver or notification destination is configured on this host
> or in this repository. The alert chain in this report was proven against a
> throwaway webhook listener running on the same machine as Prometheus. That proves
> the rule → Prometheus → Alertmanager → receiver protocol, including severity
> routing and resolved notifications. It does **not** prove that a human being at
> 03:00 is paged, because nothing has ever been sent to anyone.

Everything below is what was actually executed, not what was intended.

---

## 1. Release candidate identity

| | |
|---|---|
| Tip | `7ef5d2c0c2f39c29a72d40b38380f9e1a540419b` |
| Parent | `e421a3516a9837702530e2015920a12a829016ca` |
| Tree | `ca9792db17225651838b6e3a9294416aea75eec8` |
| Branch | `master` |
| Working tree | clean |
| Remotes | none |
| Tags | none |
| Diff vs `1c6e57b` | 19 files, +2277 / −18 |

Release commits, oldest first:

```
ab4771a  fix: declare the column comments the migrations already applied
0cfaba5  test: pin the degraded health response and isolate the suite from .env
d396368  fix: let the one-shot jobs start under ENVIRONMENT=production
e421a35  feat: ship the purge schedule and the alert rules
7ef5d2c  docs: record the PRODUCTION-RELEASE-01 verification and the five open gaps
```

Nothing is pushed and nothing is tagged. There is no remote to push to.

---

## 2. Alert delivery: end to end, with timestamps

A Prometheus and an Alertmanager were run against the release-candidate stack,
both on the stack's Docker network, loading the shipped
`deploy/alerting/kai-alerts.yml`. Alertmanager routed to two throwaway local
receivers (`kai-harness-critical` for `severity: critical`, `kai-harness`
otherwise), each writing an arrival line containing the alert name, severity,
`startsAt`, `endsAt` and the time the notification was received.

### 2a. No false positive on a healthy stack

Held the stack healthy for 165 s — longer than the shortest `for:` in the file
(60 s) — then read every rule, the `ALERTS` series and the receiver:

```
all 14 rules                 inactive
ALERTS series                count = 0
alerts held by Alertmanager  0
receiver log file            does not exist
```

Nothing was delivered. The receiver was never written to.

### 2b. The three required alerts, fired and delivered

All UTC.

| Alert | Severity | Trigger | `startsAt` | **Delivered to receiver** |
|---|---|---|---|---|
| `KaiDatabaseUnavailable` | critical | postgres stopped `20:18:52` | `20:24:23` | **`20:24:53`** |
| `KaiDatabaseErrors` | warning | postgres stopped `20:18:52` | `20:29:23` | **`20:29:53`** |
| `KaiApplicationDown` | critical | app stopped `20:31:06` | `20:33:23` | **`20:33:53`** |

The `for:` windows are respected end to end: 5 min for database unavailability,
10 min for the database error rate, 2 min for the application being down. Each
notification reached the correct receiver for its severity, and repeat
notifications continued at the configured interval.

### 2c. Resolution is delivered too

| | |
|---|---|
| Restore (db + app) | `20:35:08` |
| Prometheus `ALERTS` back to empty | `20:36:38` |
| `KaiApplicationDown` `endsAt` | `20:35:53` |
| `KaiDatabaseErrors` `endsAt` | `20:35:53` |
| `KaiDatabaseUnavailable` `endsAt` | `20:35:53` |
| **All three `resolved` notifications delivered** | **`20:36:23`** |
| `/api/v1/health` after restore | `{"status":"ok","database":"reachable"}` |

A page that resolves must say so. It does.

### 2d. What this does and does not establish

Established: the rules are correct and loadable, Prometheus fires and resolves
them on schedule, Alertmanager accepts them, severity routing splits critical
from warning, and both firing and resolved notifications are actually
transmitted to a receiver endpoint.

Not established: that any human or paging system receives them. See the verdict.

---

## 3. Purge scheduler: all four cases, really executed

`deploy/scheduler/run-purge-revocations.sh` was run unstubbed against the
release-candidate database. No test doubles.

| Case | Result |
|---|---|
| **Success** | exit `0`; `{"event":"scheduler_run","status":"ok","exit_code":"0","duration_seconds":"8"}` |
| **Overlap** — 6 concurrent runs | **all exit `0`**; 4 ran the purge, **2 recorded `revocation_purge_skipped` / `reason":"PurgeSkipped"`**; zero non-zero |
| **Database failure** | exit `1`; the DSN appears **0 times** in the captured output |
| **Timeout** (`KAI_PURGE_TIMEOUT_SECONDS=1`) | exit `124`; `{"status":"timeout","exit_code":"124","duration_seconds":"1"}` |

The overlap case is the one worth reading twice. Two of the six runs did not
fail — they recorded `PurgeSkipped` and exited `0`. A purge that finds another
run already holding the advisory lock is *not* a fault, and if it exited
non-zero the 15-minute cron would page operations every time two runs collided.
The lock is doing exactly what it should.

---

## 4. Security regression

`101 passed` across the auth, configuration and purge-maintenance suites, run
from the release candidate. Every area named in the brief has a dedicated test
that was executed:

| Area | Evidence |
|---|---|
| JWT expiry | `test_expired_token_is_rejected`, `test_token_without_expiry_is_rejected` |
| Revocation | `test_revoked_token_cannot_reach_a_protected_route`, `test_a_revoked_token_still_stops_authenticating_after_a_purge` |
| Logout | revokes presented token only; idempotent; requires authentication; error does not distinguish cause |
| Deactivated principal (`is_active`) | `test_deactivated_user_cannot_use_an_existing_token`, `test_deactivated_user_cannot_log_in_again` |
| Algorithm | `test_unapproved_algorithm_cannot_be_used_to_sign`, `test_alg_none_token_is_rejected_on_decode`, `test_production_refuses_the_none_algorithm` |
| CORS | 7 tests: wildcard refused, empty allowlist not installed, never wildcard+credentials, echoes only configured origins, never advertises wildcard |
| TrustedHost | `test_trusted_hosts_rejects_an_unknown_host`, `test_trusted_hosts_allows_a_configured_host` |
| Rate limiting | login limited, `Retry-After` advertised, authenticated routes exempt, production refuses a per-process limiter |
| Startup guards | `ENVIRONMENT=production` rejects `none` algorithm, wildcard CORS, per-process limiter — and every Compose service is asserted to pass real production validation |

---

## 5. Semantic freeze

This release was not supposed to change behaviour, and it did not. The only
source edits are six SQLAlchemy `comment=` declarations. Proven at the AST
level rather than by inspection: comments are absent from the AST by
construction, and `comment=` keyword arguments were removed before comparison.

```
source files changed since 1c6e57b: src/kai/runtime/models.py, src/kai/tasks/models.py

src/kai/runtime/models.py
  AST identical ignoring comments and comment= kwargs : True
  comment= values before : 0     after : 5     added : 5
src/kai/tasks/models.py
  AST identical ignoring comments and comment= kwargs : True
  comment= values before : 0     after : 1     added : 1

VERDICT: comment-only; AST-identical, so no semantic boundary moved
```

The comments match what the migrations already created — independently confirmed
by `alembic check` reporting no drift, which is the entire reason they were
added.

Boundaries confirmed unmoved:

- `LIVE_SEALABLE_PROFILES = frozenset({"calendar_meeting"})` — unchanged.
- `gmail_send` is still **not** live-sealable. A task can be accepted with it
  and then never fulfilled; that is unchanged and still documented.
- Recipient intent for the generic fulfillment path remains unresolved.
- Generic fulfillment remains partially proven.

The single dependency change is `PyYAML>=6.0`, added to the **`dev`** extra
only, so a test no longer depends on `uvicorn[standard]`'s transitive graph.
No runtime dependency changed.

---

## 6. Validation, from a clean clone of the candidate

A fresh clone of the candidate, given a production-shaped `.env`, with real
PostgreSQL and Redis, images built from that clone:

| Check | Result |
|---|---|
| `pytest` | **1173 passed, 9 skipped** (381.67 s) |
| `ruff check` | All checks passed |
| `mypy src` | Success — no issues in 129 source files |
| `alembic check` | No new upgrade operations detected |
| `alembic heads` | `d4e5f6a7b8c9` (single head) |
| `git diff --check` | clean |
| Docker build | `app`, `migrate`, `purge-revocations` all built |
| `migrate` container | exit `0`; ran both final upgrades `a4c7e2b9d1f3 → c8e1f3a5b7d9 → d4e5f6a7b8c9` |
| `purge-revocations` container | exit `0`, structured record emitted |
| `/api/v1/health` | `{"status":"ok","database":"reachable"}` |
| `/api/v1/metrics` with `Host: app:8000` | **200**, 17 `kai_` metric families — the Prometheus scrape is *not* rejected by `TrustedHostMiddleware` |
| register / login / me | `201` / `200` / `200` |

The `Host: app:8000` check is the one that matters for alerting. If the
`TRUSTED_HOSTS` default does not admit the container name Prometheus scrapes
with, every scrape returns 400, `up` is `0`, and `KaiApplicationDown` fires
permanently against a perfectly healthy service. It returns 200.

One deployment stumble is worth recording because it is the kind that gets
misread as an application fault: the first `compose up` in the clean clone
failed on `migrate` with `socket.gaierror: Name or service not known`. The
cause was **not** in the release. An earlier failed `up` had left the `db`
container created but attached to no network, so `db` did not resolve. A clean
`down --volumes` followed by `up` brought the whole stack up correctly on the
first attempt, with no code or configuration change.

---

## 7. Release hygiene

| Check | Result |
|---|---|
| BOM in release commit subjects | **none** in `1c6e57b..HEAD` |
| `core.hooksPath` | unset at local, global and system level |
| Untracked or generated artifacts | none; working tree clean |
| Commits pushed / tags | none |
| Secret material committed | none — the production `.env` used for testing is gitignored and local |

The five release commits originally carried a UTF-8 BOM in the subject line.
They were rewritten subject-only, preserving tree, files, patches, author,
dates and base parent; `git diff 66426b978ee569ea5303012f1df174d9249cab22
HEAD` is empty.

The older commit `d421326` still carries a BOM. It predates this round and was
outside the authorised scope, so it was left alone and is recorded here rather
than silently rewritten.

---

## 8. Things found and deliberately left alone

Disclosed rather than quietly fixed, because fixing them would have been
unrelated change:

1. **`ruff format` is not a project gate.** There is no `[tool.ruff.format]`
   config and no script invokes it; 173 of 211 Python files were already
   unformatted *before* this round. This round adds three test files to that
   set (`test_alerting_rules.py`, `test_scheduler_deployment.py`,
   `test_schema_metadata_parity.py`). They match the existing house style.
   Running the formatter would rewrite 177 files and bury the actual release in
   noise. `ruff check` — the gate that does exist — is clean.

2. **`src/kai/api/router.py` has one bare LF on line 13** in this working tree,
   making its line endings mixed. This is a **local checkout artifact that
   predates this round**: the file is not in the release diff, and the committed
   blob is byte-identical at `1c6e57b` and at HEAD. `core.autocrlf` normalises
   it away, which is why `git status` stays clean. It has no effect on Python or
   on `ruff check`. Not fixed, because fixing it would mean committing an
   unrelated file.

3. **This report supersedes parts of `docs/PRODUCTION-RELEASE-01-REPORT.md`**,
   which quotes the pre-rewrite tip `66426b9` and lists the commit-subject BOM
   as an open gap. Both are now stale; see that file's header.

---

## 9. What would make this PRODUCTION-READY

One thing, and it is not code:

1. Run a real Alertmanager against the deployed stack, pointed at a real
   destination — PagerDuty, Slack, or a webhook your on-call actually watches.
2. Confirm `TRUSTED_HOSTS` on the deployed environment admits whatever host
   Prometheus presents, since that is what decides whether `up` is ever `1`.
3. Re-run §2 against that destination. The rule side is already proven; only
   the last hop is unproven.

Then the verdict becomes `PRODUCTION-READY`, with the five gaps from
`PRODUCTION-RELEASE-01-REPORT.md` still open and tracked separately. None of
them block the deploy; they bound what the deployed system can be trusted to
do.
