import frappe

from biozone_web.utils import (
    get_effective_item_prices,
    get_header_context,
    redirect_staff_away_from_store,
)


def get_context(context):
    redirect_staff_away_from_store()

    context.active_page = "home"
    context.no_cache = 1
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
    # البند 5: نفس تسعير الفئة المستخدم في /catalog (سيرفر-سايد).
    effective = get_effective_item_prices(item_codes)
    for item in items:
        item["price"] = effective.get(item["item_code"], {}).get("price")
    context.featured_items = items

    return context
