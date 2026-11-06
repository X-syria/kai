# KAI — قائمة تحقق الاستلام

## قبل التطوير

- [ ] تأكيد archive/branch/commit المصدر.
- [ ] حفظ baseline دون أسرار.
- [ ] حصر التغييرات غير المثبتة وربطها بمصدر.
- [ ] تدوير أي credential ظهر في حزمة أو سجل.
- [ ] التحقق من Python وDocker وPostgreSQL.
- [ ] `alembic heads` رأس واحد.
- [ ] إنشاء قاعدة `kai_test` ودور مقيد.

## قبل الدمج

- [ ] اختبار unit وfailure paths.
- [ ] `ruff check .`.
- [ ] `mypy`.
- [ ] normal pytest منفصل عن migration pytest.
- [ ] migration tests على قاعدة مؤقتة وموسومة.
- [ ] مراجعة أسرار وملفات generated.
- [ ] مراجعة أثر خارجي: timeout/retry/idempotency/evidence.
- [ ] تحديث ADR/README/roadmap.

## قبل التشغيل التجريبي

- [ ] لا OAuth حي قبل مراجعة identity-confirmation.
- [ ] لا provider effect بلا approval صالح ودليل غير قابل للمحو.
- [ ] runbook للنسخ الاحتياطي والاسترجاع والتراجع.
- [ ] owner وon-call وrollback محددون.
