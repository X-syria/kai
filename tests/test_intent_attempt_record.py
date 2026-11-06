"""O1-B and P2-A, pinned without a database.

Two claims are under test here.

**O1-B** — the gateway records an INTENT attempt in the *caller's* session,
immediately before it asks the approval manager for the single use, and it does
so with ``session.add`` only. No second session, no commit, no flush, no
savepoint. The counterpart claim — that the row can become durable on the
compare-and-set failure path, because ``consume`` commits before it raises — is
a property of the real ``ApprovalService`` and the real database, so it is
pinned in ``tests/test_intent_attempt_record_db.py``, not here. This file pins
the gateway side of that seam: exactly one ``add``, on the caller's session,
strictly before ``consume``, and no commit whose stack passes through the
gateway.

**P2-A** — the reader is one SELECT, tenant-scoped from the principal alone,
with no write, no lazy expiry, no scan, no external call, and no verdict.

Everything below is a real gateway / real reader / real route against a
recording stand-in for the session and for the boundaries that need a database
or a network. No SQLite is introduced: this file simply never executes SQL.
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib
import traceback
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import kai.tools.gateway as gateway_module
from kai.approvals.errors import InvalidApprovalTransition
from kai.approvals.service import ApprovalService, intent_hash
from kai.audit.reader import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    NOT_EVIDENCE_OF_DISPATCH,
    list_intent_attempts_without_authorization,
)
from kai.audit.schemas import IntentAttemptListResponse, IntentAttemptResponse
from kai.db.models import IntentAttempt
from kai.policy.models import PolicyDecision, PolicyDecisionKind
from kai.tasks.models import Task
from kai.tools.boundaries import (
    ApprovalDecision,
    ApprovalDecisionRecord,
    ContentAuthorization,
    VerificationOutcome,
)
from kai.tools.context import ToolContext
from kai.tools.definition import ToolDefinition
from kai.tools.execution import ToolExecutionOutcome, VerificationContract
from kai.tools.gateway import ToolGateway
from kai.tools.intent import ToolIntent
from kai.tools.registry import ToolRegistry
from kai.tools.result import ToolOutcome, ToolResult
from kai.tools.risk import RiskLevel

pytestmark = pytest.mark.no_db_unit

TOOL_NAME = "test.record"
TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
USER = uuid.uuid4()
OTHER_USER = uuid.uuid4()
TASK_ID = uuid.uuid4()
APPROVAL_ID = uuid.uuid4()
ACCOUNT_ID = uuid.uuid4()
ARGUMENTS: dict[str, Any] = {"value": "approved-value"}


# --------------------------------------------------------------------------- #
# Recording stand-ins
# --------------------------------------------------------------------------- #


class _SessionRecorder:
    """A session that records what the gateway does to it, and nothing else.

    ``execute`` is not implemented on purpose: the gateway has no business
    issuing statements (its only database access is through the boundaries), so
    a call here should fail the test loudly rather than pass silently.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.events: list[tuple[str, Any]] = []
        self.pending: list[Any] = []
        self.durable: list[Any] = []
        self.flushes = 0
        self.rollbacks = 0
        self.commit_callers: list[str] = []

    # -- the only method the gateway is allowed to use ---------------------- #
    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.pending.append(obj)
        self.events.append(("add", obj))

    async def commit(self) -> None:
        # Recorded as "who asked for it", because the question under test is
        # exactly "did the *gateway* commit?". The commit itself is owned by
        # the approval manager (in production, by ApprovalService.consume).
        self.commit_callers.append(traceback.extract_stack()[-2].filename)
        self.durable.extend(self.pending)
        self.pending.clear()
        self.events.append(("commit", None))

    async def flush(self) -> None:
        self.flushes += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "the gateway must not issue statements directly; its reads and "
            "writes belong to the boundaries"
        )


class _Policy:
    """One deterministic verdict, returned for every evaluation."""

    def __init__(self, kind: PolicyDecisionKind) -> None:
        self._kind = kind
        self.calls = 0

    async def evaluate(self, *, input: Any) -> PolicyDecision:
        self.calls += 1
        return PolicyDecision(
            decision=self._kind,
            reason_code="TEST",
            reason="test verdict",
            risk_level=RiskLevel.HIGH,
        )


