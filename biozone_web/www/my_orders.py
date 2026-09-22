import frappe

from biozone_web.utils import (
	find_customer_for_current_user,
	is_unique_customer_binding,
)

# تسمية عربية لحالات Sales Order القياسية في ERPNext، عشان تتعرض للعميل
# بلغة مفهومة بدل قيم النظام الإنجليزية الخام.
STATUS_LABELS = {
	"Draft": "قيد المراجعة",
	"On Hold": "معلّق",
	"To Deliver and Bill": "تم التأكيد",
	"To Bill": "تم التسليم",
	"To Deliver": "تم التأكيد",
	"Completed": "تم التسليم بالكامل",
	"Cancelled": "ملغى",
	"Closed": "مغلق",
}

# تلوين شارة الحالة — يتماشى مع نفس نظام الألوان المستخدم في باقي الموقع
# (primary للحالات الإيجابية، رمادي لسه معلّقة، أحمر للملغاة).
STATUS_STYLES = {
	"Draft": "bg-surface-container text-on-surface-variant",
	"On Hold": "bg-surface-container text-on-surface-variant",
	"To Deliver and Bill": "bg-primary/10 text-primary",
	"To Deliver": "bg-primary/10 text-primary",
	"To Bill": "bg-primary/10 text-primary",
	"Completed": "bg-primary/10 text-primary",
	"Cancelled": "bg-error/10 text-error",
	"Closed": "bg-surface-container text-on-surface-variant",
}

# حجم صفحة تبويب المديونية في /account (قرار B10 المعتمد).
INVOICES_PAGE_SIZE = 20


def get_context(context):
	# أُعيد توجيه المسار القديم إلى /account/orders (B10) — الملف باقٍ
	# عمدًا حتى لا ينكسر أي رابط قديم محفوظ، والمنطق نفسه يعيش ويُستخدَم
	# من هناك عبر build_customer_orders_list (بلا تكرار).
	frappe.local.flags.redirect_location = "/account/orders"
	raise frappe.Redirect


def build_customer_orders_list():
	"""يبني قائمة طلبات المستخدم الحالي بنفس الصلاحيات والتسميات دائمًا.

	يُستخدم من /account/orders (ومن أي مسار لاحق) — المصدر الوحيد لمنطق
	القائمة حتى لا تتكرر نسخه. يُرجع (orders, orders_count).
	"""
	# الربط بين المستخدم والـCustomer بيتم عبر جدول Portal User الفرعي
	# (find_customer_for_current_user)، مش بافتراض Customer.name == email
	# القديم المكسور — نفس الإصلاح اللي اتطبق على
	# get_or_create_customer_for_current_user بتاريخ 8 سبتمبر 2026. لو
	# رجّعت None، معناها المستخدم لسه ما عملش أي طلب خالص (مفيش ربط
	# Portal User اتعمل له لحد دلوقتي).
	customer = find_customer_for_current_user()

	orders = []
	# حماية Customer المشترك: لو نفس الـ Customer مرتبط بأكثر من
	# مستخدم (Portal User)، لا نعرض أي طلبات — حتى لا يرى مستخدم
	# طلبات مستخدم آخر يشترك معه في نفس الـ Customer.
	if customer and is_unique_customer_binding(customer, frappe.session.user):
		rows = frappe.get_all(
			"Sales Order",
			filters={"customer": customer},
			fields=["name", "transaction_date", "status", "grand_total"],
			order_by="creation desc",
		)
		for so in rows:
			orders.append(
				{
					"name": so.name,
					"date_display": frappe.utils.format_date(so.transaction_date, "d MMMM yyyy"),
					"status_label": STATUS_LABELS.get(so.status, so.status),
					"status_style": STATUS_STYLES.get(so.status, "bg-surface-container text-on-surface-variant"),
					"items_count": frappe.db.count("Sales Order Item", {"parent": so.name}),
					"grand_total": so.grand_total,
				}
			)

	return orders, len(orders)


def build_customer_invoices_list(page=1, name_filter="", date_from="", date_to=""):
	"""يبني قائمة فواتير المستخدم الحالي لتبويب المديونية في /account.

	القواعد المعتمدة (B10): الفواتير المرحّلة فقط (docstatus = 1 —
	الملغاة مستبعدة تلقائيًا)، كل الفواتير بترقيم صفحات (20/صفحة)،
	الترتيب الأحدث أولًا (posting_date desc)، الصفرية ظاهرة، والمرتجع
	صف عادي (يُميَّز عرضه فقط عبر is_return). كل صف = الاسم + القيمة
	الرقمية (outstanding_amount) بلا شارة وبلا عملة — قرار عرض، لا
	يغيّر أي بيانات.

	نفس حراس القوائم: العميل من الجلسة فقط (لا يُقبل أي customer من
	الواجهة)، والربط المشترك يُخفي الكل. يُرجع
	(invoices, total_count, total_pages, page).
	"""
	customer = find_customer_for_current_user()

	invoices = []
	total_count = 0
	total_pages = 1
	try:
		page = int(page or 1)
	except (TypeError, ValueError):
		page = 1
	page = max(page, 1)

	if customer and is_unique_customer_binding(customer, frappe.session.user):
		where = "docstatus = 1 and customer = %(customer)s"
		params = {"customer": customer}

		name_filter = (name_filter or "").strip()
		if name_filter:
			where += " and name like %(name)s"
			params["name"] = f"%{name_filter}%"

		try:
			date_from = (date_from or "").strip()
			if date_from:
				frappe.utils.getdate(date_from)
				where += " and posting_date >= %(date_from)s"
				params["date_from"] = date_from
		except Exception:
			pass

		try:
			date_to = (date_to or "").strip()
			if date_to:
				frappe.utils.getdate(date_to)
				where += " and posting_date <= %(date_to)s"
				params["date_to"] = date_to
		except Exception:
			pass

		total_count = (
			frappe.db.sql(
				f"select count(*) from `tabSales Invoice` where {where}",
				params,
			)[0][0]
			or 0
		)
		total_pages = max((total_count + INVOICES_PAGE_SIZE - 1) // INVOICES_PAGE_SIZE, 1)
		page = min(page, total_pages)

		rows = frappe.db.sql(
			f"""select name, posting_date, outstanding_amount, is_return
				from `tabSales Invoice` where {where}
				order by posting_date desc, creation desc
				limit %(limit)s offset %(offset)s""",
			{
				**params,
				"limit": INVOICES_PAGE_SIZE,
				"offset": (page - 1) * INVOICES_PAGE_SIZE,
			},
			as_dict=True,
		)
		for r in rows:
			value = float(r.outstanding_amount or 0)
			invoices.append(
				{
					"name": r.name,
					"value": value,
					"value_display": f"{value:,.2f}",
					"is_return": bool(frappe.utils.cint(r.is_return)),
				}
			)

	return invoices, total_count, total_pages, page
