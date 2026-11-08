import frappe
from frappe.model.document import Document


class BZCartSnapshot(Document):
	pass


def on_doctype_update():
	"""Composite index for the lastUndo/expiry lookups.

	`frappe.db.add_index` is idempotent (`has_index` + `ADD INDEX IF NOT
	EXISTS`) and only runs on migrate/sync. It must stay in this hook:
	Frappe skips `on_doctype_update` entirely for `custom=1` DocTypes
	(doctype.py:561-562), which is why this DocType is `custom=0`.
	"""
	frappe.db.add_index(
		"BZ Cart Snapshot",
		["source_user", "status", "expires_on"],
		"bz_cart_snapshot_user_status_expiry",
	)
