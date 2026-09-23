import frappe

from biozone_web.hooks import _BZ_NOTIFICATION_TYPES


_DESCRIPTIONS = {
	"BZ Order Returned": "تم قبول مرتجع على الطلب (للعميل).",
}


def execute():
	"""تسجيل نوع إشعار المرتجع (S2a).

	اسم جديد إلزامي (فخ F22): v3 نُفِّذ فعلًا فإعادة اسمه تُتخطى بصمت.
	Idempotent: النوع الموجود يُتجاوَز بصمت — لا حذف ولا تعديل.
	"""
	for name in _BZ_NOTIFICATION_TYPES:
		if name not in _DESCRIPTIONS:
			continue
		if frappe.db.exists("Notification Type", name):
			continue
		doc = frappe.new_doc("Notification Type")
		doc.name = name
		doc.type_name = name
		doc.enabled = 1
		doc.description = _DESCRIPTIONS[name]
		doc.insert(ignore_permissions=True)
