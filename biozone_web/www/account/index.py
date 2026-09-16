import frappe

from biozone_web.utils import (
	customer_has_category_assigned_field,
	find_customer_for_current_user,
	get_header_context,
	get_portal_users_for_customer,
	redirect_staff_away_from_store,
)
from biozone_web.www.my_orders import build_customer_invoices_list, build_customer_orders_list

RECENT_ORDERS_LIMIT = 3
UNASSIGNED_CATEGORY_LABEL = "لم تُحدَّد لك فئة تسعير بعد"
# الهاتف اختياري فعليًا (السيرفر لا يشترطه) — الفراغ يُعرض شرطة (قرار B10).
PHONE_EMPTY_DISPLAY = "-"
# لا ميزة عناوين قائمة في المشروع — حالة محايدة ثابتة فقط (قرار B10).
ADDRESS_NEUTRAL_LABEL = "لم تتم إضافة عنوان بعد"


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = None
	context.update(get_header_context())

	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	# قراءة فقط: find بلا إنشاء — لو لا يوجد ربط بعد، تُعرض بيانات
	# المستخدم الأساسية مع حالة "غير محددة" وبلا طلبات.
	customer = find_customer_for_current_user()

	if customer:
		customer_name = frappe.db.get_value("Customer", customer, "customer_name") or customer
		group = frappe.db.get_value("Customer", customer, "customer_group")
		if customer_has_category_assigned_field():
			assigned = frappe.utils.cint(
				frappe.db.get_value("Customer", customer, "category_assigned_by_staff")
			)
		else:
			assigned = 0
		users = get_portal_users_for_customer(customer)
		email = users[0] if users else frappe.session.user
	else:
		customer_name = (
			frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
		)
		group = None
		assigned = 0
		email = frappe.session.user

	context.customer_name = customer_name
	context.email = email
	context.phone_display = (
		frappe.db.get_value("User", frappe.session.user, "phone") or PHONE_EMPTY_DISPLAY
	)
	context.address_note = ADDRESS_NEUTRAL_LABEL
	if assigned:
		context.category_assigned = True
		context.customer_group = group
	else:
		# قيد B10: لا يُعرض اسم الفئة الفعلي إطلاقًا قبل التعيين الإداري،
		# حتى لو كانت "الجمهور" مخزَّنة فعليًا — نص محايد فقط.
		context.category_assigned = False
		context.unassigned_label = UNASSIGNED_CATEGORY_LABEL

	orders, orders_count = build_customer_orders_list()
	context.orders_count = orders_count
	context.recent_orders = orders[:RECENT_ORDERS_LIMIT]

	# تبويبا /account (قرار B10): أساسية (افتراضي) ومديونية — رسم
	# سيرفر-سايد بلا API. قيم الفلاتر تُمرَّر دائمًا لإعادة ملئها،
	# أما الاستعلام نفسه فيُدار فقط عند فتح تبويب المديونية.
	tab = (frappe.form_dict.get("tab") or "profile").strip()
	if tab not in ("profile", "debt"):
		tab = "profile"
	context.active_tab = tab

	context.invoice_q = (frappe.form_dict.get("q") or "").strip()
	context.invoice_date_from = (frappe.form_dict.get("date_from") or "").strip()
	context.invoice_date_to = (frappe.form_dict.get("date_to") or "").strip()
	try:
		context.invoice_page = int(frappe.form_dict.get("page") or 1)
	except (TypeError, ValueError):
		context.invoice_page = 1

	if tab == "debt":
		invoices, invoices_count, invoices_pages, invoices_page = build_customer_invoices_list(
			page=context.invoice_page,
			name_filter=context.invoice_q,
			date_from=context.invoice_date_from,
			date_to=context.invoice_date_to,
		)
		context.invoices = invoices
		context.invoices_count = invoices_count
		context.invoices_pages = invoices_pages
		context.invoice_page = invoices_page
	else:
		context.invoices = []
		context.invoices_count = 0
		context.invoices_pages = 1

	return context
