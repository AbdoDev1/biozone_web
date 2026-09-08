import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Customer Group": [
				{
					"fieldname": "disabled",
					"label": "Disabled",
					"fieldtype": "Check",
					"insert_after": "customer_group_name",
					"default": "0",
				}
			]
		}
	)
