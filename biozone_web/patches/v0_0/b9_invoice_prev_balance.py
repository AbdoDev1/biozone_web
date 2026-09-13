import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	"""المرحلة B9 (إصلاح C2): تثبيت الرصيد السابق للفاتورة لحظة إنشائها.

	الحقل `custom_previous_balance` على Sales Invoice يُلتقط مرة واحدة قبل
	إنشاء الفاتورة (رصيد العميل الآجل من دفتر الأستاذ الفعلي في تلك اللحظة)
	ولا يُشتق لاحقًا، حتى لا ينجرف الرقم المطبوع مع الفواتير اللاحقة.
	نوع العملة مرتبط بعملة الفاتورة (`currency`) فتُطبَّق دقة العملة
	المعتمدة في ERPNext تلقائيًا.
	"""
	create_custom_fields(
		{
			"Sales Invoice": [
				{
					"fieldname": "custom_previous_balance",
					"label": "الرصيد السابق",
					"fieldtype": "Currency",
					"options": "currency",
					"insert_after": "outstanding_amount",
					"read_only": 1,
					"no_copy": 1,
				}
			]
		}
	)
