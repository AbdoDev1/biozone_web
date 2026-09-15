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
