import frappe


def get_context(context):
	# لو المستخدم داخل بالفعل، حوّله للصفحة الرئيسية بدل ما يشوف فورم تسجيل الدخول تاني
	if frappe.session.user != "Guest":
		frappe.local.flags.redirect_location = "/biozone-home"
		raise frappe.Redirect

	context.no_cache = 1
