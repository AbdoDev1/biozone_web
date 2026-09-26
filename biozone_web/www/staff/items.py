import frappe

from biozone_web.services.item_images import resolve_item_thumbnails
from biozone_web.utils import get_header_context, require_staff_access

PAGE_SIZE = 20


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "items"
	context.update(get_header_context())
	# قائمة الوحدات المفعّلة لدرج الصنف (اختيار بدل الكتابة الحرة — المرحلة 0).
	# تُمرَّر هنا قبل انقسام التبويبين لأن الدرج معروض في التبويبين معًا.
	context.uoms = frappe.get_all(
		"UOM", filters={"enabled": 1}, fields=["name"], order_by="name asc"
	)
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	# الصفحة مش extending من web.html بتاع Frappe (زي باقي صفحات biozone_web)،
	# يعني window.frappe.csrf_token مش متوفر تلقائي — بنمرره صراحة عشان
	# الـfetch calls بتاعة الحفظ/التعطيل تعدي فحص CSRF بتاع Frappe.
	context.csrf_token = frappe.sessions.get_csrf_token()

	tab = frappe.form_dict.get("tab") or "list"
	if tab not in ("list", "import", "units"):
		tab = "list"
	context.tab = tab

	if tab == "units":
		_load_units_tab(context)
		context.items = []
		context.item_groups = frappe.get_all("Item Group", fields=["name"], order_by="name asc")
		context.brands = []
		context.search_term = ""
		context.selected_group = None
		context.selected_brand = None
		context.page = 1
		context.has_prev = False
		context.has_next = False
		context.prev_page = 1
		context.next_page = 1
		return context

	if tab == "import":
		_load_import_tab(context)
		context.items = []
		context.item_groups = frappe.get_all("Item Group", fields=["name"], order_by="name asc")
		context.brands = []
		context.search_term = ""
		context.selected_group = None
		context.selected_brand = None
		context.page = 1
		context.has_prev = False
		context.has_next = False
		context.prev_page = 1
		context.next_page = 1
		return context

	page = frappe.utils.cint(frappe.form_dict.get("page")) or 1
	search_term = (frappe.form_dict.get("q") or "").strip()
	item_group = frappe.form_dict.get("group")
	brand = frappe.form_dict.get("brand")
	# رابط اسم الصنف من شاشة التجهيز (§8) يفتح تفاصيله في تبويب جديد عبر
	# ?code= — فلتر دقيق على الكود، لا على الاسم.
	exact_code = (frappe.form_dict.get("code") or "").strip()

	filters = {}
	if exact_code:
		filters["item_code"] = exact_code
	if item_group:
		filters["item_group"] = item_group
	if brand:
		filters["brand"] = brand
	# البحث الحر: الاسم أو الكود أو الباركود (OR) — مع بقاء فلاتر المجموعة/
	# العلامة/الكود الدقيق AND كما كانت. الباركود يُحل لأكواد أصناف عبر
	# جدول Item Barcode ثم يُضمَّن كـ item_code IN (...).
	or_filters = None
	if search_term:
		or_filters = [
			["item_name", "like", f"%{search_term}%"],
			["item_code", "like", f"%{search_term}%"],
		]
		barcode_codes = _find_item_codes_by_barcode(search_term)
		if barcode_codes:
			or_filters.append(["item_code", "in", barcode_codes])

	from frappe.query_builder.functions import Count

	total_count = frappe.qb.get_query(
		table="Item",
		filters=filters or None,
		or_filters=or_filters,
		fields=[Count("*")],
	).run()[0][0]
	items = frappe.get_all(
		"Item",
		fields=["item_code", "item_name", "item_group", "brand", "stock_uom", "disabled", "display_override"],
		filters=filters,
		or_filters=or_filters,
		order_by="item_name asc",
		start=(page - 1) * PAGE_SIZE,
		page_length=PAGE_SIZE,
	)

	item_codes = [i["item_code"] for i in items]

	# Phase-2: أول صف تحويل + الراية لكل صنف (للدرج) — دفعة واحدة.
	conv_map = {}
	if item_codes:
		for r in frappe.get_all(
			"UOM Conversion Detail",
			filters={"parent": ["in", item_codes], "parenttype": "Item"},
			fields=["parent", "uom", "conversion_factor", "min_qty"],
			order_by="idx asc",
		):
			if r.uom and r.parent not in conv_map:
				conv_map[r.parent] = {
					"uom": r.uom,
					"factor": r.conversion_factor,
					"min_qty": r.min_qty if r.min_qty not in (None, "") else 1,
				}

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

	# المصغرات دفعة واحدة (استعلامان مهما كان عدد الصفوف — بلا N+1).
	thumb_map = resolve_item_thumbnails(item_codes) if item_codes else {}

	# الباركودات الحالية لكل صنف (للعرض/التعديل في الدرج — بند 4).
	barcodes_map = {code: [] for code in item_codes}
	if item_codes:
		bc_rows = frappe.get_all(
			"Item Barcode",
			filters={"parent": ["in", item_codes], "parenttype": "Item"},
			fields=["parent", "barcode"],
		)
		for r in bc_rows:
			if r.barcode:
				barcodes_map.setdefault(r.parent, []).append(r.barcode)

	for it in items:
		it["stock_qty"] = stock_map.get(it["item_code"]) or 0
		it["price"] = price_map.get(it["item_code"])
		it["discounts"] = discounts_map.get(it["item_code"], [])
		it["barcodes"] = barcodes_map.get(it["item_code"], [])
		it["display_override"] = (it.get("display_override") or "inherit").strip() or "inherit"
		it["conversion"] = conv_map.get(it["item_code"])
		th = thumb_map.get(it["item_code"]) or {}
		it["thumbnail"] = th.get("thumbnail") or ""
		it["image"] = th.get("image") or ""

	context.items = items
	context.item_groups = frappe.get_all("Item Group", fields=["name"], order_by="name asc")
	context.brands = frappe.get_all("Brand", fields=["name"], order_by="name asc")
	context.search_term = search_term
	context.selected_code = exact_code
	context.selected_group = item_group
	context.selected_brand = brand
	context.page = page
	context.has_prev = page > 1
	context.has_next = page * PAGE_SIZE < total_count
	context.prev_page = page - 1
	context.next_page = page + 1

	return context


