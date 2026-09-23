import frappe

from biozone_web.utils import get_header_context, redirect_staff_away_from_store
from biozone_web.www.my_orders import build_customer_orders_list

PAGE_SIZE = 20


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = None
	context.update(get_header_context())

	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	# نفس منطق /my-orders القديم حرفيًا (الصلاحيات والتسميات) — عبر
	# الباني المشترك وحيد المصدر، بلا أي نسخة مكررة هنا.
	orders, orders_count = build_customer_orders_list()

	# ترقيم سيرفر بسيط (GET ?page=) — القائمة كانت كاملة بلا حد.
	try:
		page = int(frappe.form_dict.get("page") or 1)
	except (TypeError, ValueError):
		page = 1
	page = max(page, 1)
	total_pages = max((orders_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
	page = min(page, total_pages)
	start = (page - 1) * PAGE_SIZE
	context.orders = orders[start : start + PAGE_SIZE]
	context.orders_count = orders_count
	context.page = page
	context.total_pages = total_pages

	return context
