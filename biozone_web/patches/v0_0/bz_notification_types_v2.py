import frappe

from biozone_web.hooks import _BZ_NOTIFICATION_TYPES


_DESCRIPTIONS = {
	"BZ New Order": "طلب جديد بانتظار التجهيز (للستاف).",
	"BZ Delivered": "تم تسليم الطلب (للعميل).",
	"BZ Escalation": "طلب يحتاج انتباه (للستاف).",
	"BZ Escalation Reminder": "تذكير بطلب ما زال بلا معالجة (للستاف).",
}


def execute():
	"""تسجيل أنواع إشعارات biozone (المرحلة 1).

	اسم جديد إلزامي: الـPatch Log يحوي
	``biozone_web.patches.v0_0.bz_notification_types`` (نُفِّذ 2026-09-20
	ثم حُذف كوده) فإعادة الاسم نفسه تُتخطى بصمت.

	Idempotent: النوع الموجود (ومنها القدمان) يُتجاوَز بصمت — لا حذف
	ولا تعديل للموجود.
	"""
	for name in _BZ_NOTIFICATION_TYPES:
		if frappe.db.exists("Notification Type", name):
			continue
		doc = frappe.new_doc("Notification Type")
		doc.name = name
		doc.type_name = name
		doc.enabled = 1
		doc.description = _DESCRIPTIONS[name]
		doc.insert(ignore_permissions=True)
