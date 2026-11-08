import frappe

ROLE = "BZ Cart Viewer"


def execute():
	"""دور قراءة سلال العملاء — الصلاحيات نفسها لا تُمنح على DocTypes.

	الوصول يمر عبر صفحة Desk `/app/carts` وخدماتها فقط، وليس عبر REST مباشر.
	"""
	if frappe.db.exists("Role", ROLE):
		return
	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": ROLE,
			"desk_access": 1,
		}
	).insert(ignore_permissions=True)
