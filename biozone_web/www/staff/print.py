"""صفحة طباعة مستند واحد (S3b-print): فاتورة أو إشعار مرتجع.

تُعرض بيانات الطباعة فقط (بلا واجهة تشغيل) عبر نفس سياق
`order_delivery.get_context` — مصدر واحد بلا تكرار. الروابط من
صفحة التسليم تفتحها في تبويب جديد باسم المستند.
"""

import frappe


def get_context(context):
	from biozone_web.www.staff.order_delivery import get_context as load_delivery

	kind = (frappe.form_dict.get("doc") or "invoice").strip()
	if kind not in ("invoice", "credit"):
		kind = "invoice"
	context.print_kind = kind
	return load_delivery(context)
