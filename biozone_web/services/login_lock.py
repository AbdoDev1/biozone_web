"""B4: per-account redis mutex around login authenticate (Contract locked).

Why a lock, not a retry: a deadlock victim invalidates the whole
transaction (the savepoint itself is gone), so retrying inside it fails by
construction; and run_notifications lives inside the framework session
start (sessions.py:284-296) where no wrapper can separate it. Serializing
same-account concurrent logins removes the contention instead.

Contract: TTL=10s (measured holder p-max 1.56s x~6), waiter polls up to
3s/50ms then a 429 LOGIN_BUSY contract (never 500). Redis down =
fail-open (today's exact behavior) + logged, never fail-closed.
Keys carry sha256 of the normalized identifier only -- no emails, no
passwords, nothing logged per attempt.

Recorded UX note (non-blocking): with TTL 10s and a 3s wait budget, a
genuinely crashed holder surfaces as one or two consecutive 429s (~t3s,
~t6s) before success around ~t10s -- documented, not redesigned.

Recorded harness note: dev traffic carries no CF header, so any parallel
login measurement also consumes A2's shared fallback budget -- wipe rl:
keys between measurement batches or the numbers measure A2, not B4.
"""

import hashlib
import time
import uuid

import frappe

LOCK_TTL = 10
WAIT_BUDGET = 3.0
POLL_INTERVAL = 0.05
KEY_PREFIX = "b4:login:"

BUSY_CODE = "LOGIN_BUSY"
BUSY_MESSAGE = "الطلب قيد المعالجة، يرجى المحاولة بعد لحظات"
BUSY_RETRY_AFTER = 2

_RELEASE_IF_OWNER = """
if redis.call("get", KEYS[1]) == ARGV[1] then
	return redis.call("del", KEYS[1])
else
	return 0
end
"""


def lock_key(usr):
	"""One stable key per normalized identifier, shared by every login path.

	Normalization is strip()+lower()+sha256: identical typed credentials
	map to one key however they arrive (store/staff path, case, padding).
	Over-serialization of two hypothetical case-distinct usernames is
	harmless (bounded wait, never a failure) and documented in Contract.
	"""
	normalized = (usr or "").strip().lower() if isinstance(usr, str) else ""
	return KEY_PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _acquire_once(namespaced_key, token, ttl=LOCK_TTL):
	return bool(frappe.cache.set(namespaced_key, token, ex=ttl, nx=True))


def _release_if_owner(namespaced_key, token):
	frappe.cache.eval(_RELEASE_IF_OWNER, 1, namespaced_key, token)


def acquire_login_lock(usr, ttl=LOCK_TTL, budget=WAIT_BUDGET, poll=POLL_INTERVAL):
	"""Take the per-account lock, waiting up to budget.

	Returns (state, token): "locked" (holder -- must release), "busy"
	(budget exhausted -- caller returns the busy contract), "unguarded"
	(Redis failed -- caller proceeds exactly as today, fail-open).
	"""
	key = frappe.cache.make_key(lock_key(usr))
	token = uuid.uuid4().hex
	try:
		if _acquire_once(key, token, ttl):
			return "locked", token
		deadline = time.monotonic() + budget
		while time.monotonic() < deadline:
			time.sleep(poll)
			token = uuid.uuid4().hex
			try:
				if _acquire_once(key, token, ttl):
					return "locked", token
			except Exception:
				frappe.log_error(title="B4 login lock backend failed", message=frappe.get_traceback())
				return "unguarded", None
		return "busy", None
	except Exception:
		frappe.log_error(title="B4 login lock backend failed", message=frappe.get_traceback())
		return "unguarded", None


def release_login_lock(usr, token):
	"""Release only our own lock; no-op for unguarded/busy (token None)."""
	if not token:
		return
	try:
		_release_if_owner(frappe.cache.make_key(lock_key(usr)), token)
	except Exception:
		frappe.log_error(title="B4 login lock release failed", message=frappe.get_traceback())


def login_busy_response():
	"""429 contract for an exhausted wait budget (same envelope as A2)."""
	frappe.local.response["http_status_code"] = 429
	return {
		"ok": False,
		"error": BUSY_MESSAGE,
		"error_code": BUSY_CODE,
		"retry_after": BUSY_RETRY_AFTER,
	}
