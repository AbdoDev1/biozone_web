import frappe

from biozone_web.utils import (
    get_effective_item_prices,
    get_header_context,
    redirect_staff_away_from_store,
)

PAGE_SIZE = 20
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _to_arabic_digits(number):
    return str(number).translate(ARABIC_DIGITS)


def get_context(context):
    redirect_staff_away_from_store()

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

    # Count via SQL COUNT(*) — never materialize the full code list just
    # to count it. Same filters as the page query below (incl. the OR
    # search), so total_pages clamping is unchanged.
    count_params = {}
    count_where = "disabled = 0"
    if selected_category:
        count_where += " and item_group = %(category)s"
        count_params["category"] = selected_category
    if search_term:
        count_where += " and (item_name like %(term)s or item_code like %(term)s)"
        count_params["term"] = f"%{search_term}%"
    total_count = frappe.db.sql(
        f"select count(*) from `tabItem` where {count_where}",
        count_params,
    )[0][0]
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
    # البند 5: السعر النهائي حسب فئة الطالب (سيرفر-سايد عبر محرك ERPNext)،
    # لا السعر الأساسي الخام — الزائر/غير المفعّل يرى سعر الجمهور، والمفعّل
    # يرى سعر فئته فقط. الأصناف بلا سعر أساسي تبقى price=None (غير متاحة).
    effective = get_effective_item_prices(item_codes)
    # Phase-2 display units: price converted to the customer's group unit;
    # misconfigured (large without conversion) items are hidden, never
    # priced by guess. Missing price still hides the item as before.
    try:
        from biozone_web.units import money2, resolve_items_display
        from biozone_web.utils import get_customer_price_group

        try:
            _group = get_customer_price_group()
        except Exception:
            _group = None
        _display = resolve_items_display(item_codes, _group)
    except Exception:
        _display = {}
    visible = []
    for item in items:
        d = (_display.get(item["item_code"]) or {}) if isinstance(_display, dict) else {}
        if not d.get("ok", True) or not d.get("displayable", True):
            continue
        factor = d.get("factor") or 1.0
        price = effective.get(item["item_code"], {}).get("price")
        item["price"] = money2(price * factor) if price is not None else None
        item["display_uom"] = d.get("uom") or ""
        visible.append(item)
    items = visible
    # المصغرات دفعة واحدة (استعلامان مهما كان عدد البطاقات — بلا N+1).
    # القوالب تستخدم المصغرة حصرًا؛ الأصل الكامل لا يظهر في القوائم أبدًا.
    from biozone_web.services.item_images import resolve_item_thumbnails

    thumb_map = resolve_item_thumbnails([i["item_code"] for i in items])
    for item in items:
        item["thumbnail"] = (thumb_map.get(item["item_code"]) or {}).get("thumbnail") or ""
    context.items = items
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
