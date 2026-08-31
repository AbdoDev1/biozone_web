import frappe

def get_context(context):
    items = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group"],
        filters={"disabled": 0}
    )

    prices = frappe.get_all(
        "Item Price",
        fields=["item_code", "price_list_rate"],
        filters={"price_list": "Standard Selling"}
    )
    price_map = {p["item_code"]: p["price_list_rate"] for p in prices}

    for item in items:
        item["price"] = price_map.get(item["item_code"])

    context.items = items
    return context