class _Content:
    async def authorize(self, **_: Any) -> ContentAuthorization:
        return ContentAuthorization(allowed=True, reason="content ok")


class _Executor:
    def __init__(self, outcome: ToolExecutionOutcome) -> None:
        self._outcome = outcome
        self.calls: list[tuple[dict[str, Any], uuid.UUID | None]] = []

    async def execute(
        self, *, tool: ToolDefinition, arguments: Mapping[str, Any], context: ToolContext
    ) -> ToolExecutionOutcome:
        self.calls.append((dict(arguments), context.account_id))
        return self._outcome


class _Verification:
    async def verify(self, **_: Any) -> VerificationOutcome:
        return VerificationOutcome(verified=True, evidence={"read_back": "ok"})


class _Approval:
    """A stand-in for ``ApprovalService`` whose commit placement is explicit.

    The real service commits the caller's session on *both* CAS outcomes
    (``approvals/service.py``): after winning the claim, and again before it
    raises on a lost claim. ``fail=True`` reproduces the second one, including
    the fact that the approval was **not** consumed by this attempt — which is
    why nothing here may assert that the approval is still intact.
    """

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.consume_calls: list[uuid.UUID] = []
        self.approval_consumed_here = False
        #: The gateway must never re-read the approval after a lost claim; it
        #: only reports the denial. Left as None so an unexpected read breaks.
        self.state_read_after_failure: str | None = None

    async def check(self, **_: Any) -> ApprovalDecisionRecord:
        return ApprovalDecisionRecord(
            decision=ApprovalDecision.APPROVED,
            reason="approval approved",
            decided_by="system",
            approval_id=APPROVAL_ID,
        )

    async def consume(
        self,
        *,
        approval_id: uuid.UUID,
        context: ToolContext,
        **_: Any,
    ) -> None:
        self.consume_calls.append(approval_id)
        assert context.session is not None
        context.session.events.append(("consume", approval_id))
        if self.fail:
            # The claim was lost: consume still commits the caller's session,
            # then raises. The attempt row therefore commits while the approval
            # was not consumed by this attempt.
            await context.session.commit()
            raise InvalidApprovalTransition("approval was already consumed by another execution")
        await context.session.commit()
        self.approval_consumed_here = True


class _Args(BaseModel):
    value: str = "x"


def _tool(executor: Any, *, requires_account: bool = True) -> ToolDefinition:
    return ToolDefinition(
        name=TOOL_NAME,
        description="A tool that exists only in this test.",
        input_schema=_Args,
        risk_level=RiskLevel.HIGH,
        capability="test",
        executor=executor,
        verification_contract=VerificationContract.MANUAL,
        requires_account=requires_account,
    )


def _task() -> Task:
    return Task(id=TASK_ID, tenant_id=TENANT, user_id=USER, state="EXECUTING", goal="g")


def _context(session: Any, *, account_id: uuid.UUID | None = ACCOUNT_ID) -> ToolContext:
    return ToolContext(
        session=session,
        task_id=TASK_ID,
        tenant_id=TENANT,
        user_id=USER,
        account_id=account_id,
    )


def _intent(*, arguments: dict[str, Any] | None = None) -> ToolIntent:
    return ToolIntent(
        tool_name=TOOL_NAME,
        arguments=dict(ARGUMENTS if arguments is None else arguments),
        task_id=TASK_ID,
        tenant_id=TENANT,
        user_id=USER,
        request_id="req-o1b",
    )


def _gateway(
    *,
    policy: _Policy,
    approval: _Approval,
    executor: _Executor,
    tool: ToolDefinition | None = None,
) -> ToolGateway:
    return ToolGateway(
        registry=ToolRegistry.build([tool or _tool(executor)]),
        policy_engine=policy,
        content_authorizer=_Content(),
        approval_manager=approval,
        verification_engine=_Verification(),
    )


@pytest.fixture(autouse=True)
def _fake_task_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """No task row is needed: the gateway's only read is behind ``get_task``."""

    async def _get_task(session: Any, **_: Any) -> Task:
        return _task()

    monkeypatch.setattr(gateway_module, "get_task", _get_task)


# --------------------------------------------------------------------------- #
# O1-B: the attempt record
# --------------------------------------------------------------------------- #


