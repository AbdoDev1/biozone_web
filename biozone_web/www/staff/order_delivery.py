import frappe
from frappe import _

from biozone_web.b9_utils import amount_in_arabic_words
from biozone_web.utils import get_header_context, require_staff_access


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")

	order_name = (frappe.form_dict.get("order") or "").strip()
	si_param = (frappe.form_dict.get("invoice") or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		frappe.throw(_("الطلب غير موجود"), frappe.DoesNotExistError)

	so = frappe.get_doc("Sales Order", order_name)

	si_name = si_param or frappe.db.get_value(
		"Sales Invoice Item", {"sales_order": order_name, "docstatus": ["!=", 2]}, "parent"
	)
	if not si_name:
		# احتياط عبر التسليم المرتبط بالطلب.
		dn_name = frappe.db.get_value(
			"Delivery Note Item", {"against_sales_order": order_name, "docstatus": ["!=", 2]}, "parent"
		)
		if dn_name:
			si_name = frappe.db.get_value(
				"Sales Invoice Item", {"delivery_note": dn_name, "docstatus": ["!=", 2]}, "parent"
			)
	if not si_name or not frappe.db.exists("Sales Invoice", si_name):
		frappe.throw(_("لا توجد فاتورة معتمَدة لهذا الطلب بعد"), frappe.DoesNotExistError)

	si = frappe.get_doc("Sales Invoice", si_name)
	dn_name = frappe.db.get_value(
		"Delivery Note Item", {"against_sales_order": order_name, "docstatus": ["!=", 2]}, "parent"
	)

	# السابق من الحقل المثبَّت لحظة إنشاء الفاتورة (C2) — بلا حساب جديد هنا.
	previous_balance = frappe.utils.flt(si.get("custom_previous_balance"))
	invoice_outstanding = frappe.utils.flt(si.outstanding_amount) or frappe.utils.flt(si.grand_total)
	current_balance = previous_balance + invoice_outstanding
	confirmer = frappe.db.get_value("User", si.owner, "full_name") or si.owner
	grand = float(si.rounded_total or si.grand_total or 0)

	context.order_name = so.name
	context.delivery_note = dn_name or ""
	context.invoice_name = si.name
	context.customer_name = si.customer_name
	context.customer_address = si.address_display or si.customer_address or ""
	context.posting_date = frappe.utils.format_date(si.posting_date, "dd/MM/yyyy")
	context.items = [
		{
			"idx": i + 1,
			"item_name": it.item_name,
			"qty": float(it.qty or 0),
			"rate": float(it.rate or 0),
			"discount_percentage": float(it.discount_percentage or 0),
			"discount_amount": float(it.discount_amount or 0),
			"amount": float(it.amount or 0),
		}
		for i, it in enumerate(si.items or [])
	]
	context.items_count = len(si.items or [])
	context.net_total = float(si.net_total or 0)
	context.grand_total = grand
	context.previous_balance = previous_balance
	context.current_balance = current_balance
	context.amount_words = amount_in_arabic_words(grand)
	context.confirmed_by = confirmer
	context.print_time = frappe.utils.now_datetime().strftime("%H:%M - %d/%m/%Y")
	context.company = si.company
	return context
