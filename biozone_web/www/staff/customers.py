import datetime
from decimal import Decimal

import frappe

from biozone_web.utils import (
	customer_has_category_assigned_field,
	get_csrf_token_safe,
	get_header_context,
	get_public_customer_group,
	require_staff_access,
)

PAGE_SIZE = 20
RECENT_INVOICES_LIMIT = 5


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "customers"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = get_csrf_token_safe()

	has_review_field = customer_has_category_assigned_field()
	context.has_review_field = has_review_field
	context.public_group = get_public_customer_group()

	# D1: صفحة واحدة بلا أوضاع — مرشح الحالة: الكل أو لم تُصنَّف فقط.
	st = (frappe.form_dict.get("st") or "all").strip()
	if st not in ("all", "unclassified"):
		st = "all"
	if not has_review_field:
		st = "all"
	context.st = st

	search_term = (frappe.form_dict.get("q") or "").strip()
	context.search_term = search_term

	try:
		page = int(frappe.form_dict.get("page") or 1)
	except (TypeError, ValueError):
		page = 1
	page = max(page, 1)

	groups = frappe.get_all(
		"Customer Group",
		filters={"is_group": 0, "disabled": 0},
		fields=["name"],
		order_by="name asc",
	)
	context.groups = groups

	selected_group = (frappe.form_dict.get("group") or "all").strip()
	if selected_group != "all" and selected_group not in [g.name for g in groups]:
		selected_group = "all"
	context.selected_group = selected_group

	base_filters = _build_filters(search_term, selected_group)

	rows, total_count, unclassified_count = _paged_partitioned_rows(
		base_filters, has_review_field, st == "unclassified", page
	)
	context.total_count = total_count
	context.unclassified_count = unclassified_count

	names = [r.name for r in rows]
	emails = _portal_emails(names)
	stats = _invoice_stats(names)
	recents = _recent_invoices(names)
	changes = _last_category_changes(names)

	customers = []
	for r in rows:
		st = stats.get(r.name, {})
		recent = recents.get(r.name, [])
		last = recent[0] if recent else None
		billed = float(st.get("billed_total") or 0)
		outstanding = float(st.get("outstanding_total") or 0)
		customers.append(
			{
				"name": r.name,
				"customer_name": r.customer_name or r.name,
				"customer_group": r.customer_group,
				"reviewed": True if not has_review_field else bool(r.get("category_assigned_by_staff")),
				"last_change": changes.get(r.name) or {},
				"email": emails.get(r.name, ""),
				"created_display": frappe.utils.format_datetime(r.creation, "dd MMM yyyy"),
				"invoice_count": int(st.get("invoice_count") or 0),
				"billed_total": billed,
				"billed_display": f"{billed:,.2f} ج.م",
				"paid_display": f"{billed - outstanding:,.2f} ج.م",
				"outstanding_total": outstanding,
				"outstanding_display": f"{outstanding:,.2f} ج.م",
				"last_invoice": last,
				"invoices": recent,
			}
		)
	context.customers = [_jsonable(c) for c in customers]

	context.page = page
	context.has_prev = page > 1
	context.has_next = page * PAGE_SIZE < total_count
	context.prev_page = page - 1
	context.next_page = page + 1
	context.total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)

	return context


def _jsonable(value):
	"""حوّل أي قيمة غير بدائية إلى نص قبل tojson — بلا |safe وبلا default.

	tojson يفشل على Decimal/date/datetime (كانت تُمرَّر سابقًا عبر
	json.dumps بـ default=str، وحذفه أسقط الحماية). القوائم/القواميس
	تُعالَج بعمق؛ البدائيات (نص/رقم/منطقي/None) تمر كما هي.
	"""
	if isinstance(value, (datetime.datetime, datetime.date, datetime.time, Decimal)):
		return str(value)
	if isinstance(value, dict):
		return {k: _jsonable(v) for k, v in value.items()}
	if isinstance(value, (list, tuple)):
		return [_jsonable(v) for v in value]
	return value


def _row_fields(has_review_field):
	if has_review_field:
		return [
			"name",
			"customer_name",
			"customer_group",
			"category_assigned_by_staff",
			"creation",
		]
	return ["name", "customer_name", "customer_group", "creation"]


def _build_filters(search_term, selected_group="all"):
	"""فلاتر الأساس المشتركة: الفئة + البحث — كلها AND عبر get_all.

	البحث يُحل أولًا لأسماء عملاء (نفس أسلوب _load_orders في orders.py:
	استعلامات منفصلة ثم name IN (...)) حتى لا تختلط شروط OR الخاصة
	بالبحث مع باقي الفلاتر. حالة التصنيف ليست هنا — تُطبَّق في التقسيم
	أدناه لا في فلتر العرض.
	"""
	filters = {}
	if selected_group != "all":
		filters["customer_group"] = selected_group
	if search_term:
		matched = set()
		for field in ("customer_name", "name"):
			matched.update(
				r.name
				for r in frappe.get_all(
					"Customer",
					filters={field: ["like", f"%{search_term}%"]},
					fields=["name"],
					limit_page_length=100,
				)
			)
		matched.update(
			r.parent
			for r in frappe.get_all(
				"Portal User",
				filters={"parenttype": "Customer", "user": ["like", f"%{search_term}%"]},
				fields=["parent"],
				limit_page_length=100,
			)
			if r.parent
		)
		filters["name"] = ["in", sorted(matched)] if matched else ["in", ["__no_match__"]]
	return filters