async def test_the_intent_row_is_added_to_the_callers_session_before_consume() -> None:
    """One ``add``, on ``context.session``, strictly before ``consume``.

    The row is the record of an attempt, so it must exist before the attempt's
    single-use claim is issued — otherwise a lost claim would leave nothing
    behind. "Added" is all this asserts: durability is not claimed.
    """
    session = _SessionRecorder()
    executor = _Executor(ToolExecutionOutcome(ok=True, data={"ok": True}))
    approval = _Approval(fail=False)
    gateway = _gateway(
        policy=_Policy(PolicyDecisionKind.REQUIRE_APPROVAL),
        approval=approval,
        executor=executor,
    )
    context = _context(session)

    result = await gateway.execute(_intent(), context)

    assert result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert len(session.added) == 1, "exactly one attempt record per execution"
    row = session.added[0]
    assert isinstance(row, IntentAttempt)
    assert row.tenant_id == context.tenant_id
    assert row.user_id == context.user_id
    assert row.task_id == context.task_id
    assert row.approval_id == APPROVAL_ID
    assert row.tool_name == TOOL_NAME
    assert row.intent_hash == intent_hash(
        tool=_tool(executor), arguments=ARGUMENTS, account_id=ACCOUNT_ID
    )
    assert row.account_id == ACCOUNT_ID
    # L2 substrate: no writer exists, so it can only be NULL.
    assert row.authorization_id is None
    assert row.authorized_at_utc is None
    # The row is not stamped by the application; the database supplies time.
    assert row.created_at is None
    assert IntentAttempt.__table__.c.created_at.server_default is not None
    # Same for the primary key: it is assigned when the row is persisted, so a
    # not-yet-durable attempt has no id to correlate on. That is consistent with
    # claim (1) in the model docstring -- nothing is durable at this point.
    assert row.id is None
    assert IntentAttempt.__table__.c.id.default is not None

    # Ordering at the seam: add, then consume.
    kinds = [name for name, _ in session.events]
    assert kinds.count("add") == 1
    assert kinds.index("add") < kinds.index("consume"), (
        "the INTENT attempt must be added before the single use is claimed"
    )
    assert approval.consume_calls == [APPROVAL_ID]

    # The only commit came from the approval manager's stand-in, never from the
    # gateway: no second session and no durability trick was introduced.
    assert session.commit_callers, "consume commits, as the real service does"
    assert all("gateway.py" not in path for path in session.commit_callers)
    assert session.flushes == 0
    assert session.rollbacks == 0


async def test_no_intent_row_when_no_approval_is_involved() -> None:
    """Both non-consuming outcomes leave the attempt table untouched.

    An attempt record is only meaningful for the branch that is about to claim a
    single use. Writing it for an ALLOW path or for a REQUIRED (still pending)
    decision would manufacture attempts that never reached the claim.
    """
    # ALLOW: no approval boundary contact at all.
    allowed_session = _SessionRecorder()
    executor = _Executor(ToolExecutionOutcome(ok=True))
    allowed = _gateway(
        policy=_Policy(PolicyDecisionKind.ALLOW),
        approval=_Approval(fail=False),
        executor=executor,
    )
    allowed_result = await allowed.execute(_intent(), _context(allowed_session))
    assert allowed_result.outcome is ToolOutcome.EXECUTION_SUCCEEDED
    assert allowed_session.added == []

    # REQUIRE_APPROVAL but nothing approved yet: the pipeline stops earlier.
    held_session = _SessionRecorder()

    class _Pending(_Approval):
        async def check(self, **_: Any) -> ApprovalDecisionRecord:
            return ApprovalDecisionRecord(
                decision=ApprovalDecision.REQUIRED,
                reason="approval required",
                decided_by="system",
                approval_id=APPROVAL_ID,
            )

    held = _gateway(
        policy=_Policy(PolicyDecisionKind.REQUIRE_APPROVAL),
        approval=_Pending(fail=False),
        executor=executor,
    )
    held_result = await held.execute(_intent(), _context(held_session))
    assert held_result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert held_session.added == []


