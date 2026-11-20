import hashlib
import json
import math
import re

import frappe
from frappe import _

from biozone_web.request_id import get_request_id
from biozone_web.units import money2

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-fA-F]{64}$")
CART_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# سقف تجاري معتمد (قرار المالك 2026-11-20): 200 سطر لكل سلة.
# كان 500 تقنيًا بعد إزالة القطع الصامت عند 200؛ القرار الحالي تجاري أيضًا
# لأنه يحدّ أقصى حجم طلب من المتجر ويحافظ على زمن تأكيد معقول.
MAX_CART_LINES = 200

# حدود شكلية/تقنية لمنع overflow وبيانات غير منطقية — ليست حدودًا
# تجارية. أي حد أعمال (سعر/كمية/إجمالي) يبقى قرار مالك منفصلًا.
MAX_QTY_PER_LINE = 1000000.0
MAX_ITEM_CODE_LEN = 140
MAX_UOM_LEN = 140
QTY_EPS = 1e-6
FACTOR_EPS = 1e-6
MONEY_EPS = 0.01 + 1e-9

# CR-08: محاولة Processing أقدم من ذلك تُفحص تحت قفل الصف: إن وُجد
# Sales Order تُعتبر ناجحة، وإلا تُعلَّم Failed لبدء محاولة جديدة.
PROCESSING_TIMEOUT_MINUTES = 10

DUPLICATE_ERRORS = tuple(
	exc
	for exc in (
		getattr(frappe, "DuplicateEntryError", None),
		getattr(frappe, "UniqueValidationError", None),
		getattr(getattr(frappe, "db", None), "IntegrityError", None),
	)
	if exc
)


class CheckoutError(Exception):
	def __init__(self, error_code, message, *, status=422, item_code=None, extra=None):
		super().__init__(message)
		self.error_code = error_code
		self.message = message
		self.status = status
		self.item_code = item_code
		self.extra = extra or {}


