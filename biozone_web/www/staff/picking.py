"""صفحة طباعة ورقة المخزن (picking): مرآة الفاتورة الورقية الحقيقية.

مسار مستقل عن print.py (فاتورة/مرتجع) — لا يشاركه السياق ولا القالب ولا
ملفاته. قراءة فقط: get_doc واحد للطلب + قراءة بنوده من الذاكرة + أرصدة
الدفتر (استعلامات ثابتة لا داخل حلقة)، وبلا إنشاء مستندات. يسمح بالطلب
المؤهل فقط (Draft: docstatus=0)؛ المسلَّم والملغى وغير الموجود → صفحة حالة
ناعمة بلا ورقة. إشعارات المرتجع (credit) خارج هذا المسار أصلًا.

انحرافات موثقة عن الفاتورة (لا فاتورة بعد في المسودة):
- خلية "فاتورة رقم" تعرض رقم الطلب.
- "تسليم" يعرض "—" (لا تسليم بعد).
- اشعار الخصم والمسدد = صفر (القيمتان صفر في الفاتورتين المرجعيتين، ولا
  مستند مصدر لهما في مرحلة التجهيز).
- الرصيد السابق = رصيد الدفتر الحي (بلا فاتورة).
- التقسيم: حد أقصى 20 صنفًا للصفحة (PICKING_PAGE_SIZE) — الملخص والتوقيعات
  (صف المستخدم/الوقت) في آخر صفحة فقط.
- تنسيق الأرقام بلا فاصل آلاف وبلا أصفار زائدة (_num)، والتفقيط بصيغة
  الورقة ("مطلوب فقط وقدره ... جنيه و... قرش فقط لاغير").
"""

import frappe
from frappe import _

from biozone_web.b9_utils import _integer_in_words, get_customer_balances
from biozone_web.utils import (
	get_header_context,
	render_state_page,
	require_staff_access,
)

PICKING_PAGE_SIZE = 20


def _num(value):
	"""تنسيق رقمي بلا فاصل آلاف وبلا أصفار زائدة: 150، 112.5، 30.72."""
	text = "%.10f" % float(value or 0)
	if "." in text:
		text = text.rstrip("0").rstrip(".")
	return text if text not in ("", "-0") else "0"


def _pick_words(amount):
	"""تفقيط بصيغة الورقة من _integer_in_words (إعادة استخدام بلا تعديل)."""
	pounds = int(float(amount or 0))
	piastres = int(round((float(amount or 0) - pounds) * 100))
	if piastres == 100:
		pounds += 1
		piastres = 0
	p_text = _integer_in_words(pounds)
	if p_text.startswith("ألف"):
		p_text = "الف" + p_text[len("ألف") :]
	if not piastres:
		return "مطلوب فقط وقدره %s جنيه فقط لاغير" % p_text
	q_text = _integer_in_words(piastres)
	if q_text.startswith("ألف"):
		q_text = "الف" + q_text[len("ألف") :]
	return "مطلوب فقط وقدره %s جنيه و%s قرش فقط لاغير" % (p_text, q_text)


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")

	order_name = (frappe.form_dict.get("order") or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return render_state_page(
			context,
			_("الطلب غير موجود"),
			_("رقم الطلب المطلوب غير موجود. تحقق من الرقم أو ارجع إلى قائمة الطلبات."),
			http_status_code=404,
		)

	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus == 2:
		return render_state_page(
			context,
			_("هذا الطلب ملغى"),
			_("هذا الطلب ملغى ولا توجد ورقة تجهيز له."),
			order_name=so.name,
		)
	if so.docstatus == 1:
		return render_state_page(
			context,
			_("هذا الطلب تم تسليمه"),
			_("هذا الطلب تم تسليمه — ورقة المخزن غير متاحة له. استخدم الفاتورة الرسمية من صفحة التسليم."),
			order_name=so.name,
		)

	# نسخة عرض فقط من البنود — لا تُمرر كائنات البنود الأصلية إلى القالب.
	# الأرقام منسقة بلا فاصل آلاف وبلا أصفار زائدة (_num) مطابقة للورقة.
	items = []
	public_total = 0.0
	for idx, it in enumerate(so.items or [], start=1):
		rate = float(it.price_list_rate or it.rate or 0)
		qty = float(it.qty or 0)
		public_total += qty * rate
		items.append(
			{
				"idx": idx,
				"item_name": it.item_name,
				"qty": qty,
				"qty_s": _num(qty),
				"public_rate": rate,
				"price_s": _num(rate),
				"discount_percentage": float(it.discount_percentage or 0),
				"disc_s": _num(float(it.discount_percentage or 0)),
				"amount": float(it.amount or 0),
				"amount_s": _num(float(it.amount or 0)),
			}
		)
	grand = float(so.rounded_total or so.grand_total or 0)
	paid = 0.0
	discount_notice = 0.0
	balances = get_customer_balances(so.customer, so.company)
	previous_balance = float(balances["previous"] or 0)
	current_balance = previous_balance + grand - paid

	from biozone_web.api import SHIPPING_ACCOUNT

	_shipping = 0
	for t in (so.get("taxes") or []):
		if (t.account_head or "").strip() == SHIPPING_ACCOUNT:
			_shipping += float(t.get("tax_amount") or 0)

	context.order_name = so.name
	context.delivery_note = ""
	context.invoice_name = so.name
	context.customer_name = so.customer_name
	context.customer_address = so.address_display or so.customer_address or ""
	context.posting_date = frappe.utils.format_date(so.transaction_date, "dd/MM/yyyy")
	context.items = items
	context.items_count = len(items)
	pages = [
		{"rows": items[i : i + PICKING_PAGE_SIZE], "page_no": n + 1}
		for n, i in enumerate(range(0, max(len(items), 1), PICKING_PAGE_SIZE))
	]
	context.pages = pages
	context.total_pages = len(pages)
	context.shipping = _shipping
	context.grand_total = grand
	context.net_s = _num(grand)
	context.public_total = public_total
	context.public_s = _num(public_total)
	context.discount_notice = discount_notice
	context.discnote_s = _num(discount_notice)
	context.paid = paid
	context.paid_s = _num(paid)
	context.previous_balance = previous_balance
	context.prev_s = "%.3f" % previous_balance
	context.current_balance = current_balance
	context.curr_s = "%.3f" % current_balance
	context.amount_words = _pick_words(grand)
	staff_user = frappe.session.user
	context.staff_name = (
		frappe.db.get_value("User", staff_user, "full_name") or staff_user
	)
	now_dt = frappe.utils.now_datetime()
	context.time12 = "%s %s" % (
		now_dt.strftime("%I:%M:%S"),
		"م" if now_dt.hour >= 12 else "ص",
	)
	context.print_time = frappe.utils.now_datetime().strftime("%H:%M - %d/%m/%Y")
	context.company = so.company
	return context
