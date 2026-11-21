"""كاش تسعير قصير العمر لتقليل N+1 في محرك قواعد ERPNext.

المشكلة المقاسة: `apply_pricing_rule` يستعلم `tabPricing Rule` (وأشجار
مجموعات الأصناف) مرة لكل صنف — 200 صنفًا ≈ 1561 استعلامًا / 3.6s محليًا.

التصميم (معتمد):
- مفتاح لكل (صنف + مدخلات التسعير + السعر الأساسي) مع إصدار عام في المفتاح.
- إبطال فوري بزيادة الإصدار (O(1)) عبر doc_events على مستندات التسعير.
- TTL صارم (افتراضي 60s) كشبكة أمان لأي كتابة تتجاوز الهوكات.
- معطّل افتراضيًا: `biozone_pricing_cache_enabled = 1` لتشغيله.

الأمان: الكاش لا يُستخدم في تسعير الطلب النهائي — صفوف الطلب تُرسل بلا
أسعار ويُسعّرها ERPNext من جديد عند الإدراج، ثم `quote_mismatch` يرفض أي
اختلاف بـ`PRICE_CHANGED` مع rollback كامل.
"""

import hashlib
import json

import frappe

ENABLED_KEY = "biozone_pricing_cache_enabled"
TTL_KEY = "biozone_pricing_cache_ttl"
DEFAULT_TTL = 60
MAX_TTL = 3600
VERSION_KEY = "bz:px:version"
VERSION_META_KEY = "bz:px:version:last"
STATS_PREFIX = "bz:px:stats"
STATS_TTL = 60 * 60 * 24 * 7  # أسبوع — للقراءة بعد الويكند

# حقول Item التي تؤثر في مطابقة قواعد التسعير أو في وحدة/معامل العرض.
ITEM_PRICING_FIELDS = ("item_group", "brand", "stock_uom", "disabled", "display_override")


def _conf_int(key, default):
	try:
		value = frappe.get_conf().get(key)
	except Exception:
		return default
	if value is None:
		return default
	if isinstance(value, bool):
		return int(value)
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def enabled():
	return _conf_int(ENABLED_KEY, 0) == 1


def ttl():
	value = _conf_int(TTL_KEY, DEFAULT_TTL)
	return max(1, min(MAX_TTL, value))


def _cache_key(name):
	return frappe.cache.make_key(name)


def _safe_incr(name, amount=1):
	try:
		key = _cache_key(name)
		frappe.cache.set(key, 0, ex=STATS_TTL, nx=True)
		frappe.cache.incrby(key, amount)
	except Exception:
		# فشل الكاش لا يكسر أي عملية تسعير أو كتابة.
		pass


def version():
	"""الإصدار الحالي — يُقرأ مرة واحدة لكل طلب (200 صنفًا = قراءة واحدة)."""
	memo = getattr(frappe.local, "bz_px_version", None)
	if memo is not None:
		return memo
	try:
		key = _cache_key(VERSION_KEY)
		frappe.cache.set(key, 0, nx=True)
		value = int(frappe.cache.get(key) or 0)
	except Exception:
		value = 0
	try:
		frappe.local.bz_px_version = value
	except Exception:
		pass
	return value


def invalidate(reason=""):
	"""إبطال عام: كل المفاتيح القديمة تصبح غير قابلة للوصول (تنتهي بـTTL)."""
	try:
		key = _cache_key(VERSION_KEY)
		frappe.cache.set(key, 0, ex=None, nx=True)
		new_version = int(frappe.cache.incrby(key, 1))
	except Exception:
		return None
	try:
		# نفس الطلب يجب أن يرى الإصدار الجديد فورًا بعد الإبطال.
		frappe.local.bz_px_version = new_version
	except Exception:
		pass
	try:
		frappe.cache.set(
			_cache_key(VERSION_META_KEY),
			json.dumps({"v": new_version, "reason": str(reason)[:120], "at": frappe.utils.now()}),
			ex=STATS_TTL,
		)
	except Exception:
		pass
	return new_version


def clear():
	"""أمر المسح اليدوي (bedrock بعد أي تعديل SQL مباشر)."""
	return invalidate("manual-clear")


def context(company, currency, price_list, customer, customer_group):
	return {
		"co": company or "",
		"cur": currency or "",
		"list": price_list or "",
		"cust": customer or "",
		"group": customer_group or "",
		"date": frappe.utils.today(),
		"qty": 1,
	}


