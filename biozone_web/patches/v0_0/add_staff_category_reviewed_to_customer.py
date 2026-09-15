from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Customer": [
				{
					"fieldname": "staff_category_reviewed",
					"label": "Staff Category Reviewed",
					"fieldtype": "Check",
					"insert_after": "customer_group",
					"default": "0",
				}
			]
		}
	)