def _fetch_rows(filters, order_by, start, length, has_review_field):
	if length <= 0:
		return []
	return frappe.get_all(
		"Customer",
		fields=_row_fields(has_review_field),
		filters=filters,
		order_by=order_by,
		start=start,
		page_length=length,
	)


def _paged_partitioned_rows(base_filters, has_review_field, only_unclassified, page):
	"""صفحة واحدة بلا أوضاع: غير المصنف (علم != 1) أولًا بالأحدث
	تسجيلًا، ثم البقية أبجديًا — مع ترقيم صحيح عبر القائمتين.

	تُرجع (rows, total, unclassified_total). بلا حقل مراجعة: قائمة
	واحدة بالأحدث، والعداد صفر.
	"""
	start = (page - 1) * PAGE_SIZE
	if not has_review_field:
		total = frappe.db.count("Customer", base_filters)
		return _fetch_rows(base_filters, "creation desc", start, PAGE_SIZE, False), total, 0
	filters_a = dict(base_filters)
	filters_a["category_assigned_by_staff"] = ["!=", 1]
	total_a = frappe.db.count("Customer", filters_a)
	if only_unclassified:
		return (
			_fetch_rows(filters_a, "creation desc", start, PAGE_SIZE, True),
			total_a,
			total_a,
		)
	filters_b = dict(base_filters)
	filters_b["category_assigned_by_staff"] = 1
	total_b = frappe.db.count("Customer", filters_b)
	rows = []
	if start < total_a:
		rows += _fetch_rows(filters_a, "creation desc", start, min(PAGE_SIZE, total_a - start), True)
	rest = PAGE_SIZE - len(rows)
	if rest > 0:
		rows += _fetch_rows(filters_b, "customer_name asc", max(0, start - total_a), rest, True)
	return rows, total_a + total_b, total_a


def _last_category_changes(names):
	"""آخر تدقيق فئة لكل عميل في الصفحة — استعلام واحد بدقة البادئة.

	تعليقات التدقيق تبدأ حصرًا بـ [فئة] (تغييرًا وتثبيتًا)، فالفلتر
	like عليها يعيد تدقيق الفئة فقط — لا نصًا حرًا ولا أول مطابَق
	صدفة. أول صف لكل عميل (الأحدث أولًا) هو الأحدث.
	"""
	if not names:
		return {}
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Customer",
			"reference_name": ["in", names],
			"content": ["like", "[فئة]%"],
		},
		fields=["reference_name", "content", "creation"],
		order_by="creation desc",
		limit_page_length=100,
	)
	out = {}
	for r in rows:
		if r.reference_name in out:
			continue
		text = (r.content or "").strip()
		if not text.startswith("[فئة]"):
			continue
		out[r.reference_name] = {
			"text": text[len("[فئة]"):].strip()[:90],
			"when": frappe.utils.format_datetime(r.creation, "dd MMM yyyy")
			if r.creation
			else "",
		}
	return out


def _portal_emails(names):
	"""البريد المرتبط بكل عميل عبر Portal User — استعلام واحد للصفحة."""
	if not names:
		return {}
	rows = frappe.get_all(
		"Portal User",
		filters={"parenttype": "Customer", "parent": ["in", names]},
		fields=["parent", "user"],
		limit_page_length=len(names) * 3,
	)
	emails = {}
	for r in rows:
		if r.parent and r.user and r.parent not in emails:
			emails[r.parent] = r.user
	return emails


def _invoice_stats(names):
	"""ملخص مالي لكل عميل من الفواتير المرحّلة فقط — تجميع واحد."""
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		select customer,
			count(*) as invoice_count,
			coalesce(sum(grand_total), 0) as billed_total,
			coalesce(sum(outstanding_amount), 0) as outstanding_total
		from `tabSales Invoice`
		where docstatus = 1 and customer in %(names)s
		group by customer
		""",
		{"names": names},
		as_dict=True,
	)
	return {r.customer: r for r in rows}


def _recent_invoices(names):
	"""أحدث الفواتير المرحّلة لكل عميل (للوحة التفاصيل) — استعلام واحد."""
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		select customer, name, posting_date, grand_total, outstanding_amount, status
		from `tabSales Invoice`
		where docstatus = 1 and customer in %(names)s
		order by posting_date desc, creation desc
		limit 200
		""",
		{"names": names},
		as_dict=True,
	)
	grouped = {}
	for r in rows:
		entry = {
			"name": r.name,
			"date": str(r.posting_date or ""),
			"date_display": frappe.utils.format_date(r.posting_date, "d MMMM yyyy")
			if r.posting_date
			else "",
			"total_display": f"{float(r.grand_total or 0):,.2f} ج.م",
			"outstanding_display": f"{float(r.outstanding_amount or 0):,.2f} ج.م",
			"status": r.status or "",
		}
		if len(grouped.setdefault(r.customer, [])) < RECENT_INVOICES_LIMIT:
			grouped[r.customer].append(entry)
	return grouped