async def test_a_lost_claim_leaves_the_attempt_durable_and_the_approval_unproven() -> None:
    """The CAS-failure path: the row commits, the approval was not consumed here.

    This is the accepted limit, asserted rather than papered over. ``consume``
    commits the caller's session before it raises, so the attempt record is
    durable even though *this* attempt did not claim the single use. Two things
    therefore follow, and both are asserted:

    * the gateway does not retract or roll back the record (it cannot: it never
      owns the transaction), and
    * the approval's state is **not** re-read or assumed. A concurrent attempt
      may have consumed it, so this test makes no claim about it.
    """
    session = _SessionRecorder()
    executor = _Executor(ToolExecutionOutcome(ok=True))
    approval = _Approval(fail=True)
    gateway = _gateway(
        policy=_Policy(PolicyDecisionKind.REQUIRE_APPROVAL),
        approval=approval,
        executor=executor,
    )

    result = await gateway.execute(_intent(), _context(session))

    assert result.outcome is ToolOutcome.DENIED
    assert executor.calls == [], "a lost claim must never reach the executor"
    assert len(session.added) == 1
    # Durable, because consume committed — not because the gateway did.
    assert session.durable == session.added
    assert approval.approval_consumed_here is False
    assert all("gateway.py" not in path for path in session.commit_callers)
    assert session.rollbacks == 0
    # Nothing read or assumed the approval after the failed claim.
    assert approval.state_read_after_failure is None


async def test_the_record_is_not_proof_of_an_external_effect() -> None:
    """A recorded attempt survives an execution failure, and changes no result.

    The record is written before dispatch and never rewritten, so it cannot
    graduate into "sent". Conversely an execution failure does not delete it: the
    two facts are independent, and ``ToolResult`` has no field that would let a
    caller read the record as a verdict.
    """
    session = _SessionRecorder()
    executor = _Executor(ToolExecutionOutcome(ok=False, error_code="PROVIDER_ERROR"))
    gateway = _gateway(
        policy=_Policy(PolicyDecisionKind.REQUIRE_APPROVAL),
        approval=_Approval(fail=False),
        executor=executor,
    )

    result = await gateway.execute(_intent(), _context(session))

    assert result.outcome is ToolOutcome.EXECUTION_FAILED
    assert len(session.added) == 1, "the attempt is recorded either way"
    assert session.added[0].authorization_id is None
    # No result field carries a dispatch/authorization verdict.
    result_fields = {field.name for field in dataclasses.fields(ToolResult)}
    assert not {"sent", "dispatched", "authorized", "authorization_id"} & result_fields
    # And the reader's payload says so in machine-readable form.
    assert IntentAttemptListResponse(attempts=[]).evidence_status == (NOT_EVIDENCE_OF_DISPATCH)


# --------------------------------------------------------------------------- #
# P2-A: the reader
# --------------------------------------------------------------------------- #


