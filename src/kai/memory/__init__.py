"""Memory System: deterministic, auditable, tenant+user-scoped memory.

Memory answers "what does this user remember" for *context* only. It is not a
substitute for Policy, permissions, approvals, the Task Contract, or the
Identity Resolver, and its ``source``/weights never grant access.

Public surface mirrors the other KAI domains: pass an ``AsyncSession`` plus the
trusted ``tenant_id``/``user_id`` and call the service functions in
:mod:`kai.memory.service`.
"""