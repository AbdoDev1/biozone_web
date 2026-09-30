import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Customer Group": [
				{
					"fieldname": "min_order_amount",
					"label": "Minimum Order Amount",
					"fieldtype": "Float",
					"insert_after": "display_unit",
					"default": "0",
					"precision": "2",
				}
			]
		}
	)
