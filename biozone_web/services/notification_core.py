"""Bell endpoint core: shared by the staff and customer thin wrappers.

No whitelisted functions here on purpose (review decision): each audience
gets its own guarded module, and both call these helpers with the session
user. Every helper filters by ``for_user`` + our types only.
"""

import frappe

from biozone_web.hooks import _BZ_NOTIFICATION_TYPES

_LIST_FIELDS = ["name", "type", "title", "link", "document_name",
                "for_user", "creation", "read"]
_BZ_TYPES = list(_BZ_NOTIFICATION_TYPES)


def poll_count(user):
	"""Unread counter. One indexed query."""
	return frappe.db.count(
		"Notification Log",
		{"for_user": user, "read": 0, "type": ("in", _BZ_TYPES)})


def list_rows(user, limit=10):
	"""Latest unread rows. One query."""
	try:
		limit = int(limit)
	except (TypeError, ValueError):
		limit = 10
	limit = max(1, min(limit, 20))
	return frappe.db.get_list("Notification Log",
	                          filters={"for_user": user, "read": 0,
	                                   "type": ("in", _BZ_TYPES)},
	                          fields=_LIST_FIELDS,
	                          order_by="creation desc",
	                          limit=limit)


def mark_one(user, name):
	"""Mark one row read. Cross-user names are a silent no-op (IDOR-safe)."""
	name = (name or "").strip()
	if name:
		frappe.db.set_value("Notification Log",
		                    {"name": name, "for_user": user, "read": 0},
		                    "read", 1, update_modified=False)


def mark_all(user):
	"""Mark all own rows read."""
	frappe.db.set_value("Notification Log",
	                    {"for_user": user, "read": 0},
	                    "read", 1, update_modified=False)
