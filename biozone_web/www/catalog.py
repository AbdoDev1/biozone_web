import frappe

from biozone_web.utils import get_header_context

PAGE_SIZE = 20
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _to_arabic_digits(number):
    return str(number).translate(ARABIC_DIGITS)


def get_context(context):
    context.no_cache = 1
    context.active_page = "catalog"
    context.update(get_header_context())

    search_term = (frappe.form_dict.get("q") or "").strip()
    selected_category = (frappe.form_dict.get("category") or "").strip()

    try:
        page = int(frappe.form_dict.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    page = max(page, 1)

    filters = {"disabled": 0}
    if selected_category:
        filters["item_group"] = selected_category

    or_filters = None
    if search_term:
        or_filters = [
            ["item_name", "like", f"%{search_term}%"],
            ["item_code", "like", f"%{search_term}%"],
        ]

    # Count first (cheap — only item_code) so we know how many pages exist,
    # then clamp the requested page into range before fetching the real rows.
    matching_codes = frappe.get_all("Item", fields=["item_code"], filters=filters, or_filters=or_filters)
    total_count = len(matching_codes)
    total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, total_pages)

    items = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group"],
        filters=filters,
        or_filters=or_filters,
        order_by="item_name asc",
        limit_start=(page - 1) * PAGE_SIZE,
        limit_page_length=PAGE_SIZE,
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

    # Category chip list: every distinct item_group that has active items.
    categories = [
        d.item_group
        for d in frappe.get_all(
            "Item",
            fields=["item_group"],
            filters={"disabled": 0},
            group_by="item_group",
            order_by="item_group asc",
        )
    ]

    context.items = items
    context.categories = categories
    context.selected_category = selected_category
    context.search_term = search_term
    context.total_count_ar = _to_arabic_digits(total_count)
    context.page = page
    context.has_prev = page > 1
    context.has_next = page < total_pages
    context.prev_page = page - 1
    context.next_page = page + 1

    return context
