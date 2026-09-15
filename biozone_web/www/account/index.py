import frappe

from biozone_web.utils import (
	customer_has_category_assigned_field,
	find_customer_for_current_user,
	get_header_context,
	get_portal_users_for_customer,
	redirect_staff_away_from_store,
)
from biozone_web.www.my_orders import build_customer_orders_list

RECENT_ORDERS_LIMIT = 3
UNASSIGNED_CATEGORY_LABEL = "لم تُحدَّد لك فئة تسعير بعد"


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = None
	context.update(get_header_context())

	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	# قراءة فقط: find بلا إنشاء — لو لا يوجد ربط بعد، تُعرض بيانات
	# المستخدم الأساسية مع حالة "غير محددة" وبلا طلبات.
	customer = find_customer_for_current_user()

	if customer:
		customer_name = frappe.db.get_value("Customer", customer, "customer_name") or customer
		group = frappe.db.get_value("Customer", customer, "customer_group")
		if customer_has_category_assigned_field():
			assigned = frappe.utils.cint(
				frappe.db.get_value("Customer", customer, "category_assigned_by_staff")
			)
		else:
			assigned = 0
		users = get_portal_users_for_customer(customer)
		email = users[0] if users else frappe.session.user
	else:
		customer_name = (
			frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
		)
		group = None
		assigned = 0
		email = frappe.session.user

	context.customer_name = customer_name
	context.email = email
	if assigned:
		context.category_assigned = True
		context.customer_group = group
	else:
		# قيد B10: لا يُعرض اسم الفئة الفعلي إطلاقًا قبل التعيين الإداري،
		# حتى لو كانت "الجمهور" مخزَّنة فعليًا — نص محايد فقط.
		context.category_assigned = False
		context.unassigned_label = UNASSIGNED_CATEGORY_LABEL

	orders, orders_count = build_customer_orders_list()
	context.orders_count = orders_count
	context.recent_orders = orders[:RECENT_ORDERS_LIMIT]

	return context
