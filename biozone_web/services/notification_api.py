"""Staff bell endpoints (Phase 1). Session user only, POST only.

Deliberately not Frappe's ``get_notification_logs`` / ``mark_as_read``
(theirs is http-cached and accepts GET). ``Cache-Control: no-store`` is
the framework default for API responses (see ``frappe/app.py``).
"""

import frappe

from biozone_web.hooks import _BZ_NOTIFICATION_TYPES
from biozone_web.utils import require_staff_access

_LIST_FIELDS = ["name", "type", "title", "link", "document_name",
                "for_user", "creation", "read"]
_BZ_TYPES = list(_BZ_NOTIFICATION_TYPES)


@frappe.whitelist(methods=["POST"])
def notif_poll():
	"""Unread counter for the staff bell. One indexed query."""
	require_staff_access()
	return {"ok": True,
	        "unread_count": frappe.db.count(
		        "Notification Log",
		        {"for_user": frappe.session.user, "read": 0,
		         "type": ("in", _BZ_TYPES)})}


@frappe.whitelist(methods=["POST"])
def notif_list(limit=10):
	"""Latest *unread* rows for the open bell. One query.

	Read rows never reappear: the bell is a to-do list, not history
	(regression guard T1-23). The empty state is the meaningful "all
	caught up" signal.
	"""
	require_staff_access()
	try:
		limit = int(limit)
	except (TypeError, ValueError):
		limit = 10
	limit = max(1, min(limit, 20))
	items = frappe.db.get_list("Notification Log",
	                           filters={"for_user": frappe.session.user,
	                                    "read": 0,
	                                    "type": ("in", _BZ_TYPES)},
	                           fields=_LIST_FIELDS,
	                           order_by="creation desc",
	                           limit=limit)
	return {"ok": True, "items": items}


@frappe.whitelist(methods=["POST"])
def notif_mark_read(name):
	"""Mark one row read. Cross-user names are a silent no-op (IDOR-safe)."""
	require_staff_access()
	name = (name or "").strip()
	if name:
		frappe.db.set_value("Notification Log",
		                    {"name": name,
		                     "for_user": frappe.session.user,
		                     "read": 0},
		                    "read", 1, update_modified=False)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def notif_mark_all_read():
	"""Mark all own rows read."""
	require_staff_access()
	frappe.db.set_value("Notification Log",
	                    {"for_user": frappe.session.user, "read": 0},
	                    "read", 1, update_modified=False)
	return {"ok": True}
