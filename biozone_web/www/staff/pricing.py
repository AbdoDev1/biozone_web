import frappe

from biozone_web.utils import get_header_context, require_staff_access

PAGE_SIZE = 20


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "pricing"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	# نفس سبب csrf_token الصريح الموجود في items.py/stock.py: الصفحة مش
	# extending من web.html بتاع Frappe، يعني window.frappe.csrf_token
	# مش متوفر تلقائي.
	context.csrf_token = frappe.sessions.get_csrf_token()

	tab = frappe.form_dict.get("tab") or "groups"
	if tab not in ("groups", "discounts"):
		tab = "groups"
	context.tab = tab

	context.groups = frappe.get_all(
		"Customer Group",
		filters={"is_group": 0},
		fields=["name", "disabled"],
		order_by="disabled asc, name asc",
	)

	if tab == "discounts":
		_load_discounts_tab(context)

	return context


def _load_discounts_tab(context):
	active_groups = [g for g in context.groups if not g.disabled]
	context.active_groups = active_groups

	selected_group = frappe.form_dict.get("group")
	if not selected_group or selected_group not in [g.name for g in active_groups]:
		selected_group = active_groups[0].name if active_groups else None
	context.selected_group = selected_group

	search_term = (frappe.form_dict.get("q") or "").strip()
	context.search_term = search_term

	try:
		page = int(frappe.form_dict.get("page") or 1)
	except (TypeError, ValueError):
		page = 1
	page = max(page, 1)

	if not selected_group:
		# مفيش ولا فئة مفعّلة — مفيش أصناف نعرضها، بلا استعلامات.
		context.items = []
		context.total_count = 0
		context.page = 1
		context.has_prev = False
		context.has_next = False
		return

	base_where = "`tabItem`.`disabled` = 0"
	params: dict = {}
	if search_term:
		# البحث الحر: الاسم أو الكود أو الباركود (OR) في استعلام واحد.
		# الباركود عبر EXISTS على جدول Item Barcode بدل رحلة IN
		# منفصلة (نمط items.py)، والقيمة مربوطة كبارامتر %(term)s
		# فلا حقن SQL من مدخل المستخدم.
		base_where += """ and (
			`tabItem`.`item_name` like %(term)s
			or `tabItem`.`item_code` like %(term)s
			or exists (
				select 1 from `tabItem Barcode` bc
				where bc.parent = `tabItem`.`name`
					and bc.parenttype = 'Item'
					and bc.barcode like %(term)s
			)
		)"""
		params["term"] = f"%{search_term}%"

	total_row = frappe.db.sql(
		f"select count(*) as c from `tabItem` where {base_where}",
		params,
		as_dict=True,
	)
	total_count = total_row[0].c if total_row else 0
	items = frappe.db.sql(
		f"""select `tabItem`.`item_code`, `tabItem`.`item_name`, `tabItem`.`item_group`
			from `tabItem` where {base_where}
			order by `tabItem`.`item_name` asc
			limit %(limit)s offset %(offset)s""",
		{**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE},
		as_dict=True,
	)

	item_codes = [i.item_code for i in items]
	discount_map = {}
	if item_codes:
		rows = frappe.db.sql(
			"""
			select pri.item_code, pr.discount_percentage
			from `tabPricing Rule Item Code` pri
			inner join `tabPricing Rule` pr on pr.name = pri.parent
			where pr.apply_on = 'Item Code'
				and pr.disable = 0
				and pr.customer_group = %(group)s
				and pri.item_code in %(codes)s
			""",
			{"group": selected_group, "codes": item_codes},
			as_dict=True,
		)
		discount_map = {r.item_code: r.discount_percentage for r in rows}

	for it in items:
		it["discount_percent"] = discount_map.get(it.item_code) or 0

	context.items = items
	context.total_count = total_count
	context.page = page
	context.has_prev = page > 1
	context.has_next = page * PAGE_SIZE < total_count
	context.prev_page = page - 1
	context.next_page = page + 1
