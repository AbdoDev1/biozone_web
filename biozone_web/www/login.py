import frappe
from frappe import _

from biozone_web.utils import get_current_domain_role


def get_context(context):
	# مضيف app: تفويض كامل لدخول Frappe/ERPNext القياسي (قالب + سياق الإطار
	# الأصليين — frappe/www/login — بلا أي نموذج مكتوب يدويًا). يشمل مسار
	# POST الأصلي (/api/method/login) ودعم redirect-to وCSRF ورسائل الإطار
	# وهبوط /desk بعد النجاح، كله على نفس المضيف.
	if get_current_domain_role() == "app":
		context.no_cache = 1
		# تبديل القالب نفسه (الآلية الرسمية: post_process_context يعتمد
		# context.template) — بدونه يُصيَّر قالب المتجر رغم تفويض السياق.
		context.template = "frappe/www/login.html"
		from frappe.www.login import get_context as _frappe_login_context

		return _frappe_login_context(context)

	# مضيف staff: صفحة /login العامة (تسجيل العملاء) مرفوضة محليًا بـ403 —
	# مدخل الضيف هنا /staff/login حصرًا.
	if frappe.session.user == "Guest" and get_current_domain_role() == "staff":
		frappe.throw(_("غير متاح على هذا النطاق"), frappe.PermissionError)

	# لو المستخدم داخل بالفعل، حوّله لمدخله الصحيح بدل ما يشوف فورم تسجيل
	# الدخول تاني — الموظف (System User) على لوحة تحكم الموظف، والعميل
	# على الصفحة الرئيسية للمتجر.
	if frappe.session.user != "Guest":
		user_type = frappe.db.get_value("User", frappe.session.user, "user_type")
		frappe.local.flags.redirect_location = "/staff/dashboard" if user_type == "System User" else "/biozone-home"
		raise frappe.Redirect

	context.no_cache = 1
