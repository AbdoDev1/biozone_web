import frappe
from frappe import _

from biozone_web.b9_utils import amount_in_arabic_words
from biozone_web.utils import (
	get_csrf_token_safe,
	get_header_context,
	invoice_linked_to_order,
	render_state_page,
	require_staff_access,
)


def _b9_return_context(dn_name, si_name):
	"""صفوف المرتجع وسجله لصفحة التسليم (S2b — قراءة فقط).

	يرجع (rows, history): rows = [{so_detail, item_name, uom, delivered,
	returned, remaining}] من مسودة mapping أصيلة غير محفوظة (المتبقي
	الحقيقي بعد طرح السابق)؛ history = المرتجعات المعتمدة للزوج.
	أي فشل = ([], []) — القسم يختفي بدل كسر الصفحة.
	"""
	rows, history = [], []
	try:
		from erpnext.controllers.sales_and_purchase_return import get_already_returned_items

		dn = frappe.get_doc("Delivery Note", dn_name) if dn_name else None
		if not dn:
			return rows, history
		already = {}
		try:
			tmp = frappe.get_doc(
				{"doctype": "Delivery Note", "is_return": 1, "return_against": dn.name}
			)
			already = get_already_returned_items(tmp)
		except Exception:
			already = {}
		for it in dn.items or []:
			delivered = float(it.qty or 0)
			key = (it.item_code, it.name)
			returned = float((already.get(key) or {}).get("qty", 0) or 0)
			remaining = max(delivered - returned, 0)
			rows.append(
				{
					"so_detail": it.so_detail or "",
					"item_name": it.item_name,
					"uom": it.uom or "",
					"delivered": delivered,
					"returned": returned,
					"remaining": remaining,
				}
			)
		for dt, against in (("Delivery Note", dn.name), ("Sales Invoice", si_name)):
			for r in frappe.get_all(
				dt,
				filters={"is_return": 1, "docstatus": 1, "return_against": against},
				fields=["name", "posting_date", "grand_total"],
				order_by="creation desc",
			):
				history.append(
					{
						"doctype": dt,
						"name": r.name,
						"posting_date": frappe.utils.format_date(r.posting_date, "dd/MM/yyyy"),
						"grand_total": float(r.grand_total or 0),
					}
				)
	except Exception:
		frappe.log_error(title="Biozone return context failed")
		return [], []
	return rows, history


def _b9_credit_print(si_name):
	"""بيانات طباعة إشعار المرتجع (S3a — قراءة فقط).

	أحدث إشعار دائن معتمد للفاتورة: البنود المرتجعة (موجبة للعرض) +
	القيمة + رقم الأصل + اسم الموظف المنشئ. بلا سجل = None (لا طباعة).
	"""
	try:
		names = frappe.get_all(
			"Sales Invoice",
			filters={"is_return": 1, "docstatus": 1, "return_against": si_name},
			fields=["name"],
			order_by="creation desc",
			limit_page_length=1,
		)
		if not names:
			return None
		cn = frappe.get_doc("Sales Invoice", names[0].name)
		staff = frappe.db.get_value("User", cn.owner, "full_name") or cn.owner
		lines = []
		for idx, it in enumerate(cn.items or [], start=1):
			qty = abs(float(it.qty or 0))
			rate = float(it.rate or 0)
			lines.append(
				{
					"idx": idx,
					"item_name": it.item_name,
					"qty": qty,
					"uom": it.uom or "",
					"rate": rate,
					"amount": round(qty * rate, 2),
				}
			)
		return {
			"name": cn.name,
			"original": si_name,
			"posting_date": frappe.utils.format_date(cn.posting_date, "dd/MM/yyyy"),
			"customer_name": cn.customer_name,
			"lines": lines,
			"total": abs(float(cn.grand_total or 0)),
			"staff_name": staff,
			"print_time": frappe.utils.now_datetime().strftime("%H:%M - %d/%m/%Y"),
		}
	except Exception:
		frappe.log_error(title="Biozone credit print context failed")
		return None


