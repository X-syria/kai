---
name: kai-google
description: Manage KAI Google integration - OAuth, access_token, refresh_token, token_expires_at, credential persistence, and the Calendar, Gmail, and Contacts provider paths.
when_to_use: When changing Google OAuth, token lifecycle, credential storage or encryption, refresh behavior, 401 handling, or the Calendar, Gmail, or Contacts service clients.
user-invocable: true
---

# KAI Google Integration

Purpose: maintain a single, correct, refresh-aware credential authority for
every Google-backed capability.

## Shared base rule (binding on every KAI skill)

**Actual code is the primary source of truth.**

```
actual code
-> call graph
-> data flow
-> authority
-> state/lifecycle
-> invariants
-> tests
-> persistence
-> Git history when needed
-> architecture truth
-> documentation reconciliation
```

## Scope

Owns:
- Google OAuth authorization and callback
- `access_token`, `refresh_token`, `token_expires_at`
- credential persistence, encryption, and retrieval
- the Calendar, Gmail, and Contacts provider paths
- token refresh behavior and `401` handling

Does not own:
- Gateway execution ordering -> `kai-runtime`
- approval binding -> `kai-approval`
- generic authentication framework -> belongs to the auth domain, not here
- Gmail message licensing or body construction semantics

## Current priority issues

| ID | Title |
|---|---|
| `GOO-01` | Single refresh-aware Google token authority for all three services |
| `SEC-01` | Preserve the existing `refresh_token` on reconnect |

---

## GOO-01 - one token authority

**Problem.** The production provider path should use a single
refresh-aware Google token authority. Instead, Calendar, Gmail, and
Contacts each hold their own copy of token handling. Three copies drift:
expiry checks differ, refresh logic differs, `401` recovery differs, and a
token rotated by one path leaves the others holding stale credentials.

### Required investigation before changing anything

Establish each of the following from the actual implementation:

1. **Access token expiry** - where is `token_expires_at` compared against the
   current time? Is the comparison `>` or `>=`? Is a clock-skew margin
   applied? What happens when expiry is exactly at the boundary?
2. **Refresh token** - where is it stored, decrypted, and read? Is it present
   on every account that has an access token?
3. **Refresh call graph** - what does refresh actually call? Which HTTP client
   issues it? Which credentials and scopes does it use? What does it do on a
   refresh failure - retry, raise, or silently continue with an expired
   access token?
4. **Persistence** - is a rotated refresh token persisted? Is the new
   `token_expires_at` persisted? If refresh succeeds in memory but the write
   fails, what is the observable inconsistency?
5. **Concurrent refresh** - if two requests refresh the same credential at
   the same time, what happens? Is refresh idempotent? Is there a lock? Can a
   slower refresh overwrite a newer token with an older one?
6. **`401` behavior** - how is `401` detected, and what follows it? Is there a
   retry, a refresh, an error, or a silent failure? Does a `401` on one service
   affect the others' credentials?
7. **Production seams** - which code path does a real request actually take?
   Confirm the token authority on that path, not only on a test or helper
   path. A helper-level consolidation is not evidence that production
   consolidated.

### Direction of the fix

Establish the single authority first, then move the three provider paths onto
it. Do not refactor all three services at once. Each migration is a separate
step with its own verification, per `kai-engineering`.

---

## SEC-01 - preserve the refresh token on reconnect

**Problem.** Google does not always issue a new `refresh_token` when a user
reconnects an account that is already linked. The reconnect response may
return `refresh_token is None`.

If the reconnect handler treats the new response as a complete credential
replacement, the account is left with no refresh token. Once the current
access token expires, the integration is permanently broken.

### Required behavior

On reconnect, when the response contains no refresh token, **preserve the
existing stored refresh token**:

```python
if new_token.refresh_token is None:
    # Keep the existing refresh token; Google did not reissue one.
    new_token.refresh_token = existing_token.refresh_token
```

When the response **does** contain a new refresh token, use the new one - it
supersedes the old credential.

Confirm from the implementation, before and after the change:

- the existing refresh token is loaded before being overwritten
- the preservation branch is reached when, and only when, the response omits
  the refresh token
- the new value is not discarded by an unconditional overwrite
- the persisted record retains a usable refresh token after a reconnect with no
  refresh token
- the access token and `token_expires_at` from the new response are still
  persisted in both branches

---

## Investigation checklist for any Google change

- [ ] Scopes requested, and whether refresh preserves them
- [ ] Credential decryption path and where keys come from
- [ ] Tenant scoping of credential lookup - an account must never be
      resolvable across tenants
- [ ] Whether the service layer or the Gateway owns token freshness
- [ ] Error mapping for `401`, `403`, and revoked grants
- [ ] Whether a revoked grant is surfaced distinctly from a transient failure
- [ ] Test coverage at the production seam, per `kai-test-and-mutation`

## Prohibited

- Adding a generic authentication framework. There is no proven need for one
  here. If you believe one is required, establish the requirement with
  evidence first (see `kai-engineering`).
- Duplicating token handling into a fourth path.
- Overwriting a stored refresh token with `None`.
- Caching an access token past its expiry without a refresh.
- Changing Gmail licensing, scope handling for Gmail, or message
  construction as part of token work. That is a separate concern with a
  separate decision; do not expand scope into it.
- Consolidating only the helper layer and declaring GOO-01 resolved.
