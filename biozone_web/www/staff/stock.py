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
	item_filters = {"disabled": 0}
	or_filters = None
	if search_term:
		or_filters = [
			["item_name", "like", f"%{search_term}%"],
			["item_code", "like", f"%{search_term}%"],
		]

	rows = frappe.db.sql(
		"""
		select
			it.item_code, it.item_name, it.stock_uom,
			coalesce(b.actual_qty, 0) as actual_qty,
			r.warehouse_reorder_level as reorder_level
		from `tabItem` it
		left join `tabBin` b on b.item_code = it.item_code and b.warehouse = %(warehouse)s
		left join `tabItem Reorder` r
			on r.parent = it.item_code and r.warehouse = %(warehouse)s
		where it.disabled = 0
			{search_condition}
		order by it.item_name asc
		""".format(
			search_condition=(
				"and (it.item_name like %(term)s or it.item_code like %(term)s)" if search_term else ""
			)
		),
		{"warehouse": warehouse, "term": f"%{search_term}%"},
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

	low_stock_rows = [r for r in rows if r["status"] in ("low", "unavailable")]
	context.low_stock_count = len(low_stock_rows)

	filtered = low_stock_rows if low_only else rows
	total_count = len(filtered)
	total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
	page = min(page, total_pages)
	start = (page - 1) * PAGE_SIZE
	context.stock_rows = filtered[start : start + PAGE_SIZE]

	context.search_term = search_term
	context.low_only = low_only
	context.total_count_ar = _to_arabic_digits(total_count)
	context.page = page
	context.has_prev = page > 1
	context.has_next = page < total_pages
	context.prev_page = page - 1
	context.next_page = page + 1

	context.total_items = frappe.db.count("Item", {"disabled": 0})

	# Item picker data for the "تسجيل حركة" panel: every active item's
	# code/name/stock_uom plus its valid alternate UOMs (for the unit
	# dropdown that changes per selected item). Embedded as JSON and
	# filtered client-side — fine at a few hundred items; would need a
	# real search endpoint if the catalog grows into the thousands.
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
