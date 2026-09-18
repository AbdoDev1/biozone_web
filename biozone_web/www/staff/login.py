import frappe
from frappe import _

from biozone_web.utils import get_current_domain_role


def get_context(context):
	# صفحة دخول الموظفين لا تُعرض إلا تحت مضيف staff — أي مضيف آخر
	# (store/app) يُرفض محليًا بـ403، بلا تحويل.
	if get_current_domain_role() != "staff":
		frappe.throw(_("غير متاح على هذا النطاق"), frappe.PermissionError)

	# لو داخل بالفعل، الموظف الحقيقي على لوحة التحكم، وأي حساب تاني
	# (عميل فتح الرابط غلط) مرفوض محليًا بـ403 — بلا نقل لأي مسار متجر.
	if frappe.session.user != "Guest":
		user_type = frappe.db.get_value("User", frappe.session.user, "user_type")
		if user_type == "System User":
			frappe.local.flags.redirect_location = "/staff/dashboard"
			raise frappe.Redirect
		frappe.throw(_("غير متاح على هذا النطاق"), frappe.PermissionError)

	context.no_cache = 1
	return context
