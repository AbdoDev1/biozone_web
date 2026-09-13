import json

import frappe

from biozone_web.b9_utils import STATE_DELIVERED, STATE_PREPARING, STATE_READY, get_order_prep_state
from biozone_web.utils import get_csrf_token_safe, get_header_context, require_staff_access

PAGE_SIZE = 20


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = get_csrf_token_safe()

	filters = _read_filters()
	context.filters = filters

	orders = _load_orders(filters)
	context.orders = orders
	context.orders_count = len(orders)
	context.preparing_count = sum(1 for o in orders if o["state"] == STATE_PREPARING)
	context.ready_count = sum(1 for o in orders if o["state"] == STATE_READY)
	context.delivered_count = sum(1 for o in orders if o["state"] == STATE_DELIVERED)
	context.attention_count = sum(1 for o in orders if o["needs_attention"])
	context.customers = _customer_options()
	context.orders_json = json.dumps(orders, ensure_ascii=False, default=str)
	return context


def _read_filters() -> dict:
	"""فلاتر لوحة الفلترة المنظمة (§8): بحث + حالة + انتباه + عميل + نطاق تاريخ."""
	state = (frappe.form_dict.get("state") or "all").strip()
	if state not in ("all", "preparing", "ready", "delivered", "attention"):
		state = "all"
	return {
		"q": (frappe.form_dict.get("q") or "").strip(),
		"state": state,
		"customer": (frappe.form_dict.get("customer") or "").strip(),
		"date_from": (frappe.form_dict.get("date_from") or "").strip(),
		"date_to": (frappe.form_dict.get("date_to") or "").strip(),
	}


def _customer_options() -> list:
	rows = frappe.get_all(
		"Sales Order",
		fields=["customer", "customer_name"],
		filters={"docstatus": ["in", [0, 1]]},
		limit_page_length=500,
		order_by="customer_name asc",
	)
	seen = {}
	for r in rows:
		if r.customer and r.customer not in seen:
			seen[r.customer] = r.customer_name or r.customer
	return [{"name": k, "label": v} for k, v in sorted(seen.items(), key=lambda kv: kv[1])]


def _load_orders(filters: dict) -> list:
	db_filters: dict = {}
	q = filters["q"]
	if q:
		# بحث برقم الطلب أو العميل أو الباركود/كود الصنف.
		matching_orders = set()
		for doctype, field in (("Sales Order", "name"), ("Sales Order", "customer_name")):
			rows = frappe.get_all(
				doctype, filters={field: ["like", f"%{q}%"]}, fields=["name"], limit_page_length=100
			)
			matching_orders.update(r.name for r in rows)
		item_codes = frappe.get_all(
			"Item Barcode", filters={"barcode": ["like", f"%{q}%"]}, fields=["parent"], limit_page_length=50
		)
		codes = [r.parent for r in item_codes]
		if codes or frappe.db.exists("Item", q):
			if frappe.db.exists("Item", q):
				codes.append(q)
			so_rows = frappe.get_all(
				"Sales Order Item",
				filters={"item_code": ["in", list(set(codes))]},
				fields=["parent"],
				limit_page_length=200,
			)
			matching_orders.update(r.parent for r in so_rows)
		if matching_orders:
			db_filters["name"] = ["in", list(matching_orders)]
		else:
			db_filters["name"] = ["like", f"%{q}%"]

	if filters["customer"]:
		db_filters["customer"] = filters["customer"]
	if filters["date_from"]:
		db_filters.setdefault("transaction_date", []).append([">=", filters["date_from"]])
	if filters["date_to"]:
		db_filters.setdefault("transaction_date", []).append(["<=", filters["date_to"]])
	# تبسيط نطاق التاريخ إلى شرطين منفصلين يفهمهما get_all.
	if isinstance(db_filters.get("transaction_date"), list) and db_filters["transaction_date"]:
		conds = db_filters.pop("transaction_date")
		if len(conds) == 1:
			db_filters["transaction_date"] = conds[0]
		elif len(conds) == 2:
			db_filters["transaction_date"] = ["between", [conds[0][1], conds[1][1]]]

	rows = frappe.get_all(
		"Sales Order",
		filters=db_filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"transaction_date",
			"delivery_date",
			"grand_total",
			"net_total",
			"docstatus",
			"creation",
			"modified",
			"custom_needs_attention",
			"custom_attention_note",
		],
		order_by="creation desc",
		limit_page_length=200,
	)

	orders = []
	for r in rows:
		doc = frappe.get_doc("Sales Order", r.name)
		state = get_order_prep_state(doc)
		# الطلبات الملغاة خارج القائمة تمامًا.
		if doc.status == "Cancelled":
			continue
		entry = {
			"name": r.name,
			"customer": r.customer,
			"customer_name": doc.customer_name,
			"transaction_date": str(r.transaction_date or ""),
			"created_display": frappe.utils.format_datetime(r.creation, "dd MMM yyyy - HH:mm"),
			"grand_total": float(doc.grand_total or 0),
			"grand_total_display": f"{float(doc.grand_total or 0):,.2f} ج.م",
			"state": state["state"],
			"total": state["total"],
			"confirmed": state["confirmed"],
			"percent": state["percent"],
			"progress_text": state["progress_text"],
			"needs_attention": state["needs_attention"],
			"docstatus": doc.docstatus,
		}
		orders.append(entry)

	state_filter = filters["state"]
	if state_filter == "preparing":
		orders = [o for o in orders if o["state"] == STATE_PREPARING]
	elif state_filter == "ready":
		orders = [o for o in orders if o["state"] == STATE_READY]
	elif state_filter == "delivered":
		orders = [o for o in orders if o["state"] == STATE_DELIVERED]
	elif state_filter == "attention":
		orders = [o for o in orders if o["needs_attention"]]

	return orders
