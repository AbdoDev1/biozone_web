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

from biozone_web.services.item_images import resolve_item_thumbnails
from biozone_web.units import money2, resolve_items_display
from biozone_web.utils import get_customer_price_group, get_effective_item_prices


def enrich_store_items(items, customer_group=None):
    """Enrich item dicts with price/display_uom/thumbnail.

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
        price = (effective.get(code) or {}).get("price")
        row["price"] = money2(price * factor) if price is not None else None
        row["display_uom"] = d.get("uom") or ""
        row["thumbnail"] = (thumb_map.get(code) or {}).get("thumbnail") or ""
        enriched.append(row)
    return enriched
