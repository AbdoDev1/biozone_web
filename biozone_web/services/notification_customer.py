"""Customer bell endpoints (Phase 2). Session user only, POST only.

Thin wrappers over :mod:`biozone_web.services.notification_core` with
the customer guard: logged-in enabled Website Users. Guests and System
Users are refused. No polling endpoint here by design — the customer
counter comes from the server-rendered header context; these three
serve the open panel only.
"""

import frappe

from biozone_web.services import notification_core as core
from biozone_web.utils import require_customer_access


@frappe.whitelist(methods=["POST"])
def notif_list(limit=10):
	"""Latest unread rows for the open customer bell. One query."""
	require_customer_access()
	return {"ok": True, "items": core.list_rows(frappe.session.user, limit)}


@frappe.whitelist(methods=["POST"])
def notif_mark_read(name):
	"""Mark one row read. Cross-user names are a silent no-op (IDOR-safe)."""
	require_customer_access()
	core.mark_one(frappe.session.user, name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def notif_mark_all_read():
	"""Mark all own rows read."""
	require_customer_access()
	core.mark_all(frappe.session.user)
	return {"ok": True}
