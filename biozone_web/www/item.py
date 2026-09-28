import frappe

from biozone_web.services.store_items import enrich_store_items
from biozone_web.utils import (
    get_header_context,
    redirect_staff_away_from_store,
)


def get_context(context):
    redirect_staff_away_from_store()

    context.no_cache = 1
    context.active_page = "catalog"
    context.update(get_header_context())

    code = (frappe.form_dict.get("code") or "").strip()
    context.item_code = code
    context.item = None
    context.similar_items = []

    if not code:
        return context

    rows = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group", "brand", "stock_uom", "description"],
        filters={"item_code": code, "disabled": 0},
        limit_page_length=1,
    )
    if not rows:
        return context

    enriched = enrich_store_items(rows)
    if not enriched:
        return context
    context.item = enriched[0]

    # Similar products (fallback rule until the staff-picked field lands):
    # same item_group, newest first, excluding the current item.
    # Staff-curated selection overrides this in a follow-up prompt.
    similar = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group"],
        filters={"disabled": 0, "item_group": context.item["item_group"], "item_code": ["!=", code]},
        order_by="modified desc",
        limit_page_length=8,
    )
    context.similar_items = enrich_store_items(similar)

    return context