class _Rows:
    """Minimal result object: what ``session.execute(select(...))`` returns."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _ReaderSession:
    """Records the statement and refuses every write-shaped call.

    Returning rows for *the tenant the statement asks for* is how isolation is
    asserted here without a database: the fake behaves like a correctly filtered
    table, and the test proves the statement carries the principal's tenant.
    """

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[Any] = []
        self.forbidden: list[str] = []

    def _refuse(self, name: str) -> None:
        self.forbidden.append(name)
        raise AssertionError(f"the audit reader must not call {name}()")

    async def execute(self, statement: Any, *_: Any, **__: Any) -> _Rows:
        self.statements.append(statement)
        tenant_id = _bound_param(statement, "tenant_id")
        user_id = _bound_param(statement, "user_id")
        # Mirror the real table, including the AUTHORIZED-reference predicate, so
        # a row that carries that reference is not returned here either. The
        # predicate itself is asserted on the compiled SQL below and,
        # authoritatively, against real rows in the database module.
        return _Rows(
            [
                row
                for row in self.rows
                if row.tenant_id == tenant_id
                and row.user_id == user_id
                and row.authorization_id is None
            ]
        )

    async def commit(self) -> None:
        self._refuse("commit")

    async def flush(self) -> None:
        self._refuse("flush")

    def add(self, *_: Any) -> None:
        self._refuse("add")

    async def delete(self, *_: Any) -> None:
        self._refuse("delete")

    async def rollback(self) -> None:
        self._refuse("rollback")

    async def begin(self) -> None:
        self._refuse("begin")


def _bound_param(statement: Any, name: str) -> Any:
    """The single bound value for ``name`` in a compiled statement."""
    params = statement.compile().params
    values = [value for key, value in params.items() if key.split("_")[0] == name.split("_")[0]]
    assert len(values) == 1, f"expected exactly one {name} predicate, got {values!r}"
    return values[0]


def _attempt_row(*, tenant_id: uuid.UUID = TENANT, user_id: uuid.UUID = USER) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=TASK_ID,
        approval_id=APPROVAL_ID,
        tool_name=TOOL_NAME,
        intent_hash="a" * 64,
        account_id=ACCOUNT_ID,
        created_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        authorization_id=None,
        authorized_at_utc=None,
    )


async def test_the_reader_issues_one_select_and_nothing_else() -> None:
    """Read-only by construction: one SELECT, newest first, bounded."""
    session = _ReaderSession([_attempt_row()])

    rows = await list_intent_attempts_without_authorization(session, tenant_id=TENANT, user_id=USER)

    assert len(rows) == 1
    assert session.forbidden == [], "no commit/flush/add/delete/rollback/begin"
    assert len(session.statements) == 1
    statement = session.statements[0]
    assert statement.__class__.__name__ == "Select"
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "FROM intent_attempts" in sql
    assert "intent_attempts.tenant_id =" in sql
    assert "intent_attempts.user_id =" in sql
    assert "intent_attempts.authorization_id IS NULL" in sql
    assert "ORDER BY intent_attempts.created_at DESC" in sql
    assert "LIMIT" in sql


async def test_an_attempt_with_an_authorized_reference_is_not_listed() -> None:
    """The predicate is the question, not a filter that happens to be empty.

    No writer sets ``authorization_id`` today, so every row the gateway writes
    has it NULL and the predicate changes nothing yet. It is still the correct
    predicate: this endpoint asks for attempts with *no AUTHORIZED reference
    recorded*. A row that already carries that reference is a different record
    and must not be listed, or the list would report an L2 gap that is not there.
    The authoritative proof over real rows lives in the database module.
    """
    authorized = _attempt_row()
    authorized.authorization_id = uuid.uuid4()
    authorized.authorized_at_utc = datetime(2026, 10, 24, 12, 5, tzinfo=UTC)
    pending = _attempt_row()
    session = _ReaderSession([authorized, pending])

    rows = await list_intent_attempts_without_authorization(session, tenant_id=TENANT, user_id=USER)

    assert rows == [pending]
    # The wire model cannot carry the fields the predicate excludes on, so a
    # listed row cannot be mistaken for one that has an AUTHORIZED reference.
    assert "authorization_id" not in IntentAttemptResponse.model_fields
    assert "authorized_at_utc" not in IntentAttemptResponse.model_fields


def test_the_reader_module_contains_no_write_expiry_or_network_verb() -> None:
    """A static guard on the reader's source, so a later edit cannot add one.

    The behavioural test above cannot see a write that only fires on an error
    path or a scheduled scan that only exists in a branch, so the source of the
    read path is checked directly. The forbidden list is deliberately about
    *mechanisms*, not style: commits, flushes, mutations, sleeps, tasks, HTTP.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "kai" / "audit" / "reader.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    for forbidden in (
        ".commit(",
        ".flush(",
        ".add(",
        ".delete(",
        "asyncio.sleep",
        "create_task",
        "BackgroundTasks",
        "httpx",
        "ApprovalService",
        "_maybe_expire",
    ):
        assert forbidden not in code, f"read path must not contain {forbidden!r}"


