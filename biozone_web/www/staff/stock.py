import json

import frappe

from biozone_web.utils import get_default_warehouse, get_header_context, require_staff_access

PAGE_SIZE = 20
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _to_arabic_digits(number):
	return str(number).translate(ARABIC_DIGITS)


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "stock"
	context.update(get_header_context())

	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = frappe.sessions.get_csrf_token()

	warehouse = get_default_warehouse()
	context.warehouse = warehouse

	search_term = (frappe.form_dict.get("q") or "").strip()
	low_only = frappe.form_dict.get("low_only") == "1"
	try:
		page = int(frappe.form_dict.get("page") or 1)
	except (TypeError, ValueError):
		page = 1
	page = max(page, 1)

	# Current balances, one row per active Item, joined against the
	# single implicit warehouse's Bin (0 if the item has no Bin row yet
	# — i.e. it has never moved in/out of stock).
	# الأداء: الترقيم في SQL نفسه (LIMIT/OFFSET) بدل جلب كل الأصناف
	# النشطة وتقطيعها في بايثون. منطق الحالة مطابق تمامًا للنسخة القديمة:
	# (1) الرصيد ≤ صفر → غير متوفر. (2) حد إعادة طلب حقيقي (غير صفري)
	# والرصيد تحته → منخفض. (3) غير ذلك → متوفر. الـGROUP BY دفاعي فقط
	# (لا توجد حاليًا أصناف بصفوف إعادة طلب مكررة لنفس المخزن) حتى لا
	# يكسر أي تكرار مستقبلي دقة الصفحات.
	search_condition = (
		"and (it.item_name like %(term)s or it.item_code like %(term)s)" if search_term else ""
	)
	status_expr = """
		case
			when coalesce(b.actual_qty, 0) <= 0 then 'unavailable'
			when r.warehouse_reorder_level
				and coalesce(b.actual_qty, 0) < r.warehouse_reorder_level then 'low'
			else 'available'
		end
	"""
	base_tables = """
		from `tabItem` it
		left join `tabBin` b on b.item_code = it.item_code and b.warehouse = %(warehouse)s
		left join `tabItem Reorder` r
			on r.parent = it.item_code and r.warehouse = %(warehouse)s
		where it.disabled = 0
	"""
	params = {"warehouse": warehouse, "term": f"%{search_term}%"}

	# عدّادات الشارة والترقيم من استعلام تجميع واحد — بلا جلب أي صفوف.
	# الاستعلام الداخلي ينتج صفًا واحدًا لكل صنف مع حالته؛ الخارجي يعدّ
	# حسب الحالة. max(reorder) دفاعي فقط (لا توجد حاليًا صفوف إعادة طلب
	# مكررة لنفس الصنف/المخزن — متحقق فعليًا) حتى لا يكسر أي تكرار
	# مستقبلي دقة الصفحات.
	status_counts = frappe.db.sql(
		f"""
		select st, count(*) as c from (
			select
				case
					when coalesce(b.actual_qty, 0) <= 0 then 'unavailable'
					when max(r.warehouse_reorder_level)
						and coalesce(b.actual_qty, 0) < max(r.warehouse_reorder_level) then 'low'
					else 'available'
				end as st
			{base_tables} {search_condition}
			group by it.item_code, it.item_name, it.stock_uom, coalesce(b.actual_qty, 0)
		) t group by st
		""",
		params,
		as_dict=True,
	)
	count_by_status = {row.st: row.c for row in status_counts}
	total_matching = sum(count_by_status.values())
	low_stock_count = count_by_status.get("low", 0) + count_by_status.get("unavailable", 0)

	filtered_total = low_stock_count if low_only else total_matching
	total_pages = max((filtered_total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
	page = min(page, total_pages)
	start = (page - 1) * PAGE_SIZE

	# صفحة واحدة فقط من SQL — نفس الأعمدة والترتيب السابقين تمامًا.
	rows = frappe.db.sql(
		f"""
		select
			it.item_code, it.item_name, it.stock_uom,
			coalesce(b.actual_qty, 0) as actual_qty,
			max(r.warehouse_reorder_level) as reorder_level
		{base_tables} {search_condition}
		{"and (" + status_expr + ") in ('low', 'unavailable')" if low_only else ""}
		group by it.item_code, it.item_name, it.stock_uom, coalesce(b.actual_qty, 0)
		order by it.item_name asc
		limit %(limit)s offset %(offset)s
		""",
		{**params, "limit": PAGE_SIZE, "offset": start},
		as_dict=True,
	)

	for row in rows:
		if row["actual_qty"] <= 0:
			row["status"] = "unavailable"
			row["status_label"] = "غير متوفر"
		elif row["reorder_level"] and row["actual_qty"] < row["reorder_level"]:
			row["status"] = "low"
			row["status_label"] = "منخفض"
		else:
			row["status"] = "available"
			row["status_label"] = "متوفر"

	# rows هي صفحة واحدة جاهزة من SQL — نفس التسميات السابقة بلا تغيير.
	context.low_stock_count = low_stock_count
	context.stock_rows = rows

	context.search_term = search_term
	context.low_only = low_only
	context.total_count_ar = _to_arabic_digits(filtered_total)
	context.page = page
	context.has_prev = page > 1
	context.has_next = page < total_pages
	context.prev_page = page - 1
	context.next_page = page + 1

	# Item picker data for the "تسجيل حركة" panel: every active item's
	# code/name/stock_uom plus its valid alternate UOMs (for the unit
	# dropdown that changes per selected item). Embedded as JSON and
	# filtered client-side — kept whole-catalog on purpose: the picker
	# must search items beyond the current page, so truncating it here
	# would break the movement panel. The proper fix is a searchable
	# API endpoint (new whitelisted method + JS rewrite) — deferred as
	# larger work outside this batch; the main table above is now paged.
	all_items = frappe.get_all(
		"Item",
		fields=["item_code", "item_name", "stock_uom"],
		filters={"disabled": 0},
		order_by="item_name asc",
	)
	alt_uoms = frappe.get_all(
		"UOM Conversion Detail",
		fields=["parent", "uom"],
		filters={"parenttype": "Item"},
	)
	uoms_by_item = {}
	for row in alt_uoms:
		uoms_by_item.setdefault(row["parent"], set()).add(row["uom"])

	picker_items = []
	for it in all_items:
		units = {it["stock_uom"]} | uoms_by_item.get(it["item_code"], set())
		picker_items.append(
			{
				"item_code": it["item_code"],
				"item_name": it["item_name"],
				"units": sorted(units),
			}
		)
	context.items_json = json.dumps(picker_items, ensure_ascii=False)

	return context
