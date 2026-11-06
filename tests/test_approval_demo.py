from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from playwright.async_api import Route, async_playwright

from kai.config import get_settings
from kai.main import create_app


@contextlib.contextmanager
def _demo_ui_enabled() -> Iterator[None]:
    """Mount the demo page for one test, then restore the cached settings.

    ``/demo`` is opt-in (``DEMO_UI_ENABLED``), so these UI tests must build an
    app that was explicitly authorized to serve it rather than relying on the
    shared default app. The cache is cleared on both sides so the flag cannot
    leak into another test.
    """
    previous = os.environ.get("DEMO_UI_ENABLED")
    os.environ["DEMO_UI_ENABLED"] = "true"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DEMO_UI_ENABLED", None)
        else:
            os.environ["DEMO_UI_ENABLED"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An ASGI client over an app that explicitly enabled the demo page."""
    with _demo_ui_enabled():
        app = create_app()
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c


@pytest.mark.no_db_unit
async def test_approval_demo_serves_existing_api_workflow_without_persistent_tokens(
    client: AsyncClient,
) -> None:
    response = await client.get("/demo")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    page = response.text

    for expected in (
        "id=\"loginForm\"",
        "id=\"taskForm\"",
        "id=\"prepareButton\"",
        "id=\"approvalSummary\"",
        "موافق",
        "رفض",
        "متابعة التنفيذ صراحةً",
        "REQUIRES_ATTENTION",
        'item.external_outcome === "UNKNOWN"',
        'item.tool === "calendar.create_event"',
        "المهمة تحتاج إلى مراجعة. راجع التفاصيل قبل المتابعة.",
        "لا توفر Google mock",
        'request("/auth/login"',
        'request("/runtime/tasks"',
        "`/api/v1${path}`",
        "/runtime/tasks/${encodeURIComponent(taskId)}/trigger",
        "/tasks/${encodeURIComponent(taskId)}/plan",
        "/approvals/${encodeURIComponent(ids[0])}",
        "/approvals/${encodeURIComponent(activeApproval.id)}/approve",
        "/approvals/${encodeURIComponent(activeApproval.id)}/reject",
        'request("/auth/logout", { method: "POST" })',
        "finally {\n        clearSession();\n      }",
    ):
        assert expected in page

    assert 'snapshot?.task_state === "REQUIRES_ATTENTION"' in page
    assert (
        "snapshot.task_state === \"REQUIRES_ATTENTION\"\n        || snapshot.evidence"
        not in page
    )
    assert '(snapshot.last_error || "").includes("INDETERMINATE")' not in page
    assert "INDETERMINATE" not in page
    assert "نتيجة غير محسومة. قد يكون الحدث أُنشئ وقد لا يكون" in page
    fetch_url = page.split("fetch(", 1)[1].split(",", 1)[0]
    assert "accessToken" not in fetch_url
    assert 'headers["Authorization"]' in page
    assert "Bearer" in page
    assert "localStorage" not in page
    assert "sessionStorage" not in page
    assert "URLSearchParams" not in page
    assert "console." not in page
    assert "/execute" not in page
    assert "calendar.create_event(" not in page
    assert "google mock" in page.lower()


@pytest.mark.no_db_unit
async def test_approval_demo_browser_workflow_is_fully_intercepted(
    client: AsyncClient,
) -> None:
    page_response = await client.get("/demo")
    assert page_response.status_code == 200
    html = page_response.text

    origin = "http://kai.test"
    task_id = "11111111-1111-4111-8111-111111111111"
    approval_id = "22222222-2222-4222-8222-222222222222"
    tenant_id = "33333333-3333-4333-8333-333333333333"
    user_id = "44444444-4444-4444-8444-444444444444"
    tokens = ["fake-demo-token-one", "fake-demo-token-two"]
    requests: list[tuple[str, str, dict[str, str], str]] = []
    hosts: set[str] = set()
    unexpected: list[str] = []
    trigger_count = 0
    approval_status = "PENDING"
    snapshot_mode = "waiting"
    login_count = 0
    logout_count = 0

    plan = {
        "plan_id": "55555555-5555-4555-8555-555555555555",
        "task_id": task_id,
        "tenant_id": tenant_id,
        "version": 1,
        "status": "ACCEPTED",
        "model_id": "mock-planner",
        "created_at": "2026-01-01T00:00:00Z",
        "plan": {
            "goal": "Schedule a meeting",
            "success_criteria": ["Meeting scheduled"],
            "steps": [{
                "step_id": "step-1",
                "order": 1,
                "action": "create_calendar_event",
                "purpose": "Create the approved meeting",
                "inputs": [],
                "expected_output": "Calendar event",
                "verification_hint": "Event exists",
            }],
            "branches": [],
            "assumptions": [],
            "unresolved_questions": [],
        },
    }
    approval = {
        "id": approval_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "task_id": task_id,
        "tool_name": "calendar.create_event",
        "risk_level": "MEDIUM",
        "status": "PENDING",
        "recipient_class": "known",
        "intent_hash": "a" * 64,
        "request_summary": "Create the approved calendar event",
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
        "decided_at": None,
        "decided_by": None,
        "decision_reason": None,
        "consumed_at": None,
    }

    def runtime_snapshot() -> dict[str, object]:
        if snapshot_mode == "waiting":
            state, next_action, approval_ids, evidence = (
                "WAITING_APPROVAL", "APPROVE", [approval_id], []
            )
        elif snapshot_mode == "approved":
            state, next_action, approval_ids, evidence = (
                "EXECUTING", "EXECUTE", [], []
            )
        elif snapshot_mode == "review":
            state, next_action, approval_ids, evidence = (
                "REQUIRES_ATTENTION", "DONE", [], []
            )
        else:
            state, next_action, approval_ids = "REQUIRES_ATTENTION", "DONE", []
            evidence = [{
                "step_order": 1,
                "tool": "calendar.create_event",
                "outcome": "EXECUTION_FAILED",
                "ok": False,
                "verified": False,
                "error_code": "PROVIDER_RESPONSE_LOST",
                "request_id": "fake-request-id",
                "ir_fingerprint": "b" * 64,
                "external_outcome": "UNKNOWN",
            }]
        return {
            "task_id": task_id,
            "task_state": state,
            "goal": "Schedule a meeting",
            "timezone": "Asia/Riyadh",
            "scenario": None,
            "identities": [],
            "evidence": evidence,
            "cursor": 0,
            "steps_used": trigger_count,
            "next_action": next_action,
            "approval_ids": approval_ids,
            "last_error": None,
        }

    async def fulfill_json(route: Route, payload: object, status: int = 200) -> None:
        await route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    async def intercept(route: Route) -> None:
        nonlocal approval_status, login_count, logout_count, snapshot_mode, trigger_count
        request = route.request
        parsed = urlsplit(request.url)
        hosts.add(parsed.netloc)
        requests.append((
            parsed.path,
            request.method,
            dict(request.headers),
            request.url,
        ))

        if parsed.netloc != "kai.test":
            unexpected.append(request.url)
            await route.abort()
            return
        if parsed.path == "/demo" and request.method == "GET":
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=html,
            )
        elif parsed.path == "/favicon.ico":
            await route.fulfill(status=204)
        elif parsed.path == "/api/v1/auth/login" and request.method == "POST":
            login_count += 1
            await fulfill_json(route, {
                "access_token": tokens[login_count - 1],
                "token_type": "bearer",
                "user_id": user_id,
            })
        elif parsed.path == "/api/v1/runtime/tasks" and request.method == "POST":
            await fulfill_json(route, {
                "task_id": task_id,
                "task_state": "CREATED",
                "goal": "Schedule a meeting",
            }, status=201)
        elif (
            parsed.path == f"/api/v1/runtime/tasks/{task_id}/trigger"
            and request.method == "POST"
        ):
            trigger_count += 1
            if trigger_count == 1:
                snapshot_mode = "waiting"
                await fulfill_json(route, {
                    "task_id": task_id,
                    "task_state": "WAITING_APPROVAL",
                    "outcome": "WAITING_APPROVAL",
                    "message": "Human approval required",
                    "approval_ids": [approval_id],
                    "steps_used": 0,
                })
            elif trigger_count == 2:
                snapshot_mode = "review"
                await fulfill_json(route, {
                    "task_id": task_id,
                    "task_state": "REQUIRES_ATTENTION",
                    "outcome": "REQUIRES_ATTENTION",
                    "message": "Human review required",
                    "approval_ids": [],
                    "steps_used": 1,
                })
            else:
                unexpected.append(request.url)
                await route.abort()
        elif (
            parsed.path == f"/api/v1/runtime/tasks/{task_id}"
            and request.method == "GET"
        ):
            await fulfill_json(route, runtime_snapshot())
        elif (
            parsed.path == f"/api/v1/approvals/{approval_id}"
            and request.method == "GET"
        ):
            await fulfill_json(route, {**approval, "status": approval_status})
        elif (
            parsed.path == f"/api/v1/approvals/{approval_id}/approve"
            and request.method == "POST"
        ):
            approval_status = "APPROVED"
            snapshot_mode = "approved"
            await fulfill_json(route, {**approval, "status": approval_status})
        elif (
            parsed.path == f"/api/v1/approvals/{approval_id}/reject"
            and request.method == "POST"
        ):
            approval_status = "DENIED"
            await fulfill_json(route, {**approval, "status": approval_status})
        elif (
            parsed.path == f"/api/v1/tasks/{task_id}/plan"
            and request.method == "GET"
        ):
            await fulfill_json(route, plan)
        elif parsed.path == "/api/v1/auth/logout" and request.method == "POST":
            logout_count += 1
            if logout_count == 1:
                await fulfill_json(route, {"revoked": True})
            else:
                await fulfill_json(route, {"detail": "simulated logout failure"}, status=503)
        else:
            unexpected.append(request.url)
            await route.abort()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", intercept)
            page = await context.new_page()
            await page.goto(f"{origin}/demo", wait_until="domcontentloaded")

            await page.locator("#email").fill("demo@example.test")
            await page.locator("#password").fill("fake-password-only")
            await page.locator("#loginForm button").click()
            await page.locator("#goal").fill("Schedule a meeting")
            await page.locator("#taskForm button").click()
            await page.locator("#prepareButton").click()
            await page.wait_for_function(
                "() => document.getElementById('approvalStatus').textContent.includes('PENDING')"
            )

            triggers_before_approval = trigger_count
            await page.locator("#approveButton").click()
            await page.wait_for_function(
                "() => document.getElementById('message').textContent"
                ".includes('سُجّلت الموافقة')"
            )
            assert approval_status == "APPROVED"
            assert trigger_count == triggers_before_approval == 1

            await page.locator("#continueButton").click()
            await page.wait_for_function(
                "() => !document.getElementById('outcomeNotice').hidden"
            )
            assert trigger_count == 2
            review_notice = await page.locator("#outcomeNotice").inner_text()
            assert "المهمة تحتاج إلى مراجعة" in review_notice
            assert "قد يكون الحدث أُنشئ" not in review_notice
            assert "اكتملت المهمة" not in await page.locator("#taskState").inner_text()

            trigger_count_before_refresh = trigger_count
            snapshot_mode = "unknown"
            await page.locator("#refreshButton").click()
            await page.wait_for_function(
                "() => document.getElementById('outcomeNotice').textContent"
                ".includes('النتيجة غير محسومة')"
            )
            assert trigger_count == trigger_count_before_refresh
            ambiguous_notice = await page.locator("#outcomeNotice").inner_text()
            assert "تحقّق من التقويم" in ambiguous_notice

            first_token = tokens[0]
            await page.locator("#logoutButton").click()
            await page.locator("#loginSection").wait_for(state="visible")
            success_message = await page.locator("#message").inner_text()
            assert (
                "تم تسجيل الخروج وإبطال الرمز على الخادم."
                in success_message
            )
            first_logout = next(
                item
                for item in requests
                if item[0] == "/api/v1/auth/logout" and item[1] == "POST"
            )
            assert first_logout[2]["authorization"] == f"Bearer {first_token}"
            logout_requests_before_replay = logout_count
            await page.locator("#logoutButton").evaluate("(button) => button.click()")
            await page.wait_for_function(
                "() => document.getElementById('error').textContent"
                ".includes('سجّل الدخول أولًا')"
            )
            assert logout_count == logout_requests_before_replay

            await page.locator("#email").fill("demo@example.test")
            await page.locator("#password").fill("fake-password-only")
            await page.locator("#loginForm button").click()
            await page.locator("#workspace").wait_for(state="visible")
            second_token = tokens[1]
            await page.locator("#logoutButton").click()
            await page.locator("#loginSection").wait_for(state="visible")
            error_text = await page.locator("#error").inner_text()
            assert "تعذّر تأكيد إبطاله على الخادم" in error_text
            assert (
                "تم تسجيل الخروج وإبطال الرمز على الخادم"
                not in error_text
            )
            second_logout = [
                item
                for item in requests
                if item[0] == "/api/v1/auth/logout" and item[1] == "POST"
            ][1]
            assert second_logout[2]["authorization"] == f"Bearer {second_token}"
            logout_requests_before_failure_replay = logout_count
            await page.locator("#logoutButton").evaluate("(button) => button.click()")
            await page.wait_for_function(
                "() => document.getElementById('error').textContent"
                ".includes('سجّل الدخول أولًا')"
            )
            assert logout_count == logout_requests_before_failure_replay

            browser_state = await page.evaluate(
                "() => ({"
                "url: location.href, "
                "local: localStorage.length, "
                "session: sessionStorage.length"
                "})"
            )
            assert first_token not in browser_state["url"]
            assert second_token not in browser_state["url"]
            assert browser_state["local"] == 0
            assert browser_state["session"] == 0

            trigger_requests = [
                index
                for index, (path, method, _, _) in enumerate(requests)
                if path == f"/api/v1/runtime/tasks/{task_id}/trigger" and method == "POST"
            ]
            approval_request = next(
                index
                for index, (path, method, _, _) in enumerate(requests)
                if path == f"/api/v1/approvals/{approval_id}/approve" and method == "POST"
            )
            assert len(trigger_requests) == 2
            assert trigger_requests[0] < approval_request < trigger_requests[1]
            assert all(
                token not in request_url
                for token in tokens
                for _, _, _, request_url in requests
            )
            assert hosts == {"kai.test"}
            assert not unexpected
        finally:
            await browser.close()
