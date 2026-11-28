import frappe

from biozone_web.services.assistant import assert_can_use_assistant, can_manage, is_enabled
from biozone_web.utils import get_header_context, require_staff_access


def get_context(context):
	require_staff_access()
	# الصفحة نفسها محمية بالدور — لا يكفي إخفاء الرابط.
	assert_can_use_assistant()
	context.no_cache = 1
	context.active_page = "assistant"
	context.update(get_header_context())
	# صفحات الستاف لا ترث web.html → CSRF يُمرَّر صراحةً (نمط staff/items.py).
	context.csrf_token = frappe.sessions.get_csrf_token()
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.assistant_enabled = 1 if is_enabled() else 0
	context.can_manage_assistant = 1 if can_manage() else 0
	return context
