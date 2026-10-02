# PRODUCTION-SECURITY-01 — Final Report

## 1. Verdict

**DEPLOYMENT-READY — PRODUCTION HARDENING INCOMPLETE**

Not `PRODUCTION-READY`, and the reason is narrow and specific rather than
general: the request asked for observability/metrics export, and that was not
built. Everything in sections 2–6 is implemented, tested, and verified against a
real container. Section 7 lists what is deliberately still open.

This verdict is a change from the previous round's `DEPLOYMENT-READY`, not a
retraction of it: five concrete, exploitable defects were found in the shipped
code and are now closed.

## 2. What was actually wrong (found by reading code, not by inspection)

Each item cites the code that was wrong before the change.

| # | Defect | Location (pre-fix) | Impact |
| --- | --- | --- | --- |
| D1 | `is_active` was reported by `/auth/me` and enforced nowhere | `src/kai/auth/deps.py:35`, `src/kai/api/auth.py:71` | Deactivating an account did not stop a token already in hand. Suspension was cosmetic. |
| D2 | Login short-circuited on a missing user and skipped the scrypt call | `src/kai/api/auth.py:71` | Free user-enumeration oracle. **Measured: 13.5 ms for an unknown email vs 89.1 ms for a registered one** — a 6.6x gap. |
| D3 | No `jti`, no denylist, no logout | `src/kai/auth/jwt.py:30-44` | A leaked bearer token was valid for its full 120-minute life with no way to kill it short of rotating the secret. |
| D4 | `LoginRequest.password` was unbounded while registration capped it at 128 | `src/kai/api/auth.py:24` | Unauthenticated CPU exhaustion: one request with a multi-megabyte body made the server do megabytes of deliberate scrypt work. |
| D5 | No algorithm allowlist, no `iss`/`aud`, `exp` not required | `src/kai/auth/jwt.py:47-57` | A token omitting `exp` was never treated as non-expiring, and a token minted by another service sharing the secret was accepted. |

Two further weaknesses were closed while in the same code paths:

- **scrypt work-factor downgrade.** `verify_password` trusted `n`/`r`/`p` from
  the stored hash, so a row weakened to `n=2` would verify as-is.
- **Deployment-only boot failure (found by the smoke test, not by tests).**
  Docker Compose passes every declared variable through, including empty ones.
  `CORS_ALLOWED_ORIGINS=""` could not be parsed as a list, so the app died at
  import with a `SettingsError` and served nothing. No test could have caught
  this, because a test process has no empty-string variables. It is now covered
  by `tests/test_config.py::test_empty_env_value_becomes_an_empty_list`.

## 3. What changed

- **`src/kai/auth/jwt.py`** — added `jti` (uuid4) per token; added and verified
  `iss`/`aud`; `exp`, `iat`, `sub`, `jti`, `iss`, `aud` are all *required*;
  algorithm checked against a closed allowlist on both sign and decode.
- **`src/kai/auth/revocation.py`** (new) — `is_token_revoked`,
  `revoke_token` (idempotent), `purge_expired_revocations`.
- **`src/kai/db/models.py` + `alembic/versions/d4e5f6a7b8c9_add_revoked_tokens.py`** —
  `revoked_tokens` table keyed by `jti`, with `expires_at` mirroring the token's
  own `exp` so a record can never outlive the token it suppresses.
- **`src/kai/auth/deps.py`** — checks revocation, then `is_active`. All four
  failure reasons return one identical opaque 401 body.
- **`src/kai/api/auth.py`** — `POST /auth/logout`; `is_active` enforced at
  login; unconditional password verification against a dummy hash; password
  bounded to 128 chars on login.
- **`src/kai/security/ratelimit.py`** (new) — per-source fixed-window limiting on
  the two unauthenticated auth routes. Redis backend for production, in-memory
  for local/test. **Fails closed** if the store is unreachable.
- **`src/kai/security/cors.py`** (new) — explicit-origin allowlist; `*` refused;
  not installed at all when the list is empty.
