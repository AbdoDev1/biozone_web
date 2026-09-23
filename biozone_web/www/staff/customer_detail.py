import frappe
from frappe import _

from biozone_web.utils import (
	customer_has_category_assigned_field,
	get_header_context,
	render_state_page,
	require_staff_access,
)


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "customers"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.error_state = False

	# الاسم يأتي من قاعدة الـroute (website_route_rules) عبر form_dict —
	# بلا أي افتراض لشكله، والتحقق الوحيد هو وجود السجل فعليًا.
	customer_name = (frappe.form_dict.get("customer_name") or "").strip()
	if not customer_name or not frappe.db.exists("Customer", customer_name):
		return render_state_page(
			context,
			_("العميل غير موجود"),
			_("بيانات العميل المطلوب غير موجودة. تحقق من الاسم أو ارجع إلى قائمة العملاء."),
			http_status_code=404,
			back_url="/staff/customers",
			back_label=_("العودة إلى قائمة العملاء"),
		)

	doc = frappe.get_doc("Customer", customer_name)

	context.customer_code = doc.name
	context.customer_display_name = doc.customer_name or doc.name
	context.customer_group = doc.customer_group
	context.reviewed = (
		bool(frappe.utils.cint(doc.get("category_assigned_by_staff")))
		if customer_has_category_assigned_field()
		else False
	)
	context.email = frappe.db.get_value(
		"Portal User", {"parenttype": "Customer", "parent": doc.name}, "user"
	) or ""
	context.created_display = frappe.utils.format_datetime(doc.creation, "dd MMM yyyy")
	context.invoices = _all_invoices(doc.name)

	return context


def _all_invoices(customer):
	"""نفس شكل مدخلات customers_json بالضبط (الاسم/التاريخ/الإجمالي/
	المستحق/الحالة) — بلا سقف الخمس فواتير الخاص بقائمة الانتظار."""
	rows = frappe.db.sql(
		"""
		select name, posting_date, grand_total, outstanding_amount, status
		from `tabSales Invoice`
		where docstatus = 1 and customer = %(name)s
		order by posting_date desc, creation desc
		""",
		{"name": customer},
		as_dict=True,
	)
	invoices = []
	for r in rows:
		invoices.append(
			{
				"name": r.name,
				"date": str(r.posting_date or ""),
				"date_display": frappe.utils.format_date(r.posting_date, "d MMMM yyyy")
				if r.posting_date
				else "",
				"total_display": f"{float(r.grand_total or 0):,.2f} ج.م",
				"outstanding_display": f"{float(r.outstanding_amount or 0):,.2f} ج.م",
				"status": r.status or "",
			}
		)
	return invoices
