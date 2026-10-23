from urllib.parse import urlencode

import frappe

from biozone_web.services.cart_staff import list_carts, require_cart_viewer
from biozone_web.utils import get_csrf_token_safe, get_header_context

STATUS_LABELS = {
	"Active": "نشطة",
	"Converted": "محوّلة",
	"Expired": "منتهية",
	"All": "الكل",
}
STATUS_CLASSES = {
	"Active": "bg-emerald-50 text-emerald-700 border-emerald-200",
	"Converted": "bg-blue-50 text-blue-700 border-blue-200",
	"Expired": "bg-slate-100 text-slate-600 border-slate-200",
}


def get_context(context):
	require_cart_viewer()
	context.no_cache = 1
	context.active_page = "carts"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = get_csrf_token_safe()

	st = (frappe.form_dict.get("st") or "Active").strip()
	if st not in STATUS_LABELS:
		st = "Active"
	q = (frappe.form_dict.get("q") or "").strip()
	try:
		page = int(frappe.form_dict.get("page") or 1)
	except (TypeError, ValueError):
		page = 1
	page = max(page, 1)

	data = list_carts(status=st, q=q, page=page)
	carts = []
	for row in data["carts"]:
		row = dict(row)
		row["last_activity_display"] = _format_dt(row.get("last_activity_on"))
		row["expires_display"] = _format_dt(row.get("expires_on"))
		row["converted_display"] = _format_dt(row.get("converted_on"))
		row["status_label"] = STATUS_LABELS.get(row.get("status"), row.get("status"))
		row["status_class"] = STATUS_CLASSES.get(row.get("status"), "")
		carts.append(row)

	context.carts = carts
	context.counts = data["counts"]
	context.total = data["total"]
	context.page = data["page"]
	context.pages = data["pages"]
	context.has_prev = data["page"] > 1
	context.has_next = data["page"] < data["pages"]
	context.prev_page = data["page"] - 1
	context.next_page = data["page"] + 1
	context.st = st
	context.q = q
	context.query_string = urlencode({"st": st, "q": q})
	context.status_labels = STATUS_LABELS
	return context


def _format_dt(value):
	if not value:
		return "—"
	try:
		return frappe.utils.format_datetime(value, "dd MMM yyyy HH:mm")
	except Exception:
		return str(value)
