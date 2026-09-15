import frappe

from frappe import _
from biozone_web.utils import (
	assert_can_view_sales_order,
	get_header_context,
	redirect_staff_away_from_store,
)
from biozone_web.www.my_orders import STATUS_LABELS, STATUS_STYLES


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = None

	context.update(get_header_context())

	# نفس بوابة /my-orders: الزائر يُوجَّه لصفحة الدخول قبل أي فحص ملكية.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	# الاسم يأتي من قاعدة الـroute (website_route_rules) عبر form_dict.
	order_name = (frappe.form_dict.get("order_name") or "").strip()

	if not order_name:
		frappe.throw(
			_("رقم الطلب غير موجود"),
			frappe.DoesNotExistError,
		)

	if not frappe.db.exists("Sales Order", order_name):
		frappe.throw(
			_("الطلب غير موجود"),
			frappe.DoesNotExistError,
		)

	so = frappe.get_doc("Sales Order", order_name)

	# نفس حارس الملكية المستخدم في /order-confirmed — الطلبات الجديدة
	# للمالك الحقيقي، والقديمة (owner = Administrator) عبر Customer الفريد.
	assert_can_view_sales_order(so)

	context.order_number = str(so.name)
	context.status_label = STATUS_LABELS.get(so.status, so.status)
	context.status_style = STATUS_STYLES.get(so.status, "bg-surface-container text-on-surface-variant")
	context.date_display = frappe.utils.format_date(so.transaction_date, "d MMMM yyyy")
	context.item_count = len(so.items or [])

	context.items = [
		{
			"item_name": item.item_name or "",
			"qty": float(item.qty or 0),
			"uom": item.uom or "",
			"rate": float(item.rate or 0),
			"rate_display": f"{float(item.rate or 0):,.2f}",
			"amount": float(item.amount or 0),
			"amount_display": f"{float(item.amount or 0):,.2f}",
		}
		for item in (so.items or [])
	]

	grand_total = float(so.grand_total or 0)
	context.grand_total = grand_total
	context.grand_total_display = f"{grand_total:,.2f}"

	return context
