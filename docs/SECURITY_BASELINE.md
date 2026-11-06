# KAI — Security Baseline (Assessment)

**Purpose:** Baseline assessment and production gates. This is an assessment document, not a full security hardening project.

## Principles

- Local functionality is not production readiness.
- Passing tests does not by itself establish security.
- Defense-in-depth: tenant isolation, policy/approval gates, verification before completion, durable evidence.

## Assessment Areas

### Authentication
- Login/session/token lifecycle present (JWT-based). Review revocation coverage (jti revocation) and token rotation/expiry handling.

### Authorization
- Tenant isolation enforced at DB layer; policy service exists. Review object-level access in extension paths.

### Secrets Management
- Credentials encrypted for Google accounts (per codebase). No hardcoded secrets in source; verify .env handling and logs.

### API Security
- Input validation via Pydantic schemas. Rate limiting, CORS, TrustedHost middleware in place (per README). Error handling avoids leaking internals.

### Database Security
- Migrations via Alembic; transactions/constraints. Connection handling via SQLAlchemy 2 async. Review least-privilege DB user.

### Logging & Monitoring
- Structured logging (structlog). Metrics via prometheus-client (opt-in). Tracing configurable; defaults conservative.

### Backups, Recovery, Rollback
- Durable state in PostgreSQL. Migrations support forward/rollback in principle; test covers rollback path (migration test). Ensure backup/restore procedures documented operationally.

### Dependencies & Scanning
- Dependencies pinned in pyproject.toml. Consider automated dependency/security scanning in CI.

## Required Production Gates
1. Secrets not logged or committed.
2. Tenant isolation verified.
3. AuthZ enforced on all mutating endpoints (Core governed).
4. Rate limiting/revocation tested under load.
5. Migrations tested (upgrade+rollback) in staging.
6. Observability signals present and actionable.
7. Incident response/rollback plan defined.

## Notes
- Core path (ToolGateway) enforces policy/approval/identity/verification. Extension paths must be treated separately (see Canonical Architecture §4.2).
