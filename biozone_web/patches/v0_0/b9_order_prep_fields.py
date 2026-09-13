import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	"""حقول المرحلة B9 — تجهيز الطلبات (فحص الباركود + الفاتورة + التسليم).

	قرار تقني موثّق: البرومبت يسمّي النموذج "Sales Order Item Scan" كـ Child
	DocType منفصل مرتبط ببند الطلب. نُفّذ هنا كحقول مخصصة على بند الطلب
	نفسه (Sales Order Item) بدل جدول حفيد منفصل، للأسباب التالية:

	1. مصدر حقيقة واحد: بند الطلب هو نفسه المرجع المستخدم في روابط
	   Delivery Note (so_detail) وSales Invoice (so_detail) — جدول منفصل
	   يتطلب مزامنة مستمرة (إنشاء/حذف/إعادة ترتيب البنود) ومصدرًا مزدوجًا
	   للحقيقة، مع خطر انحراف صامت بين الجدولين.
	2. حذف البند أثناء التجهيز (ميزة B9 §4) يحدّث العداد فورًا تلقائيًا —
	   لا حاجة لحذف متزامن في جدول ثانٍ.
	3. الحقول نفسها مطابقة للمواصفة (confirmed + confirmation_method، بلا
	   أي عمود كمية) — الفرق في مكان التخزين فقط، لا في السلوك.

	الحقول على Sales Order (رأس الطلب):
	- custom_needs_attention (Check): فلاج داخلي بدل حالة منفصلة (§2).
	- custom_attention_note (Small Text): تفاصيل المشكلة، تظهر داخل صفحة
	  الطلب فقط، وليست في القائمة.

	الحقول على Sales Order Item (البند — نموذج الفحص §3):
	- custom_confirmed (Check): الصنف مؤكَّد أو غير مؤكَّد (ثنائي فقط).
	- custom_confirmation_method (Select: ماسح / يدوي).
	- custom_confirmed_by (Data): اسم الموظف الذي أكّد (يدويًا أو آليًا) —
	  للتدقيق، وليس جزءًا من منطق التأكيد نفسه.
	- custom_confirm_reason (Small Text): سبب التأكيد اليدوي فقط (§3) —
	  يبقى فارغًا عند التأكيد بالماسح.
	- custom_confirm_time (Datetime): وقت التأكيد الفعلي.
	"""
	create_custom_fields(
		{
			"Sales Order": [
				{
					"fieldname": "custom_needs_attention",
					"label": "يحتاج انتباه",
					"fieldtype": "Check",
					"insert_after": "status",
					"default": "0",
				},
				{
					"fieldname": "custom_attention_note",
					"label": "ملاحظة الانتباه",
					"fieldtype": "Small Text",
					"insert_after": "custom_needs_attention",
				},
			],
			"Sales Order Item": [
				{
					"fieldname": "custom_confirmed",
					"label": "مؤكَّد",
					"fieldtype": "Check",
					"insert_after": "item_code",
					"default": "0",
				},
				{
					"fieldname": "custom_confirmation_method",
					"label": "طريقة التأكيد",
					"fieldtype": "Select",
					"options": "\nماسح\nيدوي",
					"insert_after": "custom_confirmed",
				},
				{
					"fieldname": "custom_confirmed_by",
					"label": "أكّده",
					"fieldtype": "Data",
					"insert_after": "custom_confirmation_method",
				},
				{
					"fieldname": "custom_confirm_reason",
					"label": "سبب التأكيد اليدوي",
					"fieldtype": "Small Text",
					"insert_after": "custom_confirmed_by",
				},
				{
					"fieldname": "custom_confirm_time",
					"label": "وقت التأكيد",
					"fieldtype": "Datetime",
					"insert_after": "custom_confirm_reason",
				},
			],
		}
	)
