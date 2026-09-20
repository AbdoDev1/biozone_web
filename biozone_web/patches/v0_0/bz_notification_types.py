import frappe


NOTIFICATION_TYPES = [
	("BZ New Order", "طلب جديد بانتظار التجهيز (للستاف)."),
	("BZ Delivered", "تم تسليم الطلب (للعميل)."),
]


def execute():
	"""تسجيل أنواع إشعارات biozone (Slice 1 — N4/N17).

	Idempotent: النوع الموجود يُتجاوَز بصمت — لا حذف ولا تعديل للموجود.
	"""
	for name, description in NOTIFICATION_TYPES:
		if frappe.db.exists("Notification Type", name):
			continue
		doc = frappe.new_doc("Notification Type")
		doc.name = name
		doc.type_name = name
		doc.enabled = 1
		doc.description = description
		doc.insert(ignore_permissions=True)
