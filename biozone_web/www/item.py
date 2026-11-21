import frappe

from biozone_web.services.store_items import enrich_store_items, get_item_actual_qty
from biozone_web.utils import (
    get_header_context,
    get_storefront_free_shipping_threshold,
    redirect_staff_away_from_store,
)


def get_context(context):
    redirect_staff_away_from_store()

    context.no_cache = 1
    context.active_page = "catalog"
    context.update(get_header_context())
    context.free_shipping_threshold = get_storefront_free_shipping_threshold()

    code = (frappe.form_dict.get("code") or "").strip()
    context.item_code = code
    context.item = None
    context.similar_items = []

    if not code:
        # 404 حقيقي للحالات الواضحة فقط، مع نفس قالب item.html.
        # فرع not enriched أدناه لم يُحسم بعد (بند منفصل).
        context.http_status_code = 404
        return context

    rows = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group", "brand", "stock_uom", "is_stock_item", "description", "image"],
        filters={"item_code": code, "disabled": 0},
        limit_page_length=1,
    )
    if not rows:
        # مجهول أو معطّل: 404 بنفس واجهة "الصنف غير متاح".
        context.http_status_code = 404
        return context

    enriched = enrich_store_items(rows)
    if not enriched:
        # لا نعتبرها وحدها دليل 404: قد يكون إعداد عرض/استثناء داخلي.
        # يُترك كما هو حتى يُحسم التمييز في بند منفصل.
        return context
    context.item = enriched[0]

    # Actual stock is shown on the item page only. A missing warehouse/stock
    # setting must not take the storefront down; the template hides None.
    if context.item.get("is_stock_item"):
        try:
            context.item["actual_qty"] = get_item_actual_qty(code)
        except Exception:
            context.item["actual_qty"] = None

    # Similar products (fallback rule until the staff-picked field lands):
    # same item_group, newest first, excluding the current item.
    # Staff-curated selection overrides this in a follow-up prompt.
    similar = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group", "brand"],
        filters={"disabled": 0, "item_group": context.item["item_group"], "item_code": ["!=", code]},
        order_by="modified desc",
        limit_page_length=8,
    )
    context.similar_items = enrich_store_items(similar)

    return context
