import frappe
from frappe import _


def require_staff_access():
	"""يتأكد إن اللي بيفتح أي صفحة تحت /staff/* هو حساب موظف (System User)
	مش حساب عميل (Website User اللي بيتعمل وقت التسجيل في /catalog).

	ملحوظة مهمة: ده فحص أولي بس (نوع الحساب) — مفيش لسه Role مخصص يفرّق
	بين موظف مخزن وموظف تسعير وموظف تجهيز طلبات (B6a/B6b/B6d كل واحد
	محتاج صلاحية مختلفة زي ما اتحدد في الخطة). لما B6d (الحسابات
	والخصومات) تتبنى فعليًا، لازم نضيف هنا (أو في كل صفحة لوحدها) فحص
	frappe.has_permission(...) على الـdoctype المناسب بدل الاكتفاء
	بـ"موظف عادي = يشوف كل حاجة" زي دلوقتي.
	"""
	user = frappe.session.user

	if user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	user_type = frappe.db.get_value("User", user, "user_type")
	if user_type != "System User":
		frappe.throw(_("هذه الصفحة مخصصة للموظفين فقط"), frappe.PermissionError)


def get_default_warehouse():
	"""Single-warehouse assumption: the stock-movement panel doesn't ask
	the user to pick a warehouse (per the agreed field list), so every
	movement logged from /staff/stock goes against one implicit
	warehouse. Uses Stock Settings' default_warehouse if set, otherwise
	falls back to the first non-group Warehouse. Raises clearly if
	neither exists so the failure is obvious instead of a silent None.
	"""
	warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if not warehouse:
		warehouse = frappe.db.get_value("Warehouse", {"is_group": 0}, "name", order_by="creation asc")
	if not warehouse:
		frappe.throw(_("لا يوجد مستودع معرّف في النظام — يرجى إعداد مستودع أولًا"))
	return warehouse


def get_header_context():
	"""Shared header state for any www page that includes site_header.html.
	Call this from the page's get_context() and merge the result in, e.g.:

		context.update(get_header_context())

	- is_logged_in: any authenticated user
	- user_full_name: shown in the header instead of the account icon
	"""
	user = frappe.session.user
	is_logged_in = user != "Guest"

	user_full_name = None
	if is_logged_in:
		user_full_name = frappe.db.get_value("User", user, "full_name") or user

	return {
		"is_logged_in": is_logged_in,
		"user_full_name": user_full_name,
	}