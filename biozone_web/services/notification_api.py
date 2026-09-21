"""Staff bell endpoints (Phase 1). Session user only, POST only.

Thin wrappers over :mod:`biozone_web.services.notification_core` —
the staff guard stays here. Deliberately not Frappe's
``get_notification_logs`` / ``mark_as_read`` (theirs is http-cached and
accepts GET). ``Cache-Control: no-store`` is the framework default for
API responses (see ``frappe/app.py``).
"""

import frappe

from biozone_web.services import notification_core as core
from biozone_web.utils import require_staff_access


@frappe.whitelist(methods=["POST"])
def notif_poll():
	"""Unread counter for the staff bell. One indexed query."""
	require_staff_access()
	return {"ok": True, "unread_count": core.poll_count(frappe.session.user)}


@frappe.whitelist(methods=["POST"])
def notif_list(limit=10):
	"""Latest unread rows for the open bell. One query."""
	require_staff_access()
	return {"ok": True, "items": core.list_rows(frappe.session.user, limit)}


@frappe.whitelist(methods=["POST"])
def notif_mark_read(name):
	"""Mark one row read. Cross-user names are a silent no-op (IDOR-safe)."""
	require_staff_access()
	core.mark_one(frappe.session.user, name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def notif_mark_all_read():
	"""Mark all own rows read."""
	require_staff_access()
	core.mark_all(frappe.session.user)
	return {"ok": True}
