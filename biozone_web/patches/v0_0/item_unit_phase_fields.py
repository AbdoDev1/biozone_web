from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"UOM Conversion Detail": [
				{
					"fieldname": "min_qty",
					"label": "Minimum Sale Qty (in this UOM)",
					"fieldtype": "Float",
					"insert_after": "conversion_factor",
					"default": "1",
				}
			],
			"Item": [
				{
					"fieldname": "display_override",
					"label": "Display Override",
					"fieldtype": "Select",
					"options": "\ninherit\nsmall_only\nlarge_only",
					"insert_after": "stock_uom",
					"default": "inherit",
				}
			],
		}
	)
