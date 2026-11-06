# KAI — حالة بيئة العمل

## 2026-11-05

تم تثبيت Python 3.14.10 عبر `uv`، وإنشاء البيئة `.venv314`، وتثبيت اعتماديات `.[dev]` وعددها 56 حزمة. تم التحقق من `compileall` على `src`, `tests`, و`alembic`.

تم تثبيت Docker Engine 29.1.3 وDocker Compose v2.40.3. بدأ Docker daemon بنجاح، وأضيف المستخدم `ubuntu` إلى مجموعة `docker`؛ جلسة الطرفية الجديدة مطلوبة لتطبيق العضوية دون `sudo`. تشغيل حاوية بلا شبكة (`--network none`) نجح، لكن شبكة bridge تفشل في هذا sandbox لأن kernel لا يوفر جدول iptables الخام؛ لذلك لا يمكن تشغيل PostgreSQL/Compose الشبكي هنا دون بيئة Docker كاملة.

## أول بوابات Python 3.14

- `mypy`: ناجح، ويفحص 148 ملف مصدر.
- `alembic heads`: أصبح رأساً واحداً هو `7573ac048c34` بعد إضافة ترحيل دمج صريح للرأسين `a7c9e1f3b5d2` و`b2f7c1d9e4a6`.
- `ruff`: ناجح بعد إصلاح المخالفات التسع في `src/kai/demo.py` و`tests/test_approval_demo.py`.
- `mypy`: ناجح بعد التغييرات.
- الاختبارات الموسومة `db_guard_unit or no_db_unit`: 384 ناجحاً، اختبار واحد فاشل، و1479 مستبعداً. الفشل الوحيد هو اختبار Playwright للـdemo بسبب عدم توفر متصفح Chromium في البيئة، وليس فشل API أو migration.

## Docker test database

ملف `docker-compose.test.yml` مصمم لقاعدة اختبار منفصلة على `127.0.0.1:55432` مع volume مستقل ودور `kai_test` مقيد. لا تُحفظ كلمات المرور في المستودع؛ يجب تمرير مسارات ملفات سرية خارج المستودع عبر متغيرات `KAI_TEST_POSTGRES_ADMIN_PASSWORD_FILE` و`KAI_TEST_POSTGRES_PASSWORD_FILE`.

## عائق Docker الشبكي

رسالة daemon: `iptables ... table raw: Table does not exist`. هذا عائق في kernel/privilege للـsandbox وليس في ملف KAI. تم تثبيت Compose v2 والتحقق من parser، لكن تشغيل `db-test` يحتاج Cloud Computer أو جهاز المستخدم مع kernel يدعم bridge networking.

## قرار العمل

بيئة Python/Docker أصبحت جاهزة. تم إغلاق بوابة Ruff ورأس Alembic. لا يُعلن baseline أخضر قبل تشغيل PostgreSQL الاختباري واختبارات الترحيل والاختبارات العادية منفصلين، وتوفير Chromium لاختبار واجهة demo.