def _load_units_tab(context):
	"""سياق تبويب وحدات العرض: كل فئة عميل ورقية + وحدة عرضها الحالية.
	الغائب/الفارغ = الصغرى (نفس قاعدة المحلل). القراءة هنا للعرض فقط —
	الحفظ عبر staff_save_display_setting."""
	groups = frappe.get_all(
		"Customer Group",
		filters={"is_group": 0},
		fields=["name", "disabled", "display_unit"],
		order_by="name asc",
	)
	for g in groups:
		g["display_unit"] = (g.get("display_unit") or "small").strip() or "small"
	context.display_groups = groups


def _load_import_tab(context):
	"""سياق تبويب الاستيراد/التصدير: الفئات النشطة + المخزن الافتراضي
	المحلول حيًا + العملة + الحدود. قراءة فقط — أي فشل في حل المخزن
	يُعرض كخطأ إعداد واضح بدل التخمين (المهمة A)."""
	from biozone_web.utils import get_default_warehouse

	context.import_groups = frappe.get_all(
		"Customer Group",
		filters={"is_group": 0, "disabled": 0},
		fields=["name"],
		order_by="name asc",
	)
	try:
		context.import_warehouse = get_default_warehouse()
		context.import_warehouse_error = ""
	except Exception as e:
		context.import_warehouse = ""
		context.import_warehouse_error = str(e)
	context.import_price_list = "Standard Selling"
	context.import_max_mb = 5
	context.import_max_rows = 2000


def _find_item_codes_by_barcode(search_term):
	"""أكواد الأصناف المطابقة لباركود مدخل في البحث الحر.

	تطابق تام أولًا (الباركود معرّف دقيق — يتجنب ضجيج المطابقة الجزئية)،
	ثم جزئي عند انعدام التام. تُرجع قائمة أكواد فريدة (قد تكون فارغة).
	"""
	for barcode_filter in (search_term, ["like", f"%{search_term}%"]):
		rows = frappe.get_all(
			"Item Barcode",
			filters={"parenttype": "Item", "barcode": barcode_filter},
			fields=["parent"],
			limit_page_length=50,
		)
		codes = []
		for r in rows:
			if r.parent and r.parent not in codes:
				codes.append(r.parent)
		if codes:
			return codes
	return []


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