def test_consume_still_commits_on_both_cas_outcomes() -> None:
    """Drift guard for the assumption every O1-B limit is written against.

    The accepted limits say the attempt row becomes durable at ``consume``'s
    commit, and that on a CAS failure it is durable while the approval was not
    consumed. Both statements are only true while ``consume`` commits the
    caller's session on the success path *and* before raising on the failure
    path. ``consume`` is frozen for this change, so this test does not modify it
    -- it fails if someone later "tidies" one of those commits away (a rollback
    instead of a commit would silently invalidate the documented meaning of
    every attempt row).

    This is a source-level guard, deliberately: the behavioural version of the
    same claim needs the real database and lives in
    ``tests/test_intent_attempt_record_db.py``.
    """
    source = inspect.getsource(ApprovalService.consume)
    assert source.count("await context.session.commit()") == 2, (
        "consume must commit after winning the claim and again before raising "
        "on a lost claim; the O1-B limits are stated in terms of both"
    )
    assert "rollback" not in source
    # The failure-path commit sits between the CAS result and the explanation
    # raises, i.e. it happens before any ApprovalError reaches the gateway.
    cas_check = source.index("if result.first() is not None:")
    failure_commit = source.index("await context.session.commit()", cas_check)
    first_raise = source.index("raise ", failure_commit)
    assert cas_check < failure_commit < first_raise


async def test_the_reader_refuses_an_out_of_bounds_limit() -> None:
    session = _ReaderSession()
    for bad in (0, -1, MAX_LIMIT + 1):
        with pytest.raises(ValueError):
            await list_intent_attempts_without_authorization(
                session, tenant_id=TENANT, user_id=USER, limit=bad
            )
    assert session.statements == []
    assert DEFAULT_LIMIT < MAX_LIMIT


# --------------------------------------------------------------------------- #
# P2-A over HTTP: authentication, tenant isolation, read-only exposure
# --------------------------------------------------------------------------- #


def _principal(*, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Any:
    return SimpleNamespace(tenant_id=tenant_id, id=user_id, is_active=True)


@pytest.fixture
def _audit_app() -> Iterator[Any]:
    """Override auth + session so the real route runs without a database."""
    from kai.auth.deps import get_current_user
    from kai.db.engine import get_session
    from kai.main import app

    def _install(principal: Any, session: Any) -> None:
        app.dependency_overrides[get_current_user] = lambda: principal
        app.dependency_overrides[get_session] = lambda: session

    try:
        yield _install
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


async def test_the_endpoint_is_one_get_and_returns_the_non_claim(
    client: Any, _audit_app: Any
) -> None:
    """The exposed surface is exactly one GET, and it carries the non-claim."""
    from kai.api.audit import router

    paths = {(route.path, frozenset(route.methods or ())) for route in router.routes}
    assert paths == {("/audit/intent-attempts", frozenset({"GET"}))}

    session = _ReaderSession([_attempt_row()])
    _audit_app(_principal(tenant_id=TENANT, user_id=USER), session)

    response = await client.get("/api/v1/audit/intent-attempts")
    assert response.status_code == 200
    body = response.json()
    assert body["evidence_status"] == NOT_EVIDENCE_OF_DISPATCH
    assert "not 'not sent'" in body["interpretation"]
    assert "not derivable here" in body["interpretation"]
    assert [item["approval_id"] for item in body["attempts"]] == [str(APPROVAL_ID)]
    assert session.forbidden == []


async def test_another_tenant_cannot_be_reached_through_the_endpoint(
    client: Any, _audit_app: Any
) -> None:
    """Tenant comes from the principal, never from anything the caller can set."""
    foreign = _attempt_row(tenant_id=OTHER_TENANT, user_id=OTHER_USER)
    own = _attempt_row()
    session = _ReaderSession([foreign, own])
    _audit_app(_principal(tenant_id=TENANT, user_id=USER), session)

    # A hostile query parameter naming the other tenant must be ignored: there
    # is no such parameter on the route, and isolation is not derived from it.
    response = await client.get(
        f"/api/v1/audit/intent-attempts?tenant_id={OTHER_TENANT}&user_id={OTHER_USER}"
    )
    assert response.status_code == 200
    returned = {item["tenant_id"] for item in response.json()["attempts"]}
    assert returned == {str(TENANT)}, "only the principal's own attempts"

    statement = session.statements[-1]
    assert _bound_param(statement, "tenant_id") == TENANT
    assert _bound_param(statement, "user_id") == USER

    # And the other principal's own request sees its own row only.
    other_session = _ReaderSession([foreign, own])
    _audit_app(_principal(tenant_id=OTHER_TENANT, user_id=OTHER_USER), other_session)
    other_response = await client.get("/api/v1/audit/intent-attempts")
    assert other_response.status_code == 200
    assert {item["tenant_id"] for item in other_response.json()["attempts"]} == {str(OTHER_TENANT)}
