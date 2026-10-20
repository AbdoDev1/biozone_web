"""Shared storefront item enrichment (single source for /catalog + /item).

Replaces the duplicated inline blocks that used to live in
``www/catalog.py`` and ``www/item.py``. Commercial rules are unchanged:

- category price resolved server-side (never trusted from the client),
- display-unit conversion via the units engine (same ``item_group`` rule),
- thumbnails resolved in one batch query (no N+1),
- ``price=None`` passes through (template shows "السعر غير متاح"),
- thumbnail failures never hide an item.

Fail-closed rule: if display-unit resolution itself blows up (bad import,
DB outage, signature change), the whole batch resolves to ``[]`` instead
of falling back to factor ``1.0`` prices that could mislabel the unit.
Callers already treat ``[]`` as "nothing to show" (catalog empty state,
item "غير متاح" state).

Top-level imports are cycle-safe: ``utils`` and ``units`` and
``services.item_images`` import only ``frappe``/stdlib at module level.
"""

import frappe

from biozone_web.services.item_images import derived_url, medium_url_for, resolve_item_thumbnails
from biozone_web.units import money2, resolve_items_display
from biozone_web.utils import (
    get_customer_price_group,
    get_default_warehouse,
    get_effective_item_prices,
)


def enrich_store_items(items, customer_group=None):
    """Enrich item dicts with price/base_price/display_uom/thumbnail.

    Never mutates the caller's dicts — works on shallow copies, so the
    raw ``frappe.get_all`` rows stay pristine for any other consumer.
    """
    codes = [i["item_code"] for i in (items or [])]
    if not codes:
        return []
    effective = get_effective_item_prices(codes)
    try:
        group = customer_group
        if group is None:
            group = get_customer_price_group()
        display = resolve_items_display(codes, group)
    except Exception:
        return []
    try:
        thumb_map = resolve_item_thumbnails(codes)
    except Exception:
        thumb_map = {}
    if not isinstance(display, dict):
        return []

    enriched = []
    for item in items or []:
        row = dict(item)
        code = row.get("item_code")
        d = (display.get(code) or {})
        if not d.get("ok", True) or not d.get("displayable", True):
            continue
        factor = d.get("factor") or 1.0
        pricing = effective.get(code) or {}
        price = pricing.get("price")
        base_price = pricing.get("base_price")
        row["price"] = money2(price * factor) if price is not None else None
        row["base_price"] = (
            money2(base_price * factor) if base_price is not None else None
        )
        row["discount_percentage"] = pricing.get("discount_percentage") or 0.0
        row["display_uom"] = d.get("uom") or ""
        row["image"] = (thumb_map.get(code) or {}).get("image") or ""
        row["thumbnail"] = (thumb_map.get(code) or {}).get("thumbnail") or ""
        row["medium"] = medium_url_for(row["image"])
        row["card"] = derived_url(row["image"], "card")
        row["card2x"] = derived_url(row["image"], "card2x") if row["card"] else ""
        enriched.append(row)
    return enriched


def get_item_actual_qty(item_code, warehouse=None):
    """Return the item's actual Bin balance in the store warehouse.

    Customer-facing ``in stock`` must reflect physical stock, never
    ``projected_qty``. A missing Bin row means zero. The page layer decides
    whether the item is stock-tracked and how to display the value; errors
    from warehouse resolution are handled there so a bad stock setting
    cannot break the storefront.
    """
    code = (item_code or "").strip() if isinstance(item_code, str) else ""
    if not code:
        return None
    warehouse = warehouse or get_default_warehouse()
    qty = frappe.db.get_value(
        "Bin", {"item_code": code, "warehouse": warehouse}, "actual_qty"
    )
    return frappe.utils.flt(qty or 0)
