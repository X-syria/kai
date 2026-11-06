# KAI — دليل الأتمتة

## البوابة المحلية السريعة

بعد توفير Python المدعوم وتثبيت `.[dev]`، شغّل `scripts/kai_quality_gate.sh`. يبدأ السكربت بتجميع Python وفحص baseline، ثم يشغّل `ruff` و`mypy` والاختبارات العادية. لن يشغّل اختبارات الترحيل تلقائياً؛ لتشغيلها في بيئة PostgreSQL معزولة استخدم `KAI_RUN_MIGRATION_TESTS=1`.

## بوابة الاستلام

شغّل `scripts/kai_baseline.sh` قبل أي حزمة أو طلب دمج. يفشل السكربت عند وجود `.env` أو ملف secret-like متتبع، ويعرض branch/commit وعدد التغييرات دون كشف القيم. فشل `alembic` في بيئة لا تحتوي الاعتماديات يُسجل كـ blocked، لا كنجاح.

## CI

الـworkflow الرسمي هو `.github/workflows/quality-gate.yml` ويستخدم Python 3.14 وPostgreSQL 17. يفصل provisioning للدور المقيد عن الاختبارات، ويشغّل ترحيلات منفصلة عن suite العادية. لا تُقبل skips صامتة ولا تشغيل migration tests بالتوازي مع الاختبارات العادية.

## الترتيب التشغيلي

1. static: تثبيت الاعتماديات، `ruff`, `mypy`, `compileall`.
2. normal database: إنشاء `kai_test` بدور مقيد، تطبيق الترحيلات، ثم الاختبارات العادية.
3. migration database: إنشاء قاعدة مؤقتة باسم run-specific، تشغيل migration suite، ثم حذفها فقط إذا أثبت المشغّل الملكية والهوية.
4. artifact: فحص سياق Docker وبناء الحزمة دون `.env` أو caches.

## قواعد الأتمتة

الأتمتة لا تغيّر schema عند إقلاع التطبيق، ولا تستخدم قاعدة التطوير للاختبارات، ولا تمرر أسرار المدير إلى child process بلا allowlist. الفشل الأمني أو فشل الترحيل يوقف المسار؛ لا توجد إعادة محاولة عمياء لمسار قد أنشأ أثراً خارجياً.
