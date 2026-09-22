from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Customer Group": [
				{
					"fieldname": "display_unit",
					"label": "Display Unit",
					"fieldtype": "Select",
					"options": "small\nlarge",
					"insert_after": "is_group",
					"default": "small",
				}
			]
		}
	)
