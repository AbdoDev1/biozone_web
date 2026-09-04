import frappe

from biozone_web.utils import get_header_context, require_staff_access

RECENT_MOVEMENTS_LIMIT = 8


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "dashboard"
	context.update(get_header_context())

	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.total_items = frappe.db.count("Item", {"disabled": 0})

	# أصناف منخفضة المخزون: بنجمع actual_qty لكل صنف من Bin ونقارنه بحد
	# أدنى معرّف لنفس الصنف/المخزن في Item Reorder (child table بتاعة
	# Item). أي صنف ملوش حد أدنى معرّف أصلًا مش بيتحسب هنا خالص — ده تبسيط
	# مؤقت لحد ما يتحدد حد أدنى موحّد أو لكل صنف بشكل كامل مع صاحب المشروع.
	low_stock_rows = frappe.db.sql(
		"""
		select b.item_code
		from `tabBin` b
		inner join `tabItem Reorder` r
			on r.parent = b.item_code and r.warehouse = b.warehouse
		where b.actual_qty < r.warehouse_reorder_level
		group by b.item_code
		""",
		as_dict=True,
	)
	context.low_stock_count = len(low_stock_rows)

	context.today_movements_count = frappe.db.count(
		"Stock Ledger Entry", {"posting_date": frappe.utils.today()}
	)

	# ملحوظة: الديزاين اللي وصل من Stitch فيه 3 كروت KPI بس (مش 4 زي
	# البرومبت الأصلي) — "آخر حركة مخزون" اتشالت من الكروت، وبتظهر بدل
	# كده كأحدث صف في جدول "آخر حركات المخزون" تحت. لو الكارت ده اتضاف
	# لاحقًا في نسخة تصميم تانية، هنحتاج نرجّع query التاريخ هنا.

	# آخر حركات المخزون: أحدث صفوف Stock Ledger Entry حقيقية، مع اسم
	# الصنف. النوع (وارد/صادر) بيتحدد من إشارة actual_qty، مش من حقل
	# منفصل — نفس المنطق المتفق عليه لتبويب "سجل الحركات" في B6b.
	movements = frappe.db.sql(
		"""
		select
			sle.item_code, it.item_name, sle.actual_qty, sle.creation,
			sle.warehouse, sle.voucher_no, sle.stock_uom,
			u.full_name as staff_name
		from `tabStock Ledger Entry` sle
		inner join `tabItem` it on it.item_code = sle.item_code
		left join `tabUser` u on u.name = sle.owner
		order by sle.creation desc
		limit %(limit)s
		""",
		{"limit": RECENT_MOVEMENTS_LIMIT},
		as_dict=True,
	)
	for m in movements:
		m["direction"] = "in" if m["actual_qty"] >= 0 else "out"
		m["qty_display"] = abs(m["actual_qty"])
		m["ago"] = frappe.utils.pretty_date(m["creation"])
	context.movements = movements

	return context
