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
	# دخول الموظفين: بكت IP مستقل عن المتجر (حتى لا يستهلك موظفٌ حصة عملاء)،
	# ويُعرض عدّاده في صفحة /staff/login عند 429.
	"biozone_staff_login": {"ip": (20, 3600)},
	"biozone_sign_up": {"ip": (10, 3600)},
	"biozone_forgot_password": {"ip": (5, 3600), "email": (3, 3600)},
	# تكلفة تسعير عالية + متاح للضيف: حدّ IP سخي للتصفح الطبيعي، وحدّ
	# مستخدم أعلى للمسجّلين. لا يمنع البيع عند تعطل Redis (open).
	"biozone_get_cart_prices": {"ip": (600, 3600), "user": (1200, 3600)},
	# التأكيد يتطلب تسجيل دخول: الحد لكل مستخدم، لا لكل IP (حتى لا يُعاقب
	# منشأة كاملة تشترك في عنوان واحد).
	"biozone_confirm_order": {"user": (60, 3600)},
}

# Strict shared key when the CF header is absent/invalid (D2). Deliberately
# tight: an attacker stripping the header lands here, and legitimate
# traffic always carries the header behind Cloudflare.
FALLBACK_LIMIT = (5, 3600)

# Nadir endpoints (storefront pricing) must stay usable behind a
# non-Cloudflare proxy / local dev: a per-endpoint fallback instead of 5/h.
FALLBACK_OVERRIDES = {
	"biozone_get_cart_prices": (300, 3600),
}
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


def _remaining_ttl(namespaced_key, window):
	"""الثواني المتبقية فعلًا على نافذة العدّاد (لا طول النافذة).

	العميل يعرض retry_after كما هو، فإرجاع 3600 دائمًا كان يقول «ساعة»
	حتى لو بقي دقيقة. أي فشل/قيمة غير صالحة → نافذة كاملة (سلوك آمن).
	"""
	try:
		full_key = frappe.cache.make_key(namespaced_key)
		ttl = int(frappe.cache.ttl(full_key))
	except Exception:
		return window
	if ttl <= 0 or ttl > window:
		return window
	return ttl


def _fallback_limit(fn_name):
	"""حدّ بكت السقف للطلبات غير الموثوقة المصدر (بلا هيدر Cloudflare).

	الافتراضي الصارم يبقى كما هو (5/ساعة) أيضًا في الإنتاج. يمكن لموقع
	تطوير رفعه صراحةً عبر site_config:
	    "biozone_rate_limit_fallback": {"biozone_login": 10}
	لا يعمل هذا التجاوز إلا إذا كُتب في إعداد الموقع — لا قيمة افتراضية جديدة.
	"""
	limit, ttl = FALLBACK_OVERRIDES.get(fn_name, FALLBACK_LIMIT)
	try:
		overrides = frappe.get_conf().get("biozone_rate_limit_fallback") or {}
		if isinstance(overrides, dict) and fn_name in overrides:
			value = int(overrides[fn_name])
			if value > 0:
				limit = value
	except Exception:
		pass
	return limit, ttl


def check_rate_limit(fn_name, email=None, user=None):
	"""Decide a request: ("allow"|"deny"|"unavailable", retry_after).

	"deny" carries the **remaining** seconds of the window as retry_after
	(العميل يعرضه للمستخدم كما هو). "unavailable" means the Redis backend
	itself failed (policy applied by the caller).
	Counters are only those configured for the endpoint: some endpoints
	key by IP (guest-facing), others by user (authenticated actions).
	"""
	cfg = LIMITS[fn_name]
	counters = []
	if "ip" in cfg:
		ip, trusted = get_client_ip()
		if trusted:
			limit, ttl = cfg["ip"]
			ident = ip
		else:
			limit, ttl = _fallback_limit(fn_name)
			ident = FALLBACK_IDENT
		counters.append((f"rl:{fn_name}:ip:{ident}", limit, ttl))
	if user and "user" in cfg:
		limit, ttl = cfg["user"]
		counters.append((f"rl:{fn_name}:user:{_email_hash(user)}", limit, ttl))
	if email and "email" in cfg:
		limit, ttl = cfg["email"]
		counters.append((f"rl:{fn_name}:email:{_email_hash(email)}", limit, ttl))
	try:
		for key, limit, ttl in counters:
			if _hit(key, ttl) > limit:
				return "deny", _remaining_ttl(key, ttl)
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
			# The test runner issues many confirms per user in one run;
			# rate-limit behavior is covered by dedicated unit tests.
			if getattr(frappe.flags, "in_test", False):
				return fn(*args, **kwargs)
			try:
				form_email = frappe.form_dict.get("email")
			except Exception:
				form_email = None
			email = kwargs.get("email") or form_email
			session_user = getattr(frappe.session, "user", None)
			user = session_user if session_user and session_user != "Guest" else None
			decision, retry_after = check_rate_limit(fn_name, email=email, user=user)
			if decision == "allow":
				return fn(*args, **kwargs)
			if decision == "deny":
				return _limited_response(retry_after)
			if FAILURE_POLICY.get(fn_name) == "closed":
				return _unavailable_response()
			return fn(*args, **kwargs)

		return wrapper

	return deco
