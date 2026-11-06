# KAI — قرار توحيد سلسلة Alembic

## القرار

تم إنشاء ترحيل دمج صريح بالمعرّف `7573ac048c34`، وسلفين:

- `a7c9e1f3b5d2` — جدول `intent_attempts`.
- `b2f7c1d9e4a6` — جدول `google_oauth_pending`.

الترحيل الجديد لا يغيّر schema؛ وظيفته توحيد نقطتي السلسلة بعد تطبيق كلا الترحيلين. لذلك يحتوي `upgrade()` و`downgrade()` على `pass` عمداً.

## سبب الاختيار

الترحيلان مستقلان وظيفياً وكلاهما يعلن `e6f7a8b9c0d1` كسلف مباشر. تعديل أحدهما ليعتمد على الآخر سيغيّر ترتيب schema بلا ضرورة، وقد يجعل تسليم أحد التغييرين معتمداً على الآخر. merge revision يحافظ على استقلالية التغييرين ويعطي Alembic رأساً واحداً قابلاً للاستهداف.

## التحقق المحلي

```text
alembic heads
7573ac048c34 (head)
```

كما نجح `ruff` و`mypy` بعد إضافة الترحيل. اختبار `upgrade/downgrade` على PostgreSQL لم يُنفذ داخل Sandbox لأن Docker bridge network محجوب على مستوى Kernel؛ يجب تشغيله في بيئة PostgreSQL كاملة قبل الدمج النهائي.

## معيار الإغلاق

على بيئة PostgreSQL نظيفة يجب تنفيذ:

```bash
alembic upgrade head
alembic current
alembic downgrade base
alembic upgrade head
alembic check
```

ويجب أن يبقى `alembic heads` برأس واحد وأن تنجح اختبارات migration المعزولة.