def _digest(item_code, base, stock_uom, ctx):
	payload = {
		"item": item_code,
		"base": float(base or 0),
		"uom": stock_uom or "",
		"co": ctx.get("co"),
		"cur": ctx.get("cur"),
		"list": ctx.get("list"),
		"cust": ctx.get("cust"),
		"group": ctx.get("group"),
		"date": ctx.get("date"),
		"qty": 1,
	}
	raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _item_key(item_code, base, stock_uom, ctx):
	return _cache_key("bz:px:%s:%s" % (version(), _digest(item_code, base, stock_uom, ctx)))


def lookup(item_code, base, stock_uom, ctx):
	"""يُرجع dict السعر أو None (miss / معطّل / فشل كاش)."""
	if not enabled():
		return None
	try:
		raw = frappe.cache.get(_item_key(item_code, base, stock_uom, ctx))
	except Exception:
		return None
	if not raw:
		return None
	try:
		value = json.loads(raw)
	except Exception:
		return None
	if not isinstance(value, dict) or "price" not in value:
		return None
	return value


def store(item_code, base, stock_uom, ctx, value):
	if not enabled():
		return
	try:
		frappe.cache.set(
			_item_key(item_code, base, stock_uom, ctx),
			json.dumps(value, ensure_ascii=False),
			ex=ttl(),
		)
	except Exception:
		pass


def record_batch(hits, misses):
	"""عدّاد hit/miss مرة واحدة لكل نداء تسعير (لا لكل صنف)."""
	if not enabled():
		return
	date = frappe.utils.nowdate()
	if hits:
		_safe_incr("%s:%s:hit" % (STATS_PREFIX, date), int(hits))
	if misses:
		_safe_incr("%s:%s:miss" % (STATS_PREFIX, date), int(misses))


def note_price_changed():
	"""عدّاد 409 PRICE_CHANGED (يُستدعى من مسار التعارض فقط)."""
	_safe_incr("%s:%s:price_changed" % (STATS_PREFIX, frappe.utils.nowdate()))


def _stat(date, name):
	try:
		return int(frappe.cache.get(_cache_key("%s:%s:%s" % (STATS_PREFIX, date, name))) or 0)
	except Exception:
		return 0


def get_stats(date=None):
	"""قراءة العدّادات: bench execute biozone_web.services.pricing_cache.get_stats"""
	from frappe.utils import add_days, nowdate

	day = date or nowdate()
	hit = _stat(day, "hit")
	miss = _stat(day, "miss")
	total = hit + miss
	return {
		"date": day,
		"enabled": enabled(),
		"ttl_seconds": ttl(),
		"version": version(),
		"hit": hit,
		"miss": miss,
		"hit_rate": round(hit / total, 3) if total else 0.0,
		"price_changed_409": _stat(day, "price_changed"),
		"yesterday": {
			"hit": _stat(add_days(day, -1), "hit"),
			"miss": _stat(add_days(day, -1), "miss"),
			"price_changed_409": _stat(add_days(day, -1), "price_changed"),
		},
	}


# ---------------------------------------------------------------------------
# doc_events hooks — إبطال فوري عند أي تغيير تسعيري
# ---------------------------------------------------------------------------


def on_price_doc_change(doc=None, method=None):
	invalidate("%s:%s" % (getattr(doc, "doctype", "?"), method or ""))


def on_item_change(doc=None, method=None):
	"""Item: نُبطل فقط عند تغيّر حقل مؤثر (تحديث صورة لا يمسح الكاش)."""
	if method == "on_trash":
		invalidate("Item:on_trash")
		return
	before = None
	if doc is not None and hasattr(doc, "get_doc_before_save"):
		before = doc.get_doc_before_save()
	if before is None:
		invalidate("Item:unknown-before")
		return
	changed = [
		field
		for field in ITEM_PRICING_FIELDS
		if hasattr(doc, field) and getattr(before, field, None) != getattr(doc, field, None)
	]
	if changed:
		invalidate("Item:" + ",".join(changed))


def on_company_change(doc=None, method=None):
	if method == "on_trash":
		invalidate("Company:on_trash")
		return
	before = None
	if doc is not None and hasattr(doc, "get_doc_before_save"):
		before = doc.get_doc_before_save()
	if before is None:
		invalidate("Company:unknown-before")
		return
	if getattr(before, "default_currency", None) != getattr(doc, "default_currency", None):
		invalidate("Company:default_currency")
