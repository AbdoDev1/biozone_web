"""أدوات المرحلة B9 — تجهيز الطلبات (فحص الباركود + الفاتورة + التسليم).

كل نصوص الأخطاء والواجهات بالفصحى فقط، بلا عامية.
"""

import frappe
from frappe import _


# الحالات المعروضة (4 — §2 من البرومبت + الملغى S1).
STATE_PREPARING = "جارٍ التجهيز"
STATE_READY = "جاهز للتسليم"
STATE_DELIVERED = "تم التسليم"
STATE_CANCELLED = "ملغى"

ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"


def to_arabic_digits(value) -> str:
	"""يحوّل الأرقام اللاتينية إلى أرقام عربية مشرقية للعرض فقط."""
	return "".join(ARABIC_DIGITS[int(ch)] if ch.isdigit() else ch for ch in str(value))


def get_order_prep_state(so_doc) -> dict:
	"""يحسب حالة التجهيز المعروضة لطلب بيع من بنوده المؤكَّدة.

	- جارٍ التجهيز: طلب Draft ولم تكتمل كل البنود (التمييز بنسبة التقدم فقط).
	- جاهز للتسليم: كل البنود مؤكَّدة، وما زال Draft بانتظار التأكيد النهائي.
	- تم التسليم: الطلب معتمَد (docstatus=1) وله فاتورة معتمَدة.
	- ملغى: الطلب ملغى (docstatus=2) — مقفل، بلا تجهيز ولا حركات (S1).

	يُرجع: state, total, confirmed, percent, progress_text, needs_attention.
	"""
	items = list(so_doc.get("items") or [])
	total = len(items)
	confirmed = sum(1 for it in items if frappe.utils.cint(it.get("custom_confirmed")))
	percent = round((confirmed / total) * 100) if total else 100

	if so_doc.get("docstatus") == 2:
		state = STATE_CANCELLED
	elif so_doc.get("docstatus") == 1:
		state = STATE_DELIVERED
	elif total > 0 and confirmed == total:
		state = STATE_READY
	else:
		state = STATE_PREPARING

	progress_text = _("{0} من {1} أصناف مؤكدة").format(
		to_arabic_digits(confirmed), to_arabic_digits(total)
	)

	return {
		"state": state,
		"total": total,
		"confirmed": confirmed,
		"percent": percent,
		"progress_text": progress_text,
		"needs_attention": bool(so_doc.get("custom_needs_attention")),
		"attention_note": so_doc.get("custom_attention_note") or "",
	}


def find_item_code_by_barcode(barcode: str) -> str | None:
	"""يبحث عن صنف برقم باركود (نص حرفي، بلا تحويل رقمي).

	يغطي: جدول الباركود الفرعي (Item Barcode) + حقل الباركود المباشر إن
	وُجد + كود الصنف نفسه كحل أخير (بعض الماسحات تقرأ الكود).
	يُرجع item_code أو None.
	"""
	barcode = (barcode or "").strip()
	if not barcode:
		return None

	row = frappe.db.get_value("Item Barcode", {"barcode": barcode}, ["parent", "parenttype"])
	if row:
		parent, parenttype = row
		if parenttype == "Item":
			return parent

	if frappe.db.exists("Item", barcode):
		return barcode

	# بعض البيئات تخزّن الباركود في حقل مخصص على الصنف نفسه.
	for field in ("barcode", "barcodes"):
		try:
			meta = frappe.get_meta("Item")
		except Exception:
			break
		if meta.has_field(field):
			code = frappe.db.get_value("Item", {field: barcode}, "name")
			if code:
				return code
		break

	return None


def get_item_barcodes(item_code: str) -> list[str]:
	"""كل الباركودات المسجلة لصنف (للعرض في جدول التجهيز)."""
	rows = frappe.get_all(
		"Item Barcode", filters={"parent": item_code, "parenttype": "Item"}, fields=["barcode"]
	)
	return [r.barcode for r in rows if r.barcode]


def get_customer_balances(customer: str, company: str, current_invoice: str | None = None) -> dict:
	"""الحساب السابق والحالي للفاتورة المطبوعة (§7) — من دفتر الأستاذ الفعلي.

	- الحالي: رصيد العميل الآجل بعد هذه الفاتورة (GL فقط، بلا طلبات معلّقة).
	- السابق: الحالي مطروحًا منه مستحَق هذه الفاتورة.
	كلاهما من ERPNext الفعلي، وليس رقمًا تقديريًا.
	"""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	current = frappe.utils.flt(
		get_customer_outstanding(customer, company, ignore_outstanding_sales_order=True)
	)

	invoice_outstanding = 0.0
	if current_invoice and frappe.db.exists("Sales Invoice", current_invoice):
		invoice_outstanding = frappe.utils.flt(
			frappe.db.get_value("Sales Invoice", current_invoice, "outstanding_amount")
		)
		if not invoice_outstanding:
			invoice_outstanding = frappe.utils.flt(
				frappe.db.get_value("Sales Invoice", current_invoice, "grand_total")
			)

	previous = current - invoice_outstanding
	return {"previous": previous, "current": current}


