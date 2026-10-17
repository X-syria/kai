# External Alert Destination — Run 01

Verdict: **the alert path is real and reaches the public internet; no operator has
been paged.** `DEPLOYMENT-READY — EXTERNAL ALERT DESTINATION REQUIRED` still
stands.

## What this run built

A single application-owned webhook for task-execution incidents, off by default.
KAI previously had two alerting paths and neither could tell an operator that one
specific task failed: Prometheus rules describe the *service* and are pulled by a
monitor that has to be configured separately, and the purge job's exit status
belongs to systemd.

| Piece | Where |
| --- | --- |
| Classification, payload, delivery | `src/kai/observability/alerting.py` |
| The only seam that can fire an alert | `src/kai/tasks/service.py:_transition_task_locked` |
| Configuration and production refusal | `src/kai/config.py` |
| Counter | `kai_alerts_delivered_total{kind,outcome}` |
| Lifecycle tests | `tests/test_alerting.py` |
| Documentation | `docs/ALERTING.md` |

Two decisions carry the weight:

- **The seam is the state machine, not the runtime.** `_transition_task_locked` is
  the only writer of `task.state` in the codebase, so one call site covers the
  runtime's `_fail`/`_halt`, a client declaring its own task failed, and anything
  added later. Hooking the runtime would have covered two of those three.
- **The payload excludes `goal` and `last_error` by construction.** The alert
  names which operation failed — task and tenant IDs, both states, the sealed
  profile, retry counters — so it can be looked up, and carries nothing else.
  "No secrets in the alert" is therefore structural rather than a redaction list
  someone has to keep complete.

## What was proven

### Against a real stack — 28 tests, all passing

Real PostgreSQL, the real ASGI app, and a real HTTP receiver on a real socket. No
part of the alerting path is mocked: the service builds its own client, encodes
its own body, and opens its own connection, and the suite reads what arrived.

- **Commit precedes notification.** The receiver suspends the transition while it
  holds the request and the row is read from an independent session in that
  window; it is already `FAILED` with `failed_at` and `last_error` set. An alert
  that could describe a failure which then failed to persist would be an alert
  about a fiction.
- **The full lifecycle over HTTP**: `CREATED → … → EXECUTING → FAILED` fires once,
  `RETRY → EXECUTING → VERIFYING → COMPLETED` resolves once, and the receiver saw
  exactly two notifications for one incident.
- **Idempotence**: `FAILED → RETRY` stays silent, `REQUIRES_ATTENTION → FAILED`
  (a legal edge) does not manufacture a second incident, refused transitions emit
  nothing, and an unrecognised state is silent rather than falsely resolved.
- **No leakage**: a `fail_reason` carrying `sk_live_51H8xQ`, an email address and
  free text, plus the task's goal, are absent from the transmitted bytes — and so
  is the destination's own token path. A broken destination is logged with scheme
  and host only.
- **Alerting is not a dependency**: a 500, a dead endpoint and a refused
  connection all cost the alert and never the transition.
- **Production refuses bad configuration**: enabled with no URL, a URL set while
  disabled, plaintext HTTP, and a hostless URL are all startup failures.

### Against the public internet — transport and payload

The real config-driven entry point `notify_task_transition` was pointed at
`https://httpbin.org/post` and returned `task_failure` then
`task_failure_resolved`: both notifications were accepted by a server we do not
control. The exact production bytes (319 and 327) were echoed back:

```json
{"schema":"kai.task_failure_alert.v1","kind":"task_failure",
 "task_id":"c9a3d82b-…","tenant_id":"72c1ce25-…","from_state":"EXECUTING",
 "to_state":"FAILED","fulfillment_profile":"calendar_meeting","attempts":1,
 "max_retries":3,"occurred_at":"2026-10-02T23:19:42.353842Z"}
```

Leakage audit against the echo: provider error text, the user's address, the
user's goal, and the destination — all **absent**.

Egress was also confirmed earlier to a fake Slack URL (404 from Slack, so the
network path is real) and a fake PagerDuty `events` enqueue (400 from PagerDuty).

## What was not proven

**No human was notified.** `httpbin.org` echoes what it receives and nothing
else. It proves that bytes left the process, traversed the internet, and arrived
intact at a third party. It does not prove a person saw them, and this run had no
Slack, PagerDuty, webhook.site or on-call credential to establish that.

Until a real destination receives both the firing and the resolved notification
and a human confirms seeing it, this is not `PRODUCTION-READY`. A payload
arriving somewhere is not the same as an operator having read it.

Also unproven, and deliberately so: escalation. `ESCALATE` is terminal and stays
inside the unresolved-failure set, so an escalated task keeps the original firing
alert and is **not** told it was escalated. That is a silent extra rather than a
false "resolved", and it is a real gap.

## Known gap: the crash window

Notification happens after the commit, in the same process. A process that dies
between the two leaves a durable failure with no notification. Closing that
needs a durable outbox — a migration and a delivery worker — and was not built
here because it was not asked for. The invariant actually enforced and tested is
the weaker, true one: **a failed notification never costs a state transition**,
and the loss is always visible as `outcome="failed"` and an
`alert_delivery_failed` record.

## Regression status

| Check | Result |
| --- | --- |
| `pytest tests/test_alerting.py` | 28 passed |
| Full `pytest` | 1200 passed, 9 skipped, 1 pre-existing failure |
| `ruff check src tests` | clean |
| `mypy src` | clean, 130 files |
| `alembic check` / `heads` | no new operations; single head `d4e5f6a7b8c9` |

The single failure is
`tests/test_sealed_recipient_intent_design.py::test_a_memory_address_reaches_the_understanding_model`
(`DID NOT RAISE ValueError` on a `date.today() - 1 day` window). It is unrelated
to alerting — it exercises the understanding stage's sealed-recipient design — and
it was confirmed failing at the pristine baseline commit `758ad9bb` with this
change stashed. It is not caused by, and does not block, this work.