import json

import frappe
from frappe import _

from biozone_web.b9_utils import get_item_barcodes, get_order_prep_state
from biozone_web.utils import (
	get_csrf_token_safe,
	get_header_context,
	render_state_page,
	require_staff_access,
)


def _b9_prep_details(so):
	"""بنود الطلب وإجمالياته للعرض — تُستخدم في التجهيز وفي عرض الملغى المقفل."""
	items = []
	# Phase-2: سعر وحدة العميل (سطر الطلب) + سعر الجمهور المرجعي (الصغرى)
	# دفعة واحدة — قراءة فقط للعرض في عمود سعر موحد.
	pub_map, stock_map = {}, {}
	_codes = [(it.item_code or "").strip() for it in (so.items or []) if it.item_code]
	if _codes:
		for r in frappe.get_all(
			"Item",
			filters={"item_code": ["in", _codes]},
			fields=["item_code", "stock_uom"],
		):
			stock_map[r.item_code] = r.stock_uom or ""
		for r in frappe.get_all(
			"Item Price",
			filters={"price_list": "Standard Selling", "item_code": ["in", _codes]},
			fields=["item_code", "price_list_rate", "uom"],
		):
			if r.item_code in pub_map:
				continue
			want = (stock_map.get(r.item_code) or "").strip()
			got = (r.uom or "").strip()
			if want and got and got != want:
				continue
			pub_map[r.item_code] = {"rate": r.price_list_rate, "uom": got or want}
	for idx, it in enumerate(so.items or [], start=1):
		pub = pub_map.get(it.item_code) or {}
		items.append(
			{
				"idx": idx,
				"name": it.name,
				"item_code": it.item_code,
				"item_name": it.item_name,
				"barcodes": get_item_barcodes(it.item_code),
				"qty": float(it.qty or 0),
				"uom": it.uom or "",
				"rate": float(it.rate or 0),
				"public_price": pub.get("rate"),
				"public_uom": (pub.get("uom") or "").strip() or stock_map.get(it.item_code, ""),
				"confirmed": bool(frappe.utils.cint(it.get("custom_confirmed"))),
				"confirmation_method": it.get("custom_confirmation_method") or "",
				"confirmed_by": it.get("custom_confirmed_by") or "",
			}
		)
	items_json = json.dumps(items, ensure_ascii=False, default=str)
	# Phase-2 shipping (read-only هنا): القيمة الحالية + إجمالي الطلب.
	from biozone_web.api import SHIPPING_ACCOUNT

	_shipping = 0
	for t in (so.get("taxes") or []):
		if (t.account_head or "").strip() == SHIPPING_ACCOUNT:
			_shipping = float(t.get("tax_amount") or 0)
			break
	return items, items_json, _shipping, float(so.grand_total or 0)


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = get_csrf_token_safe()
	context.error_state = False

	order_name = (frappe.form_dict.get("order") or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return render_state_page(
			context,
			_("الطلب غير موجود"),
			_("رقم الطلب المطلوب غير موجود. تحقق من الرقم أو ارجع إلى قائمة الطلبات."),
			http_status_code=404,
		)

	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus == 2:
		# S1: الملغى يُعرض بتفاصيله للقراءة فقط — مقفل بلا أي إجراء
		# (القالب يتجاهل كل الأزرار والسكربت في فرع error_state).
		state = get_order_prep_state(so)
		items, items_json, _shipping, grand_total = _b9_prep_details(so)
		context.order_name = so.name
		context.customer_name = so.customer_name
		context.state = state["state"]
		context.total = state["total"]
		context.items = items
		context.items_json = items_json
		context.grand_total = grand_total
		context.read_only = True
		return render_state_page(
			context,
			_("هذا الطلب ملغى"),
			_("هذا الطلب ملغى ولا يمكن تجهيزه أو إنشاء مستند تسليم له — التفاصيل أدناه للمراجعة فقط."),
			order_name=so.name,
		)
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

	items, items_json, _shipping, grand_total = _b9_prep_details(so)
	context.items = items
	context.items_json = items_json
	# Phase-2 shipping (read-only هنا): القيمة الحالية + إجمالي الطلب.
	context.shipping = _shipping
	context.grand_total = grand_total
	return context
