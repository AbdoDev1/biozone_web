import frappe

from biozone_web.utils import (
	get_header_context,
	get_storefront_free_shipping_threshold,
	redirect_staff_away_from_store,
)


def get_context(context):
	redirect_staff_away_from_store()

	# سلة لكل عميل مسجل فقط (قرار المالك 2026-11-18): الزائر يُوجَّه للدخول.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	context.no_cache = 1
	context.active_page = "cart"
	context.update(get_header_context())
	context.free_shipping_threshold = get_storefront_free_shipping_threshold()
	context.csrf_token = frappe.sessions.get_csrf_token()
	return context
