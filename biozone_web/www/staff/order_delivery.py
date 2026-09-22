import frappe
from frappe import _

from biozone_web.b9_utils import amount_in_arabic_words
from biozone_web.utils import (
	get_header_context,
	invoice_linked_to_order,
	render_state_page,
	require_staff_access,
)


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.error_state = False

	order_name = (frappe.form_dict.get("order") or "").strip()
	si_param = (frappe.form_dict.get("invoice") or "").strip()

	# الطلب غير موجود → 404 مهذبة (بلا traceback).
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return render_state_page(
			context,
			_("الطلب غير موجود"),
			_("رقم الطلب المطلوب غير موجود. تحقق من الرقم أو ارجع إلى قائمة الطلبات."),
			http_status_code=404,
		)

	so = frappe.get_doc("Sales Order", order_name)

	# الطلب الملغى → رسالة منفصلة قبل أي بحث عن فاتورة.
	if so.docstatus == 2:
		return render_state_page(
			context,
			_("هذا الطلب ملغى"),
			_("هذا الطلب ملغى ولا يمكن إنشاء مستند تسليم له."),
			order_name=so.name,
		)

	if si_param:
		# فاتورة مرسلة صراحة: وجود + اعتماد + ارتباط — أي فشل → رفض صريح بلا fallback،
		# ولا يجوز أبدًا عرض فاتورة تخص طلبًا آخر.
		if not frappe.db.exists("Sales Invoice", si_param):
			return render_state_page(
				context,
				_("الفاتورة المطلوبة غير صالحة"),
				_("الفاتورة المطلوبة غير موجودة، ولا يمكن عرضها لهذا الطلب."),
				http_status_code=404,
				order_name=so.name,
			)
		si = frappe.get_doc("Sales Invoice", si_param)
		if si.docstatus != 1 or not invoice_linked_to_order(si.name, order_name):
			return render_state_page(
				context,
				_("الفاتورة المطلوبة غير صالحة"),
				_("الفاتورة المطلوبة ليست فاتورة معتمدة لهذا الطلب، ولا يمكن عرضها هنا."),
				http_status_code=404,
				order_name=so.name,
			)
	else:
		# استنتاج تلقائي فقط عند غياب المعامل — ويجب أن تكون معتمدة ومرتبطة.
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
			return render_state_page(
				context,
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد"),
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد. لا يمكن إنشاء مستند تسليم حاليًا."),
				order_name=so.name,
			)
		si = frappe.get_doc("Sales Invoice", si_name)
		if si.docstatus != 1 or not invoice_linked_to_order(si.name, order_name):
			return render_state_page(
				context,
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد"),
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد. لا يمكن إنشاء مستند تسليم حاليًا."),
				order_name=so.name,
			)
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
			"public_rate": float(it.price_list_rate or it.rate or 0),
			"discount_percentage": float(it.discount_percentage or 0),
			"discount_amount": float(it.discount_amount or 0),
			"amount": float(it.amount or 0),
		}
		for i, it in enumerate(si.items or [])
	]
	context.items_count = len(si.items or [])
	context.net_total = float(si.net_total or 0)
	context.grand_total = grand
	# Phase-2 shipping row (print only): summed from the invoice taxes.
	from biozone_web.api import SHIPPING_ACCOUNT

	_shipping = 0
	for t in (si.get("taxes") or []):
		if (t.account_head or "").strip() == SHIPPING_ACCOUNT:
			_shipping += float(t.get("tax_amount") or 0)
	context.shipping = _shipping
	context.previous_balance = previous_balance
	context.current_balance = current_balance
	context.amount_words = amount_in_arabic_words(grand)
	context.confirmed_by = confirmer
	context.print_time = frappe.utils.now_datetime().strftime("%H:%M - %d/%m/%Y")
	context.company = si.company
	return context
