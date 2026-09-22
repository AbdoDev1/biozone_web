import frappe


def get_context(context):
	# نُقلت الإعدادات إلى تبويب داخل /staff/items — تحويل دائم للرابط القديم.
	frappe.local.flags.redirect_location = "/staff/items?tab=units"
	raise frappe.Redirect