# ---------- المبلغ بالحروف (عربي فصيح — الجنيه المصري) ----------

_ONES = [
	"",
	"واحد",
	"اثنان",
	"ثلاثة",
	"أربعة",
	"خمسة",
	"ستة",
	"سبعة",
	"ثمانية",
	"تسعة",
	"عشرة",
	"أحد عشر",
	"اثنا عشر",
]
_TENS = ["", "", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
_HUNDREDS = [
	"",
	"مائة",
	"مائتان",
	"ثلاثمائة",
	"أربعمائة",
	"خمسمائة",
	"ستمائة",
	"سبعمائة",
	"ثمانمائة",
	"تسعمائة",
]


def _under_thousand(n: int) -> str:
	parts = []
	h = n // 100
	r = n % 100
	if h:
		parts.append(_HUNDREDS[h])
	if r:
		if r < len(_ONES) and _ONES[r]:
			if r == 2 and h:
				parts.append("واثنان")
			else:
				parts.append(_ONES[r] if not (r == 2 and not h) else "اثنان")
		elif r < 20:
			# 13-19: "ثلاثة عشر" ... "تسعة عشر"
			ones = r - 10
			names = ["", "", "", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة"]
			parts.append(f"{names[ones]} عشر")
		else:
			t = r // 10
			o = r % 10
			if o:
				one = "واحد" if o == 1 else ("اثنان" if o == 2 else _ONES[o])
				parts.append(f"{one} و{_TENS[t]}")
			else:
				parts.append(_TENS[t])
	return " و".join(p for p in parts if p).replace(" و و", " و")


def _integer_in_words(n: int) -> str:
	if n == 0:
		return "صفر"
	if n == 1:
		return "واحد"
	if n == 2:
		return "اثنان"
	parts = []
	millions = n // 1_000_000
	thousands = (n % 1_000_000) // 1000
	rest = n % 1000
	if millions:
		if millions == 1:
			parts.append("مليون")
		elif millions == 2:
			parts.append("مليونان")
		else:
			parts.append(f"{_under_thousand(millions)} ملايين" if millions > 10 else f"{_under_thousand(millions)} مليون")
	if thousands:
		if thousands == 1:
			parts.append("ألف")
		elif thousands == 2:
			parts.append("ألفان")
		else:
			parts.append(f"{_under_thousand(thousands)} ألف")
	if rest:
		parts.append(_under_thousand(rest))
	return " و".join(parts)


def _pound_part(pounds: int) -> str:
	if pounds == 1:
		return "جنيه واحد"
	if pounds == 2:
		return "جنيهان"
	if 3 <= pounds <= 10:
		return f"{_integer_in_words(pounds)} جنيهات"
	if pounds % 100 == 0:
		# مائة/ألف ومضاعفاتهما: الاسم بعدهما مفرد مجرور بلا تنوين.
		return f"{_integer_in_words(pounds)} جنيه"
	return f"{_integer_in_words(pounds)} جنيهًا"


def _piastre_part(piastres: int) -> str:
	if piastres == 1:
		return "قرش واحد"
	if piastres == 2:
		return "قرشان"
	if 3 <= piastres <= 10:
		return f"{_integer_in_words(piastres)} قروش"
	return f"{_integer_in_words(piastres)} قرشًا"


def amount_in_arabic_words(amount: float, currency_word: str = "جنيه") -> str:
	"""المبلغ الإجمالي مكتوبًا بالحروف (§7) — فصحى، بلا عامية.

	مثال: 1966.30 ← "ألف وتسعمائة وستة وستون جنيهًا وثلاثون قرشًا فقط لا غير".
	"""
	amount = frappe.utils.flt(amount)
	pounds = int(amount)
	piastres = int(round((amount - pounds) * 100))
	if piastres == 100:
		pounds += 1
		piastres = 0

	if pounds == 0:
		if not piastres:
			return "صفر فقط لا غير"
		return f"{_piastre_part(piastres)} فقط لا غير"

	pound_text = _pound_part(pounds)
	if piastres:
		return f"{pound_text} و{_piastre_part(piastres)} فقط لا غير"
	return f"{pound_text} فقط لا غير"
