"""Minimal same-origin approval demo page."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KAI — موافقة التقويم</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; }
    body { max-width: 880px; margin: 2rem auto; padding: 0 1rem; color: #172033; }
    section, .notice {
      border: 1px solid #cbd5e1; border-radius: .7rem; padding: 1rem; margin: 1rem 0;
    }
    label { display: block; margin: .75rem 0 .25rem; font-weight: 600; }
    input, textarea, button { font: inherit; padding: .55rem; }
    input, textarea { box-sizing: border-box; width: 100%; }
    button { margin: .5rem .25rem 0 0; cursor: pointer; }
    button:disabled { cursor: wait; opacity: .6; }
    .notice { background: #fff8db; border-color: #d6a700; }
    .error { color: #9b1c1c; white-space: pre-wrap; }
    .muted { color: #526078; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <header>
    <h1>KAI — تجربة موافقة التقويم</h1>
    <p class="notice">هذه الصفحة لا توفر Google mock. متابعة التنفيذ تستخدم إعدادات الخدمة وقد تؤثر
      في تقويم حقيقي إذا كان حساب Google مرتبطًا. لا تتابع إلا إذا كنت تقصد ذلك.</p>
  </header>

  <section id="loginSection">
    <h2>تسجيل الدخول</h2>
    <form id="loginForm">
      <label for="email">البريد الإلكتروني</label>
      <input id="email" name="email" type="email" autocomplete="username" required>
      <label for="password">كلمة المرور</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">دخول</button>
    </form>
  </section>

  <main id="workspace" hidden>
    <button id="logoutButton" type="button">تسجيل الخروج</button>
    <section>
      <h2>طلب اجتماع</h2>
      <form id="taskForm">
        <label for="goal">ما الاجتماع الذي تريد جدولته؟</label>
        <textarea id="goal" name="goal" maxlength="2000" required></textarea>
        <label for="timezone">المنطقة الزمنية</label>
        <input id="timezone" name="timezone" value="Asia/Riyadh" maxlength="64" required>
        <button type="submit">إنشاء مهمة</button>
      </form>
      <button id="prepareButton" class="action" type="button" hidden>تحليل الطلب وإعداد
        الخطة للمراجعة</button>
    </section>

    <section id="taskSection" hidden>
      <h2>الخطة والحالة</h2>
      <p id="taskState"></p>
      <p id="nextAction" class="muted"></p>
      <h3>الخطة</h3>
      <div id="planContent"></div>
      <h3>دليل التنفيذ المنقّح</h3>
      <ul id="evidenceList"></ul>
      <p id="outcomeNotice" class="notice" hidden></p>
      <button id="refreshButton" class="action" type="button">تحديث الحالة</button>
    </section>

    <section id="approvalSection" hidden>
      <h2>مراجعة الموافقة</h2>
      <p id="approvalStatus"></p>
      <p>الإجراء: إنشاء حدث تقويم جديد.</p>
      <p>معلومات الدعوات: لا يحدد API الحالي هنا هل ستُرسل دعوات.</p>
      <p>المدة: لا يوجد حقل مدة مستقل؛ راجع وقت البداية والنهاية.</p>
      <pre id="approvalSummary"></pre>
      <button id="approveButton" class="action" type="button">موافق</button>
      <button id="rejectButton" class="action" type="button">رفض</button>
      <button id="continueButton" class="action" type="button" hidden>متابعة التنفيذ صراحةً</button>
    </section>

    <p id="message" role="status" aria-live="polite"></p>
    <p id="error" class="error" role="alert"></p>
  </main>

  <script>
  (() => {
    "use strict";
    let accessToken = null;
    let taskId = null;
    let snapshot = null;
    let activeApproval = null;
    let busy = false;

    const byId = (id) => document.getElementById(id);
    const loginSection = byId("loginSection");
    const workspace = byId("workspace");
    const taskSection = byId("taskSection");
    const approvalSection = byId("approvalSection");
    const message = byId("message");
    const error = byId("error");

    class ApiError extends Error {
      constructor(status, detail) {
        super(detail || `تعذر إكمال الطلب (HTTP ${status}).`);
        this.status = status;
      }
    }

    function setMessage(value) {
      message.textContent = value || "";
      error.textContent = "";
    }

    function showError(value) {
      error.textContent = value instanceof Error ? value.message : String(value);
      message.textContent = "";
    }

    function clearSession() {
      accessToken = null;
      taskId = null;
      snapshot = null;
      activeApproval = null;
      workspace.hidden = true;
      loginSection.hidden = false;
      taskSection.hidden = true;
      approvalSection.hidden = true;
      byId("loginForm").reset();
      byId("taskForm").reset();
      byId("password").value = "";
      byId("planContent").replaceChildren();
      byId("evidenceList").replaceChildren();
      byId("approvalSummary").textContent = "";
      byId("outcomeNotice").hidden = true;
      setMessage("تم تسجيل الخروج.");
    }

    async function request(path, { method = "GET", body, authenticated = true } = {}) {
      const headers = { "Accept": "application/json" };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (authenticated) {
        if (!accessToken) throw new Error("سجّل الدخول أولًا.");
        headers["Authorization"] = `Bearer ${accessToken}`;
      }
      const response = await fetch(`/api/v1${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body)
      });
      if (response.status === 401 && authenticated) {
        clearSession();
        throw new Error("انتهت الجلسة؛ سجّل الدخول مجددًا.");
      }
      let payload = null;
      try { payload = await response.json(); } catch (_) { /* Empty response body. */ }
      if (!response.ok) {
        const detail = payload && typeof payload.detail === "string" ? payload.detail : "";
        throw new ApiError(response.status, detail);
      }
      return payload;
    }

    function setBusy(value) {
      busy = value;
      document.querySelectorAll(".action, #loginForm button, #taskForm button, #logoutButton")
        .forEach((button) => { button.disabled = value; });
    }

    async function perform(action) {
      if (busy) return;
      setBusy(true);
      try { await action(); }
      catch (cause) { showError(cause); }
      finally { setBusy(false); render(); }
    }

    function addText(parent, tag, value) {
      const element = document.createElement(tag);
      element.textContent = String(value);
      parent.append(element);
      return element;
    }

    function humanState(state) {
      const labels = {
        WAITING_APPROVAL: "بانتظار موافقتك",
        COMPLETED: "اكتملت المهمة وتحقّق Runtime من النتيجة",
        FAILED: "فشل التنفيذ",
        CREATED: "أُنشئت المهمة",
        EXECUTING: "جارٍ التنفيذ",
        VERIFYING: "جارٍ التحقق",
        REQUIRES_ATTENTION: "تتطلب المهمة انتباهًا بشريًا"
      };
      return labels[state] || "حالة المهمة: " + (state || "غير متاحة");
    }

    function renderPlan(planResponse) {
      const root = byId("planContent");
      root.replaceChildren();
      if (!planResponse || !planResponse.plan) {
        addText(root, "p", "الخطة غير متاحة حتى الآن.");
        return;
      }
      const plan = planResponse.plan;
      addText(root, "p", plan.goal || "");
      if (Array.isArray(plan.success_criteria) && plan.success_criteria.length) {
        addText(root, "h4", "معايير النجاح");
        const criteria = document.createElement("ul");
        plan.success_criteria.forEach((item) => addText(criteria, "li", item));
        root.append(criteria);
      }
      if (Array.isArray(plan.steps) && plan.steps.length) {
        addText(root, "h4", "الخطوات");
        const steps = document.createElement("ol");
        plan.steps.forEach((step) => {
          const text = [step.action, step.purpose].filter(Boolean).join(" — ");
          addText(steps, "li", text);
        });
        root.append(steps);
      }
      const notes = [...(plan.assumptions || []), ...(plan.unresolved_questions || [])];
      if (notes.length) {
        addText(root, "h4", "افتراضات وأسئلة غير محسومة");
        const list = document.createElement("ul");
        notes.forEach((item) => addText(list, "li", item));
        root.append(list);
      }
    }

    function render() {
      loginSection.hidden = Boolean(accessToken);
      workspace.hidden = !accessToken;
      taskSection.hidden = !taskId;
      byId("prepareButton").hidden =
        !taskId || Boolean(snapshot && snapshot.task_state !== "CREATED");
      byId("taskState").textContent = snapshot
        ? humanState(snapshot.task_state)
          + ` (${snapshot.task_state})`
        : (taskId ? "تم إنشاء المهمة؛ ابدأ التحليل لعرض الخطة وطلب الموافقة." : "");
      byId("nextAction").textContent = snapshot ? `الخطوة التالية: ${snapshot.next_action}` : "";
      byId("approveButton").hidden = !activeApproval || activeApproval.status !== "PENDING";
      byId("rejectButton").hidden = !activeApproval || activeApproval.status !== "PENDING";
      byId("continueButton").hidden = !activeApproval || activeApproval.status !== "APPROVED"
        || !snapshot || ["COMPLETED", "FAILED", "REQUIRES_ATTENTION"].includes(snapshot.task_state);
      approvalSection.hidden = !activeApproval;
      if (activeApproval) {
        byId("approvalStatus").textContent = `حالة الموافقة: ${activeApproval.status}`;
        byId("approvalSummary").textContent =
          activeApproval.request_summary || "ملخص الموافقة غير متاح.";
      }
      const evidence = byId("evidenceList");
      evidence.replaceChildren();
      (snapshot?.evidence || []).forEach((item) => {
        const parts = [item.tool, item.outcome, item.ok ? "نجح الاستدعاء" : "لم ينجح الاستدعاء"];
        if (item.verified) parts.push("تم التحقق");
        if (item.external_outcome) parts.push(`النتيجة الخارجية: ${item.external_outcome}`);
        addText(evidence, "li", parts.join(" — "));
      });
      const ambiguousCalendarWrite = Boolean(snapshot?.evidence?.some((item) =>
        item.tool === "calendar.create_event" && item.external_outcome === "UNKNOWN"
      ));
      const requiresReview = snapshot?.task_state === "REQUIRES_ATTENTION";
      byId("outcomeNotice").hidden = !ambiguousCalendarWrite && !requiresReview;
      if (ambiguousCalendarWrite) {
        byId("outcomeNotice").textContent =
          "النتيجة غير محسومة. قد يكون الحدث أُنشئ وقد لا يكون؛ تحقّق من التقويم قبل أي إجراء. "
          + "لن تعيد الصفحة الكتابة تلقائيًا.";
      } else if (requiresReview) {
        byId("outcomeNotice").textContent =
          "المهمة تحتاج إلى مراجعة. راجع التفاصيل قبل المتابعة.";
      }
    }

    async function refreshTask() {
      if (!taskId) return;
      snapshot = await request(`/runtime/tasks/${encodeURIComponent(taskId)}`);
      render();
      const ids = snapshot.approval_ids || [];
      if (ids.length) {
        activeApproval = await request(`/approvals/${encodeURIComponent(ids[0])}`);
      }
      render();
      const plan = await request(`/tasks/${encodeURIComponent(taskId)}/plan`);
      renderPlan(plan);
    }

    byId("loginForm").addEventListener("submit", (event) => {
      event.preventDefault();
      perform(async () => {
        const payload = await request("/auth/login", {
          method: "POST",
          authenticated: false,
          body: {
            email: byId("email").value,
            password: byId("password").value
          }
        });
        if (!payload || typeof payload.access_token !== "string")
          throw new Error("استجابة تسجيل الدخول لا تحتوي رمز وصول صالحًا.");
        accessToken = payload.access_token;
        byId("password").value = "";
        setMessage("تم تسجيل الدخول. أدخل طلب الاجتماع.");
      });
    });

    byId("logoutButton").addEventListener("click", () => perform(async () => {
      let logoutError = null;
      try {
        const result = await request("/auth/logout", { method: "POST" });
        if (!result || result.revoked !== true)
          throw new Error("لم يؤكد الخادم إبطال الرمز.");
      } catch (cause) {
        logoutError = cause;
      } finally {
        clearSession();
      }
      if (logoutError) {
        const detail = logoutError instanceof Error ? ` ${logoutError.message}` : "";
        showError(`مُسح الرمز محليًا، لكن تعذّر تأكيد إبطاله على الخادم.${detail}`);
      } else {
        setMessage("تم تسجيل الخروج وإبطال الرمز على الخادم.");
      }
    }));

    byId("taskForm").addEventListener("submit", (event) => {
      event.preventDefault();
      perform(async () => {
        const payload = await request("/runtime/tasks", {
          method: "POST",
          body: {
            goal: byId("goal").value,
            timezone: byId("timezone").value
          }
        });
        taskId = payload.task_id;
        snapshot = {
          task_state: payload.task_state, next_action: "EXECUTE", evidence: [], approval_ids: []
        };
        activeApproval = null;
        render();
        setMessage("أُنشئت المهمة. اضغط تحليل الطلب لإعداد الخطة والموافقة.");
      });
    });

    byId("prepareButton").addEventListener("click", () => perform(async () => {
      await request(`/runtime/tasks/${encodeURIComponent(taskId)}/trigger`, { method: "POST" });
      await refreshTask();
      setMessage("تم تحديث الخطة والحالة. راجع التفاصيل قبل اتخاذ القرار.");
    }));

    byId("approveButton").addEventListener("click", () => perform(async () => {
      activeApproval = await request(
        `/approvals/${encodeURIComponent(activeApproval.id)}/approve`,
        {
          method: "POST",
          body: {}
        }
      );
      await refreshTask();
      setMessage("سُجّلت الموافقة. لم يبدأ التنفيذ؛ استخدم زر المتابعة الصريح عند الاستعداد.");
    }));

    byId("rejectButton").addEventListener("click", () => perform(async () => {
      activeApproval = await request(`/approvals/${encodeURIComponent(activeApproval.id)}/reject`, {
        method: "POST",
        body: {}
      });
      await refreshTask();
      setMessage("سُجّل الرفض؛ لم تطلب الصفحة تنفيذ أداة التقويم.");
    }));

    byId("continueButton").addEventListener("click", () => perform(async () => {
      await request(`/runtime/tasks/${encodeURIComponent(taskId)}/trigger`, { method: "POST" });
      await refreshTask();
      setMessage("استجاب Runtime لطلب المتابعة. راجع الحالة والأدلة أدناه.");
    }));

    byId("refreshButton").addEventListener("click", () => perform(async () => {
      await refreshTask();
      setMessage("تم تحديث الحالة من Runtime.");
    }));

    render();
  })();
  </script>
</body>
</html>"""


@router.get("/demo", response_class=HTMLResponse, include_in_schema=False)
async def approval_demo() -> HTMLResponse:
    return HTMLResponse(
        _PAGE,
        headers={"Cache-Control": "no-store"},
    )
