"""A2: per-IP rate limiting for guest auth endpoints (D1-D11).

Key source: CF-Connecting-IP only inside the documented Cloudflare chain
(see D1/D11). No trust in X-Forwarded-For. Missing/invalid header falls
back to a separate strict shared key -- never to an empty key.

Counter primitive (D4): SET key 0 EX ttl NX (single atomic command:
create-if-absent WITH ttl) then INCRBY. A key can never exist without a
TTL because creation and TTL happen in the same atomic command; there is
no separate EXPIRE step to lose. Documented residual: concurrent first
hits may over-count by one (same guarantee level as Frappe's own
decorator) -- accepted, not hidden.

429 contract (D7): returned as a plain dict with http_status_code set, no
throw -- frappe.handle_exception would replace any thrown 429 body with a
generic "Too Many Requests" (app.py:399-400). Same pattern as 1a's 503.

Redis failure (D6): signup/forgot -> 503 service response; login ->
fail-open (proceed) with the failure logged. No passwords, emails, or
reset tokens are ever logged or placed raw in key names (email keys use
sha256 of the normalized address).
"""

import hashlib
import ipaddress
from functools import wraps

import frappe

RATE_LIMITED_CODE = "RATE_LIMITED"
SERVICE_UNAVAILABLE_CODE = "SERVICE_TEMPORARILY_UNAVAILABLE"

RATE_LIMITED_MESSAGE = "تم تجاوز عدد المحاولات المسموح بها، حاول لاحقاً."
SERVICE_UNAVAILABLE_MESSAGE = "الخدمة مشغولة مؤقتًا، يرجى المحاولة بعد لحظات"

# (limit, window_seconds) -- starting values, tunable without shape change.
LIMITS = {
	"biozone_login": {"ip": (20, 3600)},
	"biozone_sign_up": {"ip": (10, 3600)},
	"biozone_forgot_password": {"ip": (5, 3600), "email": (3, 3600)},
}

# Strict shared key when the CF header is absent/invalid (D2). Deliberately
# tight: an attacker stripping the header lands here, and legitimate
# traffic always carries the header behind Cloudflare.
FALLBACK_LIMIT = (5, 3600)
FALLBACK_IDENT = "untrusted"

# Redis-failure policy per endpoint (D6): "open" proceeds, "closed" 503s.
FAILURE_POLICY = {
	"biozone_login": "open",
	"biozone_sign_up": "closed",
	"biozone_forgot_password": "closed",
}


def normalize_ip(raw):
	"""Canonicalize an IP string, or (None, False) when unusable.

	Uses stdlib ipaddress only: v4/v6 are reduced to one stable textual
	form (so keying is stable per acceptance #9); anything else -- empty,
	non-string, garbage, hostnames -- is rejected toward the fallback.
	"""
	if not raw or not isinstance(raw, str):
		return None, False
	try:
		return str(ipaddress.ip_address(raw.strip())), True
	except ValueError:
		return None, False


def get_client_ip():
	"""Extract the rate-limit identity: (ip_string, trusted).

	trusted=True only for a syntactically valid CF-Connecting-IP. Missing
	header, invalid value, or no request context at all -> (None, False)
	and the caller must use the shared fallback key. Never reads
	X-Forwarded-For (spoofable, D1).
	"""
	try:
		raw = frappe.get_request_header("CF-Connecting-IP")
	except Exception:
		return None, False
	return normalize_ip(raw)


def _email_hash(email):
	return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _hit(namespaced_key, ttl):
	"""Atomic increment with guaranteed TTL. Returns the new count."""
	full_key = frappe.cache.make_key(namespaced_key)
	frappe.cache.set(full_key, 0, ex=ttl, nx=True)
	return int(frappe.cache.incrby(full_key, 1))


def check_rate_limit(fn_name, email=None):
	"""Decide a request: ("allow"|"deny"|"unavailable", retry_after).

	"deny" carries the window seconds as retry_after. "unavailable" means
	the Redis backend itself failed (policy applied by the caller).
	"""
	cfg = LIMITS[fn_name]
	counters = []
	ip, trusted = get_client_ip()
	if trusted:
		limit, ttl = cfg["ip"]
		counters.append((f"rl:{fn_name}:ip:{ip}", limit, ttl))
	else:
		limit, ttl = FALLBACK_LIMIT
		counters.append((f"rl:{fn_name}:ip:{FALLBACK_IDENT}", limit, ttl))
	if email and "email" in cfg:
		limit, ttl = cfg["email"]
		counters.append((f"rl:{fn_name}:email:{_email_hash(email)}", limit, ttl))
	try:
		for key, limit, ttl in counters:
			if _hit(key, ttl) > limit:
				return "deny", ttl
		return "allow", 0
	except Exception:
		frappe.log_error(
			title=f"Rate limit backend failed: {fn_name}",
			message=frappe.get_traceback(),
		)
		return "unavailable", 0


def _limited_response(retry_after):
	frappe.local.response["http_status_code"] = 429
	return {
		"ok": False,
		"error": RATE_LIMITED_MESSAGE,
		"error_code": RATE_LIMITED_CODE,
		"retry_after": retry_after,
	}


def _unavailable_response():
	frappe.local.response["http_status_code"] = 503
	return {
		"ok": False,
		"error": SERVICE_UNAVAILABLE_MESSAGE,
		"error_code": SERVICE_UNAVAILABLE_CODE,
	}


def rate_limited(fn_name):
	"""Decorator gating one whitelisted endpoint (D10 scope).

	Must sit BELOW @frappe.whitelist (closest to the function) so the
	command name and the methods map keep working via functools.wraps.
	The check runs before any DB write or authenticate call.
	"""

	def deco(fn):
		@wraps(fn)
		def wrapper(*args, **kwargs):
			try:
				form_email = frappe.form_dict.get("email")
			except Exception:
				form_email = None
			email = kwargs.get("email") or form_email
			decision, retry_after = check_rate_limit(fn_name, email=email)
			if decision == "allow":
				return fn(*args, **kwargs)
			if decision == "deny":
				return _limited_response(retry_after)
			if FAILURE_POLICY.get(fn_name) == "closed":
				return _unavailable_response()
			return fn(*args, **kwargs)

		return wrapper

	return deco
