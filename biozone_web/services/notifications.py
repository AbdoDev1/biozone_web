"""Single entry point for Biozone in-app notifications (Phase 1).

Rule: no business code creates ``Notification Log`` directly. Everything
goes through :func:`notify`. New events are registry rows + one call.
"""

import frappe
from urllib.parse import urlparse

from biozone_web.hooks import _BZ_NOTIFICATION_TYPES

SAVE_POINT = "bz_ntf_sp"
FLAG_KEY = "biozone_notifications_enabled"

# Registry: data only, no classes. ``audience`` is "staff" | "order_owner"
# | callable(ctx) -> list[str]. ``dedupe`` mirrors Frappe's ``dedupe_on``;
# None means the event may legitimately repeat on the same document.
EVENTS = {
	"order_new": {
		"type": "BZ New Order",
		"audience": "staff",
		"title": "طلب جديد {order}",
		"link": lambda ref: f"/staff/order-prep?order={ref}",
		"dedupe": ("document_name", "type"),
		"sound": True,  # staff bell only; wired in Phase 2
	},
	"order_delivered": {
		"type": "BZ Delivered",
		"audience": "order_owner",
		"reference_doctype": "Sales Order",
		"title": "تم تسليم طلبك {order}",
		"link": lambda ref: f"/account/orders/{ref}",
		"dedupe": ("document_name", "type"),
	},
	"order_cancelled": {
		"type": "BZ Order Cancelled",
		"audience": "order_owner",
		"reference_doctype": "Sales Order",
		"title": "تم إلغاء طلبك {order}",
		"link": lambda ref: f"/account/orders/{ref}",
		"dedupe": ("document_name", "type"),
	},
	"order_returned": {
		"type": "BZ Order Returned",
		"audience": "order_owner",
		"reference_doctype": "Sales Order",
		"title": "تم قبول المرتجع على طلبك {order}",
		"link": lambda ref, ctx: f"/account/returns/{ctx['credit']}",
		"link_needs_context": True,
		"dedupe": None,  # كل مرتجع جزئي حدث مقصود مستقل — لا كتم للاحق
	},
	"attention_raised": {
		"type": "BZ Escalation",
		"audience": "staff",
		"title": "طلب يحتاج انتباه {order}",
		"link": lambda ref: f"/staff/order-prep?order={ref}",
		"dedupe": None,  # a re-raise after clearing is a new deliberate action
	},
	"escalation_reminder": {
		"type": "BZ Escalation Reminder",
		"audience": "staff",
		"title": "تذكير: طلب ما زال بلا معالجة {order}",
		"link": lambda ref: f"/staff/order-prep?order={ref}",
		"dedupe": ("document_name", "type"),  # exactly one reminder per order
	},
}


def notifications_enabled():
	"""Kill switch. Absent flag == off."""
	try:
		val = frappe.get_conf().get(FLAG_KEY, 0)
	except Exception:
		return False
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, float)):
		return bool(val)
	if isinstance(val, str):
		return val.strip().lower() in ("1", "true", "yes", "on")
	return False


def recipients_for(event, reference_name=None, actor=None):
	"""List of recipient *emails* for an event. Actor is always excluded."""
	aud = event.get("audience")
	if callable(aud):
		try:
			emails = aud({"reference_name": reference_name, "actor": actor}) or []
		except Exception:
			frappe.log_error(title="Biozone notification audience failed")
			return []
	elif aud == "staff":
		emails = _staff_emails()
	elif aud == "order_owner":
		emails = _owner_emails(event.get("reference_doctype", "Sales Order"),
		                       reference_name)
	else:
		frappe.log_error(title=f"Biozone unknown audience: {aud}")
		return []
	excluded = _excluded_emails(actor)
	return sorted({e for e in emails if e and e.strip().lower() not in excluded})


def notify(event_key, *, reference_doctype, reference_name, context=None, actor=None):
	"""Synchronous insert inside a savepoint. Never breaks the caller.

	Must run inside the transaction, last write before commit, outside
	any Administrator window. Off-flag, unknown events, bad links and
	DB failures all degrade to 0 + an Error Log line.
	"""
	if not notifications_enabled():
		return 0
	event = EVENTS.get(event_key)
	if not event:
		frappe.log_error(title=f"Biozone unknown notification event: {event_key}")
		return 0
	actor_user = actor or frappe.session.user
	emails = recipients_for(event, reference_name, actor_user)
	if not emails:
		if event.get("staff_event", event.get("audience") == "staff"):
			frappe.log_error(title="Biozone notification: no recipients",
			                 message=f"event={event_key} ref={reference_name}")
		return 0
	try:
		title = _render_title(event, context or {})
	except Exception:
		frappe.log_error(title=f"Biozone notification bad context: {event_key}")
		return 0
	link = _safe_link(event, reference_name, context)
	if link is None:
		frappe.log_error(title=f"Biozone notification bad link: {event_key}",
		                 message=f"ref={reference_name}")
		return 0
	doc = {
		"doctype": "Notification Log",
		"type": event["type"],
		"title": title,
		"subject": title,
		"document_type": reference_doctype,
		"document_name": reference_name,
		"from_user": actor_user,
		"link": link,
	}
	if event.get("dedupe"):
		doc["dedupe_on"] = list(event["dedupe"])
	frappe.db.savepoint(SAVE_POINT)
	try:
		return _create_rows(doc, emails)
	except Exception:
		frappe.db.rollback(save_point=SAVE_POINT)
		frappe.log_error(title=f"Biozone notification failed: {event_key}",
		                 message=frappe.get_traceback())
		return 0


def _staff_emails():
	rows = frappe.get_all("User",
	                      filters={"user_type": "System User",
	                               "enabled": 1,
	                               "name": ("!=", "Administrator")},
	                      fields=["name", "email"])
	return [r.email for r in rows if r.email]


def _owner_emails(doctype, ref):
	if not ref:
		return []
	owner = frappe.db.get_value(doctype, ref, "owner")
	if not owner or owner == "Administrator":
		return []
	info = frappe.db.get_value("User", owner, ["user_type", "enabled", "email"],
	                           as_dict=True)
	if not info or info.user_type != "Website User" or not info.enabled:
		return []
	return [info.email or owner]


def _excluded_emails(actor):
	out = {"administrator"}
	if actor:
		out.add(str(actor).strip().lower())
		email = frappe.db.get_value("User", actor, "email")
		if email:
			out.add(str(email).strip().lower())
	return out


def _render_title(event, context):
	clean = {k: str(v) for k, v in context.items()}
	return event.get("title", "").format(**clean)


def _safe_link(event, reference_name, context=None):
	try:
		if event.get("link_needs_context"):
			raw = event["link"](reference_name, context or {})
		else:
			raw = event["link"](reference_name)
	except Exception:
		return None
	if not isinstance(raw, str):
		return None
	parsed = urlparse(raw)
	if parsed.scheme or parsed.netloc:
		return None
	if not parsed.path.startswith("/") or parsed.path.startswith("//"):
		return None
	return raw


def _create_rows(doc, emails):
	"""Synchronous insert via Frappe core. Returns rows actually created."""
	from frappe.desk.doctype.notification_log.notification_log import (
		make_notification_logs)
	key = {"type": doc["type"], "document_name": doc["document_name"],
	       "for_user": ("in", list(emails))}
	before = frappe.db.count("Notification Log", key)
	make_notification_logs(dict(doc), list(emails))
	after = frappe.db.count("Notification Log", key)
	return max(after - before, 0)
