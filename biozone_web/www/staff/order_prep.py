import json

import frappe
from frappe import _

from biozone_web.b9_utils import get_item_barcodes, get_order_prep_state
from biozone_web.utils import get_csrf_token_safe, get_header_context, require_staff_access


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = get_csrf_token_safe()

	order_name = (frappe.form_dict.get("order") or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		frappe.throw(_("الطلب غير موجود"), frappe.DoesNotExistError)

	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus == 1:
		frappe.local.flags.redirect_location = f"/staff/order-delivery?order={so.name}"
		raise frappe.Redirect

	state = get_order_prep_state(so)
	context.order_name = so.name
	context.customer_name = so.customer_name
	context.state = state["state"]
	context.total = state["total"]
	context.confirmed = state["confirmed"]
	context.percent = state["percent"]
	context.progress_text = state["progress_text"]
	context.needs_attention = state["needs_attention"]
	context.attention_note = state["attention_note"]

	items = []
	for idx, it in enumerate(so.items or [], start=1):
		items.append(
			{
				"idx": idx,
				"name": it.name,
				"item_code": it.item_code,
				"item_name": it.item_name,
				"barcodes": get_item_barcodes(it.item_code),
				"qty": float(it.qty or 0),
				"uom": it.uom or "",
				"confirmed": bool(frappe.utils.cint(it.get("custom_confirmed"))),
				"confirmation_method": it.get("custom_confirmation_method") or "",
				"confirmed_by": it.get("custom_confirmed_by") or "",
			}
		)
	context.items = items
	context.items_json = json.dumps(items, ensure_ascii=False, default=str)
	return context
