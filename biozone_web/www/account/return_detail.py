import frappe

from frappe import _
from biozone_web.utils import (
	assert_can_view_sales_order,
	get_header_context,
	redirect_staff_away_from_store,
	render_state_page,
)


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = None

	context.update(get_header_context())
	context.error_state = False

	# نفس بوابة /my-orders: الزائر يُوجَّه لصفحة الدخول قبل أي فحص ملكية.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	# الاسم يأتي من قاعدة الـroute (website_route_rules) عبر form_dict.
	credit_name = (frappe.form_dict.get("credit_name") or "").strip()

	if not credit_name or not frappe.db.exists("Sales Invoice", credit_name):
		return render_state_page(
			context,
			_("إشعار المرتجع غير موجود"),
			_("الإشعار المطلوب غير موجود. ارجع إلى حسابك وحاول مجددًا."),
			http_status_code=404,
			back_url="/account",
			back_label=_("العودة إلى الحساب"),
		)

	cn = frappe.get_doc("Sales Invoice", credit_name)
	if cn.docstatus != 1 or not frappe.utils.cint(cn.is_return):
		return render_state_page(
			context,
			_("إشعار المرتجع غير صالح"),
			_("هذا المستند ليس إشعار مرتجع معتمدًا."),
			http_status_code=404,
			back_url="/account",
			back_label=_("العودة إلى الحساب"),
		)

	# الملكية عبر الطلب الأصلي المرتبط ببنود الإشعار — نفس حارس الطلبات.
	order_name = frappe.db.get_value(
		"Sales Invoice Item", {"parent": cn.name}, "sales_order"
	)
	so = None
	if order_name and frappe.db.exists("Sales Order", order_name):
		so = frappe.get_doc("Sales Order", order_name)
		assert_can_view_sales_order(so)
	else:
		# بلا طلب مرتبط مرئي: قارن عميل الفاتورة بعميل الجلسة.
		from biozone_web.utils import find_customer_for_current_user

		if cn.customer != find_customer_for_current_user():
			frappe.throw(_("غير مصرح"), frappe.PermissionError)

	context.credit_number = str(cn.name)
	context.original_invoice = str(cn.return_against or "")
	context.order_number = str(so.name) if so else ""
	context.date_display = frappe.utils.format_date(cn.posting_date, "d MMMM yyyy")
	context.items = [
		{
			"item_name": it.item_name or "",
			"qty": abs(float(it.qty or 0)),
			"uom": it.uom or "",
			"rate": float(it.rate or 0),
			"rate_display": f"{float(it.rate or 0):,.2f}",
			"amount": abs(float(it.amount or 0)),
			"amount_display": f"{abs(float(it.amount or 0)):,.2f}",
		}
		for it in (cn.items or [])
	]
	total = abs(float(cn.grand_total or 0))
	context.grand_total_display = f"{total:,.2f}"

	return context
