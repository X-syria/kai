"""The injectable Google network boundary.

FastAPI resolves :func:`get_google_http_client` per request; tests override it
with an ``httpx.AsyncClient`` backed by ``httpx.MockTransport`` so no Google
traffic ever leaves the test process.
"""

from collections.abc import AsyncIterator

import httpx


async def get_google_http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        yield client