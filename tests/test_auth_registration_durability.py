"""Registration must not depend on the row surviving a second round-trip.

``src/kai/api/auth.py`` used to call ``session.refresh(user)`` *after* the commit.
That is not free. The commit has already made the row durable, and the session
factory sets ``expire_on_commit=False``, so the refresh re-read a row the request
had just written — and opened a second transaction to do it. Anything that deleted
the row inside that window turned a successful registration into a 500::

    sqlalchemy.exc.InvalidRequestError: Could not refresh instance '<User ...>'

It was not hypothetical. The suite's own ``_clean_state`` fixture truncates those
tables between tests from a different pooled connection, so a full-suite run
produced that error in whichever test happened to land in the window — it showed up
in ``tests/test_gmail.py``, three files away from anything to do with auth, which is
what made it look like a session leak rather than a lost row.

A HTTP-level test cannot hit the window, because the response only exists once the
request has finished. So the interleaving is driven here instead: the session's
``commit`` deletes the row it just committed, from a second connection, before
control returns to the endpoint.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kai.api.auth import RegisterRequest, register
from kai.db.engine import async_session_factory, engine
from kai.db.models import User


class _RowVanishesAfterCommit(AsyncSession):
    """A session that deletes the row it just committed.

    This is the whole point of the test and it is deterministic: no sleeps, no
    timing assumptions, no retries. The old ``refresh(user)`` sat directly on the
    other side of this line and raised.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._users: list[User] = []

    def add(self, instance: object, _warn: bool = True):  # type: ignore[no-untyped-def]
        if isinstance(instance, User):
            self._users.append(instance)
        return super().add(instance, _warn=_warn)

    async def commit(self) -> None:
        await self.flush()  # populate server-side defaults before we need the id
        user_ids = [user.id for user in self._users]
        await super().commit()
        for user_id in user_ids:
            async with async_session_factory() as other:
                await other.execute(delete(User).where(User.id == user_id))
                await other.commit()


@pytest.fixture(autouse=True)
async def _dispose_pool() -> None:
    yield
    await engine.dispose()


async def test_registration_survives_the_row_being_deleted_after_its_commit() -> None:
    """A registration whose row vanishes must still be a registration."""
    email = f"{uuid.uuid4().hex}@makepoets.example"

    session = _RowVanishesAfterCommit(engine, expire_on_commit=False)
    try:
        request = RegisterRequest(email=email, password="Password123!")
        response = await register(request, session)

        # The account is genuinely gone, so this cannot be passing because the
        # delete silently did nothing.
        async with async_session_factory() as other:
            found = await other.execute(select(User).where(User.email == email))
            assert found.scalar_one_or_none() is None, "precondition: row deleted"

        # And the endpoint still produced a real token for that principal.
        assert response.access_token
        assert response.user_id
    finally:
        await session.rollback()
        await session.close()


def test_registration_does_not_re_read_its_own_write() -> None:
    """The structural half: no post-commit ``refresh`` on the row we just wrote.

    The behavioural test above pins the consequence. This pins the cause, so the
    redundant round-trip cannot be reintroduced under a different name. It is
    parsed rather than grepped so that the comment explaining the removal cannot
    satisfy it.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(register)))

    def lines_matching(name: str) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        ]

    commits = lines_matching("commit")
    assert commits, "the endpoint no longer commits the user it created"

    after_commit = [
        line for line in lines_matching("refresh") if line > commits[0]
    ]
    assert after_commit == [], (
        "a refresh after commit re-reads a row this request already made durable "
        "and opens a second transaction that can lose the row underneath it"
    )