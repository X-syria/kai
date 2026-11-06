# KAI — Playwright وChromium

## البيئة المحلية

بعد تثبيت اعتماديات `.[dev]`، ثبّت المتصفح واعتماديات Linux عبر:

```bash
.venv314/bin/python -m playwright install --with-deps chromium
```

ثم شغّل اختبار الواجهة:

```bash
.venv314/bin/pytest -q tests/test_approval_demo.py::test_approval_demo_browser_workflow_is_fully_intercepted
```

## CI

يحتوي `.github/workflows/quality-gate.yml` على خطوة صريحة لتثبيت Chromium بعد تثبيت حزمة التطوير. لا تعتمد الاختبارات على وجود متصفح مسبق في runner.

## Docker

`Dockerfile.test` صورة اختبار منفصلة تحتوي Python 3.14، اعتماديات KAI، Playwright Chromium، والاختبارات. لا تمت إضافة Chromium إلى `Dockerfile` الإنتاجي حتى لا تتضخم صورة API بمتصفح غير مطلوب أثناء التشغيل.

البناء والتشغيل:

```bash
docker build -f Dockerfile.test -t kai-test .
docker run --rm --network none kai-test
```

اختبارات PostgreSQL تحتاج شبكة Docker كاملة؛ `--network none` مناسب فقط لاختبارات الوحدة وواجهة demo التي تعترض طلبات المتصفح داخل الاختبار.
