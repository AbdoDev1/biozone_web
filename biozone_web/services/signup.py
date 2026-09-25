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

# Username-collision suffix retry (§4-30): at most 5 username candidates in
# total -- the framework-derived base, then -2..-5. Same bounded
# range-style shape as the 1a loop above (total attempts, never open-ended).
# Single source of truth: suffixes fill attempts 2..MAX.
SIGNUP_USERNAME_MAX_ATTEMPTS = 5
SIGNUP_USERNAME_SUFFIXES = tuple(range(2, SIGNUP_USERNAME_MAX_ATTEMPTS + 1))

USERNAME_EXHAUSTED_MESSAGE = _("تعذر إنشاء الحساب لأن الاسم مستخدم حالياً ولا يتوفر اسم بديل ضمن المحاولات المسموحة. يرجى المحاولة لاحقاً.")


def _is_username_collision(exc, username):
	"""True only for a proven clash on the username unique key.

	Two signals, either suffices: (1) another row visibly holds the name
	(committed rival); (2) the DB error itself names the username key --
	this covers an uncommitted rival whose row exists-check cannot see yet
	(MariaDB reports `for key 'username'`). Anything else is some other
	unique violation and must surface exactly as today.
	"""
	if not username:
		return False
	if frappe.db.exists("User", {"username": username}):
		return True
	cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None) or exc
	return "key 'username'" in str(cause)


def _build_signup_user(email, full_name, phone, pwd, username=None):
	"""Signup User doc. Payload identical to the pre-collision path; the
	explicit username is set only for suffix candidates (None leaves the
	framework to derive, byte-identical to today)."""
	payload = {
		"doctype": "User",
		"email": email,
		"first_name": full_name,
		"phone": phone,
		"send_welcome_email": 0,
		"enabled": 1,
		"user_type": "Website User",
		"new_password": pwd,
		"roles": [
			{
				"doctype": "Has Role",
				"parentfield": "roles",
				"role": "Biozone Storefront Customer",
			}
		],
	}
	if username is not None:
		payload["username"] = username
	return frappe.get_doc(payload)


def create_signup_records(email, full_name, phone, pwd):
	"""One atomic unit: User (+ role) and Customer. No commit inside.

	Username-collision suffix retry: the first candidate leaves username
	unset so the framework derives exactly as before (sequential signups
	behave byte-identically). Only a proven unique collision on username
	(held row, or the DB error naming the username key for a still
	uncommitted rival) advances to -2..-5; anything else re-raises untouched.
	QueryDeadlockError and DuplicateEntryError are NOT caught here -- the
	1a loop owns them, unchanged.
	"""
	from biozone_web.utils import create_customer_for_user

	user = _build_signup_user(email, full_name, phone, pwd)
	try:
		user.insert(ignore_permissions=True)
	except frappe.UniqueValidationError as e:
		attempted = user.get("username") or ""
		if not _is_username_collision(e, attempted):
			# Unique violation on another field -- surface exactly as today.
			raise
		frappe.db.rollback()
		frappe.clear_messages()
		user = _insert_with_username_suffix(email, full_name, phone, pwd, attempted)
		if user is None:
			# Same HTTP code as today's failure class (417), Arabic body,
			# no internals leaked.
			frappe.throw(USERNAME_EXHAUSTED_MESSAGE)

	# Same lazy-path values and guard as the atomic signup path: the same
	# Portal User link find_customer_for_current_user expects, the default
	# category, staff_category_reviewed = 0 (shows in /staff/customers).
	# Deliberately before commit: any failure rolls the whole signup back
	# with no orphan user without a customer. Idempotent (returns binding).
	create_customer_for_user(email, full_name=full_name)


def _insert_with_username_suffix(email, full_name, phone, pwd, base):
	"""Try base-2..base-5. Returns the inserted doc, or None on exhaustion.

	Proactive exists-checks only skip ahead; the unique index stays the
	final judge inside each attempt. Rollback + message cleanup mirror 1a.
	"""
	for i in SIGNUP_USERNAME_SUFFIXES:
		username = f"{base}-{i}"
		if frappe.db.exists("User", {"username": username}):
			continue
		candidate = _build_signup_user(email, full_name, phone, pwd, username)
		try:
			candidate.insert(ignore_permissions=True)
			return candidate
		except frappe.UniqueValidationError as e:
			if not _is_username_collision(e, username):
				raise
			frappe.db.rollback()
			frappe.clear_messages()
			continue
	return None


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