def _json(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_response(value):
	if isinstance(value, dict):
		return value
	try:
		parsed = json.loads(value or "{}")
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


def validate_checkout_metadata(checkout_token, quote_fingerprint):
	if not isinstance(checkout_token, str) or not TOKEN_RE.fullmatch(checkout_token.strip()):
		raise CheckoutError("CHECKOUT_TOKEN_INVALID", _("معرّف التأكيد غير صالح"), status=422)
	if not isinstance(quote_fingerprint, str) or not FINGERPRINT_RE.fullmatch(quote_fingerprint.strip()):
		raise CheckoutError("CART_QUOTE_INVALID", _("بصمة السعر غير صالحة — حدّث السلة"), status=422)
	return checkout_token.strip(), quote_fingerprint.strip().lower()


def _quantity(value):
	if isinstance(value, bool):
		raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	try:
		qty = float(value)
	except (TypeError, ValueError):
		raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	if not math.isfinite(qty) or qty <= 0:
		raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	qty = round(qty, 3)
	if qty <= 0 or qty > MAX_QTY_PER_LINE:
		raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	return qty


def _text(value, max_len):
	if not isinstance(value, str):
		raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	value = value.strip()
	if not value or len(value) > max_len:
		raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	return value


def _cart_too_large_message():
	return _(
		"السلة تحتوي على أكثر من {0} صنفًا — قسّمها إلى طلبات أصغر ثم أعد المحاولة."
	).format(MAX_CART_LINES)


def normalize_cart_rows(items):
	"""Shape/number validation shared by pricing and confirmation.

	CR-05: the guest pricing endpoint used to pass raw rows into the quote
	builder, so ``items=[1]`` or a huge quantity raised TypeError /
	InvalidOperation instead of a business 4xx. This normalizer does not
	query Item availability — pricing needs to answer price=None for a
	disabled/unpriced item; confirmation adds that gate separately.
	Duplicate (item_code, uom) rows are accepted as independent lines; the
	fingerprint preserves their order, and the post-insert guard compares
	them as a multiset.
	"""
	if isinstance(items, str):
		try:
			items = frappe.parse_json(items)
		except Exception:
			raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	if not isinstance(items, list):
		raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
	if len(items) > MAX_CART_LINES:
		raise CheckoutError("CART_TOO_LARGE", _cart_too_large_message())

	normalized = []
	for row in items:
		if not isinstance(row, dict):
			raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
		code = _text(row.get("item_code"), MAX_ITEM_CODE_LEN)
		uom = row.get("uom")
		if uom is None:
			uom = ""
		if not isinstance(uom, str):
			raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"), item_code=code)
		uom = uom.strip()
		if len(uom) > MAX_UOM_LEN:
			raise CheckoutError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"), item_code=code)
		normalized.append(
			{
				"item_code": code,
				"quantity": _quantity(row.get("quantity")),
				"uom": uom,
			}
		)
	return normalized


def normalize_checkout_items(items):
	items = normalize_cart_rows(items)
	codes = list(dict.fromkeys(row["item_code"] for row in items))

	# One query for every distinct item. The old per-line get_value loop made
	# confirmation N+1 and could also pass an unbounded default result limit.
	rows = (
		frappe.get_all(
			"Item",
			filters={"item_code": ["in", codes]},
			fields=["item_code", "disabled"],
			limit_page_length=len(codes),
		)
		if codes
		else []
	)
	item_map = {row.item_code: row for row in rows}
	for code in codes:
		item = item_map.get(code)
		if not item or item.disabled:
			raise CheckoutError(
				"ITEM_UNAVAILABLE",
				_("أحد الأصناف في السلة لم يعد متاحًا، يرجى تحديث السلة"),
				item_code=code,
			)
	return items


def build_checkout_quote(
	items,
	*,
	customer,
	customer_group,
	company=None,
	selling_price_list=None,
	currency=None,
):
	from biozone_web.units import resolve_items_display
	from biozone_web.utils import get_effective_item_prices

	if len(items) > MAX_CART_LINES:
		raise CheckoutError("CART_TOO_LARGE", _cart_too_large_message())

	codes = list(dict.fromkeys(row["item_code"] for row in items))
	prices = get_effective_item_prices(codes, customer=customer, customer_group=customer_group)
	display = resolve_items_display(codes, customer_group) if codes else {}
	price_meta = {}
	quote_items = []
	so_items = []
	issues = []
	net_total = 0.0

	for code in codes:
		p = dict(prices.get(code) or {})
		d = display.get(code) or {}
		displayable = bool(d.get("ok") and d.get("displayable", True))
		factor = d.get("factor") if displayable else None
		uom = (d.get("uom") or "").strip() if displayable else ""
		price = p.get("price") if displayable else None
		if price is not None and factor is not None:
			price = money2(price * factor)
		else:
			price = None
		price_meta[code] = {
			"price": price,
			"base_price": p.get("base_price"),
			"discount_percentage": p.get("discount_percentage", 0.0),
			"display_uom": uom or None,
			"display_factor": factor,
		}

	for row in items:
		code = row["item_code"]
		qty = _quantity(row.get("quantity"))
		meta = price_meta[code]
		uom = meta.get("display_uom")
		factor = meta.get("display_factor")
		price = meta.get("price")
		sent_uom = row.get("uom") or ""
		available = price is not None and bool(uom) and factor is not None
		if available and sent_uom and sent_uom != uom:
			available = False
			issue_code = "UOM_CHANGED"
		elif available:
			issue_code = ""
		elif not meta.get("display_uom"):
			issue_code = "UOM_UNAVAILABLE"
		else:
			issue_code = "ITEM_UNAVAILABLE"
		if not available:
			issues.append({"item_code": code, "code": issue_code})
		line_total = money2(price * qty) if available else None
		if line_total is not None:
			net_total += line_total
		quote_items.append(
			{
				"item_code": code,
				"qty": qty,
				"uom": uom,
				"price": price,
				"line_total": line_total,
				"available": available,
				"issue": issue_code or None,
			}
		)
		if available:
			so_items.append(
				{
					"item_code": code,
					"qty": qty,
					"uom": uom,
					"conversion_factor": factor,
				}
			)

	quote = {
		"v": 2,
		"customer": customer,
		"customer_group": customer_group,
		# CR-04: مدخلات التسعير الفعلية جزء من العقد — تغيّر الشركة/قائمة
		# السعر/العملة يغيّر البصمة ولا يمر كـ«نفس quote».
		"company": company,
		"selling_price_list": selling_price_list,
		"currency": currency,
		"net_total": money2(net_total),
		"items": quote_items,
	}
	return {
		"fingerprint": hashlib.sha256(_json(quote).encode("utf-8")).hexdigest(),
		"net_total": quote["net_total"],
		"items": quote_items,
		"prices": price_meta,
		"so_items": so_items if not issues else [],
		"issues": issues,
		"snapshot": quote,
		"company": company,
		"selling_price_list": selling_price_list,
		"currency": currency,
	}


def evaluate_post_insert_minimum(inserted_net, precalc_net, min_limit):
	"""Decide the minimum-order gate from the *inserted* order total.

	The pre-computed total is only an early estimate; the inserted Sales
	Order is authoritative and has already been checked against the
	customer's quote by ``quote_mismatch``. A divergence is reported (the
	caller logs it) and must not fail an otherwise valid order.
	"""
	inserted = money2(inserted_net)
	precalc = money2(precalc_net)
	limit = money2(min_limit)
	return {
		"diverged": precalc != inserted,
		"allowed": inserted >= limit,
		"inserted_net": inserted,
		"precalc_net": precalc,
		"shortfall": money2(max(0.0, limit - inserted)),
	}


def quote_contract_fields():
	"""Pricing inputs that must be identical between the prices call and
	confirmation — otherwise the fingerprint legitimately differs."""
	from biozone_web.utils import PUBLIC_PRICE_LIST, get_default_company

	company = get_default_company()
	currency = None
	if company:
		try:
			currency = frappe.get_cached_value("Company", company, "default_currency")
		except Exception:
			currency = None
	return {
		"company": company,
		"selling_price_list": PUBLIC_PRICE_LIST,
		"currency": currency,
	}


def _field(row, name, default=None):
	if isinstance(row, dict):
		return row.get(name, default)
	return getattr(row, name, default)


def _line_groups(rows, code_attr, uom_attr, qty_attr):
	groups = {}
	for row in rows:
		code = str(_field(row, code_attr) or "")
		uom = str(_field(row, uom_attr) or "").strip()
		groups.setdefault((code, uom), []).append(row)
	for key in groups:
		groups[key].sort(key=lambda r: float(_field(r, qty_attr) or 0))
	return groups


def quote_mismatch(so, quote):
	"""CR-04: return a reason when the inserted Sales Order does not match
	the quote the customer reviewed, else None.

	Compares every line (item, qty, UOM, factor, rate, line total) plus the
	net total and the pricing inputs — a matching net_total alone is not
	enough (qty/UOM could change while the total stays the same).
	"""
	expected_rows = quote.get("items") or []
	if any(not row.get("available") for row in expected_rows):
		return "quote contains unavailable lines"

	expected = _line_groups(expected_rows, "item_code", "uom", "qty")
	actual = _line_groups(so.items, "item_code", "uom", "qty")
	if set(expected) != set(actual):
		return f"line set changed: expected={sorted(expected)} actual={sorted(actual)}"

	total = 0.0
	for key, exp_rows in expected.items():
		act_rows = actual[key]
		if len(exp_rows) != len(act_rows):
			return f"line count changed for {key}"
		for exp, act in zip(exp_rows, act_rows):
			if abs(float(_field(act, "qty") or 0) - float(exp["qty"])) > QTY_EPS:
				return f"qty changed for {key}: {_field(act, 'qty')} != {exp['qty']}"
			factor = float(_field(act, "conversion_factor", 0) or 0)
			if abs(factor - float(exp.get("conversion_factor") or 1.0)) > FACTOR_EPS:
				return f"uom factor changed for {key}: {factor}"
			rate = float(_field(act, "rate", 0) or 0)
			if abs(rate - float(exp.get("price") or 0)) > MONEY_EPS:
				return f"rate changed for {key}: {rate} != {exp.get('price')}"
			amount = float(_field(act, "amount", 0) or 0)
			if abs(amount - float(exp.get("line_total") or 0)) > MONEY_EPS:
				return f"line total changed for {key}: {amount} != {exp.get('line_total')}"
			total += amount

	if abs(money2(total) - float(quote.get("net_total") or 0)) > MONEY_EPS:
		return f"net total changed: {money2(total)} != {quote.get('net_total')}"
	if quote.get("company") and (_field(so, "company") or "") != quote["company"]:
		return f"company changed: {_field(so, 'company')} != {quote['company']}"
	if quote.get("selling_price_list") and (
		(_field(so, "selling_price_list") or "") != quote["selling_price_list"]
	):
		return f"price list changed: {_field(so, 'selling_price_list')} != {quote['selling_price_list']}"
	if quote.get("currency") and (_field(so, "currency") or "") != quote["currency"]:
		return f"currency changed: {_field(so, 'currency')} != {quote['currency']}"
	return None


def get_checkout_receipt(token, *, for_update=False):
	if for_update:
		rows = frappe.db.sql(
			"select name from `tabBZ Checkout Receipt` where checkout_token = %s for update",
			(token,),
			as_dict=True,
		)
		if not rows:
			return None
		return frappe.get_doc("BZ Checkout Receipt", rows[0].name)
	name = frappe.db.get_value("BZ Checkout Receipt", {"checkout_token": token}, "name")
	return frappe.get_doc("BZ Checkout Receipt", name) if name else None


# CR-01: هوية دورة السلة (cart_instance) من المتصفح — تُستخدم فقط لمنع
# التكرار داخل نفس الدورة، وليست بديلًا عن checkout_token.
def validate_cart_instance(cart_instance):
	if cart_instance is None or (isinstance(cart_instance, str) and not cart_instance.strip()):
		return None
	if not isinstance(cart_instance, str) or not CART_INSTANCE_RE.fullmatch(cart_instance.strip()):
		raise CheckoutError("CART_INSTANCE_INVALID", _("هوية جلسة السلة غير صالحة"), status=422)
	return cart_instance.strip()


def success_response(receipt):
	response = _parse_response(receipt.response_snapshot)
	response.setdefault("ok", True)
	response.setdefault("sales_order", receipt.sales_order)
	response.setdefault("redirect", f"/order-confirmed?name={receipt.sales_order}")
	return response


CART_CLAIM_DOCTYPE = "BZ Checkout Claim"


def _claim_key(user, cart_instance):
	return hashlib.sha256(f"{user}|{cart_instance}".encode("utf-8")).hexdigest()


def _claim_age_seconds(creation):
	if not creation:
		return 0.0
	if isinstance(creation, str):
		creation = frappe.utils.get_datetime(creation)
	return max(0.0, (frappe.utils.now_datetime() - creation).total_seconds())


def _instance_conflict_extra(existing):
	extra = {
		"already_created": True,
		"deduplicated": True,
		"rotate_cart_instance": True,
	}
	sales_order = (existing or {}).get("sales_order") if existing else None
	if sales_order:
		extra["sales_order"] = sales_order
		extra["redirect"] = f"/order-confirmed?name={sales_order}"
	return extra


def acquire_cart_cycle(user, cart_instance, fingerprint, attempts=3):
	"""Atomically claim one cart cycle (user + cart_instance).

	The unique ``cycle_key`` is the guarantee: concurrent requests with the
	same cycle cannot both proceed — the loser blocks on the unique index,
	then reads the winner's state (same fingerprint ⇒ replay; different
	fingerprint ⇒ 409). The row is inserted in the same transaction as the
	Sales Order, so a failed/rolled-back attempt leaves no claim behind.

	Returns {"claim": doc|None, "response": response|None}.
	"""
	key = _claim_key(user, cart_instance)
	for _attempt in range(attempts):
		try:
			claim = frappe.get_doc(
				{
					"doctype": CART_CLAIM_DOCTYPE,
					"cycle_key": key,
					"user": user,
					"cart_instance": cart_instance,
					"quote_fingerprint": fingerprint,
				}
			)
			claim.insert(ignore_permissions=True)
			return {"claim": claim, "response": None}
		except DUPLICATE_ERRORS:
			# The winner has committed (or rolled back) by the time InnoDB
			# answers the duplicate check; re-read its state.
			frappe.db.rollback()
			existing = frappe.db.get_value(
				CART_CLAIM_DOCTYPE,
				{"cycle_key": key},
				["name", "quote_fingerprint", "sales_order", "creation"],
				as_dict=True,
			)
			if not existing:
				continue
			if (existing.quote_fingerprint or "") != fingerprint:
				raise CheckoutError(
					"CART_INSTANCE_CONFLICT",
					_("تم إنشاء طلب من دورة السلة هذه بالفعل — ابدأ دورة سلة جديدة للمتابعة"),
					status=409,
					extra=_instance_conflict_extra(existing),
				)
			if existing.sales_order and frappe.db.exists("Sales Order", existing.sales_order):
				return {
					"claim": None,
					"response": {
						"ok": True,
						"sales_order": existing.sales_order,
						"redirect": f"/order-confirmed?name={existing.sales_order}",
					},
				}
			if _claim_age_seconds(existing.creation) < PROCESSING_TIMEOUT_MINUTES * 60:
				raise CheckoutError(
					"CHECKOUT_IN_PROGRESS",
					_("طلبك قيد المعالجة — انتظر قليلًا ثم حدّث الصفحة"),
					status=409,
				)
			# A stale claim without an order (crash between claim and commit
			# is not possible without rollback, but be defensive): drop it.
			frappe.delete_doc(
				CART_CLAIM_DOCTYPE, existing.name, force=True, ignore_permissions=True
			)
			frappe.db.commit()
	raise CheckoutError(
		"CHECKOUT_IN_PROGRESS",
		_("طلبك قيد المعالجة — انتظر قليلًا ثم حدّث الصفحة"),
		status=409,
	)


def complete_cart_cycle(claim, sales_order):
	if not claim:
		return
	claim.sales_order = sales_order
	claim.completed_on = frappe.utils.now_datetime()
	claim.save(ignore_permissions=True)


def _processing_age_seconds(receipt):
	created = getattr(receipt, "creation", None)
	if not created:
		return 0.0
	if isinstance(created, str):
		created = frappe.utils.get_datetime(created)
	return max(0.0, (frappe.utils.now_datetime() - created).total_seconds())


def recover_stale_processing(receipt):
	"""Recover a Processing attempt older than the cutoff, under a row lock.

	- Sales Order exists -> mark Success and return the original response.
	- No Sales Order -> mark Failed (PROCESSING_TIMEOUT) so a new attempt
	  can start instead of returning 409 forever.
	- Anything recent/not Processing -> None (never free a live attempt).
	"""
	if _processing_age_seconds(receipt) < PROCESSING_TIMEOUT_MINUTES * 60:
		return None
	fresh = get_checkout_receipt(receipt.checkout_token, for_update=True)
	if not fresh or fresh.status != "Processing":
		return None
	if _processing_age_seconds(fresh) < PROCESSING_TIMEOUT_MINUTES * 60:
		return None
	if fresh.sales_order and frappe.db.exists("Sales Order", fresh.sales_order):
		fresh.status = "Success"
		fresh.completed_on = frappe.utils.now_datetime()
		fresh.save(ignore_permissions=True)
		return {"receipt": fresh, "response": success_response(fresh)}
	fresh.status = "Failed"
	fresh.error_code = "PROCESSING_TIMEOUT"
	fresh.save(ignore_permissions=True)
	return {"receipt": fresh, "response": None}


def _receipt_result(receipt, user, fingerprint):
	if receipt.user != user:
		raise CheckoutError("CHECKOUT_TOKEN_CONFLICT", _("معرّف التأكيد مستخدم بالفعل"), status=409)
	if receipt.status == "Success":
		if (receipt.quote_fingerprint or "") != fingerprint:
			# الطلب أُنشئ فعلًا تحت هذه الهوية لكن بسعر/لقطة أقدم (مثل:
			# ضاع الرد ثم تغيّر السعر). لا يجوز إنشاء Sales Order ثانٍ؛
			# نرفض الطلب الجديد ونعيد بيانات الطلب القائم كي ينتقل العميل
			# إليه ويعاينه بدل تكراره.
			raise CheckoutError(
				"CHECKOUT_TOKEN_CONFLICT",
				_("تم إنشاء طلب بهذه المحاولة بالفعل — لا يُنشأ طلب ثانٍ"),
				status=409,
				extra={
					"already_created": True,
					"sales_order": receipt.sales_order,
					"redirect": f"/order-confirmed?name={receipt.sales_order}",
				},
			)
		return {"receipt": receipt, "response": success_response(receipt)}
	if receipt.status == "Processing":
		recovered = recover_stale_processing(receipt)
		if recovered and recovered.get("response"):
			return recovered
		if recovered:
			receipt = recovered["receipt"]
		else:
			raise CheckoutError(
				"CHECKOUT_IN_PROGRESS",
				_("طلبك قيد المعالجة — انتظر قليلًا ثم حدّث الصفحة"),
				status=409,
			)
	# سجل فاشل: يمكن إعادة استخدامه حتى لو تغيّرت البصمة — لا طلب أُنشئ
	# بعد، وbegin_checkout يعيد ضبط الحالة/البصمة داخل المحاولة الجديدة.
	return {"receipt": receipt, "response": None}


def checkout_replay(token, user, fingerprint):
	receipt = get_checkout_receipt(token)
	return _receipt_result(receipt, user, fingerprint) if receipt else None


def _mark_receipt_processing(receipt, quote, cart_instance=None):
	receipt.status = "Processing"
	receipt.quote_fingerprint = quote["fingerprint"]
	receipt.quote_snapshot = _json(quote["snapshot"])
	receipt.request_id = get_request_id()
	receipt.error_code = None
	if cart_instance:
		receipt.cart_instance = cart_instance
	receipt.save(ignore_permissions=True)


def begin_checkout(token, user, quote, cart_instance=None):
	state = checkout_replay(token, user, quote["fingerprint"])
	if state and state.get("response"):
		return state

	receipt = state["receipt"] if state else None
	if receipt:
		_mark_receipt_processing(receipt, quote, cart_instance)
		return {"receipt": receipt, "response": None}

	receipt = frappe.get_doc(
		{
			"doctype": "BZ Checkout Receipt",
			"checkout_token": token,
			"user": user,
			"status": "Processing",
			"quote_fingerprint": quote["fingerprint"],
			"cart_instance": cart_instance or None,
			"request_id": get_request_id(),
			"quote_snapshot": _json(quote["snapshot"]),
		}
	)
	try:
		receipt.insert(ignore_permissions=True)
	except DUPLICATE_ERRORS:
		frappe.db.rollback()
		existing = get_checkout_receipt(token, for_update=True)
		if not existing:
			raise
		state = _receipt_result(existing, user, quote["fingerprint"])
		if state.get("response"):
			return state
		_mark_receipt_processing(existing, quote, cart_instance)
		return {"receipt": existing, "response": None}
	return {"receipt": receipt, "response": None}


def complete_checkout(receipt, sales_order, response):
	receipt.status = "Success"
	receipt.sales_order = sales_order
	receipt.completed_on = frappe.utils.now_datetime()
	receipt.response_snapshot = _json(response)
	receipt.save(ignore_permissions=True)


def checkout_status(token, user):
	if not isinstance(token, str) or not TOKEN_RE.fullmatch(token.strip()):
		raise CheckoutError("CHECKOUT_TOKEN_INVALID", _("معرّف التأكيد غير صالح"), status=422)
	receipt = get_checkout_receipt(token.strip())
	if not receipt or receipt.user != user:
		return {"ok": True, "status": "NotFound"}
	response = _parse_response(receipt.response_snapshot)
	return {
		"ok": True,
		"status": receipt.status,
		"sales_order": receipt.sales_order,
		"redirect": response.get("redirect") if receipt.status == "Success" else None,
		"request_id": receipt.request_id,
		"quote_fingerprint": receipt.quote_fingerprint,
	}
