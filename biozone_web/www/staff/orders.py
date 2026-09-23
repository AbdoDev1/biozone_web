import frappe
from urllib.parse import urlencode

from biozone_web.b9_utils import (
	STATE_CANCELLED,
	STATE_DELIVERED,
	STATE_PREPARING,
	STATE_READY,
	get_order_prep_state,
)
from biozone_web.utils import get_header_context, require_staff_access

PAGE_SIZE = 20


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")

	filters = _read_filters()
	context.filters = filters
	page = _read_page()

	data = _load_orders(filters, page)
	if data is None:
		# صفحة خارج النطاق → توجيه نظيف لـpage=1 مع بقاء الفلاتر.
		qs = _build_querystring(filters)
		frappe.local.flags.redirect_location = f"/staff/orders?{qs}" if qs else "/staff/orders"
		raise frappe.Redirect

	context.orders = data["orders"]
	context.orders_count = data["total"]
	context.filter_urls = data["filter_urls"]
	context.customers = _customer_options()
	context.page = data["page"]
	context.total_pages = data["total_pages"]
	context.total = data["total"]
	return context


def _read_filters() -> dict:
	"""فلاتر لوحة الفلترة المنظمة (§8): بحث + حالة + انتباه + عميل + نطاق تاريخ."""
	state = (frappe.form_dict.get("state") or "all").strip()
	if state not in ("all", "preparing", "ready", "delivered", "attention", "cancelled", "returned"):
		state = "all"
	return {
		"q": (frappe.form_dict.get("q") or "").strip(),
		"state": state,
		"customer": (frappe.form_dict.get("customer") or "").strip(),
		"date_from": (frappe.form_dict.get("date_from") or "").strip(),
		"date_to": (frappe.form_dict.get("date_to") or "").strip(),
	}


def _read_page() -> int:
	"""رقم الصفحة من querystring — أي قيمة فاسدة أو <1 تعني 1."""
	raw = frappe.form_dict.get("page", 1)
	try:
		page = int(raw)
	except (ValueError, TypeError):
		return 1
	return page if page >= 1 else 1


def _build_querystring(filters: dict) -> str:
	"""querystring نظيفة للفلاتر الخمسة + page=1 (بلا تكرار page)."""
	params = {"page": 1}
	for key in ("q", "state", "customer", "date_from", "date_to"):
		if filters.get(key):
			params[key] = filters[key]
	return urlencode(params)


def _filter_urls_without(filters: dict) -> dict:
	"""روابط إسقاط كل فلتر مفرد (لشارات الفلاتر المفعلة) — تحفظ الباقي."""
	urls = {}
	for key in ("q", "state", "customer", "date_from", "date_to"):
		val = filters.get(key)
		if not val or val == "all":
			continue
		rest = {
			k: v
			for k, v in filters.items()
			if k != key and v and v != "all"
		}
		qs = urlencode({"page": 1, **rest})
		urls[key] = f"/staff/orders?{qs}" if rest else "/staff/orders"
	return urls


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


def _load_orders(filters: dict, page: int) -> dict | None:
	"""القائمة المفلترة كاملة للعدّادات + شريحة الصفحة للعرض.

	- الجلب الأول أسماء فقط (name/status/creation/needs_attention) بلا حد،
	  مرتبة `creation desc, name desc` (tiebreaker ثابت).
	- إسقاط Cancelled وفلترة state/attention بايثون-سايد (كما كانت).
	- `total` = طول المفلترة كاملة؛ `None` عند تجاوز آخر صفحة.
	"""
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
		fields=["name", "status", "creation", "docstatus", "custom_needs_attention", "custom_attention_note"],
		order_by="creation desc, name desc",
	)

	# إسقاط الملغاة من الصف الخفيف مباشرة (status عمود مخزّن) — بلا get_doc.
	# تُستثنى من الإسقاط عند فلتر "ملغى" صراحة (S1: الملغى ظاهر ومقفل).
	if filters["state"] != "cancelled":
		rows = [r for r in rows if r.get("status") != "Cancelled"]

	# حالة التجهيز لكل طلب دفعة واحدة: عدد البنود وعدد المؤكَّدة عبر
	# GROUP BY واحد، بدل get_doc كامل (بكل جداوله الفرعية) لكل صف.
	counts = _get_prep_counts([r.name for r in rows])

	filtered = []
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
		filtered.append((r, state))

	state_filter = filters["state"]
	if state_filter == "preparing":
		filtered = [(r, s) for r, s in filtered if s["state"] == STATE_PREPARING]
	elif state_filter == "ready":
		filtered = [(r, s) for r, s in filtered if s["state"] == STATE_READY]
	elif state_filter == "delivered":
		filtered = [(r, s) for r, s in filtered if s["state"] == STATE_DELIVERED]
	elif state_filter == "attention":
		filtered = [(r, s) for r, s in filtered if s["needs_attention"]]
	elif state_filter == "cancelled":
		filtered = [(r, s) for r, s in filtered if s["state"] == STATE_CANCELLED]
	elif state_filter == "returned":
		# الطلبات ذات مرتجع معتمد (DN مرتجع) — استعلام واحد بلا API/مخطط جديد.
		returned_orders = {
			r[0]
			for r in frappe.db.sql(
				"""select distinct ch.against_sales_order from `tabDelivery Note Item` ch
				inner join `tabDelivery Note` par on par.name = ch.parent
				where par.is_return = 1 and par.docstatus = 1"""
			)
		}
		filtered = [(r, s) for r, s in filtered if r.name in returned_orders]

	total = len(filtered)
	total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
	if page > total_pages:
		return None
	start = (page - 1) * PAGE_SIZE
	page_rows = [r for r, _ in filtered[start : start + PAGE_SIZE]]

	detail_map = {}
	if page_rows:
		for d in frappe.get_all(
			"Sales Order",
			filters={"name": ["in", [r.name for r in page_rows]]},
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
		):
			detail_map[d.name] = d

	state_by_name = {r.name: s for r, s in filtered}
	orders = []
	for r in page_rows:
		d = detail_map.get(r.name)
		if not d:
			continue
		state = state_by_name[d.name]
		entry = {
			"name": d.name,
			"customer": d.customer,
			"customer_name": d.customer_name,
			"transaction_date": str(d.transaction_date or ""),
			"created_display": frappe.utils.format_datetime(d.creation, "dd MMM yyyy - HH:mm"),
			"grand_total": float(d.grand_total or 0),
			"grand_total_display": f"{float(d.grand_total or 0):,.2f} ج.م",
			"state": state["state"],
			"total": state["total"],
			"confirmed": state["confirmed"],
			"percent": state["percent"],
			"progress_text": state["progress_text"],
			"needs_attention": state["needs_attention"],
			"docstatus": d.docstatus,
		}
		orders.append(entry)

	return {
		"orders": orders,
		"total": total,
		"page": page,
		"total_pages": total_pages,
		"filter_urls": _filter_urls_without(filters),
	}
