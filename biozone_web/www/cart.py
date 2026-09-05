import frappe

from biozone_web.utils import get_header_context, redirect_staff_away_from_store


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = "cart"
	context.update(get_header_context())
	context.csrf_token = frappe.sessions.get_csrf_token()
	return context
