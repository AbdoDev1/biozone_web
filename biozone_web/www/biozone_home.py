import frappe

from biozone_web.utils import get_header_context


def get_context(context):
    context.active_page = "home"
    context.update(get_header_context())

    # Top categories: real item groups, ranked by how many active items
    # they contain. Dynamic on purpose — avoids hardcoding category names
    # that could drift from the actual Item Group data.
    context.top_categories = frappe.db.sql(
        """
        select item_group, count(*) as item_count
        from `tabItem`
        where disabled = 0
        group by item_group
        order by item_count desc
        limit 4
        """,
        as_dict=True,
    )

    # Featured products: first 4 active items (same simple item+price join
    # used by /catalog). Temporary rule — replace with a real "Featured"
    # flag on Item later if a curated selection is wanted instead.
    items = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group"],
        filters={"disabled": 0},
        order_by="creation asc",
        limit_page_length=4,
    )
    item_codes = [i["item_code"] for i in items]
    prices = frappe.get_all(
        "Item Price",
        fields=["item_code", "price_list_rate"],
        filters={"price_list": "Standard Selling", "item_code": ["in", item_codes]},
    )
    price_map = {p["item_code"]: p["price_list_rate"] for p in prices}
    for item in items:
        item["price"] = price_map.get(item["item_code"])
    context.featured_items = items

    return context
