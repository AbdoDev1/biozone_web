import frappe


def get_context(context):
	# لو داخل بالفعل، حوّله لمكانه الصحيح بدل ما يشوف الفورم تاني —
	# موظف حقيقي على لوحة التحكم، وأي حساب تاني (لو حد فتح الرابط ده
	# غلط) على الصفحة الرئيسية للمتجر.
	if frappe.session.user != "Guest":
		user_type = frappe.db.get_value("User", frappe.session.user, "user_type")
		frappe.local.flags.redirect_location = "/staff/dashboard" if user_type == "System User" else "/biozone-home"
		raise frappe.Redirect

	context.no_cache = 1
	return context
