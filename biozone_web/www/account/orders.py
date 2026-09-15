import frappe

from biozone_web.utils import get_header_context, redirect_staff_away_from_store
from biozone_web.www.my_orders import build_customer_orders_list


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
	context.orders = orders
	context.orders_count = orders_count

	return context
