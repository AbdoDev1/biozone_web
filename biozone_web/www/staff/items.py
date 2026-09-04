import frappe

from biozone_web.utils import get_header_context, require_staff_access

PAGE_SIZE = 20


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "staff_items"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	# الصفحة مش extending من web.html بتاع Frappe (زي باقي صفحات biozone_web)،
	# يعني window.frappe.csrf_token مش متوفر تلقائي — بنمرره صراحة عشان
	# الـfetch calls بتاعة الحفظ/التعطيل تعدي فحص CSRF بتاع Frappe.
	context.csrf_token = frappe.sessions.get_csrf_token()

	page = frappe.utils.cint(frappe.form_dict.get("page")) or 1
	search_term = (frappe.form_dict.get("q") or "").strip()
	item_group = frappe.form_dict.get("group")
	brand = frappe.form_dict.get("brand")

	filters = {}
	if item_group:
		filters["item_group"] = item_group
	if brand:
		filters["brand"] = brand
	if search_term:
		filters["item_name"] = ["like", f"%{search_term}%"]

	total_count = frappe.db.count("Item", filters)
	items = frappe.get_all(
		"Item",
		fields=["item_code", "item_name", "item_group", "brand", "disabled"],
		filters=filters,
		order_by="item_name asc",
		start=(page - 1) * PAGE_SIZE,
		page_length=PAGE_SIZE,
	)

	item_codes = [i["item_code"] for i in items]

	# الكمية الحالية: مجموع actual_qty لكل صنف عبر كل المخازن — قراءة بس،
	# مفيش أي تعديل عليها من الشاشة دي (Bin هو مصدر الحقيقة الوحيد).
	stock_map = {}
	if item_codes:
		stock_rows = frappe.db.sql(
			"""
			select item_code, sum(actual_qty) as qty
			from `tabBin`
			where item_code in %(codes)s
			group by item_code
			""",
			{"codes": item_codes},
			as_dict=True,
		)
		stock_map = {r["item_code"]: r["qty"] for r in stock_rows}

	# السعر: سعر واحد بس لكل صنف (سعر الجمهور) من Item Price بقائمة
	# Standard Selling — لا يوجد أكتر من سعر مخزّن، زي ما اتفقنا.
	price_map = {}
	if item_codes:
		price_rows = frappe.db.sql(
			"""
			select item_code, price_list_rate
			from `tabItem Price`
			where price_list = 'Standard Selling' and item_code in %(codes)s
			""",
			{"codes": item_codes},
			as_dict=True,
		)
		price_map = {r["item_code"]: r["price_list_rate"] for r in price_rows}

	discounts_map = _get_active_discounts(item_codes)

	for it in items:
		it["stock_qty"] = stock_map.get(it["item_code"]) or 0
		it["price"] = price_map.get(it["item_code"])
		it["discounts"] = discounts_map.get(it["item_code"], [])

	context.items = items
	context.item_groups = frappe.get_all("Item Group", fields=["name"], order_by="name asc")
	context.brands = frappe.get_all("Brand", fields=["name"], order_by="name asc")
	context.search_term = search_term
	context.selected_group = item_group
	context.selected_brand = brand
	context.page = page
	context.has_prev = page > 1
	context.has_next = page * PAGE_SIZE < total_count
	context.prev_page = page - 1
	context.next_page = page + 1

	return context


def _get_active_discounts(item_codes):
	"""بيرجع dict: item_code -> [{group, percent}, ...] لكل خصم (Pricing
	Rule) نشط منطبق على الصنف، سواء مباشر (apply_on = Item Code) أو عن
	طريق المجموعة (apply_on = Item Group).

	ملحوظة مهمة: النسخة دي مبسّطة عمدًا — بتتجاهل شروط زي تاريخ السريان
	(valid_from/valid_upto)، الحد الأدنى للكمية، وحالة "أكتر من نوع حساب
	على نفس القاعدة". لازم تتراجع مقابل الإعداد الفعلي لـPricing Rule في
	السيرفر الحقيقي قبل الاعتماد عليها بشكل نهائي في B6d.
	"""
	if not item_codes:
		return {}

	result = {code: [] for code in item_codes}

	# خصومات مطبّقة مباشرة على كود الصنف
	direct_rules = frappe.db.sql(
		"""
		select pri.item_code, pr.discount_percentage, pr.customer_group
		from `tabPricing Rule Item Code` pri
		inner join `tabPricing Rule` pr on pr.name = pri.parent
		where pr.disable = 0 and pr.apply_on = 'Item Code'
			and pri.item_code in %(codes)s
		""",
		{"codes": item_codes},
		as_dict=True,
	)
	for r in direct_rules:
		result[r["item_code"]].append(
			{"group": r["customer_group"], "percent": r["discount_percentage"]}
		)

	# خصومات مطبّقة على مستوى المجموعة (Item Group) — بنجيبها لكل مجموعات
	# الأصناف الحالية، ونوزّعها على الأصناف اللي فعلاً في المجموعة دي
	item_group_map = frappe.get_all(
		"Item", filters={"item_code": ["in", item_codes]}, fields=["item_code", "item_group"]
	)
	group_to_items = {}
	for row in item_group_map:
		group_to_items.setdefault(row["item_group"], []).append(row["item_code"])

	if group_to_items:
		group_rules = frappe.db.sql(
			"""
			select prg.item_group, pr.discount_percentage, pr.customer_group
			from `tabPricing Rule Item Group` prg
			inner join `tabPricing Rule` pr on pr.name = prg.parent
			where pr.disable = 0 and pr.apply_on = 'Item Group'
				and prg.item_group in %(groups)s
			""",
			{"groups": list(group_to_items.keys())},
			as_dict=True,
		)
		for r in group_rules:
			for code in group_to_items.get(r["item_group"], []):
				result[code].append(
					{"group": r["customer_group"], "percent": r["discount_percentage"]}
				)

	return result
