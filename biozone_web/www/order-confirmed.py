import frappe
from frappe import _

from biozone_web.utils import get_header_context, redirect_staff_away_from_store


def get_context(context):
	redirect_staff_away_from_store()

	context.no_cache = 1
	context.active_page = None
	context.update(get_header_context())

	order_name = frappe.form_dict.get("name")
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		frappe.throw(_("الطلب غير موجود"), frappe.DoesNotExistError)

	so = frappe.get_doc("Sales Order", order_name)

	# فحص ملكية يدوي (مش نظام صلاحيات Frappe الافتراضي، زي باقي صفحات
	# الموقع) — بس صاحب الطلب الأصلي. الموظفين اتحجبوا عن الصفحة دي من
	# الأساس فوق (redirect_staff_away_from_store)؛ مراجعة الطلبات من
	# ناحيتهم بتتم من /staff/orders نفسها.
	if so.owner != frappe.session.user:
		frappe.throw(_("لا تملك صلاحية عرض هذا الطلب"), frappe.PermissionError)

	context.order_number = so.name
	context.item_count = len(so.items)
	context.items = [
		{
			"item_name": it.item_name,
			"qty": it.qty,
			"uom": it.uom,
			"rate": it.rate,
			"amount": it.amount,
		}
		for it in so.items
	]
	context.net_total = so.net_total
	# الضرايب بتظهر بس لو فعلًا متسجلة على الطلب (Sales Taxes and
	# Charges Template) — مفيش رقم وهمي بيتحط لو مفيش نظام ضريبة متبني
	# لحد دلوقتي.
	context.taxes = [{"description": t.description, "tax_amount": t.tax_amount} for t in so.taxes]
	context.grand_total = so.grand_total
	return context