def get_context(context):
	require_staff_access()
	context.no_cache = 1
	context.active_page = "orders"
	context.update(get_header_context())
	context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
	context.csrf_token = get_csrf_token_safe()
	context.error_state = False

	order_name = (frappe.form_dict.get("order") or "").strip()
	si_param = (frappe.form_dict.get("invoice") or "").strip()

	# الطلب غير موجود → 404 مهذبة (بلا traceback).
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return render_state_page(
			context,
			_("الطلب غير موجود"),
			_("رقم الطلب المطلوب غير موجود. تحقق من الرقم أو ارجع إلى قائمة الطلبات."),
			http_status_code=404,
		)

	so = frappe.get_doc("Sales Order", order_name)

	# الطلب الملغى → رسالة منفصلة قبل أي بحث عن فاتورة.
	if so.docstatus == 2:
		return render_state_page(
			context,
			_("هذا الطلب ملغى"),
			_("هذا الطلب ملغى ولا يمكن إنشاء مستند تسليم له."),
			order_name=so.name,
		)

	if si_param:
		# فاتورة مرسلة صراحة: وجود + اعتماد + ارتباط — أي فشل → رفض صريح بلا fallback،
		# ولا يجوز أبدًا عرض فاتورة تخص طلبًا آخر.
		if not frappe.db.exists("Sales Invoice", si_param):
			return render_state_page(
				context,
				_("الفاتورة المطلوبة غير صالحة"),
				_("الفاتورة المطلوبة غير موجودة، ولا يمكن عرضها لهذا الطلب."),
				http_status_code=404,
				order_name=so.name,
			)
		si = frappe.get_doc("Sales Invoice", si_param)
		if si.docstatus != 1 or not invoice_linked_to_order(si.name, order_name):
			return render_state_page(
				context,
				_("الفاتورة المطلوبة غير صالحة"),
				_("الفاتورة المطلوبة ليست فاتورة معتمدة لهذا الطلب، ولا يمكن عرضها هنا."),
				http_status_code=404,
				order_name=so.name,
			)
	else:
		# استنتاج تلقائي فقط عند غياب المعامل — ويجب أن تكون معتمدة ومرتبطة.
		# S2b: صفوف المرتجع تحمل sales_order أيضًا — الأصل وحده (غير مرتجع).
		si_rows = frappe.db.sql(
			"""select ch.parent from `tabSales Invoice Item` ch
			inner join `tabSales Invoice` par on par.name = ch.parent
			where ch.sales_order = %s and par.docstatus = 1 and par.is_return = 0
			group by ch.parent order by min(par.creation)""",
			order_name,
		)
		si_name = si_rows[0][0] if si_rows else None
		if not si_name:
			# احتياط عبر التسليم المرتبط بالطلب (S2b: يستبعد المرتجعات أيضًا).
			si_rows = frappe.db.sql(
				"""select ch.parent from `tabSales Invoice Item` ch
				inner join `tabSales Invoice` par on par.name = ch.parent
				inner join `tabDelivery Note Item` dch on dch.parent = ch.delivery_note
				where dch.against_sales_order = %s and par.docstatus = 1 and par.is_return = 0
				group by ch.parent order by min(par.creation)""",
				order_name,
			)
			si_name = si_rows[0][0] if si_rows else None
		if not si_name or not frappe.db.exists("Sales Invoice", si_name):
			return render_state_page(
				context,
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد"),
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد. لا يمكن إنشاء مستند تسليم حاليًا."),
				order_name=so.name,
			)
		si = frappe.get_doc("Sales Invoice", si_name)
		if si.docstatus != 1 or not invoice_linked_to_order(si.name, order_name):
			return render_state_page(
				context,
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد"),
				_("لا توجد فاتورة معتمدة لهذا الطلب بعد. لا يمكن إنشاء مستند تسليم حاليًا."),
				order_name=so.name,
			)
	# S2b: صفوف المرتجع تحمل against_sales_order أيضًا — الأصل وحده هو
	# المعتمد غير المرتجع (تدفقنا: واحدة)؛ الأقدم إنشاءً لو تعدد لسبب ما.
	orig_dns = frappe.db.sql(
		"""select ch.parent from `tabDelivery Note Item` ch
		inner join `tabDelivery Note` par on par.name = ch.parent
		where ch.against_sales_order = %s and par.docstatus = 1 and par.is_return = 0
		group by ch.parent order by min(par.creation)""",
		order_name,
	)
	dn_name = orig_dns[0][0] if orig_dns else ""

	# السابق من الحقل المثبَّت لحظة إنشاء الفاتورة (C2) — بلا حساب جديد هنا.
	previous_balance = frappe.utils.flt(si.get("custom_previous_balance"))
	invoice_outstanding = frappe.utils.flt(si.outstanding_amount) or frappe.utils.flt(si.grand_total)
	current_balance = previous_balance + invoice_outstanding
	confirmer = frappe.db.get_value("User", si.owner, "full_name") or si.owner
	grand = float(si.rounded_total or si.grand_total or 0)

	context.order_name = so.name
	context.delivery_note = dn_name or ""
	context.invoice_name = si.name
	context.customer_name = si.customer_name
	context.customer_address = si.address_display or si.customer_address or ""
	context.posting_date = frappe.utils.format_date(si.posting_date, "dd/MM/yyyy")
	context.items = [
		{
			"idx": i + 1,
			"item_name": it.item_name,
			"qty": float(it.qty or 0),
			"rate": float(it.rate or 0),
			"public_rate": float(it.price_list_rate or it.rate or 0),
			"discount_percentage": float(it.discount_percentage or 0),
			"discount_amount": float(it.discount_amount or 0),
			"amount": float(it.amount or 0),
		}
		for i, it in enumerate(si.items or [])
	]
	context.items_count = len(si.items or [])
	context.return_rows, context.returns_history = _b9_return_context(dn_name, si.name)
	if context.returns_history and all(r["remaining"] <= 0 for r in context.return_rows):
		context.return_state = "مكتمل المرتجع"
	elif any(r["returned"] > 0 for r in context.return_rows):
		context.return_state = "مرتجع جزئيًا"
	else:
		context.return_state = ""
	context.credit_print = _b9_credit_print(si.name)
	context.net_total = float(si.net_total or 0)
	context.grand_total = grand
	# Phase-2 shipping row (print only): summed from the invoice taxes.
	from biozone_web.api import SHIPPING_ACCOUNT

	_shipping = 0
	for t in (si.get("taxes") or []):
		if (t.account_head or "").strip() == SHIPPING_ACCOUNT:
			_shipping += float(t.get("tax_amount") or 0)
	context.shipping = _shipping
	context.previous_balance = previous_balance
	context.current_balance = current_balance
	context.amount_words = amount_in_arabic_words(grand)
	context.confirmed_by = confirmer
	context.print_time = frappe.utils.now_datetime().strftime("%H:%M - %d/%m/%Y")
	context.company = si.company
	return context
