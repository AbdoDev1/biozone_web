import json

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

	show = (frappe.form_dict.get("show") or "unreviewed").strip()
	if show not in ("unreviewed", "all"):
		show = "unreviewed"
	if not has_review_field:
		show = "all"
	context.show = show

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

	filters = _build_filters(show, search_term, has_review_field, selected_group)

	total_count = frappe.db.count("Customer", filters)
	context.total_count = total_count

	rows = frappe.get_all(
		"Customer",
		fields=["name", "customer_name", "customer_group", "category_assigned_by_staff", "creation"]
		if has_review_field
		else ["name", "customer_name", "customer_group", "creation"],
		filters=filters,
		order_by="creation asc",
		start=(page - 1) * PAGE_SIZE,
		page_length=PAGE_SIZE,
	)

	names = [r.name for r in rows]
	context.unreviewed_count = (
		frappe.db.count("Customer", _build_filters("unreviewed", "", has_review_field, "all"))
		if has_review_field
		else 0
	)
	emails = _portal_emails(names)
	stats = _invoice_stats(names)
	recents = _recent_invoices(names)

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
				"reviewed": bool(r.get("category_assigned_by_staff")),
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
	context.customers = customers
	context.customers_json = json.dumps(customers, ensure_ascii=False, default=str)

	context.page = page
	context.has_prev = page > 1
	context.has_next = page * PAGE_SIZE < total_count
	context.prev_page = page - 1
	context.next_page = page + 1
	context.total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)

	return context


def _build_filters(show, search_term, has_review_field, selected_group="all"):
	"""فلاتر العملاء: المراجعة + الفئة + البحث — كلها AND عبر get_all.

	البحث يُحل أولًا لأسماء عملاء (نفس أسلوب _load_orders في orders.py:
	استعلامات منفصلة ثم name IN (...)) حتى لا تختلط شروط OR الخاصة
	ب	البحث مع شرط المراجعة. القائمة لا تعتمد أبدًا على customer_group
	وحده لتحديد المراجعة — الفاصل هو category_assigned_by_staff فقط، وفلتر
	الفئة هنا تصفية عرض اختيارية لا علاقة لها بحالة المراجعة.
	"""
	filters = {}
	if show == "unreviewed" and has_review_field:
		filters["category_assigned_by_staff"] = ["!=", 1]
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
