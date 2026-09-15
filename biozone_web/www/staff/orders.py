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
	# خيارات فلتر العميل: عميل واحد لكل صف، بلا سقف 500.
	# استعلام تجميع واحد بدل جلب 500 طلب كامل وإسقاط المكرر في بايثون.
	# MIN(customer_name) يطابق اختيار الاسم القديم تمامًا (كان يرتّب بالاسم
	# تصاعديًا ويأخذ أول ظهور لكل عميل). الترتيب النهائي في بايثون عمدًا —
	# ترتيب MySQL (collation) يختلف عن ترتيب بايثون (codepoints) فيطابق
	# القديم حرفيًا.
	rows = frappe.db.sql(
		"""
		select customer as name, min(customer_name) as label
		from `tabSales Order`
		where docstatus in (0, 1) and customer is not null and customer != ''
		group by customer
		""",
		as_dict=True,
	)
	options = [{"name": r.name, "label": r.label or r.name} for r in rows if r.name]
	return sorted(options, key=lambda o: o["label"])


def _get_prep_counts(order_names: list) -> dict:
	"""عدد البنود والمؤكَّدة لكل طلب: {order_name: (total, confirmed)}.

	استعلام تجميع واحد على بنود الطلب — يغني عن تحميل كل مستند كاملًا
	لمجرد حساب عدّاد التقدم. custom_confirmed مخزّن 0/1 فيغني SUM عنه.
	"""
	if not order_names:
		return {}

	rows = frappe.db.sql(
		"""
		select parent as name, count(*) as total,
			coalesce(sum(custom_confirmed), 0) as confirmed
		from `tabSales Order Item`
		where parent in %(names)s
		group by parent
		""",
		{"names": order_names},
		as_dict=True,
	)
	return {r.name: (int(r.total), int(r.confirmed)) for r in rows}


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
			"status",
			"creation",
			"modified",
			"custom_needs_attention",
			"custom_attention_note",
		],
		order_by="creation desc",
		limit_page_length=200,
	)

	# إسقاط الملغاة من الصف الخفيف مباشرة (status عمود مخزّن) — بلا get_doc.
	rows = [r for r in rows if r.get("status") != "Cancelled"]

	# حالة التجهيز لكل طلب دفعة واحدة: عدد البنود وعدد المؤكَّدة عبر
	# GROUP BY واحد، بدل get_doc كامل (بكل جداوله الفرعية) لكل صف.
	counts = _get_prep_counts([r.name for r in rows])

	orders = []
	for r in rows:
		total, confirmed = counts.get(r.name, (0, 0))
		# كائن خفيف بنفس الحقول التي تقرأها get_order_prep_state تمامًا
		# (docstatus + البنود + حقلي الانتباه) — نفس منطق العرض، بلا تحميل.
		state = get_order_prep_state(
			frappe._dict(
				{
					"docstatus": r.docstatus,
					"custom_needs_attention": r.custom_needs_attention,
					"custom_attention_note": r.custom_attention_note,
					"items": [frappe._dict(custom_confirmed=1)] * confirmed
					+ [frappe._dict(custom_confirmed=0)] * (total - confirmed),
				}
			)
		)
		entry = {
			"name": r.name,
			"customer": r.customer,
			"customer_name": r.customer_name,
			"transaction_date": str(r.transaction_date or ""),
			"created_display": frappe.utils.format_datetime(r.creation, "dd MMM yyyy - HH:mm"),
			"grand_total": float(r.grand_total or 0),
			"grand_total_display": f"{float(r.grand_total or 0):,.2f} ج.م",
			"state": state["state"],
			"total": state["total"],
			"confirmed": state["confirmed"],
			"percent": state["percent"],
			"progress_text": state["progress_text"],
			"needs_attention": state["needs_attention"],
			"docstatus": r.docstatus,
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
