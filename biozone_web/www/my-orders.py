import frappe

from biozone_web.utils import get_header_context, redirect_staff_away_from_store

# تسمية عربية لحالات Sales Order القياسية في ERPNext، عشان تتعرض للعميل
# بلغة مفهومة بدل قيم النظام الإنجليزية الخام.
STATUS_LABELS = {
	"Draft": "قيد المراجعة",
	"On Hold": "معلّق",
	"To Deliver and Bill": "تم التأكيد",
	"To Bill": "تم التسليم",
	"To Deliver": "تم التأكيد",
	"Completed": "تم التسليم بالكامل",
	"Cancelled": "ملغي",
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


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = None
	context.update(get_header_context())

	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	# نفس تسمية get_or_create_customer_for_current_user في utils.py: اسم
	# الـCustomer = بريد المستخدم نفسه. لو مفيش Customer بالاسم ده لسه،
	# معناه المستخدم لسه ما عملش أي طلب خالص.
	customer = frappe.session.user

	orders = []
	if frappe.db.exists("Customer", customer):
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

	context.orders = orders
	context.orders_count = len(orders)
	return context
