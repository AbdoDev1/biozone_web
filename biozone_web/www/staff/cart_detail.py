import frappe
from frappe import _

from biozone_web.services.cart_staff import get_cart_detail, require_cart_viewer
from biozone_web.utils import (
	get_csrf_token_safe,
	get_header_context,
	render_state_page,
)

ACTION_LABELS = {
	"View": "عرض",
	"Copy": "نسخ",
	"CSV": "CSV",
	"Recheck": "إعادة فحص",
}


def get_context(context):
	require_cart_viewer()
	context.no_cache = 1
	context.active_page = "carts"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = get_csrf_token_safe()
	context.error_state = False

	cart_name = (frappe.form_dict.get("cart_name") or "").strip()
	if not cart_name or not frappe.db.exists("BZ Cart", cart_name):
		return render_state_page(
			context,
			_("السلة غير موجودة"),
			_("السلة المطلوبة غير موجودة أو تم حذفها وفق سياسة الاحتفاظ."),
			http_status_code=404,
			back_url="/staff/carts",
			back_label=_("العودة إلى قائمة السلال"),
		)

	state = get_cart_detail(cart_name)
	context.cart = state["cart"]
	context.quote = state["quote"]
	context.customer = state["customer"]
	context.access_log = state["access_log"]
	context.state = state
	context.action_labels = ACTION_LABELS
	context.last_activity_display = _format_dt(state["cart"].get("last_activity_on"))
	context.expires_display = _format_dt(state["cart"].get("expires_on"))
	context.converted_display = _format_dt(state["cart"].get("converted_on"))
	return context


def _format_dt(value):
	if not value:
		return "—"
	try:
		return frappe.utils.format_datetime(value, "dd MMM yyyy HH:mm")
	except Exception:
		return str(value)
