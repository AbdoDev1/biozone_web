import frappe
from frappe import _

from biozone_web.services.assistant import can_manage
from biozone_web.utils import get_header_context, require_staff_access


def get_context(context):
	require_staff_access()
	if not can_manage():
		frappe.throw(_("غير مصرح بإدارة إعدادات المساعد"), frappe.PermissionError)
	context.no_cache = 1
	context.active_page = "assistant"
	context.update(get_header_context())
	context.csrf_token = frappe.sessions.get_csrf_token()
	return context