- **`src/kai/main.py`** — wires TrustedHost, CORS, and the rate limiter.
- **`src/kai/config.py`** — new settings plus production guards that refuse to
  start on: an unapproved algorithm, a wildcard CORS origin, a per-process rate
  limiter, `redis` without a URL, or a `JWT_SECRET` under 32 characters.
- **`pyproject.toml` / `docker-compose.yml` / `.env.example` / `README.md`** —
  declared the now-proven `redis` dependency and the new configuration.

## 4. Evidence

**Tests: 1020 passed** on a clean clone with a fresh virtualenv
(was 977 at baseline; +43 new). Ruff and mypy clean across 121 source files.
Alembic single head `d4e5f6a7b8d9`, verified `downgrade base` → `upgrade head`.

**The tests were proven to fail against the unfixed code.** Reverting only the
four production files and re-running gave:

```
14 failed, 19 passed
```

including the timing oracle quantified at runtime
(`unknown-email login took 13.5ms vs 89.1ms for a registered email`).
A test that cannot fail on the broken code proves nothing, so this was run
deliberately. All 14 were re-verified to pass after restoring the fixes.

**Smoke test: fully green**, including the new revocation gate exercised over
real HTTP against the running container — token authenticates, logout accepted,
revoked token refused, and `revoked_tokens` row confirmed in Postgres.

**One real bug was caught by the smoke test and not by the suite** (the
empty-env-var boot failure above). It is now regression-tested. I am recording
this because it is the honest measure of what the test suite does and does not
cover.

## 5. Invariants verified

- `LIVE_SEALABLE_PROFILES` is still exactly `{"calendar_meeting"}`.
- `git diff` over `src/kai/{ir,runtime,approvals,planner,identity,tasks,google}`
  is **empty** — no semantic area was touched.
- `tests/test_identity_single_gate_lock.py` passes unchanged in substance. Three
  guards were updated *only* because the round legitimately adds a 12th
  migration and moves the head; the assertions' intent is intact and the reason
  is commented in place.
- Tenant isolation, approval boundaries, and execution authority are untouched.

## 6. Configuration required in production

Production now **refuses to start** unless: `JWT_SECRET` is non-default and 32+
characters, `GOOGLE_CREDS_ENCRYPTION_SECRET` is 32+ characters, and
`RATE_LIMIT_BACKEND=redis` with a `REDIS_URL`. `CORS_ALLOWED_ORIGINS` must not
contain `*`. This is deliberate — a control that looks enabled while doing
nothing is worse than a visible startup failure.

## 7. What is still open

- **No metrics or tracing export.** Structured JSON logging and `X-Request-ID`
  exist; there is no Prometheus endpoint and no alerting hook. This is the
  specific reason the verdict is not `PRODUCTION-READY`.
- **Rate limiting is per source address and ignores `X-Forwarded-For`.** Behind
  a proxy all clients share one address. Honouring the header without a trusted
  proxy would let any client mint a fresh rate-limit identity per request, so the
  safe default was chosen; deployments needing per-client limits should terminate
  them at a proxy.
- **No refresh tokens.** A client re-authenticates at expiry. Revocation makes
  expiry enforceable early but does not extend sessions.
- **Test-tree typing debt** unchanged: `mypy src tests` still reports 196
  findings, all in `tests/`, none in `src/`.
- **`gmail_send` remains non-live-sealable.** Recipient intent is unresolved and
  was deliberately not opened.
- **Purge is available but not scheduled.** `purge_expired_revocations` exists
  and is safe to call at any time; no job invokes it yet, so the table grows
  until one is wired up.

## 8. Note on a mid-round error

To prove the tests were not tautological I reverted the four production files
and re-ran them. I then restored the fixes by rewriting the files rather than
restoring from a snapshot, and the first rewrite of `jwt.py` and `deps.py` was
verified by re-running the full suite and diffing against intent before
continuing. The final state is verified by a clean-clone run, which is the
authoritative check. I mention it because restoring from memory rather than from
the recorded bytes is the one step in this round that was not checksum-verified.
