import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Customer": [
				{
					"fieldname": "category_assigned_by_staff",
					"label": "Category Assigned By Staff",
					"fieldtype": "Check",
					"insert_after": "customer_group",
					"default": "0",
				}
			]
		}
	)
