"""Slice 1a: retry user creation on InnoDB deadlock during signup.

Single atomic unit per attempt: User (+ role) and Customer. No commit
inside — the caller (biozone_sign_up) keeps its single commit.
"""

import random
import time
import uuid

import frappe
from frappe import _

SIGNUP_MAX_ATTEMPTS = 3
# seconds to wait before attempt 2 and before attempt 3
SIGNUP_BACKOFF_RANGES = ((0.10, 0.20), (0.30, 0.60))
SIGNUP_UNAVAILABLE_CODE = "SIGNUP_TEMPORARILY_UNAVAILABLE"

DUPLICATE_MESSAGE = _("هذا البريد الإلكتروني مسجل بالفعل")


def create_signup_records(email, full_name, phone, pwd):
	"""One atomic unit: User (+ role) and Customer. No commit inside."""
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": full_name,
			"phone": phone,
			"send_welcome_email": 0,
			"enabled": 1,
			"user_type": "Website User",
			"new_password": pwd,
		}
	)

	user.insert(ignore_permissions=True)

	# NOTE (api.py): get_roles() does not exist on Document. Read the
	# child table directly instead of a missing helper.
	existing_roles = {r.role for r in user.get("roles", [])}

	if "Biozone Storefront Customer" not in existing_roles:
		user.add_roles("Biozone Storefront Customer")

	# Same lazy-path values and guard as the atomic signup path: the same
	# Portal User link find_customer_for_current_user expects, the default
	# category, staff_category_reviewed = 0 (shows in /staff/customers).
	# Deliberately before commit: any failure rolls the whole signup back
	# with no orphan user without a customer. Idempotent (returns binding).
	from biozone_web.utils import create_customer_for_user

	create_customer_for_user(email, full_name=full_name)


def signup_unavailable(request_id):
	frappe.local.response["http_status_code"] = 503
	return {
		"ok": False,
		"error": _("الخدمة مشغولة مؤقتًا، يرجى المحاولة بعد لحظات"),
		"error_code": SIGNUP_UNAVAILABLE_CODE,
		"request_id": request_id,
	}


def create_signup_with_retry(email, full_name, phone, pwd):
	"""Return None on success, or the 503 body when attempts are exhausted."""
	request_id = uuid.uuid4().hex
	log = frappe.logger("biozone_signup")

	for attempt in range(SIGNUP_MAX_ATTEMPTS):
		if attempt > 0 and frappe.db.exists("User", email):
			frappe.throw(DUPLICATE_MESSAGE)
		try:
			create_signup_records(email, full_name, phone, pwd)
			if attempt > 0:
				log.error(f"signup_retry_succeeded request_id={request_id} attempts={attempt + 1}")
			return None
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			frappe.clear_messages()
			if attempt == SIGNUP_MAX_ATTEMPTS - 1:
				log.error(f"signup_deadlock_exhausted request_id={request_id} attempts={attempt + 1}")
				return signup_unavailable(request_id)
			log.error(f"signup_deadlock_retry request_id={request_id} attempt={attempt + 1}")
			time.sleep(random.uniform(*SIGNUP_BACKOFF_RANGES[attempt]))
		except frappe.DuplicateEntryError:
			# True race between exists-check and insert: same duplicate
			# response as today, no retry.
			frappe.db.rollback()
			frappe.clear_messages()
			if frappe.db.exists("User", email):
				frappe.throw(DUPLICATE_MESSAGE)
			raise
