# KAI — تقرير Full Test Suite

## تاريخ التشغيل

2026-11-05 باستخدام Python 3.14.10 وبيئة `.venv314`.

## النتائج

### التشغيل المباشر

الأمر:

```bash
pytest -q
```

النتيجة: رفض مقصود من حارس الاختبارات:

```text
ERROR: run migration tests separately: use -m db_migration or -m "not db_migration"
```

هذا ليس فشل اختبار؛ المشروع يمنع خلط اختبارات الترحيل بالاختبارات العادية.

### الاختبارات العادية قبل تهيئة PostgreSQL

الأمر:

```bash
pytest -q -m "not db_migration"
```

النتيجة:

- 385 اختباراً ناجحاً.
- 1474 خطأ إعداد أثناء fixtures التي تحتاج PostgreSQL.
- السبب الموحد: `KAI_TEST_ALLOW_DB_TRUNCATE` غير مضبوط، وقاعدة الاختبار المعزولة غير مهيأة في هذه الجلسة.

لا تشير هذه الأخطاء إلى 1474 عيباً مستقلاً؛ جميعها تتوقف عند fail-closed database guard قبل تنفيذ الاختبار.

### تشغيل PostgreSQL الفعلي داخل Docker

تم تشغيل PostgreSQL 17 داخل الحاوية `postgres:17-alpine` باستخدام `--network host` لأن Docker bridge غير متاح في Sandbox. أُنشئت كلمات مرور مؤقتة خارج المستودع، ونُظّفَت بعد التشغيل. تم التحقق من:

- قاعدة `kai_test` مع المستخدم الإداري `kai_test_admin`.
- اتصال الدور المقيد `kai_test` بنجاح.
- `alembic upgrade head` بنجاح حتى الرأس `7573ac048c34`.

### الاختبارات العادية بعد تهيئة PostgreSQL

الأمر:

```bash
pytest -q -m "not db_migration"
```

النتيجة الجزئية قبل الإيقاف الآمن:

- `942 passed`.
- `35 failed`.
- توقّف التشغيل بعد نحو 10 دقائق بسبب بطء/تعليق مسار اختبارات OAuth، ثم أُوقف بـ`Ctrl-C`.

الفشل ليس من PostgreSQL. جميع الإخفاقات تتركز في Contacts/Gmail/Google pending/token lifecycle، وأول سبب ظاهر هو أن callback يعيد `400` بدلاً من `303`.

السبب الجذري المرجح والمثبت من الكود: الاختبار يستخدم `http://testserver`، بينما `/auth/google/connect` يضبط cookie binding بعلامة `Secure`. لذلك لا يرسل عميل HTTPX cookie إلى callback، فيفشل التحقق من browser binding برسالة `invalid or expired oauth state`. يجب إصلاح fixture/عنوان الاختبار ليستخدم HTTPS محلياً أو توفير transport اختبار آمن، دون إزالة `Secure` من سياسة الإنتاج.

### اختبارات الترحيل

الأمر:

```bash
pytest -q -m db_migration
```

عند تشغيل `scripts/run_migration_tests.py` بعد تهيئة الحاوية، رفض orchestrator مرساة Docker اليدوية برسالة التحقق من daemon. الحاوية اليدوية لا توفر snapshot/configuration الذي يتطلبه orchestrator للتحقق من daemon، رغم أن PostgreSQL نفسه كان سليماً. يجب تشغيله عبر Compose الرسمي أو توفير Docker context/config مطابق للمرساة الموثوقة.

### الاختبارات المستقلة عن قاعدة البيانات

الأمر:

```bash
pytest -q -m "db_guard_unit or no_db_unit"
```

النتيجة: `385 passed`.

## العوائق الحالية

Docker Engine يعمل، لكن Docker bridge network محجوبة في Sandbox بسبب عدم توفر جدول `iptables raw` في Kernel. تم تجاوز ذلك مؤقتاً بـ`--network host` لتشغيل PostgreSQL، إلا أن هذا لا يطابق مرساة Compose التي يحتاجها migration orchestrator.

## القرار

لا توجد أخطاء كود متفرقة في المسار المستقل عن قاعدة البيانات. إغلاق Full Test Suite يتطلب:

1. إصلاح HTTPS/ Secure-cookie في fixture اختبارات Google.
2. تشغيل PostgreSQL عبر Compose الرسمي في Cloud Computer أو جهاز يدعم Docker bridge.
3. تشغيل migration orchestrator بمرساة Docker موثوقة.
4. إعادة تشغيل الاختبارات العادية مع `KAI_TEST_ALLOW_DB_TRUNCATE=1` بعد التحقق من الهوية والصلاحيات.

## تحديث إصلاح Secure Cookie

تم إصلاح بيئة الاختبار في `tests/conftest.py` دون تغيير سياسة الإنتاج:

- أصبح `base_url` لعميل ASGI هو `https://testserver`.
- أصبح `GOOGLE_OAUTH_REDIRECT_URI` هو `https://testserver/api/v1/auth/google/callback`.
- بقيت cookies التطبيق بعلامة `Secure` وخصائصها الأمنية كما هي.
- تم جعل `GoogleStub` في اختبارات pending يعلن `scope` صراحة عندما يختبر الاختبار حالة `scopes_from_provider=True`.

بعد الإصلاح، نجحت مسارات OAuth وContacts وGmail التي كانت تفشل بـ`400` بسبب عدم إرسال cookie. إعادة تشغيل المجموعة المتأثرة وصلت إلى `59 passed`، مع فشلين مستقلين في اختبارات pending لا علاقة لهما بـSecure Cookie:

1. تعارض ملكية الحساب أعاد `303` بدلاً من `409`.
2. تأكيد pending منتهي أعاد `404` بدلاً من `410`.

لذلك أُغلق سبب الـ35 فشلاً الأصلي المرتبط ببيئة HTTP، بينما بقي فشلان وظيفيان منفصلان يحتاجان إصلاحاً مستقلاً في منطق pending/الملكية قبل إعلان المجموعة كلها خضراء.
